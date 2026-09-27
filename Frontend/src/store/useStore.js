/**
 * Client store — 02_FRONTEND_CONTRACT.md §7.
 *
 * Two rules this file exists to enforce:
 *
 *  1. `state_update` is a **delta**. `mergeEntities` merges by `entity_id` and
 *     never replaces wholesale — an entity absent from a delta keeps its state.
 *     Replacing would blank the map every cycle.
 *  2. Only `replaceAll` (the `resync` handler) does a full replace.
 *
 * No component fetches its own data; everything flows from here.
 */
import { create } from 'zustand';

const LOAD_VARIANCE_BUFFER = 6; // current + 5 cycles back, for the delta arrow

export const useStore = create((set, get) => ({
  // --- static, fetched once per world ------------------------------------
  event: null,
  // The world the engine simulates (synthetic demo or a generated blueprint).
  // A change of world_id / run_id means the graph must be refetched.
  world: null,
  graph: { nodes: [], edges: [], segments: [], bounds: null },
  nodesById: {},

  // --- live -----------------------------------------------------------
  simTime: null,
  cycleNumber: 0,
  summary: {
    overall_risk_score: 0,
    overall_risk_band: 'low',
    critical_count: 0,
    high_count: 0,
    load_variance: 0,
  },
  loadVarianceHistory: [],
  entities: {},
  pressureTimeline: [],
  activeForecastSource: 'persistence',
  cascades: {},
  activeCascadeRootId: null,
  interventions: [],
  twinFidelity: null,
  regret: { entries: [], summary: null },
  anomalies: [],
  operations: null,
  events: [],
  disruptions: [],
  // --- weather-driven digital twin -----------------------------------------
  // The full `GET /weather` payload: reading, forecast, impact vector, and the
  // two honesty flags (`availability`, `driving_live`). Rendered, never
  // recomputed — the impact numbers and their bands are the backend's.
  weather: null,
  socialSignals: null,
  // A completed weather what-if, kept separately from `whatIf` so the Digital
  // Twin page and the What-If lab do not overwrite each other's result.
  weatherScenario: { status: 'idle', result: null, params: null },
  // Bumped whenever the city changes outside the regular cycle (schedule
  // change, disruption, approval) so pages holding REST snapshots refetch.
  worldVersion: 0,

  // --- attendee -------------------------------------------------------
  journey: null,
  nudges: [],

  // --- ui-only --------------------------------------------------------
  selectedEntityId: null,
  entityDetail: null,
  driftModeEnabled: false,
  commanderMessages: [],
  wsStatus: 'connecting',
  speed: null,
  paused: false,
  mockMode: false,
  toasts: [],
  whatIf: { status: 'idle', result: null, label: null },
  menuOpen: false,

  // --- action visualisation (TASK 2) ---------------------------------
  // Tracked lifecycle of executed / rejected actions. Every value the HUD and
  // map layers show is derived from live `entities` vs a T0 snapshot captured
  // at approval — see lib/actionEffects.js.
  trackedActions: [],
  actionHudExpanded: false,
  // A completed what-if projection, shown on the map in a distinct "SIMULATED"
  // style. Never touches `entities` — it is a forked-twin result, not live.
  whatIfOverlay: null,

  // --- static setters --------------------------------------------------
  setEvent: (event) => set({ event }),
  setGraph: (graph) =>
    set({
      graph,
      nodesById: Object.fromEntries((graph.nodes || []).map((n) => [n.entity_id, n])),
    }),
  setMockMode: (mockMode) => set({ mockMode }),
  setWorld: (world) => set({ world: world || null }),
  setSimControl: ({ speed, paused }) =>
    set((s) => ({ speed: speed ?? s.speed, paused: paused ?? s.paused })),
  setWsStatus: (wsStatus) => set({ wsStatus }),

  // --- WS handlers (02 §6) ----------------------------------------------
  setSummary: (summary, simTime, cycleNumber, operations) =>
    set((s) => {
      const history = [...s.loadVarianceHistory, summary.load_variance].slice(-LOAD_VARIANCE_BUFFER);
      return {
        summary, simTime, cycleNumber, loadVarianceHistory: history,
        operations: operations || s.operations,
      };
    }),

  setEvents: (events) => set((s) => ({ events: events || [], worldVersion: s.worldVersion + 1 })),
  upsertEvent: (event) =>
    set((s) => ({
      events: s.events.some((e) => e.event_id === event.event_id)
        ? s.events.map((e) => (e.event_id === event.event_id ? event : e))
        : [...s.events, event],
      worldVersion: s.worldVersion + 1,
    })),
  removeEvent: (eventId) =>
    set((s) => ({ events: s.events.filter((e) => e.event_id !== eventId), worldVersion: s.worldVersion + 1 })),
  setDisruptions: (disruptions) =>
    set((s) => ({ disruptions: disruptions || [], worldVersion: s.worldVersion + 1 })),

  /**
   * `GET /weather` or the `weather_update` WS event. A weather change that is
   * driving the live city arrives as an ordinary `state_update` /
   * `state_reconciled` too — this only carries the reading and the impact
   * vector, so the page never has to compute either.
   */
  setWeather: (weather) => set({ weather }),
  setSocialSignals: (socialSignals) => set({ socialSignals }),
  setWeatherScenario: (patch) =>
    set((s) => ({ weatherScenario: { ...s.weatherScenario, ...patch } })),
  clearWeatherScenario: () => set({ weatherScenario: { status: 'idle', result: null, params: null } }),
  bumpWorld: () => set((s) => ({ worldVersion: s.worldVersion + 1 })),

  /** MERGE, never replace. This is the rule that keeps the map from blanking. */
  mergeEntities: (list) =>
    set((s) => {
      if (!list || list.length === 0) return {};
      const next = { ...s.entities };
      for (const entity of list) next[entity.entity_id] = entity;
      return { entities: next, simTime: list[0].sim_time || s.simTime };
    }),

  setPressureTimeline: (items, activeSource) =>
    set({
      pressureTimeline: items || [],
      activeForecastSource: activeSource || 'persistence',
    }),

  setCascade: (cascade) =>
    set((s) => ({
      cascades: { ...s.cascades, [cascade.root_entity_id]: cascade },
      // Do NOT auto-arm activeCascadeRootId here. Cascade lines only appear
      // when the operator explicitly clicks "Show cascade" (02 §5.8) or
      // "Show cascade" in the EntityDetailPanel — never automatically.
    })),

  setCascades: (list) =>
    set(() => ({
      cascades: Object.fromEntries((list || []).map((c) => [c.root_entity_id, c])),
    })),

  setActiveCascadeRoot: (rootId) => set({ activeCascadeRootId: rootId }),

  upsertIntervention: (intervention) =>
    set((s) => {
      const rest = s.interventions.filter(
        (i) => i.intervention_id !== intervention.intervention_id,
      );
      // Backend order is authoritative (rank_score desc). New arrivals are
      // appended and the list is re-ordered ONLY by the score the API gave us.
      const merged = [...rest, intervention].sort((a, b) => b.rank_score - a.rank_score);
      return { interventions: merged };
    }),

  setInterventions: (interventions) => set({ interventions: interventions || [] }),

  setInterventionEffects: (effects) =>
    set((s) => {
      const byId = Object.fromEntries((effects || []).map((e) => [e.intervention_id, e.live_effect]));
      return {
        interventions: s.interventions.map((i) =>
          byId[i.intervention_id] ? { ...i, status: 'executing', live_effect: byId[i.intervention_id] } : i,
        ),
      };
    }),

  updateInterventionStatus: (interventionId, status) =>
    set((s) => ({
      interventions: s.interventions.map((i) =>
        i.intervention_id === interventionId ? { ...i, status } : i,
      ),
    })),

  setTwinFidelity: (twinFidelity) =>
    set({ twinFidelity, driftModeEnabled: !!twinFidelity?.drift_mode_enabled }),

  setDriftMode: (enabled) => set({ driftModeEnabled: enabled }),

  appendRegret: (entry, summary) =>
    set((s) => ({
      regret: {
        entries: [...s.regret.entries.filter((e) => e.regret_id !== entry.regret_id), entry],
        summary: summary || s.regret.summary,
      },
      // A regret entry IS the settled result of an executed action (engine.py
      // `_settle_executing_interventions`). Fold it into the matching tracked
      // action so the HUD can show realised vs do-nothing.
      trackedActions: s.trackedActions.map((a) =>
        a.kind === 'executed' && a.interventionId === entry.intervention_id && !a.settled
          ? { ...a, settled: entry, phase: 'settled', settledAtMs: Date.now() }
          : a,
      ),
    })),

  setRegret: (regret) => set({ regret: regret || { entries: [], summary: null } }),

  // --- action visualisation reducers (TASK 2) -----------------------------
  beginExecutedAction: ({ intervention, snapshot, t0Cascade, simTime, cycleNumber, mock }) =>
    set((s) => {
      const targetIds = intervention.target_entity_ids || [];
      const action = {
        id: `act_${intervention.intervention_id}`,
        interventionId: intervention.intervention_id,
        type: intervention.intervention_type,
        title: intervention.title,
        targetIds,
        rootId: intervention.triggered_by_entity_id || targetIds[0] || null,
        kind: 'executed',
        mock: !!mock,
        estimatedReliefPct: intervention.estimated_relief_pct,
        verdict: intervention.certificate?.verdict || null,
        t0: { simTime, cycleNumber, byEntity: snapshot || {}, cascade: t0Cascade || null },
        phase: 'starting',
        live: { deltas: [], downstream: [], cascade: null, nowCascade: null },
        settled: null,
        startedAtMs: Date.now(),
        settledAtMs: null,
      };
      return {
        trackedActions: [
          ...s.trackedActions.filter((a) => a.interventionId !== action.interventionId),
          action,
        ],
        actionHudExpanded: true,
      };
    }),

  noteRejectedAction: (intervention) =>
    set((s) => ({
      trackedActions: [
        ...s.trackedActions.filter((a) => a.kind !== 'rejected'),
        {
          id: `rej_${intervention.intervention_id}`,
          interventionId: intervention.intervention_id,
          title: intervention.title,
          kind: 'rejected',
          phase: 'rejected',
          startedAtMs: Date.now(),
        },
      ],
    })),

  updateActionLive: (id, patch) =>
    set((s) => ({
      trackedActions: s.trackedActions.map((a) =>
        a.id === id ? { ...a, live: { ...a.live, ...patch } } : a,
      ),
    })),

  setActionPhase: (id, phase) =>
    set((s) => ({
      trackedActions: s.trackedActions.map((a) => (a.id === id ? { ...a, phase } : a)),
    })),

  dismissAction: (id) =>
    set((s) => ({ trackedActions: s.trackedActions.filter((a) => a.id !== id) })),

  /**
   * Drop all tracked-action UI state. Used when the mock replay wraps to a new
   * iteration (lib/mockLifecycle.js) — an action from the previous pass is
   * stale and, in mock mode, would never settle. Regret/history is separate.
   */
  clearTrackedActions: () => set({ trackedActions: [], actionHudExpanded: false }),

  /**
   * Full reset of transient simulation state at an iteration boundary.
   * Called by the mock driver when the recording loops. Does NOT reset
   * entities/summary/twinFidelity/pressureTimeline because those are
   * immediately overwritten by the first frame of the new iteration.
   */
  resetSimState: () =>
    set({
      loadVarianceHistory: [],
      interventions: [],
      cascades: {},
      activeCascadeRootId: null,
      selectedEntityId: null,
      entityDetail: null,
      trackedActions: [],
      actionHudExpanded: false,
      whatIfOverlay: null,
      whatIf: { status: 'idle', result: null, label: null },
      weatherScenario: { status: 'idle', result: null, params: null },
      anomalies: [],
    }),

  pruneActions: (nowMs) =>
    set((s) => {
      const kept = s.trackedActions.filter((a) => {
        if (a.kind === 'rejected') return nowMs - a.startedAtMs < 5000;
        if (a.phase === 'settled') return nowMs - (a.settledAtMs || nowMs) < 30000;
        return true;
      });
      return kept.length === s.trackedActions.length ? {} : { trackedActions: kept };
    }),

  setActionHudExpanded: (actionHudExpanded) => set({ actionHudExpanded }),

  setWhatIfOverlay: (whatIfOverlay) => set({ whatIfOverlay }),
  clearWhatIfOverlay: () => set({ whatIfOverlay: null }),

  pushAnomaly: (anomaly) =>
    set((s) => ({ anomalies: [anomaly, ...s.anomalies].slice(0, 20) })),

  /** Full replace — only ever called from the `resync` handler (02 §6 rule 3). */
  replaceAll: (payload) =>
    set((s) => {
      const state = payload.state || {};
      const world = payload.world || s.world;
      const worldChanged = Boolean(payload.world && s.world && payload.world.world_id !== s.world.world_id);
      // A clock that went backwards, a new run id, or a different world is a new
      // run (reset / seek / activation): drop the old run's transient UI state,
      // not just the server-owned collections.
      const newRun =
        worldChanged ||
        Boolean(payload.world && s.world && payload.world.run_id !== s.world.run_id) ||
        (state.cycle_number ?? s.cycleNumber) < s.cycleNumber;
      const runReset = newRun
        ? {
            trackedActions: [],
            actionHudExpanded: false,
            whatIfOverlay: null,
            whatIf: { status: 'idle', result: null, label: null },
            weatherScenario: { status: 'idle', result: null, params: null },
            loadVarianceHistory: [],
            anomalies: [],
            activeCascadeRootId: null,
            selectedEntityId: null,
            entityDetail: null,
            journey: null,
          }
        : {};
      const entities = Object.fromEntries(
        (state.entities || []).map((e) => [e.entity_id, e]),
      );
      return {
        entities,
        simTime: state.sim_time || s.simTime,
        cycleNumber: state.cycle_number ?? s.cycleNumber,
        summary: state.summary || s.summary,
        pressureTimeline: payload.pressure_timeline || [],
        activeForecastSource: payload.active_forecast_source || s.activeForecastSource,
        interventions: payload.interventions || [],
        cascades: Object.fromEntries(
          (payload.cascades || []).map((c) => [c.root_entity_id, c]),
        ),
        twinFidelity: payload.twin_fidelity || s.twinFidelity,
        regret: payload.regret || s.regret,
        nudges: payload.nudges || s.nudges,
        events: payload.events || s.events,
        disruptions: payload.disruptions || s.disruptions,
        operations: payload.operations || s.operations,
        worldVersion: s.worldVersion + 1,
        world,
        // The reading belongs to a world's venue: a different world means the
        // old weather and the old signals describe the wrong place.
        ...(worldChanged ? { weather: null, socialSignals: null } : {}),
        // A different world: the old graph must not be drawn against the new
        // state. App refetches /graph for the new world_id.
        ...(worldChanged
          ? { graph: { nodes: [], edges: [], segments: [], bounds: null }, nodesById: {}, event: null }
          : {}),
        ...runReset,
      };
    }),

  // --- attendee ---------------------------------------------------------
  pushNudge: (nudge) =>
    set((s) => ({
      nudges: [nudge, ...s.nudges.filter((n) => n.nudge_id !== nudge.nudge_id)],
    })),
  setNudges: (nudges) => set({ nudges: nudges || [] }),
  updateNudgeStatus: (nudgeId, status) =>
    set((s) => ({
      nudges: s.nudges.map((n) => (n.nudge_id === nudgeId ? { ...n, status } : n)),
    })),
  setJourney: (journey) => set({ journey }),
  setJourneyRisk: (payload) =>
    set((s) => ({
      journey: s.journey
        ? {
            ...s.journey,
            journey_risk_score: payload.journey_risk_score,
            journey_risk_band: payload.journey_risk_band,
          }
        : s.journey,
    })),

  // --- ui ----------------------------------------------------------------
  // Selecting a different entity clears the cascade overlay so stale lines
  // from the previous selection do not persist on the map.
  selectEntity: (selectedEntityId) =>
    set({ selectedEntityId, entityDetail: null, activeCascadeRootId: null }),
  setEntityDetail: (entityDetail) => set({ entityDetail }),
  // Closing the detail panel also clears the cascade overlay.
  clearSelection: () =>
    set({ selectedEntityId: null, entityDetail: null, activeCascadeRootId: null }),

  pushCommanderMessage: (message) =>
    set((s) => ({ commanderMessages: [...s.commanderMessages, message] })),
  replaceLastCommanderMessage: (message) =>
    set((s) => {
      const next = [...s.commanderMessages];
      next[next.length - 1] = message;
      return { commanderMessages: next };
    }),

  setWhatIf: (whatIf) => set((s) => ({ whatIf: { ...s.whatIf, ...whatIf } })),

  setMenuOpen: (menuOpen) => set({ menuOpen }),

  toast: (message, tone = 'info') =>
    set((s) => ({
      toasts: [...s.toasts, { id: `${Date.now()}-${Math.random()}`, message, tone }],
    })),
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

/** Load variance five cycles ago — the one display-only computation 02 §5.1 allows. */
export function loadVarianceDelta(state) {
  const history = state.loadVarianceHistory;
  if (history.length < 2) return null;
  return history[history.length - 1] - history[0];
}
