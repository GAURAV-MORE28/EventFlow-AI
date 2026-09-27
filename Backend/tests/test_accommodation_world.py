"""Phase A end to end on a generated world: venue -> blueprint -> activate -> event ->
accommodation demand -> hotel allocation -> simulation cycles -> API (no network)."""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.geospatial.service import BlueprintService, set_blueprint_service
from app.main import app
from app.services.engine import get_engine
from tests.geo_fixtures import SnapshotLike, _pt, city

API = "/api/v1"
LAT, LON = 23.0917717, 72.5973345
DEMO_IDS = {"hotel_north_cluster", "hotel_core_cluster", "htl_central_budget", "stadium_main", "evt_demo"}


def _payload():
    p = city(LAT, LON)
    for k, (tags, n, e) in enumerate((
        ({"tourism": "hotel", "name": "Mapped Rooms Hotel", "rooms": "120"}, 500, -300),
        ({"tourism": "hotel", "name": "Beds Only Hotel", "beds": "90"}, -500, 300),
        ({"tourism": "hotel", "name": "Capacity Rooms Hotel", "capacity:rooms": "60"}, 300, 700),
    )):
        la, lo = _pt(LAT, LON, n, e)
        p["elements"].append({"type": "node", "id": 7_100_000 + k, "lat": la, "lon": lo, "tags": tags})
    return p


class _Snap:
    name, live = "osm_snapshot", False

    def fetch(self, footprint, venue):
        return SnapshotLike(_payload()).fetch(footprint, venue)


def _cycles(n):
    engine = get_engine()
    loop = asyncio.new_event_loop()
    try:
        for _ in range(n):
            loop.run_until_complete(engine.run_cycle())
    finally:
        loop.close()


@pytest.fixture(scope="module")
def client():
    set_blueprint_service(BlueprintService(provider=_Snap()))
    with TestClient(app) as c:
        get_engine().paused = True
        yield c
        c.post(f"{API}/world/synthetic-demo")
    set_blueprint_service(None)


def _activate(c, **event):
    r = c.post(f"{API}/blueprints", json={"venue": {"lat": LAT, "lon": LON, "display_name": "Test Stadium"},
                                          "radius_m": 2000, "venue_capacity": 40000})
    bid = r.json()["build_id"]
    for _ in range(200):
        job = c.get(f"{API}/blueprints/builds/{bid}").json()
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["status"] == "complete", job
    r = c.post(f"{API}/blueprints/{job['blueprint_id']}/activate", json={"event": event})
    assert r.status_code == 200, r.json()
    get_engine().paused = True
    return job["blueprint_id"]


def _hotels(c):
    return c.get(f"{API}/accommodation/hotels").json()


def test_generated_world_accommodation_end_to_end(client):
    _activate(client, expected_attendance=6000, lodging_share=0.15)
    first = _hotels(client)
    hotels = {h["name"]: h for h in first["hotels"]}
    assert len(hotels) >= 5 and not set(hotels) & DEMO_IDS
    assert hotels["Mapped Rooms Hotel"]["rooms_total"] == 120 and hotels["Mapped Rooms Hotel"]["rooms_source"] == "osm_rooms"
    assert hotels["Mapped Rooms Hotel"]["rooms_confidence"] == "high"
    assert hotels["Beds Only Hotel"]["bed_capacity"] == 90
    assert hotels["Beds Only Hotel"]["rooms_source"] == "derived_from_osm_beds"
    assert hotels["Capacity Rooms Hotel"]["rooms_source"] == "osm_capacity_rooms"
    assert any(h["rooms_source"] == "derived_estimate" and h["rooms_confidence"] == "low" for h in hotels.values())
    s0 = first["summary"]
    assert s0["attendance_total"] == 6000 and s0["lodging_guests"] == 900 and s0["local_guests"] == 5100
    assert s0["rooms_available"] > 0                                  # not saturated before check-in
    free0 = s0["rooms_available"]

    _cycles(100)                                                   # 14:00 -> 14:50, check-in under way
    mid = _hotels(client)["summary"]
    _cycles(100)                                                   # -> 15:40
    late = _hotels(client)
    s = late["summary"]
    assert mid["lodging_requested_guests"] < s["lodging_requested_guests"]          # occupancy changes over time
    assert s["rooms_available"] < free0 and s["rooms_occupied"] > s0["rooms_occupied"]
    assert s["rooms_in_service"] == s["rooms_occupied"] + s["rooms_available"]
    assert s["lodging_unmet_guests"] > 0 and "could not be placed" in s["shortage_message"]   # capacity < demand
    assert abs(s["lodging_requested_guests"] - (s["lodging_allocated_guests"] + s["lodging_unmet_guests"])) < 1
    assert abs(s["lodging_guests"] - (s["lodging_allocated_guests"] + s["lodging_unmet_guests"]
                                      + s["lodging_pending_guests"])) < 1
    for h in late["hotels"]:
        assert h["rooms_occupied"] <= h["rooms_in_service"]
        assert h["event_guests"] <= h["effective_guest_capacity"] + 1e-6
    assert any(h["status"] == "saturated" for h in late["hotels"])
    ev = late["lodging_by_event"][0]
    assert ev["lodging_share"] == 0.15 and ev["lodging_share_source"] == "event"
    assert 0 < ev["hotel_origin_share"] < 0.15                  # only housed guests start from hotels
    engine = get_engine()
    led = engine.generator.event_ledger()[engine.events.primary_event_id]
    assert abs(led["arrived"] - led["inside"] - led["egressed"] - led["pending"]) < 1e-6
    # event view carries the same split
    view = client.get(f"{API}/events").json()["events"][0]
    assert view["lodging_share_effective"] == 0.15 and view["lodging_guests"] == 900 and view["local_guests"] == 5100
    # saturated hotels are not recommended; if nothing is free, say so
    rec = client.post(f"{API}/accommodation/recommend", json={}).json()
    sat = {h["property_id"] for h in late["hotels"] if h["status"] == "saturated"}
    assert not {o["property"]["property_id"] for o in rec["options"]} & sat
    if not rec["options"]:
        assert rec["explanation"].startswith("No room available in the selected network") and rec["no_availability_reason"]


def test_hotel_origin_demand_reaches_the_transport_network(client):
    _activate(client, expected_attendance=20000, lodging_share=0.0)
    _cycles(160)
    local_only = {e["entity_id"]: e["utilisation"] for e in client.get(f"{API}/state").json()["entities"]}
    _activate(client, expected_attendance=20000, lodging_share=0.6)
    _cycles(160)
    hotel_heavy = {e["entity_id"]: e["utilisation"] for e in client.get(f"{API}/state").json()["entities"]}
    engine = get_engine()
    # the hotels' own transport nodes carry more of the load when guests start from hotels
    hotel_stations = {p["transport_entity_id"] for p in engine.store.properties if p.get("transport_entity_id")}
    assert hotel_stations
    assert sum(hotel_heavy[s] for s in hotel_stations) > sum(local_only[s] for s in hotel_stations)
    lodging = client.get(f"{API}/accommodation/hotels").json()["summary"]
    assert lodging["attendance_total"] == 20000                           # attendance unchanged


def test_lodging_share_is_validated_over_the_api(client):
    _activate(client, expected_attendance=10000)
    venue = get_engine().world["venue_entity_id"]
    body = {"operator_id": "t", "name": "Night match", "venue_entity_id": venue, "start_time": "2026-09-04T19:00:00Z",
            "end_time": "2026-09-04T21:00:00Z", "expected_attendance": 5000}
    for bad in (-0.2, 1.01):
        assert client.post(f"{API}/events", json={**body, "lodging_share": bad}).status_code == 400
    ok = client.post(f"{API}/events", json={**body, "lodging_share": 0.5}).json()
    assert ok["lodging_share"] == 0.5 and ok["lodging_share_effective"] == 0.5 and ok["lodging_guests"] == 2500
    upd = client.post(f"{API}/events/{ok['event_id']}", json={"operator_id": "t", "lodging_share": 0.1}).json()
    assert upd["lodging_share"] == 0.1 and upd["lodging_guests"] == 500
    assert client.post(f"{API}/events/{ok['event_id']}", json={"operator_id": "t", "lodging_share": 2}).status_code == 400
    default = client.get(f"{API}/events").json()["events"][0]
    assert default["lodging_share"] is None and default["lodging_share_source"] == "default"
    act = client.post(f"{API}/blueprints/{get_engine().world['blueprint_id']}/activate",
                      json={"event": {"lodging_share": 1.5}})
    assert act.status_code == 400


def test_restart_restores_hotel_capacity_metadata(client):
    from app.main import _restore_active_world
    from app.services.engine import Engine
    _activate(client, expected_attendance=30000, lodging_share=0.3)
    fresh = Engine()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_restore_active_world(fresh))
    finally:
        loop.close()
    live = {p["property_id"]: (p["rooms_total"], p["rooms_source"], p.get("bed_capacity")) for p in get_engine().store.properties}
    restored = {p["property_id"]: (p["rooms_total"], p["rooms_source"], p.get("bed_capacity")) for p in fresh.store.properties}
    assert restored == live
    assert fresh.events.events[fresh.events.primary_event_id]["lodging_share"] == 0.3
