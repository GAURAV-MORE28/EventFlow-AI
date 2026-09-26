#!/usr/bin/env node
/**
 * A resync that names a different world (blueprint activation) must not leave
 * the old graph or the old run's UI state behind: the store clears the graph
 * (App refetches /graph for the new world) and drops selection, overlays, HUD.
 * A resync of the same world keeps the graph.
 *
 *   npm test
 */
import assert from 'node:assert/strict';

globalThis.window = { location: { search: '' } };
const { useStore } = await import('../src/store/useStore.js');
const s = useStore.getState;

s().setGraph({ nodes: [{ entity_id: 'stadium_main', lat: 1, lon: 1 }], edges: [], segments: [], bounds: null });
s().setWorld({ world_id: 'synthetic_demo', run_id: 1, source: 'synthetic_demo' });
useStore.setState({ selectedEntityId: 'stadium_main', whatIfOverlay: { result: {} }, trackedActions: [{ id: 'x' }], cycleNumber: 40 });

// same world, same run: graph and selection survive
s().replaceAll({ state: { cycle_number: 41, entities: [] }, world: { world_id: 'synthetic_demo', run_id: 1 } });
assert.equal(s().graph.nodes.length, 1);
assert.equal(s().selectedEntityId, 'stadium_main');

// a new world: graph cleared, run state dropped, world recorded
s().replaceAll({
  state: { cycle_number: 0, entities: [{ entity_id: 'n_0123456789', utilisation: 0.1 }] },
  world: { world_id: 'bp_abc', run_id: 2, source: 'generated_blueprint' },
  interventions: [], cascades: [],
});
assert.equal(s().graph.nodes.length, 0, 'old graph cleared on world change');
assert.deepEqual(Object.keys(s().nodesById), []);
assert.equal(s().world.world_id, 'bp_abc');
assert.equal(s().selectedEntityId, null);
assert.equal(s().whatIfOverlay, null);
assert.deepEqual(s().trackedActions, []);
assert.deepEqual(Object.keys(s().entities), ['n_0123456789']);

// same world, new run (reset): run state dropped, graph kept
s().setGraph({ nodes: [{ entity_id: 'n_0123456789', lat: 1, lon: 1 }], edges: [], segments: [], bounds: null });
useStore.setState({ selectedEntityId: 'n_0123456789', cycleNumber: 30 });
s().replaceAll({ state: { cycle_number: 31, entities: [] }, world: { world_id: 'bp_abc', run_id: 3 } });
assert.equal(s().graph.nodes.length, 1);
assert.equal(s().selectedEntityId, null, 'new run clears selection');
console.log('✓ world change clears the old graph and run state; same-world resync keeps the graph');
