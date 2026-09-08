/**
 * Replay-loop lifecycle for the mock driver (lib/mocks.js).
 *
 * The mock driver replays a 90-frame recording. One full pass of the recording
 * is one "simulation iteration": the state / pressure / twin streams already
 * loop with `cycle % length`, so the calm→critical entity ramp restarts every
 * 90 ticks.
 *
 * THE BUG this module exists to prevent: the cascade and intervention "arm"
 * points were keyed on the driver's *monotonic* tick — `if (cycle === 8)` /
 * `if (cycle === 10)` — so they fired exactly once, ever. After the first pass,
 * every new iteration replayed the state ramp but never re-armed the cascade
 * overlay or re-populated the intervention queue. An intervention approved in
 * iteration 1 was left `executing` (mock mode has no settlement engine),
 * permanently occupying the queue, and no fresh recommendations ever appeared.
 *
 * `mockFramePlan` keys the arm points on the position WITHIN the current pass
 * (`frameIndex`), so each fires once per iteration, and marks the boundary
 * (`isNewIteration`) where the previous iteration's ACTIVE state — queue,
 * cascade overlay, tracked actions — must be dropped so it cannot suppress the
 * next iteration. Historical records (regret) are untouched.
 */

export const MOCK_CASCADE_ARM_FRAME = 8;
export const MOCK_INTERVENTIONS_ARM_FRAME = 10;

/**
 * @param {number} cycle              monotonic driver tick (never wraps)
 * @param {number} framesPerIteration length of the recording (one iteration)
 */
export function mockFramePlan(cycle, framesPerIteration) {
  const frameIndex = ((cycle % framesPerIteration) + framesPerIteration) % framesPerIteration;
  return {
    frameIndex,
    iteration: Math.floor(cycle / framesPerIteration),
    // cycle 0 seeds iteration 0 but is not a *transition* into a new one.
    isNewIteration: cycle > 0 && frameIndex === 0,
    armCascade: frameIndex === MOCK_CASCADE_ARM_FRAME,
    armInterventions: frameIndex === MOCK_INTERVENTIONS_ARM_FRAME,
  };
}

/**
 * One tick of the replay. All effects go through the same store actions the
 * WebSocket handlers use.
 *
 * @param store  the zustand store (or a compatible fake, for the regression test)
 * @param ctx    mutable driver state: { cycle, working, fixtures }
 * @returns the frame plan for this tick
 */
export function advanceMockDriver(store, ctx) {
  const { fixtures } = ctx;
  const frames = fixtures.stateSequence.length;
  const plan = mockFramePlan(ctx.cycle, frames);

  if (plan.frameIndex === 0) {
    // Fresh fixture copies for this iteration, so an approve() from the
    // previous pass does not carry its `executing` status into this one.
    ctx.working = {
      interventions: structuredClone(fixtures.interventionsData),
      nudges: structuredClone(fixtures.nudgeData),
    };
    if (plan.isNewIteration) {
      // A new pass is a fresh run. resetSimState clears ALL transient state
      // (interventions, cascades, entity selection, tracked actions, what-if,
      // load-variance history, anomalies) so the map/queue/overlays return to
      // initial conditions. Regret and commander history are intentionally
      // preserved — they accumulate across passes.
      store.resetSimState?.();
    }
  }

  const frame = fixtures.stateSequence[plan.frameIndex];
  const pressure = fixtures.pressureSequence[ctx.cycle % fixtures.pressureSequence.length];
  const twin = fixtures.twinSequence[ctx.cycle % fixtures.twinSequence.length];

  store.setSummary(frame.summary, frame.sim_time, frame.cycle_number);
  store.mergeEntities(frame.entities);
  store.setPressureTimeline(pressure.items, pressure.active_source);
  if (twin) store.setTwinFidelity(twin);

  // The cascade and the intervention pair arrive partway into each iteration,
  // so every pass has a genuinely calm opening before the alarm (02 §5.4).
  if (plan.armCascade) {
    store.setCascades(fixtures.cascadesActive.cascades);
    store.setCascade(fixtures.cascadeMetroB);
  }
  if (plan.armInterventions) {
    store.setInterventions(ctx.working.interventions.interventions);
  }

  ctx.cycle += 1;
  return plan;
}
