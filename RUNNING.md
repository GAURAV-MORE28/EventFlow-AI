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
python run.py                    # http://localhost:8000, API docs at /docs

# Terminal 2
cd Frontend
npm install
npm run dev:live                 # VITE_MOCK=0 via .env.live; proxies /api and /ws to :8000
```

Open the printed URL (default http://localhost:5173). Pages:

| Route | What it is for |
|---|---|
| `/` | Command Centre: live map, pressure timeline, intervention queue, commander, quick what-if |
| `/events` | Event schedule; delay / bring forward / resize / cancel events (re-plans demand) |
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
(`src/mocks/*.json`, schema-validated). Read-only pages work; actions that
change the city (schedule changes, disruptions, sim controls) say they need
the live backend.

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
   concrete actions (redirect riders, redistribute gates, stagger entry, add
   shuttles, divert parking, move crowds, rebalance hotel guests). Each is
   simulated on a copy of the live city before it is shown; ones that would not
   help are dropped. The equilibrium certificate is an independent check.
6. **Approval changes the city.** The action is applied to the live model at
   the current attendee compliance rate, a do-nothing copy is forked, and 15
   sim-minutes later the realised relief is measured against it (regret ledger).
7. **Attendees** plan journeys against the look-ahead projection; nudges reach
   only attendees whose route or hotel an approved action touches; accepting
   changes their plan and raises the compliance the simulator applies.
8. **What-if** runs baseline and scenario copies of the live city forward and
   compares them; any scenario can then be applied to the live city.

## Tests

```bash
cd Backend
python -m pytest tests/ -q       # 71 tests; uses a temporary database (tests/conftest.py)
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
| Forecaster | Quadratic-trend reference (Chronos is used if installed) |
| Cascade | HX-Cascade GNN (`ML/cascade.py`) with physical-support gating; deterministic propagator as fallback |
| Risk, anomaly | Arithmetic by design (explainable) |
| Optimiser | Rule templates with executable actions; relief measured by simulation |
| Equilibrium certificate | Fixed-point best-response solver |
| Commander | Deterministic templates over tool results; every number validated |
