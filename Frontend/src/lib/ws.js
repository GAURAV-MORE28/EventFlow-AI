/**
 * WebSocket client — 02_FRONTEND_CONTRACT.md §6.
 *
 * Three rules, all of which exist because breaking them is visible on screen:
 *
 *  1. Drop any message whose `seq` <= the last processed seq.
 *  2. Exponential backoff on disconnect (1s, 2s, 4s, max 8s) with an amber chip
 *     in the TopBar — and the last-known data stays rendered. Never blank.
 *  3. On reconnect send `{action: "resync", last_seq}` and apply the reply as a
 *     full replace. `resync` is the only handler that replaces.
 *
 * Rule 1 is scoped to ONE connection: every new socket starts a new sequence
 * context (lastSeq = 0), because the server's seq restarts with the process.
 */

const BACKOFF_MS = [1000, 2000, 4000, 8000];

export function connectWebSocket(store, { client = 'command_centre', attendeeId = null } = {}) {
  let socket = null;
  let lastSeq = 0;
  let attempt = 0;
  let closedByCaller = false;
  let retryTimer = null;

  const handlers = {
    tick: (p) => store.setSummary(p.summary, p.sim_time, p.cycle_number),
    state_update: (p) => store.mergeEntities(p.entities), // MERGE, never replace
    forecast_update: (p) => store.setPressureTimeline(p.pressure_timeline, p.active_source),
    cascade_alert: (p) => store.setCascade(p.cascade),
    intervention_queued: (p) => store.upsertIntervention(p.intervention),
    intervention_resolved: (p) => store.updateInterventionStatus(p.intervention_id, p.status),
    twin_fidelity: (p) => store.setTwinFidelity(p),
    regret_update: (p) => store.appendRegret(p.entry, p.summary),
    anomaly: (p) => store.pushAnomaly(p),
    resync: (p) => store.replaceAll(p), // full replace, only here
    nudge_pushed: (p) => store.pushNudge(p.nudge),
    journey_risk_update: (p) => store.setJourneyRisk(p),
    demo_status: (p) => store.setDemo(p), // observer mode (Phase 0)
    pong: () => {},
  };

  function url() {
    const configured = import.meta.env?.VITE_WS_URL || '/ws';
    const base = configured.startsWith('ws')
      ? configured
      : `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}${configured}`;
    const params = new URLSearchParams({ client });
    if (attendeeId) params.set('attendee_id', attendeeId);
    return `${base}?${params.toString()}`;
  }

  function open() {
    if (closedByCaller) return;
    store.setWsStatus(attempt === 0 ? 'connecting' : 'reconnecting');

    const ws = new WebSocket(url());
    socket = ws;

    ws.onopen = () => {
      if (ws !== socket) return;
      attempt = 0;
      // A new connection is a new sequence context. `seq` is only monotonic
      // within one server process; after a backend restart it starts again at
      // 1, so carrying the previous connection's lastSeq forward would drop
      // every message — including the resync below — and freeze the UI while
      // the chip says "connected". Within this connection, rule 1 still holds.
      lastSeq = 0;
      store.setWsStatus('connected');
      // Ask for the full picture rather than waiting for the next delta.
      ws.send(JSON.stringify({ action: 'resync', last_seq: lastSeq }));
    };

    ws.onmessage = (raw) => {
      // Frames from a superseded socket must not touch this connection's state.
      if (ws !== socket) return;
      let message;
      try {
        message = JSON.parse(raw.data);
      } catch {
        return;
      }

      // Rule 1: out-of-order and replayed messages are dropped.
      if (typeof message.seq === 'number' && message.seq <= lastSeq) return;
      if (typeof message.seq === 'number') lastSeq = message.seq;

      const handler = handlers[message.event];
      if (handler) handler(message.payload || {});
    };

    ws.onerror = () => {
      // `onclose` always follows; retry logic lives there so it runs once.
    };

    ws.onclose = () => {
      if (closedByCaller || ws !== socket) return;
      store.setWsStatus('reconnecting');
      const wait = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
      attempt += 1;
      retryTimer = setTimeout(open, wait);
    };
  }

  open();

  return () => {
    closedByCaller = true;
    if (retryTimer) clearTimeout(retryTimer);
    if (socket && socket.readyState <= WebSocket.OPEN) socket.close();
    store.setWsStatus('offline');
  };
}
