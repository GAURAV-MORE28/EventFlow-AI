# Backend contracts — the causal chain

```
EVENT → DEMAND → PEOPLE FLOW → GRAPH STATE → CROWD/VENUE STATE → RISK
      → CASCADE → INTERVENTION → UPDATED STATE → MAP / WebSocket
```

Units follow `00_SHARED_CONTRACT.md §0`: utilisation 0.0–1.0 (may exceed 1.0
only where the entity physically holds more than its design load — roads,
zones, stations, lines; never a venue), durations `_sec`, money `_paise`,
UTC `Z` timestamps, snake_case ids, `null` = unknown / not modelled.

## 1. EVENT → DEMAND

Input: `EventSchedule` entries (`event_id`, `venue_entity_id`, `start_time`,
`end_time`, `expected_attendance`, `out_of_town_share`, `status`).
Output (per world, per step): arrival rate `A · φ((t − (start − lead)) / sd)`
differentiated over time, capped at a physical rate; egress after `end`
(20-minute evacuation if cancelled). Changes go through
`POST /events/{id}` → `Engine.update_event` → `set_events` on every world.
Guarantee: +N attendance produces +N arrivals over the remaining arrival
window (or waits outside if the venue is full).

### 1a. Event mutations

| Call | Effect |
|---|---|
| `GET /events/venues` | where an event can be held (reachable venues and zones) |
| `POST /events` `EventCreateRequest` | new event; 201 `EventView`; its arrivals are planned at once |
| `POST /events/{id}` `EventUpdateRequest` | any of: `name`, `venue_entity_id`, `start_time`, `end_time`, `delay_sec`, `expected_attendance`, `category`, `description`, arrival/departure window bounds (`""` clears), `status` (`scheduled`/`cancelled`) |
| `DELETE /events/{id}` | `EventDeleteResponse` (`arrived`, `inside` = visitors who will leave normally) |

Rules: datetimes are exact and canonical `YYYY-MM-DDTHH:MM:SSZ`; end > start;
≤ 24 h; windows must be real intervals (arrivals close by the end, departures
open after the start); a started event cannot move its start; a venue cannot
change once visitors are on their way; a cancelled event must be restored
before other edits. Violations → 400 `INVALID_SCHEDULE` / `INVALID_REQUEST`,
unknown id → 404. `EventView` (additive): `description`, effective
`arrival_window_*` / `departure_window_*`, `custom_windows`,
`remaining_demand`. Every mutation is followed by the reconciliation in §8.

## 2. DEMAND → PEOPLE FLOW

Arrivals split over access nodes by mode shares and route coefficients, then
over gates (congestion-aware), then into the venue. Interventions and
disruptions act here (diversions, closures, capacity multipliers, staggering).
Ledger invariant per event: `arrived = inside + egressed + pending`,
`pending ≥ 0`, `arrived ≤ expected_attendance`.

## 3. PEOPLE FLOW → GRAPH STATE

`generator.utilisation()` (count / effective capacity) and
`generator.flow_state()` → `{inflow_per_min, outflow_per_min, queue_people}`.
Queue balance at gates and stations: `Δqueue = (inflow − outflow) · dt`.

## 4. GRAPH STATE → CROWD / VENUE STATE (published)

`EntityState` (per entity, `store.entity_states`): `current_count`,
`utilisation`, `flow_rate_per_min`, `inflow_per_min`, `outflow_per_min`,
`queue_people`, `risk_score`, `risk_band`, `is_observed`.
Venue: `current_count ≤ nominal_capacity` always; people waiting are in
`queue_people` (gate queues + outside queue), never in `current_count`.
`/overview` rows add `available_capacity`, `event_ids`, `forecast_1800`,
`time_to_critical_sec`, `queue_delay_sec`.

## 5. STATE → RISK

`risk_score` (int 0–100) from `RiskScorer`: utilisation floor per type +
bounded escalations. `risk_band` from the score via `thresholds.risk_bands`,
then hysteresis (`band_hold_cycles`). Band and score never disagree.

## 6. RISK → CASCADE (`services/cascade_flow.py`)

Roots: entities (not hotels, not closed) whose max(now, forecast 15/30 min) ≥
their critical line; top `cascade.max_roots` by risk.
Propagation from each root:
- overflow = people above the critical line;
- split over **every** outbound people edge (`feeds`, `adjacent_to`, `serves`,
  `last_mile_to`) in proportion to `transfer_coefficient` — the shares sum to
  the overflow;
- `evacuates_to` is an impact coupling (simulator formula), carries no people;
- a downstream entity is a step only if its projected load + share reaches its
  warning line; it passes on only its own excess over its critical line;
- loads are capped at the type's physical maximum.
`CascadeResult` (additive fields): `ml_enhanced`; each `CascadeStep` adds
`source_entity_id`, `utilisation_before`, `utilisation_after`,
`flow_change_people` (null for impact edges), `reason`, `confidence` (ML
probability or null). Invariant: `via_edge_id` is a real edge from
`source_entity_id` to `entity_id`. Steps ordered by `eta_sec`, root first.

## 7. CASCADE → INTERVENTION → UPDATED STATE

`Intervention` carries `_action` (executable parameters, internal),
`evaluation` (simulated before proposal) and, while executing,
`live_effect: [{entity_id, utilisation, counterfactual_utilisation, delta}]`
= live world vs its do-nothing copy at the same instant. Approval applies the
action to the live world (and the nominal plan); the effect appears in the
next cycle's `entity_states` and on the map.

## 8. STATE → MAP / WebSocket

Envelope `{event, sim_time, seq, payload}`; clients drop `seq ≤ last_seq`.
- `state_update {entities}` — changed `EntityState`s (merge by id)
- `cascade_update {cascades}` — full active set, sent when it changes (replace)
- `cascade_alert {cascade}` — a newly active root
- `intervention_effect {effects: [{intervention_id, live_effect}]}`
- `resync` (on reset/seek and reconnect) — full replace
- `state_reconciled {reason, changed_entities}` — an operator change was
  re-stated into the live state; sent after the state/forecast/cascade messages
- `event_updated {event}` / `event_deleted {event_id}` — schedule changes
REST `GET /state`, `/overview`, `/state/{id}` return the same values as WS.
After any mutation the new state is broadcast before the HTTP response
returns (no waiting for the next cycle, also while paused).

## 9. ML boundary

```
SIMULATION STATE (store.node_state_for_ml(): plain dicts)
  → ML INPUT (model builds its own features; no backend imports)
  → ML PREDICTION (e.g. cascade failure probabilities, forecasts)
  → enhancement only (cascade step confidence; forecaster drop-in)
```
Every ML call goes through `call_ml` (latency budget, thread, fallback). If a
model is missing, slow, or raises: cascades are produced unchanged with
`confidence = null` / `ml_enhanced = false`; forecasts come from the
deterministic reference; the cycle never stops. `/health` reports the active
source honestly.
