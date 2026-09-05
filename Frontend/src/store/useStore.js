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
  // --- static, fetched once -------------------------------------------
  event: null,
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

  // --- attendee -------------------------------------------------------
  journey: null,
  nudges: [],

  // --- ui-only --------------------------------------------------------
  selectedEntityId: null,
  entityDetail: null,
  driftModeEnabled: false,
  commanderMessages: [],
  wsStatus: 'connecting',
  mockMode: false,
  toasts: [],
  whatIf: { status: 'idle', result: null, label: null },

  // --- static setters --------------------------------------------------
  setEvent: (event) => set({ event }),
  setGraph: (graph) =>
    set({
      graph,
      nodesById: Object.fromEntries((graph.nodes || []).map((n) => [n.entity_id, n])),
    }),
  setMockMode: (mockMode) => set({ mockMode }),
  setWsStatus: (wsStatus) => set({ wsStatus }),

  // --- WS handlers (02 §6) ----------------------------------------------
  setSummary: (summary, simTime, cycleNumber) =>
    set((s) => {
      const history = [...s.loadVarianceHistory, summary.load_variance].slice(-LOAD_VARIANCE_BUFFER);
      return { summary, simTime, cycleNumber, loadVarianceHistory: history };
    }),

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
      // Arm the newest cascade only if the operator has not pinned one.
      activeCascadeRootId: s.activeCascadeRootId || cascade.root_entity_id,
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
      regret: { entries: [...s.regret.entries, entry], summary: summary || s.regret.summary },
    })),

  setRegret: (regret) => set({ regret: regret || { entries: [], summary: null } }),

  pushAnomaly: (anomaly) =>
    set((s) => ({ anomalies: [anomaly, ...s.anomalies].slice(0, 20) })),

  /** Full replace — only ever called from the `resync` handler (02 §6 rule 3). */
  replaceAll: (payload) =>
    set((s) => {
      const state = payload.state || {};
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
  selectEntity: (selectedEntityId) => set({ selectedEntityId, entityDetail: null }),
  setEntityDetail: (entityDetail) => set({ entityDetail }),
  clearSelection: () => set({ selectedEntityId: null, entityDetail: null }),

  pushCommanderMessage: (message) =>
    set((s) => ({ commanderMessages: [...s.commanderMessages, message] })),
  replaceLastCommanderMessage: (message) =>
    set((s) => {
      const next = [...s.commanderMessages];
      next[next.length - 1] = message;
      return { commanderMessages: next };
    }),

  setWhatIf: (whatIf) => set((s) => ({ whatIf: { ...s.whatIf, ...whatIf } })),

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
