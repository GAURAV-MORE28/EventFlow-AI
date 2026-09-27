# ML

This folder is where the **trained** models go — the ones you're building on
Kaggle (HX-Cascade GNN, and the LightGBM warm-start forecaster if you train it;
Chronos-Bolt needs no training, just the weights).

**You do not need to write any backend-integration code.** The backend already
runs a full deterministic reference implementation of every module in
`Backend/app/ml_reference/`, matching `03_ML_CONTRACT.md` exactly — that's what
powers the demo right now. `Backend/app/ml_registry.py` (the *only* place ML is
instantiated, per `01_BACKEND_CONTRACT.md` §6) checks this folder first and
silently falls back to the reference implementation for any module you haven't
dropped in yet. So you can add your GNN alone, leave everything else as the
reference version, and the backend picks it up with zero code changes.

## Drop-in contract

Add a file here named after the module, exporting a class with the matching
name. The registry imports it as `ML.<file>.<Class>`:

| File | Class | Constructor gets |
|---|---|---|
| `forecaster.py` | `Forecaster` | `{seed, critical_utilisation, risk_bands, horizons_sec, tsfm_model, tsfm_enabled, warm_start_after_points, device, step_sec}` |
| `cascade.py` | `CascadePredictor` | `{seed, critical_utilisation, risk_bands, use_gnn, max_depth, propagation_threshold, gnn_artifact, gnn_mode, gnn_min_probability}` |
| `twin.py` | `AssimilatedTwin` | `{seed, ensemble_size, inflation_factor, obs_noise_var, process_noise_var, drift_mode_enabled}` |
| `equilibrium.py` | `EquilibriumSolver` | `{seed, max_iterations, convergence_tol, damping, compliance_sweep, alpha, beta, sigmoid_k}` |
| `optimiser.py` | `InterventionOptimiser` | `{seed, max_candidates, min_feasibility, weights}` |
| `risk.py` | `RiskScorer` | `{seed, weights, risk_bands}` |
| `anomaly.py` | `AnomalyDetector` | `{seed, z_threshold, window}` |
| `generator.py` | `SyntheticGenerator` | `(config, topology, seed)` — different signature, see `03_ML_CONTRACT.md` §8 |

Every class must implement the interface in `03_ML_CONTRACT.md` for that
module exactly — same method names, same return dict shape (key names, units,
casing), and a working `.fallback(...)`. `Backend/app/ml_reference/*.py` is a
complete, working reference for every one of these; copy its shape.

**Most likely path for this project:** only `cascade.py` needs to change. Once
HX-Cascade beats the deterministic propagator on held-out topologies (03 §4.3
swap criterion), drop it in here, flip `use_gnn: true` in `Backend/config.yaml`,
and the backend's `active_source` field switches from `"deterministic"` to
`"gnn"` automatically — no other file changes.

## Model bundles

A trained model ships as `ML/artifacts/<version>/` with `model.pt`,
`feature_norm.json`, optional `eval.json` / `calibration.json`, and a
`manifest.json` that binds every file by SHA-256 (`ML/manifest.py`:
`write_manifest()` creates it, `load_manifest()` verifies it). Point
`cascade.gnn_artifact` at the directory. A bundle that fails verification is not
loaded; `/health` then reports `gnn_mode: "off"` and `model_ready: false`. How the
backend uses the model (`shadow` / `annotate` / `off`) is 03 §4.5.

## Rules (03_ML_CONTRACT.md §0 — non-negotiable)

1. **No I/O.** Load weights in `__init__`; never touch Postgres, Redis, the
   network, or the filesystem afterward.
2. **No imports from `Backend/`.** The dependency arrow points one way:
   `ML/` → nothing backend-shaped. Plain dicts and NumPy arrays in and out.
3. **Every module exposes `.fallback()`** with the same return shape as its
   primary method. The backend calls it on timeout/exception — that's a normal
   operating state, not an error.
4. **Deterministic under `config["seed"]`.** The demo's `seed: 42` rehearsal
   depends on this.
5. **No exceptions escape.** Catch, log, return `self.fallback(...)`.

## Validating a drop-in module

```bash
cd Backend
python -m pytest tests/ -q          # contract tests still pass with your module loaded
python -m scripts.export_schemas --out ../contracts/schemas
python -m scripts.export_mocks --out ../Frontend/src/mocks --cycles 90
```

If your module's return dict doesn't match the shared schema, Pydantic
rejects it in `app/schemas.py` before it reaches the wire — that's the CI gate
described in `00_SHARED_CONTRACT.md`, "Schema validation in CI".

## Training data and the v3 pipeline

The GNN is trained on **random maps simulated by the live engine**, so the
features at training time are exactly what serving sees (sensor noise and
dropout, the EnKF twin's estimates, the twin-model forecast), while the labels
come from the simulator's ground truth. `generate_cascade_dataset` on the
reference generator stays unimplemented: the dataset needs the whole engine,
not the generator alone, so it lives in `Backend/scripts/` (the dependency
arrow still points one way — `ML/` imports nothing from `Backend/`).

| Step | Code |
|---|---|
| random maps (add/remove gates, roads, zones; rescale capacities, coefficients, travel times; move events and attendance; validated and hashed) | `ML/data/topology_randomiser.py` |
| dataset: each map simulated headless, snapshots of the published state, ground-truth labels at 900/1800/3600 s, the three baselines recorded beside them | `Backend/scripts/cascade_dataset.py` |
| one feature builder, imported by training and by serving | `ML/features/graph_features.py` |
| networks (v2 kept for its bundle; v3 edge-attributed, monotone hazards, time-to-critical head) | `ML/models/hx_cascade.py` |
| train, calibrate (temperature per horizon on validation maps), OOD stats, evaluate, write the bundle | `ML/training/train_v3.py` |
| metrics with cluster-bootstrap 95% intervals; baselines; 03 §4.3 swap criterion | `ML/evaluation/cascade_eval.py` |
| Kaggle, parameters in the first cell: the dataset on a CPU session (time-budgeted, resumable), then the sweep + calibration + evaluation on a GPU session | `ML/kaggle/cascade_v3_dataset.ipynb`, `ML/kaggle/hx_cascade_v3.ipynb` |

Splits are by map (train / val / test are distinct random maps). The unmodified
live map is never randomised into any split and is reported on its own. v2 was
trained on the live map, so its `live` numbers are in-sample.

```bash
# smoke run (about 2 minutes on 8 cores)
cd Backend
python -m scripts.cascade_dataset --out ../data/smoke --maps-train 1 --maps-val 1 --maps-test 1     --scenarios 2 --live-scenarios 2 --workers 8
cd ..
python -m ML.training.train_v3 --data data/smoke --out data/smoke_bundle --epochs 2
```

A bundle is served only by pointing `cascade.gnn_artifact` at it; that is a
separate decision made on its `eval.json`.

Report generalisation on held-out topologies separately from in-sample results,
and never claim field validity — every metric is framed as "in simulation, on
held-out topologies" (03 §8.4). Volunteering that limitation is worth more than
a judge finding it themselves.
