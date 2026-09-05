# Running EventFlow AI

Current status: **Backend and Frontend are complete and integrated. ML is a
deterministic reference implementation until the trained models land in `ML/`**
(see `ML/README.md` for the drop-in contract).

## Frontend only, zero backend (fastest way to see it)

```bash
cd Frontend
npm install
npm run dev
```

Open the printed `localhost` URL. `VITE_MOCK=1` is already set in `.env`, so
the whole app — map, cascade animation, intervention cards, twin drift toggle,
commander, metrics panel, attendee PWA — runs off `src/mocks/*.json`, which
were generated **by the backend itself** and validate against the same JSON
Schemas the backend uses. Nothing here is hand-authored fake data.

## Full stack (real backend, real 30-second cycle)

```bash
# Terminal 1
cd Backend
pip install -r requirements.txt
python run.py                    # http://localhost:8000, docs at /docs

# Terminal 2
cd Frontend
echo "VITE_MOCK=0" > .env
npm run dev                      # proxies /api and /ws to :8000
```

No database or Redis setup needed — SQLite and an in-process cache are the
defaults (see `Backend/.env.example` to point at real Postgres/Redis instead).

## Regenerating the frontend mocks

Whenever the backend's topology, forecaster, or cascade logic changes, refresh
the fixtures so mock mode keeps matching reality:

```bash
cd Backend
python -m scripts.export_schemas --out ../contracts/schemas
python -m scripts.export_mocks --out ../Frontend/src/mocks --cycles 40
cd ../Frontend
npm run validate:mocks           # fails the build if a mock drifted from contract
```

## Tests

```bash
cd Backend
python -m pytest tests/ -q       # 18 contract tests: ordering, error envelope,
                                  # verdict ownership, demo cascade chain, seed
                                  # reproducibility, commander grounding, etc.
```

## What's real vs. reference right now

| Module | State |
|---|---|
| REST API (all of `01_BACKEND_CONTRACT.md` §3) | **Real**, fully implemented |
| WebSocket + reconnect/resync | **Real** |
| 30s orchestration cycle, latency budgets, backpressure | **Real** |
| Demo topology (66 entities, the `metro_b→gate_3→road_4→emergency_north` chain, the `gate_5` saturation trap) | **Real** |
| `SyntheticGenerator` | **Real** (deterministic, seeded) |
| `RiskScorer`, `AnomalyDetector` | **Real** (these are contractually *not* ML — arithmetic, by design) |
| `EquilibriumSolver.certify()` | **Real** (the fixed-point solver + `derive_verdict`, exactly as specified) |
| `InterventionOptimiser` | **Real** (rule-based templates + ranking) |
| `Forecaster` | Reference (persistence / local trend model). Swap in Chronos-Bolt or your trained LightGBM by dropping `ML/forecaster.py` |
| `CascadePredictor` | Reference (deterministic propagator — this *is* the guaranteed-demo fallback per `03_ML_CONTRACT.md` §4.2). Drop in `ML/cascade.py` for HX-Cascade GNN |
| `AssimilatedTwin` | Real EnKF (aggregate state, covariance inflation) — this doesn't need Kaggle at all |
| Commander (LLM explainer) | **Deterministic, no paid API** — template synthesis over real tool-call results, so `ungrounded_count` is 0 by construction. `LOCAL_LLM_URL` env var (Ollama) optionally swaps in free local phrasing; the grounding validator runs identically either way |
| Frontend (all of `02_FRONTEND_CONTRACT.md`) | **Real**, all components, mock mode and live mode share the same store/WS code path |

Nothing here needs Kaggle to run end-to-end today — that's the point of the
reference implementations. They upgrade in place when the trained models land.
