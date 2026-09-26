# EventFlow AI — Contract Set

Four files. Read them in order. **Nobody writes code before reading `00`.**

| File | Owner | Everyone else's relationship to it |
|---|---|---|
| `00_SHARED_CONTRACT.md` | Backend lead | **Read-only for everyone.** Single source of truth for types, enums, units, IDs. |
| `01_BACKEND_CONTRACT.md` | Backend | Frontend reads §3–4 (endpoints, WS). ML reads §6 (boundary). |
| `02_FRONTEND_CONTRACT.md` | Frontend | Backend reads §5 to know what shape each component needs. |
| `03_ML_CONTRACT.md` | ML | Backend reads §0–1 for the calling convention. |

---

## The three rules that prevent merge hell

**1. One direction of dependency.**
```
ml/  ←── backend/  ←── frontend/
```
`ml/` never imports `backend/`. `backend/` never imports `frontend/`. No exceptions, no "just this once."

**2. Nothing is computed twice.**
Every derived value has exactly one owner:

| Value | Computed by | Everyone else |
|---|---|---|
| `risk_band` | `RiskScorer` (ML) | renders the string |
| `verdict` | `derive_verdict()` in `EquilibriumSolver` (ML) | renders the string |
| `time_to_critical_sec` | `Forecaster` (ML) | formats to minutes |
| `improvement_pct` | ML modules | renders |
| `rank_score` | `InterventionOptimiser` (ML) | renders in received order |
| `load_variance` | Backend from state | renders |
| Sort order | Backend | **never re-sorts** |

If the frontend ever writes `if (score > 80) return 'critical'`, the contract is broken.

**3. Mocks are authored before endpoints.**
Frontend builds against `src/mocks/*.json` from hour zero. Those mocks validate against the same schemas the backend validates against. The first real integration is then a config flip, not a debugging session.

---

## Day-zero sequence (first 3 hours, in parallel)

| Hour | Backend | Frontend | ML |
|---|---|---|---|
| H0 | Write `00_SHARED_CONTRACT` §5 topology seed | Scaffold Vite + Tailwind + routes | Scaffold `ml/` package, stub all 8 classes with `fallback()` returning valid shapes |
| H1 | DDL + `GET /graph`, `GET /health` | Author `mocks/graph.json` + `mocks/state_sequence.json` | `SyntheticGenerator.tick()` |
| H2 | Cycle loop skeleton calling ML stubs | MapCanvas rendering from mock graph | `RiskScorer`, `AnomalyDetector`, persistence `Forecaster` |
| H3 | **Review frontend mocks against the contract** ← this is the integration test | Pressure timeline from mock | Deterministic cascade propagator |

That H3 mock review is the single highest-leverage 30 minutes of the build. Do not skip it.

---

## Schema validation in CI

Create `contracts/schemas/` with JSON Schema files generated from the Pydantic models:

```bash
# backend generates them
python -m backend.scripts.export_schemas --out contracts/schemas/

# CI validates BOTH sides against the same files
pytest tests/test_ml_contract.py       # every ML return validates
npm run validate:mocks                 # every mock file validates
```

Required schema files:
```
contracts/schemas/
  entity.json          graph_edge.json      entity_state.json
  forecast.json        cascade_result.json  certificate.json
  intervention.json    twin_fidelity.json   regret_entry.json
  nudge.json           segment.json         error_envelope.json
  ws_message.json
```

**If a mock file fails validation, the build fails.** That is the whole mechanism.

---

## Integration checkpoints

| Hour | Checkpoint | Pass condition |
|---|---|---|
| **H8** | Frontend flips `VITE_MOCK=0` and hits real `GET /graph` + `GET /state` | Map renders from live backend |
| **H14** | WS connected, `state_update` deltas merging | Entities move on the map without blanking |
| **H21** | Twin fidelity + drift toggle end-to-end | Red line diverges from green within 5 cycles |
| **H27** | Interventions with embedded certificates | UNSTABLE card renders struck-through |
| **H31** | Commander with grounding validator | `ungrounded_count: 0` on all four scripted questions |
| **H33** | **Full demo run, seed 42, three times** | Identical every time |

Miss a checkpoint → cut the corresponding stretch feature immediately, do not push the checkpoint.

---

## Cut order (memorise this)

When time runs short, delete in exactly this order. Do not deliberate:

1. `CascadePredictor` GNN → keep deterministic (`use_gnn: false`)
2. Edge crowd counting
3. Multi-event collision
4. Regret ledger chart → keep the logging
5. `EquilibriumSolver.solve_leader()` → keep `certify()`

**Never cut:** the assimilated twin, the certificate, or the H33 rehearsal block.

---

## Contract change protocol

1. Bump the version at the top of `00_SHARED_CONTRACT.md`.
2. Post a one-line diff in the team channel.
3. Backend updates `00` + `01` + regenerates schemas in one commit.
4. **After H20: additive changes only.** New fields with defaults. No renames, no removals, no type changes.

The reason for that H20 freeze is simple — a rename at H28 costs three people an hour each and buys nothing a judge will ever see.

---

## Generated worlds (venue → blueprint → event graph)

Besides the synthetic demo city, the engine can simulate a network generated from OpenStreetMap
around any venue (`/venue` in the UI; `app/geospatial/` in the backend). All new wire types are
additive (`Entity.subtype/capacity_source/capacity_confidence/provenance`, `GraphEdge.geometry/
distance_m/via_entity_ids/provenance`, edge type `connects_to`, `GraphResponse.world`, blueprint
and venue endpoints) and exported to `contracts/schemas/`. See `RUNNING.md` and
`PHASE2_BLUEPRINT_VALIDATION_REPORT.md`.
