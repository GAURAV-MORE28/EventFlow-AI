"""Venue resolution, radius validation and the monitoring footprint (no network)."""
from __future__ import annotations

import json
import math

import pytest

from app.geospatial.footprint import Footprint, RadiusError, haversine_m, make_footprint, validate_radius
from app.geospatial.http import HttpClient, ProviderError
from app.geospatial.venues import (
    CoordinateVenueProvider, GooglePlacesVenueProvider, NominatimVenueProvider, VenueError, VenueResolver,
    build_resolver,
)

NMS = {  # shape of a real Nominatim jsonv2 result
    "place_id": 248309842, "osm_type": "way", "osm_id": 776718456, "lat": "23.0917717", "lon": "72.5973345",
    "category": "leisure", "type": "stadium", "importance": 0.425, "name": "Narendra Modi Stadium",
    "display_name": "Narendra Modi Stadium, Motera Stadium Entry Road, Ahmedabad, Gujarat, India",
    "boundingbox": ["23.0904963", "23.0930908", "72.5958115", "72.5988545"], "extratags": {},
}
WEMBLEY = {**NMS, "osm_id": 4244999, "osm_type": "way", "lat": "51.5559", "lon": "-0.2796",
           "name": "Wembley Stadium", "display_name": "Wembley Stadium, London", "extratags": {"capacity": "90000"}}


def fake_transport(routes):
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers, body))
        for key, resp in routes.items():
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                status, payload = resp
                return status, json.dumps(payload).encode()
        return 404, b"{}"
    transport.calls = calls
    return transport


def client(routes, **kw):
    return HttpClient("EventFlow-AI/test", transport=fake_transport(routes), **kw)


# --- A. venue resolver -------------------------------------------------------------------------
def test_nominatim_search_normalises_deterministically():
    http = client({"/search": (200, [NMS, WEMBLEY])})
    r = VenueResolver([NominatimVenueProvider(http, "https://nominatim.example")])
    a = r.search("Narendra Modi Stadium")
    b = r.search("narendra   modi stadium ")
    assert a["results"][0]["display_name"] == "Narendra Modi Stadium"
    assert a["results"][0]["lat"] == 23.0917717 and a["results"][0]["lon"] == 72.5973345
    assert a["results"][0]["source_id"] == "way/776718456" and a["results"][0]["confidence"] == "high"
    assert len(a["results"]) == 2 and a["results"][1]["osm_capacity"] == 90000
    assert a["results"][0]["venue_id"] == b["results"][0]["venue_id"]          # deterministic identity
    assert http.transport.calls[0][2]["User-Agent"] == "EventFlow-AI/test"     # policy: identify ourselves


def test_search_with_no_result_and_invalid_query():
    r = VenueResolver([NominatimVenueProvider(client({"/search": (200, [])}), "https://n.example")])
    assert r.search("nowhere venue xyz")["results"] == []
    for bad in ("", "ab", "x" * 201, None):
        with pytest.raises(VenueError):
            r.search(bad)


def test_provider_timeout_and_rate_limit_fall_back_to_the_next_provider():
    http = client({"/search": TimeoutError("timed out")}, retries=0)
    r = VenueResolver([NominatimVenueProvider(http, "https://n.example"), CoordinateVenueProvider()])
    res = r.search("Narendra Modi Stadium")
    assert res["results"] == [] and res["attempts"][0]["status"] == "failed"
    assert "network error" in res["attempts"][0]["reason"]
    limited = VenueResolver([NominatimVenueProvider(client({"/search": (429, {})}, retries=0), "https://n")])
    out = limited.search("Wembley Stadium")
    assert out["attempts"][0]["reason"] == "rate limited by the provider" and out["all_failed"]


def test_malformed_provider_results_are_skipped():
    bad = [{"osm_type": "way", "osm_id": 1, "lat": "nan", "lon": "0"}, {"lat": "1", "lon": "2"}, "junk", NMS]
    r = VenueResolver([NominatimVenueProvider(client({"/search": (200, bad)}), "https://n.example")])
    assert [v["source_id"] for v in r.search("stadium")["results"]] == ["way/776718456"]


def test_coordinates_are_a_first_class_offline_venue():
    r = VenueResolver([CoordinateVenueProvider(), NominatimVenueProvider(client({}), "https://n")])
    res = r.search("51.5559, -0.2796")
    assert res["results"][0]["source"] == "coordinates" and res["results"][0]["lat"] == 51.5559
    v = r.resolve({"lat": -33.8915, "lon": 151.2255, "display_name": "Sydney Cricket Ground"})
    assert v["display_name"] == "Sydney Cricket Ground" and v["confidence"] == "high"
    for lat, lon in ((91, 0), (0, 181), (float("nan"), 0), ("x", 1)):
        with pytest.raises(VenueError):
            r.resolve({"lat": lat, "lon": lon})


def test_resolve_uses_the_search_index_and_osm_lookup():
    http = client({"/search": (200, [NMS]), "/lookup": (200, [WEMBLEY])})
    nom = NominatimVenueProvider(http, "https://n.example")
    r = VenueResolver([nom], nom)
    vid = r.search("Narendra Modi Stadium")["results"][0]["venue_id"]
    assert r.resolve({"venue_id": vid})["osm_id"] == 776718456
    looked = r.resolve({"source": "nominatim", "osm_type": "way", "osm_id": 4244999})
    assert looked["display_name"] == "Wembley Stadium"
    with pytest.raises(VenueError):
        r.resolve({})


def test_google_is_optional_discovery_and_never_persisted_as_google_content():
    assert not GooglePlacesVenueProvider(client({}), None).available()
    places = {"places": [{"id": "ChIJabc", "displayName": {"text": "Narendra Modi Stadium"},
                          "formattedAddress": "Ahmedabad", "location": {"latitude": 23.0918, "longitude": 72.5974}}]}
    http = client({"places.googleapis.com": (200, places), "/search": (200, [NMS])})
    g = GooglePlacesVenueProvider(http, "KEY")
    nom = NominatimVenueProvider(http, "https://n.example")
    r = VenueResolver([g, nom], nom)
    res = r.search("Narendra Modi Stadium")
    assert res["results"][0]["source"] == "google_places" and res["results"][0]["persistable"] is False
    method, url, headers, body = http.transport.calls[0]
    assert headers["X-Goog-FieldMask"] == GooglePlacesVenueProvider.FIELD_MASK and json.loads(body)["textQuery"]
    final = r.resolve({"venue_id": res["results"][0]["venue_id"]})
    # Re-resolved to OSM: persistable identity, only the place_id kept from Google.
    assert final["source"] == "nominatim" and final["google_place_id"] == "ChIJabc" and final["persistable"]


def test_build_resolver_skips_google_without_a_key():
    r = build_resolver({"venue_providers": ["coordinates", "google_places", "nominatim"]}, client({}), env={})
    status = {s["provider"]: s["available"] for s in r.status()}
    assert status == {"coordinates": True, "google_places": False, "nominatim": True}


# --- B. radius ----------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [0, -1, -500, float("nan"), float("inf"), float("-inf"), "abc", None, True, 100, 60000])
def test_invalid_radius_is_rejected(bad):
    with pytest.raises(RadiusError):
        validate_radius(bad, 250, 5000)


@pytest.mark.parametrize("ok", [250, 1000, 2000.5, "3000", 5000])
def test_valid_radius_is_accepted_in_metres(ok):
    assert validate_radius(ok, 250, 5000) == round(float(ok), 1)


# --- C. footprint -------------------------------------------------------------------------------
def test_footprint_geometry_is_correct_and_deterministic():
    fp = make_footprint(23.0917717, 72.5973345, 2000, 250, 5000)
    d = fp.to_dict()
    assert (d["center_lat"], d["center_lon"], d["radius_m"]) == (23.0917717, 72.5973345, 2000.0)
    ring = d["ring"]
    assert ring[0] == ring[-1] and len(ring) == 73
    for lon, lat in ring[:-1]:
        assert abs(haversine_m(23.0917717, 72.5973345, lat, lon) - 2000.0) < 0.5
    b = d["bounds"]
    assert abs(haversine_m(b["min_lat"], 72.5973345, b["max_lat"], 72.5973345) - 4000) < 1.0
    assert d == make_footprint(23.0917717, 72.5973345, 2000, 250, 5000).to_dict()
    assert abs(d["area_km2"] - math.pi * 4) < 1e-3


def test_larger_radius_expands_the_footprint():
    small, large = Footprint(51.5559, -0.2796, 1000), Footprint(51.5559, -0.2796, 3000)
    sb, lb = small.bounds, large.bounds
    assert lb["min_lat"] < sb["min_lat"] and lb["max_lat"] > sb["max_lat"]
    assert lb["min_lon"] < sb["min_lon"] and lb["max_lon"] > sb["max_lon"]
    p = (51.5559 + 0.015, -0.2796)  # ~1.67 km north
    assert not small.contains(*p) and large.contains(*p)


def test_provider_error_is_structured():
    with pytest.raises(ProviderError) as exc:
        client({"x": (500, {})}, retries=0).request_json("overpass", "GET", "https://h/x")
    assert exc.value.status == 500 and exc.value.provider == "overpass"
