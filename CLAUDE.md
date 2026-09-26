# EventFlow AI — Claude Project Context

Operating context for Claude Code sessions. Everything here was verified against the code on
branch `Yash` at commit `bab9bf5` (2026-09-25, the night before the hackathon). **Code beats docs:**
where `README*.md`, `RUNNING.md` or the `0x_*_CONTRACT.md` files disagree with the code, this
file describes the code and flags the difference (⚠).

---

## 1. Project Identity

| | |
|---|---|
| Name | EventFlow AI — "predictive event-flow orchestration" |
| Nature | Hackathon demo. A **seeded synthetic simulation** of one stadium event, not a system connected to real sensors |
| Scenario | "National Cup Final", `evt_demo`, 70,000 expected, venue `stadium_main` (Mumbai coordinates 19.076, 72.8777). Sim clock starts `2026-09-04T14:00:00Z`, kickoff 16:00Z |
| Workstreams | `Backend/` (FastAPI + orchestration engine), `Frontend/` (Vite + React operator UI), `ML/` (trained HX-Cascade GNN) |
| Contracts | `00_SHARED_CONTRACT.md` … `03_ML_CONTRACT.md` (v1.0.0) + generated `contracts/schemas/*.json` |

## 2. Problem Statement

Large events fail through **cascades**: a crowded metro station overloads a gate, the gate spills
onto a road, the road blocks an emergency post. Operators react to the first failure after it
happens. EventFlow tries to (a) forecast which entity crosses critical load and when, (b) predict
how that spreads across the venue graph, (c) recommend interventions, and (d) check whether an
intervention actually holds once attendees respond to it (a naive reroute can just move the jam,
e.g. rerouting Metro B riders to Metro C saturates `gate_5`).

Users: an **operator** (Command Centre) who approves/rejects actions, and an **attendee** (mobile
PWA view) who gets route advice and incentive nudges.

## 3. Product Objective (what the demo is meant to show)

1. A calm opening, then an entity (hero: `metro_b`) projected to go critical ~minutes ahead.
2. A cascade prediction across the graph from that root.
3. Ranked intervention cards, each with an **equilibrium certificate** (STABLE / CONDITIONAL /
   UNSTABLE) — the highest-relief option can be UNSTABLE and ranked below a safer one.
4. Human approval that **actually changes the simulation**, measured later against a do-nothing
   counterfactual (regret ledger).
5. A Digital Twin (EnKF) whose value is shown by a drift toggle (assimilated vs uncorrected RMSE).
6. A grounded "Commander" Q&A where every number traces to a tool call.
7. A `/metrics` judging page where every metric carries a baseline.

Differentiators the code genuinely implements: equilibrium certificate + rank demotion, real
state-changing approval with counterfactual settlement, EnKF assimilation with drift comparison,
trained R-GCN cascade model with deterministic fallback, backend-side number grounding.

## 4. Core System Flow (as implemented)

```
SyntheticGenerator.tick()            deterministic logistic crowd curves (ground truth), 75% of
   │                                 entities "observed" with ±1.5% deterministic sensor noise
   ▼
AssimilatedTwin.step() + assimilate()  EnKF: advance ensemble (+process noise, inflation 1.05),
   │                                   then Kalman-correct against observations
   ▼
Engine._merge_states                 observed → use observation; unobserved → twin mean estimate
   ▼
Forecaster.predict                   900/1800/3600s utilisation + 90% band + time_to_critical_sec
   ▼
RiskScorer.score (pass 1)            0–100 score + band
AnomalyDetector.detect               z-score on forecast residuals
CascadePredictor.predict_all         HX-Cascade GNN (one forward pass) → per-root step chains
RiskScorer.score (pass 2)            re-score with cascade exposure
   ▼
IF any entity time_to_critical ≤ 3600s (most urgent root, no live proposal for it, <8 live):
   InterventionOptimiser.generate → EquilibriumSolver.certify (each) → optimiser.rank
   ▼
expire proposals past TTL → settle executing interventions ≥900s old → regret entries
   ▼
persist (SQLite) → cache → WebSocket broadcast → cycle metrics / backpressure
                                                         │
Operator (UI) ── approve ──► generator transfers from action_effects (source→destination, §33)
                            fork + nudges → 900 sim-s later: realised vs counterfactual relief
```

Pipeline stages from the prompt that **do not exist as separate systems**: no real data
ingestion, no execution against real-world systems, no learned feedback loop (observed nudge
compliance is recorded but never fed back into the solver — ⚠ `routes.py` comment claims it is).

## 5. Architecture

```
Frontend (React, Zustand store)  ──REST /api/v1 + WS /ws (Vite proxy in dev)──►  FastAPI (app/main.py)
                                                                                   │
                                          api/routes.py, api/ws_routes.py  (read store, few mutations)
                                                                                   │
                                   services/engine.py  Engine: clock, cycle loop, ONLY writer of state
                                         │                     │                      │
                        services/state_store.py       app/ml_registry.py         db/ (SQLAlchemy)
                        in-memory authority           MLRegistry + call_ml()     SQLite eventflow.db
                                                              │
                                  ML/cascade.py (real GNN)  +  app/ml_reference/*.py (7 reference modules)
```

Boundaries:
- **ML ← Backend ← Frontend** only. `ML/` imports nothing from `Backend/` (its deterministic
  fallback is a self-contained copy). Backend never imports frontend.
- ML modules receive/return plain dicts; Pydantic (`app/schemas.py`, `extra="forbid"`) validates
  everything before it reaches the wire.
- External services: none required. Optional Postgres (`DATABASE_URL`), Redis (`REDIS_URL`),
  Ollama (`LOCAL_LLM_URL`). None are installed/configured in the current venv.

## 6. Repository Structure

```
00_SHARED_CONTRACT.md … 03_ML_CONTRACT.md   frozen contract set (types, endpoints, WS, ML interfaces)
README.md            contract-set overview + team rules (not a product README)
README.txt           long product README (mostly accurate; see ⚠ notes below)
RUNNING.md           run instructions + real-vs-reference table
requirements.txt     copy of Backend/requirements.txt
contracts/schemas/   28 JSON Schemas exported from Pydantic (committed)
.github/workflows/ci.yml   backend tests → export schemas → validate mocks → vite build
Backend/             FastAPI app, engine, reference ML, tests, scripts, config.yaml
Frontend/            Vite React app, mocks, validation scripts
ML/                  cascade.py (HX-Cascade), hx_cascade.pt, feature_norm.json, training artifacts
```

## 7. Frontend

**Stack:** Vite 6, React 18, React Router 6, Zustand 5, Tailwind 3, deck.gl 9 (no basemap),
Recharts 2, lucide-react. Entry `index.html` → `src/main.jsx` → `src/App.jsx`. Dev port 5173.

**Theme gotcha (important for any UI edit):** `tailwind.config.js` **redefines** `surface-*` as
beige/cream (`surface-950` = `#F5EFE6` page bg) and **inverts** `slate-*` (`slate-200` =
`#1e293b` dark text, `slate-900` = light). Class names that look dark-theme render a **light beige
theme**. `Frontend/test.css` (53 KB compiled Tailwind) is not imported anywhere — stray artifact.

**Bootstrap (`App.jsx`, runs once):** `api.event()` + `api.graph()` → store. Then mock mode →
`startMockDriver(store)`; live mode → REST seed (`/state`, `/forecast/pressure-timeline`,
`/interventions?status=proposed&limit=10`, `/cascade/active`) then
`connectWebSocket(store, {client:'command_centre'})`. One WS for all routes (the attendee view does
not open its own `client=attendee` socket).

**Network layer** (`src/lib/`):
| File | Role |
|---|---|
| `api.js` | Only HTTP caller. `MOCK_MODE = VITE_MOCK !== '0' \|\| ?mock=1`. Every method routes to `mocks.js` in mock mode. Normalises the error envelope into `ApiError` (`isWarmingUp` for `MODEL_NOT_READY`/`INSUFFICIENT_HISTORY`, which are never toasted) |
| `ws.js` | Drops `seq <= lastSeq`; backoff 1/2/4/8s with `wsStatus` → amber TopBar chip; sends `{action:'resync', last_seq}` on every open; maps events → store actions |
| `mocks.js` | Serves `src/mocks/*.json` with 120 ms latency; mutable approve/reject/nudge copies; `startMockDriver` ticks every 900 ms |
| `mockLifecycle.js` | Replays `state_sequence.json` (**100 frames** — ⚠ comments/docs say 90) in a loop; arms cascades at frame 8, interventions at frame 10; `resetSimState()` at each loop wrap |
| `actionEffects.js` / `useActionTracking.js` | Post-approval HUD: T0 snapshot vs live entities, ≤2-hop downstream easing, cascade before/after (re-fetches `GET /cascade/{root}` each cycle), phase from sim cycles (settle = 30 cycles), `GET /regret` fallback |
| `colors.js`, `format.js`, `labels.js` | Band/verdict colours keyed on API strings; formatting only; map label declutter |

**Store** (`src/store/useStore.js`): `mergeEntities` merges deltas by `entity_id` (never replace);
`replaceAll` (resync) is the only full replace; `upsertIntervention` re-sorts by backend
`rank_score` only; `loadVarianceDelta` is the one permitted display computation. Also holds
tracked actions, what-if overlay, commander messages, toasts, `anomalies` (stored, **never
rendered**).

**Screens:**
| Route / component | What it shows | Data source | Actions |
|---|---|---|---|
| `/` `CommandCentre.jsx` | Layout: TopBar; map + right sidebar (Commander, What-If); bottom row (Pressure Timeline, Intervention Queue, Twin) | store | — |
| `TopBar` | event name, RECORDED REPLAY / LIVE SIMULATION badge, sim clock, kickoff countdown, cycle (live: real count; mock: frame mod 100), overall risk band+score, load variance + 5-cycle delta arrow, crit/high counts, WS chip, links | `tick`, `event` | nav to `/metrics`, `/attendee` |
| `MapCanvas` | 66 nodes (radius ∝ capacity, colour = `risk_band`, weak fill + white ring = unobserved twin estimate), `feeds` edges only, glow on high/critical, hover tooltip, cascade arcs revealed 400 ms/step with ETA labels, action rings (teal), what-if rings/arcs (cyan), green "eased" downstream edges | `graph`, `state_update`, cascades, tracked actions | click node → select |
| `EntityDetailPanel` | load, risk, headcount/capacity, net flow, forecast chart with 90% band + `baseline_comparison`, risk breakdown, in/out edges | `GET /state/{id}` (on-demand) | "Show Cascade Propagation Lines" → `GET /cascade/{id}`; click edge → navigate |
| `PressureTimeline` | entities forecast critical ≤3600s in backend order, sparkline over 6 offsets, "min to critical"; first row is hero; forecast-source badge | `forecast_update` | click → select entity |
| `InterventionQueue` / `InterventionCard` | proposed/executing cards in `rank_score` order; verdict badge; UNSTABLE = red border, opacity, struck-through title; relief/cost/delay/feasibility; verbatim `certificate.reason`; expandable 40/60/90% compliance sweep | `intervention_queued/resolved` | Approve (`op_demo`) / Reject; optimistic status with rollback |
| `ActionHUD` | per executed action: target util before→after (pp), band change, downstream easing, cascade from→to, settled realised vs do-nothing + regret; what-if card; rejected note | tracked actions, `regret_update` | dismiss/expand |
| `TwinFidelityGauge` | collapsed by default: RMSE-reduction %, ensemble N, RMSE; expanded: spread, drift toggle, red (uncorrected) vs green (assimilated) chart | `twin_fidelity` | `POST /twin/drift-mode` |
| `CommanderBar` | chat, 4 scripted questions, TOOL GROUNDED / CACHED chips, "Audit Sources" tray of tool calls | `POST /commander/query` | ask |
| `WhatIfPanel` | 4 presets (Blue Line −15%, Heavy Rain, Gate 3 Closure, Combined); baseline vs scenario table, new critical nodes, certified contingency actions | `POST /simulate` + poll `GET /simulate/{id}` 1 s, 15 s timeout | run / reset |
| `/attendee` `Attendee.jsx` | max-420px mobile view: journey risk traffic light, Recommended vs Shortest route (shortest always labelled worse), zone recommendation, nudge card; step-free toggle switches segment | `POST /attendee/journey`, `GET /attendee/nudges?attendee_id=att_demo_1` | Accept/Decline nudge |
| `/metrics` `Metrics.jsx` | 4 sections of metric cards (value · baseline · improvement · target), 3 emphasised, regret ledger chart | `GET /metrics` + `GET /regret`, polled every 5 s | — |
| `Toasts` | bottom-right, 5 s auto-dismiss | `store.toast` | dismiss |

## 8. Backend

**Startup** (`app/main.py` lifespan): `create_all()` → `clear_run_tables()` → `Engine()` (builds
StateStore + topology, MLRegistry, generator, initialises twin) → `Commander(engine)` →
`seed_topology()` (idempotent) → `engine.start()` (background asyncio loop). CORS allows any
`localhost`/`127.0.0.1` port. `run.py` = `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`
(⚠ reload: editing any backend file restarts the process and **restarts the sim from cycle 0**).
**For demos use `python run_demo.py`** (one process, no reload — see §32).

**Cycle pacing** (`Engine._loop`): wall sleep = `max(0.2, cycle_sec / speed_multiplier)`. Default
`speed_multiplier: 60` ⇒ one 30-sim-second cycle every **0.5 s wall** (1 sim-minute per wall
second). Cap is 150× (0.2 s floor). Cycle exceptions are logged and the loop continues.

**Budgets** (`config.yaml budgets_ms`, enforced by `call_ml`): assimilate 200 (×4 allowed),
forecast 300, risk 200, anomaly 150, cascade 150, optimise 200, certify 400, total cap 2000. On
timeout/exception `call_ml` runs the module's `.fallback()` synchronously. **Backpressure:** a cycle
over 2000 ms skips the *forecast* for the next 2 cycles; assimilation is never skipped.

**Services:**
| File | Responsibility |
|---|---|
| `services/engine.py` | the cycle (§4 above), payload builders, cascade precision/recall bookkeeping, certificate-accuracy cross-check, settlement, persistence, broadcast, `demo_control`, `_reset`, `set_drift_mode`; `get_engine()` singleton |
| `services/state_store.py` | all live state: nodes/edges/segments, entity states, histories (240), residuals, forecasts, forecast snapshots, pressure timeline, cascades, interventions, certificates, twin fidelity + 20-point history, regret, nudges, summary, telemetry counters |
| `services/simulation.py` | what-if: validate scenario → two clones of the live generator (do-nothing vs scenario, same horizon) → peaks/variance; then cascade + optimiser(3) + world-model certify + rank on the worst entity; in-memory job registry (§33) |
| `services/commander.py` | deterministic tool-planning Q&A + `GroundingValidator`; 8 allow-listed tools; logs to `commander_log` |
| `services/attendee.py` | Dijkstra journey (time-weighted vs congestion-weighted `(1+3·util^2.5)`), nudge issuing on approval (3 attendees `att_demo_1..3`, one per first 3 segments) |
| `services/metrics.py` | `/metrics` and `/regret` builders |
| `app/topology.py` | code-built demo graph + `verify()` (fails startup if demo chain/trap missing) |
| `app/cache.py` | in-process TTL dict (or Redis if `REDIS_URL` + client installed) |
| `app/errors.py` | the single error-envelope builder; unknown errors → `INTERNAL_ERROR` envelope, never bare 500 |
| `app/ws/manager.py` | global monotonic `seq`, fan-out, attendee filtering |

**REST endpoints** (prefix `/api/v1`, all verified in `api/routes.py`):
| Endpoint | Behaviour |
|---|---|
| `GET /health` | module readiness + `active_source` (forecaster, cascade, twin, equilibrium, commander) |
| `GET /event`, `GET /graph` | static event + topology (nodes, edges, segments, bounds) |
| `GET /state`, `GET /state/{id}` | live state (cache-first); detail adds forecast, edges in/out, risk breakdown |
| `GET /forecast[?entity_id=…]`, `GET /forecast/pressure-timeline` | `MODEL_NOT_READY` before first forecast |
| `GET /cascade/active`, `GET /cascade/{id}` | non-root entity → computed on demand via `cascade.predict` |
| `GET /interventions?status=&limit=` | default `proposed`, `all` allowed, sorted by `rank_score` desc |
| `GET /interventions/{id}`, `GET /certificates/{id}` | |
| `POST /interventions/{id}/approve` `{operator_id, note?}` | 409 if not proposed / expired; see §17 |
| `POST /interventions/{id}/reject` `{operator_id, reason?}` | status → `rejected`, nothing else changes |
| `POST /simulate` → 202, `GET /simulate/{id}` | what-if (horizon 60–7200 s) |
| `GET /twin/fidelity`, `POST /twin/drift-mode {enabled}` | |
| `GET /regret`, `GET /metrics` | |
| `POST /commander/query {query, session_id?}` | |
| `POST /attendee/journey`, `GET /attendee/nudges?attendee_id=`, `POST /attendee/nudges/{id}/respond {accepted}` | |
| `POST /demo/control {action?, seed?, speed_multiplier?, seek_to_sim_time?, inject?, auto_pause_on_intervention?}` | actions `play|pause|reset|seek|set_speed|step|next_decision`; `speed_multiplier` must be finite, `0 < s ≤ 150` (else 400); `inject` applies a scenario to the **live generator**; reset broadcasts a `resync`. Response = demo status (§32) |
| `GET /demo/status` | observer status: paused/playing, speed, `wall_seconds_per_cycle`, `run_id`, `auto_pause_on_intervention`, `pause_reason` (§32) |
| `GET /` (no prefix) | service info; OpenAPI at `/docs` |

**WebSocket** `ws://localhost:8000/ws?client=command_centre|attendee[&attendee_id=]`. Envelope
`{event, sim_time, seq, payload}`. Server sends `resync` immediately on connect and on
`{action:'resync'}`; answers `{action:'ping'}` with `pong`. Per-cycle events: `tick`,
`state_update` (changed entities only), `forecast_update`, `twin_fidelity`; conditional:
`cascade_alert` (newly active root only), `intervention_queued`, `intervention_resolved`
(`executing|rejected|expired|completed`), `regret_update`, `anomaly` (top 3); on user action:
`nudge_pushed`, `journey_risk_update`, `demo_status` (any control change or observer auto-pause; resync also
carries `demo`). Attendee clients only receive `nudge_pushed`,
`journey_risk_update`, `resync`.

## 9. Digital Twin (`Backend/app/ml_reference/twin.py`, `AssimilatedTwin`) — REAL EnKF, simplified model

> ⚠ **Superseded by Phase 1A (§33).** The description below is the pre-Phase-1 design and its
> audit findings; the current twin is an entity-localised EnKF for observed entities plus a
> documented peer-trend estimate for unobserved ones.

- **State:** aggregate per-entity `[count; flow]` (2N = 132 dims), ensemble m = 20 (seeded NumPy RNG).
- **Forecast step `step(dt)`** (called every cycle before assimilation): crude ABM surrogate —
  `count += flow·dt/60`, `flow *= 0.92`, Gaussian process noise (var 9 / 2.25), clip ≥0,
  multiplicative covariance inflation ×1.05 around the mean (mandatory; prevents collapse).
- **Analysis `assimilate(obs, sim_time, truth)`:** stochastic EnKF with perturbed observations,
  H selects observed counts, R = 25·I, `K = PHᵀ(HPHᵀ+R)⁻¹` (pinv on singularity); flow rows
  nudged consistent with the correction. Returns fidelity: `assimilated_rmse` vs **generator
  ground truth** (counts, not utilisation), `uncorrected_rmse` + `improvement_pct` only in drift
  mode, `ensemble_spread`.
- **Downstream use:** `state()` means become the displayed state for the ~25% **unobserved**
  entities (`is_observed:false`, dashed on map). Observed entities use the raw observation.
- **Drift mode:** copies the corrected ensemble into an uncorrected copy that is stepped but never
  assimilated; divergence feeds the red/green chart. The committed mock shows ~52.6% RMSE reduction.
- **`branch(scenario, horizon)`:** copies the ensemble, applies capacity/demand multipliers, rolls
  6 steps with the same crude surrogate (no inflation). Used by what-if, approval counterfactual,
  and certificate-accuracy checks. ⚠ demand multipliers are applied **every step** (compounding,
  e.g. ×1.22⁶ over the horizon). ⚠ branches draw from the live twin RNG, so user-triggered
  what-ifs/approvals perturb later live noise (seed reproducibility holds only for identical
  action sequences).

## 10. Event Graph / Topology (`Backend/app/topology.py`)

**66 entities, 120 edges, 5 segments** (⚠ docstring says 67/~130). Built in code; `verify()` runs
at startup and raises `TopologyIntegrityError` if broken.

| entity_type | count | examples / capacity unit |
|---|---|---|
| venue | 1 | `stadium_main` 70,000 |
| gate | 6 | `gate_1…6` (gate_5 smallest, 1,500 — the saturation trap) |
| transport_node | 9 | `metro_a…e`, `bus_hub_north/south`, `shuttle_hub_east/west` (`metro_b` 4,200) |
| transport_route | 3 | `line_blue/red/green` (people/hour) |
| road | 18 | `road_1…18` (660–1,100) |
| zone | 10 | `zone_north/south/east/west/core`, concourses, fanpark, plazas (load-variance surface) |
| hotel | 6 | `hotel_*_cluster` (rooms) |
| parking | 10 | `parking_p1…p10` (vehicles) |
| emergency_facility | 3 | `emergency_north/south/core` (110–140) |

| edge_type | count | meaning in code |
|---|---|---|
| `adjacent_to` | 38 | gate→road spill, road network, zone adjacency (cascade coefficient ×0.5) |
| `feeds` | 23 | line→station, station/hub→gate, gate→venue (only type drawn on map) |
| `substitutes_for` | 22 | alternatives for rerouting (parking, hotel, station, zone); no cascade transfer |
| `serves` | 16 | parking→gate, gate→zone (cascade only if dst forecast >0.7) |
| `last_mile_to` | 16 | hotel→transport, station→zone |
| `evacuates_to` | 5 | road/stadium→emergency post |

Edge fields: `transfer_coefficient`, `travel_time_sec`, `substitutability`; `edge_id =
"{src}__{dst}__{type}"`. Segments (`price_sensitive 0.28`, `time_sensitive 0.24`,
`accessibility_constrained 0.09`, `group 0.27`, `premium 0.12`) carry stated (not measured)
elasticities and `compliance_base_rate`. Structural guarantees: chain `metro_b→gate_3→road_4→
emergency_north` exists; edge `metro_c__gate_5__feeds` (0.68) exists. Graph usage: generator
(inject/relief propagation), cascade (GNN tensors + BFS), optimiser (templates), equilibrium
(receivers), attendee routing, frontend map/detail/action-effects.

## 11. Machine Learning — what is actually ML

`app/ml_registry.py` is the only instantiation point. For each module it tries `ml.<m>` then
`ML.<m>` (only if `ML/<m>.py` exists), else `app.ml_reference`. **Verified resolution on this
machine:** `cascade → ML.cascade` (GNN loaded); all 7 others → reference. (Log shows a harmless
warning that `ml.cascade` failed to import on the case-insensitive macOS FS before `ML.cascade`
succeeds.)

| Module | Implementation in live path | Truly ML? |
|---|---|---|
| CascadePredictor | `ML/cascade.py` HX-Cascade R-GCN, trained checkpoint, CPU | **Yes — trained model, inference only** |
| Forecaster | `ml_reference/forecaster.py` persistence (<10 pts) → `local_model` (ridge-shrunk quadratic trend on last 16 points) | No (statistical extrapolation). TSFM (Chronos) path exists but `chronos` is not installed → never `"tsfm"` |
| AssimilatedTwin | EnKF (§9) | State estimation (real algorithm, no learning) |
| RiskScorer | weighted arithmetic | No, by design |
| AnomalyDetector | rolling z-score | No, by design |
| InterventionOptimiser | rule templates + formula rank | No, by design |
| EquilibriumSolver | damped fixed-point follower model | No (game-theoretic simulation) |
| SyntheticGenerator | deterministic logistic curves | No (simulation) |

No LightGBM model, no Chronos weights, no RL anywhere. `generate_cascade_dataset` raises
`NotImplementedError` in the backend (training was done off-repo, "Kaggle notebook" per docs).

## 12. GNN / R-GCN Cascade (`ML/cascade.py`)

- **Architecture `HXCascade`:** `Linear(12→64)` + ReLU → 2 × [`RGCNConv(64,64, num_relations=6)`
  → LayerNorm → ReLU + residual] → heads `failure_logits` (3: 900/1800/3600 s) and `ttc`
  (ReLU scalar). Checkpoint shapes verified against `ML/hx_cascade.pt` (241 KB).
- **Node features (12, `ML/feature_norm.json`):** utilisation clipped at 3.0; capacity /
  89,152.07; one-hot of 9 entity types; in-degree over {feeds, last_mile_to, serves, evacuates_to}
  / 3. Edge types indexed in `edge_type_order`. Built from live `node_state` each cycle.
- **Inference:** `predict_all` roots = entities whose current band is high/critical **or** whose
  1800 s forecast implies high/critical; sorted by utilisation. **One forward pass per cycle**,
  shared by all roots. Uses sigmoid of the **3600 s** logit. BFS from each root over outgoing
  edges (depth ≤4); emits a step only for `cascade_relevant_types` (gate, road, transport_node,
  emergency_facility) with probability band high/critical (p > 0.60 → high, > 0.80 → critical);
  other types are traversed as context. `eta = max(parent_eta + travel_time, model ttc)`; root
  eta = forecaster's `time_to_critical_sec`. Steps sorted by eta.
- **Fallback:** self-contained deterministic propagator (overflow × edge coefficient, threshold
  0.15) on any load failure, NaN, exception; boot-time dummy forward pass sanity check. Engine
  also falls back per current high/critical root if `predict_all` exceeds its 150 ms budget.
- **Training evidence (committed, not used at runtime):** `swap_decision_v1.json` — train pool
  AP 0.836, precision@recall≥0.70 = 0.797 (32 topologies); held-out AP 0.900 / precision 0.996
  (8 topologies, flagged as likely small-sample inflation — **quote the train-pool figures**);
  deterministic propagator recall 0.322; TTC MAE ~204 s vs 3,092 s. `topology_meta_FINAL.pkl` holds
  40 perturbed topologies; `cascade_*_FINAL/v1.parquet` are training/held-out data. None are read
  by the app.
- ⚠ **Narrative caveat:** with the GNN active, the cascade from `metro_b` does **not** necessarily
  reach `emergency_north` (the committed `cascade_metro_b.json` goes metro_b→gate_3/gate_4→road_4…
  21 steps, no emergency post). The 4-hop hand-tuned chain is the deterministic propagator's
  story. `test_the_demo_cascade_chain_actually_propagates` only asserts depth ≥2.
- **Influence downstream:** cascade exposure → RiskScorer `cascading` term (weight 0.2);
  optimiser `emergency_corridor` template triggers if an emergency facility is in the chain;
  online precision/recall/lead-time metrics; map overlay.

## 13. Forecasting / Risk / Anomaly

| Component | Predicts / computes | Details |
|---|---|---|
| Forecaster | utilisation at 900/1800/3600 s, `lower_90/upper_90`, `time_to_critical_sec` | quadratic trend with curvature shrink 0.35, far-horizon damping, curve capped ±0.35, clip [0,1.6]; band = 1.645·residual RMS widened with horizon; TTC = linear interpolation to first crossing of 0.90 (0 if already ≥0.90, null if none); `baseline_comparison` = 1-step walk-forward MAE vs persistence (reported even if worse) |
| Forecast validation | engine compares each 900 s prediction to the actual 30 cycles later → `store.residuals`, `forecast_errors` → `/metrics forecast_mae` vs persistence |
| RiskScorer | `score = 0.5·100·util + 0.3·100·max(0, f1800−util)·1.5 + 0.2·100·cascade_exposure`, clamp 0–100; **Phase 1E: util ≥ 1.0 ⇒ score ≥ 81 (CRITICAL)**; non-finite inputs = missing; bands low ≤30, moderate ≤60, high ≤80, else critical; breakdown rows (type-specific primary, capacity, cascading, overall) |
| Summary | `overall = round(0.4·mean + 0.6·max)` of entity scores; `load_variance` = population variance of **zone** utilisations |
| AnomalyDetector | `|z| ≥ 3` on last 20 residuals (≥5 needed); broadcast + stored; **not rendered in UI** |

## 14. What-If Simulation (`services/simulation.py` + `twin.branch`)

> ⚠ **Superseded by Phase 1B (§33):** what-if now clones the live generator; `twin.branch()` is no
> longer used by what-if, counterfactual or certification.

- **Input:** list of `{scenario_type, params}` (10 enum types incl. `combined`), `horizon_sec`.
- **Mapping:** `attendance_delta`/`concurrent_event` → global demand ×; `*_capacity_delta`/
  `parking_loss` → capacity × on one entity; `weather_rain` → demand ×1.05/1.12/1.22 on roads,
  parking, gates; `gate_closure`/`transport_outage` → capacity 0.01 + same-type demand spread;
  `hotel_shortage` → hotels ×1.15.
- **Execution:** background task (202 then poll), `twin.branch()` → baseline vs scenario peak,
  variance, critical count; `delta` %; `new_critical_entities` (scenario ≥0.90 > baseline).
  Then on the peak entity: cascade predict (with `forecast_1800` overridden by the branch's final
  value), optimiser (max 3), certify, rank → "certified contingency actions".
- **Never mutates live state** (except the twin RNG cursor, §9). Frontend shows it as
  "SIMULATED · WHAT-IF" (cyan) — distinct from executed actions.
- Concepts: **current state** = StateStore; **baseline** = un-modified twin branch rollout;
  **scenario** = modified branch; **counterfactual** = the `{}`-scenario branch forked at approval.
- Note: the generator also supports *live* injection via `POST /demo/control {inject:{…}}` — that
  does change the running world (not exposed in the UI).
- Mock mode always returns the same `simulation.json` (Blue Line −15% + heavy rain) for any preset.

## 15. Optimization (`ml_reference/optimiser.py`)

- **Trigger:** most urgent entity with `time_to_critical_sec ≤ 3600`; skipped if it already has a
  proposed intervention or ≥8 proposals are live (`MAX_LIVE_PROPOSALS`).
- **Templates** (rule-based, graph-driven): `reroute_transport` (root is transport with
  `substitutes_for`; relief 18+24·subst), `deploy_shuttle` (root has `last_mile_to`),
  `stagger_entry` (root is/feeds a gate), `gate_redistribution` (hot >0.6 → cool gate),
  `parking_redistribution` (>0.85), `zone_incentive` (zone <0.5 and >0.85),
  `accommodation_rebalance` (hotel >0.88), `emergency_corridor` (emergency post in cascade),
  `notify_only` (always, relief 4%). Relief/cost/delay/feasibility are **hand-set constants /
  simple formulas**, not simulated. Filter `feasibility ≥ 0.3`, keep first 5.
- **Rank:** `rank_score = clamp(relief/100 · stability / ((0.5+0.5·cost/500000)·(0.5+0.5·delay/1800)), 0, 1)`,
  stability STABLE 1.0 / CONDITIONAL 0.6 / UNSTABLE 0.15 / uncertified 0.6. ⚠ `optimiser.weights`
  in `config.yaml` are **not read**. TTL 900 sim-s (`expires_at`).
- `unstable_interventions_caught` += 1 when the max-relief candidate is UNSTABLE and not ranked first.
- IDs deterministic: `int_` + hash(seed, root, type, targets, sim_time).

## 16. Stability / Equilibrium (`ml_reference/equilibrium.py`) — the signature feature

> ⚠ **Superseded by Phase 1D (§33):** the follower loop is now a genuine fixed point over the
> intervention's own sources/destinations against a world-model rollout. `row_verdict` /
> `derive_verdict` are unchanged.

- **Concept:** Stackelberg framing (organiser leads with incentives, attendee segments follow).
  Only the **follower equilibrium** (`certify`) is implemented; `solve_leader()` returns `[]`.
- **Model:** surface = zones + targets + receivers. Receivers = destinations of
  `substitutes_for`/`feeds`/`serves`/`last_mile_to` edges out of the targets (weighted by
  substitutability / coefficient). Per iteration, each segment's compliance =
  `base_rate · sweep_rate · sigmoid(3 · (cost(target) − cost(receiver, incentive)))` with
  `cost = util^2.5 − incentive·price_elasticity`, incentive = claimed relief. Moved fraction =
  `Σ share·compliance · relief` (≤0.95); load removed from relief targets and added to receivers
  by capacity; damped update γ=0.5; converge when ‖Δ‖∞ < 0.005 within 40 iters; oscillation if
  t≈t−2 but not t−1 twice. Simplified vs 03 §5.2 (no travel-time, perk, accessibility terms, no
  per-zone argmin).
- **Sweep:** compliance 0.4 / 0.6 / 0.9 → per-row `row_verdict`: max util ≥1.0 → UNSTABLE;
  post variance > baseline → UNSTABLE; max ≥0.90 → CONDITIONAL; else STABLE.
  `derive_verdict`: not converged or oscillating → UNSTABLE; all STABLE → STABLE; ≥2 UNSTABLE →
  UNSTABLE; otherwise CONDITIONAL. **These two functions are the only verdict logic in the repo.**
- **Output certificate:** verdict, converged, iterations, baseline/post (at 60%) variance, max
  utilisation + entity, oscillation flag, sensitivity (max−min sweep util), sweep rows,
  template `reason` (≤140 chars, deterministic, rendered verbatim).
- **Effect:** multiplies rank (§15); UI badge/strike-through; Commander contrasts it.
- **Certificate accuracy** (engine): for converged certs, compares `max_zone_utilisation` to an
  independent `twin.branch()` flat-demand-cut rollout peak (±15% or 0.05) → `/metrics`.
- Demo pairing in committed mocks: `notify_only` STABLE (4%, rank 0.16) outranks
  `reroute_transport` metro_b→Metro C UNSTABLE (35.3%, rank 0.131).

## 17. Intervention Lifecycle

> ⚠ **Approve / settle rows superseded by Phase 1B/1C (§33):** approval executes explicit
> `action_effects` transfers; settlement uses the matched same-world counterfactual, no clamp.

Statuses in code: `proposed → executing → completed`, or `proposed → rejected`, or
`proposed → expired`. (`approved` exists in the enum but is never set.)

| Stage | What really happens |
|---|---|
| Proposed | engine generates + certifies + ranks; `intervention_queued` WS |
| Approve (`POST …/approve`) | validates status/expiry → `twin.branch({},1800)` do-nothing trajectory stored → records `_util_at_approval` (mean target util) → **`generator.apply_relief(targets, relief%/100)`: multiplies ground-truth demand intensity on target entities by `(1−relief)` until reset** → `issue_nudges` (3 pending nudges) → `audit_log` row → WS `intervention_resolved{executing}` + `nudge_pushed` |
| Executing | the reduced demand flows into subsequent observations → twin → forecasts → risk → cascades; the HUD measures it from live state |
| Settle (≥900 sim-s ≈ 30 cycles) | `realised = (baseline − now)/baseline`, `counterfactual = (baseline − do_nothing[idx 2])/baseline` (both % clamped ±100; 0 if baseline ≤0.05), `regret = predicted − realised`; status `completed`; WS `intervention_resolved{completed}` + `regret_update`; do-nothing values feed `/metrics` peak/variance "reductions" |
| Reject / expire | status only; nothing else changes |

**Simulated vs real:** approval is a *real mutation of the simulated world* (demand multiplier in
the generator) — it does **not** actuate anything external. Relief is applied as an exact demand
cut equal to the optimiser's estimate, not modelled from the intervention type. Mock mode approval
only flips the card status; the HUD explicitly says so.

## 18. Human-in-the-Loop

- System recommends (optimiser + certificate); **nothing executes automatically**.
- Only `POST /interventions/{id}/approve` (with `operator_id`) changes the world. The Commander's
  `propose_action` tool returns `{queued:true}` and has no execution path (tested); the planner
  never even calls it (nor `run_whatif`).
- Operator may approve an UNSTABLE card (button styled secondary, still enabled).
- Rejection is recorded (audit log) with no side effects. Attendee Accept/Decline sets nudge status
  and appends to `observed_compliance`, which nothing reads.

## 19. Database / State

- **Live authority:** in-memory `StateStore` (single process). REST/WS read from it (plus cache).
- **SQLite** `Backend/eventflow.db` (gitignored, ~18 MB now). Postgres via `DATABASE_URL`
  (`psycopg` not installed). Tables (`app/db/models.py`):

| Table | Written? | Survives restart? |
|---|---|---|
| `entity`, `graph_edge`, `segment` | seeded idempotently at startup | yes |
| `entity_state`, `forecast` | every cycle | **no** — cleared on startup and on reset |
| `risk_state` | never written (only cleared) | — |
| `intervention`, `certificate`, `regret_entry`, `nudge` | upserted every cycle | yes (never read back into memory) |
| `audit_log` | approve/reject/nudge response/demo control | yes |
| `commander_log` | every non-cached query | yes |
| `execution`, `cascade_prediction` | defined, **never written** | — |

- **Not persisted / lost on restart:** everything in StateStore (live queue, cascades, twin,
  metrics counters, simulations). A restart always begins at `sim_start` cycle 0.
- **Cache:** in-process TTL dict (`state:current` 120 s, `cascade:active`/`forecast:latest`/
  `twin:fidelity` 60 s). Written each cycle; cleared on reset.
- `POST /demo/control {action:reset}` clears StateStore, rewinds clock, rebuilds MLRegistry +
  generator + twin, disables drift, clears cache and run tables.

## 20. Contracts

- `00` (conventions, enums, object schemas, error envelope, degradation, naming registry),
  `01` (cycle, endpoints, WS, DDL, ML boundary, config), `02` (component contracts, store, WS
  handling, mocks), `03` (ML interfaces/algorithms). Treat `00` as read-only; post-H20 changes are
  **additive only**.
- Conventions: snake_case on the wire (frontend never renames); ratios 0–1 except int
  `risk_score` 0–100; durations int `_sec`; money int `_paise`; ISO-8601 UTC `Z`; `sim_time` for
  event time, `server_time` only for wall clock; slug IDs; `null` = unknown, never omit a key.
- **Single owners:** `risk_band` (RiskScorer), `verdict` (`derive_verdict`), `time_to_critical_sec`
  (Forecaster), `rank_score` + order (optimiser/backend), `load_variance` (backend). Frontend
  renders; never re-sorts or re-thresholds.
- **Enforcement:** Pydantic `extra="forbid"` on every response → `scripts/export_schemas.py`
  (28 schemas) → `Frontend/scripts/validate-mocks.mjs` (AJV 2020) over every mock → CI.
  Mocks are generated by the backend (`scripts/export_mocks.py`), so they match by construction.
- Error codes (`app/errors.py`): 400 INVALID_REQUEST/INVALID_SCENARIO/INVALID_HORIZON; 404
  ENTITY/INTERVENTION/SIMULATION/NUDGE_NOT_FOUND; 409 INTERVENTION_ALREADY_RESOLVED/EXPIRED; 422
  MODEL_NOT_READY/INSUFFICIENT_HISTORY; 500 INTERNAL_ERROR; 503 ML_MODULE_UNAVAILABLE.

## 21. Testing

| Suite | Count | Covers |
|---|---|---|
| `Backend/tests/test_contract.py` (pytest, FastAPI TestClient, 90 warm-up cycles) | 19 | ≥60 nodes + demo chain edges, 5 segments, state schema, error envelope, 3 horizons, pressure-timeline order/offsets, cascade eta order, ranking desc + UNSTABLE not filtered, certificate demotes higher-relief UNSTABLE, double-approve 409, single verdict implementation, commander grounding, regret_update after settlement, propose_action no-exec, 8 tools only, journey returns both routes, metrics baselines, seeded reset reproducible, metro_b cascade depth ≥2 |
| `Backend/tests/test_ml_contract.py` | 13 | every module has `ready()`/`fallback()`, no exceptions on garbage, schema validity of forecaster/cascade/twin/certificate, determinism, optimiser feasibility gate |
| `Frontend/scripts/validate-mocks.mjs` (`npm run validate:mocks`) | 17 mocks | AJV against `contracts/schemas` |
| `Frontend/scripts/test-mock-lifecycle.mjs` (`npm test`) | 1 script | mock replay re-arms each loop, approval doesn't leak |
| `Backend/tests/test_phase0.py` | 38 | speed validation, interruptible loop, Commander cache vs reset, pause/play/step/next_decision, auto-pause on a real proposal, lifecycle while paused, expiry by stepping, WS `demo_status` (§32) |
| `Frontend/scripts/test-ws-reconnect.mjs` (`npm test`) | 1 script | real `ws.js` vs fake socket: restart (seq back to 1) recovers, duplicate/out-of-order/stale-socket frames dropped |
| `Frontend/scripts/test-store-timeline.mjs` (`npm test`) | 1 script | real store: resync from a new run/cycle regression clears per-run UI state; same-run reconnect keeps it |

**Total pytest: 192** (32 original + 38 Phase 0 + 122 Phase 1: `test_phase1_{twin,whatif,interventions,equilibrium,risk,api}.py`). No frontend unit/component tests, no E2E/browser
tests. ⚠ Backend tests run the real lifespan against `Backend/eventflow.db` (they clear run tables
and add rows) — don't run them while a demo server is using the DB. CI: Python 3.12, Node 20.

## 22. Real vs Simulated vs Mocked

| Capability | Classification |
|---|---|
| Crowd data / sensors | SIMULATED (deterministic generator; no real data source, no ingestion) |
| Digital twin EnKF | REAL algorithm over simulated observations; SIMPLIFIED ABM surrogate |
| Forecasting | DETERMINISTIC statistical (persistence → quadratic trend); TSFM path unreachable (not installed) |
| Risk, anomaly | DETERMINISTIC / RULE-BASED (by contract) |
| Cascade prediction | REAL trained GNN inference (live, `source:"gnn"`); DETERMINISTIC propagator as FALLBACK |
| Optimiser | RULE-BASED templates, hand-set relief/cost numbers |
| Equilibrium certificate | DETERMINISTIC game-theoretic simulation (follower only); leader search NOT IMPLEMENTED |
| What-if | SIMULATED on clones of the event-world model (generator), same horizon, never touches live world/RNG (Phase 1B) |
| Intervention execution | REAL state change **inside the simulation** only; no external actuation |
| Regret / counterfactual | REAL measurement of sim vs twin do-nothing branch (twin surrogate, not generator truth) |
| Commander | DETERMINISTIC templates over tool results; local LLM optional and OFF; no paid API |
| Metrics | REAL online measurements of the simulation; some baselines are constants (`cascade_precision` 0.31, `recall` 0.30, lead-time 0.0); `ensemble_coverage` is now MEASURED by the twin (Phase 1A); decision reductions come from matched settlements (Phase 1B) |
| Database | REAL SQLite writes; mostly write-only audit record |
| Frontend default (`npm run dev`) | MOCK DATA replay of a backend-generated 100-cycle run (no backend) |
| Frontend live (`npm run dev:live`) | REAL backend REST + WS |
| Postgres, Redis, Ollama, Chronos, LightGBM | OPTIONAL / NOT PRESENT |
| `generate_cascade_dataset`, `solve_leader`, `execution` table, anomaly UI | NOT IMPLEMENTED |

## 23. Current Working State (verified 2026-09-25, macOS)

- Git: branch `Yash` (tracks `origin/Yash`), clean before this file; main branch `main`.
- `Backend/.venv` exists: **Python 3.13.15**, fastapi 0.115.6, numpy 2.2.1, pydantic 2.10.4,
  SQLAlchemy 2.0.36, torch 2.10.0, torch-geometric 2.8.0.post1, pytest 8.3.4. No chronos, redis,
  psycopg, pandas, pyarrow.
- ⚠ System `python3` is 3.9.6 and there is **no `python` on PATH** — use `Backend/.venv/bin/python`
  (or activate the venv). The repo docs' bare `python run.py` fails without activation.
- `Backend/.env` exists (identical to `.env.example`; only `DATABASE_URL` + `EVENTFLOW_CONFIG`).
  Note: nothing in the code calls `load_dotenv`; `.env` is not loaded automatically — defaults
  already match it.
- Node **v24.21.0** (nvm); `Frontend/node_modules` installed. No `engines` field. No
  `Frontend/.env` (⚠ `RUNNING.md` says one exists with `VITE_MOCK=1` — it doesn't; not needed).
  `Frontend/.env.live` (committed) sets `VITE_MOCK=0`.
- Verified in-session: registry resolves cascade to `ML.cascade` and the GNN loads; 32 tests
  collect. Servers were not running during this capture; the full suite and builds were not run
  (to avoid touching `eventflow.db`/`dist`).

## 24. Local Setup / Run Commands

```bash
# Backend  (http://localhost:8000, docs /docs, API /api/v1, WS ws://localhost:8000/ws)
cd Backend
source .venv/bin/activate            # or prefix commands with .venv/bin/python
pip install -r requirements.txt      # already done on this machine
python run.py                        # dev: uvicorn --reload on 0.0.0.0:8000
python run_demo.py                   # DEMO: one process, no reload, same port (§32)

# Frontend, live against backend (Vite proxies /api and /ws to :8000)
cd Frontend && npm install && npm run dev:live      # http://localhost:5173

# Frontend, mock mode (no backend)
cd Frontend && npm run dev                          # or append ?mock=1 to force mock

# Demo control
curl -s -X POST localhost:8000/api/v1/demo/control -H 'Content-Type: application/json' -d '{"action":"reset"}'
curl -s -X POST localhost:8000/api/v1/demo/control -H 'Content-Type: application/json' -d '{"speed_multiplier":30}'
curl -s -X POST localhost:8000/api/v1/demo/control -H 'Content-Type: application/json' -d '{"action":"pause"}'

# Tests / validation
cd Backend && python -m pytest tests/ -q                       # single: tests/test_contract.py::test_name
cd Frontend && npm run validate:mocks && npm test && npm run build

# Regenerate schemas + mocks (after topology/forecaster/cascade/schema changes)
cd Backend && python -m scripts.export_schemas --out ../contracts/schemas
python -m scripts.export_mocks --out ../Frontend/src/mocks --cycles 90   # current mocks were made with 100
cd ../Frontend && npm run validate:mocks
```

Required at runtime: `Backend/config.yaml`, `ML/cascade.py`, `ML/hx_cascade.pt`,
`ML/feature_norm.json` (missing GNN files → silent deterministic fallback, `active_source:
"deterministic"`). No seed/migration step: tables are created and topology seeded on startup.
Env overrides: `EVENTFLOW_CONFIG`, `DATABASE_URL`, `REDIS_URL`, `LOCAL_LLM_URL`,
`LOCAL_LLM_MODEL`; frontend `VITE_MOCK`, `VITE_API_BASE` (`/api/v1`), `VITE_WS_URL` (`/ws`).

## 25. Demo Flow (what a judge sees, live mode)

Timing at default 60×: 1 cycle = 0.5 s wall = 30 sim-s.

1. **Calm opening** — map mostly green/yellow, empty queue ("No Interventions Required"),
   forecast badge `persistence` for the first 10 cycles, then `local_model`.
2. **Pressure** — entities ramp; Pressure Timeline fills with minutes-to-critical (hero row first).
   Commander: "What is the biggest problem right now?" / "Why is Metro B becoming critical?"
3. **Cascade** — click an entity → detail panel → "Show Cascade Propagation Lines" → arcs reveal
   step by step with ETAs (`source: gnn`).
4. **Recommendations** — cards arrive with certificates; an UNSTABLE high-relief card appears
   struck-through below a STABLE/CONDITIONAL one. Expand to show the 40/60/90% sweep. Commander:
   "Which action gives the largest safety improvement?" explains the contrast.
5. **What-if** — run a preset; cyan SIMULATED overlay + baseline/scenario table; live world unchanged.
6. **Approve** — card → EXECUTING, toast "N nudges issued", teal ring on targets, ActionHUD shows
   measured before→after deltas over the next cycles; `/attendee` shows the nudge.
7. **Settlement** — ~30 cycles (~15 s wall) later: HUD "realised X pp vs do-nothing Y pp",
   `/metrics` regret ledger gets an entry.
8. **Twin** — expand Digital Twin, toggle drift: red uncorrected RMSE diverges from green.
9. **`/metrics`** — prediction/twin/decision/system cards with baselines.

Phase 0 mitigation (§32): turn on **Pause on proposal** in the Observer bar — the sim pauses on
the cycle a real proposal is queued, so its TTL cannot run out while it is being read.

⚠ **Timing hazards:** proposal TTL is 900 sim-s = **~15 s wall at 60×** (7.5 s at 120×); expired
cards vanish and approving returns 409. Slow the sim for the approval moment
(`{"speed_multiplier": 15}` ⇒ ~60 s window) or pause (paused ⇒ no expiry, no settlement). `RUNNING.md`
suggests 120× to go faster — that also halves the approval window. The first recommendation
timing depends on which entity is most urgent; metro_b's own UNSTABLE/STABLE pair lands past
cycle ~60 per test/doc comments.

Mock mode: 100-frame loop at 0.9 s/frame (~90 s), cascades arm at frame 8, the committed
interventions (notify_only STABLE vs reroute UNSTABLE) at frame 10; approval flips status only;
regret ledger empty; Commander answers only the 4 scripted questions; badge "DEMO MOCK".

## 26. Important Files

| Path | Purpose / why it matters |
|---|---|
| `Backend/app/services/engine.py` | the whole cycle, settlement, persistence, broadcast, reset — start here for any behaviour change |
| `Backend/app/services/state_store.py` | every piece of live state and the delta/expiry logic |
| `Backend/app/api/routes.py` | all REST handlers; approve is the real state-changing path |
| `Backend/app/api/ws_routes.py`, `app/ws/manager.py` | WS endpoint, resync payload, seq, attendee filter |
| `Backend/app/ml_registry.py` | module resolution + `call_ml` budget/fallback |
| `Backend/app/ml_reference/*.py` | reference implementations of all 8 modules (return shapes are the contract) |
| `Backend/app/ml_reference/equilibrium.py` | `row_verdict`/`derive_verdict` — the only verdict logic |
| `Backend/app/ml_reference/generator.py` | ground truth, hero profile for `metro_b`, `inject`, `apply_relief` |
| `Backend/app/topology.py` | the graph and its startup integrity checks |
| `Backend/app/schemas.py` | Pydantic wire types (strict) — change here ⇒ re-export schemas + mocks |
| `Backend/app/services/{simulation,commander,attendee,metrics}.py` | what-if, Commander, journeys/nudges, KPIs |
| `Backend/config.yaml` | seed 42, cycle 30 s, speed 60, thresholds, budgets, module params |
| `Backend/scripts/export_schemas.py`, `export_mocks.py` | contract/mocks generators |
| `ML/cascade.py`, `ML/hx_cascade.pt`, `ML/feature_norm.json` | live GNN, weights, feature normalisation |
| `ML/swap_decision_v1.json` | evaluation numbers + honest caveat for the GNN swap |
| `Frontend/src/App.jsx` | bootstrap + routes |
| `Frontend/src/lib/api.js`, `ws.js`, `mocks.js`, `mockLifecycle.js` | network, WS, mock replay |
| `Frontend/src/store/useStore.js` | single store and its merge/replace rules |
| `Frontend/src/components/*.jsx` | UI panels (see §7) |
| `Frontend/tailwind.config.js` | inverted palette (light beige theme) |
| `Frontend/src/mocks/*.json` | backend-generated fixtures (17 files) |
| `contracts/schemas/*.json` | generated JSON Schemas (do not hand-edit) |
| `.github/workflows/ci.yml` | CI gate |

## 27. Project Vocabulary

| Term | Meaning here |
|---|---|
| Entity | a node: venue, gate, zone, transport node/route, road, hotel, parking, emergency facility |
| Utilisation | `current_count / nominal_capacity`, 0–2 in state (critical at ≥0.90) |
| Risk score / band | 0–100 int from RiskScorer; low/moderate/high/critical at 30/60/80 |
| Pressure timeline | entities forecast to hit critical within 3600 s, sorted by `time_to_critical_sec`, 6-point trajectory (0/300/600/900/1200/1800 s) |
| Time to critical | seconds until forecast utilisation crosses 0.90 |
| Observed / estimated | entity has a (simulated) sensor vs value comes from the twin |
| Digital Twin | the EnKF ensemble estimate of all entities |
| Drift mode | runs an un-assimilated copy of the ensemble to show what the twin corrects |
| Branch | a forked twin rollout used for what-if / counterfactual; never touches live state |
| Cascade | predicted chain of downstream entities entering high/critical from a root, with ETAs |
| HX-Cascade | the trained 2-layer R-GCN in `ML/cascade.py` |
| Deterministic propagator | rule-based cascade fallback (overflow × edge coefficients) |
| Intervention | a candidate operational action (9 types) with relief/cost/delay/feasibility |
| Certificate | equilibrium check result for an intervention: verdict + compliance sweep + reason |
| Compliance sweep | certification repeated at 40/60/90% attendee compliance |
| STABLE / CONDITIONAL / UNSTABLE | holds / holds only at lower compliance / breaks (overload, worse variance, non-convergence, oscillation) |
| Segment | attendee class with share, price/time elasticity, base compliance |
| Nudge | attendee message from an approved intervention with a trade-off (extra time, credit, perk) |
| Relief | `estimated_relief_pct` claimed by the optimiser; applied as a demand cut on approval |
| Settlement | 900 sim-s after approval, compare realised vs counterfactual relief |
| Regret | `predicted_relief_pct − realised_relief_pct` |
| Counterfactual / do-nothing | the live world with the intervention's effects excluded, evaluated at the same instant (Phase 1B) |
| Load variance | variance of zone utilisations (the "spread load evenly" objective) |
| Commander | grounded Q&A assistant; "grounded" = every numeric token traces to a tool result |
| Cycle | one 30-sim-second orchestration pass |
| Speed multiplier | sim-seconds per wall-second |
| Hero entity | `metro_b`, hand-tuned to go critical early |
| Saturation trap | `gate_5`, overloaded by rerouting Metro B → Metro C |
| Fallback | a module's `.fallback()` — normal operating state, reported via `source` fields |

## 28. Important Constraints

- Contracts: snake_case wire, units/casing rules (§20), additive-only changes, no renames.
- Nothing computed twice: no frontend thresholds/verdicts/re-sorting; verdict logic only in
  `equilibrium.py`.
- ML modules: no I/O after `__init__`, no imports from `Backend/`, plain dicts, `.fallback()` with
  identical shape, deterministic under seed, never raise.
- Determinism: never add unseeded randomness (`random`, wall clock, Python `hash()`); use
  `stable_unit(...)` / the seeded RNG.
- Engine is the only writer of simulation state; request handlers read (approve/reject/nudge/
  drift/demo-control are the deliberate exceptions).
- Keep the Commander at 8 tools; `propose_action` must never execute.
- `verify()` guarantees (demo chain, gate_5 trap, ≥60 nodes, segment shares sum to 1) must hold.
- Changing topology/forecaster/cascade/schemas ⇒ re-export schemas and mocks, run `validate:mocks`.

## 29. Known Limitations

- All data is synthetic; metrics are "in simulation" only (the ML docs insist on this framing).
- Forecaster is a statistical trend model; "tsfm"/"LightGBM" are not present.
- Optimiser relief numbers are asserted, not simulated; approval applies exactly that relief to
  the ground truth, so "realised" relief is partly self-fulfilling (it still measures the
  combined effect through the twin/forecast pipeline and against a twin do-nothing branch).
- Twin surrogate ABM is crude; counterfactual and certificate-accuracy use it, not the generator.
- Equilibrium model is a simplified aggregate; leader optimisation absent.
- Single process, in-memory state; no auth; operator id hard-coded `op_demo` in the UI.
- Nudges are always issued to `att_demo_1..3`; live-mode attendee view receives all attendees'
  nudges through the shared command-centre socket.
- `call_ml` timeouts don't cancel the worker thread (a slow call keeps running).
- `LOCAL_LLM_URL` alone does nothing: `commander.engine` must also be `"local_llm"` in
  `config.yaml` (⚠ docs imply the env var is enough).
- `uvicorn --reload` restarts the sim on any backend file save.
- Frontend: anomalies not displayed; mock what-if ignores preset; mock entity detail has no forecast.

## 30. Future Scope (only what the repo itself implies)

- Drop-in `ML/forecaster.py` (Chronos-Bolt TSFM and/or LightGBM warm-start) — registry picks it up.
- `EquilibriumSolver.solve_leader()` (Stackelberg leader search) — stubbed.
- `generate_cascade_dataset` in an ML-side generator (training pipeline lives off-repo).
- Postgres/PostGIS + Redis deployment (supported by config, not exercised).
- Optional local-LLM Commander phrasing (Ollama).
- Contract "cut order" items noted in `README.md`: edge crowd counting, multi-event collision
  (`concurrent_events` is always `[]`).

## 31. Instructions for Future Claude Sessions

1. Read this file first; open source files only for the area you are changing. §26 maps where.
2. Before cross-cutting changes, check the relevant contract section; keep changes additive.
3. Use `Backend/.venv/bin/python` (system python is 3.9, no `python` binary).
4. Don't claim capabilities beyond §22. Say "trained GNN" only for cascade; everything else is
   deterministic/rule-based/EnKF.
5. After backend schema/topology/model changes: run pytest, re-export schemas + mocks,
   `npm run validate:mocks`, `npm run build`.
6. UI edits: remember the inverted Tailwind palette (§7). Render API strings; don't derive bands.
7. Don't run backend tests against a DB a live demo is using; don't delete `eventflow.db` casually
   (it holds the audit/regret history, though the app works without it).
8. For demo timing issues use the Observer bar / `/demo/control` (speed, pause, step,
   next_decision, auto-pause) rather than code. Run demos with `run_demo.py`, never `run.py`.
9. The hackathon is imminent: prefer no-risk changes, and confirm before anything that alters
   demo behaviour, seeds, topology, or `config.yaml`.

## 32. Phase 0 — Demo Safety, Recovery & Observability (implemented 2026-09-26)

Status: **COMPLETE** (evidence: `PHASE0_VALIDATION_REPORT.md`). No numerical-core behaviour changed
(twin, what-if, counterfactual, intervention physics, equilibrium, risk, cascade are Phase 1+).

| Area | Mechanism |
|---|---|
| WS recovery | `Frontend/src/lib/ws.js`: **a new connection is a new sequence context** — `lastSeq = 0` on every socket open; frames/closes from a superseded socket are ignored. Duplicate/out-of-order protection still applies within a connection. Store `replaceAll` clears per-run UI state (tracked actions, what-if overlay, selection, cascade overlay, anomalies) when the resync shows a new `run_id` or a cycle regression. |
| Speed validation | `DemoControlRequest.speed_multiplier`: finite, `0 < s ≤ 150` (150× = the engine's 0.2 s wall floor), `set_speed` requires a value → otherwise 400 `INVALID_REQUEST`. `engine.validated_speed()` also guards `config.yaml`. The validation error handler now returns only `loc/msg/type` (echoing a NaN input used to crash it). |
| Interruptible loop | `Engine._loop` waits in ≤0.1 s slices (`WAKE_CHECK_SEC`), re-reading speed/pause, so a speed change applies immediately (a 0.01× speed can no longer trap it in a 50-min sleep). `_cycle_lock` serialises cycles with `reset` and `step`. |
| Commander cache | `Engine.run_id` (starts 1, +1 per reset). Cache entries store `run_id`; fresh only if same run **and** `0 ≤ Δcycle ≤ 4` (negative Δ never fresh). `Engine._reset` also calls `commander.clear_cache()`. A backend restart starts with an empty cache. |
| Demo backend | `Backend/run_demo.py` — `uvicorn app.main:app` on 0.0.0.0:8000, `reload=False`, one process; refuses to start (exit 1) if the port is busy. Ctrl-C/SIGTERM → normal lifespan shutdown, port freed. |
| Observer status | `Engine.demo_status()` → `GET /demo/status`, the `POST /demo/control` response, WS `demo_status`, resync `demo`: `status, sim_time, seed, speed_multiplier, cycle_number, run_id, cycle_sec, wall_seconds_per_cycle, auto_pause_on_intervention, pause_on_next_decision, pause_reason{kind: operator|intervention_proposed|step, intervention_id?, entity_id?, cycle_number?, sim_time?}`. |
| Step / next decision | `step`: pause, run exactly one real cycle. `next_decision`: play until the optimiser queues its next REAL proposal batch, then pause (one-shot). No synthetic events. |
| Auto-pause | `auto_pause_on_intervention` (default **false** — unattended behaviour unchanged). When a cycle queues a real batch, the engine pauses with `pause_reason = intervention_proposed` (top-ranked proposal). Paused ⇒ sim clock frozen ⇒ TTL/expiry frozen; approve/reject work while paused; `step` still expires on sim time (30 steps = 900 s). |
| UI | `ObserverBar` (under TopBar): status chip + reason, sim clock/cycle, Play/Pause, Step, Next decision, speeds 0.5×/1×/2×/5×/10× + FF 60×, "1 cycle = 30 sim-s ≈ X real", Pause-on-proposal toggle, Reset. `ObserverDecisionBanner` (over the map) when paused for a proposal: entity, load, risk band, twin-estimate flag, proposal, certificate, optimiser **estimate**, expiry in sim-min and ≈ real time; Approve & resume / Reject & resume / Keep paused; afterwards shows action STATUS + live target utilisation only (no improvement claim). Both use `lib/useResolveIntervention.js`, the same path as the queue. Mock mode: controls disabled with a message (never faked). |

Speed = sim-seconds per real second: 1× = real time (one cycle per 30 s), 10× = one cycle per 3 s,
60× = one per 0.5 s. Suggested judge flow: FF 60× + Pause on proposal (or Next decision) to reach a
problem, then 5–10× to watch the outcome.

```bash
# demo-safe run
cd Backend && DATABASE_URL=sqlite:////tmp/eventflow_demo.db .venv/bin/python run_demo.py   # or plain: .venv/bin/python run_demo.py
cd Frontend && npm run dev:live
# observer control from a terminal
curl -s localhost:8000/api/v1/demo/status
curl -s -X POST localhost:8000/api/v1/demo/control -H 'Content-Type: application/json' -d '{"auto_pause_on_intervention":true,"action":"play","speed_multiplier":60}'
curl -s -X POST localhost:8000/api/v1/demo/control -H 'Content-Type: application/json' -d '{"action":"step"}'
curl -s -X POST localhost:8000/api/v1/demo/control -H 'Content-Type: application/json' -d '{"action":"next_decision"}'
# Phase 0 tests (point DATABASE_URL at a scratch DB — tests write to it)
cd Backend && DATABASE_URL=sqlite:////tmp/ef_test.db .venv/bin/python -m pytest tests/ -q
cd Frontend && npm test && npm run build
```

Known limits (not Phase 0): with auto-pause on, real proposals arrive on consecutive cycles early in
the run (cycles 10, 11, 13 — driven by twin-estimated entities, audit P0-01), so the banner shows
the previous decision's status inline; browsers log failed reconnect attempts as console errors
while the backend is down (expected).

## 33. Phase 1 — Numerical Core Correctness (implemented 2026-09-26)

Status: **COMPLETE** (evidence: `PHASE1_VALIDATION_REPORT.md`; roadmap: `plot.md`). Phase 0 (§32)
unchanged and re-verified. Scope was numerical correctness only — **no crowd-flow model**: entities
still follow independent scripted curves; people only move between entities through explicit,
conserved transfers (interventions, closures, outages).

| Sub-phase | What is true now | Where |
|---|---|---|
| 1A Twin | State still `[count; flow]` (2N). OBSERVED entities: stochastic EnKF **localised to the entity itself** (no cross-entity gain), no flow overwrite, inflation only on observed rows, observation error 1% relative (floor `obs_noise_var`), count process noise `SIGMA_COUNT_UTIL`=0.012 (calibrated seeds 42/7/123). UNOBSERVED: deterministic **peer-trend** `u(t) = u(t0) + median(observed same-type peers' change)`, spread = 1.5 × peer dispersion (floor 0.03). Approved transfers are **known control inputs** (`set_known_transfers`). Guards: non-finite → last good; counts ∈ [0, 2×cap]; flows ≤ 0.25 cap/min. `branch()` uses a private RNG and applies demand once. Fidelity gains measured `ensemble_coverage`; drift baseline = same bounded model open-loop. | `ml_reference/twin.py` |
| 1B What-if / counterfactual | `generator.clone()` + `evaluate(t, exclude=, only=)`. What-if = baseline clone vs scenario clone (scenario applied once, at "now"), evaluated every 30 s over the horizon; scenarios validated (unknown/mismatched entity, bad delta/intensity → 400 `INVALID_SCENARIO`). Capacity scenarios now RAISE utilisation (utilisation = people / effective capacity). Gate closure / transport outage = capacity 1% + conserved transfer of all people to open siblings / substitutes. Settlement: actual = live world; counterfactual = live world with this intervention's effects excluded, same instant; `realised = (cf−actual)/cf`, `counterfactual_relief_pct = (u_approval−cf)/u_approval`, `regret = predicted − realised`, all over mean SOURCE utilisation; **no clamp**, null when a denominator ≈ 0. `/metrics` peak & variance reductions = mean over matched settlements. | `ml_reference/generator.py`, `services/simulation.py`, `engine._settle_executing_interventions`, `services/metrics.py` |
| 1C Intervention physics | Interventions carry `effect_model` (`transfer`/`deferral`/`none`) + `action_effects[{source_entity_id, destination_entity_id, planned_fraction, ramp_sec, duration_sec}]`. Approve → `engine.execute_intervention`: each effect = generator transfer of `planned_fraction × response` of the source's people, ramping over `ramp_sec`; source −, destination + (conserved). `notify_only`, `emergency_corridor` = no demand effect. `stagger_entry` = deferral for 720 s. Nudges only for actions with a destination. | `ml_reference/optimiser.py`, `engine.execute_intervention`, `services/attendee.py` |
| 1D Equilibrium | `m = F(m)` damped fixed point (m = share of the offered move taken; F = segment compliance from the cost gap AFTER moving m); convergence `|Δm| < tol`, oscillation per 03 §5.2, divergence = non-finite. Scope = the action's sources+destinations only, rolled forward over 1800 s in the SAME world model (`engine.world_rollout`) vs matched do-nothing. Row `max_utilisation` = peak of entities the action ADDS load to; variance = affected peaks with/without. Breach minutes read from the rollout (else omitted). Certificate gains `response_rate` (m* at the 0.6 row) — execution uses it (fallback: nominal Σshare×base×0.6 = 0.27). `solve_leader` = deterministic grid over offer scale (0.25/0.5/0.75/1.0), best-first; not wired into ranking. Certificate accuracy = approval-time projection vs realised at settlement (same model ⇒ consistency check, not field accuracy). | `ml_reference/equilibrium.py`, `engine.certify_candidate` |
| 1E Risk | 03 §7.1 formula unchanged + invariant `util ≥ 1.0 ⇒ score = max(formula, high+1)` (CRITICAL); non-finite inputs = missing (was: NaN → 100/critical). | `ml_reference/risk.py` |

Additive contract changes (schemas re-exported, mocks regenerated with `--cycles 100`, 17/17 valid):
`Intervention.effect_model`, `Intervention.action_effects[ActionEffect]`, `Certificate.response_rate`,
`TwinFidelity.ensemble_coverage`; `RegretEntry.realised_relief_pct/counterfactual_relief_pct/regret`
now nullable. The ActionHUD labels settlement as "source relief X% vs do-nothing" (was "pp").

Key facts for future work:
- In this synthetic environment the what-if/counterfactual/certificate world model IS the
  generator — projections are exact for the simulation, not forecasts of real crowds; certificate
  accuracy ≈ 100% by construction. Say so when presenting it.
- The generator clips demand at 1.6× capacity, so late in the run large demand scenarios barely
  move the global peak (entities already at the clip). Crowd-flow phase.
- The store bounds displayed utilisation to [0, 2]; above that compare `current_count`.
- Tests that call route handlers directly must `set_engine(<that engine>)` first (they resolve the
  global engine); audit_log is durable, so assert deltas, not absolute rows.

Commands (scratch DB always — tests write to it):
```bash
cd Backend && DATABASE_URL=sqlite:////tmp/ef_p1.db .venv/bin/python -m pytest tests/ -q     # 192 passed
cd Backend && .venv/bin/python -m scripts.export_schemas --out ../contracts/schemas
cd Backend && DATABASE_URL=sqlite:////tmp/ef_mocks.db .venv/bin/python -m scripts.export_mocks --out ../Frontend/src/mocks --cycles 100
cd Frontend && npm test && npm run build
```
