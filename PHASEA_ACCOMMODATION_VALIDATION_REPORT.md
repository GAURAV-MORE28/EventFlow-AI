# Phase A: Accommodation Capacity and Partial Hotel Demand (Validation Report)

Branch `phase2-complete`, working tree on top of `8e517b8`. Not committed.
Evidence date: 2026-09-27. All runs used scratch SQLite databases under the session scratchpad;
`Backend/eventflow.db` was not written (its mtime is still 08:00).

The report uses three labels throughout:

| Label | Meaning |
|---|---|
| **REAL / MAPPED** | Read from OpenStreetMap as tagged: hotel existence, location, name, `rooms`, `capacity:rooms`, `beds`, `capacity:beds`, `capacity:persons`, brand/operator/website. |
| **DERIVED** | Computed from mapped values by a stated rule, e.g. rooms = beds ÷ `guests_per_room`, or a per-subtype default when nothing is tagged. |
| **SIMULATED** | Produced by the deterministic flow model: attendance, lodging demand, check-in/out timing, baseline occupancy, allocation, free rooms, unmet demand. None of it is observed data. |

---

## 1. Executive summary

Phase A turns hotels from "every out-of-town visitor wants a room forever" into a bounded
resource with partial, time-limited demand:

- **Capacity.** Each hotel's rooms come from OSM `capacity:rooms`, else `rooms`
  (REAL / MAPPED, high confidence). Failing those, they are DERIVED from beds
  (`capacity:beds` / `beds` / `capacity:persons` ÷ 2.2, medium confidence). Failing that, they are
  a DERIVED per-subtype estimate (low confidence). The source and confidence are explicit on every
  hotel, and the UI prefixes anything not mapped with `~`. Nothing estimated is called "actual".
- **Demand.** Only `lodging_share` of attendance needs a room. The share is per event, or
  `hospitality.default_lodging_share = 0.25`; values outside 0–1 → HTTP 400. The rest are locals.
  Unplaced lodging guests are counted as unmet and travel as locals; they are never dropped.
- **Timing.** Check-in follows a curve over the 180 min before the start. Check-out runs from
  end + 120 min over 120 min and frees rooms.
- **Allocation.** Deterministic and capacity-limited. Free rooms, saturation (0 free), TIGHT
  (≤ 10% free) and unmet demand are first-class API fields.
- **Contracts.** API changes are additive; schemas were re-exported from Pydantic; mocks were
  regenerated and validate 23/23.
- **Test and live results:**
  - backend 233 passed (41 new Phase A tests); `npm test` passes; the production build passes;
  - live Ahmedabad and live London both pass;
  - a browser E2E on Wembley passes with no JS errors;
  - allocation is identical across processes with different `PYTHONHASHSEED`.

## 2. Existing architecture before Phase A

State at `8e517b8`:

- **Hotel extraction** (`blueprint.py`):
  - `rooms` tag → `rooms_source="osm_attribute"`, medium confidence;
  - else `beds // 2` → `"osm_attribute_beds"`, low confidence;
  - else a per-subtype default → `"estimated_type_default"`, low confidence.
  - `beds` was not kept, and `capacity:rooms` / `capacity:beds` / `capacity:persons` were ignored.
- **Demand** (`generator._room_demand`):
  - rooms = attendance × `out_of_town_share` × `room_need_share` ÷ `guests_per_room`;
  - `prebooked_share` of that was booked at sim start, the rest over `booking_window_min`.
- **Ledger:** two global counters, `_requested_rooms` and `_unmet_rooms`. There was no per-event
  split, **no check-out** (rooms stayed booked until the run ended) and no local-vs-hotel field on
  events.
- **Status:** thresholds on occupancy ratio (`saturation_threshold 0.95`, `limited_threshold 0.85`),
  so a hotel could be "saturated" with free rooms.
- **Kept unchanged:** hotel-origin transport load, What-If `hotel_shortage` (`room_mult`), the
  `accommodation_rebalance` intervention and the stay finder. All of them now read the new ledger.

## 3. New architecture

```
OSM tags ──hotel_capacity()──► property {rooms_total, rooms_source, rooms_confidence,
                                           bed_capacity, bed_source, effective_guest_capacity, capacity_tags}
event {attendance, lodging_share?} ──► lodging_guests = attendance × share        (SIMULATED)
                                  └──► local_guests   = attendance − lodging_guests
check-in CDF over [start−180m, start] ──► requested guests/rooms (per event)
_allocate(): logit spread + greedy fill in score order, capacity-limited ──► placed | unmet
check-out CDF over [end+120m, end+240m] ──► checked_out (rooms released)
per event: requested = placed + unmet ; in_house = placed − checked_out
per hotel: event_rooms = Σ in_house ; occupied = baseline + event ; free = floor(bookable − occupied)
transport: hotel-origin share = lodging_share × placed/requested ; unmet → local origins
```

There is one simulator. The ledger lives in the existing `SyntheticGenerator` (`_lodging`, cloned
with the rest of `_DYNAMIC`), so the live world, the nominal twin, counterfactual forks and
What-If clones all carry it.

## 4. Exact files changed

**Backend**
- `Backend/app/geospatial/blueprint.py`: `CAPACITY_TAGS`, `parse_capacity`, `hotel_capacity`; the
  hotel loop uses them; new property and meta fields.
- `Backend/app/geospatial/service.py`: passes `guests_per_room` to the builder and `lodging_share`
  into the activated event.
- `Backend/app/ml_reference/generator.py`:
  - `DEFAULT_HOSPITALITY`;
  - the per-event `_lodging` ledger: `_lodging_fractions`, `_hotel_share`, `_release`,
    `_allocate`, `_allocate_rooms`, `lodging_state`, `_lodging_totals`;
  - integer free rooms in `properties_state`, plus lodging fields in `event_states` and `stats`.
- `Backend/app/services/events.py`: `_lodging` validation; `lodging_share` in reset / create /
  update / view.
- `Backend/app/services/accommodation.py`: new property fields, `lodging_by_event`, summary
  aggregates + `shortage_message`, "No room available" + `no_availability_reason`, `cluster_options`.
- `Backend/app/services/engine.py`: `optimiser_context["hotel_options"]`.
- `Backend/app/ml_reference/optimiser.py`: rebalance alternatives ranked by free rooms, travel
  time, load, then price.
- `Backend/app/api/routes.py`: `lodging_share` in `EVENT_FIELDS`.
- `Backend/app/providers/data.py`: file events accept `lodging_share`.
- `Backend/app/schemas.py`: additive fields, see §6.
- `Backend/config.yaml`: new `hospitality` block; demo events get explicit `lodging_share`.

**Frontend**
- `Frontend/src/lib/accommodation.js` (new): label helpers only.
- `Frontend/src/routes/Accommodation.jsx`, `Attendee.jsx`, `Events.jsx`, `VenueSetup.jsx`.
- `Frontend/scripts/test-accommodation-labels.mjs` (new), wired into `Frontend/package.json` `test`.
- `Frontend/src/mocks/*.json`: 20 files regenerated by `scripts.export_mocks`.

**Contracts** (generated by `scripts.export_schemas`, not hand-edited)
- `contracts/schemas/blueprint.json`
- `event_list_response.json`
- `hotel_list_response.json`
- `saturation_response.json`
- `stay_recommendation_response.json`

**Tests**
- New: `Backend/tests/test_accommodation_capacity.py`, `test_accommodation_demand.py`,
  `test_accommodation_world.py`.
- Updated: `Backend/tests/test_geo_blueprint.py` (new source names).

**Docs**
- `CLAUDE.md`, `README.md`, `RUNNING.md`, `implementationstate.md`, and this report.

## 5. New and changed APIs

All changes are additive. No endpoint was removed or renamed.

| Endpoint | Change |
|---|---|
| `GET /accommodation/hotels` | Per hotel: `bed_capacity`, `bed_source`, `effective_guest_capacity`, `guest_capacity_source`, `baseline_rooms_occupied`, `event_rooms_occupied`, `event_guests`, `occupancy_baseline_source`, `capacity_tags`. Summary: `attendance_total`, `local_guests`, `lodging_guests`, `lodging_requested_guests`, `lodging_allocated_guests`, `lodging_unmet_guests`, `lodging_pending_guests`, `lodging_in_house_guests`, `lodging_checked_out_guests`, `default_lodging_share`, `guests_per_room`, `shortage_message`. New `lodging_by_event[]`. |
| `POST /accommodation/recommend` | Never returns a saturated hotel. With no free room anywhere: `explanation` starts "No room available in the selected network", plus a new `no_availability_reason`. |
| `GET /events`, `POST /events`, `POST /events/{id}` | Request: optional `lodging_share` (0–1, 400 otherwise). View: `lodging_share` (null = default), `lodging_share_effective`, `lodging_share_source` (`event` / `default`), `lodging_guests`, `local_guests`, `lodging_allocated_guests`, `lodging_unmet_guests`. |
| `POST /blueprints/{id}/activate` | `event.lodging_share` (validated). |
| `GET /accommodation/saturation` | The shared `HotelProperty` gains the new fields. |

**Semantic change to disclose:**
- **Status now uses free rooms, not occupancy ratio.** A hotel is `saturated` only at 0 free
  rooms, and `limited` at ≤ 10% free. The old model could call a hotel with 5% free "saturated".
- **The `rooms_source` values follow the new vocabulary.** Old → new: `osm_attribute` →
  `osm_rooms`; `osm_attribute_beds` → `derived_from_osm_beds`; `estimated_type_default` →
  `derived_estimate`. The field and its type are unchanged.
- **Old blueprints keep the old values.** Blueprints saved before Phase A store their properties
  and keep the old strings until rebuilt; the UI renders unknown strings verbatim, so nothing breaks.
- **`guests_per_room` bounds beds-derived rooms more tightly.** They are now beds ÷ 2.2 (was beds // 2).

## 6. Schema changes

The models in `Backend/app/schemas.py` are authoritative; the JSON schemas were regenerated with
`python -m scripts.export_schemas --out ../contracts/schemas` (41 files written, 5 changed).

- **New model:** `LodgingEvent`.
- **Extended:** `HotelProperty`, `HotelPropertyRecord`, `HotelSummary`, `HotelListResponse`,
  `StayRecommendationResponse`, `EventView`, `EventCreateRequest`, `EventUpdateRequest`,
  `ActivationEvent`.
- **Defaults:** every new field is optional or defaulted, so older payloads still validate.

## 7. OSM capacity ingestion (REAL / MAPPED)

`hotel_capacity(tags, subtype, guests_per_room, default_rooms)` in `blueprint.py`:

1. `capacity:rooms` → `osm_capacity_rooms`, high confidence.
2. `rooms` → `osm_rooms`, high confidence.
3. `capacity:beds` → `derived_from_osm_capacity_beds`; `beds` → `derived_from_osm_beds`;
   `capacity:persons` → `derived_from_osm_capacity_persons`. Rooms = ceil(beds ÷ 2.2), medium
   confidence.
4. Otherwise `derived_estimate` from `hotel_default_rooms[subtype]`, low confidence.

Parsing rules:
- `parse_capacity` accepts only a plain number (`^\s*\d+(\.\d+)?\s*$`), rounded.
- Values < 0.5 or > the bound (20,000) → ignored. So are ranges (`"50-60"`), text (`"many"`),
  negatives and units; each falls through to the next source.
- When both rooms and beds are tagged, rooms set `room_capacity` and beds are kept as
  `bed_capacity`.
- `effective_guest_capacity` = min(rooms × gpr, beds) when both exist.
- The raw tags are preserved in `capacity_tags`, and in node meta alongside `brand` / `operator` /
  `website`.

Existing hotel extraction (tourism=hotel/motel/hostel/guest_house/apartment, dedup, footprint
clip) is unchanged.

## 8. Capacity provenance and confidence

| Field | Values | Label |
|---|---|---|
| `rooms_total` + `rooms_source` | `osm_capacity_rooms`, `osm_rooms` | REAL / MAPPED |
| | `derived_from_osm_*` | DERIVED (from mapped beds) |
| | `derived_estimate` | DERIVED (type default, no OSM data) |
| `rooms_confidence` | high / medium / low | as above |
| `bed_capacity`, `bed_source` | OSM bed tags or null | REAL / MAPPED or null |
| `occupancy_baseline_source` | `"simulated"` | SIMULATED |
| `price_per_night_paise` | null → "unknown" | not invented |

The UI shows `361 rooms · OSM rooms · High confidence` or `~80 rooms · Derived estimate · Low
confidence`. `test-accommodation-labels.mjs` asserts the `~` prefix and that no label says "actual".
The browser E2E found no "actual" anywhere on the Accommodation page.

## 9. `lodging_share` implementation

- **Precedence:** event `lodging_share` (API, activation form, `config.yaml` events, file
  provider), else `hospitality.default_lodging_share` (0.25). `lodging_share_source` reports which.
- **Validation:** `events._lodging()` accepts None or 0 ≤ x ≤ 1. NaN, non-numeric, < 0 and > 1 →
  `ApiError INVALID_REQUEST` (HTTP 400). Pydantic `Field(ge=0, le=1)` also guards the request
  models. Nothing is clamped.
- **Demo continuity:** the three demo events set `lodging_share` = `out_of_town_share` × the former
  `room_need_share` 0.34 (0.102, 0.034, 0.136), so the synthetic demo's hotel demand is unchanged in
  magnitude.
- **Where to set it:**
  - Events page field "Need a hotel room (%)";
  - Venue & Network activation field "Lodging share percent".

## 10. Local vs hotel flow (SIMULATED)

- `lodging_guests = attendance × share`, and `local_guests = attendance − lodging_guests`. Both are
  on `EventView` and in `lodging_by_event`.
- `generator._hotel_share(ev)` = share × placed ÷ requested (the `hotel_origin_share`). Only that
  fraction of arrivals starts at hotel access nodes; the rest, including unmet lodging guests,
  start from home origins.
- `test_hotel_origin_demand_reaches_the_transport_network` covers this. With 20,000 attendees,
  share 0.6 puts more load on hotel-served stations than share 0.0, while attendance is unchanged.
- In the Attendee app, choosing a "Local visitor (no hotel)" origin plans the journey from home
  (browser E2E: "travelling as a local visitor" shown with a journey).

## 11. Room allocation algorithm (SIMULATED)

Each step, per event, in sorted event-id order:

1. **Target.** Requested guests = lodging_guests × check-in CDF(t).
2. **New requests.** Δ > 0 → rooms = Δ ÷ gpr → `_allocate(rooms)`:
   - Score every hotel with free bookable rooms. The score uses price (0.5 when unknown), travel
     time to the event's venue, tier, free share and hotel-station congestion.
   - Spread by a deterministic logit, then fill greedily in score order up to each hotel's free
     rooms. Any remainder is unmet.
3. **Demand drops** (cancel / attendance cut). Release unmet first, then in-house rooms pro rata
   over sorted property ids.
4. **Check-out.** Release in-house rooms by the check-out CDF.
5. **Supply shrink** (What-If hotel shortage, closure). Displaced guests are re-allocated
   elsewhere. Anything that cannot be placed becomes unmet, attributed to events in proportion to
   their in-house share.

Bookable rooms = min(rooms, beds ÷ gpr) × `room_mult`. Baseline occupancy (non-event guests) =
`base_occupancy` × rooms, capped.

There is no randomness beyond the seeded generator. Iteration is over sorted ids, so no `hash()`
ordering affects the result.

## 12. Check-in and check-out timing (SIMULATED)

- **Check-in:** normal CDF centred on the middle of [start − `check_in_lead_min`, start],
  sd = window ÷ 4 (~95% inside the window).
- **Check-out:** the same shape over [end + `check_out_lag_min`, end + `check_out_lag_min` +
  `check_out_window_min`].
- **Defaults:** 180 / 120 / 120 min.
- **Schedule changes:** an event delay or reschedule moves both windows, because they derive from
  the event's current `start_time` / `end_time`.
- **Evidence:**
  - `test_occupancy_rises_through_check_in_and_drains_after_check_out`;
  - live Ahmedabad: 23:00 occupied 46 / free 49 after the check-out drain;
  - live London 60k: 1,723 guests checked out, 771 rooms free again.

## 13. Saturation and unmet demand

- **Free rooms:** `rooms_available` = floor(bookable − baseline − event rooms), an integer ≥ 0, and
  `rooms_in_service = rooms_occupied + rooms_available`.
- **Status:** `saturated` when `rooms_available == 0`; `limited` (UI "TIGHT") when free ≤
  `tight_free_share` × in-service rooms; otherwise `available`.
- **Unmet demand:** `lodging_unmet_guests` in the summary and per event. The summary
  `shortage_message` reads, e.g., "242 event guests could not be placed in 14 hotels within the
  selected network (simulated demand against mapped or estimated capacity)."
- **Stay finder:** it excludes saturated hotels. With none free it returns "No room available in
  the selected network. …" + `no_availability_reason`.
- **Overbooking is impossible:** `test_hotel_shortage_produces_unmet_demand_not_overbooking`
  asserts occupied ≤ in-service and event guests ≤ effective guest capacity for every hotel, every
  step.

## 14. Frontend changes (render only)

No business logic lives in the frontend. Status, free rooms, unmet demand and shares are all
backend values; `lib/accommodation.js` only formats them.

- **Accommodation:**
  - stat cards: total hotels, total rooms, occupied, free, lodging demand, unmet demand;
  - a red shortage banner showing the backend message;
  - an "Accommodation demand by event (simulated)" table;
  - a Capacity column (count + source + confidence);
  - "occupied · free" and event guests per hotel;
  - SATURATED rows say "0 rooms available";
  - price "unknown"; the zone filter is built from the data.
- **Attendee:**
  - hotel cards show capacity, status and source;
  - "No room available in the selected network" + reason;
  - a "Local visitor (no hotel)" origin group and a note for local travellers.
- **Events:** "Need a hotel room (%)" input; each row shows "25% need a room (event) · 2,000
  lodging · 6,000 local · 245 unplaced".
- **Venue & Network:** "Lodging share percent" on activation.

## 15. Test inventory

**`tests/test_accommodation_capacity.py`** (19 test cases; the parser is parametrised)
- Tag parser: plain numbers, decimals, whitespace, ranges, text, negatives, zero, out-of-range.
- `rooms` → mapped, high confidence; beds-only → rooms derived explicitly, beds kept.
- `capacity:rooms` takes precedence, and beds constrain guests.
- Malformed values fall through to the next source.
- Blueprint hotels keep their own provenance.
- Hotel capacity is part of the canonical blueprint (hash-stable).

**`tests/test_accommodation_demand.py`** (18 cases), mapped to spec tests A–K:
- **A** `test_lodging_share_zero_means_no_hotel_demand`
- **B** `test_lodging_share_one_means_everyone_needs_a_room`
- **C** `test_quarter_of_attendance_enters_accommodation_demand`
- **D** `test_attendance_changes_accommodation_demand`
- **E** `test_share_changes_hotel_demand_but_not_attendance`
- **F** `test_two_events_keep_their_own_shares_and_the_default_applies`
- **G** `test_invalid_lodging_share_is_rejected` (parametrised)
- **H**
  - `test_hotel_shortage_produces_unmet_demand_not_overbooking`
  - `test_whatif_hotel_shortage_raises_unmet_without_deleting_attendees`
- **I**
  - `test_occupancy_rises_through_check_in_and_drains_after_check_out`
  - `test_cancelling_an_event_releases_its_rooms`
  - `test_unmet_lodging_guests_travel_from_home_not_dropped`
- **J / K**
  - `test_same_inputs_same_allocation`
  - `test_allocation_is_identical_in_a_fresh_process` (subprocess, different `PYTHONHASHSEED`)
- **Beds limit** `test_generated_style_properties_with_beds_limit_guests`
- **Invariants.** `check_invariants()` runs throughout and asserts:
  - requested = placed + unmet;
  - checked_out ≤ placed;
  - Σ in-house = property event rooms;
  - occupied ≤ in-service;
  - attendance conserved.

**`tests/test_accommodation_world.py`** (4): the generated-world end-to-end test, with no network.
- `test_generated_world_accommodation_end_to_end`: fixture city → blueprint → activate
  (6,000 × 0.15) → cycles → API. It checks:
  - mapped / beds / `capacity:rooms` / estimate sources;
  - the 900 / 5,100 split;
  - occupancy rising, free rooms falling, unmet > 0 with the banner;
  - ledger identities and the event ledger;
  - no saturated recommendation.
- `test_hotel_origin_demand_reaches_the_transport_network`
- `test_lodging_share_is_validated_over_the_api` (create / update / activate → 400)
- `test_restart_restores_hotel_capacity_metadata`

**Geo hotel tests 1–8** (`tests/test_geo_blueprint.py` updated + the capacity tests above):
1. rooms mapped;
2. beds only;
3. `capacity:rooms`;
4. no tags → estimate;
5. malformed;
6. provenance per hotel;
7. canonical hash;
8. restore after restart.

**Frontend:** `scripts/test-accommodation-labels.mjs` checks mapped vs `~` estimated labels,
TIGHT / SATURATED wording, and never "actual".

## 16. Test results

| Command | Result |
|---|---|
| `cd Backend && DATABASE_URL=sqlite:///<scratch>/pa_final.db .venv/bin/python -m pytest tests/ -q -p no:cacheprovider -W ignore` | **233 passed** in 150.5 s |
| `cd Frontend && npm test` | mocks 23/23 valid; mock lifecycle ✓; WS reconnect ✓; world change ✓; accommodation labels ✓ |
| `cd Frontend && npm run build` | ✓ built in 2.0 s (the existing chunk-size warning only) |
| `python -m scripts.export_schemas --out ../contracts/schemas` | 41 schemas, 5 changed |
| `python -m scripts.export_mocks --out ../Frontend/src/mocks --cycles 160` → `npm run validate:mocks` | 23/23 valid |

**Browser E2E (headless Chrome over CDP, live backend + Vite, scratch DB)**
- **Setup:** Venue & Network → search "Wembley Stadium" (Nominatim) → 2 km → Build blueprint →
  attendance 8,000, lodging 25% → Activate. Result: world `bp_1266d90450a9`, 481 nodes.
- **Accommodation at 14:00:**
  - 14 hotels, 1,426 rooms, 879 occupied, **547 free**;
  - lodging demand 2,000, unmet 0;
  - one small apartment already SATURATED ("0 rooms available");
  - "OSM rooms · High confidence" and "~80 rooms · Derived estimate · Low confidence" both shown.
- **At 15:05, check-in under way:** 1,348 occupied, **78 free**; most hotels SATURATED.
- **At 16:08, event start:** 1,426 occupied, **0 free**, 14/14 SATURATED, **unmet 242**, and the
  red banner "242 event guests could not be placed in 14 hotels within the selected network …".
  - The by-event table showed 8,000 · 25% (event) · 6,000 local · 2,000 need a room · 1,726
    placed · 242 unmet · 33 not yet checked in.
- **Attendee (hotel origin):** "No room available in the selected network" with the reason; no
  saturated hotel offered.
- **Attendee (local origin):** "Ark Elvin Academy / Park Lane" → "travelling as a local visitor",
  journey shown.
- **Events:** "25% need a room (event) · 2,000 lodging · 6,000 local · 245 unplaced". The API then
  reported 245.1 unmet in both `/accommodation/hotels` and `/events`; the 242 on the Accommodation
  page was captured two cycles earlier.
- **JS console errors: none.** Screenshots: `pa_accommodation_t0.png`,
  `pa_accommodation_event.png`, `pa_attendee_hotel.png`, `pa_attendee_local.png` (session
  scratchpad).

## 17. Live Ahmedabad result (Narendra Modi Stadium, 2 km, 60,000 attendees, default share 0.25)

- **REAL / MAPPED:** 4 hotels in the footprint; 0 carry any capacity tag.
- **DERIVED:** all 4 are `derived_estimate`, low confidence, 95 rooms in total.
- **SIMULATED:**
  - lodging 15,000 guests / local 45,000;
  - allocated 115 guests, **unmet 14,885** (capacity is tiny against demand, and the page says so);
  - after check-out (23:00): occupied 46, free 49 (rooms drained back to the simulated baseline).
- **Performance:** cycle median 18 ms, p95 81 ms.

## 18. Live London result (Wembley Stadium, 2 km)

**60,000 attendees, default share 0.25**
- **REAL / MAPPED:** 14 hotels; 2 carry `rooms`, both `osm_rooms`, high confidence: Hilton London
  Wembley 361 and Wembley International Hotel 165.
- **DERIVED:** 12 `derived_estimate`; 1,426 rooms in total.
- **SIMULATED:**
  - allocated 1,726 guests, **unmet 13,274** (1,726 + 13,274 = 15,000);
  - after check-out: 1,723 checked out, 771 rooms free again.
- **Performance:** cycle median 78 ms, p95 626 ms.

**8,000 attendees, share 0.25** (API run, and repeated in the browser E2E above)
- Free rooms 547 → 104 → 0.
- Saturated hotels 1 → 12 → 14.
- Unmet guests 0 → 229 → 274.

## 19. Determinism result

- **Unit tests:** `test_same_inputs_same_allocation` and `test_allocation_is_identical_in_a_fresh_process`
  (a subprocess with a different `PYTHONHASHSEED`) pass.
- **Live London, 8,000 attendees:** the allocation fingerprint at cycle 330 is `1a046a6c9d8f5420`,
  identical in a second process started with `PYTHONHASHSEED=77`.
- **No new randomness:** allocation adds no random draws; iteration is over sorted event and
  property ids.

## 20. Performance result

| Run | Cycle median | p95 |
|---|---|---|
| Ahmedabad 2 km, 60k | 18 ms | 81 ms |
| London 2 km, 60k (481-node graph) | 78 ms | 626 ms |

Per step, allocation is O(events × hotels): 14 hotels here. The full backend suite takes 150 s
for 233 tests. A copy of the existing database (not the file itself) started
without errors and served the new fields, so no migration framework was needed.

## 21. Remaining limitations

- **Sparse OSM tags.** Most hotels are not tagged with `rooms` in OSM: 0/4 in Ahmedabad and 2/14 at
  Wembley. Capacity is therefore mostly a labelled estimate. OSM does not guarantee complete
  capacity data.
- **Simulated values.** Baseline occupancy, bookings, check-in/out timing, prices and all
  attendance-derived demand are simulated. No observed occupancy source is used.
- **Radius-bound network.** Unmet demand means "no room inside the selected network", not in the
  city. Hotels beyond the radius are not modelled; guests who miss out travel as locals.
- **Same-day stays only.** Multi-night stays, early departures and shared rooms beyond
  `guests_per_room` are not modelled.
- **`rooms_source` vocabulary changed** (see §5). Blueprints saved before Phase A keep the old
  strings until rebuilt.
- **Ledger precision.** Guest counts are fractional internally (flow model). The API rounds guests
  to 0.1 and the UI to integers, so displayed totals can differ by 1.
- **Scope.** The GNN / cascade subsystem was not changed.
