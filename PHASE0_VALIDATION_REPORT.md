# EventFlow AI — Phase 0 Validation Report
## Demo Safety, Recovery & Observability

| | |
|---|---|
| Date | 2026-09-26 |
| Branch / base commit | `Yash` @ `bab9bf5` (uncommitted working tree) |
| Scope | Workstreams A–G only. **No numerical-core change**: twin, what-if, counterfactual, intervention physics, equilibrium, risk and cascade are untouched (Phase 1+). |
| Test isolation | Every backend test/run used a scratch SQLite DB via `DATABASE_URL`. `Backend/eventflow.db` was not touched. |
| Status | **COMPLETE** — every acceptance item below was executed and passed. |

Every item follows the same method: reproduce the fault with a failing test, make the smallest fix, re-run the focused test, then run the surrounding regressions.

---

## A. WebSocket restart / recovery

| | |
|---|---|
| **Issue** | After a backend restart the UI showed "connected" but stopped updating until a manual refresh (audit P0-07). |
| **Root cause** | `Frontend/src/lib/ws.js` kept `lastSeq` across connections and dropped every frame with `seq <= lastSeq`. The backend's `seq` is a per-process counter (`ws/manager.py`) that restarts at 1, so the new server's `resync` and all ticks were discarded. |
| **Fix** | **A new connection is a new sequence context.** `lastSeq = 0` in every socket's `onopen`. Handlers are bound to the socket that created them (`ws !== socket` ⇒ ignore), so a stale socket cannot poison the new sequence. Store `replaceAll` also clears per-run UI state (tracked actions, what-if overlay, selection, cascade overlay) when a resync shows a new `run_id` or a cycle regression. |
| **Regression tests** | `Frontend/scripts/test-ws-reconnect.mjs` drives the real `ws.js` with a fake socket: cycles 59/60 → drop → reconnect → server seq restarts at 1 → resync and cycles 2, 3, 4 applied. It also checks duplicate, out-of-order and stale-socket frames are dropped, backoff 1 s → 2 s resets after success, and `demo_status` routing. `Frontend/scripts/test-store-timeline.mjs` drives the real zustand store: reset (new `run_id`) and restart (cycle regression) clear per-run state; a same-run reconnect keeps it. Both are in `npm test`. |
| **Commands** | `node scripts/test-ws-reconnect.mjs`; `node scripts/test-store-timeline.mjs`; browser restart test (headless Chrome over CDP, `npm run dev:live` + `run_demo.py`) |
| **Result** | Before the fix the test failed: `last_seq: 102` carried into the new connection (expected 0). After the fix both scripts pass. |

**Before → after (real browser; the backend was killed and relaunched while the UI was open):**

| t after restart | Before (audit): backend / UI cycle | After (Phase 0, `run_demo.py`): backend / UI cycle |
|---|---|---|
| +2 s | 5 / **60 (frozen)** | 5 / **5** |
| +8 s | 17 / **60** | 17 / **17** |
| +16 s | 33 / **60** | 33 / **33** |

The chip went RECONNECTING then back to connected, and no refresh was needed. The same result was obtained with `run.py`.

## B. Simulation speed validation

| | |
|---|---|
| **Issue** | `speed_multiplier` 0 or negative was accepted, and the engine then slept `30 / 1e-6` s — frozen permanently. Play and reset could not recover it (audit P1-01). |
| **Root causes found** | (1) No validation on `DemoControlRequest.speed_multiplier`. (2) **NaN / ±Infinity** (valid JSON literals for Python) were also accepted: the engine speed became NaN, then the response crashed with `ValueError: Out of range float values are not JSON compliant: nan` — newly found while reproducing. (3) Even a *valid* tiny speed (0.01×) committed the loop to one ~50-minute `asyncio.sleep`, so a later speed change had no effect until it ended. (4) The 400 handler echoed the raw invalid `input`, which could itself fail to serialise for NaN. |
| **Fix** | Schema: `Field(gt=0, le=150, allow_inf_nan=False)` (150× = the engine's 0.2 s wall floor, so higher values were indistinguishable), and `set_speed` without a value is a 400. Engine: `validated_speed()` guards config and direct callers. The inter-cycle wait now runs in ≤0.1 s slices that re-read speed and pause. `_cycle_lock` serialises cycles with reset and step. Validation errors return only `loc/msg/type`. |
| **Regression tests** | `Backend/tests/test_phase0.py`: 0, −1, −0.5, 1e9 → 400; NaN, Infinity, −Infinity, `"fast"`, `set_speed` with null → 400 with the existing `INVALID_REQUEST` envelope and speed unchanged; the loop keeps cycling after rejected requests; 0.5, 1, 2, 5, 10, 60, 150 applied; 0.01× held, then 150× resumes within 2 s; engine guard rejects 0, −1, NaN, inf, "abc". |
| **Commands** | `DATABASE_URL=sqlite:///<scratch>.db .venv/bin/python -m pytest tests/test_phase0.py -q`; live `curl`/Python checks against `run.py` |
| **Result** | Before: 12 failures (0 and −1 → 200; NaN/Inf → serialisation `ValueError`; "speed change did not interrupt the in-progress inter-cycle wait"). After: all pass. |

**Before → after (live backend, audit scenario):**

| Scenario | Before | After |
|---|---|---|
| speed 0, −1, −5 | 200 "playing", cycle frozen at 0 permanently | `400 INVALID_REQUEST` |
| speed NaN | not tested in the audit (accepted; would crash) | `400 INVALID_REQUEST` |
| Cycles in 2 s after the rejected requests | — | 4 |
| 0.01× held 1.5 s, then 60× | (0 held 1.5 s froze the engine; reset didn't recover) | 4 cycles in the next 2 s |

## C. Commander cache reset / invalidation

| | |
|---|---|
| **Issue** | Asked at cycle ~45, reset, asked again at cycle 3: the pre-reset answer came back with `is_cached: true` (audit P1-02). |
| **Root cause** | `commander.py`: freshness was `cycle_now − cycle_cached ≤ 4`, which is **true for any negative delta**. `Engine._reset` never cleared the cache. |
| **Fix** | Timeline epoch `Engine.run_id` (starts at 1, +1 per reset). Cache entries store `run_id`, and `_is_fresh` requires the same run **and** `0 ≤ Δcycle ≤ 4`. `_reset` also calls `commander.clear_cache()` (two independent defences). A backend restart starts with an empty in-memory cache. Caching within a session is kept. |
| **Regression tests** | `test_commander_cache_is_reused_within_one_session` (second identical ask at the same cycle → `is_cached: true`); `test_reset_invalidates_commander_cache` (ask at cycle ≥40, reset, ask at cycle ≥3 → `is_cached: false`, counts match the current summary, text differs); `test_negative_cycle_delta_is_never_fresh`. |
| **Mutation check** | With the old rule patched back in, `test_negative_cycle_delta_is_never_fresh` **fails**, so it guards the bug. The reset test still passes under the old rule because `clear_cache()` is the second defence. |
| **Result** | Before: reset test failed (`pre-reset answer served after reset`); negative-delta test failed. After: all pass. |

**Before → after (live, audit scenario):**
- **Before:** at cycle 3 after reset the response was `is_cached: true`, "…1 entities are critical and 9 are at high risk…" (the pre-reset text).
- **After:** at cycle ~45 the repeat ask is cached (`true`). After reset, at cycle 3, the response is `is_cached: false`: "Overall risk is 38 (moderate), with 0 critical and 0 high-risk entities…"

## D. Demo-safe backend execution

| | |
|---|---|
| **Issue** | `run.py` uses `--reload`: a file save restarts the sim, and a crashed or killed worker left the reloader holding :8000 (`[Errno 48]`, hung requests). |
| **Fix** | New `Backend/run_demo.py`: `uvicorn app.main:app`, 0.0.0.0:8000, `reload=False`, one process. If the port is busy it exits 1 with a message naming `lsof`. `run.py` is unchanged for development. |
| **Test (executed)** | 1) Started while `run.py` held the port → "port 8000 is already in use…", exit 1. 2) Started cleanly → ready in **1.37 s**, **one** process (no reloader, no `multiprocessing.spawn` child), log contains no "reload". 3) SIGTERM → "Shutting down / Application shutdown complete / Finished server process", process exited, **port 8000 free**. 4) Restart → cycle 1 at 14:00:30. 5) Browser: kill + relaunch under the open UI → reconnected, UI cycle equal to backend cycle (table in A). 6) Also observed: edits did **not** hot-apply to the running `run_demo.py` process until it was restarted — which is exactly the intended property. |

## E. Intervention timing / demo safety

| | |
|---|---|
| **Issue** | At 60× a proposal lived about 15 s of wall time before expiring. |
| **Approach** | Intervention semantics were **not** changed: TTL is still 900 sim-s and expiry still runs only inside a cycle. Observability comes from controlling the clock instead: slower speeds, pause, step, and auto-pause on a real proposal. Paused ⇒ no cycle ⇒ no expiry. |
| **Regression tests** (`test_phase0.py`) | Auto-pause on a real proposal: the paused-at proposal exists, is `proposed`, `created_at == pause sim_time`, `expires_at` is later. The cycle and status are frozen for 1.5 s. Approve while paused → 200 `executing`; duplicate approve → 409 `INTERVENTION_ALREADY_RESOLVED`; reject another → 200, then approve it → 409. The cycle doesn't move during decisions; after play the sim advances and the approved action is `executing`/`completed`. **Expiry by stepping:** 29 steps (870 sim-s) → still `proposed`; the 30th (900 sim-s) → `expired`; approve → 409. **High speed (150×):** expired proposals have `expires_at ≤ sim_time`, all statuses valid, approve-after-expiry → 409. **Slow speeds:** 0.5× reports 60 s per cycle; 10× 3 s; 150× 0.2 s. |
| **Result** | All pass (and stable across 3 repeated runs). |

**Before → after:**
- **Before:** at 60× a card existed for 14.9–15.2 s wall.
- **After (browser):** with "Pause on proposal", the proposal `int_82b35` stayed `proposed` with the clock frozen (cycle 10 → 10 over 3 s). The banner showed "expires in 15 sim-min (≈ 15 s real at 60× — clock frozen while paused)".

## F. Observer / Judge mode

**Backend.**
- `GET /demo/status` returns `status`, `speed_multiplier`, `cycle_number`, `run_id`, `cycle_sec`, `wall_seconds_per_cycle`, `auto_pause_on_intervention`, `pause_on_next_decision` and `pause_reason`.
- `POST /demo/control` gains the actions `step` and `next_decision` and the field `auto_pause_on_intervention`.
- The control response is the same status object; a WS `demo_status` event (operator clients only) and a resync `demo` field carry it too.
- Reset broadcasts a `resync` so every UI drops the old timeline.
- All changes are additive. The 28 exported JSON schemas are **byte-identical** to the pre-Phase-0 export (`diff -r`), since `DemoControlResponse` is not an exported contract schema.

**Frontend.**
- `ObserverBar` sits under the TopBar.
- `ObserverDecisionBanner` sits over the map.
- `lib/useResolveIntervention.js` is shared with the queue: the same approve/reject path, T0 snapshot and rollback.
- Honest labels: "LIVE SIMULATION" / "RECORDED REPLAY" (was "LIVE FEED" / "SIMULATION"); the live cycle is the real count (was cycle mod 100, so cycle 104 read "4"); "Human approval required" (was "Autonomous Dispatch Ready").
- Speed = sim-s per real s, shown as "1 cycle = 30 sim-s ≈ X real".
- No crowd-movement animation was added.

**Browser end-to-end test** (`fe_observer.mjs`: headless Chrome 154 over CDP; `npm run dev:live`; `run_demo.py`) — **19/19 pass**:

| Check | Evidence |
|---|---|
| Observer bar renders in live mode | "● PLAYING · sim 14:04 · cycle 9 · … 1 cycle = 30 sim-s ≈ 0.5 s real" |
| UI Reset restarts the run | backend cycle 3 |
| Speed 10× | backend 10; "30 sim-s ≈ 3 s real"; 0.5× → "≈ 1 min real" |
| Pause | backend `paused`; UI cycle 5 → 5 over 2.5 s; chip "❚❚ PAUSED BY OPERATOR" |
| Play | UI cycle 5 → 10 |
| Step ×2 | backend 10 → 12, UI 12 |
| Auto-pause on a real proposal | paused at cycle 10 for `int_82b35` (road_9, "Notify operations team"); banner shows cycle, entity, load 40%, HIGH·61, "(twin estimate, not a sensor reading)", certificate, optimiser estimate, expiry |
| Frozen while paused | cycle 10 → 10 over 3 s; proposal still `proposed` |
| Approve & resume | `executing`; sim resumed and **legitimately** re-paused at cycle 11 on the next real proposal (`int_4dde4`) |
| Decision status visible | "Previous decision: approved “Notify operations team” — status EXECUTING"; no improvement claim |
| Reject & resume | `rejected`; sim resumed (cycle 11 → 12, re-paused on the next real proposal) |
| Next decision | paused on the next real proposal (`int_20d7d`, cycle 13) |
| Backend restart (SIGTERM + relaunch) | chip RECONNECTING → backend 9 / UI 9, status PLAYING, pace from the new process |
| Stale HUD after restart | cleared |
| Console | 0 errors before the outage. During the deliberate outage only the browser's own `WebSocket connection … failed` lines appear (expected for reconnect attempts). |

**Mock mode** (`npx vite --port 5175`): the bar shows "Recorded replay — observer controls need the live backend (npm run dev:live)"; 0 controls rendered; 0 `/demo/*` requests; replay advances (frame 2 → 5); badge "RECORDED REPLAY"; 0 app errors (pre-existing favicon 404 only).

## G. End-to-end verification & regression

| Command | Purpose | Result |
|---|---|---|
| `DATABASE_URL=<scratch> .venv/bin/python -m pytest tests/ -q` | full backend suite | **70 passed** (32 original + 38 Phase 0) |
| `pytest tests/test_phase0.py` ×3 | flakiness of wall-clock tests | 38/38, 38/38, 38/38 |
| `npm test` | mocks + mock lifecycle + WS reconnect + store timeline | 17/17 mocks valid; 3/3 scripts pass |
| `npm run build` | production build | built in 2.41 s; JS 4,032.55 KB (614 → 618 KB gz, +13 KB raw) |
| `scripts.export_schemas` + `diff -r` vs pre-Phase-0 export | contract safety | identical |
| Audit REST sweep (74 requests) diffed vs audit baseline | no unintended API change | only 2 changes, both intended: speed −5 and 0 now `400 INVALID_REQUEST` |
| Live AJV validation (audit) | — | unchanged endpoints; the sweep diff confirms identical codes |
| `ps %cpu` on `run_demo.py` | loop cost of 0.1 s wake slices | paused 0.0–0.1%; playing 60× 1.8–2.5%; cycle latency 25 ms |
| Browser observer E2E | acceptance gate | 19/19 |
| Browser restart test | WS recovery | UI = backend cycle at +2…+16 s |
| Mock-mode browser check | no mock breakage | pass |

## Acceptance gate

Every item below was executed in this session.

**WebSocket**
- [x] Reconnects automatically after a backend restart.
- [x] Fresh sequence messages are accepted.
- [x] The UI resumes without a refresh.
- [x] Duplicate and out-of-order protection is kept.

**Speed**
- [x] 0 rejected.
- [x] Negative values rejected.
- [x] NaN and ±Infinity rejected.
- [x] Valid speeds work.
- [x] An invalid speed cannot freeze the simulation.

**Commander**
- [x] The cache works within one session.
- [x] Reset invalidates it.
- [x] A negative delta is never fresh.
- [x] Answers after reset reflect the current state.

**Demo backend**
- [x] A command exists and is documented (`run_demo.py`).
- [x] It does not use reload.
- [x] Clean stop and start work.
- [x] The frontend reconnects after a restart.

**Timing**
- [x] Approve, reject and expire semantics are unchanged.
- [x] High speed keeps the state machine consistent.
- [x] Observer mode gives enough wall-clock time: the proposal is frozen until a decision.

**Observer mode**
- [x] Play, pause and resume work.
- [x] Speed changes are safe.
- [x] Status is visible.
- [x] Cycle and time are visible.
- [x] A real proposal triggers an auto-pause.
- [x] Approve and reject work while paused.
- [x] The simulation resumes.
- [x] No fake crowd movement was added.

**Regression**
- [x] Backend, frontend and build all pass.
- [x] No test was weakened or deleted.
- [x] Two of my own new tests were corrected for wrong assumptions: a template-wording assertion, and a query already cached by the previous test. Neither was a product failure.
- [x] No unrelated files changed.

## Incident during testing (disclosed)

At 16:57 a backend that was **not started by this session** was already listening on :8000. It had been running since about 16:35 against the real `Backend/eventflow.db`, next to a user-started mock `vite` on :5173.
- My first `run.py` launch logged `ERROR: [Errno 48] Address already in use`.
- My restart test then ran `kill $(pgrep -f "Python run.py")`. That pattern also matched the pre-existing process and **terminated it at 16:57:39**, which is the DB's last-modified time.
- The DB content changes (`entity_state` restarting at 14:00:30 and running to 2026-09-05 12:06; MD5 now `933bbd9f…`, was `168ea37e…` at the end of the audit) were written by that process during its own run.
- Every process this session started logged a scratch DB path.
- The terminated backend was **not restarted**, because doing so would write to the real DB. That decision is left to the owner.
- The user's `vite` on :5173 (PID 17129) was left running untouched.
- All later kills targeted `run_demo.py` / the specific Vite command lines started by this session.
- The first browser restart run in A also hit that mock UI on :5173 by mistake. Its output was discarded, and the verified A result comes from the live UI on :5174.

## Remaining issues (genuine, not Phase 0 scope)
- With auto-pause on, real proposals arrive on consecutive early cycles (10, 11, 13). They are rooted on twin-estimated entities (audit P0-01), so an observer walking from the start sees several quick pauses before the `metro_b` decision at cycle ~49. "Next decision" or FF 60× shortens this. The root cause is Phase 1 twin work.
- The Pressure Timeline `current_band` can disagree with the live risk band (observed: Arterial 9 "LOW" in the timeline vs "HIGH · 61" in the banner). This is pre-existing and comes from the timeline being built before the second risk pass. It belongs to the Phase 1 numerical/risk work.
- Malformed seek timestamps still return 500 and seek semantics are unchanged (audit P2-05). This was not in the Phase 0 list.
- A malformed client WS frame still creates a zombie socket (audit P2-01). The frontend never sends one; it was not in the Phase 0 list.
