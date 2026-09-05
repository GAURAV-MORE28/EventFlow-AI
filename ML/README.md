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
| `cascade.py` | `CascadePredictor` | `{seed, critical_utilisation, risk_bands, use_gnn, max_depth, propagation_threshold, gnn_checkpoint}` |
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

## Training data

`SyntheticGenerator.generate_cascade_dataset(n_scenarios, randomise_topology=True)`
is the GNN's training source (03 §8.1, §8.4). `randomise_topology=True` is
required — training on one fixed map teaches the model that map's geometry,
not propagation mechanics. The reference generator (`Backend/app/ml_reference/generator.py`)
raises `NotImplementedError` on this method by design; it drives the live demo
only. Implement `generate_cascade_dataset` here, run it standalone (e.g. in the
Kaggle notebook), and bring back only the trained checkpoint plus the
`CascadePredictor` class that loads it — the generator itself never needs to be
imported by the backend.

Report generalisation on held-out topologies separately from in-sample results,
and never claim field validity — every metric is framed as "in simulation, on
held-out topologies" (03 §8.4). Volunteering that limitation is worth more than
a judge finding it themselves.
