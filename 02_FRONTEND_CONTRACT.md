# 02 — FRONTEND CONTRACT

> **Reads:** `00_SHARED_CONTRACT.md` (mandatory), `01_BACKEND_CONTRACT.md` (endpoint reference).
> **Owner:** Frontend workstream.

**Stack:** React 18 · Vite · Deck.gl + Mapbox GL · Recharts · TailwindCSS · TanStack Query · native WebSocket

---

## 1. Responsibilities

Frontend **owns**: layout, colour, animation, formatting, local UI state, mock mode.

Frontend **does not own**:
- Any derived metric. If a number appears on screen it came from the API. **The frontend never computes `risk_band`, `verdict`, `improvement_pct`, or `time_to_critical` — it renders them.**
- Any sorting the backend already applied (`interventions` by `rank_score` desc, `cascade.steps` by `eta_sec` asc, `pressure_timeline` by urgency). Re-sorting breaks the animation contract.
- Any filtering of UNSTABLE interventions. They are shown, de-emphasised.

---

## 2. Formatting rules (so the demo reads consistently)

| Data | Rule | Example |
|---|---|---|
| `*_sec` durations | Convert to minutes, round to nearest int, suffix "min". Under 60s → "<1 min". | `1080` → `18 min` |
| `utilisation` | Percent, 0 decimals | `0.61` → `61%` |
| `risk_score` | Integer as-is | `54` |
| `*_pct` fields | 1 decimal, suffix `%` | `31.0` → `31.0%` |
| `*_paise` | `₹` + value/100, thousands separator | `50000` → `₹500` |
| `sim_time` | `HH:mm` (24h) | `14:32` |
| Unknown / `null` | Em dash `—`. Never `0`, never "N/A". | |

**The hero number rule:** `time_to_critical_sec` on the top pressure-timeline item renders at `text-6xl font-bold`. It is the largest text on the screen at all times. Nothing else competes with it.

---

## 3. Colour tokens (from `00_SHARED_CONTRACT.md` §1.3)

```js
export const RISK_COLORS = {
  low:      { hex: '#22C55E', bg: 'bg-green-500',  text: 'text-green-400'  },
  moderate: { hex: '#EAB308', bg: 'bg-yellow-500', text: 'text-yellow-400' },
  high:     { hex: '#F97316', bg: 'bg-orange-500', text: 'text-orange-400' },
  critical: { hex: '#EF4444', bg: 'bg-red-500',    text: 'text-red-400'    },
};

export const VERDICT_STYLE = {
  STABLE:      { hex: '#22C55E', label: 'STABLE',      icon: 'shield-check' },
  CONDITIONAL: { hex: '#EAB308', label: 'CONDITIONAL', icon: 'shield-alert' },
  UNSTABLE:    { hex: '#EF4444', label: 'UNSTABLE',    icon: 'shield-x'     },
};
```

**Never derive a colour from a numeric threshold in the frontend.** Always key off the `risk_band` / `verdict` string the API returned.

---

## 4. Application shell

```
/                       → Command Centre
/attendee               → Attendee PWA (separate route, mobile viewport)
/metrics                → KPI panel (judging slide, full screen)
```

### Command Centre layout (single screen, no scrolling)

```
┌────────────────────────────────────────────────────────────────────────┐
│ TopBar: event name · sim clock · overall risk pill · load_variance     │
├──────────────────────────────────────────┬─────────────────────────────┤
│                                          │  PressureTimeline           │
│                                          │  (hero: time_to_critical)   │
│           MapCanvas                      ├─────────────────────────────┤
│           (Deck.gl, dominant)            │  InterventionQueue          │
│           + CascadeOverlay               │  (cards + certificate badge)│
│                                          ├─────────────────────────────┤
│                                          │  TwinFidelityGauge          │
│                                          │  (+ drift toggle)           │
├──────────────────────────────────────────┴─────────────────────────────┤
│ CommanderBar (docked chat, collapsible, with sources tray)             │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Component contracts

Each component declares exactly what it consumes. **No component fetches its own data** — all data flows from the store (§7).

---

### 5.1 `<TopBar />`
**Consumes:** `state.summary`, `event`, `sim_time`

| Element | Source field |
|---|---|
| Event name | `event.name` |
| Sim clock | `state.sim_time` → `HH:mm` |
| Countdown to event start | `event.start_time - state.sim_time` |
| Overall risk pill | `state.summary.overall_risk_band` + `overall_risk_score` |
| Load variance | `state.summary.load_variance`, 3 decimals |
| Critical/high counts | `state.summary.critical_count`, `high_count` |

**Load variance gets a delta arrow** comparing to the value 5 cycles ago (frontend keeps a small ring buffer for this — this is the one exception to "never compute", and it is display-only).

---

### 5.2 `<MapCanvas />`
**Consumes:** `graph.nodes`, `graph.edges`, `graph.bounds`, `state.entities`

**Layers (Deck.gl, in z-order):**

| Layer | Data | Encoding |
|---|---|---|
| `ScatterplotLayer` "entities" | `graph.nodes` joined to `state.entities` by `entity_id` | radius ∝ `nominal_capacity`; fill = `RISK_COLORS[risk_band]`; **stroke dashed if `is_observed === false`** |
| `IconLayer` "types" | same | icon per `entity_type` |
| `PathLayer` "static-edges" | `graph.edges` where `edge_type === "feeds"` | thin grey, 20% opacity |
| `ArcLayer` "cascade" | `cascade.steps` (see 5.3) | animated, red gradient |
| `TextLayer` "eta" | `cascade.steps` | `eta_sec` formatted, offset above node |

**Join rule:** `state.entities` is a **delta** on WS updates. The map must merge into its existing node map by `entity_id`, never replace wholesale. An entity missing from a delta keeps its previous state.

**Interaction:**
- Click node → `GET /state/{entity_id}` → open `<EntityDetailPanel />`
- Click node → also `GET /cascade/{entity_id}` → arm `<CascadeOverlay />`
- Hover → tooltip: `display_name`, `utilisation`, `risk_band`

**Fit-to-view** uses `graph.bounds` on mount. Never recompute from node coordinates.

---

### 5.3 `<CascadeOverlay />`
**Consumes:** `CascadeResult`

**Animation contract — this is a demo-critical component:**
1. Render `steps` **in array order** (backend already sorted by `eta_sec` ascending — do not re-sort).
2. Reveal each step with a 400ms delay after the previous. Total reveal ≈ `steps.length × 400ms`.
3. Each step draws an `ArcLayer` arc along `via_edge_id` from that edge's `src` to `dst` coordinates.
4. Arc opacity = `failure_probability` (clamped to min 0.35 so nothing is invisible).
5. A countdown label at each node showing `eta_sec` formatted as minutes, ticking down live against `sim_time`.
6. `step_index: 0` (the root) pulses continuously; downstream steps do not.

**Empty state:** `total_downstream_failures === 0` → render only the root pulse and the text "No downstream propagation predicted."

---

### 5.4 `<PressureTimeline />`
**Consumes:** `GET /forecast/pressure-timeline`

For each item, a row:
```
┌─────────────────────────────────────────────────────┐
│ Metro B Station              ██████████████  18 min │  ← hero number
│ 61% now                      [6-pt sparkline]       │
└─────────────────────────────────────────────────────┘
```
- Sparkline from `trajectory` — always 6 points, fixed x-offsets `[0,300,600,900,1200,1800]`. Fixed width, no responsive recompute.
- Horizontal line at the critical threshold; sparkline segment above it turns red.
- Rows are **already sorted** by urgency. Render in order.
- Top row's `time_to_critical_sec` is the hero (`text-6xl`).

**Empty state:** "No entity predicted to reach critical within 60 minutes." — green background. This is the calm opening of the demo and it should look genuinely calm.

---

### 5.5 `<InterventionQueue />` / `<InterventionCard />`
**Consumes:** `interventions[]` (certificate embedded)

Card layout:
```
┌────────────────────────────────────────────────────────┐
│ [🛡 UNSTABLE]                        rank 0.41         │
│ Redirect 8,000 attendees to Metro C                    │
│ Relief 34.0%  ·  ₹1,200  ·  +9 min  ·  feasibility 0.9 │
│ ⚠ At 60% compliance, gate_5 exceeds capacity in 22 min │
│ ▸ Certificate detail                                   │
│                              [ Reject ]  [ Approve ]   │
└────────────────────────────────────────────────────────┘
```

**Rules:**
- `certificate.verdict === "UNSTABLE"` → card gets `opacity-60`, a red left border, and the title is **struck through**. Approve button remains enabled (the operator may override) but is styled secondary.
- `reason` renders verbatim from the certificate. **Never rewrite it in the frontend.**
- Expanding "Certificate detail" shows the `compliance_sweep` as exactly three fixed cells (40% / 60% / 90%) plus `post_nudge_variance` vs `baseline_variance`, `oscillation_risk`, `converged`, `iterations`.
- Cards render in received order (`rank_score` desc). Do not re-sort.
- On approve → `POST /interventions/{id}/approve` → optimistic status change to `executing`; reconcile on the `intervention_resolved` WS event.

**The demo pairing:** when two cards are visible with one UNSTABLE and one STABLE, the visual contrast between them is the single most important frame in the presentation. Test it explicitly.

---

### 5.6 `<TwinFidelityGauge />`
**Consumes:** `TwinFidelity`

Two states:
- **Collapsed (default):** small gauge showing `improvement_pct` and ensemble size. Understated.
- **Expanded (drift toggle on):** dual-line Recharts `LineChart` from `history` — `assimilated_rmse` in green, `uncorrected_rmse` in red, diverging over time.

**Toggle behaviour:**
1. User flips the switch → `POST /twin/drift-mode { enabled: true }`.
2. Chart clears and both series restart from the same point.
3. Over the next ~5 cycles the red line visibly diverges while green stays flat.
4. Caption below: *"Uncorrected agent-based model vs. Ensemble Kalman Filter assimilation."*

This is the highest-value 15 seconds of the demo. Build it early and rehearse the timing.

---

### 5.7 `<CommanderBar />`
**Consumes:** `POST /commander/query` response

- Input + message list.
- Every assistant message has a collapsed **"Sources"** tray. Expanding shows one row per `tool_calls[]` entry: tool name, args, `result_digest`.
- If `grounding.passed === false`, show an amber chip: "Some values could not be grounded and were removed."
- If `is_cached === true`, show a small grey "cached" chip.
- **Scripted demo questions** are pinned as quick-action buttons:
  1. "What is the biggest problem right now?"
  2. "Why is Metro B becoming critical?"
  3. "What happens if we do nothing?"
  4. "Which action gives the largest safety improvement?"

**Never render an assistant message without the sources tray available.** The visible grounding is the point.

---

### 5.8 `<EntityDetailPanel />`
**Consumes:** `GET /state/{entity_id}` response

Sections: current state · forecast chart (3 horizons with 90% bands) · risk breakdown bar chart by `risk_type` · inbound/outbound edges list (clickable to navigate) · "Show cascade" button.

---

### 5.9 `<WhatIfPanel />`
**Consumes:** `POST /simulate` + poll `GET /simulate/{id}`

- Scenario builder: dropdown of `scenario_type` + params form.
- Preset buttons for the demo scenarios: "Blue line −15%", "Heavy rain", "Gate 3 closure", "Combined".
- Result view: baseline vs scenario side-by-side (`peak_utilisation`, `load_variance`, `critical_count`) with delta arrows; `new_critical_entities` as red chips; candidate interventions listed below.
- While `status === "running"`: skeleton with a spinner, poll every 1000ms, give up after 15s with a retry button.

---

### 5.10 `<MetricsPanel />` (route `/metrics`)
**Consumes:** `GET /metrics`

Every metric renders as: **value · baseline name · improvement**. Never a bare number.

```
Cascade lead time
   17.4 min          vs threshold rule: 0 min
```

**Three metrics are visually emphasised** (larger, boxed):
- `prediction.cascade_lead_time_sec`
- `decision.load_variance_reduction_pct`
- `decision.unstable_interventions_caught`

---

### 5.11 Attendee PWA (route `/attendee`)

Four cards, mobile viewport (max-width 420px):

| Card | Data source |
|---|---|
| **Journey risk** | `POST /attendee/journey` → `journey_risk_score`, `journey_risk_band` as a traffic light |
| **Smart route** | `recommended_route` **and** `shortest_route` side by side, with the shortest one labelled with its `predicted_crowding_band` in red |
| **Zone recommendation** | From active nudge `target_entity_id` + `tradeoff`, showing the honest trade-off explicitly |
| **Nudge card** | `Nudge` object; Accept/Decline → `POST /attendee/nudges/{id}/respond` |

Accessibility toggle sets `segment_id: "accessibility_constrained"` on the journey request.

**Design point for the demo:** the shortest route must be visibly labelled as the *worse* option. That contrast is the attendee-side equivalent of the certificate badge.

---

## 6. WebSocket handling

```js
// src/lib/ws.js
const HANDLERS = {
  tick:                  p => store.setSummary(p.summary, p.sim_time, p.cycle_number),
  state_update:          p => store.mergeEntities(p.entities),      // MERGE, never replace
  forecast_update:       p => store.setPressureTimeline(p.pressure_timeline, p.active_source),
  cascade_alert:         p => store.setCascade(p.cascade),
  intervention_queued:   p => store.upsertIntervention(p.intervention),
  intervention_resolved: p => store.updateInterventionStatus(p.intervention_id, p.status),
  twin_fidelity:         p => store.setTwinFidelity(p),
  regret_update:         p => store.appendRegret(p.entry, p.summary),
  anomaly:               p => store.pushAnomaly(p),
  resync:                p => store.replaceAll(p),                  // full replace, only here
  nudge_pushed:          p => store.pushNudge(p.nudge),
  journey_risk_update:   p => store.setJourneyRisk(p),
};
```

**Rules:**
1. Drop any message whose `seq` ≤ last processed `seq`.
2. On disconnect: exponential backoff (1s, 2s, 4s, max 8s), show a small amber "reconnecting" chip in the TopBar.
3. On reconnect: send `{ action: "resync", last_seq }` and apply the `resync` payload as a **full replace**.
4. `state_update` is a delta — merging is mandatory. Replacing wholesale will blank the map.

---

## 7. Client store shape

```js
{
  // static, fetched once
  event: null,
  graph: { nodes: [], edges: [], segments: [], bounds: null },

  // live
  simTime: null,
  cycleNumber: 0,
  summary: { overall_risk_score, overall_risk_band, critical_count, high_count, load_variance },
  entities: {},                 // keyed by entity_id — merge target
  pressureTimeline: [],
  activeForecastSource: 'persistence',
  cascades: {},                 // keyed by root_entity_id
  activeCascadeRootId: null,
  interventions: [],
  twinFidelity: null,
  regret: { entries: [], summary: null },
  anomalies: [],

  // attendee
  journey: null,
  nudges: [],

  // ui-only
  selectedEntityId: null,
  driftModeEnabled: false,
  commanderMessages: [],
  wsStatus: 'connected',        // 'connected' | 'reconnecting' | 'offline'
  mockMode: false,
}
```

---

## 8. Mock mode — the parallel-development unblock

**This is the most important section for avoiding merge pain.** Frontend must be fully buildable and demoable before the backend exists.

```
src/mocks/
  graph.json                  # ~70 nodes, ~120 edges, frozen IDs from SHARED §5
  state_sequence.json         # 40 cycles of state deltas
  pressure_timeline.json      # progression where metro_b hits 18 min at cycle 12
  cascade_metro_b.json        # the demo chain: metro_b → gate_3 → road_4 → emergency_north
  interventions.json          # exactly 2: one UNSTABLE (metro_c reroute), one STABLE (stagger)
  twin_fidelity.json          # 20 cycles, divergence starting at cycle 8
  metrics.json
  commander_responses.json    # keyed by the 4 scripted questions
  attendee_journey.json
```

**Rules:**
- Enable with `VITE_MOCK=1` or `?mock=1`.
- Mock mode replays `state_sequence.json` on the same 30s cycle (accelerated), driving the identical store actions as the WS handlers.
- **Mock payloads must validate against the exact same schemas.** Backend and frontend both import the JSON Schema files in `contracts/schemas/` and run validation in CI. If a mock diverges from the contract, the build fails.
- Every mock file is authored on **day one, before any real endpoint exists.**

**Ownership:** the frontend lead authors mocks from `00_SHARED_CONTRACT.md`; the backend lead reviews them within the first two hours. That review *is* the integration test.

---

## 9. Loading, empty and error states

| Condition | UI |
|---|---|
| `MODEL_NOT_READY` / `INSUFFICIENT_HISTORY` | Skeleton + "Warming up…" — **never an error toast** |
| `forecast.baseline_comparison === null` | Hide the improvement badge, show the forecast |
| `interventions` empty | "No interventions required. System nominal." on a green panel |
| `pressureTimeline` empty | "No entity predicted to reach critical within 60 minutes." |
| `cascade.total_downstream_failures === 0` | Root pulse only + "No downstream propagation predicted." |
| WS disconnected | Amber chip in TopBar; last-known data stays rendered (do not blank the screen) |
| `POST` 409 | Toast: "This intervention was already resolved." + refetch queue |
| Any 5xx | Toast with `error.message`; retry button |

---

## 10. Performance guardrails

- Deck.gl layers use `updateTriggers` keyed on `simTime` — never rebuild layer objects every render.
- `state_update` deltas are merged in a single `setState`, not per-entity.
- Recharts series capped at 20 points (matches `TwinFidelity.history` length).
- Cascade arc animation uses `requestAnimationFrame`, not `setInterval`.
- **Default Deck.gl styling until H30.** Visual polish is the last thing, and it is the first thing to cut.

---

## 11. Frontend definition-of-done checklist

- [ ] Runs fully in mock mode with zero backend
- [ ] All mock JSON validates against `contracts/schemas/`
- [ ] `state_update` merges (verified: an entity absent from a delta retains state)
- [ ] Cascade animates in received array order with correct 400ms stagger
- [ ] Hero `time_to_critical` is the largest text on screen
- [ ] UNSTABLE card is struck-through, de-emphasised, and still approvable
- [ ] `certificate.reason` renders verbatim
- [ ] Drift toggle produces visible divergence within 5 cycles
- [ ] Commander sources tray is present on every assistant message
- [ ] Attendee view shows recommended vs shortest route side by side
- [ ] WS reconnect performs a full resync without a blank frame
- [ ] No component computes `risk_band`, `verdict`, or any `_pct` value locally
