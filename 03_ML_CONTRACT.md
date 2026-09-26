# 03 — ML CONTRACT

> **Reads:** `00_SHARED_CONTRACT.md` (mandatory).
> **Owner:** ML workstream.
> **Consumer:** Backend only (`01_BACKEND_CONTRACT.md` §6).
> **Shared contract version:** 1.1.0 — additions in this file are marked *(1.1.0)*.

---

## 0. Universal module rules

Every ML module is a class in `ml/` obeying this shape:

```python
class <Module>:
    def __init__(self, config: dict) -> None: ...
    def ready(self) -> bool: ...
    def fallback(self, *args, **kwargs) -> <SameReturnType>: ...
    # + its own primary method(s)
```

**Hard constraints — these prevent the integration failures that kill hackathon merges:**

1. **No I/O.** ML modules never touch Postgres, Redis, the filesystem (except loading model weights at `__init__`), or the network. They take plain Python dicts / NumPy arrays and return plain dicts.
2. **No imports from `backend/`.** The dependency arrow points one way. `ml/` may import from `ml/` and third-party libs only.
3. **Every module exposes `.fallback()`** with an identical return signature to its primary method. The backend calls it on timeout or exception. A fallback is a normal operating state, not an error.
4. **Every return value declares its `source`** where the shared contract defines one (`forecast_source`, `cascade_source`).
5. **Deterministic under a seed.** `config["seed"]` must fully determine output. The demo depends on this.
6. **No exceptions escape.** Catch internally, log, return `self.fallback(...)`. The only acceptable escape is a timeout the backend enforces.
7. **Return dicts matching `00_SHARED_CONTRACT.md` exactly** — same key names, same units, same casing. The backend validates with Pydantic; a mismatch is a build failure.

---

## 1. Module inventory

| Module | File | ML? | Primary method | Report §|
|---|---|---|---|---|
| Forecaster | `ml/forecaster.py` | **Yes** | `predict()` | 8.4 |
| CascadePredictor | `ml/cascade.py` | **Yes** (with deterministic fallback) | `predict()` | 8.1 |
| AssimilatedTwin | `ml/twin.py` | No (classical estimation) | `step()`, `assimilate()`, `branch()` | 8.3 |
| EquilibriumSolver | `ml/equilibrium.py` | No (fixed-point iteration) | `certify()` | 8.2 |
| InterventionOptimiser | `ml/optimiser.py` | No (MILP) | `generate()` | — |
| RiskScorer | `ml/risk.py` | **No — deliberately** | `score()` | 13 |
| AnomalyDetector | `ml/anomaly.py` | **No — deliberately** | `detect()` | 13 |
| SyntheticGenerator | `ml/generator.py` | No | `tick()`, `inject()` | 15 |

**Five of eight use no machine learning. This is the deliberate engineering position from the strategy report §13 — at the decision layer, explainability beats accuracy. Do not "upgrade" RiskScorer or the optimiser to a learned model.**

---

## 2. `Forecaster`

### 2.1 Interface
```python
class Forecaster:
    def __init__(self, config: dict): ...
    def ready(self) -> bool: ...

    def predict(
        self,
        series: dict[str, list[float]],      # entity_id -> utilisation history, oldest first
        capacities: dict[str, float],        # entity_id -> nominal_capacity
        horizons_sec: list[int] = [900, 1800, 3600],
        sim_time: str = None,
        model_prior: dict[str, dict] | None = None,   # (1.1.0) entity_id -> {"now": u, "points": {h: u}}
    ) -> dict[str, dict]:                    # entity_id -> Forecast (SHARED §2.4, minus entity_id key)
        ...

    def fallback(self, series, capacities, horizons_sec, sim_time) -> dict[str, dict]:
        """Persistence: predicted_utilisation = last observed value at every horizon."""
```

### 2.2 Return (per entity — must match SHARED §2.4)
```python
{
  "source": "tsfm",                 # forecast_source enum
  "generated_at": "2026-09-04T14:32:00Z",
  "baseline_value": 0.61,
  "points": [
    {"horizon_sec": 900,  "predicted_utilisation": 0.78, "lower_90": 0.71, "upper_90": 0.86},
    {"horizon_sec": 1800, "predicted_utilisation": 0.94, "lower_90": 0.83, "upper_90": 1.05},
    {"horizon_sec": 3600, "predicted_utilisation": 0.88, "lower_90": 0.70, "upper_90": 1.06},
  ],
  "time_to_critical_sec": 1080,      # or None
  "baseline_comparison": {           # or None if <10 points of history
    "persistence_mae": 0.094,
    "model_mae": 0.061,
    "improvement_pct": 35.1
  }
}
```

**`model_prior` (1.1.0).** The twin's process-model projection (announced plan +
approved actions) at the forecast horizons. When given, the forecast is that
projection corrected by the gap between the latest reading and the model's own
"now", decaying with horizon, and `source = "twin_model"`. A forecaster that does
not accept the argument still works: the backend retries without it.

**`baseline_comparison` (1.1.0, precise definition).** For each entity and for the
`source` that produced the forecast: the mean absolute error of that source's
900 s points against the value observed 900 s later, and of persistence over the
same interval, over the last 30 validated forecasts; `null` until 10 exist.

### 2.3 Model selection ladder
```
len(history) <  10   → source="persistence"   (fallback, always available)
len(history) < warm_start_after_points (40) and tsfm_enabled
                     → source="tsfm"          (Chronos-Bolt-Small, zero-shot)
otherwise            → source="local_model"   (LightGBM / small GRU trained in-run)
```

**Grounded in the research:** Chronos-Bolt is the right zero-shot choice — a distilled variant reported as up to 250× faster and 20× more memory-efficient than Chronos-T5, which is what makes CPU inference viable inside a 300ms budget.

**Mandatory honesty requirement:** `baseline_comparison` is computed on a rolling window and returned **always**, even when the model loses to persistence. Benchmark work shows TSFMs can be beaten by a naïve persistence baseline in sparse/out-of-distribution regimes, and a mega-event surge *is* out-of-distribution. Reporting this is a credibility gain in the demo, not a weakness. **Never suppress an unfavourable `improvement_pct`.**

### 2.4 `time_to_critical_sec` computation
```
critical = config.thresholds.critical_utilisation   # 0.90
Interpolate linearly between forecast points.
Return the first crossing in seconds, or None if no crossing within max(horizons_sec).
```
This single field is the demo's hero number. Its correctness matters more than forecast MAE.

### 2.5 Evaluation
| Metric | Baseline | Target |
|---|---|---|
| MAE @1800s | persistence | ≥25% reduction |
| `time_to_critical_sec` absolute error | — | ≤180s on the demo scenario |
| Cold-start MAE (first 2h) | untrained LSTM | decisive win; report vs persistence honestly |

---

## 3. `AssimilatedTwin`

### 3.1 Interface
```python
class AssimilatedTwin:
    def __init__(self, config: dict): ...
    def ready(self) -> bool: ...

    def step(self, dt_sec: int) -> None:
        """Advance every ensemble member one timestep."""

    def assimilate(self, observations: dict[str, float]) -> dict:
        """EnKF update. observations: entity_id -> observed count.
           Returns TwinFidelity (SHARED §2.8, minus history — backend appends)."""

    def state(self) -> dict[str, dict]:
        """entity_id -> {"current_count", "utilisation", "flow_rate_per_min",
                         "is_observed", "ensemble_std"}"""

    def branch(self, scenario: dict, horizon_sec: int) -> dict:
        """Fork the ensemble, apply a scenario or intervention, roll forward.
           Returns simulation outputs (see §3.5)."""

    def set_drift_mode(self, enabled: bool) -> None:
        """When True, also step an UNCORRECTED ensemble for comparison."""

    def fallback(self, observations) -> dict:
        """Return last-known fidelity with improvement_pct=0.0."""
```

### 3.2 Ensemble Kalman Filter — the algorithm
Grounded in published work coupling an EnKF with an agent-based crowd model, demonstrated on Grand Central Station, where the filter substantially improved accuracy by assimilating data as it evolved.

```
State vector x ∈ R^(2N):  [count_1..count_N, flow_1..flow_N]
Ensemble X = [x^(1) ... x^(m)], m = config.ensemble_size (20)

FORECAST STEP
  for each member i:
      x^(i) ← ABM_step(x^(i), dt) + process_noise
  Apply covariance inflation:  X ← x̄ + λ(X - x̄),  λ = config.inflation_factor (1.05)

ANALYSIS STEP (on observations y, observation operator H selecting observed entities)
  P  = cov(X)
  R  = observation noise covariance (diag, config.obs_noise_var)
  K  = P Hᵀ (H P Hᵀ + R)⁻¹
  for each member i:
      y^(i) = y + N(0, R)                     # perturbed observations
      x^(i) ← x^(i) + K (y^(i) - H x^(i))
```

**Critical implementation notes:**
- **Aggregate state only.** Per-zone counts and flows, ~2×80 = 160 dimensions. **Do not attempt per-agent assimilation** — it will not finish in time and adds nothing to the demo.
- **Covariance inflation is mandatory.** Without it the ensemble spread collapses within ~15 cycles and the filter stops correcting. This is failure mode R4 in the strategy report; budget 2 hours to tune `λ`.
- If `H P Hᵀ + R` is singular, use `np.linalg.pinv`, log a warning, continue.

### 3.3 Drift mode (the demo toggle)
When `set_drift_mode(True)`:
1. Deep-copy the current corrected ensemble → `uncorrected_ensemble`.
2. Both ensembles step each cycle; **only the corrected one is assimilated**.
3. `assimilate()` returns both `assimilated_rmse` and `uncorrected_rmse` against generator ground truth.

Toggling on **resets the uncorrected copy to the current corrected state**, so divergence begins from zero. This is what makes the visual legible within five cycles.

### 3.4 `assimilate()` return
```python
{
  "sim_time": "2026-09-04T14:32:00Z",
  "assimilated_rmse": 41.2,
  "uncorrected_rmse": 118.7,        # None when drift_mode disabled
  "improvement_pct": 65.3,          # None when drift_mode disabled
  "ensemble_size": 20,
  "ensemble_spread": 0.14,
  "drift_mode_enabled": True
}
```

### 3.5 `branch()` return
```python
{
  "branch_id": "sim_4c8e",
  "baseline": {"peak_utilisation": 0.94, "peak_entity_id": "metro_b",
               "load_variance": 0.164, "critical_count": 1},
  "scenario": {"peak_utilisation": 1.12, "peak_entity_id": "gate_3",
               "load_variance": 0.243, "critical_count": 4},
  "new_critical_entities": ["gate_3", "road_4", "emergency_north"],
  "trajectory": {"metro_b": [0.61, 0.68, 0.75, ...]}    # per entity, 6 points
}
```

`branch()` serves three consumers: `/simulate`, the counterfactual for the regret ledger, and the equilibrium solver's ground-truth check.

### 3.6 Evaluation
| Metric | Baseline | Target |
|---|---|---|
| State RMSE @30 sim-min | uncorrected ABM | **≥50% reduction** |
| Ensemble 90% coverage | — | 0.85–0.95 |

---

## 4. `CascadePredictor`

### 4.1 Interface
```python
class CascadePredictor:
    def __init__(self, config: dict): ...
    def ready(self) -> bool: ...

    def predict(
        self,
        root_entity_id: str,
        node_state: dict[str, dict],   # entity_id -> {utilisation, forecast_900, forecast_1800,
                                       #               entity_type, nominal_capacity, degree}
        edges: list[dict],             # GraphEdge dicts (SHARED §2.2)
        max_depth: int = 4,
    ) -> dict:                         # CascadeResult (SHARED §2.5)
        ...

    def predict_all(self, node_state, edges) -> list[dict]:
        """Run predict() for every entity currently in band high|critical."""

    def fallback(self, root_entity_id, node_state, edges, max_depth) -> dict:
        """Deterministic flow propagation. source='deterministic'."""

    # (1.1.0) — what the backend calls (see §4.5)
    def node_risk(self, node_state, edges, generated_at=None) -> dict:
        """{"source": "gnn"|"deterministic", "model_version": str|None, "generated_at": str,
            "calibrated": bool, "topology_match": bool|None,
            "nodes": {entity_id: {"p_fail_900", "p_fail_1800", "p_fail_3600": float, "ttc_sec": int}}}"""
    def node_risk_fallback(self, node_state, edges, generated_at=None) -> dict:
        """Same shape, source="deterministic", nodes={}."""
    def model_info(self) -> dict:
        """{"model_version", "ready", "bundle", "checkpoint_sha256", "norm_sha256",
            "topology_hash", "calibrated", "trained_on", "evaluated_outputs", "error"}"""
```

### 4.2 Deterministic propagator — **BUILD THIS FIRST**

This is the fallback that guarantees a demo exists. It is also, per the strategy report, still a novel *domain* contribution even without the GNN, because the contribution is the heterogeneous cross-domain graph, not the learning method.

```
overflow[root] = max(0, forecast_utilisation[root] - 1.0) * capacity[root]
IF overflow[root] == 0:  overflow[root] = (forecast_util - critical_thresh) * capacity  # pre-emptive

frontier = [(root, 0, overflow[root], eta[root])]
visited  = {root}

WHILE frontier and depth < max_depth:
    (node, depth, load, t) = frontier.pop(0)
    FOR each outbound edge e from node:
        transferred = load * e.transfer_coefficient
        IF transferred / capacity[e.dst] < config.propagation_threshold (0.15): skip
        new_util = forecast_utilisation[e.dst] + transferred / capacity[e.dst]
        eta_dst  = t + e.travel_time_sec
        band     = band_from_utilisation(new_util)
        failure_probability = clamp(new_util / critical_thresh, 0, 0.99)
        IF band in {high, critical} and e.dst not in visited:
            emit step(entity_id=e.dst, predicted_band=band, eta_sec=eta_dst,
                      failure_probability=fp, via_edge_id=e.edge_id, depth=depth+1)
            visited.add(e.dst); frontier.append((e.dst, depth+1, transferred, eta_dst))

SORT emitted steps by eta_sec ASC, reindex step_index from 0
```

**Edge-type transfer semantics** (must be encoded in the seed data, not hardcoded here):

| `edge_type` | Transfer behaviour |
|---|---|
| `feeds` | Full directional transfer at `transfer_coefficient` |
| `adjacent_to` | Bidirectional, halve the coefficient |
| `serves` | Transfer only when dst utilisation > 0.7 (capacity coupling) |
| `last_mile_to` | Transfer with `travel_time_sec` delay, coefficient as given |
| `substitutes_for` | **Negative** transfer — relieves dst (used by the equilibrium solver, not by cascade) |
| `evacuates_to` | Transfer only in emergency scenarios |

### 4.3 GNN upgrade (`source="gnn"`) — attempt only if everything else is done

Grounded in cascade-prediction GNN literature, where GCN models over equipment-node / relationship-edge graphs reach ~97.75% accuracy, and recent work notes that treating cascades as static classification ignores temporal evolution — motivating autoregressive round-by-round rollout.

```
Architecture: R-GCN / HeteroConv (PyTorch Geometric), 2–3 layers, hidden=64
Node features: [utilisation, forecast_900, forecast_1800, capacity_norm,
                type_embedding(9), degree_norm]
Edge features: [edge_type_onehot(6), transfer_coefficient, travel_time_norm, substitutability]
Heads:
  (a) failure_probability per node at horizons {900, 1800, 3600}  → BCE loss
  (b) time_to_critical regression                                  → MSE (masked to failing nodes)
  (c) cascade_severity (downstream failure count)                  → MSE
Rollout: autoregressive, feed round t predictions as round t+1 node features, max_depth rounds
Training data: 5,000 scenarios from SyntheticGenerator (§8)
```

**Swap criterion — do not swap on vibes:** flip `config.cascade.use_gnn = true` only if, on **held-out topologies** (not just held-out scenarios on the same map), the GNN beats the deterministic propagator on both precision and lead-time MAE. Otherwise ship deterministic. The demo is visually identical either way.

### 4.4 Evaluation
| Metric | Baseline | Target |
|---|---|---|
| Mean lead time before critical crossing | threshold rule (0s) | **≥900s** |
| Precision on downstream failure set | random propagation | ≥0.75 |
| Recall | random propagation | ≥0.70 |
| Generalisation | held-out topologies | report separately, never conflate with in-sample |

### 4.5 Integration, bundles and modes *(1.1.0)*

**The published cascade is always the deterministic flow cascade**
(`Backend/app/services/cascade_flow.py`, overflow split over every outbound people
edge). The cascade model contributes per-entity failure probabilities only, through
`node_risk()`. `predict_all()` is kept for drop-ins that lack `node_risk()`.

**Modes** (`config.cascade.gnn_mode`, reported by `/health`):

| Mode | Model called | Published cascades | Persisted |
|---|---|---|---|
| `shadow` (default) | yes | unchanged: `confidence = null`, `ml_enhanced = false` | `ml_node_prediction` every cycle |
| `annotate` | yes | `confidence` = `p_fail_h` at the first horizon ≥ the step's `eta_sec`; never on a topology the model was not trained on (`topology_match == false`) | same |
| `off` | no | unchanged | — |

A model that is not loaded (bundle missing or failing verification) forces `off`.
A model graduates from `shadow` to `annotate` only by the §4.3 swap criterion.

**Bundles.** A model is loaded only from a directory with a `manifest.json` that
binds `model.pt`, `feature_norm.json` and any `eval.json` / `calibration.json` by
SHA-256 (`ML/manifest.py`); no filename guessing. `model_version` =
`<manifest model_version>@<first 8 hex of model.pt sha256>`. If any hash does not
match, `ready()` is False and the backend runs without the model. The manifest also
records the training topology's hash; `node_risk()` reports `topology_match`.

**Calibration.** `calibration.json` = `{"temperature": {"900": T, "1800": T, "3600": T}}`;
probabilities are `sigmoid(logit / T)`. Without it, `calibrated` is false and
probabilities must not be read as frequencies.

**Online evaluation** (backend, `prediction_eval.py`), one definition for every
predictor: an alert is "this gate/road/station/emergency post, not over its critical
line now, will cross it within 3600 s" (model: `p_fail_3600 ≥ gnn_min_probability`;
published cascade: a step projected critical). Precision is over closed alerts,
recall over actual crossings, lead time from first alert to crossing. Reported in
`/metrics` as `cascade_*` and `gnn_*`.

---

## 5. `EquilibriumSolver` — the signature innovation

### 5.1 Interface
```python
class EquilibriumSolver:
    def __init__(self, config: dict): ...
    def ready(self) -> bool: ...

    def certify(
        self,
        intervention: dict,              # Intervention dict (pre-certificate)
        node_state: dict[str, dict],
        edges: list[dict],
        segments: list[dict],            # Segment dicts (SHARED §2.10)
    ) -> dict:                           # Certificate (SHARED §2.6)
        ...

    def solve_leader(self, risk_context, node_state, edges, segments) -> list[dict]:
        """Stackelberg leader: search a discrete incentive grid, return best incentive vectors.
           OPTIONAL — drop this first if time-constrained (see §5.5)."""

    def fallback(self, intervention, node_state, edges, segments) -> dict:
        """Return verdict='UNSTABLE', converged=False,
           reason='Did not converge within iteration cap.'"""
```

### 5.2 The game

Grounded in congestion-game research, where subsidies, tolls and informational nudges are established to shift equilibria in ways that must be *computed rather than assumed*, and in incentive-framework work that explicitly warns a single global incentive offered to every participant is disadvantageous — hence per-segment elasticities rather than one compliance number.

**Leader** (organiser): sets incentive vector **i** over candidate zones.
**Followers** (attendee segments): choose a zone/route/time minimising personal cost.

```
Cost for segment s choosing zone z:
    c_s(z) = travel_time_z / 60
           + α · (load_z / capacity_z)^β                      # congestion disutility
           - incentive_z · price_elasticity_s
           - perk_value_z
           + accessibility_penalty(z, s)                      # ∞ if s constrained and z inaccessible

Defaults: α = config.alpha (1.0), β = config.beta (2.5)
```

**Follower equilibrium — iterative best response:**
```
load ← current utilisation vector
FOR iter in 1..max_iterations (40):
    FOR each segment s:
        compliance_s = compliance_base_rate_s · sigmoid(k · (c_s(current) - c_s(target)))
        moved_s      = population_s · compliance_s
        redistribute moved_s from current zones to argmin_z c_s(z)
    load_new = recompute
    IF ||load_new - load||_inf < convergence_tol (0.005):
        converged = True; BREAK
    IF oscillation_detected(load_history, window=4):
        oscillation_risk = True; BREAK
    load = (1-γ)·load + γ·load_new            # damping, γ = 0.5 (Frank-Wolfe style)
```

`oscillation_detected` = the load vector at iteration *t* is within `tol` of iteration *t-2* but not *t-1*, for two consecutive checks.

### 5.3 Verdict derivation — **defined here once, nowhere else**

Backend and frontend must **never** recompute this.

```python
def derive_verdict(sweep, converged, oscillation_risk):
    if not converged or oscillation_risk:
        return "UNSTABLE"
    verdicts = [row["verdict"] for row in sweep]     # per-compliance-rate
    if all(v == "STABLE" for v in verdicts):  return "STABLE"
    if any(v == "UNSTABLE" for v in verdicts):
        return "UNSTABLE" if verdicts.count("UNSTABLE") >= 2 else "CONDITIONAL"
    return "CONDITIONAL"

def row_verdict(max_utilisation, post_variance, baseline_variance):
    if max_utilisation >= 1.0:                 return "UNSTABLE"   # new critical entity created
    if post_variance > baseline_variance:      return "UNSTABLE"   # made distribution worse
    if max_utilisation >= 0.90:                return "CONDITIONAL"
    return "STABLE"
```

**`compliance_sweep` is always exactly three rows at `[0.4, 0.6, 0.9]`.** Fixed, because the frontend renders three fixed cells and because a sweep is a stronger result than a single point estimate — it directly addresses the risk that our elasticities are invented (strategy report R11).

### 5.4 `reason` string
Generated by the solver, **not the LLM**. Max 140 chars. Templates:
```
UNSTABLE (new critical): "At {rate:.0%} compliance, {entity} exceeds capacity within {mins} minutes."
UNSTABLE (variance):     "Redistribution increases load variance from {base:.3f} to {post:.3f}."
UNSTABLE (oscillation):  "Load oscillates between {a} and {b}; no stable equilibrium."
UNSTABLE (no converge):  "Did not converge within iteration cap."
CONDITIONAL:             "Holds below {rate:.0%} compliance; {entity} reaches {util:.0%} above that."
STABLE:                  "Equilibrium holds across 40–90% compliance; peak {util:.0%} at {entity}."
```

### 5.5 Scope control
If time-constrained, **drop `solve_leader()` and keep `certify()` only.** Certifying operator-proposed interventions retains ~80% of the demo value at ~40% of the effort. The demo moment is the *red badge on the higher-relief option* — it does not require the leader search.

### 5.6 Evaluation
| Metric | Method | Target |
|---|---|---|
| Certificate accuracy | Compare predicted equilibrium vs `twin.branch()` ground truth | within 15% on held-out scenarios |
| Convergence rate | Fraction of certifications reaching `converged=True` | ≥85% |
| Unstable caught | Count of UNSTABLE that a relief-only ranking would have ranked #1 | **report this — it is the argument for the layer** |

---

## 6. `InterventionOptimiser`

### 6.1 Interface
```python
class InterventionOptimiser:
    def __init__(self, config: dict): ...
    def generate(
        self,
        risk_context: dict,      # {root_entity_id, cascade: CascadeResult, node_state, edges}
        max_candidates: int = 5,
    ) -> list[dict]:             # Intervention dicts WITHOUT certificate (backend certifies after)
        ...
    def rank(self, interventions: list[dict]) -> list[dict]:
        """Attach rank_score, sort desc."""
    def fallback(self, risk_context, max_candidates) -> list[dict]:
        """Single 'notify_only' intervention."""
```

### 6.2 Candidate generation
Per `intervention_type`, rule-based templates parameterised by the cascade root and its edges. **This is deliberately not learned** — RL would need interaction data we don't have and would produce policies we can't explain, which is unacceptable under a human-approval gate.

| Type | Generated when | Parameters |
|---|---|---|
| `reroute_transport` | root is `transport_node`/`transport_route` | target = nearest `substitutes_for` neighbour |
| `deploy_shuttle` | a `last_mile_to` edge is congested | count = ceil(overflow / 400) |
| `stagger_entry` | root is `gate` or feeds a gate | delay_sec per segment |
| `gate_redistribution` | ≥2 gates serve the same venue | reassign share |
| `parking_redistribution` | parking utilisation > 0.85 | target lot |
| `zone_incentive` | a zone has utilisation < 0.5 while another > 0.85 | credit_paise, perk |
| `accommodation_rebalance` | hotel cluster saturation | target cluster |
| `emergency_corridor` | an `emergency_facility` appears in the cascade | road IDs to reserve |
| `notify_only` | always (baseline candidate) | — |

### 6.3 Ranking
```python
rank_score = clamp(
    (relief_norm * stability_factor) / max(cost_norm * delay_norm, 0.05), 0.0, 1.0)

relief_norm      = estimated_relief_pct / 100
cost_norm        = 0.5 + 0.5 * (estimated_cost_paise / max_cost_paise)
delay_norm       = 0.5 + 0.5 * (estimated_delay_sec / 1800)
stability_factor = {"STABLE": 1.0, "CONDITIONAL": 0.6, "UNSTABLE": 0.15}[verdict]
```
Weights from `config.optimiser.weights`. Feasibility below 0.3 gates the candidate out entirely.

**Note the `stability_factor`:** this is how the certificate actually changes the ranking rather than just decorating it. A 34%-relief UNSTABLE option scores `0.34 × 0.15 = 0.051`; a 31%-relief STABLE option scores `0.31 × 1.0 = 0.31`. The stable one wins. **That arithmetic is the demo.**

---

## 7. `RiskScorer` and `AnomalyDetector` — deliberately not ML

### 7.1 RiskScorer
```python
class RiskScorer:
    def score(self, node_state, forecast, cascade_exposure) -> dict[str, dict]:
        """entity_id -> {"risk_score": int 0-100, "risk_band": str,
                         "breakdown": [{"risk_type": str, "score": int}]}"""
```

```
base      = 100 * utilisation
growth    = 100 * max(0, forecast_1800 - utilisation) * 1.5
cascade_x = 100 * cascade_exposure          # normalised downstream failure count
score     = clamp(0.5*base + 0.3*growth + 0.2*cascade_x, 0, 100)
band      = low(<=30) | moderate(<=60) | high(<=80) | critical(>80)
```

**Why not learned:** a learned risk score would be unexplainable and unauditable in a safety-critical human-approval system. The operator must be able to ask "why 72?" and get an arithmetic answer. This is a downgrade, not an upgrade — do not change it.

### 7.2 AnomalyDetector
```python
class AnomalyDetector:
    def detect(self, residuals: dict[str, list[float]], z_threshold: float = 3.0) -> list[dict]:
        """-> [{"entity_id", "z_score", "description"}]"""
```
Rolling z-score on forecast residuals. Sufficient, explainable, and fast. Target FP rate <10% at equal recall vs a fixed threshold.

---

## 8. `SyntheticGenerator`

Per the strategy report §15, this is not a workaround — it is simultaneously the training set, the test harness, the demo environment and the evaluation ground truth. Treat it as a first-class deliverable.

### 8.1 Interface
```python
class SyntheticGenerator:
    def __init__(self, config: dict, topology: dict, seed: int = 42): ...
    def tick(self, dt_sec: int) -> dict[str, float]:
        """-> entity_id -> observed count (ground truth)."""
    def ground_truth(self) -> dict[str, dict]:
        """Full true state, for evaluation only. Never exposed via the API."""
    def inject(self, scenario_type: str, params: dict) -> None: ...
    def reset(self, seed: int = None) -> None: ...
    def generate_cascade_dataset(self, n_scenarios: int = 5000,
                                 randomise_topology: bool = True) -> list[dict]:
        """Training data for the GNN. randomise_topology=True is REQUIRED —
           see §8.4."""
```

### 8.2 Arrival process
Non-homogeneous Poisson with a schedule-driven intensity curve:
```
λ(t) = base_rate · pre_event_ramp(t) · lull(t) · exit_spike(t) · weather_factor(t)
```
The **post-event exit spike is where cascades bite** — that is the demo's pressure source. Tune it so `metro_b` crosses critical at roughly T+12 cycles under seed 42.

### 8.3 Injectable disruptions
All `scenario_type` values from `00_SHARED_CONTRACT.md` §1.11. Each modifies capacities or arrival intensities and persists until reset.

### 8.4 Validity discipline — non-negotiable

The generator encodes our assumptions, so a model trained on it learns our assumptions. Mitigations, all mandatory:

1. **Calibrate density thresholds against published crowd-safety guidance**, not invented numbers.
2. **`randomise_topology=True` when generating GNN training data** so the model learns propagation *mechanics*, not one specific map.
3. **Hold out unseen topologies** and report generalisation separately.
4. **Never claim field validity.** Every reported metric is framed as *"in simulation, on held-out topologies."*

Volunteering this limitation is worth more than concealing it. A judge who finds it themselves discounts everything.

---

## 9. Commander tool-grounding (ML side)

The LLM is **not** an ML module in this repo — it is a backend concern (`01_BACKEND_CONTRACT.md` §3.11). ML's only obligation is that every tool the Commander can call maps 1:1 to a value the ML modules actually produced, so the grounding validator can match numbers.

**Rule: no ML module ever generates prose that reaches the user, with exactly one exception — `Certificate.reason`, which is template-generated and deterministic.** This preserves the architectural principle from the strategy report: *ML predicts; optimisation decides; the LLM explains.*

---

## 10. Config reference (`config.yaml` keys owned by ML)

```yaml
seed: 42

forecaster:
  horizons_sec: [900, 1800, 3600]
  tsfm_model: "amazon/chronos-bolt-small"
  tsfm_enabled: true
  warm_start_after_points: 40
  device: "cpu"

cascade:
  use_gnn: false
  max_depth: 4
  propagation_threshold: 0.15
  gnn_artifact: "ML/artifacts/hx_cascade_v2"   # (1.1.0) verified bundle; replaces gnn_checkpoint
  gnn_mode: shadow                             # (1.1.0) off | shadow | annotate (§4.5)
  gnn_min_probability: 0.6                     # alert threshold for online evaluation / annotation

twin:
  ensemble_size: 20
  inflation_factor: 1.05
  obs_noise_var: 25.0
  process_noise_var: 9.0
  drift_mode_enabled: false

equilibrium:
  max_iterations: 40
  convergence_tol: 0.005
  damping: 0.5
  compliance_sweep: [0.4, 0.6, 0.9]
  alpha: 1.0
  beta: 2.5
  sigmoid_k: 3.0

optimiser:
  max_candidates: 5
  min_feasibility: 0.3
  weights: { relief: 0.45, stability: 0.30, cost: 0.15, delay: 0.10 }

risk:
  weights: { base: 0.5, growth: 0.3, cascade: 0.2 }

anomaly:
  z_threshold: 3.0
  window: 20
```

---

## 11. Build order (matches the roadmap)

| Order | Module | Phase | Why this order |
|---|---|---|---|
| 1 | `SyntheticGenerator` | H0–H5 | Everything downstream depends on it |
| 2 | `RiskScorer`, `AnomalyDetector` | H5–H8 | Trivial, unblocks the map immediately |
| 3 | `Forecaster` — persistence only | H5–H8 | The always-available baseline |
| 4 | `CascadePredictor.fallback` (deterministic) | H8–H13 | **The fallback that guarantees a demo** |
| 5 | `Forecaster` — TSFM + local | H10–H13 | Strictly additive upgrade |
| 6 | `AssimilatedTwin` + EnKF | H13–H20 | Highest demo value per hour |
| 7 | `EquilibriumSolver.certify` | H20–H26 | The signature differentiator |
| 8 | `InterventionOptimiser` | H20–H26 | Needs certify() to rank properly |
| 9 | `EquilibriumSolver.solve_leader` | H26–H28 | **First to cut** |
| 10 | `CascadePredictor` GNN | H26+ | **Only if 1–9 complete** |

---

## 12. ML definition-of-done checklist

- [ ] Every module has `ready()` and a working `fallback()` with matching return signature
- [ ] No module imports from `backend/`
- [ ] No module performs I/O beyond loading weights at `__init__`
- [ ] Same seed → byte-identical output across two runs
- [ ] Every return dict validates against the shared schema (CI check)
- [ ] `Forecaster` returns `baseline_comparison` even when it loses to persistence
- [ ] `time_to_critical_sec` error ≤180s on the demo scenario
- [ ] EnKF inflation tuned; ensemble spread does not collapse over 40 cycles
- [ ] Drift mode produces visible divergence within 5 cycles
- [ ] `derive_verdict` exists in exactly one place and nothing else recomputes it
- [ ] `compliance_sweep` is always exactly 3 rows at 0.4 / 0.6 / 0.9
- [ ] Certificate `reason` is template-generated, ≤140 chars, never LLM-written
- [ ] GNN training data generated with `randomise_topology=True`
- [ ] Held-out-topology metrics reported separately from in-sample
