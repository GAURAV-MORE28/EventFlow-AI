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

## Full stack (real backend, real 30-second cycle) — the intervention demo

```bash
# Terminal 1
cd Backend
pip install -r requirements.txt
python run.py                    # http://localhost:8000, docs at /docs

# Terminal 2
cd Frontend
npm run dev:live                 # VITE_MOCK=0 via .env.live; proxies /api and /ws to :8000
```

Open the printed `localhost` URL. `npm run dev` (no suffix) stays mock mode —
`dev:live` is the only switch, so a fresh clone still needs no `.env` file.

The first ~30–45 s is the deliberate calm opening (02 §5.4): the forecaster
warms up and entities ramp toward critical before the first intervention
recommendation appears. To move faster during a demo:

```bash
curl -s -X POST localhost:8000/api/v1/demo/control \
  -H 'Content-Type: application/json' -d '{"speed_multiplier": 120}'
```

Approve a recommendation and the target entity's utilisation, risk band, and
downstream cascade prediction visibly change over the next few cycles — the
numbers on the ActionHUD are read straight from `GET /state`, not projected.
`POST /demo/control {"action": "reset"}` starts a fresh run (queue clears, then
regenerates after the warm-up); the live backend has no automatic reset.

No database or Redis setup needed — SQLite and an in-process cache are the
defaults (see `Backend/.env.example` to point at real Postgres/Redis instead).
`entity_state`/`risk_state`/`forecast` are cleared on every startup (and on
`POST /demo/control {action: reset}`) since `sim_time` is fully determined by
the seed — a restart against a leftover `eventflow.db` would otherwise collide
on `(entity_id, sim_time)`. `intervention`/`certificate`/`regret_entry`/`nudge`
persist across restarts (see the table below — they used to be write-never).

## Closing the loop: approving an intervention is real

`POST /interventions/{id}/approve` doesn't just flip a status flag — it calls
`generator.apply_relief(target_entity_ids, relief_pct)`, a real demand-side
reduction on the simulation, and forks a `twin.branch()` "do nothing"
counterfactual at that moment. 900s later, `realised_relief_pct` and
`counterfactual_relief_pct` on the resulting regret-ledger entry are genuine
measurements — the do-nothing branch vs. what the simulation, with the relief
actually applied, produced — not a placeholder.

## Regenerating the frontend mocks

Whenever the backend's topology, forecaster, or cascade logic changes, refresh
the fixtures so mock mode keeps matching reality:

```bash
cd Backend
python -m scripts.export_schemas --out ../contracts/schemas
python -m scripts.export_mocks --out ../Frontend/src/mocks --cycles 90
cd ../Frontend
npm run validate:mocks           # fails the build if a mock drifted from contract
```

90 cycles, not 40: metro_b's own critical crossing plus the certification
pass that produces both an UNSTABLE and a STABLE verdict for it both land well
past cycle 60 once the EnKF forecast step is correctly wired in (see below) —
other twin-estimated entities now genuinely compete with it for the
"most urgent" trigger slot each cycle, which is expected, not a regression.

## Tests

```bash
cd Backend
python -m pytest tests/ -q       # 31 tests: test_contract.py (18 — ordering,
                                  # error envelope, verdict ownership, demo
                                  # cascade chain, seed reproducibility,
                                  # commander grounding) + test_ml_contract.py
                                  # (13 — every ML module's return validates
                                  # against its schema, §0 universal rules)
```

CI (`.github/workflows/ci.yml`) runs the backend suite, exports schemas, then
runs `npm run validate:mocks` and `npm run build` against them — the "schema
validation in CI" the contracts always described now actually runs.

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
| `CascadePredictor` | **Real HX-Cascade GNN** (`ML/cascade.py`, `use_gnn: true`) — 2-layer R-GCN, single-shot multi-horizon. Deterministic propagator (`03_ML_CONTRACT.md` §4.2) stays wired as `.fallback()`, firing automatically on any GNN exception/timeout/NaN |
| `AssimilatedTwin` | Real EnKF (aggregate state, covariance inflation) — this doesn't need Kaggle at all. `step()` (the forecast half) is wired into the cycle; measured RMSE improvement vs. an uncorrected ABM is ~53%, clearing the ≥50% target |
| Commander (LLM explainer) | **Deterministic, no paid API** — template synthesis over real tool-call results, so `ungrounded_count` is 0 by construction. `LOCAL_LLM_URL` env var (Ollama) optionally swaps in free local phrasing; the grounding validator runs identically either way |
| Frontend (all of `02_FRONTEND_CONTRACT.md`) | **Real**, all components, mock mode and live mode share the same store/WS code path |
| `/metrics` (`prediction`/`twin`/`decision` panels) | **Real** — `cascade_precision`/`cascade_recall` are measured online against actual outcomes (not a formula over cascade count), `certificate_accuracy_pct` cross-checks a certified equilibrium against an independent `twin.branch()` rollout, `convergence_rate_pct` is a separate field from that (03 §5.6 names two distinct metrics; they used to be one), and the decision-panel reductions compare against a genuine do-nothing counterfactual instead of a cycle-3 snapshot |

Nothing here needs Kaggle to run end-to-end today — that's the point of the
reference implementations. They upgrade in place when the trained models land.
