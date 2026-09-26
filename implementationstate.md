# Implementation state

Current, verified description of how EventFlow AI works. Companion files:
`contracts.md` (the data contracts between stages) and `changelock.md` (what
may change). The frozen wire contracts remain `00_SHARED_CONTRACT.md` …
`03_ML_CONTRACT.md`; everything below is additive to them.

## 1. Architecture

```
Backend/  FastAPI + one long-lived Engine (app/services/engine.py)
          ├─ city model: SyntheticGenerator (app/ml_reference/generator.py)
          ├─ services: events, accommodation, attendee, projection, evaluation,
          │            simulation (what-if), cascade_flow, commander, metrics
          ├─ ML registry (app/ml_registry.py) + call_ml budget/fallback wrapper
          └─ StateStore (in-memory, the published state) + SQLite persistence
Frontend/ Vite + React + Zustand; renders the store, which is fed by REST
          bootstrap + WebSocket (lib/ws.js). No component computes state.
ML/       optional models (cascade R-GCN). Owned by the ML workstream.
```

## 2. Authoritative state

| What | Owner | Where |
|---|---|---|
| The physical city (people, queues, flows, closures, events) | `engine.generator` (live world) | `SyntheticGenerator` |
| Announced plan (schedule + approved actions, not incidents) | `engine.nominal` | twin process model |
| Do-nothing copy per executing action | `engine.counterfactuals[iid]` | forked at approval |
| Published per-entity state (what REST, WS, map, Commander read) | `engine.store.entity_states` | written only by `Engine._run_cycle` / `prime_state` |
| Severity (score, band) | `RiskScorer` + `Engine._stabilise_bands` | once per cycle |
| Cascades | `services/cascade_flow.py` | once per cycle, and on demand for one entity |
| Simulation clock | `store.sim_time` (advanced by `run_cycle`, reset by `_reset`/seek) | one clock |

Every mutation of a world happens under `engine.world_lock`. A cycle and a
reset/seek are serialised by the engine's cycle lock, so a reset never lands
mid-cycle.

## 3. The cycle (every `cycle_sec` = 30 s of sim time)

1. Tick all worlds (nominal, live, counterfactuals) by 30 s; read sensor
   observations, ground truth and per-entity flows from the live world;
   measure each executing action (live vs its do-nothing copy).
2. Twin: EnKF step (process model = nominal world) + assimilate observations.
3. Merge: observed entities report their sensor reading; the rest the twin's
   estimate. Flows/queues come from the simulation.
4. Forecast (twin-model forecast; ML forecaster drop-in optional).
5. Risk score, anomaly detection.
6. Cascades: deterministic flow cascade (+ ML confidence if a model answered).
7. Risk re-score with cascade exposure, then band hysteresis.
8. Interventions: propose (optimiser) → simulate each on a clone → certify →
   rank; expire; settle executing ones against their counterfactual.
9. Persist (entity states each cycle, forecasts every 10th), cache, broadcast.

## 4. Event model → demand

Events come from the schedule (`EventSchedule`, config or file provider) and
from operators: create (`POST /events`), edit (`POST /events/{id}`), cancel
(`status: cancelled`, stays in the record, restorable) and delete
(`DELETE /events/{id}`, leaves the active schedule). Per event:
`expected_attendance`, exact `start_time` / `end_time` (UTC, the simulation
clock; a value without a zone is read as UTC), venue (must be reachable),
category, description, status, and optional exact arrival / departure windows.
Arrivals follow a normal curve: centre and spread from the arrival window
(~95% inside it), or by default around `start - arrival_lead_min` with
`demand.arrival_sd_min`; departures likewise from the departure window or
`end + egress_lag_min`. A started event keeps its start; its end, visitors and
status can change. Its venue is fixed once visitors are on their way.
Attendance semantics: `expected_attendance` is the total the event will bring;
lowering it (even to 0) stops new arrivals immediately, visitors already here
stay and leave through the normal departure logic. Cancel and delete both stop
future demand and send the event's visitors home; a deleted event is kept in
the simulator (never in the schedule) only so its visitors can leave. Changing attendance or the
schedule re-plans demand for every world that knows about it (live, nominal,
counterfactuals; What-If clones copy the live world). A change mid-arrival
keeps everyone already arrived and re-schedules only the remaining people.
Out-of-town visitors book hotel rooms (allocation by price, travel time, tier).

## 4a. Immediate reconciliation (operator change → live state)

Every mutation (event create/edit/cancel/delete, disruption start/clear,
intervention approval) ends with `Engine.reconcile()`, under the cycle lock:
1. every world re-runs the step in progress from its starting state under the
   new schedule/modifiers (`generator.resimulate_last_step`; anyone who already
   arrived in that step still arrives — nobody is removed);
2. the twin's ensemble moves by the plan model's change (`twin.shift`);
3. the published state is re-stated for the same instant (history point
   replaced, not added); forecasts, risk, cascades are recomputed; severity
   hysteresis is suspended for `post_change_no_hold_cycles` so the
   consequence is visible as it unfolds;
4. `tick`, `state_update`, `forecast_update`, `cascade_update` (full set),
   `intervention_effect`, `state_reconciled` are broadcast before the HTTP
   response returns. The clock does not move; the next cycle continues from
   this state. Measured on a live server: 100–150 ms from request to WS.
What changes at once: the event's future demand, forecasts, risk, cascades,
anything the re-run step changes. What changes over the next cycles: physical
occupancy (people already at stations, in queues or inside drain at the rates
the simulation allows).

## 5. People flow

Arriving people per minute are split over access nodes by mode (metro
stations / lines, bus and shuttle hubs, car parks, walking), then choose gates
by route coefficient discounted by congestion, then enter the venue.

* Stations/hubs: fluid queue on service rate μ; `queue_{t+1} = max(0, queue_t + (λ − μ)·dt)`.
* Gates: scan queue; service limited by scan rate **and by the venue's remaining
  capacity**; queue beyond `GATE_HOLD_FACTOR × capacity` spills onto adjacent roads.
* Venues: occupancy = people inside; hard-capped at capacity. People who
  cannot get in wait outside (gate queues or the venue's outside queue) and are
  never counted as occupancy. Entry closes once an event starts emptying;
  anyone still outside then goes home (counted as departed).
* Roads/zones: pass-through occupancy = flow × dwell (Little's law), capped at
  a physical density maximum; closed roads pass their traffic to neighbours.
* Parking: vehicles accumulate on arrival, drain on egress.
* Emergency posts: incident load rises with crowding upstream.
* Per-event ledger: `arrived = inside + egressed + pending` at all times
  (`generator.event_ledger()`); nothing is created or deleted.

## 6. Graph topology

The active **world** supplies the topology: the synthetic demo (`app/topology.py`,
or `providers/data.py` file mode) or a **generated blueprint** (`app/geospatial/`,
built from OpenStreetMap around a venue + radius and activated at runtime via
`Engine.activate_world`). Entities have lat/lon, type, `subtype`, nominal capacity
(+ `capacity_source`/`capacity_confidence`, provenance); edges have type (`feeds`,
`adjacent_to`, `serves`, `last_mile_to`, `evacuates_to`, `substitutes_for`,
`connects_to`), transfer coefficient and travel time (generated: distance, road
geometry, `via_entity_ids` road path for access→gate routes). The flow model reads
behaviour from type/subtype only; visitors on an access→gate route load the road
junctions on its path. Validation is generic (`geospatial/validation.py`); the
demo's scripted structures are checked only for the demo world (`verify_demo`).

## 7. Node state (published, per entity)

`current_count`, `utilisation = count / capacity`, `flow_rate_per_min`,
`inflow_per_min`, `outflow_per_min`, `queue_people` (null when not modelled),
`risk_score`, `risk_band`, `is_observed`. Crowd & Venues (`/overview`) adds
capacity, available capacity, forecast, time to critical, queue delay and the
events at a venue — from the same `entity_states`.

## 8. Risk / severity

`RiskScorer` (arithmetic, not ML): a floor from utilisation against the
entity type's warning/critical lines (`config.yaml thresholds`, per-type for
venues and hotels), plus bounded escalations for forecast growth, cascade
exposure and persistence. Band from score (`risk_bands`). Hysteresis
(`thresholds.band_hold_cycles`, default 3): escalation is immediate, a lower
band is shown only after it held for 3 consecutive cycles (score held at the
band floor meanwhile).

## 9. Cascade

See `contracts.md §6`. Deterministic, topology- and flow-based, all outbound
paths, multi-level, stops at spare capacity; ML annotates steps with
`confidence`. The same function serves the live cycle, `GET /cascade/{id}` and
What-If.

## 10. Interventions

propose → simulate (clone + `run_forward`, evaluation attached) → certify →
rank → approve (`POST /interventions/{id}/approve`) → apply to the live world
and the nominal model at current compliance, fork a do-nothing copy → measured
every cycle (`live_effect`, WS `intervention_effect`) → settled after
`settle_delay_sec` (regret ledger: predicted vs realised relief). An event delay
is applied as a real schedule change. Candidates that would not help are
dropped before an operator sees them.

## 11. What-If

`POST /simulate` clones the live world twice (baseline, scenario), applies the
scenario to the scenario clone only, runs both forward with the same
`run_forward`, and compares. The live world is never touched. Applying to live
is a separate explicit call (`POST /disruptions` or `POST /events/{id}`).

## 12. Attendee journeys

Time-dependent Dijkstra over the look-ahead projection (a clone of the live
world run forward). Each leg's time = base edge time × BPR congestion factor
(`1 + α·u^β`, config `attendee.congestion_alpha/beta`) at the load projected
for the moment the traveller reaches it, plus station queue waits and, on the
way *into* a venue, gate queue waits. Departure options (+0/15/30/45 min) and
the return trip are each planned against their own future state.

## 13. WebSocket

Per cycle: `tick`, `state_update` (delta: only changed entities, merged by
id), `forecast_update`, `cascade_alert` (new roots), `cascade_update` (the
complete active set whenever it changes), `intervention_effect` (executing
actions' live effect), plus lifecycle events. Reset/seek broadcast `resync`
(full replace). REST and WS read the same `entity_states`.

## 14. Frontend ↔ backend

The frontend renders backend values only. The Command Centre is map-first
(TopBar + NavBar + full-size live map with the entity panel and the action
strip). Commander, What-If, intervention queue, pressure timeline and the twin
gauge are on their own pages (/commander, /whatif, /interventions,
/metrics).

## 15. ML boundary

See `contracts.md §9`. With ML disabled or failing, every stage still
produces its output from deterministic logic.

## 16. Known limitations

* All data is synthetic; the geo/data providers are seams for real sources.
* Road/zone occupancy is flow × dwell, not individual agents.
* Cascade overflow split by transfer coefficient is a documented heuristic,
  not a calibrated model; its online precision is measured on /metrics.
* The run tables persist the pre-change row for the instant of a
  reconciliation; the next cycle persists the new state.
* Two backend processes sharing one SQLite file (e.g. a dev server and
  `export_mocks`) collide on run-table rows; use separate `DATABASE_URL`s.
* Departure options differ only when the network state along the route
  differs; on an uncongested route the times are (correctly) the same.
* A What-If "peak utilisation" can be pinned at a station's physical maximum in
  both worlds; compare the other metrics in that case.
