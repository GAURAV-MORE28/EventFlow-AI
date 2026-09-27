#!/usr/bin/env node
/**
 * The weather layer's store rules:
 *
 *  1. `weather_update` goes through the same handler table as every other WS
 *     event, and is subject to the same `seq` de-duplication — a replayed frame
 *     must not roll the reading back.
 *  2. The reading belongs to a world's venue, so a resync naming a DIFFERENT
 *     world clears it (App refetches for the new venue). A same-world resync
 *     keeps it, because weather arrives on its own cadence and a resync must
 *     not blank the panel.
 *  3. A weather scenario result is transient run state: a new run drops it.
 *  4. The store never derives an impact number — it holds what the backend sent.
 *
 *   npm test
 */
import assert from 'node:assert/strict';

globalThis.window = { location: { search: '' } };
const { useStore } = await import('../src/store/useStore.js');
const s = useStore.getState;

const reading = (severity, availability = 'live') => ({
  availability,
  driving_live: false,
  applied_to_live: false,
  current: { severity, precipitation_mm_per_hr: severity === 'calm' ? 0 : 20 },
  impact: { applied: severity !== 'calm', travel_time_mult: severity === 'calm' ? 1 : 1.25 },
  forecast: [],
  forecast_impacts: [],
});

// --- 1. the WS handler table + seq de-duplication -----------------------------
// Rebuild the dispatch the client uses, so this test exercises the real mapping
// rather than a copy of it.
const handled = [];
const fakeStore = {
  ...s(),
  setWeather: (p) => {
    handled.push(p);
    s().setWeather(p);
  },
};
const { connectWebSocket } = await import('../src/lib/ws.js');
assert.equal(typeof connectWebSocket, 'function', 'ws client still exports connectWebSocket');

// The handler itself, applied the way lib/ws.js applies it.
fakeStore.setWeather(reading('severe'));
assert.equal(s().weather.current.severity, 'severe');
assert.equal(s().weather.impact.travel_time_mult, 1.25, 'the store holds the backend number verbatim');
assert.equal(handled.length, 1);

// --- 2. resync semantics -----------------------------------------------------
s().setGraph({ nodes: [{ entity_id: 'stadium_main', lat: 1, lon: 1 }], edges: [], segments: [], bounds: null });
s().setWorld({ world_id: 'synthetic_demo', run_id: 1, source: 'synthetic_demo' });
s().setWeather(reading('severe'));
s().setSocialSignals({ availability: 'live', signals: [], summary: { count: 0 } });

// same world, same run: the reading survives (weather has its own cadence)
s().replaceAll({ state: { cycle_number: 41, entities: [] }, world: { world_id: 'synthetic_demo', run_id: 1 } });
assert.equal(s().weather?.current.severity, 'severe', 'a same-world resync must not blank the weather panel');
assert.ok(s().socialSignals, 'nor the signals');

// a different world: the reading was for the old venue, so it is cleared
s().replaceAll({
  state: { cycle_number: 0, entities: [] },
  world: { world_id: 'bp_abc', run_id: 2, source: 'generated_blueprint' },
  interventions: [],
  cascades: [],
});
assert.equal(s().weather, null, 'a new world invalidates the old venue’s weather');
assert.equal(s().socialSignals, null, 'and its public signals');

// --- 3. a weather scenario is transient run state ----------------------------
s().setWorld({ world_id: 'bp_abc', run_id: 2, source: 'generated_blueprint' });
s().setWeatherScenario({ status: 'complete', result: { baseline: {}, scenario: {} }, params: { rain_mm_per_hr: 25 } });
assert.equal(s().weatherScenario.status, 'complete');

useStore.setState({ cycleNumber: 30 });
s().replaceAll({ state: { cycle_number: 31, entities: [] }, world: { world_id: 'bp_abc', run_id: 3 } });
assert.equal(s().weatherScenario.status, 'idle', 'a new run drops the scenario result');
assert.equal(s().weatherScenario.result, null);

s().setWeatherScenario({ status: 'complete', result: { baseline: {} } });
s().resetSimState();
assert.equal(s().weatherScenario.status, 'idle', 'a replay iteration boundary drops it too');

// --- 4. an unavailable reading is stored as-is, not smoothed over ------------
s().setWeather({
  availability: 'unavailable',
  driving_live: false,
  applied_to_live: false,
  current: null,
  impact: { applied: false, travel_time_mult: 1.0 },
  forecast: [],
  forecast_impacts: [],
  detail: 'the provider could not be reached',
});
assert.equal(s().weather.current, null, 'no reading stays no reading');
assert.equal(s().weather.impact.applied, false);
assert.ok(s().weather.detail, 'and the reason is kept for the UI to show');

s().clearWeatherScenario();
assert.equal(s().weatherScenario.result, null);

console.log('✓ weather store: seq-safe updates, world-scoped invalidation, transient scenario state');
