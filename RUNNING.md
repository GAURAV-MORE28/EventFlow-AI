# Running EventFlow AI

EventFlow AI orchestrates a city hosting several concurrent events (a 70,000-seat
cup final, a fan festival and a conference expo): hotels, transport, roads,
gates, venues and visitors in one live model, with forecasting, cascade
analysis, simulated interventions, what-if analysis and attendee guidance.

## Full product (real backend + live simulation)

```bash
# Terminal 1
cd Backend
pip install -r requirements.txt
pip install -r requirements-ml.txt   # optional: the trained models (torch, PyG, LightGBM; CPU)
python run.py                    # http://localhost:8000, API docs at /docs

# Terminal 2
cd Frontend
npm install
npm run dev:live                 # VITE_MOCK=0 via .env.live; proxies /api and /ws to :8000
```

Open the printed URL (default http://localhost:5173). Pages:

| Route | What it is for |
|---|---|
| `/` | Command Centre: full-size live map (entity detail, cascades, live effect of executing actions) |
| `/events` | Event schedule: add, edit (exact date/time, windows), delay, cancel, delete; the live city re-plans at once |
| `/accommodation` | Live hotel availability, saturation with explained alternatives, stay finder |
| `/transport` | Stations, lines, roads, parking; report outages and capacity cuts |
| `/crowd` | Venues, gates, crowd zones, emergency posts; close gates |
| `/interventions` | Live queue plus full decision history and realised relief |
| `/whatif` | Scenario lab: compose, simulate, compare, apply to the live city |
| `/attendee` | Visitor journey: routes, off-peak departure, return trip, hotel options, nudges |
| `/commander` | Plain-language questions answered from live data (grounded) |
| `/metrics` | Measured outcomes: operations, prediction, twin, decisions |

The simulation runs at 10x by default (one 30-second cycle every 3 wall
seconds; 14:00 to ~20:00 sim time takes about 35 minutes). Pause, speed and
reset are in the navigation bar, or:

```bash
curl -s -X POST localhost:8000/api/v1/demo/control -H 'Content-Type: application/json' -d '{"speed_multiplier": 30}'
curl -s -X POST localhost:8000/api/v1/demo/control -H 'Content-Type: application/json' -d '{"action": "reset"}'
```

To run the frontend against a backend on another port:
`BACKEND_URL=http://localhost:8765 npm run dev:live`.

## Frontend only (recorded replay, no backend)

```bash
cd Frontend && npm install && npm run dev
```

`npm run dev` replays fixtures recorded from a real backend run
(`src/mocks/*.json`, schema-validated). Every page shows a "Recorded replay"
banner. Read-only pages work; controls that would change the city (schedule
changes, disruptions, sim controls), plan a new journey, answer a nudge or run a
new what-if are disabled, and What-If shows the one recorded scenario.

## How the product works

1. **Events drive demand.** Each event's arrivals and egress follow its
   schedule; out-of-town visitors book hotel rooms (allocated by price, travel
   time and tier; what cannot be placed is unmet demand).
2. **Visitors move through the graph** (`app/ml_reference/generator.py`):
   hotel/home → station, hub or car park → gate → venue, choosing routes by
   graph coefficients discounted by congestion. Stations and gates have
   service rates; queues build when inflow exceeds them and spill onto roads;
   crowded roads load emergency posts. Closures send demand to substitutes.
3. **The digital twin** (EnKF) assimilates sensor readings. Entities without a
   reading keep their last valid state carried forward by the process model
   (the announced plan), so estimates never drift to impossible values.
4. **Forecast, risk, cascade.** Risk severity is floored by utilisation
   (at/above the critical line is always "critical"; venues and hotels use
   their own lines). Cascades follow only edges load really moves along,
   capped to the most severe roots.
5. **Interventions.** For the most urgent entity the optimiser proposes
   concrete actions (redirect riders, spread riders over several alternative
   stations, redistribute gates, stagger entry, add shuttles, divert parking,
   move crowds, rebalance hotel guests, delay an event's start). Each is
   simulated on a copy of the live city before it is shown; ones that would not
   help are dropped. The equilibrium certificate is an independent check.
   An approved event delay is a real schedule change: `/events`, attendee
   plans and every model see the new time (except the do-nothing copy).
6. **Approval changes the city.** The action is applied to the live model at
   the current attendee compliance rate, a do-nothing copy is forked, and 15
   sim-minutes later the realised relief is measured against it (regret ledger).
7. **Attendees** plan journeys against the look-ahead projection, optionally
   by preferred mode (metro, bus/shuttle, car, walk; car and walk access
   times come from the geo provider); nudges reach
   only attendees whose route or hotel an approved action touches; accepting
   changes their plan and raises the compliance the simulator applies.
8. **What-if** runs baseline and scenario copies of the live city forward and
   compares them (attendance, event delay or cancellation, gate / station /
   road closure, capacity reduction, outages, rain, hotel shortage, pop-up
   event, combinations); any scenario can then be applied to the live city.
9. **Digital twin layers.** Each entity's detail shows the sensor reading (if
   instrumented), the twin's estimate with its ensemble uncertainty, what the
   announced plan implies, the 30-minute forecast, the do-nothing value for
   every executing action, and an over-capacity flag.

## External data and maps (optional)

Everything runs on synthetic data by default. Two provider seams exist for
real sources (`Backend/app/providers/`):

- **Geography** (`geo.py`): travel time and distance between points.
  `synthetic` (default) uses great-circle distance with a street detour
  factor. Set `EVENTFLOW_GEO_PROVIDER=osrm` with `OSRM_URL`, or
  `EVENTFLOW_GEO_PROVIDER=google` with `GOOGLE_MAPS_API_KEY`, in the
  environment (never in a committed file). Calls have a timeout, one retry and
  a cache, and fall back to synthetic on failure (the `source` field says so).
  `GET /api/v1/geo/travel?from_entity_id=..&to_entity_id=..&mode=walk|drive`.
- **City data** (`data.py`): `data.provider: file` in `config.yaml` reads
  `events.json`, `hotels.json` and `topology.json` from `data.dir` (each is
  optional) and normalises them to the wire conventions (ids, UTC timestamps,
  paise, fractions), rejecting invalid records.

## Tests

```bash
cd Backend
python -m pytest tests/ -q       # 126 tests; uses a temporary database (tests/conftest.py)
cd ../Frontend
npm run validate:mocks            # fixtures vs contracts/schemas
npm run build
```

## Regenerating schemas and fixtures

Whenever backend payloads or simulation behaviour change:

```bash
cd Backend
python -m scripts.export_schemas --out ../contracts/schemas
python -m scripts.export_mocks --out ../Frontend/src/mocks --cycles 160
cd ../Frontend && npm run validate:mocks
```

## Configuration

`Backend/config.yaml` holds every threshold and knob: warning/critical
utilisation (with per-type overrides for venues and hotels), intervention
lifetime (`interventions.ttl_sec`, `min_wall_visible_sec`), effect duration,
default compliance, demand model, hotel saturation thresholds, cascade caps,
projection horizon, departure options and the event schedule.

No database or Redis setup is needed: SQLite (`Backend/eventflow.db`) and an
in-process cache are the defaults; `DATABASE_URL` / `REDIS_URL` environment
variables override them. Run tables are cleared on startup and on reset;
interventions, certificates, executions, regret entries, nudges, the event
schedule and the hotel catalogue persist.

## What is real vs. reference

| Module | State |
|---|---|
| City model (`SyntheticGenerator`) | Deterministic flow model: schedule-driven demand, hotel bookings, route choice, queues, spill, egress; seeded |
| Digital twin | Real EnKF with localised analysis and a process model |
| Forecaster | Twin-model forecast: the digital twin's plan projection, read at its age, corrected by the live gap; time-to-critical found on the dense projected path. With LightGBM installed, the gated correction bundle `ML/artifacts/forecast_correction_v2/` corrects it (`source: "local_model"`; 03 §2.3); otherwise `source: "twin_model"`. `baseline_comparison` scores the model that produced the forecast: its 900 s point vs what was observed 900 s later, against persistence |
| Cascade | Deterministic flow cascade (`services/cascade_flow.py`, the one propagator) produces every published cascade (`source: "deterministic"`). The HX-Cascade v3 GNN (`ML/cascade.py`, bundle `ML/artifacts/hx_cascade_v3/`, trained on random maps, evaluated on held-out maps against that propagator) runs in `gnn_mode: annotate`: its calibrated per-entity failure probabilities are attached to cascade steps as `confidence` (evidence: `ML/evaluation/results/`, gate: `tests/test_cascade_swap_gate.py`). Its out-of-distribution guard skips the model on a graph outside its training data and says why in `/health.modules.cascade.fallback_reason`. Without torch it is off and cascades are unchanged apart from `confidence = null`. Live precision, recall and lead time are on `/metrics` as `gnn_*`, next to the published cascade's `cascade_*`; every prediction is stored in `ml_node_prediction` |
| Risk, anomaly | Arithmetic by design (explainable) |
| Optimiser | Rule templates with executable actions; relief measured by simulation |
| Equilibrium certificate | Fixed-point best-response solver |
| Commander | Deterministic templates over tool results; every number validated |
