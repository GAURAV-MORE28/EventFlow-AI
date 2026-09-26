#!/usr/bin/env node
/**
 * Regression test: a resync from a different simulation timeline (demo reset,
 * backend restart, seek) must drop per-timeline UI state, and a resync from the
 * SAME timeline (ordinary reconnect) must keep it.
 *
 * Drives the real zustand store (src/store/useStore.js) — no mocks.
 *
 *   npm test
 */
import assert from 'node:assert/strict';

const { useStore } = await import('../src/store/useStore.js');
const S = () => useStore.getState();

function resync({ cycle, run }) {
  S().replaceAll({
    state: { cycle_number: cycle, sim_time: 't', entities: [], summary: S().summary },
    interventions: [],
    cascades: [],
    demo: { status: 'playing', run_id: run, cycle_number: cycle },
  });
}

function seedActionState() {
  useStore.setState({
    trackedActions: [{ id: 'act_x', interventionId: 'int_x', kind: 'executed', phase: 'live' }],
    whatIfOverlay: { label: 'Heavy Rain' },
    activeCascadeRootId: 'metro_b',
    selectedEntityId: 'metro_b',
  });
}

// Baseline: run 1 at cycle 50 with an executed action being tracked.
resync({ cycle: 50, run: 1 });
seedActionState();

// 1. Same timeline (plain reconnect, cycle moved forward): keep everything.
resync({ cycle: 52, run: 1 });
assert.equal(S().trackedActions.length, 1, 'reconnect within the same run keeps tracked actions');
assert.equal(S().selectedEntityId, 'metro_b');
assert.equal(S().demo.run_id, 1);

// 2. Demo reset -> run_id changes: per-timeline state is dropped.
resync({ cycle: 0, run: 2 });
assert.deepEqual(S().trackedActions, [], 'reset clears tracked actions');
assert.equal(S().whatIfOverlay, null);
assert.equal(S().activeCascadeRootId, null);
assert.equal(S().selectedEntityId, null);
assert.equal(S().cycleNumber, 0);
assert.equal(S().demo.run_id, 2);

// 3. Backend restart -> run_id restarts at 1 but the cycle goes backwards.
resync({ cycle: 40, run: 2 });
seedActionState();
resync({ cycle: 3, run: 1 });
assert.deepEqual(S().trackedActions, [], 'restart (cycle regression) clears tracked actions');
assert.equal(S().cycleNumber, 3);

// 4. setDemo mirrors the demo_status event verbatim.
S().setDemo({ status: 'paused', pause_reason: { kind: 'operator' } });
assert.equal(S().demo.status, 'paused');

console.log('✓ resync from a new timeline (reset / restart) clears stale per-run UI state');
console.log('  resync from the same timeline (reconnect) keeps it');
