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
python run.py                       # http://localhost:8000, OpenAPI docs at /docs
python -m pytest tests/ -q          # 192 tests (temp DB via tests/conftest.py; no network)
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

## Venue → radius → footprint → blueprint → event graph (`app/geospatial/`)

The engine simulates a **world**: the synthetic demo city (`topology.py`, the default and the
offline fallback) or a **generated blueprint** built from OpenStreetMap around an organiser's
venue. Pipeline: `venues.py` (VenueResolver: coordinates → optional Google Places, discovery
only → Nominatim, explicit search, 1 req/s) → `footprint.py` (radius validated, never clamped;
monitoring circle + bounds) → `overpass.py` (one focused Overpass query per build; endpoints are
trusted config with failover; `SnapshotProvider` replays a recorded payload, labelled
`osm_snapshot`) → `blueprint.py` (BlueprintBuilder: junction graph, access points only from
mapped entrances / road approaches, snapped POIs, access→gate routes along road paths,
provenance + capacity_source/confidence on everything) → `validation.py` (generic invariants —
no demo ids, no size minimum) → `service.py` (build jobs, DB persistence of the normalised
blueprint, `world_from_blueprint`).

- **Activation is a world swap, not a display change:** `Engine.activate_world()` (under the cycle
  guard; atomic — on failure the old world keeps running) builds a new `StateStore`, events,
  registry, generator/nominal worlds and twin from the blueprint, clears caches/what-ifs/Commander
  cache, starts a new `run_id` at sim start, and the route broadcasts a full `resync` carrying
  `world`. The frontend store clears the old graph on a new `world_id` and `App.jsx` refetches.
- **Behaviour comes from `entity_type` + `subtype`, never from ids.** Generated ids are opaque
  hashes (`n_…`, `e_…`); the demo's former id assumptions live in `topology.py` data
  (`subtype`, `defaults.popup_venue`). `tests/test_geo_world.py` greps for id-prefix logic.
- **GNN honesty:** `world.gnn_supported` is true only for the synthetic demo topology; generated
  worlds use the deterministic flow cascade and `/health` says so.
- Last activated blueprint is restored on startup (`geospatial.restore_active_world`).
- Tests never hit the network: `tests/geo_fixtures.py` builds Overpass-format grid cities.

## Frontend architecture

Vite + React 18 + React Router + Zustand + Tailwind; deck.gl for the map (OSM raster basemap via
`lib/basemap.js` for generated worlds, attribution always visible), Recharts for KPI charts.
Routes (`App.jsx`): `/` Command Centre, `/venue` Venue & Network setup, operator pages
(`/events`, `/accommodation`, `/transport`, `/crowd`, `/interventions`, `/whatif`, `/twin`,
`/commander`), `/attendee` PWA, `/metrics` judging panel.

- **`App.jsx` bootstraps once:** fetch static topology (`/event`, `/graph`), then either
  `startMockDriver(store)` or seed from REST + `connectWebSocket(store)`. It refetches the topology
  only when a `resync` names a different `world_id` (blueprint activation / back to the demo). **No component fetches
  its own data** — everything flows through the store.
- **`lib/api.js`** is the only file that speaks HTTP. `MOCK_MODE` (anything but `VITE_MOCK=0`)
  routes every call through `lib/mocks.js` instead. Flipping `VITE_MOCK` is the entire
  mock↔live switch; both paths share the identical store/WS code.
- **`lib/ws.js`** (`02 §6`): drop any message with `seq <= lastSeq` (per connection — `lastSeq`
  resets on every open because the server's `seq` restarts with the process); exponential backoff
  (1/2/4/8s) with an amber TopBar chip and last-known data left on screen (**never blank**); on
  reconnect send `{action:"resync", last_seq}` and apply the reply as a full replace.
- **`store/useStore.js`** (`02 §7`): `state_update` is a **delta** — `mergeEntities` merges by
  `entity_id`, an entity absent from a delta keeps its state. `replaceAll` (the `resync`
  handler) is the *only* full replace. `interventions` is re-ordered only by the backend's
  `rank_score`, never by a local heuristic. `loadVarianceDelta` is the one display-only
  computation the contract permits.

## ML workstream

`ML/` holds trained models (HX-Cascade GNN `cascade.py` + checkpoint, optionally a LightGBM
forecaster). To swap one in: add `ML/<module>.py` exporting the contract class; the registry
picks it up with zero backend changes and `/health` starts reporting the new `active_source`.
`ML/README.md` has the per-module constructor signatures and the `03 §0` rules (no I/O after
`__init__`, no imports from `Backend/`, every module exposes `.fallback()` with the same return
shape, deterministic under `config["seed"]`, no exceptions escape). `Backend/app/ml_reference/*.py`
is a complete working reference for every module — copy its return shapes exactly (key names,
units, casing) or Pydantic rejects the output in `app/schemas.py` before it hits the wire.

Per `RUNNING.md`, `RiskScorer`, `AnomalyDetector`, `EquilibriumSolver.certify()`,
`InterventionOptimiser`, and `SyntheticGenerator` are contractually *arithmetic, not ML* and are
"real" in `ml_reference/`. `Forecaster` there is the twin-model forecast (the nominal world's projection plus a decaying
live-gap correction; trend reference without it); `CascadePredictor` is the real R-GCN
(`ML/hx_cascade_v2.pt`, retrained on flow-simulator scenarios with forecast features by
`Backend/scripts/train_cascade.py`; held-out results in `ML/cascade_eval_v2.json`) with the
deterministic propagator wired as its `.fallback()`.

Pluggable sources live in `Backend/app/providers/`: `geo.py` (travel times; synthetic by default,
OSRM/Google via env vars, cached with timeout/retry/fallback), `data.py` (topology/hotels/events;
synthetic by default, `data.provider: file` reads and normalises JSON), `weather.py` and `social.py`
(below).

## Weather-driven digital twin

Weather is an **input to the existing twin**, not a second twin. There is one world, one cycle, one
what-if engine; weather enters as a modifier on the city model and the ordinary cycle does the rest.

```
providers/weather.py   Open-Meteo (no API key) → WeatherConditions
services/weather_impact.py   rule-based coefficients → an impact vector (pure, no state, no I/O)
services/weather.py          WeatherService: refresh loop, live-drive switch, scenario translation
generator.py                 inject("weather", {"impact": …}) → one modifier kind
   ↓ the normal flow model propagates it
travel time · dwell · service rate · type capacity · attendance · arrival timing & bunching ·
incident-load gain → queues → gate spill → roads → `evacuates_to` → cascades → risk
```

- **Provenance is a field, not a claim.** Every reading carries `availability`:
  `live` | `cached` | `synthetic` | `unavailable`. A fallback is never relabelled as live, and an
  unavailable reading applies *nothing* (the identity vector) rather than guessing a calm day.
- **Advisory by default** (`weather.drive_live: false`). Weather is always fetched, always shown and
  always usable in What-If, but it only modifies the live city when an operator calls
  `POST /weather/apply` — so `sim_time` stays fully determined by the seed and the demo run does not
  depend on today's actual weather. `driving_live` / `applied_to_live` are on every payload.
- When it *is* driving the live city the same vector goes to `engine.nominal` too: a forecast is
  announced information, so the twin's process model knows it (hence `weather` in the nominal
  clone's `sources`). A do-nothing counterfactual keeps the conditions it was forked under.
- **Rule-based, and it says so.** Every coefficient in `config.yaml`'s `weather:` block has a
  `k_min`/`k_max` range and a `basis` string; `model_kind` is `rule_based`. Nothing here is fitted or
  learned. Impact scalars are keyed by **`entity_type`**, never by entity id, so a generated OSM
  world behaves exactly like the demo city. Flooding closes only entities the operator named.
- **Uncertainty comes from runs, not from a guess.** `bands` on each scalar span the configured
  coefficient range, widened by forecast horizon. A weather what-if runs the scenario three times —
  at the model's `mild`, central and `severe` coefficients — and reports the outcome span. A variant
  must mix by coefficient extreme (`mild`/`severe` on each band), not by band edge: for a shrinking
  multiplier the band's lower edge is the *worse* case, and selecting by edge yields an outcome
  outside its own band.
- What-If: `scenario_type: "weather_scenario"` with continuous params (`rain_mm_per_hr`, `temp_c`,
  `wind_kph`, `flood_severity`, `storm_duration_min`, `flooded_entity_ids`), validated against
  physical ranges — an out-of-range value is rejected, never silently clamped. It runs through the
  **existing** clone path, so the "never mutates the live city" guarantee is the same one
  `test_hardening.py` already pinned. The coarse `weather_rain` scenario still works unchanged.
- `providers/social.py` reads **public Mastodon hashtag timelines** (keyless) for topics derived
  from the active world's venue. A post's text/author/url are the platform's
  (`observation_kind: "user_report"`); `signal_type` / `scope` / `relevance` are our keyword
  classification (`classification_kind: "derived"`, `classifier: keyword_match_v1`). `scope` is
  `local` only when the post named this world's venue — a `#rain` post from elsewhere is kept and
  labelled `global`. An empty result is a real answer. The offline provider reads
  `app/data/social_fixture.json` and every signal it returns is `is_real_post: false` — it is **not**
  social media content and is never shown as anyone's words. **Nothing in the simulation reads post
  text**; the twin is driven by the weather provider alone.
- Frontend: `/twin` (`routes/DigitalTwin.jsx`) renders the same store the Command Centre uses and
  computes no impact number of its own. `MapCanvas` gains a conditions disc and affected-entity
  rings, both keyed off the backend's `severity` string and `type_capacity_mult`.
