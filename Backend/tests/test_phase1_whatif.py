"""Phase 1B — What-If and counterfactual correctness (FINAL_AUDIT_REPORT P0-02, P0-04).

What-if = two clones of the live event-world model at the same instant:
do-nothing vs scenario, same horizon. Counterfactual at settlement = the live
world with the intervention's effects excluded, same instant.
"""
from __future__ import annotations

import asyncio
import json
import statistics

import numpy as np
import pytest

from app import schemas as S
from app.api import routes
from app.db.base import create_all
from app.db.seed import clear_run_tables
from app.errors import ApiError
from app.services.engine import Engine, set_engine
from app.services.simulation import SIMULATIONS, validate_scenarios


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


def _cycles(engine: Engine, n: int) -> None:
    loop = asyncio.new_event_loop()
    try:
        for _ in range(n):
            loop.run_until_complete(engine.run_cycle())
    finally:
        loop.close()


def _whatif(engine: Engine, scenarios: list[dict], horizon: int = 1800) -> dict:
    validate_scenarios(scenarios, engine.store.nodes)
    return SIMULATIONS._execute(engine, scenarios, horizon)


def _fingerprint(engine: Engine) -> str:
    gen = engine.generator
    return json.dumps({
        "elapsed": gen.elapsed_sec(),
        "effects": gen.effects(),
        "cap": gen._capacity_mult, "int": gen._intensity_mult, "global": gen._global_intensity,
        "now": gen.evaluate(),
        "states": engine.store.entity_states,
        "cycle": engine.store.cycle_number,
        "twin_X": engine.registry.twin._X.round(9).tolist(),
        "twin_rng": str(engine.registry.twin._rng.bit_generator.state),
    }, sort_keys=True, default=str)


ALL_SCENARIOS = {
    "attendance_up": [{"scenario_type": "attendance_delta", "params": {"delta_pct": 20}}],
    "attendance_down": [{"scenario_type": "attendance_delta", "params": {"delta_pct": -20}}],
    "blue_line_capacity": [{"scenario_type": "metro_capacity_delta", "params": {"entity_id": "line_blue", "delta_pct": -15}}],
    "road_capacity": [{"scenario_type": "road_capacity_delta", "params": {"entity_id": "road_4", "delta_pct": -50}}],
    "rain_heavy": [{"scenario_type": "weather_rain", "params": {"intensity": "heavy"}}],
    "gate_closure": [{"scenario_type": "gate_closure", "params": {"entity_id": "gate_3"}}],
    "transport_outage": [{"scenario_type": "transport_outage", "params": {"entity_id": "metro_b"}}],
    "parking_loss": [{"scenario_type": "parking_loss", "params": {"entity_id": "parking_p1", "delta_pct": -50}}],
    "hotel_shortage": [{"scenario_type": "hotel_shortage", "params": {}}],
    "concurrent_event": [{"scenario_type": "concurrent_event", "params": {"overlap_pct": 25}}],
    "combined": [{"scenario_type": "combined", "params": {"scenarios": [
        {"scenario_type": "metro_capacity_delta", "params": {"entity_id": "line_blue", "delta_pct": -15}},
        {"scenario_type": "weather_rain", "params": {"intensity": "heavy"}}]}}],
}


@pytest.fixture(scope="module")
def engine():
    return _engine(60)


@pytest.fixture(scope="module")
def results(engine):
    before = _fingerprint(engine)
    out = {name: _whatif(engine, sc) for name, sc in ALL_SCENARIOS.items()}
    assert _fingerprint(engine) == before, "a what-if changed live state / twin / RNG"
    return out


def _traj(res, side, eid):
    return res["_trajectories"][side][eid]


def _ratios(res, eids, lo=0.05):
    out = []
    for e in eids:
        for b, s in zip(_traj(res, "baseline", e), _traj(res, "scenario", e)):
            if b > lo:
                out.append(s / b)
    return out


def _counts(engine, res, side, eid):
    cap = engine.generator.capacity(eid)
    return [u * cap for u in _traj(res, side, eid)]


# --- same snapshot, same model, same horizon ----------------------------------------
def test_baseline_branch_is_the_live_world_rolled_forward(engine, results):
    res = results["attendance_up"]
    start = res["_trajectories"]["start_elapsed_sec"]
    assert start == engine.generator.elapsed_sec()
    probe = engine.generator.clone()
    for k in (0, 29, 59):
        live_future = probe.evaluate(start + 30 * (k + 1))
        for eid in ("metro_b", "gate_3", "zone_core", "hotel_north_cluster"):
            assert _traj(res, "baseline", eid)[k] == pytest.approx(live_future[eid]["utilisation"], abs=1e-12)
    assert len(_traj(res, "baseline", "metro_b")) == len(_traj(res, "scenario", "metro_b")) == 1800 // 30


def test_values_are_numerically_sane(results):
    for name, res in results.items():
        for side in ("baseline", "scenario"):
            peak = res[side]["peak_utilisation"]
            assert np.isfinite(peak) and 0.0 <= peak <= 3.0, (name, side, peak)  # was 20.48-964.05


# --- each scenario changes the mechanism it claims to change -------------------------
def test_attendance_scales_demand_once(engine, results):
    ups = _ratios(results["attendance_up"], engine.store.nodes)
    downs = _ratios(results["attendance_down"], engine.store.nodes)
    assert statistics.median(ups) == pytest.approx(1.2, abs=0.02)
    assert statistics.median(downs) == pytest.approx(0.8, abs=0.02)
    # Applied once: the ratio at the last step equals the ratio at the first.
    b, s = _traj(results["attendance_down"], "baseline", "metro_b"), _traj(results["attendance_down"], "scenario", "metro_b")
    assert s[0] / b[0] == pytest.approx(s[-1] / b[-1], abs=0.03)


def test_capacity_reduction_makes_the_target_more_utilised(results):
    for name, eid, factor in (("blue_line_capacity", "line_blue", 1 / 0.85),
                              ("road_capacity", "road_4", 2.0),
                              ("parking_loss", "parking_p1", 2.0)):
        r = _ratios(results[name], [eid])
        assert r and all(x == pytest.approx(factor, rel=1e-9) for x in r), (name, r[:3])


def test_rain_hits_roads_parking_gates_only(engine, results):
    res = results["rain_heavy"]
    by_type = lambda t: [e for e, n in engine.store.nodes.items() if n["entity_type"] == t]
    assert statistics.median(_ratios(res, by_type("road") + by_type("parking") + by_type("gate"))) == pytest.approx(1.22, abs=0.03)
    for zone in by_type("zone"):
        assert _traj(res, "baseline", zone) == _traj(res, "scenario", zone)


def test_hotel_shortage_hits_hotels_only(engine, results):
    res = results["hotel_shortage"]
    hotels = [e for e, n in engine.store.nodes.items() if n["entity_type"] == "hotel"]
    assert statistics.median(_ratios(res, hotels)) == pytest.approx(1.15, abs=0.02)
    for e in ("metro_b", "gate_3", "zone_core"):
        assert _traj(res, "baseline", e) == _traj(res, "scenario", e)


def test_concurrent_event_scales_everything(engine, results):
    assert statistics.median(_ratios(results["concurrent_event"], engine.store.nodes)) == pytest.approx(1.25, abs=0.02)


def test_gate_closure_empties_the_gate_and_conserves_people(engine, results):
    res = results["gate_closure"]
    assert max(_traj(res, "scenario", "gate_3")) == pytest.approx(0.0, abs=1e-9)
    gates = [e for e, n in engine.store.nodes.items() if n["entity_type"] == "gate"]
    for k in range(len(_traj(res, "baseline", "gate_3"))):
        before = sum(_counts(engine, res, "baseline", g)[k] for g in gates)
        after = sum(_counts_scenario(engine, res, g)[k] for g in gates)
        assert after == pytest.approx(before, rel=1e-9)   # turned away, not deleted
    for g in gates:
        if g != "gate_3":
            assert all(s >= b for b, s in zip(_traj(res, "baseline", g), _traj(res, "scenario", g)))


def _counts_scenario(engine, res, eid):
    world = engine.generator.clone()
    for sc in ALL_SCENARIOS["gate_closure"]:
        world.inject(sc["scenario_type"], sc["params"])
    cap = world.capacity(eid)
    return [u * cap for u in _traj(res, "scenario", eid)]


def test_transport_outage_moves_riders_to_substitutes(engine, results):
    res = results["transport_outage"]
    assert max(_traj(res, "scenario", "metro_b")) == pytest.approx(0.0, abs=1e-9)
    subs = [e["dst_entity_id"] for e in engine.store.edges
            if e["src_entity_id"] == "metro_b" and e["edge_type"] == "substitutes_for"]
    assert subs
    for s in subs:
        assert all(x > y for y, x in zip(_traj(res, "baseline", s), _traj(res, "scenario", s)))


def test_unrelated_scenarios_give_different_results(results):
    keys = [json.dumps(r["scenario"], sort_keys=True) for r in results.values()]
    assert len(set(keys)) == len(keys)


# --- validation --------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    [{"scenario_type": "gate_closure", "params": {"entity_id": "gate_99"}}],
    [{"scenario_type": "gate_closure", "params": {"entity_id": "road_4"}}],
    [{"scenario_type": "gate_closure", "params": {}}],
    [{"scenario_type": "metro_capacity_delta", "params": {"entity_id": "metro_b", "delta_pct": -100}}],
    [{"scenario_type": "parking_loss", "params": {"entity_id": "parking_p1"}}],
    [{"scenario_type": "weather_rain", "params": {"intensity": "biblical"}}],
    [{"scenario_type": "combined", "params": {"scenarios": [{"scenario_type": "gate_closure", "params": {"entity_id": "nope"}}]}}],
])
def test_invalid_scenarios_are_rejected(engine, bad):
    with pytest.raises(ApiError) as err:
        validate_scenarios(bad, engine.store.nodes)
    assert err.value.code == "INVALID_SCENARIO"


# --- isolation / reproducibility ---------------------------------------------------------
def test_same_scenario_twice_is_identical(engine):
    a = _whatif(engine, ALL_SCENARIOS["rain_heavy"])
    b = _whatif(engine, ALL_SCENARIOS["rain_heavy"])
    assert a["_trajectories"] == b["_trajectories"]
    assert a["scenario"] == b["scenario"]


def test_whatif_order_does_not_change_the_live_trajectory():
    plain = _engine(40)
    _cycles(plain, 20)
    ab = _engine(40)
    _whatif(ab, ALL_SCENARIOS["gate_closure"]); _whatif(ab, ALL_SCENARIOS["attendance_up"])
    _cycles(ab, 20)
    ba = _engine(40)
    _whatif(ba, ALL_SCENARIOS["attendance_up"]); _whatif(ba, ALL_SCENARIOS["gate_closure"])
    _cycles(ba, 20)
    ref = json.dumps(plain.store.entity_states, sort_keys=True)
    assert json.dumps(ab.store.entity_states, sort_keys=True) == ref
    assert json.dumps(ba.store.entity_states, sort_keys=True) == ref
    assert np.array_equal(plain.registry.twin._X, ab.registry.twin._X)
    assert np.array_equal(plain.registry.twin._X, ba.registry.twin._X)


# --- counterfactual: same world, same instant, no clamp ----------------------------------
def _approve(engine: Engine, iid: str) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(routes.approve(iid, S.ApproveRequest(operator_id="op_test")))
    finally:
        loop.close()


def test_counterfactual_equals_an_independent_do_nothing_run():
    acted = _engine(49)
    proposals = acted.store.interventions_by_status("proposed", 10)
    assert proposals, "seed 42 produced no proposal by cycle 49"
    target = next((p for p in proposals if p["intervention_type"] != "notify_only"), proposals[0])
    iid = target["intervention_id"]
    _approve(acted, iid)
    applied = acted.store.interventions[iid]
    sources = applied["_sources"]
    _cycles(acted, 30)   # settles exactly 900 sim-s (30 cycles) after approval
    entry = next(e for e in acted.store.regret_entries if e["intervention_id"] == iid)

    untouched = _engine(49 + 30)          # the same world with no approval, at the settlement instant
    assert untouched.generator.elapsed_sec() == acted.generator.elapsed_sec()
    cf = sum(untouched.generator.evaluate()[s]["utilisation"] for s in sources) / len(sources)
    actual = sum(acted.generator.evaluate()[s]["utilisation"] for s in sources) / len(sources)
    assert entry["realised_relief_pct"] == pytest.approx(round((cf - actual) / cf * 100, 1), abs=0.051)
    at_approval = applied["_source_util_at_approval"]
    assert entry["counterfactual_relief_pct"] == pytest.approx(round((at_approval - cf) / at_approval * 100, 1), abs=0.051)
    assert entry["regret"] == pytest.approx(entry["predicted_relief_pct"] - entry["realised_relief_pct"], abs=0.011)
    for key in ("realised_relief_pct", "counterfactual_relief_pct"):
        assert entry[key] not in (-100.0, 100.0), "clamped value"
