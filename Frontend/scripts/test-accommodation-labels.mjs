#!/usr/bin/env node
/** Hotel capacity labels: mapped values plain, estimates marked, never "actual". */
import assert from 'node:assert/strict';
import { capacitySourceLabel, confidenceLabel, roomCapacityLabel, statusLabel } from '../src/lib/accommodation.js';

assert.equal(roomCapacityLabel(235, 'high'), '235 rooms');
assert.equal(roomCapacityLabel(140, 'low'), '~140 rooms');
assert.equal(roomCapacityLabel(150, 'medium'), '~150 rooms');
assert.equal(roomCapacityLabel(null, 'high'), '—');
assert.equal(capacitySourceLabel('osm_rooms'), 'OSM rooms');
assert.equal(capacitySourceLabel('derived_estimate'), 'Derived estimate');
assert.equal(capacitySourceLabel('derived_from_osm_beds'), 'Derived from OSM beds');
for (const s of ['osm_rooms', 'osm_capacity_rooms', 'derived_from_osm_beds', 'derived_estimate', 'catalogue', 'x_y']) {
  assert.ok(!/actual/i.test(capacitySourceLabel(s)), `no "actual" in ${s}`);
}
assert.equal(confidenceLabel('low'), 'Low');
assert.equal(statusLabel('limited'), 'TIGHT');
assert.equal(statusLabel('saturated'), 'SATURATED');
console.log('✓ accommodation labels: mapped vs estimated capacity, TIGHT/SATURATED, never "actual"');
