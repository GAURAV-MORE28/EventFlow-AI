# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

EventFlow AI is a predictive event-flow orchestration demo: a seeded crowd simulation of a
stadium event drives a 30-second orchestration cycle that forecasts congestion, predicts
cascading failures across a venue topology, certifies candidate interventions against a
game-theoretic equilibrium, and streams the result to an operator UI. Three workstreams —
`Backend/` (FastAPI), `Frontend/` (Vite + React), `ML/` (trained models) — developed in
parallel against a frozen contract set.

## The contract set — read before changing anything cross-cutting

`00_SHARED_CONTRACT.md` … `03_ML_CONTRACT.md` are the single source of truth for every type,
enum, unit, field name, endpoint, and WS message. `00` is read-only for everyone except the
backend lead. Non-negotiable conventions from `00 §0`:

- **snake_case on the wire, always.** The frontend network layer (`Frontend/src/lib/`) never
  renames a field; components may destructure into camelCase locals, nothing else.
- Utilisation / percentages are floats `0.0`–`1.0`. `risk_score` is the one exception: int `0`–`100`.
- Durations are always integer seconds with a `_sec` suffix. Money is integer paise (`_paise`).
- Timestamps are ISO 8601 UTC `Z`. Event-domain time is `sim_time` (simulated clock); wall
  clock only ever appears as `server_time`.
- IDs are lowercase snake_case slugs (`"metro_b"`, `"gate_3"`) — never numeric, never UUID.
- `null` means "unknown"; never omit a documented key, never use `-1`/`""` as a sentinel.

**Nothing is computed twice.** Every derived value has exactly one owner (`00`, "three rules"
table): `risk_band`, `verdict`, `time_to_critical_sec`, `rank_score`, sort order, etc. are
computed once (mostly in ML / the engine) and only *rendered* everywhere else. If the frontend
ever writes `if (score > 80) return 'critical'`, or re-sorts a list the backend ordered, the
contract is broken.

**Dependency arrow points one way:** `ML/` ← `Backend/` ← `Frontend/`. `ML/` imports nothing
backend-shaped (plain dicts / NumPy in and out); `Backend/` imports nothing from `Frontend/`.

After H20 of the original build, contract changes are **additive only** (new fields with
defaults; no renames, removals, or type changes).

## Commands

### Frontend (fastest way to see the whole app — no backend needed)
```bash
cd Frontend
npm install
npm run dev          # VITE_MOCK=1 by default; runs entirely off src/mocks/*.json
npm run build
npm run validate:mocks   # AJV-validates every mock against contracts/schemas/*.json
```

### Backend
```bash
cd Backend
pip install -r requirements.txt
pip install -r requirements-ml.txt  # optional: torch/PyG (cascade GNN), LightGBM (forecast correction)
python run.py                       # http://localhost:8000, OpenAPI docs at /docs
python -m pytest tests/ -q          # 197 tests, ~11 min (temp DB via tests/conftest.py)
python -m pytest tests/test_contract.py::test_name -q     # single test
```
No DB/Redis setup required — SQLite (`Backend/eventflow.db`) and an in-process cache are the
defaults. `Backend/.env.example` shows how to point at Postgres/Redis/Ollama instead; every
value there is optional.

### Full stack (real backend + real 30s cycle)
```bash
# terminal 1
cd Backend && python run.py
# terminal 2
cd Frontend && npm run dev:live   # proxies /api and /ws to :8000 (BACKEND_URL overrides)
```
`Frontend/.env` is gitignored; a fresh clone has none, and `lib/api.js` treats anything but an
explicit `VITE_MOCK=0` as mock mode, so the zero-setup path always works.

### Regenerating schemas + mocks (do this whenever backend topology/forecaster/cascade logic changes)
```bash
cd Backend
python -m scripts.export_schemas --out ../contracts/schemas
python -m scripts.export_mocks   --out ../Frontend/src/mocks --cycles 160
cd ../Frontend && npm run validate:mocks
```
CI (`.github/workflows/ci.yml`) runs the backend suite, exports schemas as an artifact, then
runs `validate:mocks` + `build` against them. A mock that drifts from the schema fails the build.

## Backend architecture

Everything hangs off one long-lived `Engine` (`app/services/engine.py`), created in the FastAPI
lifespan (`app/main.py`) and reachable via `get_engine()`. It owns the simulated clock, the ML
registry, and is the **only writer** to `StateStore` (`app/services/state_store.py`, in-memory).
REST handlers and WS broadcasts read from the store / cache; they never mutate simulation state.

**The cycle** (`Engine.run_cycle`, numbered to match `01_BACKEND_CONTRACT.md §2`): tick generator
→ twin assimilate (EnKF) → forecast → risk score → anomaly detect → cascade predict → if any
entity is predicted critical within 3600s: optimise + certify + queue interventions → persist →
broadcast WS events → write cycle metrics. Wall-clock pacing is `cycle_sec / speed_multiplier`.

**Three simulated worlds** (all `SyntheticGenerator`, a deterministic flow model: event schedule →
arrivals/hotel bookings → stations/hubs/parking → gates (queues, road spill) → venue → egress):
`engine.generator` is the live city; `engine.nominal` is the twin's process model (announced
schedule + approved interventions, *not* unannounced disruptions); `engine.counterfactuals[iid]`
are do-nothing forks taken at approval, used to measure realised relief. Every mutation or clone
happens under `engine.world_lock`. What-If (`services/simulation.py`), candidate evaluation
(`services/evaluation.py`) and the look-ahead projection (`services/projection.py`) all run clones
through the same `run_forward`. Other services: `events.py` (schedule), `accommodation.py`
(hotels/recommendation), `attendee.py` (time-dependent routing, departure options, nudges).

**Backpressure rule (`§2`, enforced in `_maybe_shed_load`):** when a cycle blows the total
latency cap, shed the *forecast* refresh, never assimilation — drift compounds, a stale forecast
does not.

**ML is instantiated in exactly one place:** `app/ml_registry.py`. For each of the 8 modules it
tries the real package (`ml.<module>.<Class>` / `ML.<module>.<Class>`) and silently falls back to
the deterministic reference in `app/ml_reference/`. Every ML invocation goes through
`call_ml(...)`, which runs the primary under its `§2` latency budget in a thread and routes to
that module's `.fallback()` on timeout or exception. **A fallback is a normal operating state,
not an error** — the backend never 500s because a model was slow or missing. The `source` /
`active_source` fields on payloads report honestly which produced the value.

Config: `Backend/config.yaml` (`01 §7`, authoritative runtime knobs — seed 42, budgets, per-module
params), loaded via `app/config.py`. `EVENTFLOW_CONFIG` overrides the path.

**Determinism:** `sim_time` is fully determined by the seed. Run tables (`entity_state`,
`risk_state`, `forecast`) are cleared on every startup and on `POST /demo/control {action:
reset}` so a restart never collides on `(entity_id, sim_time)`. `intervention` / `certificate` /
`regret_entry` / `nudge` persist across restarts. Never introduce non-seeded randomness
(`hash()` of an id, wall-clock jitter) into the cycle — it breaks the reproducible demo run.

**Approving an intervention is real:** `POST /interventions/{id}/approve` calls
`generator.apply_relief(...)` (an actual demand-side reduction on the sim) and forks a
`twin.branch()` "do nothing" counterfactual; 900s later the regret-ledger entry's
`realised_` vs `counterfactual_relief_pct` are genuine measurements.

## Frontend architecture

Vite + React 18 + React Router + Zustand + Tailwind; deck.gl for the map, Recharts for KPI
charts. Three routes (`App.jsx`): `/` Command Centre, `/attendee` PWA, `/metrics` judging panel.

- **`App.jsx` bootstraps once:** fetch static topology (`/event`, `/graph`), then either
  `startMockDriver(store)` or seed from REST + `connectWebSocket(store)`. **No component fetches
  its own data** — everything flows through the store.
- **`lib/api.js`** is the only file that speaks HTTP. `MOCK_MODE` (anything but `VITE_MOCK=0`)
  routes every call through `lib/mocks.js` instead. Flipping `VITE_MOCK` is the entire
  mock↔live switch; both paths share the identical store/WS code.
- **`lib/ws.js`** (`02 §6`): drop any message with `seq <= lastSeq`; exponential backoff
  (1/2/4/8s) with an amber TopBar chip and last-known data left on screen (**never blank**); on
  reconnect send `{action:"resync", last_seq}` and apply the reply as a full replace.
- **`store/useStore.js`** (`02 §7`): `state_update` is a **delta** — `mergeEntities` merges by
  `entity_id`, an entity absent from a delta keeps its state. `replaceAll` (the `resync`
  handler) is the *only* full replace. `interventions` is re-ordered only by the backend's
  `rank_score`, never by a local heuristic. `loadVarianceDelta` is the one display-only
  computation the contract permits.

## ML workstream

`ML/` holds trained models: the HX-Cascade GNN (`cascade.py` + bundle) and the forecast correction
(`forecast_correction.py` + `ML/artifacts/forecast_correction_v2/`; LightGBM quantile models of the
twin model's residual, served as `local_model` only from a bundle whose eval.json passed
`forecaster.correction_gate`; `correction_artifact: null` serves the twin model). To swap one in: add `ML/<module>.py` exporting the contract class; the registry
picks it up with zero backend changes and `/health` starts reporting the new `active_source`.
`ML/README.md` has the per-module constructor signatures and the `03 §0` rules (no I/O after
`__init__`, no imports from `Backend/`, every module exposes `.fallback()` with the same return
shape, deterministic under `config["seed"]`, no exceptions escape). `Backend/app/ml_reference/*.py`
is a complete working reference for every module — copy its return shapes exactly (key names,
units, casing) or Pydantic rejects the output in `app/schemas.py` before it hits the wire.

Per `RUNNING.md`, `RiskScorer`, `AnomalyDetector`, `EquilibriumSolver.certify()`,
`InterventionOptimiser`, and `SyntheticGenerator` are contractually *arithmetic, not ML* and are
"real" in `ml_reference/`. `Forecaster` there is the twin-model forecast (the nominal world's 60 s projection, re-based to
its age, plus a decaying live-gap correction; time-to-critical found on that dense path; trend
reference without it), optionally corrected by the gated LightGBM bundle. `CascadePredictor` in
`ML/cascade.py` is the HX-Cascade GNN (bundle `ML/artifacts/hx_cascade_v3/` — `model.pt`,
`feature_norm.json`, `eval.json`, `calibration.json`, `ood_stats.json`, bound by SHA-256 in
`manifest.json`; trained on random maps by `ML/training/train_v3.py`). A bundle that fails
verification is not loaded. It only *scores* (`node_risk()`); there is exactly one propagator,
`services/cascade_flow.py`, which builds every published cascade (`ml_reference/cascade.py`
delegates to it; `ML/cascade.py`'s `predict`/`fallback` return no steps). `cascade.gnn_mode`
decides what the GNN does: `shadow` (predictions kept in `store.cascade_ml`, never published),
`annotate` (as configured — non-root steps get `confidence`; allowed only while
`tests/test_cascade_swap_gate.py` passes) or `off`. Its out-of-distribution guard
(`cascade.ood_guard`) skips the model on a graph outside its training data (other map hash,
features outside `ood_stats.json`); `/health` reports `active_source`, `gnn_mode` and that
`fallback_reason`. torch/PyG/LightGBM are optional (`requirements-ml.txt`); without them the
registry serves the references.

Pluggable sources live in `Backend/app/providers/`: `geo.py` (travel times; synthetic by default,
OSRM/Google via env vars, cached with timeout/retry/fallback) and `data.py` (topology/hotels/events;
synthetic by default, `data.provider: file` reads and normalises JSON).
