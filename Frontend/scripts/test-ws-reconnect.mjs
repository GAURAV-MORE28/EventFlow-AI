#!/usr/bin/env node
/**
 * Regression test for the WebSocket restart freeze (adapted from the Mehul branch,
 * where it was written for audit item P0-07; the same bug existed on Paresh).
 *
 *   BUG: after the backend restarts, its global `seq` starts again at 1, but
 *        the client kept `lastSeq` from the previous connection and dropped
 *        every message with `seq <= lastSeq` — including the reconnect
 *        `resync`. The TopBar said "connected" while the UI stayed frozen
 *        until a manual browser refresh.
 *
 * This drives the real `connectWebSocket` (src/lib/ws.js) against a fake
 * WebSocket and a recording store, and asserts:
 *
 *   1. within one connection, duplicate and out-of-order seqs are dropped;
 *   2. a closed socket moves the status to `reconnecting` and schedules a retry;
 *   3. a NEW connection is a NEW sequence context: the restarted server's
 *      `resync` (seq 1) and subsequent ticks are applied without a refresh;
 *   4. frames arriving on a stale (superseded) socket are ignored;
 *   5. duplicate protection still works on the new connection.
 *
 *   npm test
 */
import assert from 'node:assert/strict';

// --- minimal browser surface ws.js needs --------------------------------------
globalThis.window = { location: { protocol: 'http:', host: 'localhost:5173' } };

const scheduled = [];
globalThis.setTimeout = (fn, ms) => {
  scheduled.push({ fn, ms });
  return scheduled.length;
};
globalThis.clearTimeout = () => {};

const sockets = [];
class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.sent = [];
    sockets.push(this);
  }
  send(data) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.readyState = FakeWebSocket.CLOSED;
  }
  // --- test helpers -------------------------------------------------------
  serverOpen() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  serverSend(event, seq, payload = {}) {
    this.onmessage?.({ data: JSON.stringify({ event, seq, sim_time: 't', payload }) });
  }
  serverDrop() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }
}
globalThis.WebSocket = FakeWebSocket;

// --- recording store ------------------------------------------------------------
const calls = [];
const statuses = [];
const record = (name) => (...args) => calls.push({ name, args });
const store = {
  setWsStatus: (s) => statuses.push(s),
  setSummary: record('setSummary'),
  mergeEntities: record('mergeEntities'),
  setPressureTimeline: record('setPressureTimeline'),
  setCascade: record('setCascade'),
  upsertIntervention: record('upsertIntervention'),
  updateInterventionStatus: record('updateInterventionStatus'),
  setTwinFidelity: record('setTwinFidelity'),
  appendRegret: record('appendRegret'),
  pushAnomaly: record('pushAnomaly'),
  replaceAll: record('replaceAll'),
  pushNudge: record('pushNudge'),
  setJourneyRisk: record('setJourneyRisk'),
  setCascades: record('setCascades'),
  setInterventionEffects: record('setInterventionEffects'),
  upsertEvent: record('upsertEvent'),
  removeEvent: record('removeEvent'),
  bumpWorld: record('bumpWorld'),
  setDisruptions: record('setDisruptions'),
};
const cycles = () => calls.filter((c) => c.name === 'setSummary').map((c) => c.args[2]);
const count = (name) => calls.filter((c) => c.name === name).length;

const { connectWebSocket } = await import('../src/lib/ws.js');
const stop = connectWebSocket(store, { client: 'command_centre' });

// --- connection 1 (original backend process) -------------------------------------
const s1 = sockets[0];
assert.equal(s1.url, 'ws://localhost:5173/ws?client=command_centre');
s1.serverOpen();
assert.equal(statuses.at(-1), 'connected');
assert.deepEqual(s1.sent[0], { action: 'resync', last_seq: 0 }, 'resync requested on open');

s1.serverSend('resync', 100, { state: { cycle_number: 58 } });
s1.serverSend('tick', 101, { summary: {}, sim_time: 't', cycle_number: 59 });
s1.serverSend('tick', 102, { summary: {}, sim_time: 't', cycle_number: 60 });
s1.serverSend('tick', 102, { summary: {}, sim_time: 't', cycle_number: 60 }); // duplicate
s1.serverSend('tick', 101, { summary: {}, sim_time: 't', cycle_number: 59 }); // out of order
assert.deepEqual(cycles(), [59, 60], 'duplicate / out-of-order frames dropped on one connection');
assert.equal(count('replaceAll'), 1);

// --- backend restarts: socket drops, client schedules a retry --------------------
s1.serverDrop();
assert.equal(statuses.at(-1), 'reconnecting', 'status shows reconnecting after drop');
assert.equal(scheduled.length, 1, 'exactly one retry scheduled');
assert.equal(scheduled[0].ms, 1000, 'first backoff is 1s');
scheduled.shift().fn();

// --- connection 2 (fresh backend process, seq restarts at 1) --------------------
const s2 = sockets[1];
assert.ok(s2, 'a new socket was opened');
s2.serverOpen();
assert.equal(statuses.at(-1), 'connected');
assert.deepEqual(s2.sent[0], { action: 'resync', last_seq: 0 }, 'new connection = new sequence context');

s2.serverSend('resync', 1, { state: { cycle_number: 1 } });
assert.equal(count('replaceAll'), 2, "restarted server's resync (seq 1) is applied, not dropped");
s2.serverSend('tick', 2, { summary: {}, sim_time: 't', cycle_number: 2 });
s2.serverSend('tick', 3, { summary: {}, sim_time: 't', cycle_number: 3 });
assert.deepEqual(cycles(), [59, 60, 2, 3], 'UI resumes updating after restart without a refresh');

// --- duplicate protection still holds on the new connection ----------------------
s2.serverSend('tick', 3, { summary: {}, sim_time: 't', cycle_number: 3 });
s2.serverSend('tick', 2, { summary: {}, sim_time: 't', cycle_number: 2 });
assert.deepEqual(cycles(), [59, 60, 2, 3], 'duplicates still dropped after reconnect');

// --- a late frame on the superseded socket is ignored -----------------------------
s1.serverSend('tick', 999, { summary: {}, sim_time: 't', cycle_number: 999 });
assert.deepEqual(cycles(), [59, 60, 2, 3], 'frames from a stale socket are ignored');
s2.serverSend('tick', 4, { summary: {}, sim_time: 't', cycle_number: 4 });
assert.deepEqual(cycles(), [59, 60, 2, 3, 4], 'stale frame did not poison the new sequence');

// --- backoff grows and resets after a successful open ------------------------------
s2.serverDrop();
scheduled.shift().fn();
sockets[2].serverDrop(); // fails before opening
assert.equal(scheduled.at(-1).ms, 2000, 'second consecutive failure backs off to 2s');
scheduled.shift().fn();
sockets[3].serverOpen();
sockets[3].serverDrop();
assert.equal(scheduled.at(-1).ms, 1000, 'backoff resets after a successful connection');

stop();
assert.equal(statuses.at(-1), 'offline');
console.log('✓ WebSocket recovers after a backend restart (new connection = new sequence context)');
console.log('  connection 1 cycles 59,60 → restart → connection 2 cycles 2,3,4 applied');
console.log('  duplicate / out-of-order / stale-socket frames still dropped');
