"""Phase 1D — equilibrium / stability (FINAL_AUDIT_REPORT P0-05).

Before Phase 1: relief >= 8% never converged (relief re-applied every
iteration), any overloaded zone anywhere poisoned every certificate, and
"minutes to breach" was 26 - overshoot x 100.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import schemas as S
from app.api import routes
from app.config import get_config
from app.db.base import create_all
from app.db.seed import clear_run_tables
from app.ml_reference.equilibrium import EquilibriumSolver
from app.services.engine import Engine, set_engine
from app.topology import build_topology


def _solver(**overrides) -> EquilibriumSolver:
    cfg = dict(get_config().raw["equilibrium"])
    cfg.update(seed=42, **overrides)
    return EquilibriumSolver(cfg)


TOPO = build_topology()
NODES = {n["entity_id"]: n for n in TOPO["nodes"]}


def _ns(overrides: dict[str, float], default: float = 0.5) -> dict[str, dict]:
    return {e: {"entity_id": e, "entity_type": n["entity_type"], "display_name": n["display_name"],
                "nominal_capacity": n["nominal_capacity"], "utilisation": overrides.get(e, default)}
            for e, n in NODES.items()}


def _reroute(relief: float, src: str = "metro_b", dst: str = "metro_c") -> dict:
    return {"intervention_id": f"int_test_{src}_{dst}_{relief}", "intervention_type": "reroute_transport",
            "target_entity_ids": [src, dst], "triggered_by_entity_id": src, "estimated_relief_pct": relief,
            "effect_model": "transfer",
            "action_effects": [{"source_entity_id": src, "destination_entity_id": dst,
                                "planned_fraction": relief / 100, "ramp_sec": 0, "duration_sec": None}]}


def _engine(cycles: int) -> Engine:
    create_all()
    clear_run_tables()
    engine = Engine()
    set_engine(engine)
    loop = asyncio.new_event_loop()
    try:
        for _ in range(cycles):
            loop.run_until_complete(engine.run_cycle())
    finally:
        loop.close()
    return engine


# --- fixed point: converge / oscillate / diverge ---------------------------------------------
def test_simple_stable_intervention_converges():
    cert = _solver().certify(_reroute(20), _ns({"metro_b": 0.95, "metro_c": 0.3}), TOPO["edges"], TOPO["segments"])
    assert cert["converged"] and not cert["oscillation_risk"]
    assert cert["iterations"] < 40
    assert cert["verdict"] == "STABLE", cert
    assert 0 < cert["response_rate"] < 1


def test_genuine_oscillation_is_detected():
    # Undamped best response with a near-step sigmoid: everyone moves, the
    # receiver overloads, everyone moves back — the 03 §5.2 oscillation pattern.
    solver = _solver(damping=1.0, sigmoid_k=400.0)
    cert = solver.certify(_reroute(100), _ns({"metro_b": 0.9, "metro_c": 0.85}), TOPO["edges"], TOPO["segments"])
    assert cert["oscillation_risk"] is True
    assert cert["verdict"] == "UNSTABLE"
    assert "oscillates" in cert["reason"]


def test_genuine_divergence_is_detected():
    bad_segments = [dict(s, price_elasticity=float("nan")) for s in TOPO["segments"]]
    cert = _solver().certify(_reroute(20), _ns({"metro_b": 0.95, "metro_c": 0.3}), TOPO["edges"], bad_segments)
    assert cert["converged"] is False
    assert cert["verdict"] == "UNSTABLE"


def test_verdict_is_not_tied_to_relief_magnitude():
    """Relief sweep 2..35%: every run converges (the old loop hit the cap at
    >= 8%), and a large relief into a roomy receiver is STABLE."""
    solver = _solver()
    roomy = _ns({"metro_b": 0.95, "metro_c": 0.25})
    for relief in (2, 4, 8, 12, 20, 35):
        cert = solver.certify(_reroute(relief), roomy, TOPO["edges"], TOPO["segments"])
        assert cert["converged"], (relief, cert)
        assert cert["iterations"] < 40, (relief, cert["iterations"])
        assert cert["verdict"] == "STABLE", (relief, cert)
    # ...and the same large relief into a nearly-full receiver is not.
    tight = solver.certify(_reroute(35), _ns({"metro_b": 0.95, "metro_c": 0.97}), TOPO["edges"], TOPO["segments"])
    assert tight["converged"] and tight["verdict"] in ("CONDITIONAL", "UNSTABLE"), tight


def test_unrelated_overload_does_not_change_the_certificate():
    solver = _solver()
    calm = solver.certify(_reroute(20), _ns({"metro_b": 0.95, "metro_c": 0.3}), TOPO["edges"], TOPO["segments"])
    stressed = solver.certify(_reroute(20), _ns({"metro_b": 0.95, "metro_c": 0.3, "zone_core": 1.6, "gate_5": 1.4}),
                              TOPO["edges"], TOPO["segments"])
    assert calm == stressed   # before Phase 1 a 1.05 zone alone made a 4% notify UNSTABLE


def test_same_inputs_same_certificate():
    s = _solver()
    args = (_reroute(35), _ns({"metro_b": 0.95, "metro_c": 0.6}), TOPO["edges"], TOPO["segments"])
    assert json.dumps(s.certify(*args), sort_keys=True) == json.dumps(_solver().certify(*args), sort_keys=True)


# --- certificates against the event-world model --------------------------------------------------
@pytest.fixture(scope="module")
def live():
    return _engine(49)


def test_certificate_peak_matches_an_independent_world_rollout():
    # Stop on the cycle a reroute is CREATED, so "now" is the certificate's snapshot.
    create_all()
    clear_run_tables()
    live = Engine()
    set_engine(live)
    loop = asyncio.new_event_loop()
    item = None
    try:
        for _ in range(120):
            loop.run_until_complete(live.run_cycle())
            item = next((i for i in live.store.interventions.values()
                         if i["intervention_type"] == "reroute_transport" and i["created_at"] == live.store.sim_time), None)
            if item:
                break
    finally:
        loop.close()
    assert item, "no reroute proposed within 120 cycles"
    cert = live.store.certificates[item["intervention_id"]]
    eff = item["action_effects"][0]
    worst = 0.0
    for m_rate in (0.4, 0.6, 0.9):
        m, *_ = live.registry.equilibrium._solve_response(
            item["action_effects"], live.store.node_state_for_ml(), live.store.segments,
            item["estimated_relief_pct"] / 100, m_rate)
        world = live.generator.clone()
        world.add_transfer(eff["source_entity_id"], eff["destination_entity_id"], eff["planned_fraction"] * m,
                           ramp_sec=eff["ramp_sec"])
        start = world.elapsed_sec()
        for k in range(1, 1800 // 30 + 1):
            now = world.evaluate(start + 30 * k)
            worst = max(worst, now[eff["source_entity_id"]]["utilisation"], now[eff["destination_entity_id"]]["utilisation"])
    assert cert["max_zone_utilisation"] == pytest.approx(worst, abs=1e-4)


def test_breach_time_is_read_from_the_rollout_or_omitted():
    engine = _engine(49)
    # Force a genuine saturation trap: the receiver is nearly full.
    engine.generator._capacity_mult["metro_c"] = 0.5
    ns = engine.store.node_state_for_ml()
    ns["metro_c"] = {**ns["metro_c"], "utilisation": engine.generator.evaluate()["metro_c"]["utilisation"]}
    cert = engine.certify_candidate(_reroute(35), ns)
    rollout = engine.world_rollout()
    if cert["verdict"] == "UNSTABLE" and "within" in cert["reason"]:
        minutes = int(cert["reason"].split("within ")[1].split(" minute")[0])
        m, *_ = engine.registry.equilibrium._solve_response(_reroute(35)["action_effects"], ns, engine.store.segments, 0.35,
                                                              next(r["compliance_rate"] for r in cert["compliance_sweep"] if r["verdict"] == "UNSTABLE"))
        traj = rollout([{**_reroute(35)["action_effects"][0], "fraction": 0.35 * m}], ["metro_c"], 1800)["metro_c"]
        first = next(i for i, u in enumerate(traj) if u >= 1.0)
        assert minutes == max(1, round((first + 1) * 30 / 60))
    else:
        assert "within" not in cert["reason"]
    assert "26" not in cert["reason"] or "within 26" not in cert["reason"]


def test_execution_uses_the_certified_response(live):
    item = next(i for i in live.store.interventions.values()
                if i["intervention_type"] == "reroute_transport" and i["status"] == "proposed")
    set_engine(live)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(routes.approve(item["intervention_id"], S.ApproveRequest(operator_id="op_test")))
    finally:
        loop.close()
    assert item["_response_rate"] == item["certificate"]["response_rate"]
    eff = live.generator.effects()[-1]
    assert eff["fraction"] == pytest.approx(item["action_effects"][0]["planned_fraction"] * item["certificate"]["response_rate"])


# --- leader ---------------------------------------------------------------------------------------
def test_leader_returns_ranked_offers():
    solver = _solver()
    opts = solver.solve_leader({"intervention": _reroute(35)}, _ns({"metro_b": 0.95, "metro_c": 0.45}),
                               TOPO["edges"], TOPO["segments"])
    assert [o["scale"] for o in sorted(opts, key=lambda o: o["scale"])] == [0.25, 0.5, 0.75, 1.0]
    order = {"STABLE": 0, "CONDITIONAL": 1, "UNSTABLE": 2}
    keys = [(order[o["verdict"]], -o["expected_source_relief_pct"]) for o in opts]
    assert keys == sorted(keys), "best-first: most stable, then most relief"
    assert opts[0]["expected_source_relief_pct"] > 0
    assert solver.solve_leader({"intervention": {"action_effects": []}}, _ns({}), TOPO["edges"], TOPO["segments"]) == []
