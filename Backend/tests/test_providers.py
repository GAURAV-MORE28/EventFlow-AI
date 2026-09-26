"""Geo and data providers: synthetic defaults, external fallbacks, normalisation."""
from __future__ import annotations

import json

import pytest

from app.providers import geo as G
from app.providers.data import DataError, FileData, normalise_event, normalise_hotel, slug, iso_utc, fraction
from app.topology import build_topology


def test_synthetic_travel_is_distance_based_and_mode_aware():
    geo = G.SyntheticGeo()
    near = geo.travel((19.07, 72.87), (19.075, 72.87), "walk")
    far = geo.travel((19.07, 72.87), (19.10, 72.87), "walk")
    drive = geo.travel((19.07, 72.87), (19.10, 72.87), "drive")
    assert 0 < near["duration_sec"] < far["duration_sec"]
    assert drive["duration_sec"] < far["duration_sec"]
    assert far["source"] == "synthetic"


def test_remote_provider_falls_back_retries_and_caches(monkeypatch):
    calls = {"n": 0}
    osrm = G.OSRMGeo("http://127.0.0.1:9", timeout_sec=0.2, retries=1)
    monkeypatch.setattr(G.time, "sleep", lambda s: None)

    def boom(url):
        calls["n"] += 1
        raise OSError("down")

    monkeypatch.setattr(osrm, "_get_json", boom)
    r = osrm.travel((19.07, 72.87), (19.08, 72.88), "walk")
    assert r["source"] == "synthetic_fallback:osrm" and r["duration_sec"] > 0
    assert calls["n"] == 2  # one retry

    monkeypatch.setattr(osrm, "_get_json", lambda url: {"code": "Ok", "routes": [{"duration": 321.4, "distance": 900}]})
    a = osrm.travel((19.07, 72.87), (19.08, 72.88), "walk")
    monkeypatch.setattr(osrm, "_get_json", boom)
    b = osrm.travel((19.07, 72.87), (19.08, 72.88), "walk")
    assert a == b == {"duration_sec": 321, "distance_m": 900, "source": "osrm"}
    assert osrm.stats["cache_hits"] == 1


def test_provider_selection_needs_its_secret_from_the_environment():
    assert G.build_geo_provider({"provider": "google"}, env={}).name == "synthetic"
    assert G.build_geo_provider({}, env={"EVENTFLOW_GEO_PROVIDER": "google", "GOOGLE_MAPS_API_KEY": "k"}).name == "google"
    assert G.build_geo_provider({}, env={"EVENTFLOW_GEO_PROVIDER": "osrm", "OSRM_URL": "http://x"}).name == "osrm"


def test_normalisers_enforce_wire_conventions():
    assert slug("Metro B") == "metro_b"
    assert iso_utc("2026-09-04T21:30:00+05:30") == "2026-09-04T16:00:00Z"
    assert fraction(35) == 0.35
    nodes = {n["entity_id"]: n for n in build_topology()["nodes"]}
    ev = normalise_event({"name": "Night Concert", "venue": "Stadium Main", "start_time": "2026-09-04T20:00:00",
                          "end_time": "2026-09-04T22:00:00", "expected_attendance": 30000, "out_of_town_share": 20},
                         set(nodes))
    assert ev["event_id"] == "night_concert" and ev["venue_entity_id"] == "stadium_main" and ev["out_of_town_share"] == 0.2
    h = normalise_hotel({"name": "Sea View Inn", "cluster_entity_id": "hotel_north_cluster", "lat": 19.09, "lon": 72.88,
                         "rooms_total": 80, "price_per_night_inr": 4500, "tier": "Budget"}, nodes)
    assert h["property_id"] == "htl_sea_view_inn" and h["price_per_night_paise"] == 450000 and h["tier"] == "budget"
    with pytest.raises(DataError):
        normalise_event({"name": "x", "venue": "atlantis", "start_time": "2026-09-04T20:00:00Z",
                         "end_time": "2026-09-04T22:00:00Z", "expected_attendance": 1}, set(nodes))


def test_file_provider_reads_partial_real_data(tmp_path):
    (tmp_path / "events.json").write_text(json.dumps([{
        "event_id": "evt_real", "name": "Real Match", "venue_entity_id": "stadium_main",
        "start_time": "2026-09-04T16:00:00Z", "end_time": "2026-09-04T18:00:00Z", "expected_attendance": 50000}]))
    fd = FileData(tmp_path)
    assert [e["event_id"] for e in fd.events([])] == ["evt_real"]
    topo = fd.topology()  # no topology.json: synthetic
    assert len(topo["nodes"]) == len(build_topology()["nodes"])
    assert len(fd.properties(topo["nodes"], topo["edges"])) > 0
