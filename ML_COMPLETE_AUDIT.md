# EventFlow-AI ML Complete Audit

Audit date: 2026-09-27. Scope: the working tree on branch `gaurav`, which is **mid-merge** of `Paresh` (`71f5290`); see §2.
Read-only audit: no source files were changed. The only file added is this report. Runtime checks used a throwaway SQLite DB in the session scratchpad.

Confidence tags: **[CONFIRMED]** means verified directly from code or by running it. **[LIKELY]** means strong evidence but not fully exercised. **[UNKNOWN]** means the repository does not contain enough evidence to decide.

---

## 1. Executive Summary

**The main finding:** EventFlow-AI has a strong deterministic simulation and decision backend. Its only learned model, HX-Cascade v2 (an R-GCN), is loaded and runs every cycle, but its output is reduced to an optional `confidence` number on steps of a *separate, deterministic* cascade. Nothing in the backend acts on that number, and nothing in the frontend displays it. [CONFIRMED]

- **ML subsystem as it exists.** One trained model, `ML/cascade.py` + `ML/hx_cascade_v2.pt`. The other 7 contract modules resolve to deterministic reference code in `Backend/app/ml_reference/`. `ML/` contains no forecaster, twin, equilibrium, optimiser, risk, anomaly or generator module.
- **Does the GNN matter?** Very little. In a 420-cycle live probe (14:00–17:30 sim time):
  - 1,230 of 2,054 downstream cascade steps received a `confidence` value, with a mean of 0.944, so the values are close to saturated.
  - 1,005 further `confidence` values sit on **root** steps. Those are not model output: they are `utilisation / critical` arithmetic that `ML/cascade.py:392` puts on the root step (§7.4).
  - The published `source` was always `"deterministic"`.
- **Honesty and contract defects.**
  - `/health` reports the cascade `active_source` as `"gnn"` (`routes.py:66-71`), while every `CascadeResult` and `/cascade/active` report `"deterministic"`.
  - The forecast source `"twin_model"` was added to the frozen `forecast_source` enum without a version bump.
  - `baseline_comparison` on `twin_model` forecasts measures a *different* model (the trend fit).
- **Training and evaluation of v2 do not meet the ML contract's own rules.**
  - The model was trained and "held out" on the **same single topology**: held-out *scenarios*, not held-out *topologies* (03 §4.3, §8.4).
  - It was compared to a forecast-threshold rule, not the deterministic propagator named in the swap criterion.
  - Its precision at the deployed 0.6 threshold (0.712 / 0.686 / 0.788) is **below** the contract target of 0.75 at 900 s and 1800 s, and below the baseline's precision at every horizon.
  - The TTC head is never evaluated.
  - The swap was decided on average precision (AP) alone.
- **What is actually "intelligent" in the product.**
  - The flow simulator, used as the city, the twin process model, What-If, candidate evaluation and counterfactuals.
  - A localised EnKF.
  - A twin-model forecaster: the nominal-plan projection plus a decaying gap correction.
  - Arithmetic risk scoring and z-score anomaly detection.
  - A rule-based optimiser checked by simulation.
  - A fixed-point equilibrium certifier.

  These are mostly the *right* design choices, and the contract explicitly makes 5 of 8 modules non-ML.
- **Blockers unrelated to ML that affect any ML work.**
  - The frontend has **unresolved merge-conflict markers in 11 files**, so the frontend build cannot succeed.
  - Backend tests pass: **126 passed** in 355 s on Python 3.14 with torch 2.10 CPU.
- **Answer to the special question: yes.** The ML subsystem is a cascade-only implementation, and the single model is not meaningfully integrated even for cascades. The broader backend (forecasting, twin, what-if, routing, accommodation, interventions) runs with zero learned components. For most of those features that is correct and should stay that way. The justified ML expansion is small:
  1. Make HX-Cascade's output real and honest, or demote it.
  2. Build a genuine learned or calibrated forecaster *residual* model against the twin-model baseline, with honest evaluation.
  3. Estimate per-segment compliance online from nudge answers.
  4. Build proper held-out-topology training data.

  **No embeddings are required** by any current feature.

---

## 2. Repository State

| Item | Evidence | Status |
|---|---|---|
| Branch | `git status`: `On branch gaurav`, `You have unmerged paths` | [CONFIRMED] |
| Merge in progress | `.git/MERGE_HEAD` = `71f5290` ("Harden event simulation…", branch `Paresh`); `MERGE_MSG` "Merge branch 'Paresh'" | [CONFIRMED] |
| Unresolved conflicts | `UU` on 11 frontend files; conflict markers present, e.g. `Frontend/src/components/EntityDetailPanel.jsx:179,196,234`, `Frontend/src/routes/Attendee.jsx` (16 markers), `Metrics.jsx` (14), `WhatIfPanel.jsx` (10) | [CONFIRMED] — the frontend cannot build (not executed; JSX with `<<<<<<<` is a syntax error) |
| Backend / ML changes staged | 47 backend/ML files changed, +8304/−1265 (`git diff --cached --stat`) | [CONFIRMED] |
| ML changes from `Paresh` | `0fe613a` changed `ML/cascade.py`; `7a2a9e9` added `hx_cascade_v2.pt`, `feature_norm_v2.json`, `cascade_eval_v2.json` and `Backend/scripts/train_cascade.py` | [CONFIRMED] |
| Change lock | `changelock.md` (committed in `71f5290`, *after* the retrain) forbids "retraining ML", "redesigning R-GCN", "inventing additional AI models" and modifying `ML/` for the hardening pass | [CONFIRMED] — the roadmap in §24 needs the owner to lift this lock |
| Untracked duplicate | `ML/cascade - Copy.py` (`??`) is byte-identical to `git show HEAD:ML/cascade.py` (the v1 integration) | [CONFIRMED] |
| Tests | `python -m pytest tests/ -q` → **126 passed, 884 warnings** (torch/PyG deprecation warnings on Python 3.14) | [CONFIRMED] |
| Env | Python 3.14.3; torch 2.10.0+cpu; torch_geometric 2.8.0.post1; `chronos` **not installed**; CI uses Python 3.12 (`.github/workflows/ci.yml`) | [CONFIRMED] |
| Changelog filename | The file is `changelock.md` (it is a change *lock*); there is no `changelog.md` | [CONFIRMED] |

What expanded ML requirements recently: `7a2a9e9` / `0fe613a` replaced the per-entity curve generator with a flow-coupled city model (`app/ml_reference/city_model.py:10-16` decision record). They added events CRUD, disruptions, accommodation, a projection-based attendee router, candidate evaluation by simulation, the twin-model forecaster and `cascade_flow.py`. **v1 of the GNN (32-entity, curve-generator data) became obsolete as a result**, and v2 was retrained on the new simulator.

---

## 3. Current System Architecture (derived from code)

```
Frontend (React; NO ML logic)                          mock mode: src/mocks/*.json replay
  lib/api.js (REST)  lib/ws.js (WS)  → store/useStore.js → components
        │  renders: forecast.source, baseline_comparison, cascade steps (failure_probability → arc opacity)
        │  never reads: cascade.confidence, cascade.ml_enhanced, /health
        ▼
Backend FastAPI  app/api/routes.py, ws_routes.py  (Pydantic validation on REST only)
        ▼
Engine (services/engine.py) — one cycle every 30 sim-s
  1 _tick_worlds: generator (live) / nominal (plan) / counterfactuals  ← SyntheticGenerator (flow model)
  3 twin.step(nominal delta) + call_ml(twin.assimilate)               ← ml_reference/twin.py (EnKF)
    _merge_states: observed → sensor reading; else twin mean
  4 _model_prior (nominal clone run forward, cached 4 cycles) → call_ml(forecaster.predict)
                                                                       ← ml_reference/forecaster.py ("twin_model")
  5 call_ml(risk.score)                                               ← ml_reference/risk.py (arithmetic)
  6 call_ml(anomaly.detect) on 900s forecast residuals                ← ml_reference/anomaly.py (z-score)
  7 call_ml(cascade.predict_all, fallback=None)                       ← ML/cascade.py (R-GCN)  ─┐
    build_cascades(...) deterministic flow split                      ← services/cascade_flow.py │
    ml_confidence(gnn output) → step["confidence"] only  ◄──────────────────────────────────────┘
    risk.score again with cascade exposure; band hysteresis
  8 optimiser.generate (rules) → evaluate_candidates (clone + run_forward) → equilibrium.certify
    → optimiser.rank → queue;  approve → apply to live + nominal, fork counterfactual; settle at +900s
  9 persist (entity_state; forecast every 10th cycle; NOT cascade_prediction, NOT risk_state) → cache → WS
On-demand: /cascade/{id} (cascade_flow, no ML), /simulate (clones, cascade_flow, optimiser, certify — no ML),
           /attendee/journey (Dijkstra over projection clone — no ML), /accommodation/* (weighted rules — no ML),
           /commander/query (templates / optional Ollama; backend concern per 03 §9)
```

Integration facts:
- ML is imported **in-process** via `app/ml_registry.py:55-78`. It tries `ml.<m>` then `ML.<m>`, then falls back to `app.ml_reference`. No model serving and no HTTP inference endpoint exist, which is the design in 01 §6. [CONFIRMED]
- Inference is **synchronous** in a worker thread, under `asyncio.wait_for` with a latency budget (`ml_registry.py:154-185`). [CONFIRMED]
- Graph construction for the GNN happens inside `ML/cascade.py:_build_graph_tensors` (`:274-333`) every cycle, from `store.node_state_for_ml()` (`state_store.py:159-184`) and `store.edges`. [CONFIRMED]
- Features are built **inside ML**, which is correct per `contracts.md §9`. There is no shared feature code between training (`Backend/scripts/train_cascade.py:151-160`) and inference (`ML/cascade.py:294-311`). They are two hand-kept copies. [CONFIRMED]
- **Hidden coupling:**
  - `cascade_flow.py:24-26` imports the simulator's physical constants.
  - `train_cascade.py:34-41` imports backend app code *and* `ML.cascade.HXCascade`, so ML training cannot run without `Backend/`.
  - `train_cascade.py:273` needs the obsolete v1 `feature_norm.json` in order to write v2.

  [CONFIRMED]

---

## 4. Documentation / Contract Reconciliation

| # | Claim | Source | Actual implementation | Verdict |
|---|---|---|---|---|
| D1 | "Cascade: HX-Cascade R-GCN … deterministic propagator as fallback" | `RUNNING.md` "What is real vs. reference" | The GNN is **not** the cascade producer. `cascade_flow.build_cascades` always produces the cascade, with `source:"deterministic"` (`cascade_flow.py:149`). The GNN only fills `confidence` (`engine.py:315-328`). `ML/cascade.py.fallback()` is never called by the engine (`fallback=None`, `engine.py:318`). | **Contradiction** |
| D2 | `/health` `cascade.active_source` reflects the producer | 01 §3.1, 00 §4 | `routes.py:69` returns `registry.cascade.active_source()` = `"gnn"`, while `/cascade/active.source` = `store.active_cascade_source` = `"deterministic"` (`engine.py:713`). The mock `health.json` says `"deterministic"`. | **Contradiction** (confirmed by probe) |
| D3 | "the registry imports this module … the moment `use_gnn: true` is set" | `ML/cascade.py:3-5`, `ML/README.md:37-41` | The registry imports `ML/cascade.py` whenever the file exists (`ml_registry.py:60-68`). `use_gnn` only gates loading inside the class. | Doc inaccurate (harmless) |
| D4 | Swap justified by `swap_decision_v1.json` | `ML/cascade.py:7-12` | That file evaluates **v1** (32-entity curve generator). v2 has its own `cascade_eval_v2.json` with a different baseline. The docstring is stale. | **Stale** |
| D5 | "Forecaster ladder persistence → tsfm → local_model" | 03 §2.3, 00 §1.9 | There is no TSFM (`chronos` missing; `forecaster.py:43-53`). "local_model" is a hand-coded damped quadratic trend (`forecaster.py:155-204, 224-265`), not a model "trained in-run". The live source is `"twin_model"` (`forecaster.py:147`), which is a new enum value. | **Contract drift** |
| D6 | "Horizons/enum frozen; bump version on change" | 00 §1, §6 | `schemas.py:38` adds `twin_model`; `schemas.py:27-31, 40-45` add intervention/scenario types. `00_SHARED_CONTRACT.md` is still `1.0.0`. | **Contract violation** (additive but unversioned) |
| D7 | Nudge responses "feed the observed compliance rate, which the equilibrium solver uses to refine elasticities" | 01 §3.12 | Compliance is one global Bayesian average (`engine.py:795-801`). It feeds simulation (`apply_intervention`), **not** the solver. Segment elasticities are static (`equilibrium.py:229-266`). | **Not implemented** |
| D8 | `certificate_accuracy` compared to "twin.branch() ground truth" | 03 §5.6; `metrics.py:73,164`; `state_store.py:121` | Compared against the candidate's clone simulation (`engine.py:938-950`). `twin.branch()` (`twin.py:282`) has **no caller**. | Label inaccurate |
| D9 | Held-out **topology** evaluation, `randomise_topology=True` | 03 §4.3, §4.4, §8.4; `ML/README.md:71-84` | v2 trains and tests on the one live topology (`train_cascade.py:195, 202-203, 220-221`). `generate_cascade_dataset` raises `NotImplementedError` (`generator.py:1419`). | **Violated** |
| D10 | "on 20 held-out scenarios it beats a forecast-threshold baseline on AP at every horizon" | `RUNNING.md` | True on AP (`cascade_eval_v2.json`). Omitted: model precision is lower than the baseline at every horizon (0.712 vs 0.951; 0.686 vs 0.923; 0.788 vs 0.923). | Accurate but selective |
| D11 | Persist `cascade_prediction`, `risk_state` | 01 §5 | Tables exist (`db/models.py:97,106`) but are **never written** (no `CascadePrediction(` / `RiskState(` outside models). | **Not implemented** |
| D12 | 03 §10: `gnn_checkpoint: "ml/checkpoints/hx_cascade.pt"`, `use_gnn: false` | 03 §10 | `config.yaml`: `use_gnn: true`, `gnn_checkpoint: "ML/hx_cascade_v2.pt"` | Config diverged from contract (intentional, undocumented in the contract) |
| D13 | "ML/ holds hx_cascade.pt … topology_meta_FINAL.pkl Topology metadata for the GNN" | `README.txt:75-79` | The pkl is never loaded by any code (grep). The v1 checkpoint is only loaded if the config points to it. | Stale docs |
| D14 | `implementationstate.md §9`, `contracts.md §6`: "ML annotates steps with `confidence`" | implementationstate/contracts | Accurate description of the real behaviour, and it contradicts RUNNING.md D1. These two files are the honest source of truth. | Accurate |
| D15 | 03 §3.2 EnKF with `K = P Hᵀ(HPHᵀ+R)⁻¹` (cross-covariance) | 03 §3.2 | Localised per-entity scalar analysis (`twin.py:145-175`), a deliberate and documented deviation | Deviation (justified) |

---

## 5. Current Backend Capability Inventory

**TABLE 1 — Backend Feature Inventory**

| # | Feature | Endpoint(s) / WS | Implementation | Uses ML today? | Needs learned ML? |
|---|---|---|---|---|---|
| F1 | City simulation (demand → flow → queues → egress) | cycle; `/overview`, `/state` | `ml_reference/generator.py` (1424 lines, no RNG) | No (simulator) | No — physics/rules; calibration is a *data* task |
| F2 | Digital twin state estimation | `/twin/fidelity`, `/twin/drift-mode`, `/state/{id}.twin`, WS `twin_fidelity` | `ml_reference/twin.py` EnKF; `engine._merge_states` | Classical estimation | No |
| F3 | Forecast + `time_to_critical_sec` | `/forecast`, `/forecast/pressure-timeline`, WS `forecast_update` | `forecaster.py` twin_model (prior from `engine._model_prior`) | No | **Optional/justified** residual model (see §18) |
| F4 | Risk score / band | `/state`, `/state/{id}.risk_breakdown` | `risk.py` arithmetic + `engine._stabilise_bands` | No (by contract) | **No** (03 §7.1 forbids) |
| F5 | Anomaly detection | WS `anomaly` | `anomaly.py` z-score | No (by contract) | No |
| F6 | Cascade prediction (live) | `/cascade/active`, WS `cascade_alert`/`cascade_update` | `cascade_flow.build_cascades` + GNN `confidence` | Marginal (annotation only) | Yes — the only contract-sanctioned learned model |
| F7 | Cascade on demand | `/cascade/{id}` | `cascade_flow.cascade_for` (`routes.py:181-188`) | **No** (GNN never consulted) | Same model as F6 |
| F8 | Intervention generation | WS `intervention_queued`, `/interventions` | `optimiser.py` rule templates | No (by contract) | No (03 §6.2 forbids RL) |
| F9 | Candidate evaluation | embedded `evaluation` | `services/evaluation.py` (clone + `run_forward`) | No | No |
| F10 | Equilibrium certificate | `/certificates/{id}`, embedded | `equilibrium.py` certify; `solve_leader` returns `[]` (`:379-385`) | No (by contract) | No; *parameters* (elasticities) should be estimated (F16) |
| F11 | Ranking | `/interventions` order | `optimiser.rank` | No | No |
| F12 | Approve / counterfactual / regret | `/interventions/{id}/approve`, `/regret` | `engine.approve_intervention`, `_settle_executing_interventions` | No | No |
| F13 | What-If | `/simulate`, `/simulate/{id}` | `services/simulation.py` clones; cascade via `cascade_for` | No | No (simulation is the right tool) |
| F14 | Events CRUD + reconcile | `/events*`, WS `event_updated`, `state_reconciled` | `services/events.py`, `engine.reconcile` | No | No |
| F15 | Disruptions | `/disruptions*` | `engine.inject_disruption` | No | No |
| F16 | Nudges / compliance | `/attendee/nudges*` | `attendee.issue_nudges`, `engine.current_compliance` (global prior-weighted mean) | No | **Light statistical estimation** justified (per-segment) |
| F17 | Attendee journey | `/attendee/journey`, WS `journey_risk_update` | time-dependent Dijkstra over `PROJECTIONS` (`attendee.py`) | No | No |
| F18 | Accommodation search / recommend / saturation | `/accommodation/*` | weighted 5-factor score (`accommodation.py:1-30`) | No | No |
| F19 | Commander | `/commander/query` | templates / optional Ollama; grounding validator | No (backend concern, 03 §9) | No ML-subsystem work |
| F20 | Metrics / KPIs | `/metrics` | `services/metrics.py` online counters | No | Needs *evaluation infrastructure*, not models |
| F21 | Geo travel | `/geo/travel` | `providers/geo.py` synthetic/OSRM/Google | No | No |
| F22 | Data providers | config `data.provider: file` | `providers/data.py` | No | No — but **it can change the topology under the GNN** (see B9) |

---

## 6. Current ML Capability Inventory

**TABLE 2 — ML Capability Inventory**

| Component | File(s) | Problem | Classification | Notes |
|---|---|---|---|---|
| HX-Cascade v2 (R-GCN) | `ML/cascade.py`, `hx_cascade_v2.pt`, `feature_norm_v2.json` | P(node crosses its critical line within 900/1800/3600 s) + TTC | **PARTIALLY IMPLEMENTED** | Loaded and run every cycle (~10 ms). Output reduced to `confidence`, which is never consumed |
| HX-Cascade deterministic fallback | `ML/cascade.py:471-583` | 03 §4.2 propagator | **DEAD/UNUSED** in production | Engine passes `fallback=None`. Used only by `export_mocks.py:124-126` when no live cascade exists, and by tests |
| HX-Cascade v1 | `hx_cascade.pt`, `feature_norm.json`, `swap_decision_v1.json` | same, 12 features, 32-entity map | **DUPLICATE/OBSOLETE** | Trained on the retired curve generator; the live map has 67 nodes |
| v1 datasets | `cascade_train_FINAL.parquet` (320k rows, 32 "perturbed_*" topologies, 32 entities, 10k scenarios), `cascade_heldout_topology_FINAL.parquet` / `_v1.parquet` (76.8k rows, 8 topologies; different hashes) | v1 training data | **DUPLICATE/OBSOLETE** | Held-out "topologies" share the same 32 entity IDs: they are perturbations of one graph. The training notebook/code is not in the repo, so v1 is **not reproducible** |
| `topology_meta_FINAL.pkl` | pickle: `capacities`, `edges` per `perturbed_*` topology (inspected with `pickletools`, no `GLOBAL` refs) | v1 metadata | **DEAD/UNUSED** | No loader anywhere |
| `cascade - Copy.py` | untracked | — | **DUPLICATE/OBSOLETE** | Identical to HEAD v1 `cascade.py` |
| `ML/__pycache__/cascade.cpython-314.pyc` | — | — | build artifact | Should be gitignored |
| v2 training pipeline | `Backend/scripts/train_cascade.py` | data generation + training + eval | **IMPLEMENTED BUT NOT INTEGRATED** into CI/tests | Seeded; single topology; ~100 scenarios |
| Forecaster | `Backend/app/ml_reference/forecaster.py` | 3-horizon forecast + TTC | **IMPLEMENTED & INTEGRATED** (non-ML) | "tsfm"/"local_model" labels are misnomers (D5) |
| TSFM (Chronos-Bolt) | `forecaster.py:43-53` | zero-shot forecast | **CONTRACT-ONLY** | Package not installed or listed in requirements |
| LightGBM/GRU local model | 03 §2.3, `ML/README.md:3-5` | warm forecast | **DOCUMENTATION-ONLY** | Nothing exists |
| AssimilatedTwin | `ml_reference/twin.py` | EnKF | **IMPLEMENTED & INTEGRATED** | `branch()` DEAD |
| EquilibriumSolver | `ml_reference/equilibrium.py` | certify | **IMPLEMENTED & INTEGRATED** | `solve_leader` stub returns `[]` |
| InterventionOptimiser | `ml_reference/optimiser.py` | candidates + rank | **IMPLEMENTED & INTEGRATED** | rule-based by contract |
| RiskScorer / AnomalyDetector | `ml_reference/risk.py`, `anomaly.py` | arithmetic | **IMPLEMENTED & INTEGRATED** | by contract |
| SyntheticGenerator | `ml_reference/generator.py` | city model | **IMPLEMENTED & INTEGRATED** | `generate_cascade_dataset` raises (`:1419`) |
| Embeddings / retrieval / learned ranking / learned demand / learned travel time | — | — | **absent** | None is required (§13) |

---

## 7. HX-Cascade Deep Audit

### 7.1 What it predicts [CONFIRMED]
- **Model:** `HXCascade` (`ML/cascade.py:85-106`) has `Linear(15→64)`, then 2 × `RGCNConv(64,64,num_relations=6)` with LayerNorm and residual connections, then heads `head_failure: 64→3` and `head_ttc: 64→1` (ReLU).
- **Checkpoints:** the v2 checkpoint shapes match (`input_proj.weight (64,15)`). v1 is `(64,12)`.
- **Label** (`train_cascade.py:124-131`): node *i* has label 1 at horizon *h* if its **ground-truth** utilisation reaches its critical line (venue 1.02, hotel 0.98, else 0.90) within *h* seconds of the snapshot.
- **What it is, in plain terms:** a **graph-contextual per-node threshold-crossing classifier**. It is *not* a propagation model. It does not say which root caused a failure, or through which edge. Its semantics overlap heavily with the forecaster's `time_to_critical_sec`.

### 7.2 Entities, relations, features [CONFIRMED]
- **Entity types:** all 9 as one-hot. Outputs are surfaced only for `cascade_relevant_types` = gate, road, transport_node, emergency_facility (`feature_norm_v2.json`, `ML/cascade.py:429-430`).
- **Relations:** all 6 `edge_type`s as R-GCN relation IDs, including `substitutes_for`.
- **Edge attributes are not used.** `transfer_coefficient`, `travel_time_sec` and `substitutability` are **ignored by the network** (`_build_graph_tensors` builds only `edge_index`/`edge_type`, `:313-331`), even though 03 §4.3 requires edge features. Propagation strength, the core physical quantity, is invisible to the model.
- **Node features (15):**
  - `util` clipped to 3.0
  - `capacity / 70000`
  - type one-hot (9)
  - in-degree over {feeds, last_mile_to, serves, evacuates_to} / 6
  - `forecast_900/1800/3600` clipped to 3.0

  The order matches between training (`train_cascade.py:151-160`) and inference (`ML/cascade.py:294-311`). [CONFIRMED by reading both, plus `test_gnn_uses_forecast_features_when_the_checkpoint_has_them`]
- **Not implemented:** "cascade_severity" head (c), autoregressive rollout, and `type_embedding` (a one-hot is used instead). Disclosed in the docstring (`:14-23`).

### 7.3 Output semantics
- `_run_gnn_forward` (`:335-347`) keeps **only the 3600 s head** (`failure_logits[:, 2]`). The 900/1800 heads are computed and discarded. [CONFIRMED]
- **TTC head:**
  - It is used only to set `eta_sec` of GNN steps (`:422-424`).
  - GNN steps are discarded by the engine, so **TTC is never visible**.
  - TTC was trained only on 3600 s positives with weight 0.2 (`train_cascade.py:236-238`).
  - It was **never evaluated** (no TTC metric in `cascade_eval_v2.json`).

  [CONFIRMED]

### 7.4 Backend integration path [CONFIRMED + probe]
1. `engine.py:315-323`: `call_ml("cascade.predict_all", …, fallback=None, budget 150 ms)`.
2. `ML/cascade.py:203-242`: it selects **its own** roots with `_select_roots` (global `critical=0.9`, `risk_band=="critical"`, no closed-entity exclusion). Then it runs one forward pass and extracts BFS steps with `prob≥0.6` and a physical-support gate.
3. `cascade_flow.ml_confidence` (`:180-189`): per entity, the max `failure_probability` over **all** GNN steps, **including step 0**. The root's step-0 probability is `root_util/critical` arithmetic (`ML/cascade.py:392`), not the model.
4. `build_cascades(..., confidence)`: a separate deterministic cascade with different root selection (per-type critical lines, closed exclusion; `cascade_flow.py:56-66`). `confidence.get(dst)` is attached where entity IDs coincide.
5. **Consumers of `confidence` / `ml_enhanced`:** none in the backend (grep: only `cascade_flow.py`, `schemas.py`) and none in the frontend (grep: no match in `Frontend/src`).

**Probe results** (`scratchpad/probe_engine.py`, 420 cycles, seed 42):

| Metric | Value |
|---|---|
| GNN latency | median 9.7 ms, p95 13.7 ms, max 47.3 ms (budget 150 ms); 0 fallbacks |
| Deterministic downstream steps | 2,054; with GNN `confidence`: 1,230 (60%); mean confidence 0.944 |
| Root steps carrying `confidence` | 1,005 of 1,029 roots. **Not model output** |
| GNN steps whose entity was not in any deterministic cascade | 17 of 1,289 (dropped silently) |
| Published cascade `source` | always `deterministic` |
| `/health` cascade `active_source` | `gnn` |

### 7.5 Topology / normalisation / version consistency
| Check | Result |
|---|---|
| Training topology equals production topology | Yes. Both use `build_topology()` (67 nodes, 127 edges, max cap 70,000, max in-degree 6), matching `feature_norm_v2.json` (`capacity_norm_const 70000`, `max_degree 6.0`). [CONFIRMED] |
| With `data.provider: file` (a different topology) | Degree/capacity normalisation silently drifts. No guard or warning, and the model has never seen another graph. [CONFIRMED by code; impact LIKELY] |
| Norm file selection | `"feature_norm_v2.json" if "v2" in ckpt_name` (`ML/cascade.py:152-153`): a filename heuristic, no hash or version binding between checkpoint and norm. [CONFIRMED] |
| Train/serve feature skew | (a) Training uses **ground truth** `gen.utilisation()` (`train_cascade.py:106,121`). Serving uses the **published** value, which is a noisy sensor reading or the twin estimate (`engine.py:464-477`; `observed_fraction 0.75`, `sensor_dropout_rate 0.03`). (b) Training recomputes the nominal projection at every snapshot. Serving caches it for 4 cycles (`engine.py:523-527`). (c) The serving forecaster clamps to 1.8 (`forecaster.py:138`); training does not. [CONFIRMED by code; magnitude UNKNOWN] |
| Label thresholds vs config | Training hard-codes venue 1.02 / hotel 0.98 / 0.90 (`train_cascade.py:96`), which currently equals `config.yaml thresholds.by_type`. There is no linkage, so they drift if the config changes. [CONFIRMED] |

### 7.6 Evaluation validity
- **Held-out split** (`train_cascade.py:202-203, 220-221`): seeds 1000–1079 train, 1080–1099 test, on the **same graph and same 3 events**. Scenario variation is only 0–3 random disruptions (`:63-87`). This is *held-out scenarios*, not held-out topologies. **Generalisation beyond this one city is untested.** [CONFIRMED]
- **Sample size:** 540 held-out snapshots, about 27 per scenario, strongly autocorrelated. There are no confidence intervals. [CONFIRMED]
- **Baseline** = "own forecast ≥ critical line". Contract 03 §4.3 requires beating the **deterministic propagator** on precision *and* lead-time MAE. Neither comparison is made for v2. [CONFIRMED]
- **Decision rule** (`:280-282`): `use_v2` if AP beats the baseline on ≥2 horizons. At the deployed threshold (0.6, `gnn_min_probability`) the model has lower precision than the baseline everywhere, and is below the 0.75 target at 900/1800 s. [CONFIRMED]
- **Calibration:** training uses `pos_weight` up to 20, which inflates probabilities. There is no calibration step, which matches the probe's mean confidence of 0.944 on emitted steps. Probabilities are **not interpretable as probabilities**. [LIKELY]
- **Online:** `/metrics` `cascade_precision` / `recall` measure the **deterministic** cascade, not the GNN (`engine.py:734-779`).
  - The probe gives precision 0.454 (n=97) and recall 1.0 (n=4).
  - Precision counts `high|critical` as confirmed (`engine.py:743`), while recall uses `critical` (`:764`), so the definitions are inconsistent. [CONFIRMED]

### 7.7 Can it generalise?
Not demonstrated. It was trained on one graph, the network ignores edge coefficients, and there are no held-out topologies. v1 claimed held-out-topology results, but those "topologies" were capacity/coefficient perturbations of one 32-node graph that shared entity IDs. [CONFIRMED]

**Verdict:** HX-Cascade v2 is a working, fast, deterministic inference component. As integrated today it is **decorative**: it has no effect on risk, ranking, interventions, the UI or metrics, and its one visible trace (`/health: gnn`) is misleading.

---

## 8. Digital Twin Audit

**What "digital twin" means here** [CONFIRMED], from `engine.py:19-26`, `implementationstate.md §2`, and `RUNNING.md` item 9:
- **Three simulator worlds:**
  - `generator`: the live "real" city, which includes unannounced disruptions.
  - `nominal`: the announced plan plus approved actions, used as the twin's *process model*.
  - `counterfactuals`: do-nothing forks.
- **An EnKF** (`twin.py`) over `[count_1..N, flow_1..N]` (N=67, m=20). It steps with the nominal world's per-step delta (`engine.py:180-194`), and assimilates sensor observations with localised scalar gains (`twin.py:145-175`).
- **Published state** (`engine._merge_states`): the sensor value if observed this cycle, else the twin's ensemble mean.
- **Layers per entity** (`engine.twin_view`, `schemas.TwinView`): observed, estimated ± std, plan, forecast_1800, counterfactuals.

Properties:
- **Deterministic or probabilistic?** Deterministic under the seed. The simulator has no RNG (`generator.py:34-35`). The EnKF uses a seeded `np.random.default_rng` (`twin.py:41,55`). `test_the_simulation_is_deterministic` and `test_seeded_reset_is_reproducible` pass. [CONFIRMED]
- **ML participation:** none. The twin is classical estimation plus a mechanistic simulator, which is exactly as 03 §1 intends. [CONFIRMED]
- **Fidelity issues:**
  - `ensemble_coverage` = **1.0** in the probe, outside the target 0.85–0.95. The coverage uses a floor of `1.645·obs_noise_rel·|v|` (`twin.py:211`), which with the relative process noise makes the ensemble over-dispersed or coverage near-trivial. [CONFIRMED value; cause LIKELY]
  - RMSE is measured against simulator ground truth (sim-only validity).
  - `uncorrected_rmse` is null unless drift mode is on, so `/metrics twin.rmse.baseline` is null by default.
- **Dead contract surface:** `twin.branch()` has no caller. What-If, evaluation and counterfactuals use simulator clones, which is the better design; the contract (03 §3.5 "branch serves three consumers") is stale.

**Responsibility split (recommendation):**
| Responsibility | Owner | ML? |
|---|---|---|
| Physics of flow, queues, capacity, schedule | Simulation engine (`generator.py`) | No |
| State estimation from sensors | Twin (EnKF) | No (classical) |
| Forecast of the twin forward | Simulation (nominal projection) + a **learned residual correction** | Optional ML (the only twin-adjacent place learning adds value) |
| Calibration of simulator parameters (dwell, service rates, mode split) to real data | Offline calibration job (data/ML workstream) | Statistical fitting, when real data exists |
| Scenario generation for What-If | Simulation (clones) | No |
| Cascade propagation | Deterministic flow (`cascade_flow`) + learned failure probability | ML annotation, once made meaningful |

**A digital twin is not "another ML model" here, and should not become one.**

---

## 9. Backend → ML Capability Matrix

**TABLE 3 — Backend Feature → ML Requirement Matrix**

| Backend feature | Endpoint(s) | Current ML support | Existing model | Missing ML capability | Required input | Required output | Priority |
|---|---|---|---|---|---|---|---|
| Live cascade (F6) | `/cascade/active`, WS | Annotation only, never consumed | HX-Cascade v2 | Honest, calibrated, *used* per-node failure probability; lead-time/TTC evaluated; held-out-topology validity | `node_state_for_ml()`, edges **with attributes** | `{entity_id: {p_fail[900,1800,3600], ttc_sec, calibrated: bool}}` + model_version | **P0** (fix honesty) / P1 (make useful) |
| On-demand cascade (F7) | `/cascade/{id}` | None | — | Same probabilities attached to the on-demand path (consistency with F6) | same | same | P1 |
| What-If cascade (F13) | `/simulate/{id}.cascade` | None | — | Same (run the model on the scenario end state) | scenario end state | same | P2 |
| Forecast (F3) | `/forecast`, WS | None (twin_model rule) | — | Learned residual forecaster over the twin projection, with prediction intervals; honest per-model `baseline_comparison` | per-entity history, twin prior, calendar/event features | Forecast (00 §2.4) + `source:"local_model"` | P1 |
| Risk (F4) | `/state` | None | — | **None: must stay arithmetic** (03 §7.1) | — | — | — |
| Anomaly (F5) | WS `anomaly` | None | — | None required. Evaluation (FP rate) is missing | — | — | P3 |
| Candidates / rank / certify (F8–F11) | `/interventions` | None | — | **None: stays rule/solver** | — | — | — |
| Compliance / elasticities (F10, F16) | `/attendee/nudges/*/respond` | None | — | Per-segment online compliance estimation (Beta-Binomial), fed to `EquilibriumSolver` `segments` and to `apply_intervention` | nudge answers keyed by segment | `{segment_id: compliance_rate, n}` | P2 |
| Twin (F2) | `/twin/*` | None | — | None (classical). Coverage calibration of noise params | — | — | P2 (tuning) |
| Simulation calibration (F1) | — | None | — | Offline parameter fitting when real data exists | real sensor/booking logs | config params | P3 (blocked on data) |
| Journey / accommodation / geo (F17, F18, F21) | `/attendee/journey`, `/accommodation/*` | None | — | **None**: deterministic routing and explainable scoring are correct | — | — | — |
| Metrics (F20) | `/metrics` | n/a | — | Model-specific online eval (GNN vs deterministic), persisted predictions for offline eval | persisted predictions + outcomes | per-model precision/recall/lead-time with n | P1 |
| Commander (F19) | `/commander/query` | n/a | — | None in ML subsystem (03 §9) | — | — | — |

---

## 10. ML Contract Compliance Matrix

**TABLE 4 — ML Contract Compliance** (03 unless noted)

| Contract requirement | Actual implementation | Evidence | Status | Gap → Required change |
|---|---|---|---|---|
| §0.1 No I/O after `__init__` | GNN loads weights and norm in `__init__` only | `ML/cascade.py:147-171` | ✅ | — |
| §0.2 No imports from backend | `ML/cascade.py` imports torch/PyG only | file header | ✅ | (Training script lives in Backend and imports both; acceptable, but ML cannot train standalone) |
| §0.3 `.fallback()` same shape | Present | `ML/cascade.py:244-254` | ⚠️ exists, **never called by the engine** | Engine should pass it, or drop it from the contract for annotation-mode |
| §0.4 Declare `source` | CascadeResult `source` is always "deterministic"; `/health` says "gnn" | `cascade_flow.py:149`, `routes.py:69` | ❌ **inconsistent** | Single source of truth (§21 I-4) |
| §0.5 Deterministic under seed | eval mode, no dropout, seeded; probe stable | `:149,160` | ✅ [LIKELY byte-identical; not diffed across processes] | Add a determinism test for the GNN |
| §0.6 No exceptions escape | try/except around forward and extraction | `:196-201, 224-240` | ✅ | — |
| §0.7 Return dicts match 00 | GNN result is **never validated** (never reaches Pydantic); `confidence` is | `engine.py:315-328` | ⚠️ | Validate the ML output schema at the boundary |
| §1 Eight modules in `ml/` | 1 of 8 in `ML/` | `ls ML` | ❌ (by project choice; README sanctions reference) | Document officially |
| §2.1 Forecaster.predict signature | Extended with `model_prior` kwarg | `forecaster.py:66-73`, `engine.py:508-514` | ⚠️ additive | Put into contract |
| §2.2 `source` ∈ forecast_source enum | `"twin_model"` | `forecaster.py:147`, `schemas.py:38` | ❌ unversioned enum change | Bump 00 to 1.1.0 |
| §2.3 Ladder persistence/tsfm/local_model | No TSFM, no trained local model | `forecaster.py:43-63` | ❌ | Either implement a learned residual model or rename sources honestly |
| §2.3 baseline_comparison always, honest | Present, but measures **the trend model** for twin_model forecasts | `forecaster.py:152, 286-307` | ❌ misattributed | Compute it on the model that produced the forecast |
| §2.4 TTC by linear interpolation | Yes, per-entity critical lines | `forecaster.py:267-284` | ✅ | — |
| §2.5 MAE@1800 ≥25% vs persistence; TTC error ≤180 s | `/metrics` measures MAE@**900** s (probe: 0.0053 vs 0.0367, +85.6%); **TTC error never measured** | `engine.py:506,547-562` | ⚠️ / ❌ | Add @1800 and TTC error |
| §3 Twin interface / EnKF | Localised EnKF; `branch()` unused | `twin.py` | ⚠️ deviation | Update contract |
| §3.6 Coverage 0.85–0.95 | 1.0 in probe | probe | ❌ | Tune noise / fix coverage floor |
| §4.1 CascadePredictor interface | Implemented (+ `generated_at` kwarg) | `ML/cascade.py:188-242` | ✅ | — |
| §4.2 Deterministic propagator | Two copies (`ML/cascade.py`, `ml_reference/cascade.py`) + a third, *different* production algorithm (`cascade_flow.py`) | files | ❌ **triplicated, divergent** | One propagator |
| §4.3 GNN node features incl. forecasts | Yes (15-dim) | `feature_norm_v2.json` | ✅ | — |
| §4.3 Edge features | **Missing** | `:313-331` | ❌ | Add edge-attributed conv (e.g. RGAT/GINE with edge_attr) |
| §4.3 Heads (a)(b)(c) | (a), (b) only; only 3600 head used | `:96-97, 345` | ⚠️ | Use all horizons; add or drop (c) in contract |
| §4.3 Autoregressive rollout | Not done (disclosed) | docstring | ⚠️ | Drop from contract (single-shot is defensible) |
| §4.3 Swap criterion: beat deterministic on held-out **topologies**, precision + lead-time MAE | Not evaluated for v2 | `cascade_eval_v2.json` | ❌ | Re-run eval |
| §4.4 Precision ≥0.75, recall ≥0.70, lead ≥900 s | Offline v2 @0.6: P 0.712/0.686/0.788, R 0.959/0.93/0.912. Online (deterministic cascade): P 0.454 (n=97), R 1.0 (n=4), lead 2070 s (n=4) | json, probe | ❌ precision | Threshold tuning + calibration; larger online samples |
| §8.1 `generate_cascade_dataset(randomise_topology=True)` | Raises NotImplementedError | `generator.py:1419-1424` | ❌ | Implement (topology randomiser) |
| §8.4 Held-out topologies reported separately | No | `train_cascade.py` | ❌ | — |
| §12 DoD "every return validates against schema (CI check)" | `test_ml_contract.py` validates cascade output shape | tests | ✅ (shape only) | Add behavioural/quality gates |
| 01 §2 cascade on timeout → deterministic | `fallback=None` → no annotation (the deterministic cascade still exists) | `engine.py:318` | ✅ in spirit | — |
| 01 §5 persist `cascade_prediction`, `risk_state` | Never written | grep | ❌ | Persist predictions for eval and retraining |
| 01 §3.12 compliance refines elasticities | Not implemented | §4 D7 | ❌ | Per-segment estimator |

---

## 11. ML API / Endpoint Audit

**TABLE 5 — Existing Endpoint Audit (ML-bearing)**

There is **no ML-specific HTTP endpoint**. That is correct: 01 §6 mandates in-process calls, and the audit does not recommend adding an HTTP hop. The endpoints below carry ML-produced or ML-relevant values.

| Route | Method | ML value carried | Producer | Frontend caller | Validation | Issue |
|---|---|---|---|---|---|---|
| `/health` | GET | `modules.cascade.active_source`, `forecaster.active_source` | `registry.cascade.active_source()`, store | `api.health` defined, **never called** | Pydantic | cascade source contradicts the payload (`gnn` vs `deterministic`); `ready()` is always True even if the GNN failed to load (`ML/cascade.py:132-133`) |
| `/cascade/active` | GET | `source`, `steps[].confidence`, `ml_enhanced` | `cascade_flow` + GNN confidence | `api.js:81` | Pydantic | confidence not rendered; root confidence not from the model |
| `/cascade/{id}` | GET | CascadeResult | `cascade_for` (no ML) for non-root entities | `api.js:80` | Pydantic | inconsistent with `/cascade/active` (ML-annotated vs not) |
| `/forecast`, `/forecast/pressure-timeline` | GET | Forecast, `active_source` | forecaster | `api.js:77-79` | Pydantic | `baseline_comparison` misattributed |
| `/state/{id}` | GET | forecast, twin layers, risk breakdown | engine | EntityDetailPanel | Pydantic | displays `forecast.source` and the misattributed MAE (`EntityDetailPanel.jsx:304,319-323`) |
| `/metrics` | GET | cascade precision/recall/lead | engine counters | Metrics page | Pydantic | measures the deterministic cascade, not the model; tiny n; inconsistent definitions |
| WS `cascade_update` / `cascade_alert` / `forecast_update` | WS | same | engine | `lib/ws.js` | **Not Pydantic-validated** (raw dicts, `ws/manager.py:70-80`) | a malformed drop-in model output would reach clients unvalidated |

**TABLE 6 — Missing / Required ML Interfaces** (in-process Python interfaces, not HTTP; see §21)

| Interface | Why (repository evidence) | Caller |
|---|---|---|
| `CascadePredictor.node_failure(node_state, edges) -> {entity_id: NodeRisk}` | The engine only needs per-node probabilities (`ml_confidence`), not the GNN's own BFS cascades, which are discarded | `engine._analyse`, `/cascade/{id}`, `simulation.py` |
| `CascadePredictor.model_info() -> {model_version, checkpoint_sha, norm_sha, trained_on, eval_ref}` | No versioning anywhere; norm chosen by filename heuristic | `/health`, `/metrics` |
| `Forecaster.predict(..., model_prior)` formalised + `baseline_comparison` per producing model | `model_prior` already used (`engine.py:512`) but absent from 03 §2.1 | engine |
| `ComplianceEstimator.update(segment_id, accepted)` / `.estimate()` | 01 §3.12 promise; global-only today (`engine.py:795-801`) | `routes.respond`, `engine.approve_intervention`, `EquilibriumSolver.certify` (segments) |
| `generate_cascade_dataset(n, randomise_topology=True)` (offline, ML workstream) | 03 §8.1; raises today | training pipeline only |

---

## 12. Data & Feature Pipeline Audit

**Cascade pipeline, stage by stage** [CONFIRMED]:

| Stage | Training (`train_cascade.py`) | Inference (live) | Match? |
|---|---|---|---|
| Raw state | `gen.utilisation()` ground truth, every 20th step from k≥30 | `store.entity_states` (sensor or twin estimate), every cycle | ❌ skew (sensor noise/dropout, twin estimate) |
| Forecast features | nominal clone projected at 900/1800/3600 + `(u−now_nom)·e^{−h/1800}`, `max(0,·)` | forecaster: cached prior (≤4 cycles stale) + gap decay, clamp [0,1.8] | ⚠️ near-match, 3 differences |
| Capacity norm | `/ max(caps)` = 70000 | `/ capacity_norm_const` = 70000 | ✅ (single topology only) |
| Degree | in-degree over 4 edge types / max 6 | same | ✅ |
| Clip | `min(·,3.0)` | `util_clip` 3.0 | ✅ |
| Missing values | none possible | `forecast_h is None → util` (`:310-311`) | ⚠️ undocumented imputation |
| Categorical | one-hot by `TYPES` list | one-hot by `entity_type_order` | ✅ same order (hand-duplicated) |
| Edges | all edges; unknown type **crashes** (`EDGE_TYPES.index`) | unknown type skipped | ⚠️ |
| Output transform | — | sigmoid on the 3600 head only; clamp 0.99; ≥0.6 and support gate; max over roots | ⚠️ drops 2 horizons |
| Validation | — | none (GNN output never passes Pydantic) | ❌ |
| Persistence | — | not persisted | ❌ |

**Forecaster pipeline:** `store.history` (published utilisation, 240 points) → `_model_prior` (nominal clone run forward in 60 s steps) → `_forecast_model` → store → REST/WS. Residuals are measured only at 900 s and feed anomaly detection and `/metrics`. [CONFIRMED]

**Normalisation files:** `feature_norm.json` (v1: cap const 89152.07, max deg 3, 12 features) vs `feature_norm_v2.json` (70000, 6, 15). They are incompatible, and there is no checksum binding to the checkpoint. `train_cascade.py:273` *derives* v2 from v1, so deleting the v1 file breaks retraining. [CONFIRMED]

---

## 13. Training & Evaluation Audit

| Aspect | v1 (`hx_cascade.pt`) | v2 (`hx_cascade_v2.pt`) |
|---|---|---|
| Data source | External (Kaggle, per `ML/README.md:3`); parquet only; **training code absent** | `train_cascade.py`, flow simulator |
| Topologies | 32 "perturbed_*" variants of the 32-entity map (same entity IDs) | 1 (the live 67-node map) |
| Scenarios | 10,000 train / 2,400 held-out | 80 train / 20 held-out (2,160 / 540 snapshots) |
| Labels | `failed_{900,1800,3600}`, `eta_sec`, `severity` | crossing critical within h; TTC (3600 cap) |
| Split | by topology (perturbations) | by scenario seed |
| Leakage risk | Low across split. The held-out > train anomaly is documented in `swap_decision_v1.json` | Low across split (split by scenario); temporal autocorrelation within split inflates effective n |
| Baseline | deterministic propagator (P 0.842 / R 0.322; lead MAE 3092 s) | forecast-threshold rule |
| Metrics | AP, precision@R≥0.7, TTC MAE ~204 s | AP, P/R/F1 at 0.6; **no TTC, no lead time, no calibration** |
| Reproducibility | ❌ | ✅ seeded (`torch.manual_seed(42)`, seeds 1000+) [LIKELY; not re-run: long] |
| Dataset versioning | files only | none (regenerated on the fly, not saved) |
| Checkpoint versioning | filename | filename; the norm is chosen by substring `"v2"` |

**Do the reported metrics support the production use?** No.
- The production use is to annotate a deterministic cascade with a probability that no one consumes, on a map the model was trained on, from sensor-derived features it was not trained on.
- Offline AP on autocorrelated snapshots from one map does not establish generalisation.
- Precision at the deployed threshold is below both the baseline and the contract.

---

## 14. Testing Audit

Existing ML-relevant tests [CONFIRMED]:
- `test_ml_contract.py` (13): shape/no-raise/determinism for the resolved modules, with the GNN when `ML/cascade.py` is present.
- `test_hardening.py::test_live_cascades_use_real_edges_and_work_without_ml`.
- `test_product.py::test_gnn_uses_forecast_features_when_the_checkpoint_has_them`: checks tensor width only.
- `test_contract.py::test_cascade_steps_are_ordered…`, `test_cascades_are_physical_readable_and_capped`.

**Testing gap matrix**

| Test type | Exists? | Gap |
|---|---|---|
| Unit: feature builder parity train↔serve | ❌ | No test that `train_cascade.features` == `CascadePredictor._build_graph_tensors` on the same state (they are separate copies) |
| Schema: GNN raw output | ✅ shape | — |
| Inference behaviour: GNN beats trivial baseline on a fixed fixture | ❌ | No quality regression gate |
| Model loading: norm/checkpoint mismatch fails loudly | ⚠️ | Sanity forward exists (`:173-185`). No test that a wrong norm file is rejected; `ready()` hides failure |
| Topology consistency: norm constants vs live topology | ❌ | Needed (data provider can change the topology) |
| Honesty: `/health` source == payload source | ❌ | Would have caught D2 |
| Forecast `baseline_comparison` refers to the producing model | ❌ | Would have caught the misattribution |
| Integration: ML confidence consumed downstream | ❌ | Nothing consumes it; no test asserts purpose |
| Determinism of the GNN across processes | ❌ | — |
| Latency budget regression | ❌ | Probe shows ~10 ms; no guard |
| Training pipeline smoke test (1 scenario, 1 epoch) | ❌ | `train_cascade.py` untested; depends on the v1 norm file |
| Frontend: confidence/source rendering | ❌ | UI ignores both |
| E2E: build of frontend | ❌ currently broken | conflict markers |

---

## 15. Production Readiness Audit

| Area | Finding | Evidence |
|---|---|---|
| Startup / loading | Loads at `MLRegistry()`. On failure it logs and runs deterministic-only, but `ready()` stays True and `/health` `active_source` would say "deterministic". OK. | `ML/cascade.py:147-171` |
| CPU/GPU | CPU-only; ~10 ms/forward on 67 nodes | probe |
| Memory | 242 KB checkpoint; torch + PyG import is heavy (test suite 355 s) | [CONFIRMED sizes] |
| Dependencies | `torch==2.10.0`, `torch_geometric==2.8.0.post1` are mandatory in `requirements.txt` although the product works without them; in CI (Linux) `pip install torch==2.10.0` pulls the default CUDA wheel (multi-GB) | `Backend/requirements.txt`; [LIKELY] |
| Python 3.14 | torch.jit / PyG deprecation warnings (884) | pytest output |
| Concurrency | `call_ml` threads **keep running after timeout** (`asyncio.to_thread` cannot be cancelled). For `twin.assimilate` (budget 800 ms), a late thread can mutate `_X` while the next `twin.step` runs. `evaluate_candidates` (`engine.py:884-891`) mutates candidate dicts in place after a timeout, while they are certified/ranked/broadcast. | `ml_registry.py:167-172`, `evaluation.py:38-62` — [CONFIRMED by code; triggering LIKELY rare: mean cycle 153 ms] |
| Versioning / rollback | None beyond filenames; no `model_version` on any payload | — |
| Logging / monitoring | Logs on exception only; no per-model latency or drift metrics; GNN outputs not persisted | `engine.py:315-328` |
| Validation | WS payloads unvalidated; ML output never validated | `ws/manager.py` |
| Security | `torch.load(..., weights_only=True)` ✅. `topology_meta_FINAL.pkl` is an unused pickle (loading pickles executes code; keep it out of any loader). | `ML/cascade.py:158` |
| Determinism | ✅ | tests |

**Verdict: demo-grade.** It is not production-ready because of the honesty defects, the missing versioning, the unpersisted predictions, the untestable generalisation and the timeout races.

---

## 16. Errors / Bugs / Inconsistencies

**TABLE 8 — Bugs & Inconsistencies**

| ID | Sev | Type | Problem | Evidence | Status |
|---|---|---|---|---|---|
| B1 | Critical | build | Unresolved merge conflict markers in 11 frontend files | `grep -c '<<<<<<<'` (§2) | CONFIRMED |
| B2 | High | honesty/contract | `/health` cascade `active_source="gnn"`; payloads say `deterministic` | `routes.py:69`, `cascade_flow.py:149`, probe | CONFIRMED |
| B3 | High | integration | GNN output only used as `confidence`, which has zero consumers (backend and frontend) | §7.4 | CONFIRMED |
| B4 | High | correctness | Root-step `confidence` is `util/critical` arithmetic presented as ML probability | `ML/cascade.py:392` + `cascade_flow.py:186-188` | CONFIRMED |
| B5 | High | honesty | `baseline_comparison` on `twin_model` forecasts measures the trend model; the UI shows it as the forecast's "edge" | `forecaster.py:152,286-307`; `EntityDetailPanel.jsx:319-323`; probe (−5.9%) | CONFIRMED |
| B6 | High | eval | v2 held-out = same topology; swap criterion not met/evaluated; precision below target and baseline | §7.6 | CONFIRMED |
| B7 | Medium | contract | `twin_model` enum (+ intervention/scenario types) added without a 00 version bump | `schemas.py:27-45`; 00 §1, §6 | CONFIRMED |
| B8 | Medium | model | Edge attributes (coefficient, travel time, substitutability) ignored by the network | `ML/cascade.py:313-331` | CONFIRMED |
| B9 | Medium | topology | `data.provider: file` changes the topology with no normalisation/compatibility guard | `providers/data.py:178-235`, `ML/cascade.py:280-284` | CONFIRMED (code) |
| B10 | Medium | skew | Train on ground truth; serve on sensor/twin estimates; stale prior cache | §12 | CONFIRMED (code) |
| B11 | Medium | output | Only the 3600 s head used; 900/1800 heads discarded; TTC head output never reaches anything; TTC unevaluated | `:345`, §7.3 | CONFIRMED |
| B12 | Medium | concurrency | Timed-out worker threads keep mutating twin state / candidates | §15 | CONFIRMED code / LIKELY rare |
| B13 | Medium | duplication | Three cascade propagators (`ML/cascade.py` fallback, `ml_reference/cascade.py`, `cascade_flow.py`) with different semantics | files | CONFIRMED |
| B14 | Medium | eval | Online precision/recall use inconsistent definitions (high\|critical vs critical); measure the deterministic cascade; n=4 recall | `engine.py:743,764`; probe | CONFIRMED |
| B15 | Medium | persistence | `cascade_prediction`, `risk_state` never written, so no offline evaluation/retraining data from live runs | grep | CONFIRMED |
| B16 | Medium | contract | Compliance does not refine segment elasticities | §4 D7 | CONFIRMED |
| B17 | Low | twin | Ensemble coverage 1.0 (target 0.85–0.95) | probe; `twin.py:211` | CONFIRMED value |
| B18 | Low | versioning | Norm file chosen by `"v2" in ckpt_name`; no hash binding | `ML/cascade.py:152-153` | CONFIRMED |
| B19 | Low | reproducibility | `train_cascade.py` requires the v1 `feature_norm.json` | `:273` | CONFIRMED |
| B20 | Low | dead code | `twin.branch()`, `solve_leader()` (returns `[]`), `ML/cascade.py` deterministic fallback in production, `generate_cascade_dataset` raising, `apply_relief` path only for non-flow generators | grep | CONFIRMED |
| B21 | Low | obsolete artifacts | `hx_cascade.pt`, `feature_norm.json`, `swap_decision_v1.json`, 3 parquet files (~16 MB), `topology_meta_FINAL.pkl`, `cascade - Copy.py`, `__pycache__` | §6 | CONFIRMED |
| B22 | Low | docs | Stale claims (D1, D3, D4, D8, D13) | §4 | CONFIRMED |
| B23 | Low | ready() | `CascadePredictor.ready()` always True | `ML/cascade.py:132` | CONFIRMED |
| B24 | Low | root divergence | GNN root selection (global 0.9, no closed exclusion) differs from `cascade_flow` (per-type lines, closed excluded) | `ML/cascade.py:71-82` vs `cascade_flow.py:56-66` | CONFIRMED |
| B25 | Low | label | "local_model" label on a hand-written trend fit; `certificate_accuracy` baseline named `twin_branch_ground_truth` | `forecaster.py:63`; `metrics.py:164` | CONFIRMED |
| B26 | Low | deps | torch/PyG hard requirements though optional at runtime | requirements | CONFIRMED |

---

## 17. Critical Gaps

**TABLE 9 — Critical Gaps**

| ID | Priority | Type | Problem | Impact | Root cause | Resolution | Depends on |
|---|---|---|---|---|---|---|---|
| G1 | CRITICAL | deployment | Frontend un-buildable (B1) | No product UI in live mode; CI fails | Unfinished merge | Resolve conflicts, `npm run build`, `validate:mocks` | — |
| G2 | HIGH | integration/honesty | GNN has no effect but is advertised (B2–B4) | Misleading `/health` and docs; wasted inference; evaluation claims unsupported | GNN retrofitted as annotation after `cascade_flow` became the producer | Decide: **(a) make it consumed and honest**, or **(b) disable it (`use_gnn:false`)** until it passes the swap criterion | G4 |
| G3 | HIGH | contract/honesty | Forecast `baseline_comparison` misattributed; unversioned enum | Wrong accuracy claims in UI | Forecaster evolved (twin_model) without updating comparison or contract | Compare the producing model; bump 00 to 1.1.0 | — |
| G4 | HIGH | evaluation/data | No held-out-topology data; no propagator comparison; no TTC/lead eval; no calibration | Cannot justify `use_gnn:true` per 03 §4.3 | Single-map training script | Topology randomiser + eval harness | lift change lock |
| G5 | MEDIUM | model | Edge attributes ignored; only one horizon used | Model cannot learn propagation strength | Architecture choice | Edge-attributed conv; expose all heads | G4 |
| G6 | MEDIUM | observability | Predictions not persisted; no model version; no per-model online metrics | No retraining data, no drift detection | Persist step skipped | Write `cascade_prediction` (+ `model_version`), per-model metrics | — |
| G7 | MEDIUM | performance/correctness | Thread-after-timeout races | Rare nondeterminism / corrupted candidates | `asyncio.to_thread` + `wait_for` | Operate on copies; version-stamp results; discard late writes | — |
| G8 | MEDIUM | contract | Per-segment compliance estimation missing | Certificate ignores observed behaviour | Not built | Beta-Binomial per segment | — |
| G9 | LOW | cleanup | Obsolete artifacts, triplicated propagator, dead contract methods, stale docs | Confusion, repo weight | History | Archive/remove; one propagator; update docs | G2 decision |

---

## 18. Required ML Expansion

Guiding rule (03 §1, §7, §9 and `changelock.md`): **explainable arithmetic and simulation at the decision layer; ML only where it predicts something the simulator cannot know.**

**TABLE 10 — Required ML Expansion**

| # | Capability | Tier | Justification (evidence) | Model | Inputs → Outputs | Metric | Failure mode |
|---|---|---|---|---|---|---|---|
| X1 | **Cascade node-failure risk (HX-Cascade v3)** | REQUIRED (fix and justify existing) | 03 §4 is the only sanctioned learned model; currently decorative (B3) | R-GCN → edge-attributed hetero GNN, calibrated (temperature/isotonic) | `node_state_for_ml`, edges with attrs → `{eid: p_fail[3], ttc_sec}` | held-out-**topology** AP/precision/recall vs `cascade_flow`; lead-time MAE; ECE (calibration) | fall back to `confidence=null`, `source` honest |
| X2 | **Forecast residual model** | REQUIRED if the contract's `local_model` claim stays; otherwise rename | 03 §2.3 promises a learned model; the twin_model baseline is strong (probe: 85.6% better than persistence at 900 s), so a learner must beat **twin_model**, not persistence | LightGBM/quantile GBM on residual `(actual − twin_model)` per horizon | history, prior points, gap, entity type, minutes-to-event, flow/queue → corrected points + quantile intervals | MAE@900/1800/3600 vs twin_model; TTC abs error (≤180 s target); interval coverage | fall back to twin_model |
| X3 | **Per-segment compliance estimator** | REQUIRED (small, statistical) | 01 §3.12 unimplemented (B16) | Beta-Binomial with prior from `Segment.compliance_base_rate` | nudge answers + segment → rate, n | posterior calibration vs simulated truth | prior |
| X4 | **Cascade training-data generator with topology randomisation** | REQUIRED (for X1) | 03 §8.1/§8.4; `generator.py:1419` raises | procedural graph perturbation (add/remove nodes/edges, rescale capacities, move events) over the flow simulator | seed → snapshots with labels | dataset stats, topology diversity | — |
| X5 | **Evaluation harness + persisted predictions** | REQUIRED | G4, G6 | — | persisted predictions + outcomes → reports | reproducible JSON reports per model_version | — |
| O1 | TSFM (Chronos-Bolt) zero-shot | OPTIONAL | Contract ladder; likely loses to twin_model in a sim that has a perfect nominal model | — | — | must beat twin_model or not ship | — |
| O2 | Simulator parameter calibration from real data | OPTIONAL / FUTURE | Only when real sensor/booking data exist (`providers/data.py` seam) | Bayesian / least-squares fitting | real logs → config params | fit error | keep defaults |
| O3 | Learned travel times | FUTURE | `providers/geo.py` already delegates to OSRM/Google | — | — | — | — |
| ✗ | Learned risk, anomaly, optimiser, ranking, certification, routing, hotel ranking | **NOT JUSTIFIED** | 03 §6.2, §7 forbid; explainability under a human-approval gate | — | — | — | — |

---

## 19. Target ML Architecture

**TABLE 12 — Current vs Target Architecture**

| Concern | Current | Target (minimum) |
|---|---|---|
| Cascade structure (which entities, via which edges, how many people) | `cascade_flow.py` deterministic | **Unchanged**: remains the producer (it is explainable and physical) |
| Cascade probability / ETA | GNN → `confidence` (unused), root heuristics mixed in | GNN v3 → calibrated `p_fail` per horizon + TTC per node. Attached to **all** cascade paths (live, on-demand, What-If). Consumed by risk *display* (a `cascading` breakdown row) and cascade ordering/filtering where documented. `null` when unavailable |
| Source reporting | contradictory | one field set once per cycle: `cascade.source` ∈ {deterministic, gnn}, where gnn means "deterministic structure + GNN probabilities" (document it), plus `model_version` (additive) |
| Forecast | twin_model (non-ML), misattributed comparison | twin_model baseline + optional residual learner (`local_model`); comparison per producing model |
| Twin | EnKF | Unchanged; tune noise for coverage |
| Compliance | global average | per-segment Beta-Binomial → solver `segments` + simulator |
| Training data | ad hoc in `Backend/scripts` | ML-owned dataset generator using the simulator through a narrow interface; versioned datasets |
| Evaluation | offline JSON, no topology holdout; online measures the wrong thing | harness with held-out topologies + propagator baseline; online per-model metrics from persisted predictions |
| Serving | in-process, `call_ml` | Unchanged (in-process is right at this scale); fix late-thread writes |

---

## 20. Proposed ML Module Structure

Only justified modules are listed:

```
ML/
  __init__.py
  cascade.py                 # KEEP (public drop-in, CascadePredictor) — thin: load + predict + fallback
  forecaster.py              # NEW (only if X2 ships) — Forecaster drop-in wrapping twin_model + residual GBM
  compliance.py              # NEW (X3) — Beta-Binomial per segment (pure python)
  models/
    hx_cascade.py            # MOVE HXCascade nn.Module here (shared by training and inference)
  features/
    cascade_features.py      # NEW — ONE feature builder used by both training and inference (fixes B10/B18 parity risk)
  data/
    topology_randomiser.py   # NEW (X4) — generates perturbed topologies (pure dict in/out)
  training/
    train_cascade.py         # MOVE from Backend/scripts; takes a simulator factory as an argument (no Backend import inside ML)
  evaluation/
    cascade_eval.py          # NEW — held-out-topology eval, propagator baseline, lead-time, calibration
  artifacts/
    hx_cascade_v3/{model.pt, feature_norm.json, eval.json, manifest.json}   # manifest: sha256s, topology hash, dataset seed
  README.md
```

Existing files:
| File | Action |
|---|---|
| `ML/cascade.py` | Keep; slim down. Remove the duplicated deterministic propagator if the engine never calls it, **or** make it the declared fallback and delete `ml_reference/cascade.py` |
| `ML/hx_cascade_v2.pt`, `feature_norm_v2.json`, `cascade_eval_v2.json` | Move into `artifacts/hx_cascade_v2/` with a manifest |
| `ML/hx_cascade.pt`, `feature_norm.json`, `swap_decision_v1.json`, `*.parquet`, `topology_meta_FINAL.pkl` | **Deprecate → archive** (git LFS or release asset); remove from the working tree (but see B19: fix `train_cascade.py:273` first) |
| `ML/cascade - Copy.py`, `ML/__pycache__/` | Remove; add `__pycache__/` to `.gitignore` |
| `Backend/scripts/train_cascade.py` | Move to `ML/training/`; keep a thin Backend wrapper that injects the simulator |
| `Backend/app/ml_reference/cascade.py` | Merge or delete (triplication, B13) |
| `Backend/app/services/cascade_flow.py` | Keep (the production cascade) |

---

## 21. Proposed ML Interfaces / Contracts

In-process Python interfaces, consistent with 01 §6. Every change below is **additive**.

**I-1 `CascadePredictor.node_risk`** (new primary method; `predict_all` kept for compatibility)
```python
def node_risk(self, node_state: dict[str, dict], edges: list[dict], generated_at: str | None = None) -> dict:
    # returns
    {"source": "gnn" | "deterministic",          # which produced the probabilities
     "model_version": "hx_cascade_v3@<sha8>" | None,
     "generated_at": "...Z",
     "nodes": {eid: {"p_fail_900": float|None, "p_fail_1800": float|None, "p_fail_3600": float|None,
                     "ttc_sec": int|None, "calibrated": bool}}}   # only cascade-relevant types; others None
def fallback(...) -> same shape with all values None and source="deterministic"
```
- **Constraints:** probabilities in [0,1] and calibrated; `ttc_sec` integer seconds or null; only entity IDs present in `node_state`.
- **Errors:** an unknown entity type or unknown edge type is skipped and logged. A topology incompatible with the norm manifest makes `ready()` False and triggers the fallback. A NaN triggers the fallback.
- **Caller:** `engine._analyse` → `cascade_flow.build_cascades(confidence=…)` using the **horizon matching each step's `eta_sec`**, never the root heuristic. Also `/cascade/{id}` and `simulation.py`.

**I-2 `CascadePredictor.model_info()`** returns `{"model_version", "checkpoint_sha256", "norm_sha256", "topology_hash", "trained_on", "eval_report"}`. Reported by `/health.modules.cascade` through additive fields.

**I-3 Forecaster** formalises the `model_prior` kwarg (03 §2.1). It adds an optional `components: {"twin_model": [...], "residual": [...]}` for debugging. `baseline_comparison` must compare the **producing** model with persistence at the same horizon.

**I-4 Health / source consistency rule:** `/health.modules.cascade.active_source` MUST equal `/cascade/active.source` for the same cycle. Both are read from `store.active_cascade_source`, which the engine sets from I-1's `source`.

**I-5 `ComplianceEstimator`**
```python
class ComplianceEstimator:
    def __init__(self, config: dict, segments: list[dict]): ...
    def update(self, segment_id: str, accepted: bool) -> None: ...
    def estimate(self) -> dict[str, {"rate": float, "n": int}]: ...
    def fallback(self) -> same, from compliance_base_rate
```
Callers: `routes.respond` (update), and `engine.current_compliance` / `approve_intervention` / `certify(segments=…)` (read).

**I-6 Offline** `generate_cascade_dataset(n_scenarios, randomise_topology=True, seed) -> list[Snapshot]`, where each Snapshot is `{topology, node_state, edges, labels{eid:[3]}, ttc{eid}}`. This runs in the ML workstream only.

---

## 22. Backend vs ML Responsibility Boundaries

| Responsibility | Owner | ML or deterministic? |
|---|---|---|
| Topology, edges, segments | Backend / data provider | Data |
| People flow, queues, capacity, schedule | Simulation (`generator.py`) | **Deterministic** |
| Sensor merge + state estimation | Twin (EnKF) | **Classical estimation** |
| Forecast baseline | Simulation (nominal projection) | Deterministic |
| Forecast correction | ML (X2) | ML (optional) |
| `time_to_critical_sec` | Forecaster | Deterministic interpolation over the forecast |
| Risk score / band / hysteresis | RiskScorer + engine | **Deterministic (contract-mandated)** |
| Anomaly | AnomalyDetector | Deterministic |
| Cascade structure & flow amounts | `cascade_flow.py` | **Deterministic** |
| Cascade failure probability / TTC | HX-Cascade | **ML** |
| Candidate generation | Optimiser | **Deterministic (rule templates, contract-mandated)** |
| Candidate relief estimate | Simulation clones | Deterministic |
| Certificate / verdict | EquilibriumSolver | Deterministic fixed-point |
| Segment compliance parameters | ComplianceEstimator | Statistical estimation |
| Ranking | Optimiser `rank` | Deterministic |
| What-If | Simulation | Deterministic |
| Journeys, accommodation, geo | Backend services / providers | Deterministic |
| Commander phrasing | Backend (templates/LLM) with grounding | Not the ML subsystem |
| Persistence, versions, metrics | Backend (DB, `/metrics`) + ML evaluation harness | — |
| Orchestration, budgets, fallbacks | Engine + `call_ml` | — |

---

## 23. Dependency Graph

```
G1 frontend merge fix ───────────────────────────────────────────────┐ (independent; do first)
P0 honesty fixes (B2,B4,B5,B7) ─► P1 interface I-1/I-2/I-4 ─► X1 integration (consume p_fail)
                                                   ▲
X4 topology randomiser ─► dataset v3 ─► X1 train v3 ─► evaluation harness (X5) ─► swap decision
feature builder unification (B10/B18) ─────────────┘
persist predictions (G6) ─► online per-model metrics ─► monitoring
X3 compliance estimator (independent) ─► solver segments
X2 forecast residual (depends on X5 harness, G6 data)
Change lock (changelock.md) must be lifted by the owner before X1/X4 (it forbids retraining and redesign).
```

---

## 24. Implementation Roadmap

**TABLE 11 — Implementation Roadmap**

| Phase | Task | Why | Files | Depends | Expected output | Validation | Priority | Risk |
|---|---|---|---|---|---|---|---|---|
| 0 | Resolve the 11 frontend conflicts; finish the merge | Build is broken | `Frontend/src/**` (UU list) | — | clean `git status`; build passes | `npm run build`, `npm run validate:mocks` | P0 | Low |
| 0 | Make `/health` cascade source read `store.active_cascade_source` | B2 | `routes.py:66-71` | — | consistent source | new test (I-4) | P0 | Low |
| 0 | Stop propagating root-step heuristic as ML confidence (skip `depth==0` in `ml_confidence`, or have the GNN return model p for root) | B4 | `cascade_flow.py:180-189` | — | only model probabilities | unit test | P0 | Low |
| 0 | Compute `baseline_comparison` for the producing model (twin_model) | B5 | `forecaster.py:128-153, 286-307` | — | honest UI number | unit test | P0 | Low |
| 0 | Decide GNN status: set `use_gnn:false` until X1 passes the swap criterion, **or** keep it with explicit "annotation, unvalidated" labelling | B6 | `config.yaml`, RUNNING.md | owner decision | honest config | review | P0 | Low |
| 0 | Fix late-thread writes: evaluate on candidate copies; ignore results after timeout | B12 | `engine.py:884-891`, `evaluation.py` | — | no post-timeout mutation | stress test with a tiny budget | P1 | Med |
| 1 | Bump 00 to 1.1.0 documenting `twin_model`, new intervention/scenario types, `confidence`/`ml_enhanced` | B7 | `00_SHARED_CONTRACT.md`, `03_ML_CONTRACT.md` | — | contract = code | review | P1 | Low |
| 1 | Update 03: annotation-mode cascade, localised EnKF, `model_prior`, `branch()` status, edge features plan | D3, D15 | `03_ML_CONTRACT.md` | — | — | review | P1 | Low |
| 1 | Add I-1/I-2 (`node_risk`, `model_info`) + manifest/sha binding for norm | B18, B11 | `ML/cascade.py` | — | versioned outputs | tests: wrong norm → ready False | P1 | Low |
| 1 | Persist `cascade_prediction` (+ model_version) and `risk_state` | G6 | `engine._persist`, `db/models.py` | I-2 | data for eval | row counts per cycle | P1 | Low |
| 1 | Fix online cascade metrics: same definition for precision/recall; report GNN vs deterministic separately | B14 | `engine.py:734-779`, `metrics.py` | persistence | per-model metrics | test with fixture | P1 | Low |
| 2 | Attach GNN probabilities to `/cascade/{id}` and What-If cascades | F7/F13 consistency | `routes.py:181-188`, `simulation.py` | I-1 | consistent cascades | test | P2 | Low |
| 2 | Per-segment ComplianceEstimator | B16 | `ML/compliance.py`, `engine.py:795-801`, `routes.respond`, `equilibrium.certify` input | — | 01 §3.12 delivered | unit + integration | P2 | Low |
| 2 | Surface `confidence` in the UI only after calibration (e.g. step tooltip) | B3 | `MapCanvas.jsx`/cascade overlay | X1 | visible, honest | visual check | P2 | Low |
| 3 | Topology randomiser + dataset generator (X4) | G4 | `ML/data/*`, simulator factory | lock lifted | versioned datasets | diversity stats | P2 | Med |
| 3 | Unified feature builder (train = serve), trained on published (noisy/twin) state, not ground truth | B10 | `ML/features/*`, training | X4 | parity | parity test | P2 | Med |
| 3 | HX-Cascade v3: edge attributes, all horizons, calibration | B8, B11 | `ML/models/*` | X4, features | v3 checkpoint + manifest | X5 harness on held-out topologies vs `cascade_flow` | P2 | Med |
| 3 | Forecast residual GBM (X2), only if it beats twin_model | 03 §2.3 | `ML/forecaster.py` | persistence data, X5 | `local_model` source | MAE@900/1800/3600, TTC error ≤180 s | P3 | Med |
| 4 | Tests: feature parity, honesty (I-4), determinism, latency guard, training smoke test | §14 | `Backend/tests/*` | above | CI gates | CI | P1–P2 | Low |
| 4 | Make torch/PyG optional (extras); CPU wheel index in CI | B26 | requirements, CI | — | lighter install | CI time | P3 | Low |
| 4 | Twin coverage tuning | B17 | `twin.py`, config | — | coverage in 0.85–0.95 | `/metrics` | P3 | Low |
| 5 | Archive obsolete artifacts; remove duplicate propagators; update README.txt/RUNNING.md/ML/README.md | B13, B20–B22 | listed in §20 | G2 decision; B19 fix | lean `ML/` | grep for references | P3 | Low |
| 5 | Optional: TSFM trial; simulator calibration on real data | O1, O2 | — | real data | — | must beat baselines | P4 | — |

---

## 25. Prioritized Action Items

1. **Finish the merge**: resolve conflict markers in 11 frontend files, then build. (G1)
2. **Make the ML story honest**:
   - one cascade `source`;
   - no root-heuristic "confidence";
   - `baseline_comparison` for the producing model;
   - correct RUNNING.md, README.txt and the `ML/cascade.py` docstring. (B2, B4, B5, D1, D4, D13)
3. **Decide the GNN's status** while it remains unvalidated: disable it, or label it as annotation-only. (B6)
4. **Version the contracts** (00 → 1.1.0) and **version the model** (manifest + sha binding). (B7, B18)
5. **Persist predictions and fix online metrics**, so the model can be judged on live runs. (B14, B15)
6. **Fix the timeout races.** (B12)
7. **Lift the change lock**, then build the topology-randomised dataset and the evaluation harness. Retrain v3 with edge attributes and calibration, and **swap only if it beats `cascade_flow`** on held-out topologies (03 §4.3). (G4, G5)
8. **Per-segment compliance estimation.** (B16)
9. **Forecast residual learner** only if it beats twin_model. (X2)
10. **Clean up** obsolete artifacts and duplicate propagators. (B13, B20, B21)

---

## 26. Final State Assessment

### Is the ML subsystem fundamentally a cascading-focused implementation that has not expanded to support the broader backend?

**Yes, and it goes further than that.**
- `ML/` contains exactly one model (`ML/cascade.py`), and `ml_registry.py:55-78` resolves the other 7 modules to `app.ml_reference`.
- Since the flow-simulator rewrite (`7a2a9e9`/`0fe613a`), the cascade itself is produced by `services/cascade_flow.py` (`engine.py:326-328`). The GNN was reduced to a `confidence` annotation with no consumer (backend grep; frontend grep).
- Forecasting, the twin, What-If, intervention evaluation, routing and accommodation all run with **zero learned components**. For most of them, that is the correct, contract-mandated design (03 §1, §6.2, §7).

### 1. What ML capabilities already exist?
- An HX-Cascade v2 R-GCN. It is fast (~10 ms per forward pass), deterministic and loaded, and it produces per-node 3-horizon crossing probabilities plus a TTC estimate.
- A v2 training script with seeded data generation.
- Everything else that looks "intelligent" is classical or deterministic: EnKF, simulator-based forecasting and evaluation, the fixed-point solver, and arithmetic risk and anomaly scoring.

### 2. What ML capabilities are missing?
- A validated, calibrated, consumed cascade model.
- Held-out-topology data and an evaluation harness.
- Any learned forecaster: the TSFM and the "local_model" are contract-only.
- Per-segment compliance estimation.
- Model versioning and persisted predictions.

### 3. Which backend features have ZERO ML support?
F1–F5 and F7–F21: simulation, twin, forecast, risk, anomaly, on-demand cascade, candidates, evaluation, certification, ranking, approval and regret, What-If, events, disruptions, nudges and compliance, journeys, accommodation, commander, metrics, geo, and data providers.

### 4. Which backend features require ML but have incomplete support?
- The live cascade (F6), which is annotation-only and unvalidated.
- On-demand and What-If cascades (F7, F13), which do not use the model at all.
- The forecast (F3), where the contract promises a learned ladder that does not exist.
- Compliance (F16), where estimation is promised but only a global average exists.

### 5. Which features should NOT use ML?
- Risk scoring, anomaly detection, candidate generation, ranking, certification and verdicts (all contract-mandated).
- The simulator and twin physics, What-If, journey routing, accommodation ranking, event management, and commander grounding.

### 6. What exact interfaces should ML expose?
In-process only, with no HTTP (§21):
- `CascadePredictor.node_risk` / `.model_info` / `.fallback`
- `Forecaster.predict(…, model_prior)` with honest `baseline_comparison`
- `ComplianceEstimator.update/estimate/fallback`
- An offline `generate_cascade_dataset(randomise_topology=True)`

### 7. What models need to exist?
- **Required:** HX-Cascade v3, meaning edge-attributed, calibrated, and evaluated on held-out topologies.
- **Required (small):** a Beta-Binomial compliance estimator.
- **Conditional:** a residual GBM forecaster.
- **Not required:** embeddings of any kind (§13 below).

### 8. What data and features are needed?
- A topology-randomised simulator dataset, built from published (noisy or twin-estimated) state rather than ground truth.
- Node features: the current 15, plus queue and flow fields.
- **Edge features:** `transfer_coefficient`, `travel_time_sec`, `substitutability`.
- Per-horizon labels, and TTC labels.
- Persisted live predictions with outcomes.
- Nudge answers keyed by segment.
- For the forecaster: the history, the twin prior, minutes-to-event, flow and queue.

### 9. How should HX-Cascade evolve?
1. Keep `cascade_flow` as the structural producer.
2. Turn the GNN into a calibrated per-node risk-and-TTC provider that is attached to every cascade path, uses all horizons, and has consistent, honest `source` and version fields.
3. Retrain on randomised topologies with edge attributes and serve-time-realistic features.
4. Swap only if it beats `cascade_flow` on precision and lead time on held-out topologies. Otherwise run with `use_gnn:false`.

### 10. What must be fixed before adding new models?
1. The frontend merge conflicts.
2. The honesty defects: B2, B4 and B5.
3. The contract version bump (B7).
4. Model/norm version binding (B18).
5. Persisted predictions and corrected online metrics (B14, B15).
6. The timeout races (B12).
7. Lifting the change lock in `changelock.md`.

### 11. What is the minimum viable ML expansion?
- The Phase 0–1 fixes.
- The I-1/I-2/I-4 interfaces.
- Per-segment compliance (X3).
- A topology randomiser, an evaluation harness and a v3 retrain (X1, X4, X5).

No new model families.

### 12. What is the long-term architecture?
- A deterministic simulator and EnKF twin as the backbone.
- Two narrow learned components: a cascade node-risk GNN and a forecast residual model. Each is versioned, calibrated, evaluated on held-out topologies or periods, persisted, and monitored online per model.
- Statistical estimation of behavioural parameters (compliance, and later simulator calibration once real data flows through `providers/data.py`).
- Decision logic stays arithmetic, rule-based and solver-based, as the contract intends: *ML predicts; optimisation decides; the LLM explains* (03 §9).

---

### §13 (Embedding audit)

**No current feature requires embeddings.**

| Embedding type | Required? | Reason |
|---|---|---|
| Entity / location / topology / graph | No | The only graph learner (HX-Cascade) learns its own hidden states internally. Nothing retrieves or compares entities by similarity. Routing uses the explicit graph, and accommodation uses explicit factors. |
| Event | No | Events are fully structured (`services/events.py`). |
| Semantic text | No | The Commander uses deterministic intent planning and templates (`commander.py`). Retrieval over free text does not exist and is not in any contract. |
| Temporal | No | Temporal features belong inside the forecaster (X2) as ordinary features. |
| Multimodal | No | There are no image or audio inputs. |

Revisit this only if a free-text incident intake or a retrieval-based Commander is added. No current contract requires either.

### Appendix — TABLE 7 Model/Data/Feature Dependency Matrix

| Model | Artifact | Norm | Training data | Code (train) | Code (serve) | Config key | Consumers |
|---|---|---|---|---|---|---|---|
| HX-Cascade v2 | `ML/hx_cascade_v2.pt` (sha 745b2205ec4f) | `feature_norm_v2.json` (selected by the `"v2"` substring) | regenerated in memory by `train_cascade.py`, not saved | `Backend/scripts/train_cascade.py` (+ `feature_norm.json` v1 as template) | `ML/cascade.py` | `cascade.use_gnn`, `gnn_checkpoint`, `gnn_min_probability` | `engine._analyse` → `cascade_flow` `confidence` (no downstream consumer) |
| HX-Cascade v1 | `ML/hx_cascade.pt` (sha 9464c2eb5928) | `feature_norm.json` | `cascade_train_FINAL.parquet`, `cascade_heldout_topology_{FINAL,v1}.parquet`, `topology_meta_FINAL.pkl` | absent (external notebook) | `ML/cascade.py` if configured | — | none (obsolete) |
| Forecaster (non-ML) | — | — | — | — | `ml_reference/forecaster.py` | `forecaster.*` | engine, risk, cascade roots, interventions, UI |
| Twin (non-ML) | — | — | — | — | `ml_reference/twin.py` | `twin.*` | engine merge, `/twin/*`, metrics |

---

*Evidence artifacts used for runtime claims (scratchpad, not in the repo): `probe_engine.py` (420-cycle engine probe), pytest run (126 passed), checkpoint shape/hash inspection, parquet schema inspection, `pickletools` inspection of the `.pkl` (not unpickled).*
