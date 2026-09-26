"""Phase 1C — intervention physics (FINAL_AUDIT_REPORT P0-03).

Every redistribution is SOURCE -> transferred people -> DESTINATION, executed as
a conserved generator transfer. Before Phase 1, `apply_relief` cut demand on
every target — the reroute destination metro_c LOST 36% and notify_only removed
4% of real demand.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import schemas as S
from app.api import routes
from app.db import models
from app.db.base import SessionLocal, create_all
from app.db.seed import clear_run_tables
from app.services.engine import Engine, set_engine


def _engine(cycles: int, setup=None) -> Engine:
    create_all()
    clear_run_tables()
    engine = Engine()
    set_engine(engine)
    loop = asyncio.new_event_loop()
    try:
        for c in range(cycles):
            if setup and c == setup[0]:
                setup[1](engine)
            loop.run_until_complete(engine.run_cycle())
    finally:
        loop.close()
    return engine


def _cycles(engine: Engine, n: int) -> None:
    loop = asyncio.new_event_loop()
    try:
        for _ in range(n):
            loop.run_until_complete(engine.run_cycle())
    finally:
        loop.close()


def _on(engine: Engine, coro_fn):
    """Route handlers resolve the global engine — point it at the right world."""
    set_engine(engine)
    return _run(coro_fn())


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _people(engine: Engine, eid: str) -> float:
    return engine.generator.evaluate()[eid]["current_count"]


def _find(engine: Engine, itype: str, root: str | None = None) -> dict:
    for i in engine.store.interventions.values():
        if i["intervention_type"] == itype and i["status"] == "proposed" and (root is None or i["triggered_by_entity_id"] == root):
            return i
    raise AssertionError(f"no proposed {itype} for {root}")


# --- reroute: source down, destination up, conserved ---------------------------------------
@pytest.fixture(scope="module")
def reroute_pair():
    acted, rejected = _engine(49), _engine(49)
    item = _find(acted, "reroute_transport", "metro_b")
    assert item["effect_model"] == "transfer"
    assert item["action_effects"] == [{
        "source_entity_id": "metro_b", "destination_entity_id": "metro_c",
        "planned_fraction": round(item["estimated_relief_pct"] / 100, 4),
        "ramp_sec": item["estimated_delay_sec"], "duration_sec": None,
    }]
    _on(acted, lambda: routes.approve(item["intervention_id"], S.ApproveRequest(operator_id="op_test")))
    rid = _find(rejected, "reroute_transport", "metro_b")["intervention_id"]
    _on(rejected, lambda: routes.reject(rid, S.RejectRequest(operator_id="op_test")))
    trace = []
    for _ in range(30):
        _cycles(acted, 1)
        _cycles(rejected, 1)
        trace.append({e: (_people(rejected, e), _people(acted, e)) for e in ("metro_b", "metro_c", "gate_5", "gate_3")})
    return acted, rejected, acted.store.interventions[item["intervention_id"]], trace


def test_reroute_moves_people_from_source_to_destination(reroute_pair):
    _, _, item, trace = reroute_pair
    for step in trace:
        (mb_no, mb_yes), (mc_no, mc_yes) = step["metro_b"], step["metro_c"]
        assert mb_yes < mb_no, "source must lose people"
        assert mc_yes > mc_no, "destination must GAIN people (it lost 36% before Phase 1)"
        assert (mb_no - mb_yes) == pytest.approx(mc_yes - mc_no, rel=1e-9), "people are conserved"
    # Entities not involved are untouched.
    for step in trace:
        assert step["gate_3"][0] == step["gate_3"][1]


def test_reroute_ramps_in_over_its_delay_and_uses_the_response_rate(reroute_pair):
    acted, _, item, trace = reroute_pair
    eff = item["action_effects"][0]
    full = eff["planned_fraction"] * item["_response_rate"]
    ramp_cycles = eff["ramp_sec"] / 30
    # moved share of the do-nothing source after k cycles = full * min(1, k/ramp)
    for k, step in enumerate(trace, start=1):
        mb_no, mb_yes = step["metro_b"]
        share = (mb_no - mb_yes) / mb_no
        assert share == pytest.approx(full * min(1.0, k / ramp_cycles), rel=1e-6), k
    assert 0 < item["_response_rate"] < 1


def test_reroute_is_reproducible():
    def once():
        e = _engine(49)
        iid = _find(e, "reroute_transport", "metro_b")["intervention_id"]
        _on(e, lambda: routes.approve(iid, S.ApproveRequest(operator_id="op_test")))
        _cycles(e, 10)
        return json.dumps(e.generator.evaluate(), sort_keys=True), json.dumps(e.store.entity_states, sort_keys=True)
    assert once() == once()


# --- notify_only has no crowd effect ----------------------------------------------------------
def test_notify_only_does_not_touch_demand():
    acted, rejected = _engine(49), _engine(49)
    a = _find(acted, "notify_only", "metro_b")
    assert a["effect_model"] == "none" and a["action_effects"] == []
    resp = _on(acted, lambda: routes.approve(a["intervention_id"], S.ApproveRequest(operator_id="op_test")))
    assert resp.status == "executing"
    rid = _find(rejected, "notify_only", "metro_b")["intervention_id"]
    _on(rejected, lambda: routes.reject(rid, S.RejectRequest(operator_id="op_test")))
    assert acted.generator.effects() == []
    for _ in range(20):
        _cycles(acted, 1)
        _cycles(rejected, 1)
        assert acted.generator.evaluate() == rejected.generator.evaluate()
        assert acted.store.entity_states == rejected.store.entity_states


# --- the manually observed Gate 2 + Gate 3 -> Gate 1 case ---------------------------------------
def _overload_gate_2(engine: Engine) -> None:
    # Scratch world: Gate 2 genuinely over capacity (~2.0) by cutting its capacity.
    engine.generator._capacity_mult["gate_2"] = 0.23


@pytest.fixture(scope="module")
def gate_case():
    acted = _engine(181, setup=(170, _overload_gate_2))
    control = _engine(181, setup=(170, _overload_gate_2))
    truth = acted.generator.evaluate()
    assert truth["gate_2"]["utilisation"] > 1.8
    assert 0.6 < truth["gate_3"]["utilisation"] < 1.0
    assert truth["gate_1"]["utilisation"] < 0.6
    node_state = acted.store.node_state_for_ml()
    candidates = acted.registry.optimiser.generate({
        "root_entity_id": "gate_2", "cascade": None, "node_state": node_state,
        "edges": acted.store.edges, "sim_time": acted.store.sim_time}, 5)
    item = next(c for c in candidates if c["intervention_type"] == "gate_redistribution")
    item["certificate"] = acted.certify_candidate(item, node_state)
    item.update(status="proposed", created_at=acted.store.sim_time, expires_at="2099-01-01T00:00:00Z", rank_score=0.1)
    item.pop("_ttl_sec", None)
    acted.store.interventions[item["intervention_id"]] = item
    display_before = {g: acted.store.entity_states[g]["utilisation"] for g in ("gate_1", "gate_2", "gate_3")}
    item["_audit_rows_before"] = _audit_rows(item["intervention_id"])
    _on(acted, lambda: routes.approve(item["intervention_id"], S.ApproveRequest(operator_id="op_test")))
    _cycles(acted, 10)
    _cycles(control, 10)
    return acted, control, item, display_before


def test_gate_case_payload_names_sources_and_destination(gate_case):
    _, _, item, _ = gate_case
    moves = {(e["source_entity_id"], e["destination_entity_id"]) for e in item["action_effects"]}
    assert moves == {("gate_2", "gate_1"), ("gate_3", "gate_1")}
    assert item["effect_model"] == "transfer"


def test_gate_case_sources_fall_receiver_rises_people_conserved(gate_case):
    acted, control, _, _ = gate_case
    yes, no = acted.generator.evaluate(), control.generator.evaluate()
    assert yes["gate_2"]["current_count"] < no["gate_2"]["current_count"]
    assert yes["gate_3"]["current_count"] < no["gate_3"]["current_count"]
    assert yes["gate_1"]["current_count"] > no["gate_1"]["current_count"], "receiver must not fall"
    given = (no["gate_2"]["current_count"] - yes["gate_2"]["current_count"]) + \
            (no["gate_3"]["current_count"] - yes["gate_3"]["current_count"])
    received = yes["gate_1"]["current_count"] - no["gate_1"]["current_count"]
    assert received == pytest.approx(given, rel=1e-9)
    for other in ("gate_4", "gate_5", "gate_6", "metro_b"):
        assert yes[other] == no[other]


def test_gate_case_display_follows_including_unobserved_gate_2(gate_case):
    acted, control, _, _ = gate_case
    assert not acted.store.entity_states["gate_2"]["is_observed"], "premise: gate_2 is twin-estimated"
    # Gate 2 sits above the store's 0..2 utilisation bound in BOTH worlds (true
    # utilisation ~2.05), so compare the people shown; the others by utilisation.
    assert control.store.entity_states["gate_2"]["utilisation"] == 2.0
    people = lambda e, g: e.store.entity_states[g]["current_count"]
    assert people(acted, "gate_2") < people(control, "gate_2")
    for g, direction in (("gate_3", -1), ("gate_1", +1)):
        diff = acted.store.entity_states[g]["utilisation"] - control.store.entity_states[g]["utilisation"]
        assert diff * direction > 0, (g, diff)


def _audit_rows(subject_id: str) -> list[str]:
    with SessionLocal() as s:
        return [r.action for r in s.query(models.AuditLog).filter(models.AuditLog.subject_id == subject_id).all()]


def test_approval_is_audited(gate_case):
    _, _, item, _ = gate_case
    # IDs are deterministic and audit_log is durable, so compare to the count before approving.
    assert _audit_rows(item["intervention_id"]) == item["_audit_rows_before"] + ["approve"]


# --- every redistribution type follows the same contract ---------------------------------------
def _node_state(engine, overrides):
    ns = engine.store.node_state_for_ml()
    for eid, u in overrides.items():
        ns[eid] = {**ns[eid], "utilisation": u, "forecast_1800": u}
    return ns


@pytest.mark.parametrize("itype,root,overrides", [
    ("reroute_transport", "metro_b", {"metro_b": 0.95}),
    ("deploy_shuttle", "metro_b", {"metro_b": 0.95}),
    ("gate_redistribution", "gate_3", {"gate_3": 0.95, "gate_1": 0.3}),
    ("parking_redistribution", "parking_p1", {"parking_p1": 0.95}),
    ("zone_incentive", "zone_core", {"zone_core": 0.95, "zone_west": 0.3}),
    ("accommodation_rebalance", "hotel_core_cluster", {"hotel_core_cluster": 0.95}),
    ("stagger_entry", "gate_3", {"gate_3": 0.95}),
])
def test_redistribution_types_follow_the_source_destination_contract(itype, root, overrides):
    engine = _engine(60)
    candidates = engine.registry.optimiser.generate({
        "root_entity_id": root, "cascade": None, "node_state": _node_state(engine, overrides),
        "edges": engine.store.edges, "sim_time": engine.store.sim_time}, 5)
    item = next(c for c in candidates if c["intervention_type"] == itype)
    assert item["action_effects"], itype
    engine.execute_intervention(item)
    later = engine.generator.elapsed_sec() + 1200          # past every ramp
    acted = engine.generator.evaluate(later)
    untouched = engine.generator.evaluate(later, exclude=item["_effect_ids"])
    for eff in item["action_effects"]:
        src, dst = eff["source_entity_id"], eff["destination_entity_id"]
        if eff["duration_sec"]:
            # deferral: held back during the window, released afterwards
            during = engine.generator.evaluate(engine.generator.elapsed_sec() + 30)
            during_no = engine.generator.evaluate(engine.generator.elapsed_sec() + 30, exclude=item["_effect_ids"])
            assert during[src]["current_count"] < during_no[src]["current_count"]
            assert acted[src] == untouched[src]
            assert dst is None and item["effect_model"] == "deferral"
            continue
        assert acted[src]["current_count"] < untouched[src]["current_count"], (itype, src)
        assert acted[dst]["current_count"] > untouched[dst]["current_count"], (itype, dst)
    moved_out = sum(untouched[e]["current_count"] - acted[e]["current_count"] for e in {x["source_entity_id"] for x in item["action_effects"]})
    moved_in = sum(acted[e]["current_count"] - untouched[e]["current_count"]
                   for e in {x["destination_entity_id"] for x in item["action_effects"] if x["destination_entity_id"]})
    if item["effect_model"] == "transfer":
        assert moved_in == pytest.approx(moved_out, rel=1e-9)


@pytest.mark.parametrize("itype", ["notify_only", "emergency_corridor"])
def test_operational_actions_have_no_crowd_effect(itype):
    engine = _engine(60)
    item = {"intervention_id": f"int_{itype}", "intervention_type": itype, "target_entity_ids": ["road_4", "emergency_north"],
            "triggered_by_entity_id": "road_4", "estimated_relief_pct": 7.5, "effect_model": "none", "action_effects": []}
    before = engine.generator.evaluate(engine.generator.elapsed_sec() + 600)
    engine.execute_intervention(item)
    assert engine.generator.effects() == []
    assert engine.generator.evaluate(engine.generator.elapsed_sec() + 600) == before
