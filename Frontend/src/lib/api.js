/**
 * Network layer. Nothing above this file speaks HTTP.
 *
 * Casing note (00 §0): the wire is snake_case and stays snake_case here.
 * Components may destructure into camelCase locals, but the network layer never
 * renames a field — that drift is how two teams end up with two vocabularies.
 */
import * as mocks from './mocks.js';

export const MOCK_MODE =
  import.meta.env.VITE_MOCK === '1' ||
  new URLSearchParams(window.location.search).get('mock') === '1';

const BASE = import.meta.env.VITE_API_BASE || '/api/v1';

/** Error envelope from 00 §3, normalised into a throwable. */
export class ApiError extends Error {
  constructor(code, message, detail, status) {
    super(message);
    this.code = code;
    this.detail = detail || {};
    this.status = status;
  }

  /** 00 §4 — these are expected during warm-up and must never show as errors. */
  get isWarmingUp() {
    return this.code === 'MODEL_NOT_READY' || this.code === 'INSUFFICIENT_HISTORY';
  }
}

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });

  if (!response.ok) {
    let body = null;
    try {
      body = await response.json();
    } catch {
      /* a non-JSON error body is still an error */
    }
    const envelope = body?.error;
    throw new ApiError(
      envelope?.code || 'INTERNAL_ERROR',
      envelope?.message || `Request failed with ${response.status}`,
      envelope?.detail,
      response.status,
    );
  }

  return response.status === 204 ? null : response.json();
}

const get = (path) => request(path);
const post = (path, body) =>
  request(path, { method: 'POST', body: JSON.stringify(body ?? {}) });

/**
 * Every call routes through the mock table when mock mode is on. Flipping
 * `VITE_MOCK` is the whole integration switch — 02 §8.
 */
export const api = {
  health: () => (MOCK_MODE ? mocks.health() : get('/health')),
  event: () => (MOCK_MODE ? mocks.event() : get('/event')),
  graph: () => (MOCK_MODE ? mocks.graph() : get('/graph')),
  state: () => (MOCK_MODE ? mocks.state() : get('/state')),
  entity: (id) => (MOCK_MODE ? mocks.entity(id) : get(`/state/${id}`)),
  forecast: () => (MOCK_MODE ? mocks.forecast() : get('/forecast')),
  pressureTimeline: () =>
    MOCK_MODE ? mocks.pressureTimeline() : get('/forecast/pressure-timeline'),
  cascade: (id) => (MOCK_MODE ? mocks.cascade(id) : get(`/cascade/${id}`)),
  activeCascades: () => (MOCK_MODE ? mocks.activeCascades() : get('/cascade/active')),
  interventions: (status = 'proposed', limit = 10) =>
    MOCK_MODE
      ? mocks.interventions()
      : get(`/interventions?status=${status}&limit=${limit}`),
  approve: (id, operatorId = 'op_demo', note) =>
    MOCK_MODE
      ? mocks.approve(id)
      : post(`/interventions/${id}/approve`, { operator_id: operatorId, note }),
  reject: (id, operatorId = 'op_demo', reason) =>
    MOCK_MODE
      ? mocks.reject(id)
      : post(`/interventions/${id}/reject`, { operator_id: operatorId, reason }),
  twinFidelity: () => (MOCK_MODE ? mocks.twinFidelity() : get('/twin/fidelity')),
  driftMode: (enabled) =>
    MOCK_MODE ? mocks.driftMode(enabled) : post('/twin/drift-mode', { enabled }),
  regret: () => (MOCK_MODE ? mocks.regret() : get('/regret')),
  metrics: () => (MOCK_MODE ? mocks.metrics() : get('/metrics')),
  commander: (query, sessionId = 'sess_demo') =>
    MOCK_MODE
      ? mocks.commander(query)
      : post('/commander/query', { query, session_id: sessionId }),
  simulate: (scenarios, label, horizonSec = 3600) =>
    MOCK_MODE
      ? mocks.simulate(label)
      : post('/simulate', { scenarios, label, horizon_sec: horizonSec }),
  simulation: (id) => (MOCK_MODE ? mocks.simulation(id) : get(`/simulate/${id}`)),
  journey: (body) => (MOCK_MODE ? mocks.journey(body) : post('/attendee/journey', body)),
  nudges: (attendeeId) =>
    MOCK_MODE ? mocks.nudges(attendeeId) : get(`/attendee/nudges?attendee_id=${attendeeId}`),
  respondToNudge: (nudgeId, accepted) =>
    MOCK_MODE
      ? mocks.respondToNudge(nudgeId, accepted)
      : post(`/attendee/nudges/${nudgeId}/respond`, { accepted }),
  demoControl: (body) => (MOCK_MODE ? mocks.demoControl(body) : post('/demo/control', body)),
};
