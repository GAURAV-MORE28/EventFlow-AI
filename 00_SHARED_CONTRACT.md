# 00 — SHARED CONTRACT (Single Source of Truth)

> **Every team reads this file first. If a field name appears here, it is spelled EXACTLY this way in the database, in the API, in the ML module, and in the frontend. No synonyms, no camelCase/snake_case drift.**

**Version:** `1.0.0`
**Owner:** Backend lead (only the backend lead may edit this file; changes are announced in the team channel with a version bump).

---

## 0. Global conventions

| Rule | Value |
|---|---|
| **Casing** | `snake_case` everywhere — DB, JSON, Python. Frontend converts to camelCase **only** inside React components, never in the network layer. |
| **Timestamps** | ISO 8601 UTC with `Z`. Example: `"2026-09-04T14:32:00Z"` |
| **Simulated time** | All event-domain timestamps are **simulated** clock, not wall clock. Field `sim_time`. Wall clock only in `server_time`. |
| **Durations** | Always integer **seconds**, suffix `_sec`. Never "minutes" in a payload. Frontend formats to minutes for display. |
| **Percentages / utilisation** | Float `0.0`–`1.0`. Never `0`–`100`. Exception: `risk_score` which is `0`–`100` integer. |
| **IDs** | Lowercase snake_case slug strings. `"metro_b"`, `"gate_3"`, `"hotel_north_cluster"`. Never numeric, never UUID in the API surface. |
| **Money** | Integer, minor units (paise). `50000` = ₹500.00. Field suffix `_paise`. |
| **Nulls** | Use `null` explicitly for "unknown". Never omit a documented key. Never use `-1` or `""` as a sentinel. |
| **API base** | `/api/v1` |
| **Content-Type** | `application/json` |

---

## 1. Enums (frozen — do not add values without a version bump)

### 1.1 `entity_type`
```
"venue" | "zone" | "transport_node" | "transport_route" | "road"
| "hotel" | "parking" | "gate" | "emergency_facility"
```

### 1.2 `edge_type`
```
"feeds"            // src pushes flow into dst (metro_b feeds gate_3)
| "adjacent_to"    // spatial neighbour, bidirectional overflow
| "serves"         // src provides capacity to dst (parking serves venue)
| "last_mile_to"   // walk/shuttle link (station last_mile_to venue)
| "substitutes_for"// dst is a viable alternative to src (hotel A substitutes_for hotel B)
| "evacuates_to"   // emergency egress path
```

### 1.3 `risk_band`
| Band | `risk_score` range | Hex (frontend) |
|---|---|---|
| `"low"` | 0–30 | `#22C55E` |
| `"moderate"` | 31–60 | `#EAB308` |
| `"high"` | 61–80 | `#F97316` |
| `"critical"` | 81–100 | `#EF4444` |

### 1.4 `risk_type`
```
"crowd" | "capacity" | "transport" | "traffic" | "hospitality"
| "parking" | "emergency" | "cascading" | "overall"
```

### 1.5 `certificate_verdict`
```
"STABLE"       // equilibrium holds across the tested compliance band
| "CONDITIONAL"// holds only in part of the compliance band
| "UNSTABLE"   // creates a new critical entity, or oscillates, or did not converge
```

### 1.6 `intervention_type`
```
"reroute_transport" | "deploy_shuttle" | "stagger_entry" | "gate_redistribution"
| "parking_redistribution" | "zone_incentive" | "accommodation_rebalance"
| "emergency_corridor" | "notify_only"
```

### 1.7 `intervention_status`
```
"proposed" | "approved" | "rejected" | "executing" | "completed" | "expired"
```

### 1.8 `segment_id`
```
"price_sensitive" | "time_sensitive" | "accessibility_constrained"
| "group" | "premium"
```
Exactly five. The ML equilibrium solver and the attendee PWA both key off this list.

### 1.9 `forecast_source`
```
"persistence" | "tsfm" | "local_model"
```
Every forecast payload declares which produced it. The frontend shows this as a small badge; the KPI panel groups by it.

### 1.10 `cascade_source`
```
"deterministic" | "gnn"
```

### 1.11 `scenario_type` (what-if inputs)
```
"attendance_delta" | "metro_capacity_delta" | "road_capacity_delta"
| "weather_rain" | "gate_closure" | "transport_outage" | "parking_loss"
| "hotel_shortage" | "concurrent_event" | "combined"
```

---

## 2. Core object schemas

### 2.1 `Entity` (static topology — immutable during a run)
```json
{
  "entity_id": "metro_b",
  "entity_type": "transport_node",
  "display_name": "Metro B Station",
  "lat": 19.0760,
  "lon": 72.8777,
  "nominal_capacity": 4200,
  "parent_id": null,
  "meta": { "lines": ["blue"], "platforms": 2 }
}
```
- `nominal_capacity` — units depend on `entity_type`: people for zone/gate/venue, people/hour for transport_route/road, rooms for hotel, vehicles for parking.
- `meta` is free-form. **Frontend must never depend on a `meta` key for layout.**

### 2.2 `GraphEdge`
```json
{
  "edge_id": "metro_b__gate_3__feeds",
  "src_entity_id": "metro_b",
  "dst_entity_id": "gate_3",
  "edge_type": "feeds",
  "transfer_coefficient": 0.62,
  "travel_time_sec": 420,
  "substitutability": 0.0
}
```
- `edge_id` convention: `{src}__{dst}__{edge_type}`.
- `transfer_coefficient` ∈ [0,1] — fraction of src overflow that reaches dst. Used by the deterministic cascade propagator.
- `substitutability` ∈ [0,1] — only meaningful for `substitutes_for`; `0.0` otherwise.

### 2.3 `EntityState` (live)
```json
{
  "entity_id": "metro_b",
  "sim_time": "2026-09-04T14:32:00Z",
  "current_count": 2560,
  "utilisation": 0.61,
  "flow_rate_per_min": 180.5,
  "risk_score": 54,
  "risk_band": "moderate",
  "is_observed": true
}
```
- `is_observed` — `true` if this came from a sensor/generator observation, `false` if it is a twin estimate only. **The frontend renders estimated entities with a dashed outline.**

### 2.4 `ForecastPoint` / `Forecast`
```json
{
  "entity_id": "metro_b",
  "source": "tsfm",
  "generated_at": "2026-09-04T14:32:00Z",
  "baseline_value": 0.61,
  "points": [
    { "horizon_sec": 900,  "predicted_utilisation": 0.78, "lower_90": 0.71, "upper_90": 0.86 },
    { "horizon_sec": 1800, "predicted_utilisation": 0.94, "lower_90": 0.83, "upper_90": 1.05 },
    { "horizon_sec": 3600, "predicted_utilisation": 0.88, "lower_90": 0.70, "upper_90": 1.06 }
  ],
  "time_to_critical_sec": 1080,
  "baseline_comparison": {
    "persistence_mae": 0.094,
    "model_mae": 0.061,
    "improvement_pct": 35.1
  }
}
```
- **Horizons are always exactly `[900, 1800, 3600]`.** Frontend can hardcode three columns.
- `time_to_critical_sec` — seconds until `predicted_utilisation` crosses the critical threshold, or `null` if it does not within 3600s. **This is the demo's hero number** (`1080` → "18 minutes").
- `baseline_comparison` may be `null` in the first minutes of a run; frontend must handle that.

### 2.5 `CascadeStep` / `CascadeResult`
```json
{
  "root_entity_id": "metro_b",
  "source": "deterministic",
  "generated_at": "2026-09-04T14:32:00Z",
  "total_downstream_failures": 3,
  "max_depth": 3,
  "steps": [
    {
      "step_index": 0,
      "entity_id": "metro_b",
      "predicted_band": "critical",
      "eta_sec": 1080,
      "failure_probability": 0.88,
      "via_edge_id": null,
      "depth": 0
    },
    {
      "step_index": 1,
      "entity_id": "gate_3",
      "predicted_band": "critical",
      "eta_sec": 1080,
      "failure_probability": 0.81,
      "via_edge_id": "metro_b__gate_3__feeds",
      "depth": 1
    },
    {
      "step_index": 2,
      "entity_id": "road_4",
      "predicted_band": "high",
      "eta_sec": 1560,
      "failure_probability": 0.64,
      "via_edge_id": "gate_3__road_4__adjacent_to",
      "depth": 2
    },
    {
      "step_index": 3,
      "entity_id": "emergency_north",
      "predicted_band": "critical",
      "eta_sec": 1860,
      "failure_probability": 0.57,
      "via_edge_id": "road_4__emergency_north__evacuates_to",
      "depth": 3
    }
  ]
}
```
- `steps` is **ordered by `eta_sec` ascending**. The frontend animates in array order — it must not re-sort.
- `via_edge_id` is `null` only for `step_index: 0`.

### 2.6 `Certificate`
```json
{
  "certificate_id": "cert_a1b2",
  "intervention_id": "int_7f3c",
  "verdict": "UNSTABLE",
  "converged": true,
  "iterations": 22,
  "post_nudge_variance": 0.181,
  "baseline_variance": 0.164,
  "max_zone_utilisation": 1.07,
  "max_zone_entity_id": "gate_5",
  "oscillation_risk": true,
  "compliance_sensitivity": 0.42,
  "compliance_sweep": [
    { "compliance_rate": 0.4, "max_utilisation": 0.88, "verdict": "STABLE" },
    { "compliance_rate": 0.6, "max_utilisation": 1.07, "verdict": "UNSTABLE" },
    { "compliance_rate": 0.9, "max_utilisation": 1.14, "verdict": "UNSTABLE" }
  ],
  "reason": "At 60% compliance, gate_5 exceeds capacity within 22 minutes."
}
```
- **`compliance_sweep` is always exactly three rows at `0.4`, `0.6`, `0.9`.** Frontend renders three fixed cells.
- `reason` — one plain-English sentence, **generated by the solver, NOT by the LLM.** Max 140 chars.
- Verdict derivation is defined once, in `03_ML_CONTRACT.md` §4.4. Backend and frontend never recompute it.

### 2.7 `Intervention`
```json
{
  "intervention_id": "int_7f3c",
  "intervention_type": "stagger_entry",
  "status": "proposed",
  "target_entity_ids": ["gate_3", "gate_4"],
  "triggered_by_entity_id": "metro_b",
  "title": "Stagger entry by segment + 2 shuttles + North zone incentive",
  "description": "Delay group and price-sensitive segments by 12 minutes; deploy two shuttles from metro_c; activate ₹500 North-zone credit.",
  "estimated_relief_pct": 31.0,
  "estimated_cost_paise": 180000,
  "estimated_delay_sec": 720,
  "feasibility": 0.85,
  "rank_score": 0.74,
  "certificate": { "...Certificate object, or null if not yet certified..." },
  "created_at": "2026-09-04T14:32:00Z",
  "expires_at": "2026-09-04T14:47:00Z"
}
```
- `rank_score` — computed by the optimiser as `(relief_norm × stability_factor) / (cost_norm × delay_norm)`, clamped `0.0`–`1.0`. Formula owned by ML (`03_ML_CONTRACT.md` §5.3).
- `certificate` is embedded (not just an ID) so the frontend never needs a second round-trip.
- **An intervention with `certificate.verdict == "UNSTABLE"` is still returned** — the frontend shows it struck-through with the red badge. This is the demo moment. Never filter it server-side.

### 2.8 `TwinFidelity`
```json
{
  "sim_time": "2026-09-04T14:32:00Z",
  "assimilated_rmse": 41.2,
  "uncorrected_rmse": 118.7,
  "improvement_pct": 65.3,
  "ensemble_size": 20,
  "ensemble_spread": 0.14,
  "drift_mode_enabled": false,
  "history": [
    { "sim_time": "2026-09-04T14:27:00Z", "assimilated_rmse": 38.9, "uncorrected_rmse": 62.1 },
    { "sim_time": "2026-09-04T14:32:00Z", "assimilated_rmse": 41.2, "uncorrected_rmse": 118.7 }
  ]
}
```
- `history` — last 20 cycles, oldest first. Frontend plots both series as the drift chart.
- `drift_mode_enabled` — when `true`, the uncorrected ensemble is also being stepped for comparison. Costs CPU, so it is off by default and toggled for the demo.

### 2.9 `RegretEntry`
```json
{
  "regret_id": "reg_0031",
  "intervention_id": "int_7f3c",
  "intervention_type": "stagger_entry",
  "predicted_relief_pct": 31.0,
  "realised_relief_pct": 28.4,
  "counterfactual_relief_pct": 0.0,
  "regret": 2.6,
  "sim_time": "2026-09-04T14:47:00Z"
}
```
- `counterfactual_relief_pct` — from the twin's "do nothing" branch. Usually `0.0` or negative.
- `regret = predicted_relief_pct - realised_relief_pct` (signed; negative means we under-promised).

### 2.10 `Segment`
```json
{
  "segment_id": "price_sensitive",
  "display_name": "Price sensitive",
  "share": 0.28,
  "price_elasticity": 0.72,
  "time_elasticity": 0.21,
  "accessibility_constrained": false,
  "compliance_base_rate": 0.55
}
```

### 2.11 `Nudge` (attendee-facing)
```json
{
  "nudge_id": "ndg_4412",
  "attendee_id": "att_demo_1",
  "intervention_id": "int_7f3c",
  "headline": "Switch to North zone",
  "body": "15 minutes further, ₹500 credit, and priority shuttle access.",
  "tradeoff": {
    "extra_travel_sec": 900,
    "credit_paise": 50000,
    "perk": "priority_shuttle"
  },
  "target_entity_id": "hotel_north_cluster",
  "status": "pending",
  "issued_at": "2026-09-04T14:33:00Z",
  "expires_at": "2026-09-04T14:48:00Z"
}
```
`status`: `"pending" | "accepted" | "declined" | "expired"`

---

## 3. Error envelope

**Every** non-2xx response, from every endpoint:

```json
{
  "error": {
    "code": "ENTITY_NOT_FOUND",
    "message": "No entity with id 'metro_z'.",
    "detail": { "entity_id": "metro_z" }
  }
}
```

| HTTP | `code` values |
|---|---|
| 400 | `INVALID_REQUEST`, `INVALID_SCENARIO`, `INVALID_HORIZON` |
| 404 | `ENTITY_NOT_FOUND`, `INTERVENTION_NOT_FOUND`, `SIMULATION_NOT_FOUND`, `NUDGE_NOT_FOUND` |
| 409 | `INTERVENTION_ALREADY_RESOLVED`, `INTERVENTION_EXPIRED` |
| 422 | `MODEL_NOT_READY`, `INSUFFICIENT_HISTORY` |
| 500 | `INTERNAL_ERROR` |
| 503 | `ML_MODULE_UNAVAILABLE` |

**Frontend rule:** on `MODEL_NOT_READY` or `INSUFFICIENT_HISTORY`, show a skeleton/"warming up" state — never an error toast. These are expected in the first ~90 seconds of a run.

---

## 4. Degradation contract (what happens when a component fails)

This mirrors the fallback strategy in the strategy report. **Every consumer must handle these gracefully — a fallback is a normal state, not an error.**

| Component | Degraded state | How it is signalled | Consumer behaviour |
|---|---|---|---|
| TSFM forecaster | Falls back to `local_model`, then `persistence` | `forecast.source` field | Show source badge. No error. |
| Cascade GNN | Falls back to deterministic propagator | `cascade.source` field | Identical rendering. No error. |
| Equilibrium solver non-convergence | `converged: false`, `verdict: "UNSTABLE"` | Certificate fields | Show red badge, reason reads "Did not converge within iteration cap." |
| EnKF assimilation | `twin_fidelity.assimilated_rmse` present but `improvement_pct` may be low | Numeric | Chart still renders. |
| LLM Commander | Returns cached scripted answer | `commander.response.is_cached: true` | Small "cached" chip. |

**Rule: no component ever returns a 500 because a model was unavailable. It degrades and declares the degradation in a field.**

---

## 5. Naming registry (demo topology — frozen entity IDs)

So the three teams can hardcode against the same demo without waiting for each other.

| `entity_id` | type | display_name |
|---|---|---|
| `stadium_main` | venue | Main Stadium |
| `gate_1` … `gate_6` | gate | Gate 1 … Gate 6 |
| `metro_a`, `metro_b`, `metro_c` | transport_node | Metro A/B/C Station |
| `line_blue`, `line_red` | transport_route | Blue Line, Red Line |
| `road_1` … `road_6` | road | Arterial 1 … 6 |
| `zone_north`, `zone_south`, `zone_east`, `zone_west`, `zone_core` | zone | North/South/East/West/Core Zone |
| `hotel_north_cluster`, `hotel_core_cluster`, `hotel_east_cluster` | hotel | North/Core/East Hotel Cluster |
| `parking_p1` … `parking_p4` | parking | Parking P1 … P4 |
| `emergency_north`, `emergency_south` | emergency_facility | North/South Emergency Post |

**Demo cascade chain (must exist in the seed data):**
`metro_b` → `gate_3` → `road_4` → `emergency_north`

**Demo unstable intervention target:** `gate_5` (saturates when traffic is naively pushed to `metro_c`).

---

## 6. Change protocol

1. Any contract change → bump the version at the top of this file.
2. Announce in channel with a one-line diff summary.
3. Backend updates `00_SHARED_CONTRACT.md` + `01_BACKEND_CONTRACT.md` in the same commit.
4. **Additive changes only after H20.** After hour 20 of the build, no field may be renamed or removed — only added, and only with a default value.
