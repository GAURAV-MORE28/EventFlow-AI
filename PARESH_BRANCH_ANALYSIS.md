# EventFlow AI — Paresh Branch Forensic Analysis

| | |
|---|---|
| Date | 2026-09-27 |
| Branch analysed | `Paresh` @ `71f5290` (= `origin/Paresh`) |
| Compared against | `Mehul` @ `f5fa1da` (validated Phase 0 + Phase 1 baseline) |
| Merge base | `92b97bd` "Finalize EventFlow AI frontend and documentation" |
| Method | Read-only. Static reading of code and docs, git forensics, full test suites, in-process experiments, a live backend on a scratch port, and headless-Chrome checks. Every run used a scratch SQLite DB under the session scratchpad. `Backend/eventflow.db` was not touched: its mtime of 01:34:08 predates this session's first `git fetch` at 01:37:12. Every process this analysis started was stopped by exact PID. |
| Only file written in the repo | this file |

---

## 1. Executive Summary

**What Paresh is.** Paresh is a separate line of development from Mehul, not a continuation of it. It forked from `92b97bd`, before Mehul's Phase 0/1 commit `f5fa1da`, and **none of that commit's code is on Paresh**. Paresh then rebuilt most of the product in 7 commits (about 26–27 Sep, author `Arrodrax07` with Claude co-authorship), plus one frontend-restyle commit shared with Mehul.

**What Paresh changed.**
- **City model.** The per-entity scripted-curve generator was replaced by a deterministic **flow-coupled city simulator**:
  - event schedule → arrivals and hotel bookings → stations, hubs and parking → gates (fluid queues, road spill) → venue → egress.
  - This is Phase 2 on Mehul's roadmap ("crowd-flow model"), implemented differently.
- **New backend services:** event CRUD with immediate reconciliation, accommodation, live disruptions, a look-ahead projection, candidate evaluation by simulation, a deterministic flow cascade, and geo/data provider seams.
- **ML:** a retrained GNN checkpoint (v2).
- **Frontend:** 7 new pages.
- **Docs:** 3 new documentation files.
- **Tests:** 126 backend tests, all passing.

**What is genuinely good (verified at runtime).**
- The flow model is causal and conserves people. Changing event attendance from 1,000 to 12,000 changes the venue and its access nodes; moving the event moves the load. Arrivals are conserved under interventions.
- What-If runs on clones of the live world and **never mutates live state or the twin RNG**: A→B and B→A what-ifs leave the state byte-identical to a no-what-if run.
- The counterfactual is a **matched same-world fork**. Its utilisation equals an independent no-action run exactly (max |Δ| = 0.0).
- Interventions move people: the source drops and the destination rises versus the counterfactual. `notify_only` changes nothing.
- The twin is bounded: over 600 cycles there are no 0/2 artefacts, no NaN, and unobserved RMSE is ≈0.003.
- UI controls such as Pause, Resume, Speed and Approve really change backend state.
- Mock mode is honestly labelled as a replay.

**Major risks and regressions (verified).**
1. **Phase 0 is largely absent (regressed).**
   - **WebSocket recovery after a backend restart is broken.** In a real browser the UI stayed frozen at cycle 394 for 24 s while the backend ran cycles 1→9, and the chip still said "LIVE FEED".
   - The clock is not interruptible: after a 0.5× setting, raising the speed to 60× took **58 s** to take effect.
   - Invalid speeds (0, −1, NaN, ∞, 1e9) return **200** and are silently clamped.
   - The Commander serves a pre-seek cached answer after a backward seek.
   - `run_demo.py`, Step, Next Decision, auto-pause and `GET /demo/status` do not exist.
2. **Phase 1 is partially preserved.**
   - Twin boundedness, What-If isolation, matched counterfactuals and intervention direction/conservation all **pass** by independent re-implementation.
   - **The equilibrium solver is the pre-Phase-1 code.** Relief is re-applied on every iteration, the scope is every zone, and "within N minutes" is the formula `26 − overshoot×100`. An unrelated overloaded zone becomes the certificate's reason.
   - **A venue at ≥100 % utilisation is banded HIGH, not CRITICAL.** At 16:42 at runtime, `stadium_main` held 70,625 people against 70,000 capacity and scored HIGH 76.
   - The ±100 % clamp in settlement is still present; `counterfactual_relief_pct = −100.0` was observed.
   - What-If accepts out-of-range parameters.
3. **Cross-process non-determinism.** Twin estimates, published state and forecasts differ between two backend processes started with the same seed. The cause is Python's per-process hash randomisation deciding the dict key order of observations, which in turn decides the twin's RNG draw order. With `PYTHONHASHSEED` fixed, runs are identical. Interventions, cascades and What-If results matched across processes in the tests.
4. **Contracts diverged.** Mehul and Paresh extended `Intervention`, `Certificate` and `TwinFidelity` in incompatible ways: Mehul has `action_effects`, `effect_model`, `response_rate` and `ensemble_coverage`; Paresh has `evaluation` and `live_effect`. Any merge needs a deliberate reconciliation.

**The next planned feature (Venue → Radius → Footprint → Blueprint → Event Graph) is NOT IMPLEMENTED on Paresh.**
- There is no radius, footprint, geocoding, OSM, Overpass or POI code anywhere (repository-wide search).
- "Venue" means only choosing one of 9 existing graph entities as an event's location.
- The only seam is `data.provider: file`. It loads a custom `topology.json`, and coordinates and capacities then flow through the engine (verified by moving the city to London and halving the stadium).
- However, `topology.verify()` **rejects any graph that is not the demo graph**: it requires ≥60 nodes, the `metro_b→gate_3→road_4→emergency_north` chain and the `metro_c__gate_5__feeds` edge.
- The synthetic hotel catalogue also crashes (`KeyError: 'hotel_north_cluster'`) for a topology without the demo hotel IDs.
- Several generator paths assume demo IDs (`bus_` prefix, `evt_demo`, `zone_fanpark`, `stadium_main`).

**Bottom line.** Paresh has a much richer and causally sound simulation core. It is a reasonable foundation for an event-graph pipeline **once the topology coupling is removed**. It is **not** demo-safe at the Phase 0 level, and it has not preserved several Phase 1 fixes.

---

## 2. Current Git State

| Item | Value |
|---|---|
| Current branch | `Paresh` |
| HEAD | `71f5290` "Harden event simulation and live state synchronization" (= `origin/Paresh`) |
| Working tree | clean (before and after this analysis, except this file) |
| `git fetch origin` | no new refs |
| Merge base with Mehul | `92b97bd` |
| Mehul-only commits | `bab9bf5` (UI; tree-identical to Paresh's `e445d5a`), `f5fa1da` "Complete Phase 0 and Phase 1 numerical core" |
| Paresh-only commits (`Mehul..Paresh`) | 8 (table below) |
| `git diff --stat Mehul...Paresh` | 141 files, +152,233 / −123,186 (most of the line count is regenerated mocks): 45 added, 95 modified, 1 deleted, 0 renamed |
| Ports at start | 8000 and 5173 free; no project processes running |

`git diff e445d5a bab9bf5` is empty. The shared frontend restyle is byte-identical on both branches.

| Commit | Author, date | Subsystems | Purpose | Behaviour change? |
|---|---|---|---|---|
| `e445d5a` | Yash-sahu07, 09-25 | Frontend (21 files, adds unused `Frontend/test.css`, 2,981 lines, imported nowhere) | UI restyle, `lucide-react` added | Yes (UI only) |
| `0fe613a` | Arrodrax07, 09-26 18:15 | Backend, frontend, ML, contracts, docs (105 files) | "stable integrated product": flow-coupled simulator, twin fixes, per-type risk, simulated evaluation, counterfactual settlement, clone-based What-If, accommodation, schedule, disruptions, journeys/nudges, commander intents, new pages | Yes (major) |
| `1f98fc2` | 09-26 18:17 | Backend (10 files) | `CityModel` protocol (`ml_reference/city_model.py`), `city_model: flow` config switch | Refactor + test |
| `f82f4fe` | 09-26 18:29 | Backend, store | Reset/seek broadcast `resync`, `prime_state()` at t=0, store drops per-run UI state | Yes |
| `d0b916a` | 09-26 18:46 | Backend, Attendee, TopBar | Cancelled events empty their venue, hotel-transfer nudge re-plans, departure advice threshold, TopBar shows the real cycle | Yes |
| `b73c0e1` | 09-26 18:54 | Frontend, docs | Replay mode disables unfaithful controls, map glyphs, `.gitignore` journal | Yes (UI) |
| `7a2a9e9` | 09-26 20:33 | Backend, ML, frontend (58 files) | `providers/geo.py`, `providers/data.py`, GNN v2 retrain (`train_cascade.py`, `hx_cascade_v2.pt`), twin-model forecaster, `event_delay` / `transport_redistribution` actions, journey transport preference | Yes |
| `71f5290` | 09-27 00:18 | Backend, frontend, docs (55 files) | Deterministic `cascade_flow.py`, event CRUD and `Engine.reconcile()`, `test_events.py` / `test_hardening.py`, `implementationstate.md` / `contracts.md` / `changelock.md`, **deleted `Backend/.env.example`**, removed landing-page panels | Yes |

**Present on Mehul, absent on Paresh:**
- Backend: `Backend/run_demo.py`, `Backend/tests/test_phase0.py`, `test_phase1_{api,equilibrium,interventions,risk,twin,whatif}.py`.
- Frontend: `Frontend/scripts/test-ws-reconnect.mjs`, `test-store-timeline.mjs`, `components/ObserverBar.jsx`, `ObserverDecisionBanner.jsx`, `lib/useResolveIntervention.js`.
- Docs: `FINAL_AUDIT_REPORT.md`, `PHASE0_VALIDATION_REPORT.md`, `PHASE1_VALIDATION_REPORT.md`, `plot.md`.
- The Phase 1 rewrite of `ml_reference/equilibrium.py`. Paresh never modified that file, so it is still the `92b97bd` version.

---

## 3. Documentation Inventory

| File | Purpose | Relevant claims | Current / stale? |
|---|---|---|---|
| `CLAUDE.md` | Agent guide | Contract rules; "126 tests"; three worlds; providers; GNN v2; "no component fetches its own data"; `.env.example` exists; CI | Mostly current. **Stale:** `.env.example` (deleted); "no component fetches its own data" (violated, see §23) |
| `README.md` | Original contract-set overview / hackathon plan | H0–H33 plan, cut order | Planning doc, stale as a status source |
| `README.txt` | Long overview | "76 tests", "17 mocks", routes `/`, `/attendee`, `/metrics` only; `.env.example` | **Stale** (126 tests, 23 mocks, 10 routes) |
| `RUNNING.md` | Run guide + "how it works" + real-vs-reference table | 10 pages; 10× default; providers; 126 tests; "What is real" | Largely current; verified in §17 |
| `implementationstate.md` (new) | "Current, verified description" | Architecture, cycle, event model, reconciliation, flow physics, risk, cascade, interventions, What-If, WS; known limitations | Mostly accurate; some claims contradicted (§23) |
| `contracts.md` (new) | Stage-by-stage causal contract | Venue `current_count ≤ nominal_capacity` always; event endpoints; WS events | Mostly accurate; venue cap claim violated at runtime |
| `changelock.md` (new) | Scope lock for the "final hardening pass" | "ML/ is not modified in this pass"; "retraining ML" NOT allowed | Accurate for `71f5290` only; ML retraining happened in the earlier commit `7a2a9e9` |
| `00_SHARED_CONTRACT.md` … `03_ML_CONTRACT.md` | Frozen wire contracts | Types, enums, endpoints, WS | **Unmodified by Paresh.** New endpoints, enums and fields are not reflected (§11) |
| `ML/README.md` | ML drop-in contract | 8 modules, `.fallback()`, no I/O | Current |
| `Backend/README.md`, `Frontend/README.md` | One-line stubs | — | Empty |
| `plot.md` | Roadmap (Mehul) | — | **Absent on Paresh** (exists only on Mehul) |
| `changelog.md` | — | — | **Absent** (Paresh has `changelock.md`, a scope lock, not a changelog) |
| `docs/**` | — | — | **Absent** |
| `.github/workflows/ci.yml` | CI | pytest → export schemas → validate mocks → build | Present, tracked. Python 3.12 in CI vs 3.13 locally; **not observed running** |

---

## 4. Documentation Claims

What the repository says exists on Paresh:

- **Simulation.** Three worlds (live `generator`, `nominal` twin process model, `counterfactuals[iid]`), all flow-model `SyntheticGenerator`s under `world_lock`. Deterministic, "No RNG anywhere" (`generator.py` docstring).
- **Events.** Create, edit, cancel and delete with exact UTC datetimes and windows. Every mutation ends with `Engine.reconcile()`, broadcast before the HTTP response, measured at "100–150 ms".
- **People flow.**
  - Fluid queues at stations and gates.
  - Gate spill onto roads.
  - Venue occupancy "hard-capped at capacity".
  - The per-event ledger is conserved.
- **Risk.** Utilisation floor plus bounded escalations. Per-type critical lines: venue 1.02, hotel 0.98. Band hysteresis of 3 cycles.
- **Cascade.** Deterministic, topology- and flow-based; the ML adds `confidence`.
- **Interventions.** Simulated on a clone before proposal, certified, ranked. Approval applies to the live and nominal worlds and forks a do-nothing copy; settled after 900 s.
- **What-If.** Baseline and scenario clones of the live world; the live world is never touched.
- **Attendee.** Time-dependent Dijkstra with BPR congestion; departure options.
- **External data.** "Everything runs on synthetic data by default." Geo provider `synthetic` / `osrm` / `google` via env vars, with timeout, retry, cache and fallback. Data provider `file` reads `events.json`, `hotels.json`, `topology.json`.
- **ML.** GNN v2 "beats a forecast-threshold baseline on average precision at every horizon" on 20 held-out *scenarios*.
- **No venue, radius, footprint, blueprint or geocoding claims** anywhere in Paresh's documentation. Mehul's `plot.md` lists "Venue discovery / geospatial ingestion" as **Later / Not started**.

---

## 5. Git Change Inventory

1. **New files (45).**
   - Backend modules: `catalog.py`, `ml_reference/city_model.py`, `providers/{__init__,data,geo}.py`, `services/{accommodation,cascade_flow,evaluation,events,projection}.py`, `scripts/train_cascade.py`.
   - Backend tests: `tests/{conftest,test_events,test_hardening,test_product,test_providers}.py`.
   - Frontend components and libs: `DomainBoard`, `NavBar`, `PageShell`, `lib/useLiveQuery.js`.
   - Frontend routes: `Accommodation`, `Commander`, `Events`, `Interventions`, `Network`, `WhatIf`.
   - Mocks: `disruptions`, `events`, `hotels`, `overview`, `saturation`, `stay_recommendation`.
   - ML: `ML/{cascade_eval_v2.json, feature_norm_v2.json, hx_cascade_v2.pt}`.
   - Schemas: 7 new files.
   - Docs: `changelock.md`, `contracts.md`, `implementationstate.md`.
   - Other: `Frontend/test.css` (unused).
2. **Modified (95).** Core backend (`engine`, `generator`, `twin`, `risk`, `optimiser`, `forecaster`, `cascade`, `routes`, `schemas`, `state_store`, `commander`, `attendee`, `metrics`, `simulation`, `topology`, `config`), most frontend components and routes, all mocks, `ML/cascade.py`, `CLAUDE.md`, `README.txt`, `RUNNING.md`.
3. **Deleted (1).** `Backend/.env.example`.
4. **Renamed.** None.
5. **New dependencies.** `lucide-react` (frontend; shared with Mehul). No new Python dependencies. `npm test` on Paresh **drops** Mehul's `test-ws-reconnect.mjs` and `test-store-timeline.mjs`.
6. **Configuration** (`Backend/config.yaml`):
   - `city_model: flow`, `speed_multiplier: 10`
   - `thresholds.by_type` (venue 0.97/1.02, hotel 0.92/0.98), `band_hold_cycles`, `post_change_no_hold_cycles`
   - `forecaster.model_refresh_cycles`
   - `cascade.gnn_checkpoint: ML/hx_cascade_v2.pt`, `max_roots`, `max_steps`, `gnn_min_probability`, `exclude_root_types`
   - risk escalation caps
   - `budgets_ms.evaluate`
   - `interventions.*` (TTL, effect duration, settle delay, evaluation horizon, `min_relief_pct`, `default_compliance`)
   - `demand.*`, `hospitality.*`, `projection.*`, `attendee.*`, `geo.*`, `data.*`
   - a 3-event `events:` schedule
7. **Environment.** New optional variables: `EVENTFLOW_GEO_PROVIDER`, `OSRM_URL`, `GOOGLE_MAPS_API_KEY`. `Frontend/.env.live` (`VITE_MOCK=0`) is committed. `Backend/.env.example` is deleted. A gitignored `Backend/.env` exists locally; it was not read, and **the app never loads it** (no `load_dotenv` call anywhere in `Backend/app`).
8. **API.** 16 new REST routes (§11). Changed payloads for Intervention, Cascade, EntityState, Simulation, Metrics and Regret. New WS events.
9. **Contracts.** 7 new schema files, 21 modified. The `InterventionType` and `ScenarioType` enums were extended without updating `00`.
10. **Database.** New tables `event_schedule` and `hotel_property`. No migrations (`create_all`).
11. **Backend.** A near-rewrite of the simulation core (§7).
12. **Frontend.** NavBar with 10 pages. The Command Centre is now map-first. Per-page REST snapshots via `useLiveQuery`. Replay-mode honesty.
13. **ML.** `ML/cascade.py` (forecast features, v2 norm file), a new v2 checkpoint, retraining script, eval JSON.
14. **Scripts and tooling.** `export_mocks` (160 cycles), `export_schemas`, `train_cascade.py`, `validate-mocks.mjs` (23 files).
15. **Docs.** Three new docs; `CLAUDE.md`, `RUNNING.md`, `README.txt` updated (README.txt is partially stale).

---

## 6. Actual Architecture (from code)

```
DATA SOURCES   topology.py (hard-coded 67 nodes / 127 edges, Mumbai-offset coords)  ─┐
               catalog.py (20 hard-coded hotel properties)                            ├─ providers/data.py
               config.yaml events: (3 events)                                         ─┘   (synthetic | file)
               providers/geo.py (synthetic haversine×1.3 | OSRM | Google; env-selected)
       ↓
INGESTION      StateStore.__init__: provider.topology() → topology.verify() (demo-graph guard)
               → provider.properties() → verify_catalogue(); seed_topology() writes entity/edge/segment/hotel rows
       ↓
STATE          Engine (app/services/engine.py): the only writer; StateStore (in-memory) + SQLite run tables
       ↓
EVENT GRAPH    Static: store.nodes / store.edges (never regenerated at runtime)
       ↓
SIMULATION     SyntheticGenerator flow model ×3 worlds (live, nominal, counterfactuals) + clones for
               What-If / evaluation / projection (run_forward); modifiers (disruptions, interventions, schedule)
       ↓
ML             ml_registry.MLRegistry: 7 reference modules + ML/cascade.py (R-GCN v2) via call_ml (budget, fallback)
               Twin = localised EnKF over [count; flow], process model = nominal world
       ↓
RISK           RiskScorer (arithmetic) → cascade_flow (deterministic) + GNN confidence → re-score → band hysteresis
       ↓
WHAT-IF        services/simulation.py: clone live ×2, inject into one, run_forward, compare (+ candidate actions)
       ↓
OPTIMISATION   InterventionOptimiser templates → evaluation.py (simulate each on a clone) → EquilibriumSolver.certify → rank
       ↓
INTERVENTION   POST approve → Engine.approve_intervention (fork CF, apply modifier to live + nominal) → reconcile
               → live_effect each cycle → settle at +900 s (regret ledger)
       ↓
FRONTEND       App.jsx bootstrap (REST) + lib/ws.js → zustand store; pages also fetch REST snapshots (useLiveQuery)
       ↓
ATTENDEE /     /attendee (journey, nudges, stay), operator pages (events, hotels, transport, crowd, interventions,
ORGANISER      what-if, commander, metrics). "Organiser" input = event CRUD + disruptions + approvals only.
```

**Background loops.**
- `Engine._loop` (asyncio task). It sleeps `max(0.2, 30/speed)` with a single `asyncio.sleep`, so it cannot be interrupted.
- What-If jobs run as FastAPI `BackgroundTasks` (thread).

**WebSocket.**
- `/ws?client=…`. On connect it sends `resync`, and it replies to `{action: resync|ping}`.
- `seq` is a per-process counter (`ws/manager.py`).

**Database tables (16).** `entity`, `graph_edge`, `segment`, `entity_state`, `forecast`, `risk_state`, `cascade_prediction`, `intervention`, `certificate`, `execution`, `regret_entry`, `nudge`, `audit_log`, `commander_log`, `event_schedule` (new), `hotel_property` (new).

| Subsystem | In | What happens | Out | Static | Dynamic | Mocked |
|---|---|---|---|---|---|---|
| Topology | code / file | build + verify | nodes, edges, segments, bounds | the whole graph (default) | only via `file` provider at startup | no |
| City model | schedule, modifiers, compliance | 30 s flow step | counts, flows, queues, observations | physics constants | all state | no |
| Twin | observations, nominal delta | EnKF + relaxation | estimates for unobserved | config | yes | no |
| Forecast | history + nominal projection | twin-model projection + gap correction | 15/30/60-min forecasts | — | yes | no (TSFM absent → local model) |
| Risk | state, forecast, exposure | floor + escalations | score / band | thresholds | yes | no |
| Cascade | forecasts, edges | overflow propagation; GNN confidence | cascades | — | yes | no |
| Interventions | root, cascade, state | templates → simulate → certify → rank | proposals | template list | relief measured | no |
| What-If | scenarios | two clones run forward | deltas, top changes, candidates | — | yes | no |
| Frontend | REST + WS | render | UI | labels | yes | mock mode replays fixtures |

---

## 7. Backend

| Area | Finding | Classification |
|---|---|---|
| Startup | `main.py` lifespan: `create_all` → `clear_run_tables` → `Engine()` → seed → `persist_events` → `prime_state` → loop. `run.py` uses `reload=True` on `0.0.0.0:8000`. **No `run_demo.py`.** | DYNAMIC (dev only) |
| Config | `config.yaml` via `EVENTFLOW_CONFIG`. Env overrides: `DATABASE_URL`, `REDIS_URL`, `LOCAL_LLM_URL/MODEL`, geo vars. `.env` is never loaded. | DYNAMIC |
| Routes | 40 REST routes plus `/ws`; every route returns a Pydantic model | DYNAMIC |
| WebSocket | resync on connect; per-process `seq`; events `tick`, `state_update`, `forecast_update`, `cascade_alert`, `cascade_update`, `intervention_*`, `event_updated/deleted`, `state_reconciled`, `disruption_update`, `twin_fidelity`, `regret_update`, `anomaly`, `nudge_pushed`, `journey_risk_update`, `resync`. **No `demo_status`.** | DYNAMIC |
| Engine / cycle | tick 3 worlds → twin step + assimilate → merge → forecast → risk → anomaly → cascade (+GNN) → re-risk + hysteresis → interventions (propose, simulate, certify, rank, expire, settle) → persist → cache → broadcast. p50 12.8 ms, p95 74.8 ms per cycle in-process. | DYNAMIC |
| Simulation timing | wall sleep `max(0.2, 30/speed)`. Speed is silently clamped to [0.5, 600] (`engine.py:1397`); 600× is reported but the effective ceiling is 150× (`MIN_WALL_SLEEP = 0.2`). **Not interruptible** (58 s measured). | PARTIALLY_DYNAMIC / regression |
| State store | in-memory, single writer | DYNAMIC |
| Topology | hard-coded 67 nodes / 127 edges (`topology.py`); `verify()` enforces demo structure | HARDCODED (by design; blocks custom graphs) |
| Database | SQLite; run tables cleared on start and reset; schedule and hotels persisted; no migrations | DYNAMIC |
| Errors | `ApiError` envelope; validation → 400 `INVALID_REQUEST` | DYNAMIC |
| Caching | in-process `CACHE`, cleared on reset | DYNAMIC |
| Demo controls | play, pause, reset, seek, set_speed, inject. **No step, next_decision, auto-pause or status.** | PARTIAL (Phase 0 regression) |
| Interventions | 12 types; physics verified (§16). `zone_incentive zone_core→zone_north`: 0 entities changed in 900 s (zone diversion only acts on modelled zone inflow). `emergency_corridor` acts only once the emergency coupling triggers. | DYNAMIC (with caveats) |
| Optimisation | rule templates; relief = simulated drop at the triggering root on a clone of the **ground-truth** world | PARTIALLY_DYNAMIC |
| Equilibrium | pre-Phase-1 code: relief re-applied per iteration, zone-wide surface, formula breach time | BROKEN vs Phase 1D (§16) |
| Risk | floor + escalations; venue critical line 1.02 | DYNAMIC; venue ≥1.0 → HIGH (regression) |
| Forecasting | "twin_model" (nominal projection + live-gap correction). `tsfm_enabled: true` but Chronos is not installed, so it degrades to the local model. `/health` reports `twin_model`. | DYNAMIC |
| Digital twin | localised EnKF; unobserved entities relax toward the nominal world; clip at 2.5× capacity; bounded (§16) | DYNAMIC (cross-process non-deterministic) |
| Cascade / GNN | `cascade_flow.py` deterministic overflow propagation; the GNN only sets `confidence` on steps (`ml_enhanced: true`, `source: deterministic`) | PARTIALLY_DYNAMIC (ML annotative) |
| Attendee | time-dependent Dijkstra on projection; BPR; departure options; preference changes the plan (car: risk 15 vs 24) | DYNAMIC |
| Commander | deterministic templates over tool calls; optional Ollama; cache invalidated on reset but **not on seek** | DYNAMIC; partial regression |
| Metrics | computed from store (settlements, cascade eval, twin coverage); no hard-coded values found | DYNAMIC |
| Persistence | per cycle `entity_state`; forecasts every 10th cycle | DYNAMIC |
| Reset / seek | reset rebuilds worlds, re-primes, clears the cache and Commander cache, broadcasts `resync`. Seek replays the generator but does **not** reset the twin, interventions or Commander cache. | DYNAMIC / PARTIAL |

---

## 8. Frontend

- **Routes.** `/`, `/events`, `/accommodation`, `/transport`, `/crowd` (shared `DomainBoard`), `/interventions`, `/whatif`, `/attendee`, `/commander`, `/metrics`. `Network.jsx` supplies Transport and Crowd.
- **State.** Zustand `useStore`. `state_update` merges; `resync` → `replaceAll`, which clears per-run UI state when `cycle_number` goes backwards. Interventions are sorted by backend `rank_score` only.
- **API client** (`lib/api.js`). `MOCK_MODE` unless `VITE_MOCK=0`. Mutating calls return `mocks.liveOnly()` in mock mode.
- **WebSocket client** (`lib/ws.js`). `lastSeq` persists across connections: `onopen` at `ws.js:68` does not reset it. See the regression in §15.
- **Data provenance.** All numbers come from the backend. The frontend has no local risk heuristics: a search found no `score > N` or `utilisation >` banding and no `Math.random` except toast IDs.
- **Rule violation.** "No component fetches its own data" (CLAUDE.md) is **not true on Paresh**. `Events`, `Accommodation`, `Attendee`, `Metrics`, `Interventions`, `WhatIf`, `DomainBoard` and `EntityDetailPanel` call `api.*` directly.
- **Hard-coded UI.** Speed list `[1, 5, 10, 30, 60]`. Label "LIVE FEED" (`TopBar.jsx:96`); Mehul had renamed it "LIVE SIMULATION" as an honesty fix. Title "NATIONAL CUP FINAL / C2 OPS". Attendee ID `att_demo_1`.

**Control paths verified in a real browser** (headless Chrome 154, `npm run dev:live` against the scratch backend):

| Control | Path | Result |
|---|---|---|
| Pause | NavBar → `api.demoControl({action:'pause'})` → `Engine.demo_control` → `paused=True` | backend `paused`; cycle 209 → 209 over 4 s ✔ |
| Resume | → `play` | `playing` ✔ |
| Speed select (30) | → `{action:'set_speed', speed_multiplier:30}` | backend 30 ✔ |
| Approve (Interventions page) | `InterventionQueue` → `POST /interventions/{id}/approve` → `approve_intervention` + `reconcile` | `int_23b22` → `executing` ✔. The top card approved was certified **UNSTABLE** (rank 0.17). |
| Event create/edit/delete, disruptions, What-If, journey, nudges, stay | code-traced to `api.*`; the backend effect was verified via API/in-process (§12) | real |
| Drift-mode toggle | `TwinFidelityGauge` → `POST /twin/drift-mode` | real (not browser-exercised) |

- **Routes in live mode.** All 10 rendered with **0 JS errors** (only a `favicon.ico` 404).
- **Mock mode** (`npm run dev`, port 5198):
  - every page shows "Recorded replay";
  - mutating controls are disabled (e.g. Events 19 disabled, Transport 55 disabled);
  - 0 JS errors;
  - the TopBar shows "RECORDED REPLAY" plus an "OFFLINE" WS chip.
- **Label overclaims.**
  - "LIVE FEED" on a simulation.
  - The GNN badge says `gnn` in `/health` while cascades are deterministic (`source: deterministic`, GNN = confidence only).
  - The **map is not a geographic basemap**: deck.gl scatter over synthetic offsets.

---

## 9. ML

| Component | Real model or heuristic | Runtime use | Fallback | Deterministic | Tested |
|---|---|---|---|---|---|
| Twin (`ml_reference/twin.py`) | Real EnKF (localised, per-entity gain), arithmetic process model from the nominal world | every cycle | `fallback()` holds observations | **Within a process yes; across processes no** (hash order → RNG draw order) | `test_twin_estimates_for_unobserved_entities_track_truth` + experiments |
| Forecaster | Heuristic: nominal-world projection + decaying live-gap correction; TSFM path present but Chronos is not installed | every cycle (refresh every 4 cycles) | trend reference | yes (given twin) | `test_forecaster_failure_does_not_stop_the_cycle` |
| Risk | Arithmetic by design | every cycle | utilisation floor | yes | 9+ tests (none cover the venue/hotel per-type lines) |
| Anomaly | z-score | every cycle | — | yes | contract tests |
| Cascade | **Real trained R-GCN** `ML/hx_cascade_v2.pt` (242 KB; `torch.load(weights_only=True)`) trained by `scripts/train_cascade.py` on 80 flow-simulator scenarios **of the same fixed topology**; eval on 20 held-out scenarios (AP 0.94–0.97 vs baseline 0.51–0.87) | Every cycle via `call_ml("cascade.predict_all")`. **Only annotates** deterministic `cascade_flow` steps with `confidence` (runtime: `road_5` 0.99, `emergency_south` 0.948, other steps `null`) | the deterministic propagator | yes | `test_gnn_uses_forecast_features…`, `test_live_cascades_use_real_edges_and_work_without_ml` |
| Optimiser | Rule templates; relief measured by simulation | on proposals | — | yes | several |
| Equilibrium | Arithmetic best-response loop (pre-Phase-1) | per candidate | `fallback()` = UNSTABLE rows | yes | `test_ranking_demotes…` only |
| Generator | Deterministic flow model (no RNG; `stable_unit` hashing) | always | hold last observation | yes (values identical across processes) | many |

- **Calibration.** None of these modules is calibrated against real data. The GNN's held-out set shares the training topology, so it says nothing about generalisation to a new venue graph. `ML/README.md` itself asks for held-out *topologies*.
- **A new venue graph would invalidate the GNN.** It was trained on the one topology; its features and edge types are generic, but it has never seen another graph.

---

## 10. Database

- **Engine.** SQLAlchemy 2.0 on SQLite by default (`Backend/eventflow.db`, 213 MB locally, gitignored). `DATABASE_URL` overrides it. **No Alembic or migrations**: `create_all` only, so new columns on existing tables would not be added to an old DB.
- **Schema changes.** Two new tables: `event_schedule` (mirror of the current schedule, rewritten on start, reset and change) and `hotel_property` (seeded from the catalogue once; existing IDs are skipped).
- **Per cycle.** `entity_state` rows every cycle; `forecast` every 10th cycle; `risk_state`. Run tables are cleared at startup and on reset.
- **Persisting across restarts:** `intervention`, `certificate`, `execution`, `regret_entry`, `nudge`, `audit_log`, `commander_log`.
- **Topology.** `entity` and `graph_edge` rows are seeded from the in-memory topology but **never read back**. The DB is a sink, not the source of the graph.
- **Seeding.** Generated from code (`topology.py`, `catalog.py`, config events), not from fixtures.
- **Known limitation (documented).** Two processes on one SQLite file collide on `(entity_id, sim_time)`. The harness here produced one such `IntegrityError` when the TestClient's background loop raced a manual cycle. That is a harness artefact; the product logs it and continues.

---

## 11. API / Contracts

**New REST routes on Paresh (not in `01_BACKEND_CONTRACT.md`):**

| Route | Request | Data source | Runtime status |
|---|---|---|---|
| `GET /geo/travel` | `from_entity_id`, `to_entity_id`, `mode` | geo provider (synthetic) | ✔ airport→stadium walk 2,780 s / drive 556 s; unknown ID → 404 |
| `GET /overview` | — | `entity_states` + generator | ✔ |
| `GET /events`, `GET /events/venues`, `GET /events/{id}` | — | `EventSchedule` + generator | ✔ (venues = 2 venues + 7 zones) |
| `POST /events` | `EventCreateRequest` (operator_id required) | → all worlds + reconcile | ✔ dynamic (§12) |
| `POST /events/{id}` | `EventUpdateRequest` | same | ✔ (tests) |
| `DELETE /events/{id}` | `operator_id` query | same | ✔ (tests) |
| `GET /accommodation/hotels`, `/hotels/{id}`, `/saturation`; `POST /accommodation/recommend` | filters | catalogue + generator bookings | ✔ recommendation scores change with inputs |
| `GET/POST /disruptions`, `DELETE /disruptions/{id}` | scenario | live world + counterfactuals (not nominal) | ✔ (tests) |

**Contract findings.**
- **Documented but missing on Paresh:** `GET /demo/status`, the actions `step` and `next_decision`, the `auto_pause_on_intervention` field, and the WS `demo_status` event. These are Mehul's Phase 0 additions; they return 404 or 400 on Paresh.
- **Enums extended without updating `00`.**
  - `InterventionType` gains `event_delay` and `transport_redistribution`.
  - `ScenarioType` gains `event_delay`, `event_cancellation`, `road_closure`, `station_closure` and `capacity_reduction`.
- **Divergence from Mehul.** Paresh-only fields: `Intervention.evaluation`, `Intervention.live_effect`. Mehul-only fields: `Intervention.action_effects`, `effect_model`, `Certificate.response_rate`, `TwinFidelity.ensemble_coverage`.
- **Type change vs Mehul.** `RegretEntry.realised_relief_pct` and `counterfactual_relief_pct` are nullable on Mehul and a required `number` on Paresh.
- **Accepted but unused or unvalidated parameters.**
  - `demo/control {action:"set_speed"}` with no value → 200 no-op.
  - Out-of-range speeds are silently clamped.
  - What-If `attendance_delta` accepts −200 % and 100,000 % (the latter produced `unmet_demand` +31.7 M people).
  - `capacity_reduction` accepts +500 %.
  - `weather_rain` accepts `"apocalyptic"`, which silently falls back to "moderate" ×1.12 (`generator.py:485`).
- **Correctly validated:** unknown entity (`gate_99`), wrong type (`gate_closure` on `road_4`), unknown event, empty scenarios, horizon out of range, event end < start, unknown venue, gate or hotel as venue, negative attendance.
- **Schemas.** 23/23 mocks validate against `contracts/schemas`.

---

## 12. Dynamic vs Hardcoded Analysis

Each row is an A/B test at runtime. Tests ran in-process with `engine.paused`, seed 42 and a scratch DB, unless stated otherwise.

| Feature | Input A → Output A | Input B → Output B | Verdict |
|---|---|---|---|
| Event attendance (new event at `convention_centre`, 16:30–18:00, measured at 15:15) | none → venue 0.9304, metro_a 0.4633, metro_e 0.5703 | 1,000 → 0.9374 / 0.4685 / 0.5767; 12,000 → 0.9968 / 0.5255 / 0.6263; 12,000 again → identical | **DYNAMIC**, deterministic |
| Event venue | 12,000 @ convention_centre → zone_plaza_west 0.1723 | 12,000 @ zone_plaza_west → zone_plaza_west 0.4677, shuttle_hub_west 0.8533 (convention unchanged 0.9304) | **DYNAMIC** |
| What-If scenarios (cycle 100, 1800 s) | attendance +20 % → peak +38.7 % | −20 % → −18.2 %; gate_3 closure → gate_4 0.10 → 0.49; road_4 −50 % → road_4 0.456 → 0.566 | **DYNAMIC** |
| Intervention approval | CF gate_6 1.158 / gate_3 0.075 | live gate_6 1.120 / gate_3 0.154 (+29 cycles) | **DYNAMIC** |
| Geo travel | hotel_north→stadium walk 1,731 s | hotel_airport→stadium walk 2,780 s, drive 556 s | **DYNAMIC** (synthetic haversine) |
| Stay recommendation | default top score 0.670 | ≤ ₹3,000 → 0.747; premium → 0.785; to convention → 0.726 | **DYNAMIC** |
| Journey | hotel_north → risk 24, peak 39 % | hotel_airport → peak 41 %; car preference → risk 15, peak 26 % | **DYNAMIC** |
| Topology via `file` provider | default: stadium (19.076, 72.8777), cap 70,000; util 0.6076 @ 15:35 | relocated to London, cap 35,000 → (51.556, −0.2796); util 1.0076, gate_3 0.08 → 0.42; bounds and geo change | **DYNAMIC** for same-ID graphs; **custom graphs are rejected** |
| Venue selection for events | fixed list of 9 existing entities | — | **HARDCODED** (graph entities only) |
| Radius / footprint / blueprint | — | — | **NOT_IMPLEMENTED** |
| Hotel catalogue | 20 fixed properties (`catalog.py`) | only replaceable via `hotels.json` | **HARDCODED** default |
| Segments (5 behavioural segments) | fixed in `topology.py` | replaceable via file provider | **HARDCODED** default (assumptions) |
| GNN | confidence changes with state | structure is deterministic | **PARTIALLY_DYNAMIC** |
| Speed control | 10 → 30 applied | 0 / −1 / NaN clamped silently | **PARTIALLY_DYNAMIC** |

---

## 13. Venue / Radius / Footprint / Blueprint / Graph

**Overall: NOT IMPLEMENTED.** The repository-wide search for `radius|footprint|blueprint|geocod|nominatim|overpass|openstreetmap|osm|polygon|geojson|shapely|bbox` found only `geo.py`'s `EARTH_RADIUS_M` and frontend dot-radius code.

| Stage | Current state | Evidence |
|---|---|---|
| **Venue** | No venue search, geocoding or free text. "Venue" = choose an existing graph entity (2 venues + 7 zones) for an event (`GET /events/venues`, dropdown in `Events.jsx`). Venue identity and coordinates are hard-coded in `topology.py` (`VENUE_LAT, VENUE_LON = 19.0760, 72.8777`; every node is an offset). | `topology.py:16`; runtime list of 9 entities |
| **Radius** | Does not exist: no parameter, config key, endpoint or UI. Nothing could be tested for small, medium or large radius because there is no input. | search results |
| **Footprint** | Does not exist. `bounds` is `min/max` of node lat/lon ± 0.004 (`topology.build_bounds`), used only for map framing. | `topology.py:387` |
| **Blueprint** | Does not exist as a pipeline. Nodes, edges, capacities, travel times and types are **hand-authored** in `topology.py`. There is no POI classification, deduplication, filtering or provenance field. The file provider only normalises (`slug`, type whitelist, capacity > 0, edge endpoints exist). | `providers/data.py:normalise_node` |
| **Event graph** | Built once at startup. Never regenerated or replaced at runtime (no endpoint). A `file` topology replaces it at startup **only if it passes `verify()`**, which requires ≥60 nodes, entities `metro_b, gate_3, road_4, emergency_north, gate_5`, the edges `metro_b→gate_3→road_4→emergency_north` and `metro_c__gate_5__feeds`, and segment shares summing to 1. | 4-node custom graph → "REJECTED: 01 §8 requires >=60 nodes"; relocated 67-node graph → PASSED and ran |

**Other couplings that block a generated graph** (each would break or silently misbehave with non-demo IDs):
- `catalog.build_properties`: `by_id[cluster]` → `KeyError: 'hotel_north_cluster'` (reproduced) unless `hotels.json` is supplied.
- `verify_catalogue`: requires hotel rooms = cluster capacity.
- `generator.py:155`: bus hubs are identified by the `bus_` prefix.
- `generator.py:370`: `concurrent_event` looks up `evt_demo`; `:374` defaults its venue to `zone_fanpark`.
- `generator.py:494` and `:503`: defaults `evt_demo`.
- `generator.py:598`: `_main_venue` falls back to `stadium_main`.
- `attendee.py:242`: hub detection by `shuttle_hub`/`bus_hub` prefix.
- The GNN was trained on this single topology.
- The Frontend TopBar is titled from `/event`; that part is not hard-coded.

**Data sources for the chain:**

| Source | Classification |
|---|---|
| Graph nodes and edges | HARDCODED FIXTURE (in code); STATIC DATASET if `topology.json` |
| Coordinates | SYNTHETIC (offsets from a Mumbai point) |
| Capacities, travel times, coefficients | HARDCODED (hand-chosen to produce the demo cascade and the gate_5 trap, per comments) |
| Hotels | HARDCODED FIXTURE (20 properties; per-property occupancy, offsets and walk times derived by `stable_unit`) |
| Events | ORGANIZER INPUT (CRUD) on top of a config schedule |
| Travel times (attendee car/walk, `/geo/travel`) | SYNTHETIC (haversine × 1.3); OSRM/Google optional with FALLBACK |
| External POI / map data | NONE |

---

## 14. Data Provenance

| Dataset | Provenance | Retrieval time | Refreshable | User input affects it |
|---|---|---|---|---|
| Topology (67 / 127) | GENERATED in code (`topology.py`) | n/a | restart only (file provider) | no |
| Hotel properties (20) | FIXTURE in code (`catalog.py`) + `stable_unit` | n/a | restart only (`hotels.json`) | no |
| Event schedule | config (3 events) + ORGANIZER INPUT; mirrored to DB | on change | yes | yes |
| Crowd state | GENERATED by the flow simulator (SYNTHETIC ground truth) | per cycle | yes | yes (events, disruptions, approvals, compliance) |
| "Sensor" readings | SYNTHETIC: truth × (1 ± 1.5 %), 3 % dropout, 75 % coverage of partially instrumented types | per cycle | yes | no |
| Twin estimates | GENERATED (EnKF) | per cycle | yes | indirectly |
| Travel times | SYNTHETIC haversine; REAL EXTERNAL only if OSRM/Google are configured (not exercised) | on request, cached 1 h | yes | yes (entities/mode) |
| GNN weights | trained offline on SYNTHETIC flow-simulator data | commit `7a2a9e9` | re-run `train_cascade.py` | no |
| Segments / elasticities | HARDCODED assumptions (`topology.SEGMENTS`) | n/a | no | no |
| Frontend mocks | GENERATED by `export_mocks` from a real backend run (160 cycles) | commit time | regenerate | no |

No record carries a provenance, confidence or retrieval-timestamp field. There is no provenance model at all.

---

## 15. Phase 0 Compatibility

The baseline is Mehul's `PHASE0_VALIDATION_REPORT.md`. None of Mehul's Phase 0 code exists on Paresh, so every item below was re-tested against Paresh's own implementation.

| Item | Result | Evidence |
|---|---|---|
| WS reconnect after backend restart | **REGRESSION** | (1) Mehul's `test-ws-reconnect.mjs`, run against Paresh's `ws.js` (scratch copy with an `import.meta.env` shim): **6 assertions fail**, including "new connection = new sequence context", "restarted server's resync (seq 1) is applied" and "UI resumes updating after restart". (2) Real browser: before the restart, UI = backend = 392/393/394. After killing and relaunching the backend, the UI stayed at **394** at +2/+8/+16/+24 s while the backend was at 1/3/6/9, and the chip read "LIVE FEED" (no RECONNECTING visible). A manual reload → 11 = 11. Root cause: `ws.js:84` drops `seq <= lastSeq`, and `lastSeq` is not reset in `onopen` (`ws.js:68`), while the server `seq` restarts at 1 per process. |
| Speed validation | **REGRESSION** | `0` → 200, 0.5×; `-1` → 200, 0.5×; `1e9` / `NaN` / `Infinity` → 200, 600×; `"fast"` → 400; `set_speed` without a value → 200. There is no freeze (the clamp prevents it), but invalid input is not rejected, and 600× is reported while the effective ceiling is 150×. |
| Interruptible clock | **REGRESSION** | At 0.5×, then 60×: the next cycle came only after **58 s**. `_loop` uses one `asyncio.sleep(wall_sleep)` (`engine.py:162–178`). |
| Commander cache invalidation | **REGRESSION (partial)** | Reset: `is_cached: false` with fresh text ✔ (`_reset` clears `_cache`). Seek from cycle 84 back to 14:05: `is_cached: true` with the **pre-seek text** ✘. The negative cycle delta is treated as fresh (`commander.py:472`). |
| `run_demo.py` | **REGRESSION** | file absent; `run.py` uses `reload=True` |
| Observer: Play / Pause | PASS | browser: Pause → backend paused, cycle frozen; Resume → playing |
| Step | **REGRESSION** | `{"action":"step"}` → 400 literal_error |
| Next Decision | **REGRESSION** | `{"action":"next_decision"}` → 400 |
| Speed controls | PASS (UI) | select 30 → backend 30. Options are 1–60× only; 0.5× is not offered. |
| Auto-pause on proposal | **REGRESSION** | `auto_pause_on_intervention` → 400 extra_forbidden |
| `GET /demo/status` | **REGRESSION** | 404 |
| Reset / resync | PASS | reset → `resync` broadcast (`routes.py:449`), `prime_state` gives a non-blank map, the store clears per-run UI state on cycle regression (`useStore.js:303–322`); `test_reset_returns_the_initial_city_even_when_paused` passes |
| Honest labels | PARTIAL | the real cycle number is shown (fixed in `d0b916a`) ✔; "LIVE FEED" instead of "LIVE SIMULATION" ✘; replay banner ✔ |
| Mock mode | PASS | 10 routes, replay banner, controls disabled, 0 JS errors; 23/23 mocks valid |

**Phase 0 overall: REGRESSION.** 8 items regressed, 5 pass, 1 is partial.

---

## 16. Phase 1 Compatibility

The baseline is Mehul's `PHASE1_VALIDATION_REPORT.md`. Paresh reimplemented the core independently (flow model), so these checks are behavioural.

| Item | Result | Evidence |
|---|---|---|
| **Twin: bounded unobserved estimates** | PASS | 600 cycles; 10 unobserved entities; RMSE 0.0022 / 0.0030 / 0.0028 / 0.0033 / 0.0037 / 0.0032 at cycles 1/10/30/100/300/600; max \|err\| ≤ 0.0072. Under an **unannounced** `gate_3` closure: +1 → 0.0024, +10 → 0.0022, +30 → 0.0030, +60 → 0.0096 (growing slowly; worst `road_6` −0.028). |
| Twin: no 0/2 artefacts | PASS | pinned count 0 at every checkpoint |
| Twin: no exploding flows | PASS | max \|mean flow\|/cap/min 0.011–0.176 (0.176 at cycle 1 only) |
| Twin: finite, non-negative | PASS | NaN 0; negative members 0 |
| Twin: branch RNG isolation | PASS (N/A path) | `twin.branch()` is unused. What-If and evaluation use generator clones, and the A/B isolation test (below) shows the twin ensemble sum identical. |
| Twin: reproducible | **PARTIAL** | same process after reset: identical ✔. Across processes: state, forecast and twin differ (3 runs, 3 fingerprints); `PYTHONHASHSEED=0` ×2 identical, `=1` differs. Root cause: `generator._counts` key order follows set-iteration order (e.g. `generator.py:980`), and `twin._assimilate` draws `self._rng.normal` per observation in dict order (`twin.py:170`). |
| **What-If: same-world branches** | PASS | `simulation.py` clones `engine.generator` ×2 under `world_lock` |
| What-If: no live-state / RNG mutation | PASS | A→B and B→A What-Ifs at cycle 100 + 20 cycles give state, interventions and twin sum **identical** to the no-What-If run |
| What-If: scenario applied once | PASS (code) | a scenario is one modifier; `_rebuild` recomputes multipliers from the list (no compounding) |
| What-If: invalid entity rejected | PASS | `gate_99` → 400; `gate_closure` on `road_4` → 400 |
| What-If: invalid magnitudes rejected | **REGRESSION** | −200 %, +100,000 %, `capacity_reduction` +500 % and rain `"apocalyptic"` are all accepted (Mehul rejected them) |
| **Counterfactual: matched same-world** | PASS | the CF world after 20 cycles equals an independent no-approval run: **max \|Δ\| = 0.0** (live differs by up to 0.225) |
| Counterfactual: no arbitrary ±100 clamp | **REGRESSION** | `engine.py:1036–1037` clamp. Observed `counterfactual_relief_pct: -100.0` for `int_a7cd6`: root `road_6` 0.4605 at approval, CF 0.949 at settle → the true −106 % was clamped. |
| Counterfactual: valid regret | PARTIAL | `regret = predicted − realised` (52.7 − 44.4 = 8.3). It is measured at the **triggering root** (`road_6`), which was not one of the targets (`gate_6`, `gate_3`). |
| **Intervention: source ↓** | PASS | `gate_redistribution gate_6→gate_3` (live vs CF): gate_6 0.971 vs 0.988 (+1), 1.120 vs 1.158 (+29). `reroute metro_b→metro_c` (clones, 900 s): metro_b 0.433 vs 0.510. |
| Intervention: destination ↑ | PASS | gate_3 0.154 vs 0.075; metro_c 0.632 vs 0.543 |
| Intervention: conservation | PASS | event ledger `arrived` 68,754.206 both with and without action, for every type tested; `test_total_people_are_conserved_through_every_mutation` |
| `notify_only`: no world mutation | PASS | 0 entities changed in 900 s |
| **Equilibrium: fixed-point semantics** | **REGRESSION** | `equilibrium.py` is the `92b97bd` version. `_solve_followers` subtracts `load[e]*moved_fraction` from targets **every iteration** (`:269–276`). Sweeping relief 2/5/8/12/20/40 % on the same action: iterations 1/1/1/21/34/36, max_util 1.143→1.340. |
| Equilibrium: scoped stability | **REGRESSION** | the surface includes all zones (`:149`). Setting unrelated `zone_plaza_west`=1.3 changes the certificate's worst entity from `gate_5` to `zone_plaza_west`. Every metro_b→metro_c certificate is UNSTABLE because gate_5 is **already** at 1.14 in the baseline. |
| Equilibrium: no relief-size artefact | **REGRESSION** | see above; breach minutes = `clamp(26 − overshoot×100, 4, 45)` (`:373–376`), not a rollout |
| **Risk: utilisation ≥ 1.0 ⇒ CRITICAL** | **REGRESSION (venues)** | `config.yaml:19–20` venue critical 1.02. Scorer: venue@1.00 → 72 HIGH, @1.01 → 76 HIGH. **Runtime:** `stadium_main` 1.0089 (70,625 / 70,000) → 76 HIGH. All other types at 1.0 → CRITICAL ✔ (hotels at ≥0.98). |
| Risk: finite-input guards | PASS (no crash) | NaN → 100 critical; ∞ → 100 critical; −1 → 0 low |

**Phase 1 overall: PARTIAL.**
- **Preserved by reimplementation:** twin boundedness, What-If isolation, matched counterfactual, intervention direction and conservation, `notify_only`.
- **Regressed:** equilibrium (1D, entirely), the venue ≥1.0 rule, the settlement clamp, magnitude validation, and cross-process determinism.

---

## 17. Runtime Verification

| # | Command (scratch paths abbreviated `$SCR`) | Result |
|---|---|---|
| 1 | `git branch --show-current; git status; git log -5`; `lsof` on :8000 / :5173 | Paresh, clean, `71f5290`; ports free |
| 2 | `git fetch origin`; `git log Mehul..Paresh`; `git diff --stat/--name-status Mehul...Paresh` | see §2, §5 |
| 3 | `DATABASE_URL=sqlite:///$SCR/pytest.db .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` | **126 passed**, 885 warnings, 73.9 s |
| 4 | `cd Frontend && npm test` | **23/23 mocks valid**; mock lifecycle ✓ |
| 5 | `npx vite build --outDir $SCR/fe_dist` | built in 2.2 s; `index` chunk 3.56 MB (gzip 320 KB; bundles the 3.9 MB `state_sequence.json` mock), deck 693 KB, charts 561 KB |
| 6 | `DATABASE_URL=sqlite:///$SCR/live1.db uvicorn app.main:app --port 8765` | starts in about 1 s; log: 67 entities, 127 edges; `cascade: ml` (GNN loaded); "no TSFM available" |
| 7 | `curl /health` | ok; forecaster `twin_model`, cascade `gnn`, commander `deterministic` |
| 8 | `curl -X POST /demo/control` sweep | §15 speed rows |
| 9 | speed 0.5× → 60× timing | 58 s to next cycle |
| 10 | Commander ask / seek / reset | §15 |
| 11 | What-If edge cases via `POST /simulate` | §11 |
| 12 | `exp_phase1.py` (in-process, `$SCR/exp1.db`) | §16 (600-cycle twin, isolation, CF, physics, equilibrium, risk) |
| 13 | `exp_dynamic2.py` | §12 (events, stays, journeys) |
| 14 | `exp_topology.py`; `EVENTFLOW_CONFIG=$SCR/config_moved.yaml … exp_topo_run.py` | §13 |
| 15 | `fp2.py` ×3 (no background loop), then `PYTHONHASHSEED=0,0,1` | §18 |
| 16 | `node $SCR/wstest/scripts/t.mjs` (Mehul's reconnect test vs Paresh's `ws.js`) | 6 FAIL (§15) |
| 17 | `PORT=5199 BACKEND_URL=http://127.0.0.1:8765 npx vite --mode live`; headless Chrome `--remote-debugging-port=9333`; `node $SCR/cdp.mjs … tour/restart/controls` | 10 routes, 0 JS errors; restart freeze; controls work |
| 18 | `PORT=5198 npx vite` (mock); `cdp.mjs … mock` | replay honest, 0 errors |
| 19 | Latency probes (`curl -w %{time_total}`) | §20 |
| 20 | Cleanup: `kill 38905 39159 39216` (Chrome, vite, backend by verified PID) plus an earlier vite PID | all stopped; ports 8765 / 5199 / 5198 / 9333 free; `git status` clean |

**Side effect:** running the project's own Vite dev server may refresh its gitignored dependency cache under `Frontend/node_modules/.vite/` (the directory pre-existed). No tracked file changed.

---

## 18. Reproducibility

| Scope | Result | Classification |
|---|---|---|
| Generator (ground truth), same seed, separate processes | observation values identical across `PYTHONHASHSEED` 0/1/2 (hash of sorted values) | DETERMINISTIC |
| Engine after reset, same process, 200 cycles | states, interventions and twin sum identical | DETERMINISTIC |
| Engine, separate processes, 150 cycles | intervention IDs, types, relief, verdicts identical; cascades identical; What-If result identical; **published state, twin sum (3031405.55 / 3031472.99 / 3031528.53) and forecasts differ**; summary identical | PARTIALLY DETERMINISTIC |
| Same with fixed `PYTHONHASHSEED=0` ×2 | fully identical | DETERMINISTIC |
| Topology `/graph` | identical hash across processes | DETERMINISTIC |
| Event A/B repeat (12,000 twice) | identical | DETERMINISTIC |

**Where the randomness comes from.**
- The only RNG is `twin._rng`, seeded by `default_rng(seed)` and reset on `initialise`.
- It is consumed per observed entity in observation-dict order, and that order follows per-process string hashing.
- Scope: twin estimates → published state for unobserved entities → forecasts. It does **not** reach the generator or the counterfactual worlds.
- **Impact:** the "seed 42 identical every time" rehearsal (README H33) holds only if `PYTHONHASHSEED` is fixed. This contradicts CLAUDE.md's "never introduce non-seeded randomness (`hash()` of an id…)".

---

## 19. Hardcoded / Fixture Findings

| Value | Where | Why it exists | Replaceable at runtime? | Verdict |
|---|---|---|---|---|
| Venue centre 19.0760, 72.8777 and all node offsets | `topology.py:16`, `build_entities` | demo site | only via `file` provider at startup | HARDCODED APPLICATION DATA |
| 67 nodes / 127 edges, capacities, coefficients, travel times | `topology.py` | hand-tuned to create the demo cascade and the gate_5 trap (comments say so) | same | HARDCODED APPLICATION DATA |
| `verify()` demo-structure guard | `topology.py:419–450` | "fail loudly at startup" | no | HARDCODED CONSTRAINT (blocks new graphs) |
| 20 hotel properties, prices, tiers | `catalog.py:22–43` | accommodation demo | `hotels.json` | HARDCODED APPLICATION DATA |
| 5 segments and elasticities | `topology.py SEGMENTS` | stated assumptions | file provider | CONFIG-LIKE ASSUMPTION |
| 3 events | `config.yaml events:` | schedule | yes (CRUD) | VALID CONFIGURATION |
| Physics constants (`BG_UTIL`, service rates, `ROAD_MAX`…) | `generator.py:47–75` | model parameters | no | MODEL CONSTANTS (uncalibrated) |
| `evt_demo`, `zone_fanpark`, `stadium_main`, `bus_` prefix | `generator.py:155, 370, 374, 494, 503, 598` | demo defaults | no | HARDCODED IDs (break on a new graph) |
| `shuttle_hub` / `bus_hub` prefix | `attendee.py:242` | hub detection | no | HARDCODED IDs |
| Speed list 1–60 | `NavBar.jsx:43` | UI | no | UI CONFIG |
| `att_demo_1` | Attendee | demo identity | UI | DEMO FIXTURE |
| Mock JSON (23 files) | `Frontend/src/mocks` | recorded replay | regenerate | MOCK_FIXTURE (generated from a real run) |
| Recommendations, risk scores, ML outputs, scenario outputs | — | computed at runtime; none found hard-coded | — | DYNAMIC |
| `Frontend/test.css` (2,981 lines) | repo root of Frontend | none (imported nowhere) | — | DEAD FILE |

---

## 20. Performance Findings

| Measurement | Value |
|---|---|
| Cycle latency (in-process, 600 cycles) | mean 25.6 ms/cycle wall incl. harness; `cycle_latency_ms` p50 12.8, p95 74.8, max 79.3 ms (cap 2,000 ms) |
| REST reads | 1–3.5 ms each (`/state` 16.8 KB, `/overview` 27 KB, `/graph` 38 KB) |
| What-If, 3,600 s horizon (with candidates) | 0.32 s end-to-end |
| Journey (projection + Dijkstra) | 142 ms |
| Commander query | 58 ms |
| Backend CPU at 10× | ~0–9 % idle between cycles; about 46 % in a sample overlapping a What-If |
| Frontend bundle | main chunk 3.56 MB minified (mock data is bundled even in live mode) |
| Complexity notes | `generator.clone()` deep-copies dynamic state. Evaluation runs N+1 clones × 30 steps per proposal cycle. `_hotel_travel_times` runs Dijkstra per property at construction. `seek` replays from t=0 (O(elapsed)). All are fine at 67 nodes; a generated graph of hundreds or thousands of nodes would multiply clone and evaluation cost (not measured). |

---

## 21. Security / Configuration Findings

- **No committed secrets.** A `git grep` for keys, secrets, passwords and absolute paths found none. Google/OSRM credentials are read from the environment only and never logged (`geo.py`).
- **`Backend/.env` exists locally** (gitignored). Its contents were **not inspected**: reading it was blocked as a credential file. The app never loads it, since there is no dotenv call, so any value there has no effect unless exported in the shell.
- **No authentication or authorisation** on any mutating endpoint: approve, reject, events CRUD, disruptions, demo control and drift mode. `operator_id` is a self-declared string written to `audit_log`.
- `run.py` binds `0.0.0.0` with `reload=True`, which exposes these unauthenticated mutating endpoints on the LAN. CORS is restricted to localhost origins, but that does not stop non-browser clients.
- **Unvalidated inputs:** speed (clamped), What-If magnitudes and rain intensity (§11). Event local times without a zone are accepted as UTC (documented).
- `torch.load(..., weights_only=True)` is safe. `ML/topology_meta_FINAL.pkl` is present (pickle); it was not loaded by the runtime paths observed.
- **External calls:** only when `EVENTFLOW_GEO_PROVIDER` is `osrm` or `google`, or `LOCAL_LLM_URL` is set. Each has a timeout; geo also retries and caches. They are unrestricted in destination (the operator configures the host).
- **Machine-specific assumptions:** none in code. The macOS case-insensitive filesystem produces a harmless registry warning (`ml.cascade found at …/ml/cascade.py but failed to import as 'ml'`).
- **Test isolation:** `tests/conftest.py` points `DATABASE_URL` at a temp DB when unset.

---

## 22. Demo / Judge Readiness

| Story step | What exists | Nature |
|---|---|---|
| 1. Problem detected | Risk bands, cascades (GNN confidence), pressure timeline, forecasts | simulated, real computation |
| 2. Current state visible | Map (deck.gl dots on synthetic coordinates), domain pages, entity detail with twin layers (sensor / estimate / plan / forecast / do-nothing) | simulated; **no geographic basemap** |
| 3. Reason explained | Risk breakdown, cascade step `reason`, certificate `reason` | real computation, but the certificate reason can name an **unrelated** entity (§16), and breach minutes are a formula |
| 4. Recommendation generated | Proposals simulated on clones before display, with `evaluation` | real (relief measured on the ground-truth world, a "god view") |
| 5. Human decision | Approve / Reject in `/interventions` | real. There is no auto-pause, so at 10× a proposal lives ≥120 s wall (`min_wall_visible_sec`); at 60× this is shorter. |
| 6. Action executed | Modifier applied to the live and nominal worlds | real |
| 7. State changed | `live_effect` vs CF each cycle, map updates | real (verified) |
| 8. Outcome measured | Regret ledger at +900 s; `/metrics` | real, matched CF; clamp artefact possible (−100.0 observed) |

**Ambiguous or misleading for a judge:**
- The top-ranked card can be certified UNSTABLE because the baseline was already overloaded.
- "LIVE FEED" labels a simulation.
- A GNN badge is shown while cascade structure is deterministic.
- A venue at 101 % shows HIGH.
- **A backend restart mid-demo silently freezes the UI** while it still claims "LIVE FEED".
- There is no step or next-decision control to stage the story.

---

## 23. Documentation-vs-Code Discrepancies

| Claim | Documentation | Code | Runtime | Verdict |
|---|---|---|---|---|
| Venue occupancy never exceeds capacity | `contracts.md §4`, `implementationstate.md §5` | generator caps truth; published value = sensor reading ×(1 ± 1.5 %) | `stadium_main` 1.0089 (70,625 > 70,000) | MISLEADING |
| "At/above the critical line is always critical" | `RUNNING.md` §4 | per-type venue line 1.02 | venue 1.0089 → HIGH | PARTIALLY CONSISTENT (true per line, false for ≥100 %) |
| Seed reproduces the run exactly | `generator.py` docstring, README H33, CLAUDE.md | twin RNG order depends on hash order | differs across processes | PARTIALLY CONSISTENT |
| "No component fetches its own data" | CLAUDE.md | many pages call `api.*` | — | DOCUMENTED BUT NOT TRUE |
| `.env.example` shows Postgres / Redis / Ollama config | CLAUDE.md, README.txt | file deleted in `71f5290` | — | STALE |
| 76 tests / 17 mocks; 3 routes | README.txt | 126 tests, 23 mocks, 10 routes | 126 passed | STALE |
| GNN "beats baseline … on held-out" | RUNNING.md | eval on held-out *scenarios* of the same topology | GNN annotates only | PARTIALLY CONSISTENT (the claim is accurate but narrow) |
| Cascade `active_source: gnn` | `/health` | cascades are deterministic; ML adds confidence | `source: deterministic`, `ml_enhanced: true` | PARTIALLY CONSISTENT |
| ML not modified / no retraining | `changelock.md` | ML retrained in `7a2a9e9` (before the lock) | — | CONSISTENT for the lock window only |
| Equilibrium "fixed-point best-response solver" | RUNNING.md | damped loop that re-applies relief each iteration | iterations scale with relief | MISLEADING |
| Reconciliation within 100–150 ms | `implementationstate.md §4a` | synchronous reconcile before response | not timed in this analysis | UNVERIFIED |
| Speed control via `/demo/control` | RUNNING.md | clamps silently | verified | CONSISTENT (but unvalidated) |
| Replay mode disables unfaithful controls | RUNNING.md | yes | verified in browser | CONSISTENT |
| What-If never touches live | `implementationstate.md §11` | clones | verified byte-identical | CONSISTENT |
| Approved action measured vs do-nothing | RUNNING.md | matched fork | CF = independent run exactly | CONSISTENT |
| Geo providers with fallback | RUNNING.md | implemented; test uses monkeypatch | synthetic only exercised | PARTIALLY CONSISTENT (remote paths UNVERIFIED live) |
| `file` data provider "normalises real data" | RUNNING.md | yes, but `verify()` blocks non-demo graphs | 4-node graph rejected | MISLEADING for new venues |
| New endpoints, enums, WS events | not in `00`/`01` | implemented | verified | IMPLEMENTED BUT UNDOCUMENTED (in frozen contracts; partly in `contracts.md`) |
| `ws/manager.py` "seq is monotonic across the whole server" | code comment | per process | restarts at 1 | MISLEADING (root of the reconnect bug) |

---

## 24. Feature Classification Matrix

| Feature | Exists | Dynamic | Real Data | Tested | Status |
|---|---|---|---|---|---|
| Flow-coupled city simulation | YES | YES | NO | YES | WORKING |
| Event CRUD + reconciliation | YES | YES | NO | YES | WORKING |
| Hotel catalogue / bookings / recommend | YES | YES | NO | YES | WORKING (fixture catalogue) |
| Live disruptions | YES | YES | NO | YES | WORKING |
| Digital twin (EnKF) | YES | YES | NO | YES | PARTIAL (cross-process non-determinism) |
| Forecast (twin-model) | YES | YES | NO | PARTIAL | WORKING |
| Risk scoring | YES | YES | NO | YES | PARTIAL (venue ≥1.0 → HIGH) |
| Cascade (deterministic flow) | YES | YES | NO | YES | WORKING |
| GNN cascade confidence | YES | PARTIAL | NO | PARTIAL | PARTIAL (annotative only) |
| Intervention proposal + simulated evaluation | YES | YES | NO | YES | WORKING |
| Equilibrium certificate | YES | PARTIAL | NO | PARTIAL | BROKEN (pre-Phase-1 semantics) |
| Approval → real world change + CF | YES | YES | NO | YES | WORKING |
| Regret / settlement | YES | YES | NO | YES | PARTIAL (±100 clamp; root ≠ target) |
| What-If | YES | YES | NO | YES | PARTIAL (magnitude validation) |
| Attendee journeys / nudges | YES | YES | NO | YES | WORKING |
| Commander | YES | YES | NO | YES | PARTIAL (stale cache after seek) |
| Metrics panel | YES | YES | NO | PARTIAL | WORKING |
| Geo provider (synthetic) | YES | YES | NO | YES | WORKING |
| Geo provider (OSRM / Google) | YES | UNVERIFIED | UNVERIFIED | PARTIAL (mocked) | UNVERIFIED |
| Data provider `file` | YES | PARTIAL | PARTIAL | PARTIAL | PARTIAL (demo-graph guard) |
| WS live updates | YES | YES | NO | PARTIAL | WORKING |
| WS recovery after restart | YES | NO | NO | NO | BROKEN |
| Speed validation / interruptible clock | PARTIAL | PARTIAL | NO | NO | BROKEN |
| Observer step / next decision / auto-pause / status | NO | NO | NO | NO | NOT IMPLEMENTED |
| `run_demo.py` | NO | NO | NO | NO | NOT IMPLEMENTED |
| Reset / seek resync | YES | YES | NO | YES | WORKING (seek: partial) |
| Mock / replay mode | YES | NO | NO | YES | MOCK |
| Venue search / geocoding | NO | NO | NO | NO | NOT IMPLEMENTED |
| Radius | NO | NO | NO | NO | NOT IMPLEMENTED |
| Geographic footprint | NO | NO | NO | NO | NOT IMPLEMENTED |
| Blueprint generation | NO | NO | NO | NO | NOT IMPLEMENTED |
| Runtime event-graph regeneration | NO | NO | NO | NO | NOT IMPLEMENTED |
| Custom topology via file | PARTIAL | PARTIAL | PARTIAL | PARTIAL | HARDCODED (demo IDs required) |

---

## 25. Known Limitations (evidence-backed)

1. All data is synthetic. Coordinates are offsets from one Mumbai point. There is no basemap and no external POI data (§13, §14).
2. The graph is fixed at startup, and any replacement must contain the demo IDs and chain (`verify`, catalogue, generator prefixes) (§13).
3. WS clients freeze after a backend restart until a manual reload (§15).
4. Speed changes do not take effect until the current wall sleep ends (up to 60 s at 0.5×) (§15).
5. Equilibrium certificates depend on relief size and on unrelated zones; the breach time is a formula (§16).
6. Venues can be published above 100 % (sensor noise) and are banded HIGH up to 1.02 (§16, §23).
7. Twin, forecast and published state are not reproducible across processes without a fixed `PYTHONHASHSEED` (§18).
8. What-If accepts out-of-range magnitudes and unknown rain intensity (§11).
9. The settlement clamp can produce ±100 % relief entries; relief is measured at the triggering root even when it is not a target (§16).
10. Candidate evaluation, What-If and projection run on clones of the **ground-truth** world, not the twin's estimate. Predictions therefore have perfect knowledge of unannounced disruptions (`evaluation.py`, `simulation.py`, `projection.py`).
11. `zone_incentive` has no effect when the source zone has no modelled inflow; `emergency_corridor` only acts once the emergency coupling triggers (§16 physics table).
12. Seek resets the generator but not the twin, Commander cache or executing interventions (`engine.py demo_control`).
13. There is no authentication on mutating endpoints (§21).
14. `create_all` without migrations cannot evolve an existing DB schema (§10).

---

## 26. Unknowns

- **CI:** not observed running on GitHub. The workflow uses Python 3.12 with torch 2.10; installability there is unverified.
- **OSRM and Google** geo providers against real services: only the monkeypatched test ran.
- **Ollama / `local_llm`** Commander mode: not exercised.
- **Postgres / Redis** paths: not exercised.
- **The claimed 100–150 ms reconciliation latency:** not timed.
- **GNN quality on any other topology:** no evidence exists.
- **`Backend/.env` contents:** not inspected (see §21).
- **Performance on graphs larger than 67 nodes:** not measured.
- **Browser behaviour for** event editing, What-If apply-to-live, attendee nudges and the drift toggle: backend effects were verified via API and in-process, not by clicking in the UI.
- **Whether Paresh's authors intended** the venue 1.02 critical line to override the "≥1.0 ⇒ critical" rule. It is documented in `config.yaml` ("a sold-out venue … is the plan"), so it is deliberate on Paresh but conflicts with Mehul's Phase 1E.

---

## 27. What Is Safe to Build On

- **The flow-model `SyntheticGenerator` and the `CityModel` protocol** (`city_model.py`). They are causal, conserving, deterministic in value, cloneable, and heavily tested (126 tests). Every service reaches the model only through the protocol.
- **Clone-based What-If, evaluation and projection** (`run_forward`). Isolation was verified byte-for-byte.
- **Matched counterfactual forks at approval.** Exact equality with an independent run was verified.
- **The event schedule service and reconcile path.** They are dynamic and well tested (`test_events.py`, 12 tests).
- **`providers/data.py` normalisers and the `FileData` seam.** This is the natural insertion point for a generated blueprint. Relocated coordinates and capacities demonstrably flow through the whole engine.
- **The frontend store, page structure and replay mode.** All data is backend-owned; replay mode is honest.

---

## 28. What Must Not Be Touched Yet

- **`topology.verify()`, `catalog.build_properties`, and generator/attendee ID assumptions.** These must be understood and redesigned together before any generated graph can load. Changing one alone yields a startup failure or silent misbehaviour.
- **`ml_reference/equilibrium.py`.** Decide first whether to port Mehul's Phase 1D solver or redesign it against the flow model. Mehul's version expects `action_effects` and a `rollout` callable; Paresh's interventions carry `evaluation`/`_action` instead.
- **The Intervention, Certificate and TwinFidelity schemas.** The two branches diverged. Any change should wait for an explicit merge decision.
- **The GNN (`ML/`).** It is trained on this one topology, and a new graph needs a retraining and evaluation plan.
- **`lib/ws.js` and `Engine._loop`.** The Phase 0 fixes exist on Mehul and should be ported deliberately with their tests, not rewritten ad hoc.

---

## 29. Recommended Next Step

**Reconcile the baseline before building the Venue → Blueprint pipeline.** Port Mehul's Phase 0 safety layer onto Paresh, together with Mehul's own regression tests, and fix the twin's cross-process determinism.

**Scope:**
- the `ws.js` new-connection sequence reset (`test-ws-reconnect.mjs`, `test-store-timeline.mjs`);
- `DemoControlRequest` validation plus the interruptible loop;
- run-aware Commander cache invalidation, including seek;
- `run_demo.py`;
- step / next_decision / auto-pause / `GET /demo/status`, with `test_phase0.py` adapted;
- a deterministic observation order in `generator._counts`, or twin RNG draws keyed by entity rather than iteration order.

**Why this first:**
1. Four Phase 0 regressions were reproduced directly: reconnect freeze, 58 s clock lag, silent speed clamp, and stale Commander cache after seek.
2. Mehul already has working code and tests for them, so porting is low-risk and verifiable.
3. The planned feature will generate new graphs at runtime. That will exercise exactly these paths (resets, restarts, re-seeding, reproducibility), and it cannot be validated while runs are not reproducible across processes and clients silently freeze.

The Phase 1 equilibrium and contract divergence should be the step immediately after, as a deliberate merge decision.

**This recommendation has not been implemented.**
