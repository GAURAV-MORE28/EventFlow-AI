# EventFlow AI — Phase 1 Validation Report
## Numerical Core Correctness

| | |
|---|---|
| Date | 2026-09-26 |
| Branch / base | `Yash` @ `bab9bf5` + uncommitted Phase 0 and Phase 1 work |
| Status | **COMPLETE.** Every acceptance item below was executed and passed. |
| Scope | 1A Digital Twin · 1B What-If + counterfactual · 1C intervention physics · 1D equilibrium · 1E risk. **No crowd-flow model, no GNN redesign** (see "Crowd-flow limitation"). |
| Isolation | Every experiment and test used scratch SQLite DBs (`DATABASE_URL=sqlite:////private/tmp/…`). The user's own `run_demo.py` (PID 20730, real DB) and `vite` (PID 20769) were left running untouched. Every process this phase started was stopped by its exact PID. |
| "Before" values | Measured by re-running the pre-Phase-1 code (`git archive HEAD` extracted to scratch, or `git show HEAD:…` loaded in place of a module) on the same machine and seed. |

Method for every change: reproduce → root-cause experiment → smallest change → focused test → **mutation check** (the new tests re-run against the OLD module must fail) → full regression.

---

## Phase 0 reconciliation (before any Phase 1 code)
- **Backend:** `pytest` 70/70, which includes all 38 Phase 0 tests (speed validation, interruptible loop, run-aware Commander cache, pause/play/step/next_decision, auto-pause, reset/resync, WS `demo_status`).
- **Frontend:** `npm test` passed, including the WS reconnect and store timeline scripts.
- **Demo backend:** `run_demo.py` still has `reload=False`.
- **Result:** no regression found, and Phase 0 code was left unchanged. At the end, the observer flow was re-verified in a real browser (see "Frontend / demo validation").

---

## 1A — Digital Twin

**Issue (audit P0-01).** Unobserved entities were pinned at 0 or 2.0 utilisation, flows exploded, and spread collapsed while the estimate was wrong. RMSE grew about 100×, the "RMSE reduction" compared against a strawman, and `branch()` consumed the live RNG.

**Root cause — verified by experiment, not inferred.** The same filter was re-run with individual mechanisms switched off (seed 42; each cell is cycles 30 / 100 / 300):

| Variant | Unobserved utilisation RMSE | Pinned 0/2 | max \|flow\| / capacity per min |
|---|---|---|---|
| untouched (reference) | 0.485 / 0.674 / 0.993 | 1 / 1 / 8 | 1.06 / 8.36 / 3.42 |
| re-implementation, no changes (sanity) | identical to untouched | identical | identical |
| K localised to own entity | 0.093 / 0.601 / 1.244 | 0 / 1 / 13 | 1.36 / 2.61 / 2.91 |
| … + no flow overwrite | 0.093 / 0.601 / 1.244 | 0 / 1 / 13 | 0.12 / 0.12 / 0.09 |
| no inflation, unlocalised | 0.446 / 0.921 / 0.960 | 1 / 5 / 6 | 0.74 / 1.15 / 3.79 |
| **localised + no flow overwrite + no inflation on uncorrected rows** | **0.084 / 0.347 / 0.487** | **0 / 0 / 0** | **0.08 / 0.07 / 0.07** |

There are three independent mechanisms:
1. **Unlocalised Kalman gain.** Members carry independent noise, so every cross-entity covariance is sampling noise.
2. **Flow overwrite** (`flow = 2·Δ + 0.5·flow`). The next forecast step re-applies the same analysis increment, which is positive feedback.
3. **Multiplicative inflation on rows that no observation ever corrects.** Spread grows exponentially, and the ≥0 clip drags the mean.

A further experiment shared a type-level flow component through the ensemble. It drifted, because unobserved flows absorbed peers' innovations but never received a count correction (`zone_core` reached 1.85 by cycle 30). That is why unobserved entities use an explicit estimator rather than a freely integrating ensemble.

**Implementation** (`ml_reference/twin.py`; state is still `2N = [count; flow]`, per contract):
- **Observed entities:** a stochastic EnKF localised to the entity itself. No flow overwrite. Inflation only on observed rows. Observation error is 1% relative (floor `obs_noise_var`).
- **Unobserved entities:** a deterministic **peer-trend** estimate, `u(t) = u(t0) + median(observed same-type peers' change)`. Spread = 1.5 × peer dispersion (floor 0.03), realised as fixed per-member offsets. With no observed peer, the entity persists with spread 0.08.
- **Known control inputs:** approved transfers (`set_known_transfers`) adjust unobserved sources and destinations by the people actually moved.
- **Guards:** non-finite members are replaced from the last good state; counts ∈ [0, 2 × capacity]; flows within ±0.25 capacity/min; spread cap 0.30.
- **Branching:** `branch()` uses a private RNG seeded from (seed, branch number), and demand multipliers apply once.
- **Metrics:** measured `ensemble_coverage` (additive `TwinFidelity` field) replaces `/metrics`'s formula `clamp(0.85 + spread, 0.80, 0.95)`. The drift baseline is the same bounded model run open-loop.

**Calibration.** Count process noise σ was set to 0.012 on seed 42 and checked on held-out seeds 7 and 123.

| Configuration | Coverage observed / unobserved (seed 42 · 7 · 123) | Observed RMSE |
|---|---|---|
| σ 0.001, ×1.0 (the initial rewrite) | 0.66 / 0.72 · 0.62 / 0.83 · 0.63 / 0.91 | 0.0082 |
| **σ 0.012, peer spread ×1.5 (chosen)** | **0.92 / 0.83 · 0.92 / 0.93 · 0.91 / 0.98** | **0.0063** |

Raising σ improved coverage **and** accuracy: the old prior was over-confident, so the filter lagged every ramp. The spread multiplier only widens uncertainty and never moves the mean.

**Before → after** (600-cycle run, seed 42, drift mode from cycle 20):

| Cycle | Unobserved utilisation RMSE | max \|error\| | Pinned 0/2 | max \|flow\|/cap per min | Assimilated RMSE (people) | 90% coverage |
|---|---|---|---|---|---|---|
| 1 | 0.032 → **0.021** | 0.073 → 0.050 | 0 → 0 | 0.27 → 0.006 | 92.7 → 66.8 | — → 0.970 |
| 10 | 0.065 → **0.018** | 0.218 → 0.031 | 0 → 0 | 1.24 → 0.011 | 176 → 60 | — → 0.939 |
| 30 | 0.479 → **0.035** | 1.683 → 0.065 | 1 → 0 | 1.78 → 0.013 | 312 → 191 | — → 0.955 |
| 60 | 0.532 → **0.068** | 1.584 → 0.178 | 2 → 0 | 3.45 → 0.011 | 739 → 490 | — → 0.909 |
| 100 | 0.838 → **0.137** | 1.506 → 0.326 | 8 → 0 | 13.52 → 0.012 | 3,173 → 895 | — → 0.894 |
| 300 | 0.997 → **0.191** | 1.542 → 0.431 | 9 → 0 | 2.51 → 0.014 | 10,194 → 1,190 | — → 0.833 |
| 600 | 0.909 → **0.175** | 1.325 → 0.328 | 11 → 0 | 3.71 → 0.014 | 10,370 → 951 | — → 0.879 |

Other checks:
- **Mean spread at cycle 600:** observed 0.0007 → 0.009; unobserved 0.0 → 0.186. Estimated entities now carry about 20× the uncertainty of sensor-backed ones.
- **Drift comparison:** before, the uncorrected copy reached RMSE 1.28e14 with counts of −3.1e13 ("100% improvement"). After, it reaches RMSE 5,687 with a minimum count of 0, for an honest 83.3% improvement.
- **Branch RNG:** `branch()` consumed the live RNG (True → **False**) and never mutated live state.
- **NaN/Inf and negative counts:** none across 600 cycles; guard events 0.

**Tests.** `tests/test_phase1_twin.py` has **19 tests**: checkpoints 1/10/30/100/300/600, NaN/negatives, flow bound, pinning, RMSE bound, spread and coverage, honest drift, same-seed repeatability, and branch isolation (state and RNG, demand applied once). **Mutation check:** against the old twin, **12 of 19 fail**; the 7 that pass cover properties the old twin already had.

---

## 1B — What-If + counterfactual

**Issue (audit P0-02/P0-04).**
- **What-if baselines:** 2,048–8,055% peaks; a Gate 3 closure gave 96,405%.
- **Scenarios with no effect:** Blue Line −15% and hotel shortage produced about 0% change.
- **Unknown entities:** `gate_99` was accepted.
- **Counterfactual:** clamped to −100% (do-nothing `metro_b` at 915%).
- **Metrics:** `/metrics` showed an 84.1% "peak reduction" after a 4% notify.

**Root cause.**
- **Wrong model:** what-if and the counterfactual used `twin.branch()`, a different model whose flows had exploded (1A), with demand multipliers compounded every step. That branch also consumed the live RNG.
- **Capacity semantics inverted:** in the generator, count = util × *reduced* capacity, so a capacity cut hid people.
- **Unmatched comparisons:** settlement compared against approval-time utilisation instead of a matched world.

**Implementation.**
- **Generator (`generator.py`):**
  - Demand is relative to nominal capacity. People present = demand × nominal capacity; utilisation = people / effective capacity.
  - New time-stamped, removable **effects** (conserved transfers).
  - Pure `evaluate(t, exclude=, only=)` and a cheap `clone()`.
  - Gate closure and transport outage = capacity cut to 1% plus a conserved transfer of all people to open siblings or substitutes.
  - With no effects or injects the generator is **bit-identical** to the old one: 600 cycles × 66 entities, max difference 0.0.
- **What-if (`simulation.py`):** baseline clone vs scenario clone taken at the same instant, the scenario applied once "now", both evaluated every 30 s over the same horizon. Scenarios are validated synchronously: unknown or mismatched entity, delta ≤ −100 or > 500, bad intensity → `400 INVALID_SCENARIO`. Definitions: peak over all entities and steps; critical count = entities reaching ≥ 0.9; load variance = zone variance at the horizon end.
- **Settlement (`engine._settle_executing_interventions`):**
  - actual = the live world; counterfactual = the live world with this intervention's effects excluded, **at the same instant**.
  - `realised = (cf − actual)/cf`, `counterfactual_relief_pct = (u_approval − cf)/u_approval`, `regret = predicted − realised`, all computed over mean **source** utilisation.
  - **No clamp**; values are null when a denominator is about 0.
  - `/metrics` peak and variance reductions = mean over matched settlements.

**Before → after (what-if at cycle 60, 1800 s horizon):**

| Scenario | Before: baseline peak → scenario peak | After: what the scenario's own mechanism does |
|---|---|---|
| baseline peak | 20.48 (2,048%), 31 critical | **1.24** (123.7%) |
| attendance +20% / −20% | 20.48 → 43.27 / 9.37 | median entity ratio **1.20 / 0.80**, constant across the horizon (applied once) |
| Blue Line −15% capacity | −0.0% | `line_blue` utilisation × **1/0.85** exactly |
| `road_4` / `parking_p1` −50% capacity | 0.0% / 0.0% | × **2.0** exactly |
| heavy rain | +26.3% | roads/parking/gates median × **1.22**; zones unchanged |
| Gate 3 closure | 964.05 (96,405%) | `gate_3` → **0**; other gates gain exactly its people (**conserved**) |
| `metro_b` outage | 372.6 | `metro_b` → 0; every substitute gains |
| hotel shortage | 0.0% | hotels × **1.15**; others unchanged |
| concurrent event 25% | +152.8% | median × **1.25** |
| `gate_99` | 202, silent 0% | **400 INVALID_SCENARIO** |

**Counterfactual — exact A/B.** An approved intervention's counterfactual source utilisation **equals** the value from an independent engine that never approved it, at the same instant. `realised`, `counterfactual_relief_pct` and `regret` all match their definitions exactly, and none is ±100.

**Isolation and reproducibility.** Running what-if A then B, or B then A, leaves the live trajectory, entity states and twin ensemble identical to a run with no what-ifs. Before, one what-if perturbed live unobserved states from the next cycle.

**Tests.** `tests/test_phase1_whatif.py` has **20 tests**. `tests/test_phase1_api.py` checks over HTTP that invalid scenarios return 400 and that `_trajectories` never reaches the wire.

---

## 1C — Intervention physics

**Mandatory investigation — "Gate 2 + Gate 3 → Gate 1"**, reproduced on the pre-Phase-1 code:

- **Payload:** `gate_redistribution`, `target_entity_ids: [gate_2, gate_3, gate_1]`, relief 16.5%, "Move 20% of ticket-scan share from Gate 2, Gate 3 to Gate 1." Nothing in it says which target is a source.
- **Before approval (cycle 180):** Gate 2 displayed **1.890 (twin estimate) while truth was 0.458**. Gate 3 was 0.873 (observed) and Gate 1 0.550 (observed).
- **After approval (+10 cycles, do-nothing → approved):**
  - Gate 1 truth 0.556 → **0.465**: the receiver *fell*.
  - Gate 3 0.859 → 0.717.
  - Gate 2 truth 0.474 → 0.397, while its display stayed at **2.000**.

The trace separates three causes:

| Observation | Cause | Belongs to |
|---|---|---|
| Gate 2 "≈ 2.0, unchanged" | Twin artefact on an unobserved entity | **Bad numerical state** — fixed in 1A |
| Gate 2 chosen as a source | Template selected utilisation > 0.6 from the artefact (true value 0.458) | **Wrong target resolution** caused by 1A |
| Gate 1 decreased | `apply_relief` cut demand on **every** target, the receiver included | **Bad intervention semantics** — fixed here |
| Gate 1's baseline keeps moving independently | Independent scripted curve | Crowd-flow phase (documented, not faked) |

**Implementation.**
- **Optimiser:** emits `effect_model` + `action_effects[{source, destination, planned_fraction, ramp_sec, duration_sec}]`. The planned fraction comes from the *displayed* (rounded) relief.
- **Engine:** `engine.execute_intervention` turns each effect into a conserved generator transfer of `planned_fraction × response` of the source's people, ramping in over `ramp_sec`.
- **Operational actions:** `notify_only` and `emergency_corridor` are `none`, with no crowd effect. `stagger_entry` is a deferral for 720 s.
- **Nudges:** issued only when a destination exists.

**Reroute A/B** (`metro_b` → `metro_c`, reject vs approve, same seed, response 0.2208):

| After approval | `metro_b` | `metro_c` | people moved / received |
|---|---|---|---|
| **before Phase 1** (+1 cycle) | 0.901 → 0.582 | **0.479 → 0.306 (lost 36%)** | — |
| +1 cycle | 0.912 → 0.908 | 0.480 → **0.485** | 16.6 / 16.6 |
| +5 | 0.941 → 0.921 | 0.516 → **0.540** | 85.6 / 85.6 |
| +18 (ramp complete, 540 s) | 1.029 → 0.949 | 0.566 → **0.659** | 337.0 / 337.0 |
| +30 | 1.096 → 1.011 | 0.601 → **0.701** | 358.8 / 358.8 |

Every step: source ↓, destination ↑, conserved to 1e-9. The moved share follows `full × min(1, k/ramp)` exactly. Entities not involved are unchanged.

**Gate regression** (scratch world: Gate 2 genuinely at about 2.05, via capacity cut to 23%; Gate 3 at 0.6–1.0; Gate 1 below 0.6):
- **Real proposal:** `gate_redistribution` with effects {`gate_2` → `gate_1`, `gate_3` → `gate_1`}.
- **After approval vs control:** Gate 2 people ↓, Gate 3 ↓, **Gate 1 ↑**. People received = people given (rel 1e-9). Every other entity is identical.
- **Display:** follows too, including **unobserved** Gate 2 (via known-control input). Gate 2 sits above the store's 0–2 display bound in both worlds, so displayed *people* are compared.
- **Audit:** exactly one new `approve` row.

**Notify-only A/B:**
- **Before:** approving cut real demand 4% (`metro_b` 0.912 → 0.875).
- **After:** generator effects are `[]`; the ground truth **and** the displayed state are identical to reject for 20 cycles.

**Other types:** `deploy_shuttle`, `gate_redistribution`, `parking_redistribution`, `zone_incentive` and `accommodation_rebalance` all pass the same source ↓ / destination ↑ / conserved contract. `stagger_entry` holds people back during its window and returns to baseline after it.

**Tests.** `tests/test_phase1_interventions.py` has **17 tests**. Approve, reject, expire, duplicate-approve and audit tests from Phase 0 and the contract suite still pass.

---

## 1D — Equilibrium / stability

**Issue (audit P0-05).**
- **Relief artefact:** relief ≥ 8% never converged, so it was UNSTABLE; 95% of live and 99% of real-DB certificates were UNSTABLE.
- **Poisoning:** one unrelated overloaded zone made any action UNSTABLE.
- **Fabricated breach time:** "within N minutes" = 26 − overshoot × 100.
- **No leader:** `solve_leader()` returned `[]`.
- **Accuracy:** certificate accuracy was 0/38 against the exploding twin branch.

**Root cause.**
- **Re-applied relief:** the follower loop removed the relief from the targets **again every iteration**, and the convergence test required the drained load to fall below tolerance. So convergence depended on relief size, not on any dynamics.
- **Wrong surface:** it included every zone.

**Implementation** (`ml_reference/equilibrium.py`; `row_verdict` / `derive_verdict` unchanged, and the contract test pinning them still passes):
- **Fixed point:** `m = F(m)` solved by damped iteration. m = the share of the offered move taken; F = segment compliance from the congestion-cost gap **after** moving m.
- **Stopping rules:** convergence `|Δm| < 0.005` within 40 iterations; oscillation per 03 §5.2; divergence = non-finite.
- **Scope:** the action's sources and destinations only, rolled forward 1800 s through `engine.world_rollout`, the **same event-world model** (generator clone), against a matched do-nothing rollout.
- **Row `max_utilisation`:** the peak of entities the action **adds load to**, i.e. 03's "new critical entity created".
- **Breach minutes:** read from the rollout; otherwise omitted.
- **`response_rate` (m\* at 0.6):** added to the certificate, and execution uses it.
- **`solve_leader`:** a deterministic grid over offer scale, returned best-first.
- **Certificate accuracy:** the approval-time projection vs the realised value at settlement.

**Relief sweep** (`metro_b` 0.95 → `metro_c` 0.25):

| Relief | Before: verdict / converged / iterations | After: verdict / converged / iterations |
|---|---|---|
| 2% | CONDITIONAL / yes / 1 | STABLE / yes / 7 |
| 4% | CONDITIONAL / yes / 26 | STABLE / yes / 7 |
| 8% | **UNSTABLE / no / 40** | STABLE / yes / 7 |
| 12% | UNSTABLE / no / 40 | STABLE / yes / 7 |
| 20% | UNSTABLE / no / 40 | STABLE / yes / 6 |
| 35% | UNSTABLE / no / 40 | STABLE / yes / 6 |
| 35% into `metro_c` at 0.97 | — | **UNSTABLE**, converged: "At 40% compliance, Metro C Station exceeds capacity." |

- **Unrelated overload:** `zone_core` at 1.6 and `gate_5` at 1.4 elsewhere now produce an **identical** certificate. Before, `zone_core` at 1.05 made a 4% notify UNSTABLE with "Core Zone exceeds capacity within 21 minutes".
- **Oscillation:** an undamped near-step best response is detected as `oscillation_risk` and marked UNSTABLE.
- **Divergence:** NaN elasticities give `converged = False` and UNSTABLE.
- **Certificate peak = independent world rollout:** exact to 1e-4.
- **Reason precision:** four decimals. At three, a genuine 0.0016 → 0.0025 variance rise printed as "0.002 to 0.002".
- **Mutation check:** against the old solver, 4 of 4 targeted tests **fail**.
- **Tests:** `tests/test_phase1_equilibrium.py` has **10 tests**.

---

## 1E — Risk

**Issue (audit P0-06).** 1.00 → 50 MODERATE, 1.05 → MODERATE, 1.20 → 60 MODERATE, 1.50 → 75 HIGH. NaN utilisation clamped silently to 100 / CRITICAL.

**Root cause.** The 03 §7.1 formula caps the utilisation term at 50 points, and `clamp()` swallows NaN.

**Implementation.** The formula is unchanged, with one invariant added: `util ≥ 1.0 ⇒ score = max(formula, high + 1)`, so the band is CRITICAL and agrees with the score. Non-finite inputs are treated as missing and logged. The fallback path applies the same rule.

| util (flat forecast) | 0.50 | 0.60 | 0.80 | 0.90 | 0.99 | 1.00 | 1.05 | 1.20 | 1.50 |
|---|---|---|---|---|---|---|---|---|---|
| before | 25 low | 30 low | 40 mod | 45 mod | 50 mod | **50 mod** | **52 mod** | **60 mod** | **75 high** |
| after | 25 low | 30 low | 40 mod | 45 mod | 50 mod | **81 crit** | **81 crit** | **81 crit** | **81 crit** |

- **Grid:** score and band agree over a 0–2.0 × forecast × exposure grid.
- **Live run:** over 300 cycles, 0 over-capacity states below CRITICAL.
- **Mutation check:** 22 of the invariant tests fail against the old scorer; the below-capacity ones pass, as they should.
- **Tests:** `tests/test_phase1_risk.py` has **48 tests**.
- **Cascade:** not redesigned. The cascade-exposure input to risk is unchanged.

---

## Integrated chain (600 cycles, seed 42, `p1_integrated.py`)

The run goes simulation → twin → risk → what-if at 1/10/30/60/100/300/600 → top-ranked transfer approved every 60 cycles → equilibrium → execution → matched counterfactual → settlement / regret.

| Check | Result |
|---|---|
| Invariant violations (twin finite and non-negative, over-capacity critical, score/band agree, what-if ≤ 3.0 and never mutating live) | **0** across 600 cycles |
| What-if peaks at every checkpoint | 0.99–1.76 (rain, gate closure, attendance) |
| Approvals executed | 10: shuttle, reroute, 2 × gate redistribution, 6 × parking |
| Regret entries | all finite, **none ±100**, e.g. reroute: predicted 35.3, realised 7.8, counterfactual −11.3, regret 27.5 |
| Certificate meaning | Reroute certified **STABLE**; settled affected peak 1.074 vs do-nothing 1.165. Parking certified **UNSTABLE**; when approved anyway, affected peak 1.005 vs 0.992 (**made worse**) — the certificate was right. |
| Realised = offered × response | reroute: 35.3% × 0.2208 = 7.8% source relief |
| Certificate accuracy samples | 10/10 within 15%, as expected: same model, so this is a consistency check, not field accuracy |
| Twin guard events | 0 |

## Performance (same machine, 600 cycles)

| Metric | Before | After |
|---|---|---|
| Cycle ms p50 / p95 / max / mean | 28.7 / 35.8 / 81.8 / 26.6 | **27.9 / 33.6 / 82.8 / 26.1** |
| What-if (1800 s horizon, two clones) | n/a (twin branch) | p50 13.0 ms, max 20.5 ms |
| Certification (4 rollouts) | — | p50 1.0 ms, max 2.1 ms (133 certifications) |

No unbounded nested simulation: rollouts evaluate only the affected entities (`only=`).

## API / contract regression
- **Schemas:** re-exported (28). Changes are **additive only**, checked mechanically: nothing removed, nothing newly required.
  - New optional fields: `Intervention.effect_model`, `Intervention.action_effects[ActionEffect]`, `Certificate.response_rate`, `TwinFidelity.ensemble_coverage`.
  - `RegretEntry.realised_relief_pct`, `counterfactual_relief_pct` and `regret` became nullable.
  - Six other files differ only by the pre-existing `additionalProperties: true` drift already recorded in the audit.
- **Mocks:** regenerated (`--cycles 100`), 17/17 valid, and sane (0 pinned, 0 over-capacity below critical, what-if peaks 1.32/1.41 vs the old 72.5). The mock pair is now `reroute` STABLE vs `deploy_shuttle` UNSTABLE ("variance 0.0016 → 0.0025"). Mock nudges now "Switch to Metro C Station".
- **`tests/test_phase1_api.py`** (8 tests, HTTP): invalid scenarios → 400 `INVALID_SCENARIO` with the error envelope; what-if round trip is schema-valid with no internal keys; interventions and certificates carry the new fields; twin and metrics coverage are measured; state units and casing hold.
- **Unknown entity IDs:** rejected.

## Frontend / demo validation (real browser)
Setup: headless Chrome 154 over CDP → `npm run dev:live` code, served by a scratch-config Vite on :5180 proxying to an isolated `run_demo.py --port 8010` (scratch DB). The user's :8000 / :5173 processes were not used.

**Phase 0 still works: 19/19 observer checks pass.** Render, UI reset, speed 10× and 0.5× with sim/real pace labels, pause freeze, play, step ×2 (exactly 2 cycles), auto-pause on a real proposal plus the decision banner, clock frozen while paused, approve & resume, decision status visible, reject & resume, next decision, backend kill + relaunch → UI reconnects and tracks the new process, stale HUD cleared, and no console errors outside the deliberate outage.

**Phase 1 reaches the UI: 9/9.**

| Check | Evidence |
|---|---|
| No 0/200% artefacts among estimated entities (cycle 120) | all 13 in 0.60–0.87 (before: `emergency_core` 200%, `zone_concourse_south` 1.7%) |
| Every over-capacity entity CRITICAL | 7 entities, 102.9–121.2%, all `critical` |
| Pressure Timeline | no row ≥100% below CRITICAL (e.g. 103/107/109/112/113/120/121% = CRITICAL; 91/95/98% remain MODERATE/HIGH by the formula); no 200% hero row |
| What-If panel (Gate 3 closure) | a sane delta (before: "+616.8%") |
| Proposals | carry explicit effects: `metro_b->metro_c`, `gate_3->gate_1, gate_4->gate_1`, stagger `gate_3->null` (deferral) |
| Settlement | matched counterfactual, e.g. `gate_redistribution` predicted 16.5, realised 3.2, counterfactual −10.2, regret 13.3 (no ±100) |
| ActionHUD after a **UI** approval of the reroute | "Executed — 7.8% source relief vs do-nothing" · "source relief +7.8% vs do-nothing · without action source would have moved +15.3% · regret +27.5". No "pp", no "null". |

One HUD check initially failed. That was a harness error: it approved via the API, and the HUD only tracks UI approvals. Re-run with a real click, it passes (above).

**Mock mode:** mocks were regenerated and validate 17/17. `npm test` includes the mock-lifecycle re-arm test. The Phase 0 mock-mode behaviour of the observer bar is unchanged.

## Regression summary

| Suite | Result |
|---|---|
| Backend `pytest tests/` | **192 passed** (32 contract/ML + 38 Phase 0 + 122 Phase 1: twin 19, what-if 20, interventions 17, equilibrium 10, risk 48, API 8) |
| Frontend `npm test` (mocks 17/17 + mock lifecycle + WS reconnect + store timeline) | pass |
| `npm run build` | pass |
| Tests weakened or deleted | **none**. Test-construction bugs in my *new* tests were fixed and noted in the session (wrong timing instant, wrong engine registered for route calls, non-rerun-safe audit count, display clamp). |
| Real demo DB | never opened for writing by any process started in Phase 1 (every backend and test used scratch DBs; logs confirm the paths) |

## Remaining limitations (genuine)
1. **The world model is the generator.** What-if, the counterfactual and certificates use the event-world model the simulation runs on. Results are exact for the simulation and are not forecasts of real crowds, and certificate accuracy ≈ 100% by construction. Say so when presenting.
2. **Unobserved entities** are estimated from type peers (RMSE ≈ 0.18, honest spread ≈ 0.19). Entity-specific events without a sensor or a known control input are invisible.
3. **Demand clip at 1.6× capacity:** late in the run, demand scenarios barely move the global peak.
4. **The optimiser's `estimated_relief_pct` is still a hand-set claim.** Regret now measures it honestly against the matched world, which is typically a large overclaim; e.g. the reroute claims 35.3% but realises 7.8%.
5. **`solve_leader` is not wired into ranking** (the optimiser was not redesigned, per scope).
6. **Pressure-timeline band lag** (noted in Phase 0) is not addressed here.
7. **HUD per-entity rows** are approval-time → now observations, so they include the event's own growth. For example, Metro B shows "98% → 106% ▲8pp" while the matched effect is 7.8% relief versus do-nothing. The matched number is the settled line. A HUD redesign is out of scope.
8. **Optimiser titles still carry their own claims.** "Redirect 2,507 attendees" is the optimiser's overflow estimate; the executed transfer moved about 359 at the equilibrium response. This is the same class as #4.

## Crowd-flow limitation (explicitly NOT solved in Phase 1)
Entities still follow **independent scripted curves**, and people do not flow along the graph's edges. Phase 1 added only explicit, conserved transfers between entities, driven by interventions, closures and outages. So a receiver's own baseline curve still moves independently of the people it receives (the Gate 1 observation). The deferral model is a window rather than a re-timed arrival profile, and there is no onward overflow routing. All of this belongs to **Phase 2 — crowd-flow model** (see `plot.md`).

## Next phase
Phase 2: the crowd-flow model. It should make the twin, not the ground truth, the projection model, at which point certificate accuracy becomes a genuine out-of-sample measure. Phase 3: the cascade / GNN redesign.
