# EventFlow AI — Venue → Radius → Footprint → Blueprint → Event Graph
## Validation Report

| | |
|---|---|
| Date | 2026-09-27 |
| Branch / base | `Paresh` @ `71f5290` + this work (uncommitted) |
| Scope | Organiser venue + radius → live OpenStreetMap acquisition → normalised blueprint → runtime event graph → the existing Paresh flow model and every service on top of it |
| Isolation | Every test and experiment used a scratch SQLite DB (`DATABASE_URL=sqlite:////private/tmp/…`). `Backend/eventflow.db` was not touched (mtime 2026-09-27 01:34:08, before this work). Every process started for validation was stopped by exact PID. |
| Status | **COMPLETE — with the limitations listed in §24.** Every acceptance-gate item was executed; results are below. |

---

## 1. Implementation summary

An organiser can open **Venue & Network** (`/venue`), search a real venue (or type coordinates), pick a monitoring radius, and build a blueprint. EventFlow then queries OpenStreetMap inside that footprint and normalises the result into an operational graph with provenance on every entity and edge. The graph contains:

- road junctions and segments along their OSM geometry,
- rail, metro and bus access nodes,
- parking, hotels and emergency facilities,
- access points taken from mapped entrances or real road approaches,
- walking routes from each access node to the access points, over the road graph.

On **Activate network**, the engine replaces the world it simulates without a restart. That means a new state store, schedule, flow-model worlds, twin, ML registry and run. It then broadcasts a full resync, and every page (Command Centre map on an OSM basemap, Events, Hotels, Transport, Crowd, Interventions, What-If, Attendee, Commander, Metrics) runs on the generated graph.

The legacy synthetic demo world remains the default and the offline fallback.

## 2. Architecture

```
ORGANISER ─ /venue (VenueSetup.jsx) ─┐
                                     ▼
GET /venues/search ── VenueResolver (venues.py)
                        coordinates  → always, offline
                        google_places→ only with GOOGLE_PLACES_API_KEY, discovery only (place_id kept)
                        nominatim    → explicit search, 1 req/s, cached, UA, OSM attribution
POST /blueprints {venue, radius_m} ── BlueprintService job (service.py), stages:
   resolving_venue → footprint (footprint.py: validate radius, circle, bounds)
   → acquiring_osm (overpass.py: ONE focused query, `around:<r>`, timeout+maxsize, endpoint failover)
   → building_blueprint (blueprint.py: BlueprintBuilder)
   → validating (validation.py: generic invariants)
   → Blueprint (normalised; persisted to `blueprint` table unless Google-sourced)
POST /blueprints/{id}/activate ── world_from_blueprint → Engine.activate_world (atomic, cycle guard)
   → StateStore(topology) → EventSchedule → MLRegistry(nodes) → SyntheticGenerator ×(live, nominal)
   → twin → prime_state → caches/what-if/Commander cleared → new run_id → WS resync {…, world}
FRONTEND: store.replaceAll clears the old graph on a new world_id → App refetches /graph, /event, /events
          → MapCanvas (OSM basemap, road geometry, footprint, venue outline) + every page
```

## 3. Files changed

**New (backend)**
- `app/geospatial/`: `__init__.py`, `http.py`, `venues.py`, `footprint.py`, `overpass.py`, `blueprint.py`, `validation.py`, `service.py`
- `app/api/geo_routes.py`
- Tests: `tests/geo_fixtures.py`, `tests/test_geo_venue_footprint.py` (27 tests), `tests/test_geo_blueprint.py` (17), `tests/test_geo_world.py` (22)

**New (frontend)**
- `src/routes/VenueSetup.jsx`, `src/lib/basemap.js`
- Tests: `scripts/test-ws-reconnect.mjs`, `scripts/test-store-world.mjs`

**New (contracts):** 7 schemas: `venue`, `venue_search_response`, `blueprint`, `blueprint_job`, `blueprint_list_response`, `world_info`, `geospatial_status`.

**Modified (backend)**
- `engine.py`: world lifecycle, GNN gating, `run_id`.
- `state_store.py`: topology parameter, generic validation.
- `generator.py`: subtype semantics, unlined rail, mode redistribution, road-path loading, id-free defaults, unknown prices.
- `twin.py`: canonical RNG order.
- `attendee.py`: subtype modes, property lookup, `connects_to`.
- `accommodation.py`: unknown prices.
- `topology.py`: subtypes, defaults, generic `verify` plus `verify_demo`.
- `catalog.py`: no `KeyError` on other topologies.
- `ml_registry.py`: active-world nodes.
- `schemas.py`: additive fields and models.
- `routes.py`: `/graph` world, honest `/health`.
- `ws_routes.py`: `world` in resync.
- `main.py`: geo router, restore on startup.
- `errors.py`: new codes.
- `db/models.py`: nullable price/tier, `blueprint`, `active_world`.
- `db/base.py`: `hotel_property` rebuild.
- `db/seed.py`: column-safe seeding.
- `simulation.py`: `clear()`.
- `config.yaml`: `geospatial:` block.
- `scripts/export_schemas.py`: new exports.

**Modified (frontend)**
- `App.jsx`: world-aware topology reload, `/venue` route.
- `MapCanvas.jsx`: basemap, road geometry, footprint, routes along paths.
- `NavBar.jsx`, `TopBar.jsx`: source chip.
- `api.js`: geo endpoints; mock mode is honest.
- `ws.js`: per-connection sequence.
- `useStore.js`: world, graph clearing.
- `Attendee.jsx`, `Accommodation.jsx`, `WhatIf.jsx`: no demo-id defaults.
- `package.json`: `npm test` runs the two new scripts.

**Modified (contracts):** `entity`, `graph_edge`, `graph_response`, `health_response`, `hotel_list_response`, `saturation_response`, `stay_recommendation_response` (additive only). The 23 mocks still validate and were not regenerated.

**Docs:** `CLAUDE.md`, `RUNNING.md`, `README.md`, `implementationstate.md` §6.

## 4. New APIs

All are under `/api/v1`, with Pydantic request and response models and error envelopes.

| Route | Request → Response | Errors |
|---|---|---|
| `GET /venues/search?q=` | → `VenueSearchResponse` (results, provider attempts, attribution) | 400 `INVALID_VENUE` (<3 or >200 chars) |
| `POST /venues/resolve` | `VenueSelection` → `Venue` | 400 `INVALID_VENUE`, 503 `GEO_PROVIDER_UNAVAILABLE` |
| `GET /geospatial/status` | → providers, radius limits, active world | — |
| `POST /blueprints` | `BlueprintBuildRequest{venue, radius_m, venue_capacity?, max_gates?}` → 202 `BlueprintJob` | 400 `INVALID_RADIUS` / `INVALID_VENUE` (synchronous) |
| `GET /blueprints/builds/{id}` | → `BlueprintJob` (5 stages with ms; `error{code,message}` on failure) | 404 `BUILD_NOT_FOUND`; job errors `GEO_PROVIDER_UNAVAILABLE`, `NO_ROAD_NETWORK`, `NO_ACCESS`, `NO_ARRIVAL_ACCESS`, `NETWORK_TOO_LARGE` |
| `GET /blueprints` | → headers (DB and memory) | — |
| `GET /blueprints/{id}` | → full `Blueprint` (nodes, edges, properties, provenance, validation) | 404 `BLUEPRINT_NOT_FOUND` |
| `POST /blueprints/{id}/validate` | → `ValidationReport` | 404 |
| `POST /blueprints/{id}/activate` | `{operator_id, event?}` → `WorldInfo` | 422 `BLUEPRINT_INVALID`, 400 `INVALID_SCHEDULE`, 500 `ACTIVATION_FAILED` (previous world kept) |
| `GET /world` | → `WorldInfo` | — |
| `POST /world/synthetic-demo` | → `WorldInfo` | — |

**Additive changes to existing routes**
- `GET /graph` gains a `world` field.
- `Entity` gains `subtype`, `capacity_source`, `capacity_confidence`, `provenance`.
- `GraphEdge` gains `distance_m`, `capacity_per_min`, `directionality`, `travel_time_source`, `geometry`, `via_entity_ids`, `provenance`.
- Edge type `connects_to` is new.
- `HotelProperty.price_per_night_paise` and `tier` are nullable (null = unknown); `rooms_source`, `rooms_confidence`, `price_source` are new.
- `ModuleHealth.detail` is new.
- WS `resync` gains `world`.

## 5. Data providers

| Provider | Use | Live? | Evidence |
|---|---|---|---|
| Coordinates | organiser point; offline | n/a | `test_coordinates_are_a_first_class_offline_venue`; live determinism runs used it |
| Nominatim (`nominatim.openstreetmap.org`) | venue search and lookup, server-side, on explicit Search only | **yes** | "Narendra Modi Stadium" → `way/776718456` (23.0917717, 72.5973345) and `relation/10756555`, 578 ms cold / 66 ms cached. "Wembley Stadium" → `relation/12942436` (51.5560695, −0.2796034), 1,028 ms. |
| Google Places (New) Text Search | optional discovery (`X-Goog-FieldMask: places.id,places.displayName,places.formattedAddress,places.location`) | not configured here (no key) | mocked tests: field mask sent; result re-resolved to OSM; only `place_id` kept; never persisted as Google content |
| Overpass | roads, transit, parking, hotels, emergency, venue outline, entrances | **yes** via `overpass.openstreetmap.fr` | `overpass-api.de` is unreachable from this network (TLS reset, also outside the sandbox), so failover to the second configured endpoint happened in every live build |
| Snapshot provider | replay of a recorded Overpass payload (tests, offline) | labelled `osm_snapshot`, never "live" | `test_live_provider_failure_is_explicit_and_keeps_the_current_world` |
| OSM raster tiles | basemap (visible tiles only, ≤48 per view, attribution on the map) | yes | 46–269 tile requests per E2E step; "© OpenStreetMap contributors" link rendered |

## 6. Dynamic behaviour (input → output, measured)

- **Venue** (live): Ahmedabad and London blueprints share 0 entity IDs. Venue coordinates are (23.0918, 72.5973) and (51.5561, −0.2796).
- **Radius** (live, Narendra Modi Stadium): 1 / 2 / 3 / 5 km → 41 / 109 / 254 / 734 entities and 82 / 218 / 445 / 1,383 edges, each with a different graph hash (see §12).
- **Attendance** (generated world, `test_event_attendance_changes_the_generated_world`): 6,000 vs 50,000 attendees → the venue, gate, road and transport totals are all higher at 50,000.
- **Organiser event creation** at the generated venue returns 201 and places demand (`test_organizer_can_create_an_event_at_the_generated_venue`).
- **Venue capacity hierarchy**: organiser 55,000 → `organizer/high`; no capacity → `derived_venue_area/low` (`test_venue_capacity_hierarchy`).

## 7. Hardcoded values removed

| Before | After |
|---|---|
| `generator.bus_hubs = startswith("bus_")` | `subtype ∈ {bus_hub, bus_station, bus_stop_cluster, bus_stop}` |
| `concurrent_event` main event `"evt_demo"`, venue `"zone_fanpark"` | world `defaults.primary_event_id` / largest event; `defaults.popup_venue` (the demo keeps `zone_fanpark` as data in `topology.py`) |
| `event_delay` / `event_cancellation` default `"evt_demo"` | primary event of the world |
| `_main_venue` fallback `"stadium_main"` | first venue entity |
| `attendee.mode_class` / `_mode` id prefixes (`metro_`, `line_`, `bus_hub`, `shuttle_hub`) | `entity_type` + `subtype` |
| `attendee._resolve_origin` `startswith("htl_")` | membership in the world's properties |
| `topology.verify()` requiring ≥60 nodes, `metro_b`, `gate_3`, `road_4`, `emergency_north`, `gate_5` | generic `validate_topology()` for every world; demo guarantees only in `verify_demo` for the demo world |
| `catalog.build_properties` `KeyError` for missing demo clusters | skips missing clusters; other hotels get a property with price and tier = null |
| `MLRegistry` critical lines from the provider's static topology | the active world's nodes |
| Frontend defaults `stadium_main`, `htl_central_budget`, `evt_demo` | active world's primary event, venue and first hotel |

`test_generated_ids_do_not_encode_behaviour` scans `app/` for id-prefix logic and for `"stadium_main"`, `"zone_fanpark"`, `"evt_demo"` outside `topology.py` / `catalog.py`. It passes.

**Still present:** `WhatIfPanel.jsx` contains demo presets, but it is not mounted anywhere (it was dead code before this work).

## 8. Remaining legitimate configuration

`config.yaml geospatial:` holds every documented estimate:
- road-class speed and lane tables;
- people storage per lane-metre (2.5);
- lane capacity (30 veh/min);
- venue people per m² (1.2);
- gate share of venue capacity (0.2);
- transit, parking, hotel and emergency default capacities;
- snap and merge distances;
- per-category caps;
- `max_nodes` 2,500;
- radius 250–5,000 m.

Each estimate is recorded on the entity as `capacity_source` / `travel_time_source` plus a confidence. The demo world's topology, catalogue and segments are unchanged, deliberate data.

## 9. Runtime graph activation

| Check | Result |
|---|---|
| Activation without a restart | `POST /blueprints/{id}/activate` → 200 in 45–154 ms (backend), 425–429 ms until the Command Centre renders all nodes (browser, 109 and 1,116 entities) |
| The engine uses the blueprint | `/graph` node set equals the blueprint node set; `set(engine.generator.nodes) == set(store.nodes)`; no demo IDs (`test_activation_replaces_the_world_and_resyncs_clients`, `test_flow_model_runs_on_generated_topology_causally`) |
| Old graph gone / no leaks | after activating B: 0 interventions, cascades, nudges, disruptions or counterfactuals; cycle 0; no old IDs in state or generator (`test_old_world_state_does_not_leak`) |
| Resync reaches clients | WS client receives `resync` with the new `world.world_id` and higher `run_id` (test); the browser store clears the old graph (`test-store-world.mjs`) |
| Atomic | `_install_world` restores the previous world on any exception; the route returns `ACTIVATION_FAILED` "the previous world is still running" |
| Restart | the last active blueprint is restored from the DB (`test_restart_restores_the_active_blueprint`). Live: backend killed under an open browser → world `bp_b1a73c0ed9d9` restored; UI cycle 1 → 13 → 23 against backend 2 → 13 → 24, no reload |
| Back to the demo | UI button → 67-node synthetic demo, chip "SYNTHETIC DEMO", `/health` cascade `gnn` |

## 10. Real-world venue test: Narendra Modi Stadium, Ahmedabad (live, final code)

| Radius | Entities / edges | Road junctions / segments | Transit (rail+metro) | Hotels | Parking | Emergency | Access points | Overpass | Build + validate |
|---|---|---|---|---|---|---|---|---|---|
| 1 km | 41 / 82 | 24 / 37 | 4 (1) | 1 | 0 | 8 | 2 | 3.4 s | 4.7 ms |
| 2 km | 109 / 218 | 80 / 127 | 8 (2) | 4 | 4 | 9 | 2 | 14.7 s | 11.8 ms |
| 3 km | 254 / 445 | 216 / 328 | 14 (2) | 7 | 4 | 9 | 2 | 13.0 s | 27.9 ms |
| 5 km | 734 / 1,383 | 664 / 1,160 | 26 (2) | 19 | 12 | 9 | 2 | 11.6 s | 73.7 ms |

- The venue polygon comes from OSM (`way/776718456`).
- Venue capacity 85,200 is `derived_venue_area` with low confidence. The real capacity is about 132,000; an organiser value overrides it.
- The two access points are OSM entrances: `entrance=yes` on the outline, and `entrance=main` 181 m outside it (the site gate). The only public road junction of the modelled classes within 350 m of the outline is 193 m to the NW, so no further access point is inferred.

**Browser flow (live):** Search → select → 2 km → Build → Activate → the Command Centre shows "GENERATED NETWORK · LIVE OSM · r = 2.0 km · 109 Nodes" (equal to `/graph`), and the TopBar chip reads LIVE OSM. The simulation ran on it with 0 demo IDs in state; at 60× the venue held 2,051 people by cycle 58.

## 11. Second-city test: Wembley Stadium, London (live)

| Radius | Entities / edges | Road junctions / segments | Transit (rail+metro) | Hotels | Parking | Emergency | Access points | Overpass |
|---|---|---|---|---|---|---|---|---|
| 2 km | 481 / 1,093 | 392 / 497 | 42 (2) | 14 | 18 | 7 | 6 (mapped `entrance=main`) | 19.4 s |
| 3.5 km | 1,116 / 1,942 | 1,004 / 1,275 | 42 (2) | 30 | 22 | 10 | 6 | 31.6 s |
| 5 km | 2,148 / 3,234 | 2,036 / 2,574 | 42 (2) | 30 | 22 | 10 | 6 | 79.6 s (slow public Overpass) |

- It shares 0 entity IDs with Ahmedabad.
- The generated world names real places: "Empire Way × Engineers Way", "Brentfield Road (+10 stops)", "St George's Hotel", "Novotel London Wembley".
- 4 capacities are `osm_attribute` (parking `capacity` tags).
- Browser: all 10 pages render on the London world with 0 JS errors and no demo names.

## 12. Radius A/B test (live, Narendra Modi Stadium, same code, coordinate venue)

| Transition | OSM road ways | Transit (by OSM members) | Hotels | Parking | Emergency | Road-junction entity IDs |
|---|---|---|---|---|---|---|
| 1 → 2 km | 19 → 68, **superset** | 7 → 17, **superset** | 1 → 4 ⊂ | 0 → 4 ⊂ | 8 → 9 ⊂ | 24 → 80; 10 not carried over |
| 2 → 3 km | 68 → 198, **superset** | 17 → 31, **superset** | 4 → 7 ⊂ | 4 → 4 = | 9 → 9 = | 80 → 216; 25 not carried over |

The acquired OSM data is a strict superset under the filtering policy: fixed road classes, and nearest-N caps per category, which are monotone in the radius.

Junction entities at the small circle's clip boundary are dead ends there. In the larger footprint they become interior points on a longer way and are simplified away, so derived junction IDs are not a superset; this is documented. Bus-stop clusters regroup as more stops enter, so the test compares their OSM members.

The fixture test `test_larger_radius_is_a_geographic_superset` enforces the same property offline.

## 13. Reproducibility

| Test | Result |
|---|---|
| Same venue + radius twice, two **separate processes** (`PYTHONHASHSEED` 0 vs 7), live OSM (same `osm_base` 2026-09-26T22:08:27Z) | identical graph hash (Ahmedabad `717016082ad7…`, London `996a712a28c6…`), identical node-ID order, edge-ID order, capacities and travel times |
| Fixture: same input twice; reversed response order | identical hash (`test_same_input_same_canonical_blueprint`, `test_response_order_does_not_change_the_blueprint`) |
| Same blueprint activated twice, 60 cycles | identical state and intervention IDs (`test_generated_world_is_deterministic`) |
| Simulation across processes (was broken on Paresh) | fixed. The twin drew RNG in hash-dependent dict order; it now uses its canonical entity order. Demo world, 150 cycles, `PYTHONHASHSEED` 0 / 1 / 2 → identical state `c5af98242fbd69df` and twin sum `3030545.0976150776` |

## 14. Simulation integration

- **Arrivals** (fixture, 40,000-capacity venue, 34,000 attendees): the venue reaches 34,438 by 17:00. Ledger: `arrived 33,999.66 = inside 33,999.66 + pending 4e-11`.
- **Egress**: after the event ends, over half of arrivals have egressed, and `arrived = inside + egressed + pending` holds to 1e-6 (`test_flow_model_runs_on_generated_topology_causally`).
- **Road loading**: road junctions on generated access→gate paths are more loaded than off-path roads (mean utilisation gap > 0.05). Live Wembley: "Empire Way × Engineers Way" is the most loaded junction.
- **Access nodes and gates**: every generated gate has inflow; stations and parking carry arrivals.
- **Cycle latency on real graphs** (in-process):

| Graph | Median cycle | p95 cycle |
|---|---|---|
| Ahmedabad 5 km, 734 entities | 112 ms | 521 ms |
| Wembley 2 km, 479 | 75 ms | 500 ms |
| Wembley 3.5 km, 1,116 | 179 ms | 1,316 ms |
| Wembley 5 km, 2,148 | 371 ms | 2,045 ms |

At 2,148 entities, candidate evaluation exceeds its budget and the existing backpressure falls back (see §24).

## 15. What-If integration

- **Fixture** (`test_whatif_on_generated_world_is_isolated_and_meaningful`): closing a generated gate → that gate's peak falls and every other gate's peak rises. The live state is byte-identical before and after, and the twin ensemble sum is unchanged. Demo ID `gate_3` → 400 `INVALID_SCENARIO`.
- **Live Wembley**: closing "Access N 1" → "Access N 2" goes 0.11 → 0.31 and "Access NW" 0.64 → 0.80 (1,712 ms with candidates at 2,148 entities; 504 ms at 479).

## 16. Intervention integration

- **Fixture** (`test_generated_source_destination_interventions`): `gate_redistribution`, `reroute_transport` and `parking_redistribution` between generated entities all show the source falling and the destination rising versus a do-nothing clone. Arrivals are conserved to 1e-6.
- Proposals made in the world reference only generated IDs.
- **Approval** (`test_approval_changes_the_generated_world_against_a_matched_counterfactual`): 200 `executing`; the counterfactual's node set equals the generated world's; live and counterfactual diverge.
- **Live Wembley**: an approved `emergency_corridor` on "Engineers Way junction" / "Police" gives Police 1.505 against 1.595 for the do-nothing copy (Δ −0.090).

## 17. Attendee integration

- **Fixture** (`test_attendee_routes_through_the_generated_graph`): generated hotel property → … → gate → venue, with a return route.
- **Live Wembley (final code)**: St George's Hotel → 7 road junctions → gate → venue, 601 s, with a 6-leg return.
- **Hotels live**: 30 generated hotels with `estimated_type_default` rooms (2,411 in total) and price/tier `null`. At 67,150 visitors they saturate; 338 room requests are unmet. A budget-filtered recommendation returns 0 options with an explanation rather than assuming unknown prices are cheap.

## 18. GNN safety

- Generated worlds: `/health` cascade shows `active_source: "deterministic"` with detail "HX-Cascade GNN was trained on the synthetic demo topology only; this world uses the deterministic flow cascade."
- `WorldInfo.cascade_source` is `deterministic`.
- Cascades have `ml_enhanced: false` and every step has `confidence: null` (`test_generated_world_does_not_claim_gnn`).
- The GNN is not called for generated worlds (`engine._analyse`). The demo world still reports `gnn` (`test_legacy_synthetic_world_still_works`).

## 19. Frontend E2E (headless Chrome 154, `npm run dev:live`, live backend)

| Step | Result |
|---|---|
| Venue search | "Narendra Modi Stadium" → 2 results in 413 ms; "Wembley Stadium" → 2 results; coordinates `51.5560695, -0.2796034` → 1 result |
| Radius | slider plus number box; 50 km and 0 → "Radius must be between 0.25 and 5 km" and Build disabled |
| Footprint | ring animates on change before the build; the built footprint ring and venue outline render on the OSM basemap |
| Build (loading state) | 5 stages with spinner and per-stage seconds; 16.6 s (NMS 2 km) and 21.6 s (Wembley 3.5 km) click-to-summary |
| Blueprint summary | counts, provenance, warnings, LIVE OSM DATA badge, OSM timestamp, attribution; layers revealed road → transit → parking/hotels/emergency → access points (real data) |
| Activation | 0.43 s to Command Centre rendering all 109 / 1,116 nodes (= `/graph`) |
| Change radius / venue | graph and map change (109 → 734 → 479 nodes); no stale graph |
| Pages | all 10 routes on the generated world, 0 JS errors; no demo names |
| Mock mode | `/venue` shows "needs the live backend", no search box, 0 errors |
| Final run | 0 console errors. An earlier run showed React duplicate-key warnings from identical blueprint warnings; fixed (warnings collapsed with counts, index keys) |

## 20. Performance

| Stage | Typical | Slowest observed | Size |
|---|---|---|---|
| Venue search (Nominatim) | 66–580 ms | 1,028 ms | 2 results |
| Overpass acquisition | 11–20 s | 79.6 s (Wembley 5 km) | 1 request |
| Normalisation + blueprint | 4.5 ms (41 ent.) → 128 ms (1,116) | 268 ms (2,148 / 3,234) | — |
| Validation | 0.2–2 ms | 18.5 ms | — |
| Activation (backend) | 45–85 ms | 154 ms (2,148) | — |
| Activation → map rendered (browser) | 0.43 s | 0.43 s (1,116) | — |
| Cycle (median / p95) | 75 / 500 ms (479 ent.) | 371 / 2,045 ms (2,148) | — |

**Target sizes**

| Target | Nearest measured graph | Median / p95 cycle |
|---|---|---|
| ~500 nodes / 1,000 edges | 481 / 1,093 | 75 / 500 ms |
| ~1,000 / 2,000 | 1,116 / 1,942 | 179 / 1,316 ms |
| 2,000+ | 2,148 / 3,234 | 371 / 2,045 ms |

## 21. Provider fallback

- **Live failure**: every build here first hit `overpass-api.de` (connection reset: `network error (URLError)`, retried once) and then **failed over** to `overpass.openstreetmap.fr`.
- **Total failure** (test): the job fails with `GEO_PROVIDER_UNAVAILABLE`, "Live geospatial data is unavailable (…). The current world was not changed." The failed stage is `acquiring_osm`, and `/world` is unchanged and not relabelled.
- **Nominatim rate limit** (429, observed live): search returns `all_failed: true` and the UI suggests coordinates.
- **Offline**: saved blueprints re-activate from the DB, and the synthetic demo world is one click away. Snapshot data is labelled `osm_snapshot` and never "live".

## 22. Errors tested

**Radius:** 0, negative, NaN, ±∞, "abc", null, `true`, 100 m, 60 km, 1e9 → rejected (400 `INVALID_RADIUS`).

**Venue:**
- Query shorter than 3 or longer than 200 characters → rejected.
- Coordinates outside range → rejected.
- Empty selection → rejected.
- Unknown blueprint or build → 404.

**Providers:**
- Timeout → falls back to the next provider.
- 429 → rate-limited attempt recorded.
- Malformed JSON / non-Overpass payload → `ProviderError`.
- Malformed, duplicate or coordinate-less elements → skipped and counted.

**Blueprint build failures:**

| Case | Result |
|---|---|
| No roads | `NO_ROAD_NETWORK` "…within 1000 m…" |
| No transit or parking | `NO_ARRIVAL_ACCESS` |
| No access | `NO_ACCESS` |
| Too large | `NETWORK_TOO_LARGE` |
| Invalid blueprint | `BLUEPRINT_INVALID` |
| Invalid event times | `INVALID_SCHEDULE` |

**Also:**
- No hotels, parking or emergency facilities → warnings, not crashes.
- Demo IDs used in a generated world → 400.

## 23. Regression results

| Command | Result |
|---|---|
| `DATABASE_URL=<scratch> .venv/bin/python -m pytest tests/ -q` | **192 passed** (126 pre-existing + 66 new), 128.9 s |
| `pytest tests/test_geo_*.py` without `DATABASE_URL` | 44 passed (no network, temp DB) |
| `npm test` | 23/23 mocks valid; mock lifecycle ✓; WS reconnect ✓ (new); store world ✓ (new) |
| `vite build` (to scratch outDir) | built in 2.07 s |
| `scripts.export_schemas` | 41 schemas (7 new, 7 additively changed); 23/23 mocks still valid |
| Legacy synthetic world | tests pass; UI switch back renders 67 nodes, chip SYNTHETIC DEMO, `/health` cascade `gnn` |

**Blockers fixed as part of this feature** (they directly blocked runtime graph replacement, resync or deterministic startup):
1. **`ws.js` sequence reset per connection.** After a backend restart the UI froze. Now a restart with a generated world recovers without a reload.
2. **Twin RNG order depended on per-process hashing.** Now deterministic across processes.

Other Phase 0/1 regressions documented in `PARESH_BRANCH_ANALYSIS.md` were **not** touched: speed validation, interruptible clock, Commander cache on seek, equilibrium semantics, and venue ≥1.0 banding.

## 24. Remaining limitations

1. **Capacities are mostly estimates.** Examples: Wembley 2 km has 4 `osm_attribute` capacities out of 481 entities; Narendra Modi Stadium's derived 85,200 against its real ~132,000. Every value says so (`capacity_source` / `capacity_confidence`); organiser input overrides the venue.
2. **The crowd model is the Paresh synthetic flow model**, run on a real network geometry. It is not a real crowd prediction, and there are no real sensor feeds.
3. **No transit line or route modelling** for generated worlds: stations are independent access nodes, and metro riders split over rail/metro stations by capacity. Hotel prices are unknown (null).
4. **Access points are limited by mapped entrances and the modelled road classes** (motorway → tertiary, excluding service roads). Narendra Modi Stadium gets 2, both in the NW sector.
5. **Walking crowds saturate the few tertiary junctions near gates.** Many road junctions read critical at peak; this follows from the documented storage estimate (2.5 people per lane-metre) and is not tuned to look good.
6. **Performance above ~1,000 entities.** At ~2,100 entities the p95 cycle reaches ~2 s and candidate evaluation exceeds its 1.5 s budget, so relief falls back to template estimates (0.0) under the existing backpressure. Recommended: ≤3.5 km for dense cities and ≤5 km elsewhere; default 2 km. Hard cap `max_nodes` 2,500.
7. **Public Overpass latency varies** (11–80 s observed), and `overpass-api.de` is unreachable from this network. A self-hosted Overpass or regional extract would be needed for a guaranteed demo; saved blueprints re-activate offline.
8. **Road-junction entity IDs at the footprint boundary change with the radius** (the OSM data itself is a superset; see §12).
9. **The GNN is unsupported for generated topologies** (deterministic cascade only). Retraining on generated graphs is future work.
10. **`WhatIfPanel.jsx`** (unmounted) still has demo presets. **`README.txt`** and CLAUDE.md's `.env.example` line were already stale before this work and were left alone.
11. **Other Phase 0/1 regressions** from the forensic analysis remain unfixed (they are out of scope and did not block this feature).
