#!/usr/bin/env node
/**
 * Regression test for the mock-driver iteration bug.
 *
 *   BUG: after the 90-frame recording wraps to a new iteration, no new
 *        intervention recommendations appear, and an intervention approved in
 *        the previous iteration keeps occupying the queue.
 *
 * This drives the real per-tick lifecycle (`advanceMockDriver`) against a fake
 * store across three iterations, with an approval in iteration 1, and asserts:
 *
 *   1. iteration 1 populates the queue and an intervention can be approved;
 *   2. the iteration boundary clears ACTIVE state (queue, cascade, tracked
 *      actions) — but the test also proves it re-populates, so this is not a
 *      "the list is empty" check;
 *   3. iterations 2 and 3 generate fresh recommendations again;
 *   4. the iteration-1 approval (status `executing`) does NOT leak — every
 *      iteration-2/3 recommendation is a fresh `proposed`.
 *
 *   npm test   (runs after validate:mocks)
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  advanceMockDriver,
  mockFramePlan,
  MOCK_INTERVENTIONS_ARM_FRAME,
} from '../src/lib/mockLifecycle.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MOCKS = path.resolve(__dirname, '../src/mocks');
const load = (f) => JSON.parse(readFileSync(path.join(MOCKS, f), 'utf-8'));

const fixtures = {
  stateSequence: load('state_sequence.json'),
  pressureSequence: load('pressure_timeline.json'),
  twinSequence: load('twin_fidelity_sequence.json'),
  cascadesActive: load('cascades_active.json'),
  cascadeMetroB: load('cascade_metro_b.json'),
  interventionsData: load('interventions.json'),
  nudgeData: load('attendee_nudges.json'),
};
const FRAMES = fixtures.stateSequence.length;

// --- part 1: the arm points recur every iteration -----------------------------
for (const iteration of [0, 1, 2, 3]) {
  const cycle = iteration * FRAMES + MOCK_INTERVENTIONS_ARM_FRAME;
  const plan = mockFramePlan(cycle, FRAMES);
  assert.equal(plan.armInterventions, true, `interventions must re-arm in iteration ${iteration}`);
  assert.equal(plan.iteration, iteration);
}
assert.equal(mockFramePlan(0, FRAMES).isNewIteration, false, 'cycle 0 is the first iteration, not a transition');
assert.equal(mockFramePlan(FRAMES, FRAMES).isNewIteration, true, 'wrap at cycle=FRAMES is a new iteration');
assert.equal(mockFramePlan(FRAMES * 2, FRAMES).isNewIteration, true, 'wrap at cycle=2*FRAMES is a new iteration');

// --- part 2: full driver lifecycle across three iterations --------------------
function makeFakeStore() {
  return {
    interventions: [],
    cascades: {},
    activeCascadeRootId: null,
    // pretend TASK 2 left an un-settled tracked action behind (mock never settles)
    trackedActions: [{ id: 'act_stale_from_iter1', kind: 'executed' }],
    setInterventions(v) {
      this.interventions = v || [];
    },
    setCascades(v) {
      this.cascades = Object.fromEntries((v || []).map((c) => [c.root_entity_id, c]));
    },
    setCascade(c) {
      this.cascades[c.root_entity_id] = c;
    },
    setActiveCascadeRoot(id) {
      this.activeCascadeRootId = id;
    },
    clearTrackedActions() {
      this.trackedActions = [];
    },
    resetSimState() {
      this.interventions = [];
      this.cascades = {};
      this.activeCascadeRootId = null;
      this.trackedActions = [];
    },
    setMockMode() {},
    setWsStatus() {},
    setSummary() {},
    mergeEntities() {},
    setPressureTimeline() {},
    setTwinFidelity() {},
  };
}

const store = makeFakeStore();
const ctx = { cycle: 0, working: null, fixtures };
const recsPerIteration = {};

for (let i = 0; i < FRAMES * 3 + 15; i += 1) {
  const plan = advanceMockDriver(store, ctx);

  if (plan.isNewIteration) {
    assert.equal(store.interventions.length, 0, `iteration ${plan.iteration}: queue cleared at boundary`);
    assert.equal(Object.keys(store.cascades).length, 0, `iteration ${plan.iteration}: cascade cleared at boundary`);
    assert.equal(store.activeCascadeRootId, null, `iteration ${plan.iteration}: cascade selection cleared`);
    assert.equal(store.trackedActions.length, 0, `iteration ${plan.iteration}: stale tracked action cleared`);
  }

  if (plan.armInterventions && recsPerIteration[plan.iteration] === undefined) {
    assert.ok(store.interventions.length > 0, `iteration ${plan.iteration}: recommendations must appear`);
    recsPerIteration[plan.iteration] = store.interventions.map((x) => x.intervention_id);

    if (plan.iteration === 0) {
      // operator approves the first recommendation
      ctx.working.interventions.interventions[0].status = 'executing';
      store.interventions = store.interventions.map((x, idx) =>
        idx === 0 ? { ...x, status: 'executing' } : x,
      );
    } else {
      assert.ok(
        store.interventions.every((x) => x.status === 'proposed'),
        `iteration ${plan.iteration}: recommendations must be fresh proposals, ` +
          'not the executing intervention carried over from iteration 1',
      );
    }
  }
}

assert.ok(
  recsPerIteration[0] && recsPerIteration[1] && recsPerIteration[2],
  'recommendations appeared in iterations 1, 2 and 3',
);

console.log('✓ mock driver re-arms the intervention queue every iteration');
console.log(`  iter1: [${recsPerIteration[0]}]`);
console.log(`  iter2: [${recsPerIteration[1]}]  (fresh proposals, iteration-1 approval did not block)`);
console.log(`  iter3: [${recsPerIteration[2]}]`);
