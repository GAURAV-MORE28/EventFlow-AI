# EventFlow AI — Final Verification Audit

| | |
|---|---|
| Audit date | 2026-09-26 (local machine, macOS Darwin 27.0.0) |
| Branch / commit | `Yash` @ `bab9bf541ca13bf8ab47c7f9c8e5d6e81fae4a2b` (tracks `origin/Yash`) |
| Mode | Read-only behavioural audit. **Nothing was fixed.** Only this file was added to the repository. |
| Method | Backend run via its real entry point (`run.py`, uvicorn `--reload`, port 8000) against a **scratch SQLite DB**; in-process engine harnesses (600-cycle runs, A/B approval runs, determinism runs) against separate scratch DBs; REST sweep (74 requests); raw WebSocket clients (Node 24); real headless Chrome 154 driven over CDP against `npm run dev:live` and `npm run dev`; direct probes of every ML module; the project's own test suites. |
| Real demo DB | `Backend/eventflow.db` was **never written**. Only `sqlite3 -readonly` queries were run against it. MD5 before and after: `168ea37ed076591c918753a24a660c11`, mtime unchanged (`1790416912`). |

Status vocabulary used throughout: **PASS**, **PARTIAL**, **FAIL**, **UNVERIFIED** (blocked or not exercised), plus **MISLEADING** in the demo chain (§27).

---

## 1. Executive Summary

### What was tested
- **Environment:** interpreter, dependencies and startup; backend lifecycle including kill/restart.
- **REST API:** every endpoint, with valid, invalid and malformed requests.
- **Schemas:** live responses validated against `contracts/schemas` with AJV.
- **WebSocket:** four concurrent clients, including attendee filtering, ping, resync, a malformed frame, reconnect, and a server restart.
- **Database:** persistence checked in scratch DBs.
- **Topology:** full graph analysis.
- **Simulation:** 600-cycle in-process runs.
- **Digital Twin:** measured against generator ground truth.
- **Forecaster:** compared against real later values; synthetic edge cases.
- **Risk:** banding table from 0.1 to 2.0 utilisation.
- **Anomaly detector:** unit-probed.
- **GNN:** loaded, timed and sensitivity-probed; all fallbacks forced.
- **Optimiser and equilibrium solver:** per-template probes plus relief sweeps.
- **What-if:** all 10 scenario types (15 variants).
- **Intervention lifecycle:** approve (reroute and notify), reject and expire, A/B-compared against a do-nothing run with the same seed.
- **Settlement, regret, nudges and attendee journeys:** 14 origin/destination pairs.
- **Commander:** 18 queries, including stale-cache and entity-resolution tests.
- **`/metrics`:** read at several points in a run.
- **Frontend:** live UI and mock UI in a real browser, including a full mock loop wrap; build, tests and mock validation.
- **Determinism:** identical runs, reset, what-if perturbation, and three full demo rehearsals.

### What passed (infrastructure is solid)
- The backend boots in about 4.8 s, the GNN checkpoint loads and runs in 0.7 ms per forward pass, and the cycle loop ran 600 cycles without an exception. Cycle latency is p50 29 ms against a 2,000 ms cap.
- Every REST endpoint returns the expected status codes for valid requests and missing IDs. All 31 captured live responses validate against the committed JSON Schemas, which were regenerated identically apart from one cosmetic keyword.
- The WebSocket envelope, per-cycle events, ping/pong, resync and attendee filtering work.
- The simulation is fully deterministic under seed 42: two identical runs match on every field for 150 cycles, reset reproduces the fresh-start run exactly, and three clean-reset demo rehearsals were byte-identical.
- Approval really changes the simulated world, reject has no side effects, cards expire, settlement fires 900 sim-s later, and audit rows are written.
- Mock-mode replay wraps from frame 100 to 1 and re-arms cascades and interventions every loop. `pytest` passes 32/32, mocks validate 17/17, `npm test` passes, and the build succeeds.

### What failed (the numerical and decision layer)
- **The Digital Twin diverges for every unobserved entity.**
  - Estimates pin at 0% or 200% while ground truth sits at 30–110%.
  - For those entities it is up to 30× worse than simply holding their initial values.
  - These artefacts become the Pressure Timeline hero ("Core Emergency Post 200%"), the Commander's "biggest problem", the first interventions, and a 5× inflation of the load-variance headline.
- **The twin branch explodes.** It powers what-if, the approval counterfactual and certificate accuracy.
  - What-if baselines show 2,048–8,055% peak utilisation; closing Gate 3 gives 96,405%.
  - Capacity and hotel scenarios have about 0% effect.
  - The counterfactual clamps at −100%, and `/metrics` reports an **84.1% peak-utilisation reduction** after approving a 4% "notify only" action.
- **Approving a reroute removes demand from both the source and the destination.** `metro_c` loses 36% instead of receiving `metro_b`'s riders, and `gate_5` never saturates. A "notify operations team" approval cuts real demand by 4%.
- **Equilibrium verdicts are structurally determined by relief size.** Any relief of 8% or more never converges, which makes it UNSTABLE. 95% of live certificates and 99% of the certificates in the real DB are UNSTABLE, and certificate accuracy is **0%** (0/38).
- **Risk bands under-rate over-capacity.** 100% utilisation is "moderate", 120% is "moderate", and 150% is "high".
- **The GNN cascade is not root-conditioned and is poorly calibrated.**
  - A downstream node's probability is identical whichever root is traced.
  - Transport hubs score p > 0.6 at 30% load.
  - There are 40–60 simultaneous cascade roots, including hotels, zones and parking.
  - 307 `cascade_alert` events arrived in about 90 cycles.
- **The frontend freezes silently after any backend restart.** WebSocket `seq` restarts below the client's `lastSeq`, so every frame is dropped while the chip shows "connected". Every `--reload` file save triggers this.
- **`speed_multiplier: 0` (or any negative value) permanently freezes the engine.** Play and reset cannot recover it.

### Is it demo-safe?
**Only conditionally.** The scripted happy path runs reproducibly: `metro_b` cards appear at about 24 s wall (cycle 49), with the STABLE notify card above the UNSTABLE reroute, and approval settles about 14 s later. But on-screen numbers are wrong in ways a judge can see during that same path:
- Pressure-timeline hero at 200%.
- What-if Δ of +616%.
- HUD "do-nothing −100pp".
- `/metrics` "84% peak reduction".
- Attendee view recommending the congested Metro B as "LOW DENSITY".
- Over-capacity entities shown as "moderate".

A backend file save during the demo freezes the UI until the page is refreshed.

### Major blockers
Findings P0-01 to P0-08 (§30).

---

## 2. Environment

| Item | Value | Status |
|---|---|---|
| OS | macOS (Darwin 27.0.0), Apple Silicon | — |
| Backend Python | `Backend/.venv/bin/python` → **3.13.15** | PASS |
| System python3 | 3.9.6; **no `python` on PATH** (`python run.py` from the docs fails without activating the venv) | PARTIAL |
| Key Python packages | fastapi 0.115.6, numpy 2.2.1 (Accelerate BLAS), pydantic 2.10.4, SQLAlchemy 2.0.36, torch 2.10.0, torch-geometric 2.8.0.post1, websockets 14.1, pytest 8.3.4. No chronos, redis, psycopg or pandas | PASS |
| MPS | `torch.backends.mps.is_available() == True`; unused by design (`map_location="cpu"`) | PASS |
| Node / npm | v24.21.0 / 11.19.0; `Frontend/node_modules` present; Vite 6.4.3 | PASS |
| Browser for UI tests | Google Chrome 154 headless via CDP. Without GPU flags, deck.gl fails to create a WebGL context (a headless-environment limitation, not an app bug); relaunched with `--use-angle=swiftshader`, the map renders | PASS |
| `Backend/.env` | Present, identical to `.env.example`. **Never loaded**: no `load_dotenv`/`--env-file` anywhere, but the defaults match, so it is harmless | PARTIAL |
| `Frontend/.env` | Absent (`RUNNING.md` says it exists). `.env.live` (`VITE_MOCK=0`) is loaded by `--mode live` | PASS |

## 3. Repository State

| Item | Value |
|---|---|
| Branch | `Yash` (`origin/Yash`), commit `bab9bf5` — "Update EventFlow frontend UI" |
| Main branch | `main` @ `fd22dc3` |
| Git status **before** the audit | ` M CLAUDE.md` — **pre-existing modification, not made by this audit** (+699/−135 lines) |
| Git status **after** the audit | ` M CLAUDE.md` (unchanged, pre-existing) and `?? FINAL_AUDIT_REPORT.md` (this file). See the final git check at the end |
| Ignored-file side effects | Python bytecode caches (`Backend/**/__pycache__`, `ML/__pycache__`; `Backend/scripts/__pycache__` created by running the export scripts) and the Vite dep cache (`Frontend/node_modules/.vite`). All are git-ignored; no tracked file changed |
| Scratch artifacts | All DBs, logs, screenshots, harness scripts and JSON evidence live in the session scratchpad (`/private/tmp/claude-501/.../scratchpad/`), outside the repository |

## 4. Test Inventory

### 4.1 Executed test programs
| # | Test | Where | Result |
|---|---|---|---|
| T1 | `pytest tests/ -q` (32 tests) with `DATABASE_URL=<scratch>` | Backend | **32 passed**, 4.40 s |
| T2 | `npm run validate:mocks` | Frontend | 17/17 valid |
| T3 | `npm test` (validate + mock lifecycle regression) | Frontend | pass |
| T4 | `npx vite build --outDir <scratch>` (same as `npm run build`, redirected so `Frontend/dist` was not touched) | Frontend | built in 2.45 s; JS 4,019 KB (614 KB gz); CSS 40 KB; chunk-size warning |
| T5 | `scripts.export_schemas --out <scratch>` + diff against `contracts/schemas` | Backend | 28 schemas; 6 differ only by `"additionalProperties": true` on dict fields |
| T6 | `scripts.export_mocks --out <scratch> --cycles 100` + diff against `Frontend/src/mocks` | Backend | 15/17 differ (analysed in §22) |
| T7 | Backend startup via `run.py` (reload), scratch DB | live | ready in **4.84 s**; restart in 1.34 s |
| T8 | REST sweep `sweep.py` — 74 requests (valid/invalid/malformed/missing/repeated) | live | see §6 |
| T9 | Live response schema validation (AJV 2020, `contracts/schemas`) — 31 captured responses | live | **31/31 pass** |
| T10 | 600-cycle in-process engine harness (per-cycle state, twin, forecasts, cascades, interventions, timing) | in-process | see §9–§16 |
| T11 | Twin probe: drift mode, RMSE against ground truth, static baseline, branch behaviour | in-process | see §10 |
| T12 | GNN probe: checkpoint, shapes, calibration, root sensitivity, timing, forced NaN/exception/load failure | in-process | see §13 |
| T13 | Equilibrium probe: relief sweep, gate_5 trap, pre-existing overload, determinism | in-process | see §15 |
| T14 | Unit probes: risk table, forecaster edge cases, anomaly, topology integrity/reachability, optimiser per root type, rank | in-process | see §11–§14 |
| T15 | What-if: 15 scenario variants at cycle 60 **and** cycle 12, plus a live-state-unchanged check | live | see §17 |
| T16 | Lifecycle A/B: do-nothing vs approve-reroute vs approve-notify vs reject vs expire (120 cycles each, same seed) | in-process | see §16 |
| T17 | WebSocket audit (two passes): command_centre, 2 attendee sockets, no-query socket, ping, resync, malformed frame, reconnect, approval mid-stream, settlement | live | see §7 |
| T18 | Browser, live mode: open, cycle 50/52 snapshot, pressure row → detail panel → cascade arcs, 4 scripted Commander questions, 2 what-if presets, twin expand + drift toggle, approve via UI, HUD through settlement, `/metrics`, `/attendee` (400 px) accept + step-free toggle | Chrome | see §21 |
| T19 | Browser, mock mode: 12 ticks, approve, 3 what-if presets, unscripted Commander query, full loop wrap (frame 100 → 1) and 13 ticks after | Chrome | see §22 |
| T20 | Browser + backend restart (kill reloader, relaunch) → UI recovery observation; manual page refresh | Chrome | see §7/§21 |
| T21 | Commander suite: 18 queries + stale-cache-across-reset test | live | see §19 |
| T22 | Edge cases: pause/resume, speeds 15/60/120/150/300, speed 0, seek (kickoff / before start), live `inject`, 200 concurrent mixed requests, 8 rapid resets | live | see §26 |
| T23 | Attendee journeys: 14 origin/destination/segment combinations at cycle 80 | live | see §18 |
| T24 | Determinism: 2 identical 150-cycle runs; a run with a what-if at cycle 30; a run with an in-process reset at cycle 40 | in-process | see §24 |
| T25 | Demo rehearsal ×3 from clean reset (API-level, timings, approve, nudge, settle, metrics, what-if, journey) | live | see §27 |
| T26 | Load-variance truth comparison (displayed against ground-truth zone variance) | in-process | see §9 |
| T27 | Read-only inspection of the real `Backend/eventflow.db` (row counts, statuses, verdicts, regret, certificate orphans) | read-only | see §8 |

### 4.2 Check register (every individual check, with its status)

| ID | Area | Check | Status | Evidence (short) |
|---|---|---|---|---|
| C001 | Repo | Active branch is `Yash`, commit `bab9bf5` | PASS | `git branch -vv` |
| C002 | Repo | Working tree clean before audit | PARTIAL | `CLAUDE.md` already modified |
| C003 | Env | Venv Python + deps import | PASS | 3.13.15 |
| C004 | Env | torch + PyG import; GNN loads | PASS | log "HX-Cascade GNN loaded" |
| C005 | Env | `.env` loading | PARTIAL | never loaded; defaults match |
| C006 | Env | Startup free of runtime warnings | PARTIAL | numpy "divide by zero/overflow/invalid value in matmul" every cycle; spurious (Accelerate BLAS), results finite |
| C007 | Startup | `npm run dev` (mock) serves | PASS | :5174 200 |
| C008 | Startup | `npm run dev:live` serves + proxies `/api` and `/ws` | PASS | :5173 → health 200 |
| C009 | Startup | Backend via `run.py` reaches ready | PASS | 4.84 s |
| C010 | Lifecycle | Lifespan: create_all, clear_run_tables, seed, verify, engine start | PASS | log lines |
| C011 | Lifecycle | 600 cycles without exception | PASS | harness |
| C012 | Lifecycle | Restart begins at cycle 0 | PASS | health after restart |
| C013 | Lifecycle | `--reload` behaviour safe for demo | PARTIAL | save ⇒ sim restarts; killed worker leaves reloader holding :8000 with nothing serving |
| C014 | API | Every endpoint returns expected code on valid input | PASS | sweep |
| C015 | API | 404s for missing entity/intervention/certificate/simulation/nudge | PASS | sweep |
| C016 | API | 400 for malformed JSON / missing body / extra fields / bad horizon / bad scenario type / bad query params | PASS | sweep |
| C017 | API | 409 on double approve / reject-after-reject / approve-after-reject | PASS | sweep, A/B |
| C018 | API | Approve after expiry returns `INTERVENTION_EXPIRED` | PARTIAL | returns `INTERVENTION_ALREADY_RESOLVED` (engine already set status `expired`) |
| C019 | API | Malformed `seek_to_sim_time` rejected with 400 | FAIL | 500 `INTERNAL_ERROR` |
| C020 | API | 405 / unknown-route envelopes carry correct codes | PARTIAL | 405 → code `INTERNAL_ERROR`; unknown route → `ENTITY_NOT_FOUND` |
| C021 | API | Unknown `status` filter validated | PARTIAL | `status=bogus` → 200 with empty list |
| C022 | API | `/simulate` rejects unknown entity IDs | FAIL | `gate_99` accepted → 202, silently 0% effect |
| C023 | API | `/demo/control` rejects speed ≤ 0 | FAIL | accepted; engine then frozen (C080) |
| C024 | API | No-route journey uses a meaningful error code | PARTIAL | `ENTITY_NOT_FOUND` 404 "No route…" |
| C025 | API | Double nudge response uses a nudge-appropriate code | PARTIAL | `INTERVENTION_ALREADY_RESOLVED` |
| C026 | Contract | Live responses validate against schemas | PASS | 31/31 AJV |
| C027 | Contract | Committed schemas equal fresh export | PARTIAL | 6 files differ by `additionalProperties: true` |
| C028 | Contract | snake_case keys in live payloads | PASS | recursive scan |
| C029 | Contract | Error envelope shape uniform | PASS | 10 error responses validated |
| C030 | WS | Connect → immediate `resync` | PASS | first event resync for all 5 sockets |
| C031 | WS | `seq` monotonic within a server lifetime | PASS | all sockets |
| C032 | WS | ping → pong | PASS | |
| C033 | WS | `{action:resync}` → resync | PASS | |
| C034 | WS | tick / state_update / forecast_update / twin_fidelity every cycle | PASS | 89–92 each in ~45 s; no skipped ticks |
| C035 | WS | `cascade_alert` only for newly active roots, no flood | FAIL | 307 alerts / ~90 cycles, 48 roots, `hotel_airport_cluster` ×16 |
| C036 | WS | intervention_queued / resolved / regret_update / nudge_pushed / journey_risk_update / anomaly delivered | PASS | counts in §7 |
| C037 | WS | Attendee filtering | PASS | `att_other` did not receive `att_demo_1` nudge |
| C038 | WS | Survives a malformed client frame | FAIL | socket stays OPEN, server sends nothing more (zombie) |
| C039 | WS | `state_update` is a delta | PARTIAL | all 66 entities every cycle (~12 KB) |
| C040 | WS/FE | UI recovers after backend restart | FAIL | UI frozen at cycle 60 while the backend ran to 33+; "connected" chip |
| C041 | WS | Raw client reconnect to the same process | PASS | new socket got resync + stream |
| C042 | DB | Tables created, topology seeded idempotently | PASS | 66/120/5 |
| C043 | DB | entity_state + forecast written each cycle | PASS | 66 and 198 rows/cycle |
| C044 | DB | intervention/certificate/regret/nudge/audit/commander_log persisted | PASS | scratch DB counts |
| C045 | DB | execution / cascade_prediction / risk_state written | FAIL | 0 rows ever (execution & cascade never written; risk_state only cleared) |
| C046 | DB | Persisted rows never lost to ID collisions | FAIL | real DB: 7 of 1,184 interventions have no certificate row (16-bit cert IDs) |
| C047 | DB | Growth bounded / pruned | PARTIAL | real DB 213 MB after one ~4,367-cycle run |
| C048 | DB | Demo DB untouched by audit | PASS | MD5/mtime identical |
| C049 | Topology | 66 nodes / 120 edges / 5 segments; no duplicate IDs, bad refs or self-loops; one connected component | PASS | unit probe |
| C050 | Topology | `verify()` rejects a broken demo chain | PASS | `TopologyIntegrityError` |
| C051 | Topology | Capacities plausible | PASS | ranges in §9 |
| C052 | Topology | Egress / return paths exist | FAIL | nothing reachable from `stadium_main` over traversable edges |
| C053 | Topology | Alternative accommodation relationships exist | PASS | 5 hotel `substitutes_for` edges |
| C054 | Sim | Ground truth deterministic under seed | PASS | T24 |
| C055 | Sim | People flow across graph edges | FAIL | independent scripted logistic curves per entity |
| C056 | Sim | Event timing coherent (exit spike after the match) | FAIL | exit spike starts 150 sim-min = 16:30 (kickoff 16:00, end 18:30) |
| C057 | Sim | Event ends / stops at `end_time` | FAIL | runs indefinitely; real DB reached 02:23 two days later |
| C058 | Sim | Ground-truth values plausible | PARTIAL | truth utilisation up to 160% (clip) e.g. `road_13` |
| C059 | Sim | Live `inject` gate closure propagates downstream | PARTIAL | gate → 0, siblings +20%; adjacent `road_4` unchanged |
| C060 | Sim | Live `inject` capacity reduction raises utilisation | FAIL | `metro_b` −50% capacity ⇒ utilisation **0.82 → 0.42** |
| C061 | Sim | Live `inject` for attendance/rain/hotel/concurrent/parking | UNVERIFIED | not injected live (only via what-if, §17) |
| C062 | Sim | Pause/resume, speed changes (cap 150×) | PASS | §26 |
| C063 | Sim | Seek semantics | FAIL | cycle reset to 0 but history/interventions kept; seek before start accepted |
| C064 | Replay | Mock replay = 100 frames, starts, loops | PASS | T19 |
| C065 | Replay | Mock wrap resets queue, HUD, cascades, risk | PASS | post-wrap snapshots |
| C066 | Replay | Mock re-arms interventions each loop | PASS | frame 10 every loop; `npm test` |
| C067 | Replay | Mock TopBar cycle on final frame | PARTIAL | shows "0 / 100" (100 % 100) |
| C068 | Replay | Live cycle display after 100 cycles | FAIL | shows cycle mod 100 ("4 / 100" at cycle 104); the sim does not reset |
| C069 | Replay | Explicit reset reproduces the run | PASS | T24, T25 |
| C070 | Replay | Page refresh recovers | PASS | T20 |
| C071 | Twin | Ensemble 20, 132-dim state | PASS | probe |
| C072 | Twin | Observed entities displayed from raw observation | PASS | display RMSE 0.003–0.008 util |
| C073 | Twin | Unobserved estimates track truth | FAIL | e.g. `emergency_core` 2.0 vs 0.32–0.69; `zone_concourse_south` 0.017 vs 0.944 |
| C074 | Twin | Assimilation beats no-assimilation on unobserved | FAIL | util RMSE twin 11.19 vs uncorrected 0.87 vs static 0.35 (cycle 100) |
| C075 | Twin | Assimilated RMSE stable over the run | FAIL | 92.7 → 1,293 → 10,415 counts (cycles 1/100/600) |
| C076 | Twin | Ensemble spread meaningful | FAIL | ens std ≈ 0 while mean wrong (overconfident divergence) |
| C077 | Twin | Drift mode meaningful | FAIL | "improvement" 80–96% against an exploding uncorrected ensemble (min count −25,411) |
| C078 | Twin | `branch()` never mutates live store | PASS | store snapshot identical |
| C079 | Twin | `branch()` isolated from live RNG | FAIL | consumes live RNG; live state diverges (T24) |
| C080 | Engine | Loop recovers from a bad speed setting | FAIL | speed 0 ⇒ `sleep(3e7 s)`; play/reset don't recover |
| C081 | Twin | No NaN/Inf in ensemble | PASS | 600 cycles, 0 NaN/Inf |
| C082 | Forecast | 3 horizons + 90% bands + baseline_comparison present | PASS | |
| C083 | Forecast | TTC on a linear rise | PASS | 655 s vs true 630 s |
| C084 | Forecast | Already-critical ⇒ TTC 0; flat ⇒ null; <10 pts ⇒ persistence | PASS | unit probe |
| C085 | Forecast | Saturating curves not over-extrapolated | FAIL | plateau at ~0.9 forecast to 1.52 @900 s, TTC 40 s |
| C086 | Forecast | Beats persistence at 900 s in live runs | PARTIAL | early window: 0.144 vs 0.177 (better); long run: worse on all 10 watched entities; `/metrics` −154.7% at cycle 238 |
| C087 | Forecast | Hero TTC stable | PARTIAL | `metro_b` TTC 1752 → 1414 → 794 → 813 → 903 → 1270 → 1385 s over cycles 10–24 |
| C088 | Risk | Score in 0–100, bands per thresholds | PASS | table §12 |
| C089 | Risk | Utilisation ≥1.0 classified critical | FAIL | 1.0 → 50 moderate; 1.2 → 60 moderate; 1.5 → 75 high |
| C090 | Risk | Growth + cascade terms contribute as documented | PASS | table §12 |
| C091 | Anomaly | z-score math / threshold / min history | PASS | spike z = 4.34; <5 pts ignored; constant ignored |
| C092 | Anomaly | Delivered over WS | PASS | 13–14 per ~90 cycles |
| C093 | Anomaly | Rendered in UI | FAIL | stored in the store, never rendered |
| C094 | Anomaly | Operationally useful | PARTIAL | fires on twin-residual noise (e.g. `road_10` z −3.08), no linked action |
| C095 | GNN | Artifact exists; shapes match code; normalisation loads | PASS | 58,820 params; keys/shapes listed §13 |
| C096 | GNN | Runs within budget | PASS | 0.73 ms forward; `predict_all` 66 roots 1.5 ms |
| C097 | GNN | Deterministic; `/health` reports `gnn` | PASS | |
| C098 | GNN | Calibrated to load | FAIL | at 30% load 24/66 nodes p > 0.6; at 5% load 6 still p > 0.6 |
| C099 | GNN | Downstream steps conditioned on the root | FAIL | same entity ⇒ identical probability in every root's cascade |
| C100 | GNN | Demo chain to `emergency_north` responds to `metro_b` | FAIL | Δp(`road_4`) = −0.012, Δp(`emergency_north`) = 0.0 |
| C101 | GNN | Sensible root count | FAIL | 42 roots at cycle 52, 60 at cycle 600, incl. hotels/zones/parking/venue |
| C102 | GNN | Edge-type semantics | FAIL | steps via `substitutes_for` (metro_b → metro_d) and hotel → road chains |
| C103 | GNN | Precision/recall bookkeeping is measured | PASS | engine counts alerts/events |
| C104 | GNN | Recall acceptable | FAIL | 0.015–0.029 (baseline shown 0.30) |
| C105 | GNN | Fallback on NaN / exception / bad normalisation file | PASS | all → `deterministic`, identical keys |
| C106 | GNN | Timeout fallback | UNVERIFIED | 150 ms budget never exceeded; not forced |
| C107 | Optimiser | Templates fire per root type | PASS | unit probe |
| C108 | Optimiser | Candidates relevant to the root problem | PARTIAL | global templates (zone/parking/hotel) attached to unrelated roots; road/emergency roots only get `notify_only` |
| C109 | Optimiser | `MAX_LIVE_PROPOSALS = 8` enforced | FAIL | 10 proposed simultaneously (cycles 106–118) |
| C110 | Optimiser | One proposal batch per root | PASS | |
| C111 | Optimiser | TTL usable at default speed | PARTIAL | 900 sim-s = **15 s wall** at 60× |
| C112 | Optimiser | `accommodation_rebalance` triggers in a live run | FAIL | never in 600 cycles (hotel peak 0.856 < 0.88) |
| C113 | Optimiser | Rank uses verdict | PASS | STABLE 35% → 0.87; UNSTABLE 35% → 0.13 |
| C114 | Equilibrium | Single verdict implementation, rules as specified | PASS | `row_verdict`/`derive_verdict` only in `equilibrium.py` |
| C115 | Equilibrium | Deterministic | PASS | |
| C116 | Equilibrium | Convergence independent of relief size | FAIL | ≥8% relief never converges (40 iters) ⇒ UNSTABLE |
| C117 | Equilibrium | Verdict distribution plausible | FAIL | live 109/115 UNSTABLE; real DB 1,166/1,177 |
| C118 | Equilibrium | Reroute UNSTABLE for the stated reason (gate_5 overload) | FAIL | reason "Did not converge within iteration cap."; peak 0.878 at `metro_d` |
| C119 | Equilibrium | Unrelated pre-existing overload does not condemn an action | FAIL | `zone_core` 1.05 ⇒ `notify_only` on `road_9` UNSTABLE |
| C120 | Equilibrium | "Exceeds capacity within N minutes" derived from simulation | FAIL | `26 − overshoot·100` formula |
| C121 | Equilibrium | Certificate accuracy | FAIL | 0/38 within 15% (live); 0.0% on `/metrics` |
| C122 | Equilibrium | Stackelberg leader search | FAIL | `solve_leader()` returns `[]` |
| C123 | What-if | All scenario jobs complete | PASS | 15/15 in <0.25 s |
| C124 | What-if | Live state untouched | PASS | `/state` byte-identical before/after |
| C125 | What-if | Values numerically sane | FAIL | baseline peak 2,048% (c60) / 3,073% (c12) / 8,055% (rehearsal) |
| C126 | What-if | Gate closure / outage plausible | FAIL | 96,405% / 37,258% |
| C127 | What-if | Capacity scenarios have an effect | FAIL | Blue Line −15%: −0.0%; `metro_b` −50%: −0.0%; `road_4` −50%: 0.0% |
| C128 | What-if | Hotel shortage has an effect | FAIL | Δpeak 0.0%, Δvar −0.2% |
| C129 | What-if | Candidates depend on the scenario | FAIL | 9/15 scenarios give identical `metro_e` candidates |
| C130 | Lifecycle | Approve ⇒ executing + nudges + audit + WS | PASS | A/B + WS |
| C131 | Lifecycle | Reject has no side effects | PASS | trace identical to do-nothing for 120 cycles |
| C132 | Lifecycle | Expire after TTL | PASS | 900 sim-s |
| C133 | Lifecycle | Approval changes the world | PASS | metro_b 0.901 → 0.582 |
| C134 | Lifecycle | Reroute destination gains demand | FAIL | metro_c 0.479 → 0.306 (−36%) |
| C135 | Lifecycle | Effect models the intervention | FAIL | instant 35% cut in one 30-s cycle; `notify_only` cuts real demand 4% |
| C136 | Settlement | Fires ≥900 sim-s after approval | PASS | cycle 49 → 79 (14.2 s wall) |
| C137 | Settlement | Counterfactual meaningful | FAIL | do-nothing branch predicts metro_b 9.15 (915%); cf clamps to −100% |
| C138 | Settlement | Regret meaningful | FAIL | measures change vs approval time; event growth dominates (notify: realised −15.5%) |
| C139 | Lifecycle | Approval authorisation | PARTIAL | any `operator_id` string accepted; no auth (by design) |
| C140 | Nudge | 3 nudges issued on approval | PASS | |
| C141 | Nudge | Content consistent with action | FAIL | notify ⇒ "Switch to Metro B Station … 1 minutes further, Rs 0 credit" |
| C142 | Nudge | Accept/decline recorded + persisted + audited | PASS | |
| C143 | Nudge | Response feeds the solver/simulation | FAIL | `observed_compliance` appended, never read |
| C144 | Nudge | Expiry | FAIL | still `pending` 24 sim-min after `expires_at` |
| C145 | Attendee | Outbound hotel → stadium routes | PASS | 7 combos |
| C146 | Attendee | Recommended route avoids crowding when it matters | PASS | avoids gate_5 (high) via gate_6 |
| C147 | Attendee | Return journey | FAIL | stadium → hotel/metro: "No route" |
| C148 | Attendee | UI origin/destination selection | FAIL | hard-coded `hotel_core_cluster` → `stadium_main` |
| C149 | Attendee | Step-free mode changes the route | FAIL | identical legs |
| C150 | Attendee | "Shortest" labelled worse only when worse | FAIL | always red "Fastest on paper — … pinch points" even when both are MODERATE |
| C151 | Attendee | Zone recommendation dynamic and correct | FAIL | "LOW DENSITY" hard-coded; recommended the congested Metro B (113%) |
| C152 | Attendee | Live updates | FAIL | journey fetched once on mount/toggle |
| C153 | Attendee | Accept/decline in UI | PASS | "Thanks — your route is updated.", backend `accepted` |
| C154 | Commander | Deterministic template engine (no LLM) | PASS | `engine: deterministic` |
| C155 | Commander | 4 scripted questions answered | PASS | |
| C156 | Commander | Every number traceable to a tool result | PASS | 0 ungrounded across 18 queries |
| C157 | Commander | Grounding validator is strict | PARTIAL | digits inside IDs (`gate_3` ⇒ "3") ground numbers trivially |
| C158 | Commander | Hotel/transport/attendee/metrics/what-if intents | FAIL | all → generic "Core Emergency Post" answer |
| C159 | Commander | Entity resolution | FAIL | "road_12" answered about `road_1` |
| C160 | Commander | No false claims | FAIL | "The most urgent is Gate 5 / Blue Line" when merely named |
| C161 | Commander | Cache cleared on reset | FAIL | pre-reset answer served at cycle 3 as `is_cached:true` |
| C162 | Commander | Unsupported / garbage / injection queries handled | PARTIAL | 200 with a generic, irrelevant answer; no crash |
| C163 | Commander | `propose_action` never executes; only 8 tools | PASS | pytest + code |
| C164 | Metrics | forecast_mae measured | PASS | real residuals |
| C165 | Metrics | cascade_precision measured | PASS | 0.36–0.73 |
| C166 | Metrics | cascade_lead_time is a measured lead | FAIL | = max predicted ETA of active cascades |
| C167 | Metrics | Twin RMSE improvement meaningful | FAIL | vs exploding strawman |
| C168 | Metrics | ensemble_coverage measured | FAIL | `clamp(0.85 + spread, 0.80, 0.95)` formula |
| C169 | Metrics | peak_utilisation_reduction meaningful | FAIL | 84.1% after a 4% notify, from the exploded counterfactual |
| C170 | Metrics | load_variance_reduction meaningful | PARTIAL | 0.0/−0.1%; the underlying variance is twin-inflated |
| C171 | Metrics | convergence_rate / unstable_caught honest | PARTIAL | real counts, but driven by the C116 artefact |
| C172 | Metrics | tool_call_correctness meaningful | PARTIAL | = "tool didn't raise"; 0.0 (target 0.9) before any query |
| C173 | Metrics | Baselines measured | PARTIAL | precision/recall/lead-time baselines are constants 0.31/0.30/0.0 |
| C174 | Metrics | cycle_latency | PASS | 29–49 ms |
| C175 | FE-live | Command Centre loads with live data | PASS | T18 |
| C176 | FE-live | Map renders nodes, bands, labels | PASS | screenshot (SwiftShader) |
| C177 | FE-live | Entity detail + forecast + breakdown + cascade arcs | PASS | `GET /state/metro_b`, `/cascade/metro_b` |
| C178 | FE-live | Pressure timeline reflects real urgency | PARTIAL | hero row = twin artefact 200%; "Arterial 13 105% MODERATE" |
| C179 | FE-live | Queue order = backend `rank_score` | PASS | 7 cards identical order |
| C180 | FE-live | Approve via UI hits backend | PASS | toast "Approved — 3 nudges issued"; backend executing |
| C181 | FE-live | Action HUD values correct and labelled | PARTIAL | "do-nothing −100pp"; relative % labelled "pp" |
| C182 | FE-live | Twin gauge + drift toggle | PARTIAL | works; shows the misleading "RMSE REDUCTION 91.5%" |
| C183 | FE-live | Commander UI | PARTIAL | works; "CACHED INFERENCE" on first ask (stale cache) |
| C184 | FE-live | What-if UI | PARTIAL | works; displays "peak util Δ +616.8%" |
| C185 | FE-live | `/metrics` page renders | PASS | numbers are those in §20 |
| C186 | FE-live | No app console errors | PASS | only router future-flag warnings + favicon 404 |
| C187 | FE-live | Honest status copy | PARTIAL | "LIVE FEED" for a simulation; "Autonomous Dispatch Ready"; "…EnKF assimilation guarantees" |
| C188 | FE-live | Demo controls in UI | FAIL | none (curl only) |
| C189 | FE-mock | Approve in mock flips status + honest HUD note | PASS | "Executed (mock demo — live simulation not running)" |
| C190 | FE-mock | What-if differs per preset | FAIL | all presets Δ +24.1% / +62.7% |
| C191 | FE-mock | Unscripted Commander query labelled honestly | PARTIAL | "No grounded data…" shown with TOOL GROUNDED + CACHED chips |
| C192 | FE-mock | No console errors | PASS | |
| C193 | FE-mock | Mock fixtures numerically sane | FAIL | `gate_2` 200%, sim baseline 7,250%, recall 0.032, cert accuracy 0 |
| C194 | FE-mock | Committed mocks reproducible from current code | PARTIAL | only the 13 unobserved entities differ, from frame 10 on |
| C195 | Build | Production build targets the live backend | FAIL | `VITE_MOCK` unset ⇒ `MOCK_MODE` true; bundle ships mock replay |
| C196 | Build | Bundle size | PARTIAL | 4.0 MB JS (mocks bundled) |
| C197 | Determinism | Identical runs identical | PASS | 150 cycles, all fields |
| C198 | Determinism | Three demo rehearsals identical | PASS | T25 |
| C199 | Determinism | Approval perturbs only its targets | PARTIAL | also shifts twin noise: load variance 0.29 vs 0.09 at cycle 119 |
| C200 | Perf | Cycle within budget | PASS | p50 29 ms, p95 36 ms, max 87 ms |
| C201 | Perf | No fallback/backpressure triggered | PASS | 0 log lines |
| C202 | Perf | Concurrency | PASS | 200 mixed requests in 0.11 s, all 2xx |
| C203 | Perf | Cycle cost flat over time | PARTIAL | 13.7 → 33.6 ms mean (all interventions re-merged every cycle) |
| C204 | Load variance | Displayed `load_variance` reflects reality | FAIL | 0.2388 displayed vs 0.0467 truth (cycle 200) |
| C205 | Schedule | Schedule-change support | FAIL | no API or UI; times fixed in config |
| C206 | Off-peak | Off-peak recommendation exists and is dynamic | FAIL | only static "Delay … 12 minutes" text in `stagger_entry` |

**Totals (206 checks): PASS 93 · PARTIAL 38 · FAIL 73 · UNVERIFIED 2.** Counted mechanically from the Status column above.

---

## 5. Backend Results

| Aspect | Observed |
|---|---|
| Entry point | `run.py` → `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`. With `DATABASE_URL` pointing at the scratch DB, `/health` answered 200 after **4.84 s**. |
| Lifespan | `schema ready on sqlite:///…scratch…`, `cleared entity_state/risk_state/forecast`, registry resolution (cascade → `ML.cascade`, the other 7 → reference; the harmless `ml` import warning is logged), `no TSFM available`, `HX-Cascade GNN loaded`, `topology seeded: 66 new entities, 120 edges, 5 segments`, `cycle started: seed=42 dt=30s speed=60.0x`. |
| Runtime warnings | `RuntimeWarning: divide by zero / overflow / invalid value encountered in matmul` (twin.py:121/126/132) on **every** cycle. Reproduced on a plain random 132×20 matmul with numpy 2.2.1 + Accelerate: the result is finite and equals `einsum` to 1.5e-11, so the warnings are **spurious log noise** (P3). |
| Cycle loop | 600 in-process cycles and several live runs with no "cycle failed". Wall pacing: 60× ⇒ 2.0 ticks/s measured; 150× and 300× both ⇒ 5.0 cycles/s (0.2 s floor). |
| Shutdown / restart | SIGTERM to the reloader stops cleanly; relaunch ready in 1.34 s at cycle 0, seq 0. |
| `--reload` hazards | (a) Any backend file save restarts the sim at cycle 0 (by design; triggers C040). (b) Killing only the worker left the reloader (PID 15158) **holding :8000 with no worker** — requests hung and a new server failed with `[Errno 48] Address already in use` until the reloader was killed. |
| Past `end_time` | No end-of-event handling. A live 238-cycle run showed 18 entities ≥100%. The real DB shows a prior run reaching sim time 2026-09-06 02:23 (≈36 sim-hours). |

## 6. API Results

The sweep sent 74 requests. Only the endpoints and behaviours worth recording are listed; all other valid requests returned 200/202 with schema-valid bodies.

| Endpoint | Method | Purpose | Request tested | Expected | Actual | HTTP | Schema | Working | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `/` | GET | service info | plain | 200 | service JSON | 200 | n/a | PASS | |
| `/api/v1/health` | GET | readiness | plain | 200 | forecaster `persistence` → `local_model` after 10 cycles, cascade `gnn`, commander `deterministic` | 200 | ✓ | PASS | twin/equilibrium carry no `active_source` |
| `/event` | GET | event | plain | 200 | `concurrent_events: []` always | 200 | ✓ | PASS | |
| `/graph` | GET | topology | plain | 200 | 66/120/5 | 200 | ✓ | PASS | |
| `/state` | GET | live state | plain | 200 | 66 entities (13 unobserved) | 200 | ✓ | PARTIAL | values for unobserved entities wrong (§10) |
| `/state/{id}` | GET | detail | `metro_b`, `zone_core`, `nope_404` | 200/200/404 | as expected | 200/404 | ✓ | PASS | |
| `/forecast` | GET | forecasts | right after reset; all; `?entity_id=metro_b&entity_id=gate_5`; `nope` | 422/200/200/404 | `MODEL_NOT_READY` / ✓ / ✓ / `ENTITY_NOT_FOUND` | as expected | ✓ | PASS | |
| `/forecast/pressure-timeline` | GET | urgency list | plain | 200 | sorted by TTC | 200 | ✓ | PARTIAL | top rows are twin artefacts |
| `/cascade/active` | GET | cascades | plain | 200 | 42 roots, 33 with ≥1 downstream (cycle 52) | 200 | ✓ | PARTIAL | §13 |
| `/cascade/{id}` | GET | one cascade | `metro_b`, `hotel_airport_cluster` (on demand), `nope` | 200/200/404 | ✓ | as expected | ✓ | PARTIAL | hotel root cascades into roads |
| `/interventions` | GET | queue | default; `all&limit=100`; `status=bogus`; `limit=0`; `limit=abc` | 200/200/400?/400/400 | ✓/✓/**200 []**/400/400 | | ✓ | PARTIAL | unknown status unvalidated |
| `/interventions/{id}` | GET | one | valid / missing | 200/404 | ✓ | | ✓ | PASS | |
| `/interventions/{id}/approve` | POST | execute | missing id; no body; malformed; extra field; after reject; double approve; after expiry | 404/400/400/400/409/409/409 EXPIRED | ✓…; after expiry ⇒ **409 `INTERVENTION_ALREADY_RESOLVED`** | | ✓ | PARTIAL | no auth; see §16 |
| `/interventions/{id}/reject` | POST | reject | valid, repeat, missing | 200/409/404 | ✓ | | ✓ | PASS | |
| `/certificates/{id}` | GET | certificate | valid, rejected card, missing | 200/200/404 | ✓ | | ✓ | PASS | verdict semantics §15 |
| `/simulate` | POST | what-if | empty list; horizon 30; 99999; unknown type; malformed; **unknown entity `gate_99`**; valid | 400/400/400/400/400/**400**/202 | …/**202** | | ✓ | FAIL | unknown entity accepted; results §17 |
| `/simulate/{id}` | GET | result | valid / missing | 200/404 | ✓ | | ✓ | FAIL | numerically invalid content |
| `/twin/fidelity` | GET | twin | before first cycle / normal | 422/200 | ✓ | | ✓ | PARTIAL | content §10 |
| `/twin/drift-mode` | POST | drift | `"maybe"`, `{}` | 400/400 | ✓ | | ✓ | PASS | |
| `/regret` | GET | ledger | plain | 200 | ✓ | 200 | ✓ | PARTIAL | cf −100 |
| `/metrics` | GET | KPIs | plain | 200 | ✓ | 200 | ✓ | PARTIAL | §20 |
| `/commander/query` | POST | Q&A | blank; missing; valid; 20,000-char | 400/400/200/200 | ✓ | | ✓ | PARTIAL | §19 |
| `/attendee/journey` | POST | routing | valid; unknown origin; unknown segment; **stadium → hotel**; **origin = destination** | 200/404/400/200/200 | ✓/✓/✓/**404**/**404** | | ✓ | PARTIAL | return trips impossible |
| `/attendee/nudges` | GET | nudges | valid; no param; unknown attendee | 200/400/200 | ✓ | | ✓ | PASS | |
| `/attendee/nudges/{id}/respond` | POST | respond | missing; double | 404/409 | ✓ (code `INTERVENTION_ALREADY_RESOLVED`) | | ✓ | PARTIAL | response never consumed |
| `/demo/control` | POST | control | bad action; bad inject; **speed −5**; **speed 0**; **seek "not-a-time"**; empty; reset ×8 | 400/400/400/400/400/200/200 | ✓/✓/**200**/**200**/**500**/✓/✓ | | ✓ | FAIL | speed ≤0 freezes the engine (C080) |
| unknown route | GET | — | `/api/v1/does-not-exist` | 404 | 404 code **`ENTITY_NOT_FOUND`** | 404 | ✓ | PARTIAL | |
| wrong method | DELETE | — | `/api/v1/state` | 405 | 405 code **`INTERNAL_ERROR`** | 405 | ✓ | PARTIAL | |

## 7. WebSocket Results

Test: five raw sockets for about 45 s at 60× (`command_centre`, `attendee&att_demo_1`, `attendee&att_other`, no query string, plus a dedicated malformed-sender). During the run a `metro_b` notify card was approved, both attendees requested journeys, and settlement completed.

| Aspect | Result |
|---|---|
| Initial event | `resync` on every socket (PASS). |
| Envelope | `event/sim_time/seq/payload` on 100% of messages (PASS). |
| seq | Global, strictly monotonic per socket; gaps only from filtering (PASS). |
| Per-cycle | 89–92 each of tick / state_update / forecast_update / twin_fidelity over ~45 s (≈2/s); 0 non-consecutive ticks (PASS). |
| Sizes | tick 246 B; state_update **12.1 KB (all 66 entities every cycle)**; forecast_update 10.8 KB; twin_fidelity 1.8 KB; resync 90.6 KB at ~cycle 100. |
| cascade_alert | **307 alerts, 48 unique roots in ~90 cycles.** Repeats: `hotel_airport_cluster` 16, `road_2` 10, `hotel_north_cluster` 7, `road_5` 7, `parking_p1` 7… Roots flap in and out of "active", so the "new cascade only" rule re-fires (FAIL). |
| Lifecycle events | `intervention_queued` 18, `intervention_resolved` {expired 12, executing 1, completed 1}, `regret_update` 1 (identical to the in-process A/B), `nudge_pushed` 3, `journey_risk_update` 2, `anomaly` 13–14 (PASS). |
| Attendee filter | `att_demo_1` got its nudge + its journey update; `att_other` got only its own journey update (PASS). |
| ping / resync | `pong` received; `{action:'resync'}` answered (PASS). |
| Malformed frame | Sending `not json at all` ⇒ server logs `eventflow.ws websocket error` (JSONDecodeError), removes the connection from fan-out, but **does not close it**. The client stays `readyState 1` and receives nothing more (FAIL, zombie). The real frontend only sends valid JSON, so this is P2. |
| Reconnect (same process) | New socket gets resync and the stream continues with higher seq (PASS). |
| **Backend restart** | Browser test (T20): UI at cycle 59/60 → backend killed and relaunched → the TopBar chip went "RECONNECTING" and then cleared. The browser kept receiving frames (308 → 539 frames), but the **UI stayed at CYCLE 60 / 14:30 for 16 s while the backend advanced to cycle 33**. Root cause (verified in code): `ws.js:70` drops `seq <= lastSeq`, and `lastSeq` is never reset; the new server restarts `seq` at 1, so every message — including the reconnect `resync` — is discarded. A manual page refresh recovers (FAIL, P0). |

## 8. Database Results

The scratch DBs were `audit_live.db`, `h1.db`, `lc_*.db` and `det_*.db`. The real DB was only queried with `sqlite3 -readonly`.

| Table | Written? | Read back? | Survives restart? | Cleared on reset/start? | Authoritative? | Evidence |
|---|---|---|---|---|---|---|
| entity / graph_edge / segment | seeded once (idempotent) | no | yes | no | no (code topology is) | 66/120/5 |
| entity_state | every cycle (66 rows) | no | no | yes | no | 39,600 rows / 600 cycles |
| forecast | every cycle (198 rows) | no | no | yes | no | 118,800 / 600 cycles |
| risk_state | **never written** (only cleared) | no | — | yes | — | 0 rows |
| cascade_prediction | **never written** | no | — | no | — | 0 rows |
| execution | **never written** (approval data lives only in memory) | no | — | no | — | 0 rows |
| intervention | upsert every cycle (**all** of them) | no | yes | no | no | 115 / 600 cycles |
| certificate | upsert every cycle, PK `cert_` + 16-bit hash | no | yes | no | no | real DB: **1,177 certs for 1,184 interventions, 7 orphans** ⇒ later certificates overwrote earlier ones on ID collision |
| regret_entry, nudge | upsert every cycle | no | yes | no | no | 1 / 3 rows in the A/B run |
| audit_log | approve / reject / nudge_response / demo_control | no | yes | no | yes (only record) | 60 rows in the live scratch DB |
| commander_log | every non-cached query | no | yes | no | yes | 23 rows |

**Runtime vs persisted disagreement.** After a restart the in-memory queue, twin, cascades and metrics are gone, but the persisted intervention/certificate/regret rows from earlier runs remain and are never reloaded. The real DB (read-only) shows the scale:
- 288,222 `entity_state` rows and 864,666 `forecast` rows from one run spanning 2026-09-04 14:00:30 to 2026-09-06 02:23:30.
- 1,184 interventions: 1,167 expired, 15 proposed, 2 completed.
- Certificates: 1,166 UNSTABLE, 9 STABLE, 2 CONDITIONAL.
- Regret entries: 3, of which 2 have `counterfactual_relief_pct = −100`.
- DB size: **213 MB**.

## 9. Simulation Results

**How entities move.** They follow **independent scripted curves**:

`util(t) = base + amp·logistic((t − t_mid)/tau) + exit_spike(t)`, multiplied by global and per-entity multipliers, plus deterministic hash noise (`generator.py:123-144`).

No people move across edges. Edges are used only by `inject()` and `apply_relief()` to choose which multipliers to change, and by downstream ML.

| Item | Observed |
|---|---|
| Hero `metro_b` | 0.523 (14:00) → 0.600 (cycle 11) → 0.901 (cycle 49, 14:24) → 1.097 (cycle 80) → 1.25 plateau → **1.51–1.61 from ~16:40** (exit spike). |
| Exit spike timing | Starts at `exit_spike_start_min = 150` ⇒ 16:30 sim time, i.e. **30 minutes into a match that ends at 18:30**. |
| Ground-truth bounds | Truth is clipped at 1.6; e.g. `road_13` 1.60, `metro_b` 1.60 after 16:40. |
| Critical count (displayed) | cycle 10: 0 · 50: 1 · 100: 5 · 200: 8 · 400: 21 · 600: 18 (of 66). |
| Unobserved entities (13) | Values come from the twin (§10); they dominate "extreme" readings. |
| Load variance (PS "uneven distribution") | Displayed vs **truth** zone variance: cycle 50: 0.0182 vs 0.0167 · 100: 0.0762 vs 0.0315 · 150: 0.0984 vs 0.0434 · **200: 0.2388 vs 0.0467**. The inflation comes from the unobserved zones (`zone_concourse_south` 0.0 vs 1.09 truth; `zone_core` 2.0 vs 0.64). |
| Live scenario injection (`/demo/control inject`) | `metro_capacity_delta metro_b −50%` ⇒ **metro_b utilisation 0.8205 → 0.4245** (capacity cut *lowers* utilisation, because generator count = util × reduced capacity while the store divides by nominal capacity). `gate_closure gate_3` ⇒ gate_3 0.406 → 0.0; gate_1 0.276 → 0.334, gate_5 0.461 → 0.585, gate_6 0.360 → 0.447 (+~20% each); adjacent `road_4` 0.323 → 0.335 (no downstream effect); stadium 0.257 → 0.270. |
| Other live injections | Not injected live (UNVERIFIED); what-if equivalents in §17. |
| Seek | `seek 16:00` ⇒ clock jumps, `cycle_number` resets to 0, forecaster falls back to `persistence`, history/interventions/cascades retained, `metro_b` 1.27. `seek 13:00` (before start) ⇒ 200, clock shows 13:00 while the generator clamps elapsed to 0. |

## 10. Digital Twin / EnKF Results

**Classification:** REAL ALGORITHM (stochastic EnKF with perturbed observations and multiplicative inflation) on a **SIMPLIFIED surrogate model**. Its outputs for unobserved entities are **not reliable enough for downstream decisions**.

| Property | Observed |
|---|---|
| Dimensions | m = 20 members; state 132 = 66 counts + 66 flows; 53 observed / 13 unobserved (fixed per seed). |
| NaN/Inf | 0 across 600 cycles (warnings are BLAS noise). |
| Observed entities | Display uses raw observation (RMSE 0.003–0.008 utilisation), **not** the twin mean. The twin mean on observed entities has RMSE 0.13–3.1 utilisation. |
| **Unobserved estimates** (display value, truth) | cycle 30: `emergency_core` **2.000** / 0.317 · cycle 60: `gate_2` 0.000 / 0.283, `road_6` 1.154 / 0.439 · cycle 100: `zone_concourse_south` **0.017 / 0.944**, `road_13` 1.567 / 0.908 · cycle 200: `parking_p2` 0.000 / 0.817, `road_9` 0.000 / 1.033, `zone_core` 2.000 / 0.643 · cycle 600: 8 of 13 at exactly 0.000 or 2.000 (the clamp). |
| Ensemble std on those | ≈0.0–2.0 counts while wrong by hundreds (overconfident divergence). |
| **Utilisation RMSE on unobserved** (cycle: twin / uncorrected copy / static initial count) | 20: 0.262 / 0.167 / 0.050 · 40: 0.617 / 0.162 / 0.121 · 60: 0.830 / 0.211 / 0.200 · 100: **11.19 / 0.87 / 0.35** · 120: 10.47 / 2.08 / 0.41 |
| Assimilated RMSE (counts, all entities vs truth) | 92.7 (c1) → 176 (c10) → 775 (c50) → 1,293 (c100) → 7,112 (c200) → 10,415 (c600). |
| Drift mode | Enabled at cycle 20: `improvement_pct` 79.8 → 96.5%. The "uncorrected" ensemble is advanced with ×1.05 inflation and no clipping after inflation, so it reached a **min count of −25,411**. The improvement is measured against a diverging strawman, while on unobserved entities the uncorrected copy is actually **better** than the assimilated one (row above). UI shows "RMSE REDUCTION 91.5%". |
| Flow rows | Reach \|flow\| up to 92,395 counts/min (analysis step `twin.py:136` writes `(X − HX)·2` into flows). |
| `branch()` | Empty scenario at cycle 120: baseline peak **53.63 (5,363%)** at `emergency_south`, 29 entities ≥0.90. `demand×0.8` on metro_b: 0.725 → 0.383 → 0.125 → 0.004 → 0 → 0 (compounding `twin.py:271` each step plus the flow surrogate). `capacity×0.01` on gate_3: 177 → 572 utilisation. It does not mutate the store (PASS), but it **consumes the live RNG** (FAIL). |
| Root cause (inference, consistent with the evidence above) | Unobserved rows are updated only through spurious sample cross-covariances (20 members vs 53 observations); flows absorb doubled analysis increments; the surrogate `count += flow·dt` with 300-s steps in branches amplifies those flows. |

## 11. Forecasting Results

**Classification:** STATISTICAL (persistence under 10 points, then ridge-shrunk quadratic trend). The TSFM path is unreachable (Chronos not installed).

| Case (unit probe) | Output | Assessment |
|---|---|---|
| empty / <10 pts | persistence, TTC null (even when rising) | PASS (by design) |
| already ≥0.90 | TTC 0 | PASS |
| flat 0.4 | 0.4 at all horizons, TTC null | PASS |
| linear +0.01/step from 0.5 | 900 s 0.979 (true 0.99), 1800 s 1.207 (true 1.29), TTC 655 s (true 630 s) | PASS |
| falling | 0.42 / 0.19 / 0.0 | PASS |
| noisy flat | bands widen; model MAE −3.8% vs persistence (reported honestly) | PASS |
| **logistic saturating at ~0.9** | 900 s **1.518**, 1800/3600 s **1.60** (clip), TTC 40 s | **FAIL**: extrapolates convexity past saturation ⇒ false alarms |

**Live accuracy** (900-s forecast against the actual value 30 cycles later, 560 samples per entity over the 600-cycle run; model MAE / persistence MAE):

| Entity | Model MAE | Persistence MAE |
|---|---|---|
| `metro_b` | 0.058 | 0.057 |
| `gate_3` | 0.087 | 0.060 |
| `gate_5` | 0.085 | 0.069 |
| `road_4` | 0.099 | 0.052 |
| `stadium_main` | 0.042 | 0.032 |
| `emergency_north` | 0.058 | 0.022 |
| `hotel_north_cluster` | 0.045 | 0.016 |
| `parking_p1` | 0.067 | 0.033 |
| `line_blue` | 0.060 | 0.059 |
| `zone_core` (unobserved) | 0.355 | 0.120 |

- The model is **never better than persistence** over the whole run.
- `/metrics forecast_mae` read at different times: cycle ~80: 0.144 vs 0.177 (+18.6%) · UI at 14:52: 0.073 vs 0.102 (+28.6%) · cycle 238: 0.1187 vs 0.0466 (**−154.7%**).
- Directionally the forecasts are sensible during the ramp and wrong at plateaus.
- Hero TTC wobble: 1752 / 1414 / 794 / 813 / 903 / 1270 / 1385 / 514 s over cycles 10–28. The actual crossing was at cycle 49 (≈1,320 s after cycle 10's 1,752-s prediction).

## 12. Risk Results

**Classification:** DETERMINISTIC. `score = 0.5·100·u + 0.3·100·max(0, f1800 − u)·1.5 + 0.2·100·cascade`.

Each column after the first is a score with its band. Scenarios: flat forecast, forecast +0.2 (growing), full cascade exposure, and forecast −0.2 (falling).

| util | flat | +0.2 growth | full cascade | falling |
|---|---|---|---|---|
| 0.1 | 5 low | 14 low | 25 low | 5 low |
| 0.6 | 30 low | 39 moderate | 50 moderate | 30 low |
| 0.8 | 40 moderate | 49 moderate | 60 moderate | 40 moderate |
| **0.9** | **45 moderate** | 54 moderate | 65 high | 45 moderate |
| **1.0** | **50 moderate** | 59 moderate | 70 high | 50 moderate |
| **1.1** | **55 moderate** | 64 high | 75 high | 55 moderate |
| **1.2** | **60 moderate** | 69 high | 80 high | 60 moderate |
| 1.5 | 75 high | 84 critical | 95 critical | 75 high |
| 2.0 | 100 critical | 100 critical | 100 critical | 100 critical |

- **Over-capacity is misclassified.** The utilisation term is capped at 50 points, so an entity at or past capacity with a flat forecast is "moderate".
- Live examples:
  - `gate_5` 105.2% moderate, `zone_east` 108.1% moderate (cycle 238).
  - "Arterial 13 105% MODERATE" in the UI.
  - `metro_b` 109.7% high (cycle 80); HUD "Metro B Station 113% HIGH".
- Bands also flicker: `metro_b` critical (c12) → high (c14–48) → critical (c60) → high (c80, 1.097) → critical (c100). This happens because the growth term appears and disappears with forecast noise.
- `overall = round(0.4·mean + 0.6·max)` over entity scores; the summary is a PASS mechanically.

## 13. GNN / Cascade Results

| Level | Verdict | Evidence |
|---|---|---|
| **Model loads** | PASS | `ML/hx_cascade.pt` loads with `weights_only=True`. Shapes: input_proj 64×12; convs.{0,1}.weight 6×64×64 + root 64×64; LayerNorm 64; head_failure 3×64; head_ttc 1×64. 58,820 params. Dummy forward passes; `/health` shows `gnn`. |
| **Model runs** | PASS | 0.73 ms per forward (p50; p95 0.76); `predict_all` with 66 roots 1.5 ms; deterministic; never exceeded the 150 ms budget; CPU (MPS available, unused). |
| **Model output is valid** (schema, ordering) | PASS | Steps sorted by ETA, root first; schema-valid. |
| **Model output is semantically valid / useful** | **FAIL** | See below. |

Semantic problems, all measured:
1. **Calibration.** With every node at 30% utilisation, 24/66 nodes have p(fail@3600 s) > 0.6 (all parking, all lines, the stadium, 6 zones, and the transport hubs `bus_hub_north`, `bus_hub_south`, `shuttle_hub_west`). At **5%** load, 6 nodes are still > 0.6. At 95% load, 63 are.
2. **Not root-conditioned.** One forward pass per cycle; each step's probability is the destination node's own marginal. The same entity has identical probability in every root's cascade (tested: `True`). "Cascade from X" is therefore "which of X's graph descendants are independently high-probability".
3. **Demo chain insensitivity.** Raising `metro_b` from 0.3 to 1.2 (others 0.3): Δp `gate_3` +0.38, `gate_4` +0.27, **`road_4` −0.012, `emergency_north` 0.000**. Meanwhile `zone_east` −0.97 and `zone_north` −0.78 (more load at the station lowers the zones' failure probability).
4. **Root explosion.** Roots are any node whose current or forecast band is high or critical, of **any** type: 28 roots by cycle 10 (the "calm opening"), 42 at cycle 52 (road 9, transport 9, zone 7, parking 5, hotel 4, gate 3, emergency 2, route 2, venue 1), and 60 at cycle 600. Downstream steps total 72 → 335.
5. **Edge semantics.** Emitted steps traversed `adjacent_to` 146, `evacuates_to` 24, `feeds` 21, `substitutes_for` 13, `last_mile_to` 4, `serves` 2 (cycle 52). Examples:
   - `metro_b → metro_d` via `substitutes_for` (a substitute receiving riders is labelled a failure).
   - `hotel_airport_cluster` (67%) → `road_1` → `emergency_core` via hotel → transport → gate → road chains.
6. **The demo narrative doesn't hold.** `metro_b`'s cascade at cycle 52: gate_3 (558 s) → road_3 → road_18/road_5 → … → `emergency_core` (1,987 s, p 0.99, itself a twin artefact at 200%). It is not `road_4 → emergency_north`.
7. **Online quality.**
   - Precision: 0.726 (600-cycle run), 0.394 (live cycle 238) and 0.36 (UI).
   - **Recall: 0.015 / 0.018 / 0.025–0.029**, against a displayed "random" baseline of 0.30.
8. **Alert flood.** 307 `cascade_alert` events in about 90 cycles (§7).

**Fallback (§15 of the brief).**
- Checkpoint path pointing to a missing file: the resolver silently falls back to the `ML/` directory, and the model still loads (source `gnn`).
- Forced NaN forward: all cascades switch to `deterministic`.
- Forced exception: `deterministic`.
- `feature_norm.json` with `node_feat_dim 13`: the load fails and the source is `deterministic` from boot.
- Fallback and GNN outputs have identical keys.
- The deterministic `metro_b` result under the same state is `metro_b → gate_3` (420 s).
- Timeout fallback: UNVERIFIED (never triggered, not forced).

## 14. Optimization Results

**Classification:** RULE-BASED templates. Relief, cost, delay and feasibility are HARDCODED constants or simple formulas (e.g. reroute `18 + 24·subst`, `notify_only` 4%, `gate_redistribution` 16.5%). Nothing is SIMULATED; `rank_score = relief·stability / (cost_norm·delay_norm)`.

| Root tested (0.95) | Candidates produced |
|---|---|
| transport `metro_b` | reroute → metro_c 35.3%, deploy_shuttle 10.5%, stagger_entry (gate_3, gate_4) 30%, notify 4% |
| gate `gate_3` | stagger_entry 26%, gate_redistribution 16.5%, notify |
| parking `parking_p1` | stagger_entry (gate_1) 26%, parking_redistribution → p5 11%, notify |
| zone `zone_core` (+ cold `zone_west`) | zone_incentive 14%, notify |
| hotel `hotel_core_cluster` | deploy_shuttle → metro_c 10.5%, accommodation_rebalance → hotel_north 9%, notify |
| road `road_4` | **notify only** |
| emergency `emergency_north` | **notify only** |
| gate_3 with hot parking/hotel/zone elsewhere | adds zone_incentive (zone_east → zone_west) and **accommodation_rebalance (hotel_north → airport)** to a gate problem |

**600-cycle live distribution.** 115 interventions:
- By type: emergency_corridor 23, stagger_entry 23, parking_redistribution 21, gate_redistribution 16, zone_incentive 14, notify_only 12, reroute 3, deploy_shuttle 3, **accommodation_rebalance 0**.
- Most frequent roots: `gate_1` 39, `gate_4` 25, `gate_3` 15, `metro_b` 10.
- The **first** triggers are twin-estimated entities: `road_9` at cycle 10, `road_13` at cycle 11, `emergency_core` at cycles 13 and 44.

**Other issues.**
- Live proposals reached **10** (`MAX_LIVE_PROPOSALS = 8` is checked before adding up to 5 more).
- TTL 900 sim-s ⇒ 14.9–15.2 s wall at 60× (measured in all three rehearsals).
- Intervention IDs are 20-bit; certificate IDs are 16-bit (collisions, §8).
- Copy is hard-coded, e.g. "activate a Rs 500 **North-zone** credit" for any gate.

## 15. Equilibrium / Stability Results

**Classification:** DETERMINISTIC game-theoretic follower iteration. Leader search is NOT IMPLEMENTED (`solve_leader()` → `[]`).

- `row_verdict` and `derive_verdict` exist only in `equilibrium.py` and implement the stated rules. Unit cases: (1.0, ·) ⇒ UNSTABLE; variance up ⇒ UNSTABLE; 0.9 ⇒ CONDITIONAL; else STABLE. Derive gives all-STABLE ⇒ STABLE, one UNSTABLE ⇒ CONDITIONAL, not converged ⇒ UNSTABLE. The solver is deterministic (PASS).
- **Relief sweep.** Reroute `metro_b → metro_c`, metro_b 0.95, others 0.5:

| Relief | Verdict | Converged | Iterations | Max utilisation |
|---|---|---|---|---|
| 2% | CONDITIONAL | yes | 1 | 0.949 |
| 4% | CONDITIONAL | yes | 26 | 0.947 |
| **8%** | **UNSTABLE** | **no** | 40 | 0.839 |
| 12–45% | UNSTABLE | no | 40 | 0.72–0.89 (at `gate_5`) |

  The follower loop re-drains the targets every iteration, so convergence requires the drained load to fall below the tolerance. The verdict is effectively "relief ≥ ~8% ⇒ UNSTABLE" regardless of where the load goes.
- **Gate_5 trap.** With metro_c and gate_5 at 0.85: 10% relief ⇒ max 1.04 at gate_5, 35% ⇒ 1.20. Both are UNSTABLE, but the certificate reason reads "Did not converge within iteration cap."
- **Live `metro_b` pair (cycle 52).**
  - `notify_only` is STABLE: converged in 18 iterations, peak 0.898 at metro_b.
  - The reroute is **UNSTABLE for non-convergence**, with its peak 0.878 at **metro_d**. Its sweep rows are 0.4 STABLE, 0.6 UNSTABLE and 0.9 UNSTABLE (variance 0.0236 → 0.0273).
  - The demo "saturation trap" story is therefore not what the certificate computes.
- **Unrelated overload condemns everything.** With `zone_core` at 1.05, a 4% `notify_only` on `road_9` is UNSTABLE: "At 40% compliance, Core Zone exceeds capacity within 21 minutes."
- **"Minutes to breach" is fabricated.** It comes from `clamp(round(26 − overshoot·100), 4, 45)`.
- **Distribution.** 600-cycle run: 109 UNSTABLE, 5 STABLE, 1 CONDITIONAL. Real DB: 1,166 / 9 / 2. Once twin artefacts at 2.0 enter the surface, even `notify_only` goes UNSTABLE (seen from cycle 44).
- **Ranking uses the verdict (PASS).** The highest-relief option is UNSTABLE and demoted every time (`unstable_caught` 16 in 600 cycles). This is structurally guaranteed by the convergence artefact.
- **Certificate accuracy** against `twin.branch`: **0 of 38** within 15%. The branch itself explodes (§10).

## 16. Intervention Lifecycle Results

A/B experiments ran in-process over 120 cycles with the same seed. The metro_b card pair appears at cycle 49 (sim time 14:24:30). Values are observed / truth.

| Cycle | metro_b: do-nothing | metro_b: approve reroute | metro_c: do-nothing | metro_c: approve reroute | gate_5 (both) |
|---|---|---|---|---|---|
| 49 (approve) | 0.901 / 0.909 | 0.901 / 0.909 | 0.481 | 0.481 | 0.525 |
| **50** | 0.901 / 0.912 | **0.582 / 0.589** | 0.479 | **0.306** | 0.545 = 0.545 |
| 59 | 0.971 | 0.625 | 0.530 | 0.341 | 0.607 = 0.607 |
| 79 (settle) | 1.083 | 0.702 | 0.606 | **0.389** | 0.703 = 0.703 |
| 119 | 1.200 | 0.776 | 0.694 | 0.446 | 0.897 = 0.897 |

- **The reroute removes demand from source and destination alike** (`generator.apply_relief` multiplies intensity by `1 − 0.353` for **both** targets `[metro_b, metro_c]`; `generator.py:246-249`, `optimiser.py:80`). metro_c *loses* 36% instead of gaining riders; gate_5 is untouched. The same mechanism applies to gate_redistribution, zone_incentive, parking_redistribution and accommodation_rebalance, whose receivers are also targets.
- The 35% cut lands **in a single 30-s cycle** (instant, exact, equal to the optimiser's asserted relief).
- **`notify_only` changes the world.** Approving "Notify operations team" sets `_intensity_mult[metro_b] = 0.96`: metro_b truth 0.912 → 0.875 (c50), 1.096 → 1.052 (c79).
- **Reject** produced a trace identical to do-nothing for all 120 cycles (PASS). **Expire** happened at cycle 79 (+900 sim-s); approving afterwards returns 409 `INTERVENTION_ALREADY_RESOLVED`.
- Double approve ⇒ 409 (PASS). Audit rows: `operator:op_audit approve int_5c0cd {"note":"audit"}` plus two `nudge_response` rows.
- **No authorisation:** any `operator_id` string is accepted.

**Settlement / counterfactual / regret (§20 of the brief).**

| Approval | predicted | realised | counterfactual | regret |
|---|---|---|---|---|
| reroute (35.3%) | 35.3 | 21.1 | **−100.0** | 14.2 |
| notify_only (4%) | 4.0 | **−15.5** | **−100.0** | 19.5 |

- The counterfactual comes from `twin.branch({},1800)` at approval: do-nothing `metro_b` = **9.1456 (915%)** and `metro_c` 10.88 at the settle index. It is clamped to −100%.
- "Realised" = (util at approval − util now) / util at approval. It measures the event's own growth (notify: −15.5% because the station kept filling), not the intervention's effect against a counterfactual.
- The counterfactual therefore does **not** come from the same model as the actual outcome (twin surrogate vs generator truth).
- The same regret entry arrived over WS (live) and matched the in-process result exactly.

## 17. What-If Results

**Classification:** SIMULATED on a twin **surrogate branch**, not on the generator. It never mutates live state (`/state` byte-identical before and after 15 jobs, PASS) but it **does** advance the live twin RNG (§24).

Snapshot at cycle 60, horizon 1800 s. Every job completed in under 0.25 s.

| Scenario | Baseline peak @ entity, crit | Scenario peak @ entity, crit | Δpeak / Δvar | New critical | Candidates |
|---|---|---|---|---|---|
| attendance +20% | **20.48 @ metro_e**, 31 | 43.27 @ metro_e, 40 | +111.3% / +343.7% | 9 | stagger, emergency_corridor, shuttle — all UNSTABLE |
| attendance −20% | 20.48, 31 | 9.37, 26 | −54.3% / −78.9% | 0 | same 3 |
| **Blue Line −15%** (UI preset) | 20.48, 31 | 20.48, 31 | **−0.0%** / +1.2% | 0 | same 3 |
| metro_b capacity −50% | 20.48 | 20.48 | **−0.0%** / +1.3% | 0 | same 3 |
| road_4 capacity −50% | 20.48 | 20.48 | **0.0%** / −0.0% | 0 | same 3 |
| rain light | 20.47 | 20.48, 33 | +0.1% / +6.9% | 2 | same 3 |
| **Heavy Rain** (UI) | 20.48 | 25.86 @ road_3, 36 | +26.3% / +75.6% | 5 | notify STABLE |
| **Gate 3 Closure** (UI) | 20.48 | **964.05 @ gate_3** | **+4,608%** / +64,889% | 1 | notify STABLE, stagger/corridor UNSTABLE |
| transport_outage metro_b | 20.48 | **372.58 @ metro_b** | +1,719% / +9,639% | 0 | reroute/stagger UNSTABLE |
| parking_loss p1 −50% | 20.48 | 20.49 | 0.0% / +8.3% | 0 | same 3 |
| **hotel_shortage** | 20.48 | 20.48, 32 | **0.0% / −0.2%** | 1 | same 3 |
| concurrent_event 25% | 20.49 | 51.80, 42 | +152.8% / +534.1% | 11 | same 3 |
| **Combined Stress** (UI) | 20.48 | 25.83 @ road_3 | +26.1% / +76.4% | 5 | notify STABLE |
| combined (nested gate_5 + hotels) | 20.48 | 31.30 @ gate_5 | +52.8% / +77.3% | 2 | notify/stagger/corridor |
| gate_closure **gate_99** (unknown) | 20.48 | 20.48 | 0.0% | 0 | same 3 — accepted silently |

- At **cycle 12** the baseline is already **30.73 (3,073%) @ emergency_north** with 25 entities critical.
- In the three rehearsals, Heavy Rain showed baseline **80.56** vs scenario 124.45.
- The UI displays these as "peak util Δ +616.8%" for Gate 3 and "+39.3%" for rain.
- Identical results for unrelated scenarios: 9 of 15 produce the same `metro_e` candidate triplet.
- Capacity scenarios and the hotel shortage have ~0 effect on the reported peak because the baseline peak (a surrogate artefact) dominates.
- Mock mode returns one fixture (Δ +24.1% / +62.7%) for every preset.

**Verdict.** What-if is a separate surrogate calculation, not a faithful simulation. Its outputs are numerically invalid (FAIL).

## 18. Attendee Results

At cycle 80, via the API (`attendee_id: att_audit`):

| Origin → destination (segment) | Result |
|---|---|
| hotel_core → stadium (price_sensitive) | risk 58 moderate; **recommended** metro_c → gate_6 → stadium (1,160 s, moderate) vs **shortest** metro_c → gate_5 → stadium (1,060 s, high); advice "…adds 1 minutes and avoids it" — PASS |
| hotel_core → stadium (**accessibility_constrained**) | **identical routes and risk** — step-free has no effect (only gate edge weight ×1.15) |
| hotel_north → stadium (time_sensitive) | risk 79 high; routes differ, both high; "the faster one is fine" |
| hotel_airport → stadium (group) | same route both ways |
| parking_p1 → stadium; metro_b → stadium; line_blue → stadium | routable |
| **stadium → hotel_core**, stadium → metro_b, gate_3 → metro_b | **404 "No route"** — no egress/return journeys exist (no edge leaves `stadium_main` over traversable types) |
| hotel_core → zone_fanpark; hotel_core → hotel_north; road_4 → emergency_north; zone_core → stadium | 404 "No route" |
| **Total** | **7 of 14 routable** |

**UI `/attendee` (400 px, live).**
- The journey is hard-coded to `hotel_core_cluster → stadium_main` (`Attendee.jsx:241`), with no origin/destination selection. It is fetched once on mount or toggle, with no live updates.
- "Shortest" is always red: "Fastest on paper — routes you through heavy crowd pinch points." This appeared even while the advice line said "Both routes carry similar crowding right now; the faster one is fine" and both were MODERATE.
- After approving the notify card, the **Zone Recommendation** read **"LOW DENSITY · Metro B Station — Quieter right now, and the organiser is offering ₹0 to spread the load"** while Metro B was at 99–113%. "LOW DENSITY" is hard-coded (`Attendee.jsx:374`), and the target is simply the last intervention target.
- The nudge read "Switch to Metro B Station — 1 minutes further, Rs 0 credit, and priority shuttle access."
- Accept worked ("Thanks — your route is updated.", backend `accepted`), but the route did not change.
- The step-free toggle re-fetched: risk 45 → 54, same legs.

## 19. Commander Results

**Classification:** RULE/TEMPLATE SYSTEM. Keyword intent plus a fixed tool plan, template prose and a numeric grounding validator. It is **not an LLM** (`engine: deterministic`; `LOCAL_LLM_URL` unset).

| Query | Tools planned | Answer (abridged) | Assessment |
|---|---|---|---|
| "What is the biggest problem right now?" | get_state(all), get_state/forecast/cascade(**emergency_core**), summarize | "…The most urgent is **Core Emergency Post**, projected to cross critical in 0 minutes… net flow −56.7… capacity 110, at 100 risk" | grounded, but the subject is a twin artefact |
| "Why is Metro B becoming critical?" | state/forecast/cascade(metro_b) | "projected to cross critical in 0 minutes… net flow 91.1… 86 risk… reaches gate_3 in 8 minutes and road_17 in 41 minutes, 20 downstream failures" | grounded; metro_b state matched (util 0.9813, score 86, flow 91.1) |
| "What happens if we do nothing?" | summarize, forecast/cascade(emergency_core) | "No downstream propagation is predicted from this entity. Doing nothing leaves 3 entities critical and 7 at high risk, with load variance 0.0274." | no projection at all; refers to an unnamed entity |
| "Which action gives the largest safety improvement?" | get_interventions, get_certificate | notify STABLE rank 0.16; reroute 35.3% UNSTABLE ranks below | PASS (demo line) |
| "Which hotels are running out of rooms?" / "How is transport doing?" / "What should attendees do?" / "What is the cascade precision metric?" / "What if it rains heavily?" / "asdf qwerty" / "💥💥💥" / SQL-injection string / "Approve the reroute now" | all the **same** generic plan | all the **same** "Core Emergency Post" answer | no hotel/transport/attendee/metrics/what-if intent; `run_whatif` and `propose_action` never planned |
| "Is the Blue Line congested?" | line_blue | "**The most urgent is Blue Line**, projected to cross critical in 13 minutes…" | false framing (it was not the most urgent) |
| "What is the forecast for Gate 5?" | gate_5 | "**The most urgent is Gate 5**, not projected to cross critical…" | false framing |
| "Tell me about road_12" | **road_1** | answers about "Arterial 1" | wrong entity (substring match, `commander.py:241`) |
| "Why is the North Hotel Cluster full?" | hotel_north_cluster | "projected to cross critical in 21 minutes… reaches metro_b in 33 minutes…" | works when the display name is used |

- **Stale cache across reset:** asked at cycle 45, reset, asked again at **cycle 3**. The response came back `is_cached: true` with the **identical** pre-reset text ("…1 entities are critical and 9 are at high risk…"). The cache key only checks `cycle_now − cycle_cached ≤ 4`, which is negative after a reset, and `_reset` never clears it. In the browser this showed as "CACHED INFERENCE" on the very first ask.
- **Grounding:** 0 ungrounded numbers in 18 answers. The validator accepts any number appearing anywhere in any tool result, including digits inside entity IDs ("3" from `gate_3`), so it is weak.
- Latency 1.2–2.5 ms. `propose_action` has no execution path (pytest).

## 20. Metrics Results

The "Value" column shows readings at cycle 238, in the rehearsal, and in the UI.

| Metric | Formula / source | Baseline / target | Value | Measured? | Status |
|---|---|---|---|---|---|
| forecast_mae | mean \|900 s forecast − actual 30 cycles later\| (last 200) | persistence (measured) | 0.1187 vs 0.0466 (**−154.7%**) at c238; 0.1436 vs 0.1765 at ~c80; 0.073 vs 0.102 in UI | yes | PARTIAL — sign flips over the run |
| cascade_lead_time_sec | mean of `max(step ETA)` of active cascades per cycle | constant 0.0 "threshold_rule" | 1,903–2,072 s ("34 min") | **no** — predicted ETA, not realised lead | FAIL |
| cascade_precision | confirmed / (confirmed + false) at each step's ETA | constant 0.31 | 0.361–0.726 | yes | PASS (baseline constant) |
| cascade_recall | caught / (caught + missed) band transitions | constant 0.30 | **0.015–0.029** | yes | FAIL |
| twin.rmse | assimilated RMSE (counts) | uncorrected (drift only) | 1,147 (UI); 8,651 (c238); baseline null unless drift | yes, vs a strawman | FAIL |
| ensemble_coverage | `clamp(0.85 + spread, 0.80, 0.95)` | 0.85–0.95 | 0.85 always | **no** — formula | FAIL |
| peak_utilisation_reduction_pct | (cf peak − current peak) / cf peak, cf from the exploded branch | do-nothing | **84.1%** after a 4% notify (rehearsal); 52.3% (UI) | derived from invalid input | FAIL |
| load_variance_reduction_pct | same idea on zone variance | do-nothing | 0.0 / −0.1 | derived | PARTIAL |
| unstable_interventions_caught | count of max-relief UNSTABLE demoted | — | 1–16 | yes, structurally forced | PARTIAL |
| certificate_accuracy_pct | converged certs within 15% of branch peak | target 85 | **0.0** | yes | FAIL |
| convergence_rate_pct | converged / all interventions | target 85 | 32.1–38.5 | yes | PARTIAL |
| cycle_latency_ms | mean of last 40 cycles | 2,000 | 29–49 | yes | PASS |
| commander_ungrounded_rate | ungrounded / calls | 0 | 0.0 | yes (weak validator) | PARTIAL |
| tool_call_correctness | tools not raising / tools called | 0.9 | 1.0; **0.0 before any query** | trivially | PARTIAL |

The `/metrics` page header claims "Multi-horizon GNN cascades and EnKF assimilation **guarantees**" (`Metrics.jsx:287`).

## 21. Frontend Live Results (`npm run dev:live`, Chrome 154 headless)

| Screen | Loads | Live data | Interaction → backend | UI reflects backend | Issues |
|---|---|---|---|---|---|
| Command Centre / TopBar | PASS | PASS (cycle, clock, risk, variance, crit/high) | — | PASS | "LIVE FEED" for a simulation; cycle shown **mod 100**; "Autonomous Dispatch Ready" |
| Map (deck.gl) | PASS with GL | PASS (bands, labels, estimated rings) | node click via Pressure Timeline | PASS | Without a WebGL context the map is blank (environment) |
| Entity Detail | PASS | `GET /state/metro_b`: 93%, score 71 HIGH, forecast, "MAE 0.010 vs persistence 0.011 (9.2% edge)", breakdown | "Show Cascade Propagation Lines" → `GET /cascade/metro_b` 200 | PASS; 16 arcs with ETAs rendered | y-axis tick labels clipped |
| Pressure Timeline | PASS | PASS | row click → detail panel | PARTIAL | hero row "Core Emergency Post LOAD 200% CRITICAL"; "Arterial 13 105% MODERATE"; "Blue Line 80% CRITICAL" |
| Intervention Queue | PASS | 7 cards, order identical to backend | Approve → toast "Approved — 3 nudges issued" → backend `executing` | PASS | cards vanish after ~15 s wall |
| Action HUD | PASS | T0 vs live | — | PARTIAL | "Executed — congestion did not improve (realised −9.5pp)"; "Metro B Station 99% → 113% ▲14pp HIGH"; "settled: realised −9.5pp vs do-nothing **−100pp** · regret +13.5" (relative % labelled "pp") |
| Twin gauge | PASS | RMSE, N = 20, spread | Expand + drift toggle → `POST /twin/drift-mode` | PARTIAL | "RMSE REDUCTION 91.5%" (strawman, §10) |
| Commander | PASS | — | 4 scripted questions → 200 | PARTIAL | first answer flagged CACHED (stale cache); "biggest problem" = twin artefact |
| What-If | PASS | — | Heavy Rain / Gate 3 → `POST /simulate` + poll | PARTIAL | "peak util Δ +39.3%", "**+616.8%**", "load variance Δ +1863.7%" |
| `/metrics` | PASS | polled | — | numbers as in §20 | |
| `/attendee` | PASS | — | Accept → 200; step-free re-fetch | PARTIAL | §18 |
| Console | — | — | — | PASS | only React Router future-flag warnings and `favicon.ico` 404 |
| Backend restart | — | — | — | **FAIL** | frozen, chip "connected" (§7) |
| Page refresh | — | — | — | PASS | recovers immediately |

## 22. Frontend Mock Results (`npm run dev`, Chrome)

| Check | Result |
|---|---|
| Replay start | cycle 1 at 14:00; badges "SIMULATION" + "DEMO MOCK"; "No Interventions Required" |
| Frame progression | 0.9 s/frame; display cycle 1 … 99, then **"0"** on frame 100 (100 % 100) |
| Frame count | 100 (`state_sequence`, `pressure_timeline` and `twin_fidelity_sequence` all 100) — docs say 90 |
| Interventions arm | display cycle 11 (frame index 10): "2 PENDING", risk 38 → 59 |
| Cascade arm | frame index 8 |
| Approve in mock | status → executing; HUD "Executed (mock demo — live simulation not running)"; pending chip still showed "2" |
| **Transition after the final frame** | frame 100 (cycle 100, shown "0") → frame 1: `resetSimState()` clears queue, cascades, tracked actions, what-if and HUD; risk back to MODERATE 38; queue empty until display cycle 11, then the same 2 cards re-arm. Regret and Commander history are kept. |
| What-if | **identical** for Blue Line / Heavy Rain / Gate 3 (Δ +24.1% / +62.7%, same new-critical list) |
| Commander | unscripted "Which hotels are full?" → "No grounded data is available… in mock mode" shown with TOOL GROUNDED + CACHED INFERENCE chips |
| Console | no errors |
| Fixture content | last frame `gate_2` 2.0 and `road_9` 1.954 (twin artefacts); `simulation.json` baseline peak 72.5 (7,250%) / 45 critical; metrics: cert accuracy 0, recall 0.032; `interventions.json` reroute reason "Did not converge within iteration cap." |
| Reproducibility | fresh `export_mocks --cycles 100` matches observed entities exactly; the **13 unobserved entities differ from frame 10**. `event.json` sim_time 14:45 (90-cycle export) vs 14:50 (100) — the committed set was exported with mixed cycle counts |
| `npm test` (lifecycle re-arm regression) | PASS |

**Mock replay reset vs live simulation reset:**
- **Mock:** a client-side loop of 100 recorded frames. It auto-resets at the wrap and never talks to a backend. Approvals only flip a status.
- **Live:** never resets automatically. After cycle 100 it continues (cycle 238, 600 and beyond; the real DB reached ≈4,367 cycles), and the TopBar shows `cycle % 100`, which *looks* like a reset but is not.
- **Live resets happen only on:** `POST /demo/control {action:"reset"}` (reproduces the run exactly), a process restart (cycle 0, and the frontend freezes until refresh), or a `--reload` file save (same as restart).

## 23. Contract / Schema Results

- Pydantic strict models (`extra="forbid"`) on every response. **31/31** captured live responses validate against `contracts/schemas` (AJV 2020, formats), including 10 error envelopes.
- Request validation: malformed JSON, missing bodies, extra fields, wrong types, enum violations and bad query params all give 400 `INVALID_REQUEST` (PASS). Semantic validation gaps: unknown entity IDs in scenarios, speed ≤ 0, malformed seek timestamps (500), unknown status filter.
- snake_case: no camelCase keys in any payload (PASS). Units: ratios 0–1 (utilisation up to 2.0 by design), `_sec` ints, `_paise` ints (PASS). `null` semantics: `uncorrected_rmse` null outside drift, `time_to_critical_sec` null when no crossing (PASS). Enums consistent (PASS).
- Error codes are inconsistent in edge paths: 405 → `INTERNAL_ERROR`; unknown route → `ENTITY_NOT_FOUND`; no route → `ENTITY_NOT_FOUND`; nudge double-response → `INTERVENTION_ALREADY_RESOLVED`; post-expiry approve → `ALREADY_RESOLVED` rather than `EXPIRED`.
- Fresh schema export vs committed: 6 files differ only by `"additionalProperties": true` on dict-typed fields (likely a Pydantic version difference). Mocks validate 17/17.
- Semantic contract gaps, where the schema allows what the contract forbids:
  - utilisation > 2 inside `simulation_result` (e.g. 964.05);
  - `counterfactual_relief_pct −100`;
  - `realised` labelled "pp" in the UI.

## 24. Determinism Results

| Test | Result |
|---|---|
| Two fresh in-process runs, 150 cycles, all fields (states, forecasts, cascades, intervention IDs/ranks/verdicts, summary) | **identical** |
| In-process reset at cycle 40, then 111 cycles | **identical** to a fresh start |
| Three live rehearsals from `/demo/control reset` | **identical** (same timings, IDs `int_7413f`/`int_5c0cd`, regret 19.5, metrics) |
| Live WS regret vs in-process A/B | identical entry |
| One what-if (heavy rain) at cycle 30 | observed entities unaffected; **unobserved states, forecasts, cascades and summary diverge from cycle 30; the intervention set diverges from cycle 106** |
| One approval | the intended target changes, and unrelated twin noise shifts too (load variance at cycle 119: 0.2924 vs 0.0932) |
| Committed mocks vs fresh export | unobserved entities differ from frame 10 (branch-call count differs between exports) |

**Conclusion:** the run is deterministic for identical action sequences only. Any what-if or approval perturbs later live values through the shared twin RNG.

## 25. Performance Results

| Measure | Value | Budget | Status |
|---|---|---|---|
| Backend startup (reload) | 4.84 s; restart 1.34 s | — | PASS |
| Cycle (in-process, 600) | p50 29.1 ms, p95 36.2 ms, max 87.1 ms; mean 13.7 (cycles 0–100) → 23.4 (100–300) → 33.6 (300–600) | 2,000 ms | PASS (growth from re-persisting all interventions each cycle) |
| Cycle (live `/metrics`) | 29–49 ms | 2,000 | PASS |
| GNN forward / predict_all(66) | 0.73 ms / 1.5 ms | 150 ms | PASS |
| What-if job | < 0.25 s | eta 3 s | PASS |
| Fallbacks / backpressure | 0 "exceeded", 0 "shedding", 0 "cycle took" across all logs | — | PASS |
| WS | ~2 cycles/s; ~24 KB/cycle to each command-centre client + alert bursts | — | PASS |
| Concurrency | 200 mixed requests (32 threads, incl. commander + simulate): 0.11 s, all 2xx, p50 13 ms, max 19 ms | — | PASS |
| Frontend build | 2.45 s; JS 4,019 KB (614 KB gzip) | — | PARTIAL |
| Demo DB growth | ≈49 KB/cycle (213 MB over one ≈4,367-cycle run) | — | PARTIAL |

## 26. Edge Cases

| Case | Observed | Status |
|---|---|---|
| Unknown entity / intervention / simulation / nudge | 404 with envelope | PASS |
| Invalid scenario type / horizon (30, 99999) / empty scenarios | 400 | PASS |
| Duplicate approval / reject / nudge response | 409 | PASS (codes §23) |
| Expired approval | 409 `INTERVENTION_ALREADY_RESOLVED` | PARTIAL |
| Empty / whitespace Commander query | 400 | PASS |
| Malformed JSON bodies | 400 | PASS |
| No history (right after reset) | `/forecast` and `/twin/fidelity` → 422 `MODEL_NOT_READY` | PASS |
| Already-critical entity | TTC 0; Commander says "cross critical in 0 minutes" | PARTIAL |
| Utilisation > 100% | displayed up to 2.0 (clamp); risk "moderate" at 1.0–1.2 | FAIL |
| No route | 404 `ENTITY_NOT_FOUND` | PARTIAL |
| Invalid attendee id | 200 empty list | PASS |
| Restart / reconnect | restart freezes the UI until refresh | FAIL |
| Pause / resume | pause holds (0 cycles in 2 s); resume +4 cycles in 2 s | PASS |
| Reset ×8 rapidly while running | all 200; engine continued from cycle 0 | PASS |
| Seek | cycle counter reset, stale history/interventions kept; pre-start accepted | FAIL |
| Speed 15 / 120 / 150 / 300 | 0.67 / ~2.3–4 / 5.0 / 5.0 cycles/s (0.2 s floor; a change applies after the current sleep) | PASS |
| **Speed 0 (held 1.5 s)** | **engine frozen permanently**; `play` + `speed 60` + `reset` all return "playing" with cycle stuck at 0; only a process kill recovers | FAIL |
| Speed −5 | accepted (same failure mode) | FAIL |
| Malformed seek time | 500 | FAIL |

## 27. End-to-End Demo Result

**Rehearsal chain** (live API with the UI driven once in Chrome; three identical API runs, 60×):

| # | Step | Result | Evidence |
|---|---|---|---|
| 1 | Event appears | PASS | TopBar "NATIONAL CUP FINAL", kickoff countdown |
| 2 | Simulation starts | PASS | cycle 1 at +0.5 s |
| 3 | Pressure builds | PARTIAL | first pressure-timeline item at **4.6–4.9 s** (cycle 10) is `road_9`, a twin-estimated road; the "calm opening" lasts ~5 s |
| 4 | Forecast identifies future pressure | PARTIAL | metro_b TTC shown from cycle 10; wobbles; hero row at cycle 50 is "Core Emergency Post 200%" |
| 5 | Cascade appears | MISLEADING | arcs render (16 steps from metro_b), but they are not root-conditioned, include substitutes, and end at a twin artefact |
| 6 | Intervention appears | PASS | metro_b pair at **24.2–24.4 s** (cycle 49): notify STABLE 0.16 > reroute UNSTABLE 0.1314 |
| 7 | Operator opens it | PASS | card, certificate reason, sweep |
| 8 | Operator approves | PASS | 200, 3 nudges (window ≈15 s wall before expiry) |
| 9 | World changes | MISLEADING | notify cuts real demand 4%; a reroute would cut source **and** destination |
| 10 | Nudge reaches attendee | MISLEADING | delivered (WS + `/attendee`), text "Switch to Metro B Station … Rs 0", "LOW DENSITY" |
| 11 | Attendee responds | PARTIAL | recorded; no effect on anything |
| 12 | Impact measured | MISLEADING | HUD "99% → 113% ▲14pp", "do-nothing −100pp" |
| 13 | Settlement occurs | PASS | +14.2 s wall (cycle 79) |
| 14 | Regret / metrics update | MISLEADING | regret 19.5 on a −100 counterfactual; `/metrics` **peak reduction 84.1%** |
| 15 | What-if | FAIL | baseline peak 8,055% (rehearsal), +616.8% shown in UI |
| 16 | Twin drift | MISLEADING | "RMSE REDUCTION 91.5%" vs a diverging strawman |
| 17 | Attendee route | PARTIAL | outbound works; return impossible; step-free no-op |
| 18 | Reset | PASS | `/demo/control reset` → cycle 0; queue/HUD cleared by the resync |
| 19 | Demo repeatable | PASS | three runs identical |

**Repeatability (three clean-reset runs).** Every figure below was identical across runs 1, 2 and 3.

| Measure | Result (all three runs) |
|---|---|
| Time to first pressure | 4.6–4.9 s (cycle 10, `road_9`) |
| Time to first intervention | 4.6–4.9 s (notify on `road_9`) |
| metro_b cards | 24.2–24.4 s (cycle 49) |
| Card expiry (TTL) | 14.9–15.2 s |
| Approval works | yes (200) |
| State changes | metro_b 0.9007 → 1.0403 (event growth > 4% cut) |
| Nudge appears | yes (att_demo_1) |
| Settlement | +14.2 s |
| Metrics update | yes (peak reduction 84.1, cert accuracy 0, recall 0.029) |
| What-if works | completes; baseline 80.56 → 124.45 (invalid) |
| Attendee works | 200 moderate |

**Verdict.** The chain is **mechanically reproducible** but **numerically misleading** at steps 5, 9, 10, 12, 14 and 16, and it **fails** at step 15. Demo-timing hazards: 15-s card TTL at 60×; UI freeze on any backend reload; `speed_multiplier 0` kills the engine.

## 28. Problem Statement Coverage

Classified only from behaviour tested in this audit.

| # | PS item | Status | Basis |
|---|---|---|---|
| 1 | Accommodation availability | PARTIAL | hotels exist as scripted-occupancy capacity nodes; no availability model beyond utilisation |
| 2 | Transportation capacity | PARTIAL | stations/lines with capacities; utilisation scripted; capacity changes inverted live (C060), no effect in what-if (C127) |
| 3 | Event schedules | PARTIAL | fixed config times; exit spike mistimed (C056) |
| 4 | Visitor demand | PARTIAL | synthetic per-entity curves |
| 5 | Locations | PASS | geo-positioned graph, map |
| 6 | Crowd movement | FAIL | no movement across edges (C055) |
| 7 | Consolidated event view | PASS | Command Centre works end-to-end |
| 8 | Capacity pressure | PARTIAL | shown, but twin artefacts dominate and over-capacity is under-banded |
| 9 | Congestion prediction | PARTIAL | forecaster works on ramps; worse than persistence overall |
| 10 | Resource-shortage prediction | PARTIAL | same forecaster for all types; no resource model |
| 11 | Visitor redistribution | FAIL | redistribution removes demand from receivers too (C134) |
| 12 | Hotel saturation | FAIL | hotels never exceed 0.856; accommodation action never triggered live; Commander can't answer hotel questions |
| 13 | Transport congestion | PARTIAL | metro_b hero path works; reroute semantics broken |
| 14 | Sudden demand spikes | FAIL | what-if attendance/concurrent numerically invalid; live inject exists only via curl (not UI) |
| 15 | Venue capacity | PARTIAL | stadium/gates modelled |
| 16 | Last-mile connectivity | PARTIAL | `last_mile_to` edges + shuttle template; no return legs |
| 17 | Uneven distribution | FAIL | displayed load variance 5× truth (C204) |
| 18 | Schedule changes | MISSING | no API/UI |
| 19 | Alternative accommodation | FAIL | substitutes exist in the graph but are never surfaced live |
| 20 | Movement optimization | PARTIAL | rule templates + ranking; no optimisation |
| 21 | Alternative routes | PARTIAL | outbound congestion-aware routing works; return/egress missing |
| 22 | Crowd/capacity prediction | PARTIAL | forecaster + GNN run; GNN not root-conditioned |
| 23 | Off-peak recommendations | MISSING | only static stagger text |
| 24 | Incentives | PARTIAL | nudges carry credit/perk, arbitrary (Rs 0), with no modelled effect |
| 25 | AI / predictive analytics | PARTIAL | trained GNN runs but its outputs aren't useful; the rest is statistical/rule-based |
| 26 | Optimization | PARTIAL | rule-based ranking with an artefactual stability factor |
| 27 | Recommendation | PARTIAL | cards render; contents largely UNSTABLE by construction |
| 28 | Simulation | PARTIAL | live scripted sim works; what-if FAIL |
| 29 | Operator interface | PASS | all panels function, approve/reject works |
| 30 | Attendee interface | PARTIAL | loads, accept/decline work; hard-coded journey and copy |

Totals: PASS 3 · PARTIAL 19 · FAIL 6 · MISSING 2 · UNVERIFIED 0.

## 29. Real vs Simulated vs Mocked

| Capability | Classification (verified) |
|---|---|
| Data / sensors | SIMULATED (deterministic generator; no ingestion) |
| Digital Twin | REAL ALGORITHM (EnKF) + SIMPLIFIED surrogate; **unreliable for unobserved entities** |
| Forecasting | STATISTICAL (persistence → quadratic trend); TSFM UNIMPLEMENTED at runtime |
| GNN cascade | REAL trained model, inference only; runs, not semantically useful; DETERMINISTIC FALLBACK verified |
| Risk | DETERMINISTIC / RULE-BASED |
| Anomaly | STATISTICAL (z-score); not rendered |
| Optimisation | RULE-BASED templates, HARDCODED relief/cost |
| Equilibrium | ALGORITHMIC (damped follower iteration), DETERMINISTIC; leader UNIMPLEMENTED |
| Intervention execution | SIMULATED (demand multiplier inside the generator), no external actuation |
| Counterfactual / regret | ALGORITHMIC on the twin surrogate (invalid values) |
| What-if | SIMULATED on the twin branch (invalid values) |
| Attendee routing | ALGORITHMIC (Dijkstra) over a directed graph without egress |
| Nudges | RULE-BASED text; response recorded, UNUSED |
| Commander | RULE-BASED templates + grounding validator; no LLM |
| Metrics | mix: measured (forecast MAE, precision, recall, latency); FORMULA (coverage); DERIVED FROM INVALID INPUT (peak reduction, cert accuracy); CONSTANT baselines |
| Persistence | REAL SQLite writes; write-only; 3 tables UNIMPLEMENTED |
| Frontend `npm run dev` / `npm run build` | MOCKED (recorded replay) |
| Frontend `npm run dev:live` | REAL backend |
| Postgres / Redis / Ollama / Chronos / LightGBM | NOT PRESENT |

---

## 30. P0 Findings

### P0-01 — Digital Twin diverges for every unobserved entity and drives the demo's headline numbers
- **Severity:** P0 · **Component:** Twin / engine merge / UI
- **Files:** `Backend/app/ml_reference/twin.py` (assimilate L104-138, `_advance` L72-91), `Backend/app/services/engine.py` (`_merge_states` L245-279)
- **How tested:** 600-cycle and 120-cycle in-process runs comparing displayed values and twin means with `generator.ground_truth()`; a static-initial-count baseline; drift copy.
- **Expected:** estimates for the 13 unobserved entities track truth at least as well as a naive baseline.
- **Observed:**
  - `emergency_core` pinned at 2.000 from about cycle 30 (truth 0.32–0.69).
  - `zone_concourse_south` 0.017 vs 0.944 at cycle 100.
  - 8 of 13 at exactly 0.000 or 2.000 by cycle 600.
  - Utilisation RMSE on unobserved entities: twin 11.19 vs uncorrected 0.87 vs static 0.35 (cycle 100).
  - Assimilated RMSE 92.7 → 10,415 counts.
- **Evidence (downstream effects):**
  - UI Pressure Timeline hero "Core Emergency Post 200% CRITICAL" at cycle 50–52.
  - Commander "biggest problem" is Core Emergency Post.
  - First interventions rooted on `road_9`, `road_13` and `emergency_core`.
  - Load variance 0.2388 vs 0.0467 truth.
  - Mock fixtures carry `gate_2` 2.0.
- **Root cause:** not fully isolated. It is consistent with spurious-covariance updates of unobserved rows (20 members vs 53 observations) and the flow rows absorbing doubled analysis increments (L136). Flows reach \|92,395\|.
- **Impact:** judges see physically impossible readings on the main screen; everything downstream (risk, cascades, triggers, certificates, load variance) inherits them.
- **Next action:** fix unobserved-state estimation before any feature work (see §37).

### P0-02 — What-if outputs are numerically invalid
- **Severity:** P0 · **Component:** Twin branch / simulation service
- **Files:** `twin.py` `_branch` L247-293 (compounding L271); `services/simulation.py` L19-59
- **How tested:** 15 scenario variants at cycle 60 and at cycle 12 via the API, plus UI presets.
- **Expected:** bounded, scenario-dependent projections.
- **Observed:**
  - Baseline peak 20.48 (2,048%) at c60, 30.73 at c12, 80.56 in the rehearsal, with 25–31 entities critical in the unmodified baseline.
  - Gate 3 closure 964.05; outage 372.58.
  - Blue Line −15%, metro_b −50%, road_4 −50% and hotel shortage give ≈0% Δ.
  - `gate_99` accepted.
  - 9 of 15 scenarios return identical candidates.
- **Evidence:** `whatif_results.json`; UI "peak util Δ +616.8%".
- **Root cause (partly verified):** demand multipliers compound every step (L271); the surrogate uses the exploded flows from P0-01; capacity-only scenarios are masked by the artefactual baseline peak.
- **Impact:** a headline feature displays nonsense numbers.
- **Next action:** rebuild the branch on bounded dynamics (or the generator), apply multipliers once, validate entity IDs.

### P0-03 — Approving a redistribution removes demand from the receiver; approval effects are fabricated
- **Severity:** P0 · **Component:** Generator relief / optimiser targets
- **Files:** `Backend/app/ml_reference/generator.py` `apply_relief` L235-249; `Backend/app/ml_reference/optimiser.py` L80 (and the gate/zone/parking/accommodation templates); `Backend/app/api/routes.py` L239-240
- **How tested:** A/B in-process (same seed) of do-nothing vs approve-reroute vs approve-notify.
- **Expected:** metro_b loses and metro_c gains riders (gate_5 pressure rises); `notify_only` has no demand effect.
- **Observed:**
  - metro_b 0.901 → 0.582 in one cycle.
  - **metro_c 0.479 → 0.306**; gate_5 identical to do-nothing.
  - Notify approval set `_intensity_mult[metro_b] = 0.96` (truth 0.912 → 0.875).
- **Impact:** the core "approval actually changes the simulation" claim shows the wrong physics, and the "saturation trap" never happens in the live world.
- **Next action:** model transfers as source −x / receiver +x with ramp-in; make `notify_only` effect-free.

### P0-04 — Counterfactual, regret and decision metrics are built on the exploded branch
- **Severity:** P0 · **Component:** Settlement / metrics
- **Files:** `engine.py` `_settle_executing_interventions` L623-690; `services/metrics.py` L85-95; `routes.py` L221
- **How tested:** A/B runs, the live WS `regret_update`, three rehearsals, `/metrics`.
- **Expected:** a counterfactual comparable to the realised outcome; a meaningful peak/variance reduction.
- **Observed:**
  - `counterfactual_relief_pct = −100.0` in every settlement tested (do-nothing metro_b 9.1456).
  - Realised −15.5% for a notify (event growth).
  - `/metrics peak_utilisation_reduction_pct` **84.1%** (52.3% in the UI run) after a 4% notify while metro_b rose 0.90 → 1.04.
  - HUD "do-nothing −100pp".
  - Real DB: 2 of 3 regret rows at −100.
- **Impact:** the judging page claims a large safety improvement that did not happen.
- **Next action:** a counterfactual from the same world model (e.g. a generator fork without relief); guard metrics against invalid inputs.

### P0-05 — Equilibrium verdicts are an artefact of relief size, not stability
- **Severity:** P0 · **Component:** Equilibrium solver
- **Files:** `Backend/app/ml_reference/equilibrium.py` `_solve_followers` L234-300, `_reason` L325-366, `_minutes_to_breach` L373-376
- **How tested:** relief sweep, gate_5 trap, pre-existing-overload probe, 600-cycle distribution, real DB (read-only), certificate-accuracy counter.
- **Expected:** verdict reflects post-intervention equilibrium load.
- **Observed:**
  - Relief ≥ 8% ⇒ never converges ⇒ UNSTABLE (2% and 4% converge).
  - 109 of 115 live and 1,166 of 1,177 real-DB certificates are UNSTABLE.
  - The demo reroute is UNSTABLE for "Did not converge" with peak at metro_d, not gate_5.
  - `zone_core` at 1.05 makes a 4% notify on `road_9` UNSTABLE, with the "21 minutes" figure produced by a formula.
  - Certificate accuracy 0/38.
- **Impact:** the "signature feature" produces a fixed outcome (any real action is UNSTABLE) and its displayed reasons are misleading.
- **Next action:** define convergence on a fixed point of the moved share; restrict max-utilisation checks to entities the intervention changes; derive time-to-breach from a rollout or drop it.

### P0-06 — Risk bands under-rate over-capacity
- **Severity:** P0 · **Component:** RiskScorer
- **Files:** `Backend/app/ml_reference/risk.py` `_score_one` L61-78
- **How tested:** score table for 0.1–2.0 utilisation; live observations.
- **Expected:** ≥100% ⇒ critical.
- **Observed:**
  - 1.0 ⇒ 50 moderate; 1.2 ⇒ 60 moderate; 1.5 ⇒ 75 high.
  - Live: `gate_5` 105% moderate, `zone_east` 108% moderate, "Arterial 13 105% MODERATE", HUD "Metro B 113% HIGH".
- **Impact:** operators and judges see overloaded assets labelled moderate; the pressure/queue logic uses these bands.
- **Next action:** add an absolute-utilisation override or a non-linear base term.

### P0-07 — Frontend silently freezes after any backend restart (including `--reload` saves)
- **Severity:** P0 · **Component:** Frontend WS client / backend seq
- **Files:** `Frontend/src/lib/ws.js` L17, L70-71; `Backend/app/ws/manager.py` L43-70
- **How tested:** Chrome on `dev:live`; backend killed at cycle 60 and relaunched.
- **Expected:** after reconnect the UI resyncs to the new run.
- **Observed:** the chip returned to connected; frames kept arriving (308 → 539); the UI stayed at "CYCLE 60 / 14:30" for 16 s while the backend reached cycle 33; only a manual refresh recovered.
- **Root cause (verified):** the client drops `seq <= lastSeq`; `lastSeq` persists across reconnects; the server's seq restarts at 1, so even the `resync` is discarded.
- **Impact:** any backend file save or crash during the demo freezes the display while it looks healthy.
- **Next action:** reset `lastSeq` on socket open or accept `resync` unconditionally; or add a server epoch.

### P0-08 — GNN cascade is not root-conditioned, poorly calibrated, and floods alerts
- **Severity:** P0 · **Component:** `ML/cascade.py` + engine root selection
- **Files:** `ML/cascade.py` `predict_all` L176-217, `_extract_gnn_result` L331-430; `engine.py` `_apply_cascades` L409-433
- **How tested:** controlled-state probes; live runs; WS alert counts.
- **Expected:** the probability of downstream failure depends on the root's state; a small set of meaningful roots.
- **Observed:**
  - Identical per-entity probability in every root's cascade.
  - 24 of 66 nodes at p > 0.6 at 30% load (6 at 5% load).
  - metro_b 0.3 → 1.2 moves `road_4` −0.012 and `emergency_north` 0.
  - 42–60 roots including hotels/zones/parking.
  - 307 alerts in about 90 cycles.
  - Recall 0.015–0.029.
- **Impact:** "trained GNN cascade" claims are not supported by its runtime behaviour; the map arcs are descendants of X filtered by their own marginal risk, not a cascade from X.
- **Next action:** restrict roots to relevant types and truly critical states with hysteresis; condition propagation on the root (e.g. counterfactual perturbation or the deterministic propagator for chains); recalibrate.

## 31. P1 Findings

| ID | Title | Component / files | How tested | Expected | Observed (evidence) | Impact | Next action |
|---|---|---|---|---|---|---|---|
| P1-01 | `speed_multiplier ≤ 0` freezes the engine permanently | `engine.py` `_loop` L92-107; `routes.py` demo_control; `schemas.py` DemoControlRequest | speed 0 held 1.5 s, then play / speed 60 / reset | reject or recover | 200 "playing", cycle stuck at 0 for 3 s × 3 attempts; `sleep(30/1e-6)` | one curl kills the demo until process restart | validate `> 0`; make the sleep interruptible |
| P1-02 | Commander stale cache survives reset | `services/commander.py` L292-299; `engine._reset` L954-976 | ask at c45, reset, ask at c3 | fresh answer | `is_cached:true`, identical pre-reset text; UI "CACHED INFERENCE" on first ask | wrong answers after every reset | clear cache on reset; require `0 ≤ Δcycle` |
| P1-03 | Commander intent coverage / entity resolution / false framing | `commander.py` `_plan` L236-286, `_compose` L359-480 | 18 queries | hotel/transport/metrics/what-if intents; exact entity | 9 unrelated queries → identical answer; "road_12" → road_1; "The most urgent is Gate 5/Blue Line" | a judge asking a free question gets a wrong or irrelevant answer | exact ID/name matching; intent templates; drop "most urgent" when an entity is named |
| P1-04 | Attendee view hard-coded and recommends congested targets | `Frontend/src/routes/Attendee.jsx` L241, L128, L374, L382; `services/attendee.py` `issue_nudges` L160-197 | Chrome at 400 px; API | dynamic origin/destination; honest density | fixed hotel_core → stadium; "LOW DENSITY … Metro B Station" at 99–113%; "Switch to Metro B … Rs 0"; "Shortest" always red | the attendee story contradicts the data | derive density from state; hide the zone card for non-transfer actions; add origin/destination selection |
| P1-05 | No return / egress journeys; step-free is a no-op | `services/attendee.py` TRAVERSABLE L20-25, `time_weight` L108-112; `topology.py` | 14 combinations | stadium → hotel routable; accessible path differs | 7/14 unroutable incl. every return; accessible legs identical | PS items 16/21 incomplete | add reverse traversal/egress edges; a real accessibility attribute |
| P1-06 | Nudge responses never used; nudges never expire | `routes.py` L385-404 (comment claims feedback); no expiry code | accept/decline; check status after `expires_at` | compliance feeds the solver; expiry | `observed_compliance` never read; `pending` 24 sim-min after expiry | the "learning loop" claim is false | wire into segment compliance, or remove the claim; expire nudges |
| P1-07 | Forecaster overshoots plateaus; worse than persistence long-run | `ml_reference/forecaster.py` `_forecast_one` L117-138 | unit probe; 600-cycle MAE | ≥ persistence | plateau 0.9 → 1.52 @900 s; all 10 watched entities worse; `/metrics` −154.7% | false TTC alarms late in the event | damp curvature near saturation / capacity; bound by band |
| P1-08 | Simulation has no crowd movement; exit spike mid-match; no end | `generator.py` L123-144, L131-136 | runs, code | flows on edges; spike after 18:30; stop at end_time | independent curves; spike at 16:30; real DB ran to 02:23 two days later | PS "crowd movement" not met; odd late-run state | document honestly or implement edge flows; align the spike to `end_time`; auto-stop |
| P1-09 | Live `inject` capacity reduction lowers utilisation | `generator.py` L193-200, `capacity()` L118-120; `engine._merge_states` L254-266 | inject metro_b −50% | utilisation ↑ | 0.8205 → 0.4245 | disruption injections behave backwards | compute utilisation against effective capacity |
| P1-10 | Hotel/accommodation and off-peak features never surface live | `optimiser.py` L195-217; generator hotel profile L38 | 600-cycle run; unit probe | accommodation actions when hotels saturate | 0 accommodation_rebalance; hotels peak 0.856 < 0.88; no off-peak recommendations | PS items 12/19/23 fail | tune scenario so hotels saturate; add off-peak recommendation |
| P1-11 | Card TTL 15 s wall at default speed | `optimiser.py` L31; `config.yaml` speed 60 | 3 rehearsals | enough time to discuss a card | expiry after 14.9–15.2 s | an approval during a pitch can 409 | slow the sim for the approval step or lengthen the TTL (demo decision) |
| P1-12 | Proposal ceiling exceeded | `engine.py` L521-526 | 600-cycle run | ≤ 8 live | 10 live | queue noise | cap after generation |
| P1-13 | Mock fixtures embed broken values; mock what-if ignores preset | `Frontend/src/mocks/*.json`, `lib/mocks.js` `simulate` | Chrome mock | sane, preset-specific | `gate_2` 2.0, sim 7,250%, identical Δ for all presets | default `npm run dev` shows the same problems | regenerate after fixes; per-preset fixtures |
| P1-14 | No demo controls in UI; `npm run build` ships mock mode | `api.js` L16-18; no control component | build + grep | live build option; operator controls | `MOCK_MODE` true in prod bundle; reset/speed curl-only | risk of presenting a replay as "live"; no in-app recovery | add `--mode live` build script; minimal control panel |

## 32. P2 Findings

| ID | Title | Files | Evidence | Next action |
|---|---|---|---|---|
| P2-01 | Malformed WS client frame creates a zombie socket (open, no data) | `api/ws_routes.py` L53-64 | malformed-sender got only its initial resync; `readyState 1` | close the socket on error, or ignore bad frames and continue |
| P2-02 | `state_update` sends all 66 entities every cycle | `state_store.changed_entities` L164-179 | avg 66 entities, 12 KB | threshold / quantise |
| P2-03 | Anomalies never rendered | `useStore` pushAnomaly; no component | 13–14 WS anomalies, 0 UI | render or remove |
| P2-04 | `/simulate` accepts unknown entity IDs | `routes.py` L298-310; `simulation.py` | `gate_99` → 202, 0% | validate IDs |
| P2-05 | Seek semantics inconsistent | `engine.demo_control` L939-942 | cycle → 0, history kept, 13:00 accepted | clear state on seek; validate range |
| P2-06 | Error-code inconsistencies | `errors.py`, `routes.py` L392-396, `attendee.py` L124-129 | 500 on bad seek; 405 → INTERNAL_ERROR; no-route → ENTITY_NOT_FOUND; expired → ALREADY_RESOLVED | map codes properly |
| P2-07 | execution / cascade_prediction / risk_state never written | `db/models.py`; engine `_persist` | 0 rows | implement or drop |
| P2-08 | Certificate / intervention ID collisions (16/20-bit) | `equilibrium.py` L105-106; `optimiser.py` L288 | real DB: 7 orphaned interventions | widen IDs |
| P2-09 | Unbounded DB growth; all interventions re-merged every cycle | `engine._persist` L693-791 | 213 MB demo DB; cycle ms 13.7 → 33.6 | prune; merge only changed rows |
| P2-10 | What-if / approval branches perturb live RNG | `twin.py` `_branch` uses `self._rng` | divergence from cycle 30 (unobserved/forecast/cascade), interventions from cycle 106 | dedicated branch RNG |
| P2-11 | Metric definitions: lead time = predicted ETA; coverage = formula; tool correctness = no-exception; constant baselines | `metrics.py` L64-78, L117-120 | §20 | measure, or label as heuristics |
| P2-12 | UI copy overclaims / mislabels | `TopBar.jsx` L98, L123; `CommandCentre.jsx` L68; `Metrics.jsx` L287; `ActionHUD.jsx` L159-162; `actionEffects.js` L184-185 | "LIVE FEED", "Autonomous Dispatch Ready", "guarantees", cycle mod 100, "%" shown as "pp" | honest labels |
| P2-13 | Grounding validator is permissive | `commander.py` `_collect_numbers` L75-104 | digits in IDs ground numbers | require value/field match |
| P2-14 | `--reload` orphan: killed worker leaves port held | uvicorn reload | `[Errno 48]`, hung requests | run without reload for the demo |
| P2-15 | Committed mocks not reproducible from current code; schema export drift | mocks, `contracts/schemas` | 13 unobserved entities differ from frame 10; `event.json` 90-cycle vs 100 frames; 6 schemas `additionalProperties` | regenerate both in one pass |
| P2-16 | Optimiser candidates not tied to the root | `optimiser.py` L130-217 | gate root gets hotel/zone actions; road/emergency only notify | scope templates to the root neighbourhood |

## 33. P3 Findings

| ID | Title | Evidence |
|---|---|---|
| P3-01 | numpy/Accelerate spurious matmul RuntimeWarnings every cycle | `backend*.log`; reproduced on a random matrix |
| P3-02 | React Router v7 future-flag warnings; `favicon.ico` 404 | console |
| P3-03 | Entity-detail forecast chart y-axis labels clipped | screenshot `detail_02_cascade.png` |
| P3-04 | Grammar: "1 entities", "1 minutes further" (also for 0 s) | Commander, nudges, journey advice |
| P3-05 | 4.0 MB JS bundle (mocks bundled into every build) | build output |
| P3-06 | Synchronous DB writes inside async handlers (`_audit`, Commander log) | `routes.py` L428-446 |
| P3-07 | Docs vs code: README/RUNNING say `python run.py`, but there is no `python` on PATH; `Frontend/.env` absent; 90 vs 100 frames; CLAUDE.md §12/§16 narrative (reroute UNSTABLE via gate_5) differs from the observed "did not converge" / metro_d; the `routes.py` comment claims compliance feedback | §2, §15, §16 |
| P3-08 | `.env` never loaded (harmless today) | code search |
| P3-09 | Mock pending chip still "2 PENDING" after one card is executing | T19 |

## 34. What Is Actually Working

- The backend boots, seeds and verifies the topology, loads the trained GNN, and runs a stable, deterministic 30-sim-s cycle well within budget.
- The REST API is complete, validated and schema-conformant for normal inputs; error envelopes are uniform in shape.
- The WebSocket fan-out, per-cycle events, resync, ping/pong and attendee filtering work.
- The approve, reject and expire state machine works, with WS events, 3 nudges, audit rows and settlement at +900 sim-s.
- Reset reproduces the run exactly; identical action sequences reproduce identical demos (three rehearsals).
- Observed entities (53/66) display accurate values (raw observations).
- Mechanically the frontend works in live mode: map, detail panel, cascade arcs, queue ordering, approve, HUD, twin toggle, Commander, what-if panel, metrics page, attendee accept/decline. The mock replay loops and re-arms correctly.
- Test suites and CI steps pass (pytest 32/32, mocks 17/17, `npm test`, build).
- The GNN fallback paths (NaN, exception, bad normalisation) and the deterministic propagator work and share the output shape.
- Outbound congestion-aware routing works (e.g. avoids a high-crowding gate_5 via gate_6).

## 35. What Is Actually Broken

- Twin estimates for unobserved entities, and every number derived from them: hero row, Commander subject, first triggers, load variance.
- The twin branch, and with it what-if, the counterfactual, regret, certificate accuracy, and peak/variance reduction metrics.
- Redistribution semantics of approved interventions (receivers lose demand); `notify_only` changes the world.
- Equilibrium convergence (relief-size artefact) and hence the verdict distribution and reasons.
- Risk banding above 100% utilisation.
- GNN cascade semantics (root-independent, poorly calibrated, root explosion, alert flood, low recall).
- Frontend recovery after backend restart; engine recovery after speed ≤ 0.
- Commander cache after reset, entity matching, and intent coverage.
- Attendee zone recommendation and nudge copy, hard-coded journey, step-free, return trips.
- Live capacity injection direction; seek semantics; nudge expiry; proposal ceiling.

## 36. What Is Missing

- Crowd movement along graph edges.
- Schedule-change handling and an end-of-event stop.
- Off-peak recommendations.
- A use for attendee feedback (compliance never read).
- Stackelberg leader search (`solve_leader`).
- The `execution`, `cascade_prediction` and `risk_state` persistence.
- Anomaly UI.
- Demo controls in the UI.
- Return/egress routing.
- A real TSFM/LightGBM forecaster.
- Authentication for operators.
- A production build that targets the live backend by default.
- Concurrent-event modelling (`concurrent_events` always `[]`).

## 37. Recommended Fix Order (informational only — nothing was changed)

1. **Demo safety, cheap:**
   - WS client resets `lastSeq` on open / accepts `resync` (P0-07).
   - Validate `speed_multiplier > 0` (P1-01).
   - Clear the Commander cache on reset (P1-02).
   - Run the demo backend without `--reload`.
   - Slow the sim or lengthen the TTL for the approval step (P1-11).
2. **Twin for unobserved entities** (P0-01): this single change removes the 200% hero artefact, the wrong Commander subject, the spurious triggers and the load-variance inflation.
3. **Twin branch / what-if / counterfactual** (P0-02, P0-04): bounded dynamics, multipliers applied once, separate RNG, entity-ID validation; then re-check the peak/variance reduction metrics.
4. **Intervention effect model** (P0-03): source −x / receiver +x with ramp-in; no-op for `notify_only`.
5. **Equilibrium convergence and scope** (P0-05), then re-examine the STABLE/UNSTABLE distribution and certificate accuracy.
6. **Risk band for utilisation ≥ 1.0** (P0-06).
7. **Cascade root selection, hysteresis and root conditioning** (P0-08), plus alert de-duplication.
8. **Attendee and Commander correctness** (P1-03 to P1-06).
9. **Metric honesty and UI copy** (P2-11, P2-12), then regenerate the mocks and schemas together (P1-13, P2-15).
10. Remaining P2/P3 items.

---

### Final git check

`git status --porcelain` at the end of the audit:

```
 M CLAUDE.md                 <- pre-existing before the audit (not modified by this audit)
?? FINAL_AUDIT_REPORT.md     <- this report (the only file created)
```

`Backend/eventflow.db`: MD5 `168ea37ed076591c918753a24a660c11` before and after (unchanged). Git-ignored caches were created or updated as noted in §3. All servers, browsers and harnesses started for the audit were stopped.
