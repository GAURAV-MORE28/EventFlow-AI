"""A generated blueprint becomes the world the EventFlow engine simulates.

Uses synthetic Overpass payloads (tests/geo_fixtures.py) through the real API,
engine, flow model and services — only the network is replaced.
"""
from __future__ import annotations

import asyncio
import re
import time

import pytest
from fastapi.testclient import TestClient

from app.geospatial.service import BlueprintService, set_blueprint_service
from app.main import app
from app.services.engine import get_engine
from tests.geo_fixtures import SnapshotLike, city

API = "/api/v1"
A = (23.0917717, 72.5973345)          # "Ahmedabad" grid city
B = (51.5559, -0.2796)                # "London" grid city, different spacing / ids
PAYLOAD = {A: city(*A), B: city(*B, spacing=330, half=9, extra_hotels=4, base_id=2_000_000)}
DEMO_IDS = {"stadium_main", "metro_b", "gate_3", "road_4", "emergency_north", "gate_5", "evt_demo",
            "zone_fanpark", "hotel_north_cluster"}


class _Router:
    """Serves the fixture city nearest to the requested footprint (like a real OSM service)."""
    name, live = "osm_snapshot", False

    def fetch(self, footprint, venue):
        key = min(PAYLOAD, key=lambda c: abs(c[0] - footprint.center_lat) + abs(c[1] - footprint.center_lon))
        return SnapshotLike(PAYLOAD[key]).fetch(footprint, venue)


def _run(engine, cycles):
    loop = asyncio.new_event_loop()
    try:
        for _ in range(cycles):
            loop.run_until_complete(engine.run_cycle())
    finally:
        loop.close()


@pytest.fixture(scope="module")
def client():
    set_blueprint_service(BlueprintService(provider=_Router()))
    with TestClient(app) as c:
        get_engine().paused = True
        yield c
        c.post(f"{API}/world/synthetic-demo")       # leave the legacy world active for other modules
    set_blueprint_service(None)


def build(c, at, radius=2000, **kw):
    r = c.post(f"{API}/blueprints", json={"venue": {"lat": at[0], "lon": at[1], "display_name": "Test Stadium"},
                                          "radius_m": radius, **kw})
    assert r.status_code == 202, r.json()
    bid = r.json()["build_id"]
    for _ in range(200):
        job = c.get(f"{API}/blueprints/builds/{bid}").json()
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["status"] == "complete", job
    assert [s["status"] for s in job["stages"]] == ["done"] * 5
    return job["blueprint_id"]


def activate(c, bid, **event):
    r = c.post(f"{API}/blueprints/{bid}/activate", json={"operator_id": "t", **({"event": event} if event else {})})
    assert r.status_code == 200, r.json()
    get_engine().paused = True
    return r.json()


# --- B/C via API: validation of the build request ----------------------------------------------
@pytest.mark.parametrize("radius", [0, -5, 1e9, 10])
def test_invalid_radius_is_rejected_synchronously(client, radius):
    r = client.post(f"{API}/blueprints", json={"venue": {"lat": A[0], "lon": A[1]}, "radius_m": radius})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_RADIUS"


def test_invalid_venue_and_missing_blueprint(client):
    assert client.post(f"{API}/blueprints", json={"venue": {}, "radius_m": 2000}).json()["error"]["code"] == "INVALID_VENUE"
    assert client.post(f"{API}/blueprints", json={"venue": {"lat": 95, "lon": 0}, "radius_m": 2000}).status_code == 400
    assert client.get(f"{API}/blueprints/bp_nope").json()["error"]["code"] == "BLUEPRINT_NOT_FOUND"
    assert client.get(f"{API}/blueprints/builds/nope").status_code == 404


def test_no_roads_is_a_structured_build_failure(client):
    r = client.post(f"{API}/blueprints", json={"venue": {"lat": -60.0, "lon": -140.0}, "radius_m": 1000})
    bid = r.json()["build_id"]
    for _ in range(200):
        job = client.get(f"{API}/blueprints/builds/{bid}").json()
        if job["status"] != "running":
            break
        time.sleep(0.02)
    # the nearest fixture city is thousands of km away: no roads in this footprint
    assert job["status"] == "failed" and job["error"]["code"] == "NO_ROAD_NETWORK"
    assert "within 1000 m" in job["error"]["message"]


# --- F/G. generated topology + runtime activation ---------------------------------------------------
def test_activation_replaces_the_world_and_resyncs_clients(client):
    before = client.get(f"{API}/graph").json()
    assert before["world"]["source"] == "synthetic_demo" and len(before["nodes"]) == 67
    bid = build(client, A)
    bp = client.get(f"{API}/blueprints/{bid}").json()
    assert bp["validation"]["valid"] and bp["metadata"]["data_source"] == "osm_snapshot"
    run_before = get_engine().run_id
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["event"] == "resync" and first["payload"]["world"]["world_id"] == "synthetic_demo"
        info = activate(client, bid)
        msg = ws.receive_json()
        while msg["event"] != "resync":
            msg = ws.receive_json()
    assert msg["payload"]["world"]["world_id"] == bid and msg["payload"]["world"]["run_id"] > run_before
    assert info["source"] == "generated_blueprint" and info["blueprint_id"] == bid
    graph = client.get(f"{API}/graph").json()
    ids = {n["entity_id"] for n in graph["nodes"]}
    assert ids == {n["entity_id"] for n in bp["nodes"]}                     # the blueprint IS the graph
    assert not ids & {n["entity_id"] for n in before["nodes"]}               # old graph gone
    assert not ids & DEMO_IDS
    state = client.get(f"{API}/state").json()
    assert {e["entity_id"] for e in state["entities"]} <= ids and state["cycle_number"] == 0
    assert msg["payload"]["interventions"] == [] and msg["payload"]["cascades"] == []
    assert client.get(f"{API}/event").json()["venue_entity_id"] == bp["venue_entity_id"]
    assert graph["world"]["footprint"]["radius_m"] == 2000


def test_changing_venue_and_radius_changes_the_active_graph(client):
    a2 = build(client, A, 2000)
    a1 = build(client, A, 1000)
    b2 = build(client, B, 2000)
    assert a2 == build(client, A, 2000)                                      # deterministic id
    graphs = {}
    for bid in (a1, a2, b2):
        activate(client, bid)
        g = client.get(f"{API}/graph").json()
        graphs[bid] = g
        _run(get_engine(), 3)
        assert {e["entity_id"] for e in client.get(f"{API}/state").json()["entities"]} <= {n["entity_id"] for n in g["nodes"]}
    ids = {k: {n["entity_id"] for n in g["nodes"]} for k, g in graphs.items()}
    assert len(ids[a2]) > len(ids[a1]) and ids[a1] != ids[a2] and not ids[a2] & ids[b2]
    va = next(n for n in graphs[a2]["nodes"] if n["entity_type"] == "venue")
    vb = next(n for n in graphs[b2]["nodes"] if n["entity_type"] == "venue")
    assert abs(va["lat"] - vb["lat"]) > 20
    listed = {b["blueprint_id"] for b in client.get(f"{API}/blueprints").json()["blueprints"]}
    assert {a1, a2, b2} <= listed


def test_old_world_state_does_not_leak(client):
    a = build(client, A)
    activate(client, a)
    engine = get_engine()
    _run(engine, 170)
    assert engine.store.interventions and engine.store.cascades
    old_ids = set(engine.store.nodes)
    b = build(client, B)
    activate(client, b)
    s = engine.store
    assert not s.interventions and not s.cascades and not s.nudges and not s.disruptions
    assert s.cycle_number == 0 and not engine.counterfactuals
    assert not set(s.entity_states) & old_ids and not set(engine.generator.nodes) & old_ids
    assert client.get(f"{API}/interventions").json()["interventions"] == []


# --- H/I. the flow model runs on the generated graph ----------------------------------------------
def test_flow_model_runs_on_generated_topology_causally(client):
    bid = build(client, A, venue_capacity=40000)
    activate(client, bid)
    engine = get_engine()
    g = engine.generator
    assert set(g.nodes) == set(engine.store.nodes)                          # not the demo graph underneath
    assert g.route_paths and not (set(g.nodes) & DEMO_IDS)
    venue = engine.world["venue_entity_id"]
    by = engine.store.nodes
    gates = [e for e, n in by.items() if n["entity_type"] == "gate"]
    path_roads = {r for path in g.route_paths.values() for r, _ in path}
    other_roads = [e for e, n in by.items() if n["entity_type"] == "road" and e not in path_roads]
    _run(engine, 120)                                          # 15:00 — arrival wave
    st = engine.store.entity_states
    assert st[venue]["current_count"] > 1000
    assert all((g.flow_state()[x]["inflow_per_min"] or 0) > 0 for x in gates)
    on = sum(st[r]["utilisation"] for r in path_roads) / len(path_roads)
    off = sum(st[r]["utilisation"] for r in other_roads) / len(other_roads)
    assert on > off + 0.05                                     # congestion follows the generated paths
    _run(engine, 400)                                          # 18:20 is before the end; go past egress
    _run(engine, 120)
    led = g.event_ledger()[engine.events.primary_event_id]
    assert led["egressed"] > 0.5 * led["arrived"]              # egress works
    assert abs(led["arrived"] - led["inside"] - led["egressed"] - led["pending"]) < 1e-6


def test_event_attendance_changes_the_generated_world(client):
    bid = build(client, A, venue_capacity=60000)
    out = {}
    for att in (6000, 50000):
        activate(client, bid, expected_attendance=att)
        engine = get_engine()
        _run(engine, 110)
        st = engine.store.entity_states
        by = engine.store.nodes
        out[att] = {t: sum(st[e]["current_count"] for e, n in by.items() if n["entity_type"] == t)
                    for t in ("venue", "gate", "road", "transport_node")}
    for t in ("venue", "gate", "road", "transport_node"):
        assert out[50000][t] > out[6000][t], (t, out)


def test_organizer_can_create_an_event_at_the_generated_venue(client):
    bid = build(client, A)
    activate(client, bid)
    venues = client.get(f"{API}/events/venues").json()["venues"]
    vid = get_engine().world["venue_entity_id"]
    assert vid in {v["entity_id"] for v in venues}
    r = client.post(f"{API}/events", json={"operator_id": "t", "name": "Night match", "venue_entity_id": vid,
                                           "start_time": "2026-09-04T19:00:00Z", "end_time": "2026-09-04T21:00:00Z",
                                           "expected_attendance": 20000})
    assert r.status_code == 201 and r.json()["venue_entity_id"] == vid


# --- K. What-If ----------------------------------------------------------------------------------------
def test_whatif_on_generated_world_is_isolated_and_meaningful(client):
    from app.services.simulation import SIMULATIONS
    bid = build(client, A, venue_capacity=40000)
    activate(client, bid)
    engine = get_engine()
    _run(engine, 100)
    by = engine.store.nodes
    gate = sorted(e for e, n in by.items() if n["entity_type"] == "gate")[0]
    before = {e: v["utilisation"] for e, v in engine.store.entity_states.items()}
    twin_sum = float(engine.registry.twin._X.sum())
    r = SIMULATIONS._execute(engine, [{"scenario_type": "gate_closure", "params": {"entity_id": gate}}], 1800,
                             with_candidates=False)
    assert {e: v["utilisation"] for e, v in engine.store.entity_states.items()} == before
    assert float(engine.registry.twin._X.sum()) == twin_sum and not engine.generator.is_closed(gate)
    changes = {c["entity_id"]: c for c in r["top_changes"]}
    assert gate in changes and changes[gate]["scenario_peak"] < changes[gate]["baseline_peak"]
    others = [c for e, c in changes.items() if by[e]["entity_type"] == "gate" and e != gate]
    assert others and all(c["scenario_peak"] > c["baseline_peak"] for c in others)   # its people go elsewhere
    bad = client.post(f"{API}/simulate", json={"scenarios": [{"scenario_type": "gate_closure",
                                                              "params": {"entity_id": "gate_3"}}], "horizon_sec": 1800})
    assert bad.status_code == 400                                         # demo ids do not exist here


# --- L. interventions ------------------------------------------------------------------------------------
def test_generated_source_destination_interventions(client):
    bid = build(client, A, venue_capacity=40000)
    activate(client, bid)
    engine = get_engine()
    _run(engine, 100)
    by = engine.store.nodes
    gates = sorted(e for e, n in by.items() if n["entity_type"] == "gate")
    stations = sorted(e for e, n in by.items() if n["entity_type"] == "transport_node")
    lots = sorted(e for e, n in by.items() if n["entity_type"] == "parking")
    busiest = max(stations, key=lambda s: engine.store.entity_states[s]["utilisation"])
    other = next(s for s in stations if s != busiest)
    cases = [
        {"intervention_type": "gate_redistribution", "target_entity_ids": [gates[0], gates[1]],
         "_action": {"sources": [gates[0]], "destination": gates[1], "fraction": 0.4}},
        {"intervention_type": "reroute_transport", "target_entity_ids": [busiest, other],
         "_action": {"source": busiest, "destination": other, "fraction": 0.4}},
        {"intervention_type": "parking_redistribution", "target_entity_ids": lots[:2],
         "_action": {"source": lots[0], "destination": lots[1], "fraction": 0.5}},
    ]
    for item in cases:
        with engine.world_lock:
            act, noop = engine.generator.clone(), engine.generator.clone()
        act.apply_intervention(dict(item, intervention_id="int_t"), 1.0, 2700)
        act.run(900, 30)
        noop.run(900, 30)
        src, dst = item["target_entity_ids"][0], item["target_entity_ids"][1]
        ua, un = act.utilisation(), noop.utilisation()
        fa, fn = act.flow_state(), noop.flow_state()
        moved_src = (fa[src]["inflow_per_min"] or 0) < (fn[src]["inflow_per_min"] or 0) or ua[src] < un[src]
        moved_dst = (fa[dst]["inflow_per_min"] or 0) > (fn[dst]["inflow_per_min"] or 0) or ua[dst] > un[dst]
        assert moved_src and moved_dst, item["intervention_type"]
        la, ln = act.event_ledger(), noop.event_ledger()
        assert all(abs(la[k]["arrived"] - ln[k]["arrived"]) < 1e-6 for k in la)        # conservation
    # proposals generated in this world reference only generated ids
    _run(engine, 80)
    for i in engine.store.interventions.values():
        assert set(i["target_entity_ids"]) <= set(by), i["target_entity_ids"]


def test_approval_changes_the_generated_world_against_a_matched_counterfactual(client):
    bid = build(client, A, venue_capacity=40000)
    activate(client, bid)
    engine = get_engine()
    _run(engine, 110)
    item = next((i for i in engine.store.interventions.values()
                 if i["status"] == "proposed" and i["intervention_type"] not in ("notify_only", "emergency_corridor")), None)
    assert item is not None
    r = client.post(f"{API}/interventions/{item['intervention_id']}/approve", json={"operator_id": "t"})
    assert r.status_code == 200 and r.json()["status"] == "executing"
    cf = engine.counterfactuals[item["intervention_id"]]
    assert set(cf.nodes) == set(engine.store.nodes)                            # same generated world
    _run(engine, 5)
    diff = max(abs(engine.generator.utilisation()[e] - cf.utilisation()[e]) for e in engine.store.nodes)
    assert diff > 0


# --- M. attendee journeys --------------------------------------------------------------------------------
def test_attendee_routes_through_the_generated_graph(client):
    bid = build(client, A)
    activate(client, bid)
    engine = get_engine()
    _run(engine, 30)
    prop = engine.store.properties[0]
    venue = engine.world["venue_entity_id"]
    r = client.post(f"{API}/attendee/journey", json={"attendee_id": "att_x", "segment_id": "time_sensitive",
                                                    "origin_entity_id": prop["property_id"],
                                                    "destination_entity_id": venue})
    assert r.status_code == 200, r.json()
    j = r.json()
    legs = j["recommended_route"]["legs"]
    types = [engine.store.nodes[l["to_entity_id"]]["entity_type"] for l in legs]
    assert types[-1] == "venue" and "gate" in types
    assert legs[0]["from_entity_id"] == prop["cluster_entity_id"]
    assert j.get("return_route") and j["return_route"]["legs"]
    assert not {l["to_entity_id"] for l in legs} & DEMO_IDS


# --- N. GNN honesty --------------------------------------------------------------------------------------
def test_generated_world_does_not_claim_gnn(client):
    bid = build(client, A, venue_capacity=40000)
    activate(client, bid)
    engine = get_engine()
    _run(engine, 120)
    h = client.get(f"{API}/health").json()["modules"]["cascade"]
    assert h["active_source"] == "deterministic" and "synthetic demo topology only" in h["detail"]
    assert client.get(f"{API}/world").json()["cascade_source"] == "deterministic"
    cascades = client.get(f"{API}/cascade/active").json()["cascades"]
    assert cascades and all(c["ml_enhanced"] is False for c in cascades)
    assert all(s.get("confidence") is None for c in cascades for s in c["steps"])


def test_generated_world_is_deterministic(client):
    bid = build(client, A, venue_capacity=40000)
    snaps = []
    for _ in range(2):
        activate(client, bid)
        engine = get_engine()
        _run(engine, 60)
        snaps.append(({e: round(v["utilisation"], 9) for e, v in engine.store.entity_states.items()},
                      sorted(engine.store.interventions)))
    assert snaps[0] == snaps[1]


def test_legacy_synthetic_world_still_works(client):
    info = client.post(f"{API}/world/synthetic-demo").json()
    get_engine().paused = True
    assert info["source"] == "synthetic_demo" and info["node_count"] == 67
    # The published cascade's structure is always the deterministic flow cascade
    # (00 §2.5); the loaded GNN's involvement is `gnn_mode` / `model_version`.
    cascade = client.get(f"{API}/health").json()["modules"]["cascade"]
    assert cascade["active_source"] == "deterministic"
    assert cascade["gnn_mode"] in ("shadow", "annotate")
    assert "stadium_main" in {n["entity_id"] for n in client.get(f"{API}/graph").json()["nodes"]}


def test_generated_ids_do_not_encode_behaviour():
    import pathlib
    src = "\n".join(p.read_text() for p in pathlib.Path("app").rglob("*.py") if "__pycache__" not in str(p))
    # No behaviour is derived from an id prefix. (providers/data.py only *formats* file-provided
    # hotel ids as htl_<slug>; it never classifies by them.)
    assert not re.search(r"startswith\(\s*\(?\s*[\"'](bus_|shuttle_hub|metro_|line_|hotel_)", src)
    assert "startswith(\"htl_\")" not in (pathlib.Path("app/services/attendee.py").read_text())
    for bad in ('"stadium_main"', '"zone_fanpark"', '"evt_demo"'):
        offenders = [str(p) for p in pathlib.Path("app").rglob("*.py")
                     if bad in p.read_text() and p.name not in ("topology.py", "catalog.py")]
        assert not offenders, (bad, offenders)


# --- provider failure / fallback honesty / restart restore ------------------------------------------
class _Down:
    name, live = "osm_overpass", True

    def fetch(self, footprint, venue):
        from app.geospatial.http import ProviderError
        raise ProviderError("overpass", "all Overpass endpoints failed: timed out")


def test_live_provider_failure_is_explicit_and_keeps_the_current_world(client):
    from app.geospatial.service import get_blueprint_service
    bid = build(client, A)
    activate(client, bid)
    svc = get_blueprint_service()
    real, svc.provider = svc.provider, _Down()
    try:
        r = client.post(f"{API}/blueprints", json={"venue": {"lat": B[0], "lon": B[1]}, "radius_m": 2000})
        build_id = r.json()["build_id"]
        for _ in range(200):
            job = client.get(f"{API}/blueprints/builds/{build_id}").json()
            if job["status"] != "running":
                break
            time.sleep(0.02)
    finally:
        svc.provider = real
    assert job["status"] == "failed" and job["error"]["code"] == "GEO_PROVIDER_UNAVAILABLE"
    assert "current world was not changed" in job["error"]["message"]
    failed_stage = [s for s in job["stages"] if s["status"] == "failed"]
    assert [s["stage"] for s in failed_stage] == ["acquiring_osm"]
    w = client.get(f"{API}/world").json()
    assert w["world_id"] == bid and w["data_source"] == "osm_snapshot"      # never relabelled, never swapped


def test_restart_restores_the_active_blueprint(client):
    from app.main import _restore_active_world
    from app.services.engine import Engine
    bid = build(client, B)
    activate(client, bid)
    fresh = Engine()                                     # what a restarted process starts with
    assert fresh.world["source"] == "synthetic_demo"
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_restore_active_world(fresh))
    finally:
        loop.close()
    assert fresh.world["world_id"] == bid and set(fresh.store.nodes) == set(get_engine().store.nodes)
