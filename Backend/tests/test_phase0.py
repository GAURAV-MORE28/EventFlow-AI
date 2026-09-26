"""Phase 0 — demo safety, recovery and observability regression tests.

Each block pins a failure measured in FINAL_AUDIT_REPORT.md:
  * speed validation (P1-01: `speed_multiplier <= 0` froze the engine forever)
  * Commander cache across reset (P1-02)
  * observer controls: pause / play / step / next_decision / auto-pause
  * intervention lifecycle while paused and at high speed

These tests drive the real background cycle loop (TestClient runs the app's
lifespan), so a few of them wait on wall-clock time; every wait is bounded.
"""
from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.engine import get_engine

CONTROL = "/api/v1/demo/control"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _cycle(client) -> int:
    return client.get("/api/v1/health").json()["cycle_number"]


def _wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _advances(client, within: float = 3.0) -> bool:
    start = _cycle(client)
    return _wait_for(lambda: _cycle(client) > start, timeout=within)


# --- B. speed validation --------------------------------------------------------
@pytest.mark.parametrize("bad", [0, -1, -0.5, 1e9])
def test_invalid_speed_is_rejected_with_the_error_envelope(client, bad):
    before = get_engine().speed_multiplier
    response = client.post(CONTROL, json={"speed_multiplier": bad})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert get_engine().speed_multiplier == before  # nothing was applied


@pytest.mark.parametrize("raw", ['{"speed_multiplier": NaN}', '{"speed_multiplier": Infinity}',
                                 '{"speed_multiplier": -Infinity}', '{"speed_multiplier": "fast"}',
                                 '{"speed_multiplier": null, "action": "set_speed"}'])
def test_non_finite_or_malformed_speed_is_rejected(client, raw):
    before = get_engine().speed_multiplier
    response = client.post(CONTROL, content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert get_engine().speed_multiplier == before


def test_invalid_speed_cannot_freeze_the_simulation(client):
    client.post(CONTROL, json={"action": "play", "speed_multiplier": 150})
    for bad in (0, -1):
        assert client.post(CONTROL, json={"speed_multiplier": bad}).status_code == 400
    assert _advances(client), "cycle loop stopped after rejected speed requests"


@pytest.mark.parametrize("good", [0.5, 1, 2, 5, 10, 60, 150])
def test_valid_speeds_are_applied(client, good):
    response = client.post(CONTROL, json={"speed_multiplier": good})
    assert response.status_code == 200
    body = response.json()
    assert body["speed_multiplier"] == pytest.approx(good)
    assert get_engine().speed_multiplier == pytest.approx(good)


def test_tiny_speed_then_fast_speed_resumes_promptly(client):
    """A valid but very slow speed (0.01x => one cycle per ~50 wall-minutes) must
    not trap the loop in a long sleep: switching back to a fast speed has to
    take effect immediately, not after the old sleep expires."""
    client.post(CONTROL, json={"action": "play", "speed_multiplier": 150})
    assert _advances(client)
    assert client.post(CONTROL, json={"speed_multiplier": 0.01}).status_code == 200
    time.sleep(0.6)  # let the loop enter its (very long) inter-cycle wait
    stalled_at = _cycle(client)
    assert client.post(CONTROL, json={"speed_multiplier": 150}).status_code == 200
    assert _wait_for(lambda: _cycle(client) > stalled_at, timeout=2.0), \
        "speed change did not interrupt the in-progress inter-cycle wait"


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "abc"])
def test_engine_guard_rejects_bad_speed_from_config_or_direct_callers(bad):
    from app.services.engine import validated_speed

    with pytest.raises(ValueError):
        validated_speed(bad)
    assert validated_speed("60") == 60.0


# --- C. Commander cache vs reset ---------------------------------------------------
QUESTION = "What is the biggest problem right now?"


def _play_until(client, target: int) -> None:
    client.post(CONTROL, json={"action": "play", "speed_multiplier": 150})
    assert _wait_for(lambda: _cycle(client) >= target, timeout=30.0)
    client.post(CONTROL, json={"action": "pause"})
    time.sleep(0.3)  # let an in-flight cycle finish


def _ask(client) -> dict:
    response = client.post("/api/v1/commander/query", json={"query": QUESTION})
    assert response.status_code == 200
    return response.json()


def test_commander_cache_is_reused_within_one_session(client):
    client.post(CONTROL, json={"action": "reset"})
    _play_until(client, 15)
    first = _ask(client)
    second = _ask(client)
    assert first["is_cached"] is False
    assert second["is_cached"] is True
    assert second["response"] == first["response"]


def test_reset_invalidates_commander_cache(client):
    client.post(CONTROL, json={"action": "reset"})
    _play_until(client, 40)
    before = _ask(client)
    assert before["is_cached"] is False
    client.post(CONTROL, json={"action": "reset"})
    _play_until(client, 3)
    after = _ask(client)
    engine = get_engine()
    assert engine.store.cycle_number < 40
    assert after["is_cached"] is False, "pre-reset answer served after reset"
    # The answer is built from the CURRENT timeline: its counts match now
    # (either Commander template: "N entities are critical" / "N critical and").
    summary = engine.store.summary
    assert re.search(rf"\b{summary['critical_count']} (entities are )?critical", after["response"]), after
    assert re.search(rf"\b{summary['high_count']} (are at )?high", after["response"]), after
    assert after["response"] != before["response"]


def test_negative_cycle_delta_is_never_fresh(client):
    """Even within one run (e.g. after a seek rewinds the clock), an entry whose
    cycle is AHEAD of the current cycle must not count as fresh."""
    engine = get_engine()
    commander = engine.commander
    client.post(CONTROL, json={"action": "pause"})
    time.sleep(0.3)
    query = "Why is Metro B becoming critical?"
    fresh = client.post("/api/v1/commander/query", json={"query": query}).json()
    entry = commander._cache[query.strip().lower()]
    entry["cycle_number"] = engine.store.cycle_number + 3  # from the "future"
    replay = client.post("/api/v1/commander/query", json={"query": query}).json()
    assert fresh["is_cached"] is False
    assert replay["is_cached"] is False


# --- E/F. observer mode + intervention lifecycle -----------------------------------
def _status(client) -> dict:
    response = client.get("/api/v1/demo/status")
    assert response.status_code == 200
    return response.json()


def _fresh_run(client, *, auto_pause: bool) -> None:
    client.post(CONTROL, json={"action": "pause"})
    client.post(CONTROL, json={"action": "reset", "auto_pause_on_intervention": auto_pause})


def test_status_reports_speed_in_sim_and_wall_terms(client):
    client.post(CONTROL, json={"action": "pause", "speed_multiplier": 10})
    body = _status(client)
    assert body["status"] == "paused"
    assert body["pause_reason"]["kind"] == "operator"
    assert body["cycle_sec"] == 30
    assert body["wall_seconds_per_cycle"] == pytest.approx(3.0)      # 30 sim-s / 10x
    client.post(CONTROL, json={"speed_multiplier": 0.5})
    assert _status(client)["wall_seconds_per_cycle"] == pytest.approx(60.0)
    client.post(CONTROL, json={"speed_multiplier": 150})
    assert _status(client)["wall_seconds_per_cycle"] == pytest.approx(0.2)  # engine floor


def test_pause_freezes_and_play_resumes(client):
    client.post(CONTROL, json={"action": "play", "speed_multiplier": 150})
    assert _advances(client)
    client.post(CONTROL, json={"action": "pause"})
    time.sleep(0.3)
    frozen = _cycle(client)
    time.sleep(1.0)
    assert _cycle(client) == frozen, "cycles advanced while paused"
    body = client.post(CONTROL, json={"action": "play"}).json()
    assert body["status"] == "playing" and body["pause_reason"] is None
    assert _advances(client)


def test_step_advances_exactly_one_real_cycle(client):
    client.post(CONTROL, json={"action": "pause"})
    time.sleep(0.3)
    start = _cycle(client)
    for i in range(1, 4):
        body = client.post(CONTROL, json={"action": "step"}).json()
        assert body["status"] == "paused"
        assert body["cycle_number"] == start + i
    time.sleep(0.5)
    assert _cycle(client) == start + 3, "loop ran on its own after stepping"


def test_auto_pause_on_a_real_proposal_and_lifecycle_while_paused(client):
    engine = get_engine()
    _fresh_run(client, auto_pause=True)
    client.post(CONTROL, json={"action": "play", "speed_multiplier": 150})
    assert _wait_for(lambda: _status(client)["status"] == "paused", timeout=30.0), "never auto-paused"
    status = _status(client)
    reason = status["pause_reason"]
    assert reason["kind"] == "intervention_proposed"
    # The pause is tied to a REAL proposal created on the cycle it paused at.
    proposed = client.get(f"/api/v1/interventions/{reason['intervention_id']}").json()
    assert proposed["status"] == "proposed"
    assert proposed["created_at"] == reason["sim_time"] == engine.store.sim_time
    assert proposed["triggered_by_entity_id"] == reason["entity_id"]
    assert proposed["expires_at"] > engine.store.sim_time

    # Paused => the sim clock and the proposal's TTL are frozen in wall time.
    frozen = _cycle(client)
    time.sleep(1.5)
    assert _cycle(client) == frozen
    assert client.get(f"/api/v1/interventions/{reason['intervention_id']}").json()["status"] == "proposed"

    # Human decision while paused: approve, duplicate approve, reject another.
    iid = reason["intervention_id"]
    approved = client.post(f"/api/v1/interventions/{iid}/approve", json={"operator_id": "op_test"})
    assert approved.status_code == 200 and approved.json()["status"] == "executing"
    duplicate = client.post(f"/api/v1/interventions/{iid}/approve", json={"operator_id": "op_test"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "INTERVENTION_ALREADY_RESOLVED"
    others = [i for i in client.get("/api/v1/interventions").json()["interventions"]
              if i["intervention_id"] != iid]
    if others:
        rid = others[0]["intervention_id"]
        assert client.post(f"/api/v1/interventions/{rid}/reject", json={"operator_id": "op_test"}).status_code == 200
        assert client.post(f"/api/v1/interventions/{rid}/approve", json={"operator_id": "op_test"}).status_code == 409
    assert _cycle(client) == frozen, "approving/rejecting must not advance the paused sim"

    # Resume: the approved action is executing and the sim moves on.
    client.post(CONTROL, json={"action": "play"})
    assert _advances(client)
    assert client.get(f"/api/v1/interventions/{iid}").json()["status"] in ("executing", "completed")
    client.post(CONTROL, json={"action": "pause", "auto_pause_on_intervention": False})


def test_expiry_semantics_unchanged_when_stepping_the_clock(client):
    """TTL is still sim-time: 900 sim-s = 30 cycles, however slowly they pass."""
    _fresh_run(client, auto_pause=True)
    client.post(CONTROL, json={"action": "play", "speed_multiplier": 150})
    assert _wait_for(lambda: _status(client)["status"] == "paused", timeout=30.0)
    iid = _status(client)["pause_reason"]["intervention_id"]
    client.post(CONTROL, json={"auto_pause_on_intervention": False})
    for _ in range(29):
        client.post(CONTROL, json={"action": "step"})
    assert client.get(f"/api/v1/interventions/{iid}").json()["status"] == "proposed"  # 870 sim-s
    client.post(CONTROL, json={"action": "step"})                                      # 900 sim-s
    assert client.get(f"/api/v1/interventions/{iid}").json()["status"] == "expired"
    late = client.post(f"/api/v1/interventions/{iid}/approve", json={"operator_id": "op_test"})
    assert late.status_code == 409


def test_expiry_at_high_speed_keeps_the_state_machine_consistent(client):
    _fresh_run(client, auto_pause=False)
    client.post(CONTROL, json={"action": "play", "speed_multiplier": 150})
    assert _wait_for(lambda: any(
        i["status"] == "expired" for i in client.get("/api/v1/interventions?status=all&limit=100").json()["interventions"]
    ), timeout=40.0)
    client.post(CONTROL, json={"action": "pause"})
    items = client.get("/api/v1/interventions?status=all&limit=100").json()["interventions"]
    assert {i["status"] for i in items} <= {"proposed", "expired", "executing", "completed", "rejected"}
    expired = next(i for i in items if i["status"] == "expired")
    assert expired["expires_at"] <= get_engine().store.sim_time
    assert client.post(f"/api/v1/interventions/{expired['intervention_id']}/approve",
                       json={"operator_id": "op_test"}).status_code == 409


def test_next_decision_runs_until_a_real_proposal_then_pauses(client):
    _fresh_run(client, auto_pause=False)
    body = client.post(CONTROL, json={"action": "next_decision", "speed_multiplier": 150}).json()
    assert body["status"] == "playing" and body["pause_on_next_decision"] is True
    assert _wait_for(lambda: _status(client)["status"] == "paused", timeout=30.0)
    status = _status(client)
    assert status["pause_reason"]["kind"] == "intervention_proposed"
    assert status["pause_on_next_decision"] is False            # one-shot
    assert status["auto_pause_on_intervention"] is False
    iid = status["pause_reason"]["intervention_id"]
    assert client.get(f"/api/v1/interventions/{iid}").json()["status"] == "proposed"


def test_demo_status_is_pushed_over_websocket_and_included_in_resync(client):
    client.post(CONTROL, json={"action": "pause"})
    time.sleep(0.3)
    with client.websocket_connect("/ws?client=command_centre") as ws:
        first = ws.receive_json()
        assert first["event"] == "resync"
        assert first["payload"]["demo"]["status"] == "paused"
        client.post(CONTROL, json={"speed_multiplier": 5})
        msg = ws.receive_json()
        assert msg["event"] == "demo_status"
        assert msg["payload"]["speed_multiplier"] == 5
        assert msg["payload"]["wall_seconds_per_cycle"] == pytest.approx(6.0)
        run_before = msg["payload"]["run_id"]
        client.post(CONTROL, json={"action": "reset"})
        events = [ws.receive_json() for _ in range(2)]
        resync = next(m for m in events if m["event"] == "resync")
        assert resync["payload"]["demo"]["run_id"] == run_before + 1
        assert resync["payload"]["state"]["cycle_number"] == 0


def test_attendee_socket_does_not_receive_demo_status(client):
    client.post(CONTROL, json={"action": "pause"})
    with client.websocket_connect("/ws?client=attendee&attendee_id=att_demo_1") as ws:
        assert ws.receive_json()["event"] == "resync"
        client.post(CONTROL, json={"speed_multiplier": 7})
        ws.send_json({"action": "ping"})
        assert ws.receive_json()["event"] == "pong", "attendee received operator-only demo_status"


@pytest.mark.parametrize("bad", [{"action": "fly"}, {"auto_pause_on_intervention": "sometimes"},
                                 {"action": "set_speed"}])
def test_invalid_observer_requests_are_rejected(client, bad):
    response = client.post(CONTROL, json=bad)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
