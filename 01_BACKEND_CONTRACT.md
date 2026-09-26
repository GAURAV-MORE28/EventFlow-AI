# 01 — BACKEND CONTRACT

> **Reads:** `00_SHARED_CONTRACT.md` (mandatory prerequisite).
> **Owner:** Backend workstream.
> **Consumers:** Frontend (`02`), ML (`03`).

**Stack:** FastAPI · SQLAlchemy · Pydantic · PostgreSQL 16 + PostGIS · Redis 7
**Base URL:** `http://localhost:8000/api/v1`
**WebSocket:** `ws://localhost:8000/ws`

---

## 1. Responsibilities

The backend **owns**:
- The event graph (topology + live state) and its persistence
- The 30-second orchestration cycle
- The synthetic event generator
- All REST/WS surface area
- Calling ML modules and normalising their output into the shared schemas
- Audit logging and the regret ledger

The backend **does not own**:
- Any model internals (see `03_ML_CONTRACT.md`)
- Any layout, colour or formatting decision (see `02_FRONTEND_CONTRACT.md`)

**Hard rule:** the backend never returns a raw ML object. Every ML return value is validated through a Pydantic model matching `00_SHARED_CONTRACT.md` before it leaves the process. If validation fails, log and return the degraded fallback — never a 500.

---

## 2. The orchestration cycle

Runs every `CYCLE_SEC` (default `30`, demo-accelerated via `/demo/control`).

```
 1. generator.tick()                     → observations
 2. persist observations                 → Postgres + Redis
 3. twin.assimilate(observations)        → corrected ensemble    [ML §3]
 4. forecaster.predict(entities)         → Forecast[]            [ML §2]
 5. risk_scorer.score(state, forecast)   → risk_score per entity [ML §6]
 6. anomaly.detect(residuals)            → anomaly flags         [ML §7]
 7. cascade.predict(graph_state)         → CascadeResult[]       [ML §4]
 8. IF any predicted band == "critical" within 3600s:
        optimiser.generate(risk)         → Intervention[]        [ML §5]
        equilibrium.certify(each)        → Certificate           [ML §4/§5]
        persist + push to operator queue
 9. broadcast WS events
10. write cycle metrics
```

**Latency budget (must be enforced with timeouts):**

| Step | Budget | On timeout |
|---|---|---|
| assimilate | 200 ms | **Never skipped.** Extend budget, log warning. |
| forecast | 300 ms | Reuse previous horizon; set `source` to previous value |
| cascade | 150 ms | Fall back to deterministic |
| optimise | 200 ms | Emit fewer candidates |
| certify | 400 ms | Emit `verdict: "UNSTABLE"`, `converged: false` |
| broadcast | 50 ms | — |
| **Total** | **~1.3 s** | Hard cap 2000 ms |

Backpressure rule (from the strategy report): **skip the forecast refresh before you skip assimilation.** Drift compounds; a stale forecast does not.

---

## 3. REST Endpoints

### 3.1 System

#### `GET /health`
```json
{
  "status": "ok",
  "server_time": "2026-09-04T14:32:01Z",
  "sim_time": "2026-09-04T14:32:00Z",
  "cycle_number": 47,
  "modules": {
    "forecaster": { "ready": true,  "active_source": "tsfm" },
    "cascade":    { "ready": true,  "active_source": "deterministic",
                    "gnn_mode": "shadow", "model_version": "hx_cascade_v2@745b2205", "model_ready": true },
    "twin":       { "ready": true,  "ensemble_size": 20 },
    "equilibrium":{ "ready": true },
    "commander":  { "ready": true }
  }
}
```
Frontend polls this once on mount to decide which panels to enable.

*(1.1.0)* `cascade.active_source` is the source the published cascades carry
(always equal to `/cascade/active.source`). `gnn_mode` (`off` | `shadow` |
`annotate`), `model_version` and `model_ready` describe the cascade model
(03 §4.5); `ready` refers to the published cascade, which does not need the model.

---

### 3.2 Topology

#### `GET /event`
Static event metadata.
```json
{
  "event_id": "evt_demo",
  "name": "National Cup Final",
  "venue_entity_id": "stadium_main",
  "expected_attendance": 70000,
  "start_time": "2026-09-04T16:00:00Z",
  "end_time": "2026-09-04T18:30:00Z",
  "sim_time": "2026-09-04T14:32:00Z",
  "concurrent_events": []
}
```

#### `GET /graph`
Full static topology. **Called once on frontend mount, then cached client-side.**
```json
{
  "nodes": [ { "...Entity..." } ],
  "edges": [ { "...GraphEdge..." } ],
  "segments": [ { "...Segment..." } ],
  "bounds": { "min_lat": 19.02, "max_lat": 19.14, "min_lon": 72.81, "max_lon": 72.95 }
}
```
`bounds` lets the map fit-to-view without iterating nodes.

---

### 3.3 Live state

#### `GET /state`
```json
{
  "sim_time": "2026-09-04T14:32:00Z",
  "cycle_number": 47,
  "entities": [ { "...EntityState..." } ],
  "summary": {
    "overall_risk_score": 48,
    "overall_risk_band": "moderate",
    "critical_count": 0,
    "high_count": 2,
    "load_variance": 0.164
  }
}
```
- `load_variance` is the **PS-8 objective quantified** — variance of `utilisation` across all `zone`-type entities. Frontend displays it in the KPI strip; it is the number that must visibly drop after an intervention.

#### `GET /state/{entity_id}`
Returns a single `EntityState` plus its forecast and inbound/outbound edges — the click-through payload for the map.
```json
{
  "state": { "...EntityState..." },
  "forecast": { "...Forecast..." },
  "edges_in":  [ { "...GraphEdge..." } ],
  "edges_out": [ { "...GraphEdge..." } ],
  "risk_breakdown": [
    { "risk_type": "crowd", "score": 54 },
    { "risk_type": "transport", "score": 61 },
    { "risk_type": "cascading", "score": 72 }
  ]
}
```
404 → `ENTITY_NOT_FOUND`.

---

### 3.4 Forecast

#### `GET /forecast`
Query params: `entity_id` (optional, repeatable). Omitted → all entities.
```json
{
  "generated_at": "2026-09-04T14:32:00Z",
  "active_source": "tsfm",
  "forecasts": [ { "...Forecast..." } ]
}
```

#### `GET /forecast/pressure-timeline`
Purpose-built for the Pressure Timeline component. Only entities that will cross critical within 3600s, sorted by `time_to_critical_sec` ascending.
```json
{
  "sim_time": "2026-09-04T14:32:00Z",
  "items": [
    {
      "entity_id": "metro_b",
      "display_name": "Metro B Station",
      "current_utilisation": 0.61,
      "current_band": "moderate",
      "time_to_critical_sec": 1080,
      "trajectory": [
        { "horizon_sec": 0,    "utilisation": 0.61 },
        { "horizon_sec": 300,  "utilisation": 0.68 },
        { "horizon_sec": 600,  "utilisation": 0.75 },
        { "horizon_sec": 900,  "utilisation": 0.82 },
        { "horizon_sec": 1200, "utilisation": 0.91 },
        { "horizon_sec": 1800, "utilisation": 0.94 }
      ]
    }
  ]
}
```
`trajectory` is always 6 points at fixed offsets `[0,300,600,900,1200,1800]`. Frontend draws a fixed-width sparkline.

---

### 3.5 Cascade

#### `GET /cascade/{entity_id}`
Returns a `CascadeResult` (§2.5 shared).
- If the entity has no predicted downstream failures: `steps` contains only `step_index: 0`, `total_downstream_failures: 0`.

#### `GET /cascade/active`
All currently active cascade predictions, for the map overlay without per-node fetching.
```json
{
  "sim_time": "2026-09-04T14:32:00Z",
  "source": "deterministic",
  "cascades": [ { "...CascadeResult..." } ]
}
```

---

### 3.6 Interventions

#### `GET /interventions`
Query params: `status` (default `proposed`), `limit` (default 10).
```json
{
  "sim_time": "2026-09-04T14:32:00Z",
  "interventions": [ { "...Intervention (certificate embedded)..." } ]
}
```
Sorted by `rank_score` **descending**. UNSTABLE ones are included and appear in rank order — the frontend does the visual de-emphasis, not the backend.

#### `GET /interventions/{intervention_id}`
Single `Intervention`, certificate embedded.

#### `POST /interventions/{intervention_id}/approve`
```json
// request
{ "operator_id": "op_demo", "note": "Approved during rehearsal" }

// 200 response
{
  "intervention_id": "int_7f3c",
  "status": "executing",
  "applied_at": "2026-09-04T14:33:00Z",
  "nudges_issued": 3,
  "twin_branch_id": "sim_9a1b"
}
```
- Creates a counterfactual twin branch (`twin_branch_id`) so the regret ledger can compute "what if we'd done nothing."
- Emits WS `intervention_resolved` and `nudge_pushed`.
- 409 `INTERVENTION_ALREADY_RESOLVED` if status is not `proposed`.
- 409 `INTERVENTION_EXPIRED` if `expires_at` has passed.

#### `POST /interventions/{intervention_id}/reject`
```json
// request
{ "operator_id": "op_demo", "reason": "Certificate unstable" }
// 200 response
{ "intervention_id": "int_9a2d", "status": "rejected" }
```

---

### 3.7 Certificates

#### `GET /certificates/{intervention_id}`
Returns the `Certificate` alone. Provided for the expandable detail panel, though the object is already embedded in the intervention — **frontend should prefer the embedded copy and use this only on manual refresh.**

---

### 3.8 Simulation (what-if)

#### `POST /simulate`
```json
// request
{
  "scenarios": [
    { "scenario_type": "metro_capacity_delta", "params": { "entity_id": "line_blue", "delta_pct": -15 } },
    { "scenario_type": "weather_rain",         "params": { "intensity": "heavy" } }
  ],
  "horizon_sec": 3600,
  "label": "Blue line degraded + rain"
}

// 202 response
{ "simulation_id": "sim_4c8e", "status": "running", "eta_sec": 3 }
```

#### `GET /simulate/{simulation_id}`
```json
{
  "simulation_id": "sim_4c8e",
  "status": "complete",
  "label": "Blue line degraded + rain",
  "baseline": {
    "peak_utilisation": 0.94,
    "peak_entity_id": "metro_b",
    "load_variance": 0.164,
    "critical_count": 1
  },
  "scenario": {
    "peak_utilisation": 1.12,
    "peak_entity_id": "gate_3",
    "load_variance": 0.243,
    "critical_count": 4
  },
  "delta": {
    "peak_utilisation_pct": 19.1,
    "load_variance_pct": 48.2,
    "new_critical_entities": ["gate_3", "road_4", "emergency_north"]
  },
  "cascade": { "...CascadeResult..." },
  "candidate_interventions": [ { "...Intervention..." } ]
}
```
`status`: `"running" | "complete" | "failed"`. Poll every 1s; typically completes in <3s.

---

### 3.9 Twin

#### `GET /twin/fidelity`
Returns `TwinFidelity` (§2.8 shared).

#### `POST /twin/drift-mode`
```json
// request
{ "enabled": true }
// response
{ "drift_mode_enabled": true, "uncorrected_ensemble_started_at": "2026-09-04T14:30:00Z" }
```
**This is the demo toggle.** When enabled the backend also steps an uncorrected ensemble so `uncorrected_rmse` becomes meaningful. Turning it on mid-run resets the uncorrected ensemble to the current corrected state, so the divergence starts from zero — which is exactly what makes the visual legible.

---

### 3.10 Regret / KPIs

#### `GET /regret`
```json
{
  "entries": [ { "...RegretEntry..." } ],
  "summary": {
    "count": 4,
    "mean_absolute_regret": 3.1,
    "trend_slope": -0.42
  }
}
```
`trend_slope` negative = improving. Frontend renders the downward line.

#### `GET /metrics`
The judging/KPI panel. Every value carries its baseline.
```json
{
  "sim_time": "2026-09-04T14:32:00Z",
  "prediction": {
    "forecast_mae":            { "value": 0.061, "baseline": 0.094, "baseline_name": "persistence", "improvement_pct": 35.1 },
    "cascade_lead_time_sec":   { "value": 1044,  "baseline": 0,     "baseline_name": "threshold_rule" },
    "cascade_precision":       { "value": 0.79,  "baseline": 0.31,  "baseline_name": "random_propagation" },
    "cascade_recall":          { "value": 0.73,  "baseline": 0.30,  "baseline_name": "random_propagation" },
    "gnn_lead_time_sec":       { "value": 1320,  "baseline": 0,     "baseline_name": "threshold_rule", "sample_size": 12 },
    "gnn_precision":           { "value": 0.61,  "sample_size": 40 },
    "gnn_recall":              { "value": 0.80,  "sample_size": 15 }
  },
  "twin": {
    "rmse":              { "value": 41.2, "baseline": 118.7, "baseline_name": "uncorrected_abm", "improvement_pct": 65.3 },
    "ensemble_coverage": { "value": 0.91, "target_range": [0.85, 0.95] }
  },
  "decision": {
    "peak_utilisation_reduction_pct": { "value": 22.4, "baseline_name": "do_nothing_counterfactual" },
    "load_variance_reduction_pct":    { "value": 31.7, "baseline_name": "do_nothing_counterfactual" },
    "unstable_interventions_caught":  { "value": 3, "baseline_name": "naive_optimiser_would_approve" },
    "certificate_accuracy_pct":       { "value": 88.0 }
  },
  "system": {
    "cycle_latency_ms":          { "value": 1287, "target": 2000 },
    "commander_ungrounded_rate": { "value": 0.0,  "target": 0.0 },
    "tool_call_correctness":     { "value": 0.94, "target": 0.90 }
  }
}
```
*(1.1.0)* `cascade_*` score the published cascade and `gnn_*` the cascade model
(also in shadow mode), under one definition (03 §4.5); every one carries
`sample_size`.

**`unstable_interventions_caught` is the single most important number in the demo.** It is the entire argument for the equilibrium layer.

---

### 3.11 Commander (LLM)

#### `POST /commander/query`
```json
// request
{ "query": "Why is Metro B becoming critical?", "session_id": "sess_demo" }

// response
{
  "response": "Metro B is projected to cross critical in 18 minutes. Its inbound flow rate has risen to 180 per minute against a nominal capacity of 4200. The predicted propagation reaches gate_3 at the same time and road_4 nine minutes later.",
  "is_cached": false,
  "tool_calls": [
    { "tool": "get_state",    "args": { "entity_id": "metro_b" }, "result_digest": "utilisation=0.61 risk=54" },
    { "tool": "get_forecast", "args": { "entity_id": "metro_b" }, "result_digest": "time_to_critical_sec=1080" },
    { "tool": "get_cascade",  "args": { "entity_id": "metro_b" }, "result_digest": "3 downstream failures" }
  ],
  "grounding": {
    "numbers_emitted": ["18", "180", "4200", "9"],
    "numbers_grounded": ["18", "180", "4200", "9"],
    "ungrounded_count": 0,
    "passed": true
  }
}
```

**Grounding validator (mandatory, backend-side):**
1. Extract every numeric token from the LLM output.
2. Match each against values present in the tool-call results (allow unit conversion: `1080 sec` → `18 min`).
3. Any unmatched number → **strip it from the response**, append to `grounding.ungrounded`, set `passed: false`.
4. Log every failure. The `/metrics` field `commander_ungrounded_rate` is computed from this log and **must be 0.0 at demo time.**

**Allowed tools (exactly these eight — no others may be registered):**
```
get_state(entity_id | "all")
get_forecast(entity_id, horizon_sec)
get_cascade(entity_id)
get_interventions(status)
get_certificate(intervention_id)
run_whatif(scenario_spec)
summarize_window(start, end)
propose_action(intervention_id)     // QUEUES ONLY — never executes
```

`propose_action` returns `{ "queued": true, "intervention_id": "..." }` and nothing else. It has no execution path. This is enforced at the tool-registration layer, not by prompt instruction.

---

### 3.12 Attendee-facing

#### `POST /attendee/journey`
```json
// request
{
  "attendee_id": "att_demo_1",
  "segment_id": "price_sensitive",
  "origin_entity_id": "hotel_core_cluster",
  "destination_entity_id": "stadium_main",
  "planned_departure": "2026-09-04T15:10:00Z"
}

// response
{
  "journey_risk_score": 68,
  "journey_risk_band": "high",
  "recommended_route": {
    "legs": [
      { "from_entity_id": "hotel_core_cluster", "to_entity_id": "metro_c", "mode": "walk",    "duration_sec": 480 },
      { "from_entity_id": "metro_c",            "to_entity_id": "gate_5",  "mode": "transit", "duration_sec": 900 }
    ],
    "total_duration_sec": 1380,
    "predicted_crowding_band": "moderate"
  },
  "shortest_route": {
    "legs": [ "..." ],
    "total_duration_sec": 1080,
    "predicted_crowding_band": "critical"
  },
  "advice": "Leaving 12 minutes later avoids the Gate 3 peak."
}
```
**Both routes are always returned.** The whole point of the attendee view is showing that the *shortest* route is the *worse* route — the frontend renders them side by side.

#### `GET /attendee/nudges?attendee_id=att_demo_1`
```json
{ "nudges": [ { "...Nudge..." } ] }
```

#### `POST /attendee/nudges/{nudge_id}/respond`
```json
// request
{ "accepted": true }
// response
{ "nudge_id": "ndg_4412", "status": "accepted", "compliance_recorded": true }
```
Accepted/declined responses feed the observed compliance rate, which the equilibrium solver uses to refine elasticities within the run.

---

### 3.13 Demo control

#### `POST /demo/control`
```json
// request — any subset
{
  "action": "play",          // "play" | "pause" | "reset" | "seek" | "set_speed"
  "seed": 42,
  "speed_multiplier": 60,    // 60 = 1 wall-second per simulated minute
  "seek_to_sim_time": "2026-09-04T14:30:00Z",
  "inject": { "scenario_type": "metro_capacity_delta", "params": { "entity_id": "line_blue", "delta_pct": -15 } }
}

// response
{ "status": "playing", "sim_time": "2026-09-04T14:30:00Z", "seed": 42, "speed_multiplier": 60 }
```
**`seed: 42` is the frozen demo seed.** Do not change it after H30.

---

## 4. WebSocket protocol

**Connect:** `ws://localhost:8000/ws?client=command_centre` (or `client=attendee&attendee_id=att_demo_1`)

Every message:
```json
{ "event": "<event_name>", "sim_time": "2026-09-04T14:32:00Z", "seq": 471, "payload": { } }
```
`seq` is monotonic. Frontend drops out-of-order messages.

### 4.1 Events (command centre)

| `event` | Fires | `payload` |
|---|---|---|
| `tick` | Every cycle | `{ "cycle_number": 47, "sim_time": "...", "summary": { ...state.summary... } }` |
| `state_update` | Every cycle | `{ "entities": [ EntityState ] }` — **only changed entities** |
| `forecast_update` | Every cycle | `{ "active_source": "tsfm", "pressure_timeline": [ ...items... ] }` |
| `cascade_alert` | When a new cascade with ≥1 downstream failure appears | `{ "cascade": CascadeResult }` |
| `intervention_queued` | New proposal | `{ "intervention": Intervention }` (certificate embedded) |
| `intervention_resolved` | Approve/reject/expire | `{ "intervention_id": "...", "status": "executing" }` |
| `twin_fidelity` | Every cycle | `{ ...TwinFidelity... }` |
| `regret_update` | On new ledger entry | `{ "entry": RegretEntry, "summary": {...} }` |
| `anomaly` | On detection | `{ "entity_id": "...", "z_score": 3.4, "description": "..." }` |

### 4.2 Events (attendee client)

| `event` | `payload` |
|---|---|
| `nudge_pushed` | `{ "nudge": Nudge }` |
| `journey_risk_update` | `{ "attendee_id": "...", "journey_risk_score": 72, "journey_risk_band": "high" }` |

### 4.3 Reconnect contract
On reconnect the client sends `{ "action": "resync", "last_seq": 468 }`.
Server responds with a single `resync` event containing the **full** current state (not a delta):
```json
{ "event": "resync", "seq": 471, "payload": {
  "state": { "..." }, "pressure_timeline": [ "..." ],
  "interventions": [ "..." ], "twin_fidelity": { "..." }
}}
```

---

## 5. Database schema (authoritative DDL)

```sql
-- Static topology
CREATE TABLE entity (
  entity_id         TEXT PRIMARY KEY,
  entity_type       TEXT NOT NULL,
  display_name      TEXT NOT NULL,
  geom              GEOGRAPHY(POINT, 4326) NOT NULL,
  nominal_capacity  DOUBLE PRECISION NOT NULL,
  parent_id         TEXT REFERENCES entity(entity_id),
  meta              JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE graph_edge (
  edge_id              TEXT PRIMARY KEY,
  src_entity_id        TEXT NOT NULL REFERENCES entity(entity_id),
  dst_entity_id        TEXT NOT NULL REFERENCES entity(entity_id),
  edge_type            TEXT NOT NULL,
  transfer_coefficient DOUBLE PRECISION NOT NULL DEFAULT 0,
  travel_time_sec      INTEGER NOT NULL DEFAULT 0,
  substitutability     DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX ON graph_edge (src_entity_id);
CREATE INDEX ON graph_edge (dst_entity_id);

CREATE TABLE segment (
  segment_id                TEXT PRIMARY KEY,
  display_name              TEXT NOT NULL,
  share                     DOUBLE PRECISION NOT NULL,
  price_elasticity          DOUBLE PRECISION NOT NULL,
  time_elasticity           DOUBLE PRECISION NOT NULL,
  accessibility_constrained BOOLEAN NOT NULL DEFAULT FALSE,
  compliance_base_rate      DOUBLE PRECISION NOT NULL
);

-- Live state (time-partitioned by sim_time in production; plain table for MVP)
CREATE TABLE entity_state (
  entity_id        TEXT NOT NULL REFERENCES entity(entity_id),
  sim_time         TIMESTAMPTZ NOT NULL,
  current_count    DOUBLE PRECISION NOT NULL,
  utilisation      DOUBLE PRECISION NOT NULL,
  flow_rate_per_min DOUBLE PRECISION NOT NULL,
  risk_score       INTEGER NOT NULL,
  risk_band        TEXT NOT NULL,
  is_observed      BOOLEAN NOT NULL,
  PRIMARY KEY (entity_id, sim_time)
);
CREATE INDEX ON entity_state (sim_time DESC);

CREATE TABLE forecast (
  forecast_id          BIGSERIAL PRIMARY KEY,
  entity_id            TEXT NOT NULL REFERENCES entity(entity_id),
  generated_at         TIMESTAMPTZ NOT NULL,
  source               TEXT NOT NULL,
  horizon_sec          INTEGER NOT NULL,
  predicted_utilisation DOUBLE PRECISION NOT NULL,
  lower_90             DOUBLE PRECISION,
  upper_90             DOUBLE PRECISION,
  time_to_critical_sec INTEGER
);
CREATE INDEX ON forecast (entity_id, generated_at DESC);

CREATE TABLE risk_state (
  entity_id  TEXT NOT NULL REFERENCES entity(entity_id),
  sim_time   TIMESTAMPTZ NOT NULL,
  risk_type  TEXT NOT NULL,
  score      INTEGER NOT NULL,
  PRIMARY KEY (entity_id, sim_time, risk_type)
);

CREATE TABLE cascade_prediction (
  cascade_id      BIGSERIAL PRIMARY KEY,
  root_entity_id  TEXT NOT NULL REFERENCES entity(entity_id),
  generated_at    TIMESTAMPTZ NOT NULL,
  source          TEXT NOT NULL,
  steps           JSONB NOT NULL,
  total_downstream_failures INTEGER NOT NULL,
  model_version   TEXT                     -- (1.1.0) model whose probabilities annotate steps; null if none
);

-- (1.1.0) The cascade model's per-entity output every cycle it runs (shadow included).
CREATE TABLE ml_node_prediction (
  prediction_id   BIGSERIAL PRIMARY KEY,
  entity_id       TEXT NOT NULL REFERENCES entity(entity_id),
  sim_time        TIMESTAMPTZ NOT NULL,
  model_version   TEXT,
  gnn_mode        TEXT NOT NULL,
  p_fail_900      DOUBLE PRECISION,
  p_fail_1800     DOUBLE PRECISION,
  p_fail_3600     DOUBLE PRECISION,
  ttc_sec         INTEGER,
  calibrated      BOOLEAN NOT NULL,
  topology_match  BOOLEAN
);
CREATE INDEX ON ml_node_prediction (entity_id, sim_time);
-- Run tables (cleared on startup and reset): entity_state, risk_state, forecast,
-- cascade_prediction, ml_node_prediction. risk_state and forecast are written every
-- 10th cycle; cascade_prediction and ml_node_prediction every cycle.

CREATE TABLE intervention (
  intervention_id        TEXT PRIMARY KEY,
  intervention_type      TEXT NOT NULL,
  status                 TEXT NOT NULL,
  target_entity_ids      TEXT[] NOT NULL,
  triggered_by_entity_id TEXT REFERENCES entity(entity_id),
  title                  TEXT NOT NULL,
  description            TEXT NOT NULL,
  estimated_relief_pct   DOUBLE PRECISION NOT NULL,
  estimated_cost_paise   BIGINT NOT NULL,
  estimated_delay_sec    INTEGER NOT NULL,
  feasibility            DOUBLE PRECISION NOT NULL,
  rank_score             DOUBLE PRECISION NOT NULL,
  created_at             TIMESTAMPTZ NOT NULL,
  expires_at             TIMESTAMPTZ NOT NULL
);

CREATE TABLE certificate (
  certificate_id         TEXT PRIMARY KEY,
  intervention_id        TEXT NOT NULL REFERENCES intervention(intervention_id),
  verdict                TEXT NOT NULL,
  converged              BOOLEAN NOT NULL,
  iterations             INTEGER NOT NULL,
  post_nudge_variance    DOUBLE PRECISION NOT NULL,
  baseline_variance      DOUBLE PRECISION NOT NULL,
  max_zone_utilisation   DOUBLE PRECISION NOT NULL,
  max_zone_entity_id     TEXT,
  oscillation_risk       BOOLEAN NOT NULL,
  compliance_sensitivity DOUBLE PRECISION NOT NULL,
  compliance_sweep       JSONB NOT NULL,
  reason                 TEXT NOT NULL
);

CREATE TABLE execution (
  execution_id    TEXT PRIMARY KEY,
  intervention_id TEXT NOT NULL REFERENCES intervention(intervention_id),
  operator_id     TEXT NOT NULL,
  approved        BOOLEAN NOT NULL,
  note            TEXT,
  twin_branch_id  TEXT,
  applied_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE regret_entry (
  regret_id                 TEXT PRIMARY KEY,
  intervention_id           TEXT NOT NULL REFERENCES intervention(intervention_id),
  intervention_type         TEXT NOT NULL,
  predicted_relief_pct      DOUBLE PRECISION NOT NULL,
  realised_relief_pct       DOUBLE PRECISION NOT NULL,
  counterfactual_relief_pct DOUBLE PRECISION NOT NULL,
  regret                    DOUBLE PRECISION NOT NULL,
  sim_time                  TIMESTAMPTZ NOT NULL
);

CREATE TABLE nudge (
  nudge_id        TEXT PRIMARY KEY,
  attendee_id     TEXT NOT NULL,
  intervention_id TEXT REFERENCES intervention(intervention_id),
  segment_id      TEXT REFERENCES segment(segment_id),
  headline        TEXT NOT NULL,
  body            TEXT NOT NULL,
  tradeoff        JSONB NOT NULL,
  target_entity_id TEXT REFERENCES entity(entity_id),
  status          TEXT NOT NULL,
  issued_at       TIMESTAMPTZ NOT NULL,
  expires_at      TIMESTAMPTZ NOT NULL
);

-- Governance
CREATE TABLE audit_log (
  audit_id     BIGSERIAL PRIMARY KEY,
  server_time  TIMESTAMPTZ NOT NULL DEFAULT now(),
  sim_time     TIMESTAMPTZ,
  actor        TEXT NOT NULL,           -- 'system' | 'operator:<id>' | 'commander'
  action       TEXT NOT NULL,
  subject_id   TEXT,
  detail       JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE commander_log (
  log_id           BIGSERIAL PRIMARY KEY,
  session_id       TEXT NOT NULL,
  query            TEXT NOT NULL,
  response         TEXT NOT NULL,
  tool_calls       JSONB NOT NULL,
  numbers_emitted  TEXT[] NOT NULL,
  ungrounded_count INTEGER NOT NULL,
  passed           BOOLEAN NOT NULL,
  server_time      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Redis keys
| Key | Type | TTL | Contents |
|---|---|---|---|
| `state:current` | string (JSON) | 120s | Full `GET /state` payload |
| `state:entity:{id}` | string | 120s | Single `EntityState` |
| `cascade:active` | string | 60s | `GET /cascade/active` payload |
| `forecast:latest` | string | 60s | All forecasts |
| `twin:fidelity` | string | 60s | `TwinFidelity` |
| `ws:broadcast` | pub/sub channel | — | Fan-out to WS workers |

---

## 6. Backend ↔ ML boundary

ML modules are **imported in-process** for the MVP (no HTTP hop). Each is a class with the constructor `__init__(config: dict)` and the methods defined in `03_ML_CONTRACT.md`.

```python
# backend/ml_registry.py — the ONLY place ML is instantiated
from ml.forecaster    import Forecaster
from ml.cascade       import CascadePredictor
from ml.twin          import AssimilatedTwin
from ml.equilibrium   import EquilibriumSolver
from ml.optimiser     import InterventionOptimiser
from ml.risk          import RiskScorer
from ml.anomaly       import AnomalyDetector

REGISTRY = {
    "forecaster":  Forecaster(cfg["forecaster"]),
    "cascade":     CascadePredictor(cfg["cascade"]),
    "twin":        AssimilatedTwin(cfg["twin"]),
    "equilibrium": EquilibriumSolver(cfg["equilibrium"]),
    "optimiser":   InterventionOptimiser(cfg["optimiser"]),
    "risk":        RiskScorer(cfg["risk"]),
    "anomaly":     AnomalyDetector(cfg["anomaly"]),
}
```

**Rules:**
1. Every call is wrapped in `try/except` + `asyncio.wait_for(timeout)`.
2. On exception or timeout → call the module's `.fallback(...)` (every module must expose one) and set the corresponding `source` field.
3. ML modules **never** touch the database, Redis, or the network. They receive plain dicts/arrays and return plain dicts/arrays.
4. ML modules **never** import from `backend/`. The dependency arrow points one way only.

---

## 7. Configuration (`config.yaml`)

```yaml
cycle_sec: 30
demo_seed: 42
speed_multiplier: 60

thresholds:
  critical_utilisation: 0.90
  risk_bands: { low: 30, moderate: 60, high: 80 }

forecaster:
  horizons_sec: [900, 1800, 3600]
  tsfm_model: "amazon/chronos-bolt-small"
  tsfm_enabled: true
  warm_start_after_points: 40      # switch tsfm -> local_model
  device: "cpu"

cascade:
  use_gnn: false                   # flip to true only if GNN beats deterministic
  max_depth: 4
  propagation_threshold: 0.15
  gnn_artifact: "ML/artifacts/hx_cascade_v2"   # (1.1.0) verified model bundle
  gnn_mode: shadow                 # (1.1.0) off | shadow | annotate — see 03 §4.5

twin:
  ensemble_size: 20
  inflation_factor: 1.05
  drift_mode_enabled: false

equilibrium:
  max_iterations: 40
  convergence_tol: 0.005
  compliance_sweep: [0.4, 0.6, 0.9]
  alpha: 1.0
  beta: 2.5

optimiser:
  max_candidates: 5
  weights: { relief: 0.45, stability: 0.30, cost: 0.15, delay: 0.10 }

commander:
  model: "claude-sonnet-4-6"
  cache_scripted_queries: true
  strip_ungrounded_numbers: true
```

---

## 8. Backend definition-of-done checklist

- [ ] `GET /health` returns all module readiness flags
- [ ] `GET /graph` returns ≥60 nodes and the frozen demo cascade chain exists
- [ ] Cycle runs at 30s and stays under 2000ms
- [ ] Every ML call is timeout-wrapped and has a working fallback path
- [ ] `source` fields correctly reflect which model produced each output
- [ ] Grounding validator strips ungrounded numbers and logs them
- [ ] `propose_action` has **no** execution code path
- [ ] WS `resync` returns full state, not a delta
- [ ] `POST /demo/control` with `seed: 42` reproduces the identical run twice
- [ ] Every error path returns the standard envelope, never a bare 500
