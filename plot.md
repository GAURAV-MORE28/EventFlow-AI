# EventFlow AI — Roadmap & Progress

Living plan for multi-session work. **Read `CLAUDE.md` first**, then this file. Evidence for each
completed phase lives in its validation report, not here.

| Phase | Scope | Status | Evidence |
|---|---|---|---|
| Audit | Full read-only verification of branch `Yash` @ `bab9bf5` | Done (2026-09-26) | `FINAL_AUDIT_REPORT.md` |
| **Phase 0** | Demo safety, recovery & observability: WS restart recovery, speed validation + interruptible loop, run-aware Commander cache, `run_demo.py` (no reload), observer/judge mode (play/pause/step/next decision/speeds/pause-on-proposal/reset), honest labels | **COMPLETE** | `PHASE0_VALIDATION_REPORT.md`, `CLAUDE.md §32` |
| **Phase 1** | Numerical core correctness | **COMPLETE** | `PHASE1_VALIDATION_REPORT.md`, `CLAUDE.md §33` |
| ├ 1A | Digital Twin: localised EnKF + peer-trend for unobserved, bounded flows, measured coverage, honest drift baseline, isolated branch RNG | Complete | report §1A |
| ├ 1B | What-if on clones of the event-world model; matched same-world counterfactual; no clamp; scenario validation | Complete | report §1B |
| ├ 1C | Intervention physics: explicit source → destination transfers, conserved; notify-only has no crowd effect; Gate 2+3 → 1 regression | Complete | report §1C |
| ├ 1D | Equilibrium: genuine fixed point, scoped to affected entities, world-model rollout, derived breach time, leader grid | Complete | report §1D |
| └ 1E | Risk: utilisation ≥ 1.0 ⇒ CRITICAL; finite-input guards | Complete | report §1E |
| Phase 2 | **Crowd-flow model** (next) | Not started | — |
| Phase 3 | Cascade / GNN redesign | Not started | — |
| Later | Venue discovery / geospatial ingestion, attendee experience redesign, accommodation expansion, schedule-change system | Not started | — |

## Next: Phase 2 — crowd-flow model (what Phase 1 deliberately did NOT do)

Phase 1 made every number correct *for the current synthetic world*. That world still has these
structural limits, which are Phase 2's job:

1. **No network flow.** Each entity follows its own scripted logistic curve
   (`generator._demand_util`). People move between entities only through explicit transfers
   (approved interventions, closures, outages). Demand does not propagate along `feeds` /
   `last_mile_to` edges on its own, so a receiver's "baseline" is its own independent curve.
2. **Demand clip at 1.6× capacity** in the generator: late in the run, entities sit at the clip
   and large demand scenarios barely move the global peak.
3. **Deferral model is a window, not a re-timed arrival profile** (`stagger_entry` holds people
   back for 720 s and then the curve resumes).
4. **No receiver overflow routing.** A destination can exceed capacity (the certificate flags it);
   overflow is not re-routed onward.
5. **Twin for unobserved entities** is a peer-trend estimate — honest and bounded (unobserved
   utilisation RMSE ≈ 0.18 over 600 cycles), but it cannot see entity-specific events without a
   sensor. A flow model would give the EnKF genuine cross-entity dynamics to assimilate.
6. **The event-world model used for what-if / counterfactual / certificates IS the generator.**
   Once a crowd-flow model exists, the twin (not the ground truth) should drive projections, and
   certificate accuracy becomes a real out-of-sample measure.

## Phase 3 — cascade / GNN (known issues, untouched in Phase 1)
From the audit (P0-08): GNN steps are not root-conditioned, calibration is poor at low load,
40–60 simultaneous roots, alert flapping, recall ≈ 0.02. Risk now ignores none of this — the
cascade exposure term is unchanged — but the cascade output itself still needs redesign.

## Rules that carry over to every phase
- Scratch SQLite for every test/experiment (`DATABASE_URL=sqlite:////tmp/…`); never the real
  `Backend/eventflow.db`.
- Never kill processes by broad pattern; stop only PIDs you started.
- CHANGE → TEST → FIX → RETEST → regression; never weaken a test.
- After schema/model changes: re-export schemas + mocks (`--cycles 100`), `npm run validate:mocks`,
  `npm test`, `npm run build`.
