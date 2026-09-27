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
| `/` | Command Centre: full-size live map (entity detail, cascades, live effect of executing actions) |
| `/venue` | Venue & Network: search a real venue, choose a radius, build the OSM blueprint, activate it as the simulated world |
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
   schedule. Only the event's lodging share needs a room (see
   "Accommodation"); those guests check in before the event and out after it.
   Rooms are allocated by price, travel time, tier and free rooms, and what
   cannot be placed is unmet demand. Everyone else is a local travelling from home.
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

## Accommodation: capacity and partial hotel demand

**Capacity: mapped where OSM says so, estimated otherwise, and always labelled.**

| OSM tags present | `rooms_source` | Confidence | UI |
|---|---|---|---|
| `capacity:rooms` | `osm_capacity_rooms` | high | `120 rooms · OSM rooms` |
| `rooms` | `osm_rooms` | high | `361 rooms · OSM rooms` |
| `capacity:beds` / `beds` / `capacity:persons` only | `derived_from_osm_*` (beds ÷ `guests_per_room`) | medium | `~41 rooms · Derived from OSM beds` |
| none | `derived_estimate` (per-subtype default) | low | `~80 rooms · Derived estimate` |

Tag values are parsed as plain numbers only; ranges, text or implausible values are ignored and
fall through to the next source. `bed_capacity` / `bed_source` and `effective_guest_capacity`
travel with each hotel. Baseline occupancy (non-event guests), bookings, check-ins and prices are
**simulated** (`occupancy_baseline_source: "simulated"`, `price_per_night_paise: null` → "unknown").
Nothing is presented as an actual room count unless it came from a `rooms` / `capacity:rooms` tag.

**Demand: only part of the crowd needs a hotel.**
- `lodging_guests = attendance × lodging_share`. The share comes from the event (`POST /events`,
  `POST /events/{id}`, the activation form) or `hospitality.default_lodging_share` (0.25). It must
  be between 0 and 1; anything else is rejected with 400, never clamped.
- `local_guests` is the rest; they start their journey from home.
- Rooms needed = guests / `guests_per_room` (2.2).
- Guests check in over the `check_in_lead_min` (180) before the start and check out from
  `check_out_lag_min` (120) after the end, over `check_out_window_min` (120), so rooms free up again.
- Allocation is deterministic and capacity-limited (scored by price when known, travel time to
  the venue, tier, free rooms and the hotel station's load).
- Guests who cannot be placed are `unmet`. They are counted, shown as a red banner ("N event guests
  could not be placed in K hotels within the selected network …") and travel as locals.
- `GET /accommodation/hotels` returns per-hotel `rooms_occupied` / `rooms_available` /
  `event_rooms_occupied` / `event_guests`, a summary (lodging, local, placed, unmet, pending,
  in-house, checked out) and `lodging_by_event`.
- A hotel is SATURATED at 0 free rooms and TIGHT at ≤ `tight_free_share` (10%) free.
- The stay finder never offers a saturated hotel. With nothing free it answers "No room available
  in the selected network" plus the reason.

## Venue & Network: simulate any real venue

`/venue` turns a real venue into the world EventFlow simulates (live backend + internet needed):

1. **Venue** — type a name and press Search (server-side OpenStreetMap Nominatim, explicit search
   only, max 1 request/second; Google Places is used first only if `GOOGLE_PLACES_API_KEY` is set,
   and only for discovery). Offline or rate-limited: type coordinates `lat, lon`.
2. **Radius** — 0.25–5 km (validated; invalid values are rejected, never clamped).
3. **Build blueprint** — one focused Overpass query inside the footprint (roads motorway→tertiary,
   rail/metro stations, bus stops, parking, hotels, hospitals/police/fire, the venue outline and
   its mapped entrances) → junction graph + access points + walking routes. Typical: 10–20 s
   (almost all Overpass), 2 km ≈ 100–500 entities, 5 km ≈ 700–2,100.
4. **Activate network** — the engine switches to this graph without a restart (new run at 14:00
   sim time; the event defaults to 85% of the venue capacity unless you enter attendance).

Every capacity carries its source (`organizer`, `osm_attribute`, `derived_*`, `estimated_*`,
`default_estimate`) and confidence; hotel prices are `null` (unknown) — never invented. The TopBar
chip says **LIVE OSM**, **OSM SNAPSHOT** or **SYNTHETIC DEMO**. If Overpass is unreachable the build
fails with `GEO_PROVIDER_UNAVAILABLE` and the current world keeps running; saved blueprints can be
re-activated offline, and "Use the synthetic demo world" switches back to the demo city.

```bash
curl -s 'localhost:8000/api/v1/venues/search?q=Wembley%20Stadium'
curl -s -X POST localhost:8000/api/v1/blueprints -H 'Content-Type: application/json' \
  -d '{"venue": {"lat": 51.5560695, "lon": -0.2796034, "display_name": "Wembley Stadium"}, "radius_m": 2000}'
curl -s localhost:8000/api/v1/blueprints/builds/<build_id>          # stages, then blueprint_id
curl -s -X POST localhost:8000/api/v1/blueprints/<blueprint_id>/activate -H 'Content-Type: application/json' -d '{}'
curl -s -X POST localhost:8000/api/v1/world/synthetic-demo            # back to the demo city
```

Data © OpenStreetMap contributors (ODbL). Map tiles: tile.openstreetmap.org (only the visible
tiles, attribution on the map; `VITE_TILE_URL` for another server). Overpass endpoints are set in
`config.yaml geospatial.overpass_endpoints` (trusted config, tried in order).

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
python -m pytest tests/ -q       # 233 tests; temporary database, no network (tests/conftest.py)
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
default compliance, demand model, hospitality (`default_lodging_share`,
`guests_per_room`, check-in lead, check-out lag/window, `tight_free_share`), cascade caps,
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
| Forecaster | Twin-model forecast: the digital twin's plan projection corrected by the live gap (trend reference without it) |
| Cascade | HX-Cascade R-GCN (`ML/cascade.py`, checkpoint `ML/hx_cascade_v2.pt`) retrained on 80 flow-simulator scenarios with forecast features (`python -m scripts.train_cascade`); on 20 held-out scenarios it beats a forecast-threshold baseline on average precision at every horizon (see `ML/cascade_eval_v2.json`). Physical-support gating; deterministic propagator as fallback |
| Risk, anomaly | Arithmetic by design (explainable) |
| Optimiser | Rule templates with executable actions; relief measured by simulation |
| Equilibrium certificate | Fixed-point best-response solver |
| Commander | Deterministic templates over tool results; every number validated |
