# Change lock — ML Phase 2 (training pipeline)

Lifted 2026-09-27 by the owner for Phase 2 of the ML plan. The earlier
"final hardening pass" lock (ML frozen, `Paresh` branch only) is superseded.
Branch: `gaurav`; no force push; push only when the owner says so.

## ALLOWED (Phase 2 scope)

- restructuring `ML/` into `models/`, `features/`, `data/`, `training/`,
  `evaluation/`, `kaggle/`
- retraining HX-Cascade and redesigning the R-GCN (v3: edge attributes,
  three horizons, evaluated time-to-critical)
- a topology randomiser and an offline dataset generator that runs the live
  engine's observation-noise model and twin assimilation
- an evaluation harness (map-level splits, baselines, confidence intervals,
  the live map held out), calibration and out-of-distribution statistics
- a Kaggle notebook and a local smoke run
- tests for all of the above
- everything allowed by the previous lock (backend bug fixes, determinism,
  state consistency) where Phase 2 needs it

## NOT ALLOWED

- swapping the serving bundle (`cascade.gnn_artifact`) to v3 or changing
  `cascade.gnn_mode` from `shadow`; that is Phase 3, after the evaluation
- inventing additional AI models beyond HX-Cascade
- changing the published cascade (`services/cascade_flow.py`) or anything the
  frontend renders
- deleting the obsolete v1 files (scheduled for Phase 6)
- unrelated features, UI redesign, rewriting working code for style
- changing branches, force pushing

## Contract rules still in force

- Wire contract changes are additive only (new optional fields / new WS
  message types); no renames, removals or type changes.
- Every derived value has one owner (see `implementationstate.md §2`).
- `ML/` imports nothing from `Backend/`. Offline code that drives the engine
  lives in `Backend/scripts/` and hands `ML/` plain dicts / arrays.
- Thresholds come from `Backend/config.yaml`, never hard-coded in `ML/`.
