"""Partial accommodation demand: lodging_share, room conversion, check-in/out, capacity,
unmet demand and conservation — on the real flow model (no network)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys

import pytest

from app.catalog import build_properties
from app.config import get_config
from app.errors import ApiError
from app.ml_reference.generator import SyntheticGenerator
from app.services.events import EventSchedule
from app.topology import build_topology

CFG = get_config().raw
GPR = float(CFG["hospitality"]["guests_per_room"])


def event(eid="e1", att=20000, share=0.25, venue="stadium_main", start="16:00", end="18:30", **kw):
    ev = {"event_id": eid, "name": eid, "venue_entity_id": venue, "start_time": f"2026-09-04T{start}:00Z",
          "end_time": f"2026-09-04T{end}:00Z", "expected_attendance": att, "status": "scheduled", **kw}
    if share is not None:
        ev["lodging_share"] = share
    return ev


def gen(*events, **hosp):
    topo = build_topology()
    topo["properties"] = build_properties(topo["nodes"], topo["edges"])
    topo["events"] = list(events)
    return SyntheticGenerator({"demand": CFG["demand"], "hospitality": {**CFG["hospitality"], **hosp},
                               "sim_start_time": CFG["event"]["sim_start_time"], "critical_utilisation": 0.9}, topo, 42)


def run_to(g, hhmm: str):
    h, m = map(int, hhmm.split(":"))
    target = ((h - 14) * 60 + m) * 60
    while g.elapsed_sec() < target:
        g.tick(30)


def check_invariants(g):
    for eid, L in g.lodging_state().items():
        assert abs(L["attendance"] - (L["local_guests"] + L["lodging_guests"])) < 1e-6
        assert abs(L["requested_guests"] - (L["allocated_guests"] + L["unmet_guests"])) < 1e-6, eid
        assert abs(L["lodging_guests"] - (L["allocated_guests"] + L["unmet_guests"] + L["pending_guests"])) < 1e-6 \
            or L["cancelled"], eid
        assert L["checked_out_guests"] <= L["allocated_guests"] + 1e-6 and L["in_house_guests"] >= -1e-9
    for p in g.properties_state():
        assert p["rooms_available_total"] == p["rooms_occupied"] + p["rooms_available"]
        assert 0 <= p["rooms_occupied"] <= p["rooms_available_total"]
        assert p["event_guests"] <= p["effective_guest_capacity"] + 1e-6
    in_house = sum(L["in_house_rooms"] for L in g.lodging_state().values())
    assert abs(in_house - sum(g._event_rooms.values())) < 1e-6          # per-event ledger == property ledger
    for led in g.event_ledger().values():
        assert abs(led["arrived"] - led["inside"] - led["egressed"] - led["pending"]) < 1e-6


# --- A / B / C: the share of attendance that needs a room --------------------------------------
def test_lodging_share_zero_means_no_hotel_demand():
    g = gen(event(share=0.0))
    base = {p["property_id"]: p["rooms_occupied"] for p in g.properties_state()}
    run_to(g, "17:00")
    L = g.lodging_state()["e1"]
    assert L["lodging_guests"] == 0 and L["requested_guests"] == 0 and L["hotel_origin_share"] == 0
    assert L["local_guests"] == 20000
    assert {p["property_id"]: p["rooms_occupied"] for p in g.properties_state()} == base   # baseline only
    check_invariants(g)


def test_lodging_share_one_means_everyone_needs_a_room():
    g = gen(event(att=4000, share=1.0))
    run_to(g, "17:00")
    L = g.lodging_state()["e1"]
    assert L["lodging_guests"] == 4000 and L["local_guests"] == 0
    assert abs(L["allocated_guests"] + L["unmet_guests"] - 4000) < 5          # check-in window complete
    check_invariants(g)


def test_quarter_of_attendance_enters_accommodation_demand():
    g = gen(event(att=20000, share=0.25))
    run_to(g, "17:00")
    L = g.lodging_state()["e1"]
    assert L["lodging_guests"] == 5000 and L["local_guests"] == 15000
    assert abs(L["requested_guests"] - 5000) < 5
    assert abs(L["requested_guests"] / GPR - g.lodging_state()["e1"]["requested_guests"] / GPR) < 1e-9
    rooms = sum(g._lodging["e1"][k] for k in ("placed", "unmet"))
    assert abs(rooms * GPR - L["requested_guests"]) < 1e-6              # rooms = guests / guests_per_room
    check_invariants(g)


# --- D / E / F: inputs that change demand ----------------------------------------------------------
def test_attendance_changes_accommodation_demand():
    a, b = gen(event(att=10000)), gen(event(att=20000))
    run_to(a, "17:00"), run_to(b, "17:00")
    la, lb = a.lodging_state()["e1"], b.lodging_state()["e1"]
    assert lb["lodging_guests"] == 2 * la["lodging_guests"]
    assert abs(lb["requested_guests"] - 2 * la["requested_guests"]) < 5


def test_share_changes_hotel_demand_but_not_attendance():
    lo, hi = gen(event(share=0.1)), gen(event(share=0.5))
    run_to(lo, "15:40"), run_to(hi, "15:40")
    l1, l2 = lo.lodging_state()["e1"], hi.lodging_state()["e1"]
    assert l1["attendance"] == l2["attendance"] == 20000
    assert l2["lodging_guests"] == 5 * l1["lodging_guests"] and l2["local_guests"] < l1["local_guests"]
    assert abs(lo.event_ledger()["e1"]["arrived"] - hi.event_ledger()["e1"]["arrived"]) < 1e-6   # same people come
    assert sum(hi._event_rooms.values()) > sum(lo._event_rooms.values())
    # hotel-origin travel differs: a hotel-heavy event loads the network differently
    tn = [e for e, t in lo.types.items() if t == "transport_node"]
    assert any(abs(lo.utilisation()[e] - hi.utilisation()[e]) > 1e-3 for e in tn)
    check_invariants(lo), check_invariants(hi)


def test_two_events_keep_their_own_shares_and_the_default_applies():
    g = gen(event("big", att=30000, share=0.4),
            event("expo", att=10000, share=None, venue="convention_centre", start="15:00", end="18:00"))
    run_to(g, "17:00")
    st = g.lodging_state()
    assert st["big"]["lodging_share"] == 0.4 and st["big"]["lodging_share_source"] == "event"
    assert st["expo"]["lodging_share"] == CFG["hospitality"]["default_lodging_share"]
    assert st["expo"]["lodging_share_source"] == "default"
    assert st["big"]["lodging_guests"] == 12000 and st["expo"]["lodging_guests"] == 10000 * st["expo"]["lodging_share"]
    check_invariants(g)


# --- G: validation ---------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [-0.1, 1.5, float("nan"), "lots"])
def test_invalid_lodging_share_is_rejected(bad):
    with pytest.raises(ApiError):
        EventSchedule([event(share=bad)], "e1")
    sched = EventSchedule([event(share=0.2)], "e1")
    with pytest.raises(ApiError):
        sched.update("e1", "2026-09-04T14:00:00Z", lodging_share=bad)
    assert sched.get("e1")["lodging_share"] == 0.2                     # unchanged, never clamped


# --- H: shortage -> unmet, never overbooked ----------------------------------------------------------
def test_hotel_shortage_produces_unmet_demand_not_overbooking():
    g = gen(event(att=120000, share=0.6))
    run_to(g, "17:00")
    L = g.lodging_state()["e1"]
    assert L["unmet_guests"] > 1000
    assert all(p["rooms_available"] == 0 or p["status"] != "saturated" for p in g.properties_state())
    assert sum(p["rooms_available"] for p in g.properties_state()) < 5                   # everything bookable is used
    check_invariants(g)
    # a saturated hotel does not receive further bookings
    before = dict(g._event_rooms)
    g.set_events([event(att=150000, share=0.6)])
    g.tick(30)
    for p in g.properties_state():
        if p["status"] == "saturated":
            assert g._event_rooms[p["property_id"]] <= before[p["property_id"]] + 1e-6
    check_invariants(g)


def test_whatif_hotel_shortage_raises_unmet_without_deleting_attendees():
    g = gen(event(att=60000, share=0.4))
    run_to(g, "14:30")
    live_before = g.properties_state()
    short, base = g.clone(), g.clone()
    short.inject("hotel_shortage", {"rooms_offline_pct": 40})
    short.run(5400, 30), base.run(5400, 30)
    ls, lb = short.lodging_state()["e1"], base.lodging_state()["e1"]
    assert ls["unmet_guests"] > lb["unmet_guests"]
    assert ls["attendance"] == lb["attendance"] and abs(
        short.event_ledger()["e1"]["arrived"] - base.event_ledger()["e1"]["arrived"]) < 1e-6
    assert sum(p["rooms_available_total"] for p in short.properties_state()) < \
        sum(p["rooms_available_total"] for p in base.properties_state())
    assert g.properties_state() == live_before                       # the live world was not touched
    check_invariants(short)


# --- I: check-in and check-out ----------------------------------------------------------------------
def test_occupancy_rises_through_check_in_and_drains_after_check_out():
    g = gen(event(att=20000, share=0.3))
    series = {}
    for t in ("14:00", "15:00", "16:00", "17:00", "20:30", "21:30", "23:30"):
        run_to(g, t)
        series[t] = sum(g._event_rooms.values())
        check_invariants(g)
    assert series["14:00"] < series["15:00"] < series["16:00"]
    assert abs(series["17:00"] - series["16:00"]) / series["17:00"] < 0.05       # stable during the event
    assert series["21:30"] < series["20:30"] * 0.8 and series["23:30"] < series["17:00"] * 0.05
    assert g.lodging_state()["e1"]["checked_out_guests"] > 0.95 * g.lodging_state()["e1"]["allocated_guests"]


def test_cancelling_an_event_releases_its_rooms():
    g = gen(event(att=20000, share=0.3))
    run_to(g, "16:30")
    g.set_events([event(att=20000, share=0.3, status="cancelled")])
    g.tick(30)
    assert sum(g._event_rooms.values()) < 1e-6 and g.lodging_state()["e1"]["lodging_guests"] == 0
    check_invariants(g)


def test_unmet_lodging_guests_travel_from_home_not_dropped():
    g = gen(event(att=150000, share=0.8))
    run_to(g, "17:00")
    L = g.lodging_state()["e1"]
    assert L["unmet_guests"] > 0 and L["hotel_origin_share"] < 0.8
    assert abs(L["hotel_origin_share"] - 0.8 * L["allocated_guests"] / L["requested_guests"]) < 1e-9


# --- J / K: determinism -----------------------------------------------------------------------------
def _fingerprint(g) -> str:
    return hashlib.sha256(json.dumps([g.properties_state(), g.lodging_state()], sort_keys=True,
                                     default=str).encode()).hexdigest()


def test_same_inputs_same_allocation():
    a, b = gen(event(att=90000, share=0.5)), gen(event(att=90000, share=0.5))
    run_to(a, "16:30"), run_to(b, "16:30")
    assert _fingerprint(a) == _fingerprint(b)


def test_allocation_is_identical_in_a_fresh_process():
    g = gen(event(att=90000, share=0.5))
    run_to(g, "16:30")
    here = _fingerprint(g)
    code = ("import sys; sys.path.insert(0, '.'); from tests.test_accommodation_demand import gen, event, run_to, "
            "_fingerprint; g = gen(event(att=90000, share=0.5)); run_to(g, '16:30'); print(_fingerprint(g))")
    env = {**os.environ, "PYTHONHASHSEED": "123"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=os.getcwd(),
                         timeout=300)
    assert out.stdout.strip().splitlines()[-1] == here, out.stderr[-500:]


def test_generated_style_properties_with_beds_limit_guests():
    g = gen(event(att=30000, share=0.5))
    for p in g.properties:                       # pretend every hotel mapped fewer beds than rooms x gpr
        p["bed_capacity"] = int(p["rooms_total"] * 1.0)
    g2 = gen(event(att=30000, share=0.5))
    topo_props = g.properties
    g2.properties = topo_props
    g2._props_by_id = {p["property_id"]: p for p in topo_props}
    run_to(g2, "17:00")
    for p in g2.properties_state():
        assert p["rooms_available_total"] <= math.ceil(p["bed_capacity"] / GPR) + 1
        assert p["event_guests"] <= p["effective_guest_capacity"] + 1e-6
