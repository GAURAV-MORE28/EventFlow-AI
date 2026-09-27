"""Accommodation intelligence: availability, search, saturation and recommendation.

Occupancy is live simulation state (SyntheticGenerator's booking model), so a
hotel shortage, a new event or an approved `accommodation_rebalance` shows up
here on the next cycle. Recommendations score every property with rooms on
five explainable factors — never "nearest hotel wins":

    availability   free rooms relative to the property's size
    price          against the guest's budget (or the market range)
    travel         door-to-venue time over the transport graph
    transport      walking time to the property's transport node
    congestion     current/forecast load on that transport node
"""
from __future__ import annotations

from typing import Any

from ..config import get_config
from ..errors import ApiError
from ..ml_reference.common import clamp

DEFAULT_WEIGHTS = {"availability": 0.25, "price": 0.20, "travel": 0.25, "transport": 0.15, "congestion": 0.15}

# Segment emphasis (00 §1.8 segments): multipliers on the base weights.
SEGMENT_EMPHASIS = {
    "price_sensitive": {"price": 1.8},
    "time_sensitive": {"travel": 1.8, "transport": 1.3},
    "group": {"availability": 1.6},
    "premium": {"price": 0.4, "travel": 1.3},
    "accessibility_constrained": {"transport": 1.5},
}


def _rupees(paise: int | None) -> str:
    return "price unknown" if paise is None else f"Rs {paise // 100:,}"


def _minutes(sec: float) -> int:
    return int(round(sec / 60.0))


def main_venue(engine: Any) -> str:
    return engine.events.get(engine.events.primary_event_id)["venue_entity_id"]


def property_views(engine: Any, venue: str | None = None) -> list[dict[str, Any]]:
    """Every property with live occupancy and the transport context around it."""
    venue = venue or main_venue(engine)
    store = engine.store
    with engine.world_lock:
        props = engine.generator.properties_state()
        closed = engine.generator.closed_entities() if hasattr(engine.generator, "closed_entities") else set()
    out = []
    for p in props:
        station = p.get("transport_entity_id")
        st = store.entity_states.get(station or "", {})
        fc = store.forecasts.get(station or "", {})
        f1800 = next((pt["predicted_utilisation"] for pt in fc.get("points", []) if pt["horizon_sec"] == 1800), None)
        travel = p["travel_time_to_venue_sec"].get(venue)
        out.append({
            "property_id": p["property_id"],
            "name": p["name"],
            "cluster_entity_id": p["cluster_entity_id"],
            "zone": p["zone"],
            "lat": p["lat"],
            "lon": p["lon"],
            "tier": p["tier"],
            "accessible": p["accessible"],
            "price_per_night_paise": p.get("price_per_night_paise"),
            "rooms_total": p["rooms_total"],
            "rooms_source": p.get("rooms_source", "catalogue"),
            "rooms_confidence": p.get("rooms_confidence", "medium"),
            "price_source": p.get("price_source", "catalogue"),
            "rooms_in_service": p["rooms_available_total"],
            "rooms_occupied": p["rooms_occupied"],
            "rooms_available": p["rooms_available"],
            "occupancy": p["occupancy"],
            "status": p["status"],
            "transport_entity_id": station,
            "transport_name": store.nodes.get(station or "", {}).get("display_name"),
            "walk_to_transport_sec": p["walk_to_transport_sec"],
            "transport_utilisation": round(float(st.get("utilisation", 0.0)), 4),
            "transport_closed": station in closed,
            "transport_forecast_utilisation": None if f1800 is None else round(float(f1800), 4),
            "venue_entity_id": venue,
            "travel_time_to_venue_sec": None if travel is None else int(travel),
            "bed_capacity": p.get("bed_capacity"),
            "bed_source": p.get("bed_source"),
            "effective_guest_capacity": p.get("effective_guest_capacity"),
            "guest_capacity_source": p.get("guest_capacity_source") or "rooms x guests_per_room",
            "baseline_rooms_occupied": p.get("baseline_rooms_occupied"),
            "event_rooms_occupied": p.get("event_rooms_occupied"),
            "event_guests": p.get("event_guests"),
            "occupancy_baseline_source": p.get("occupancy_baseline_source") or "simulated",
            "capacity_tags": p.get("capacity_tags"),
        })
    return out


def lodging_by_event(engine: Any) -> list[dict[str, Any]]:
    """Each event's accommodation demand split (simulation state, people)."""
    with engine.world_lock:
        state = engine.generator.lodging_state() if hasattr(engine.generator, "lodging_state") else {}
    names = {e["event_id"]: e["name"] for e in engine.events.to_generator()}
    return [{"event_id": eid, "name": names.get(eid, eid),
             **{k: (round(v, 1) if k.endswith("_guests") or k == "attendance" else
                    round(v, 4) if isinstance(v, float) else v) for k, v in st.items()
                if k not in ("requested_rooms", "allocated_rooms", "unmet_rooms", "in_house_rooms")}}
            for eid, st in state.items()]


def hotel_list(
    engine: Any,
    venue: str | None = None,
    zone: str | None = None,
    tier: str | None = None,
    max_price_paise: int | None = None,
    min_rooms: int = 0,
    accessible_only: bool = False,
    status: str | None = None,
    sort: str = "occupancy",
) -> dict[str, Any]:
    if venue and venue not in engine.store.nodes:
        raise ApiError("ENTITY_NOT_FOUND", f"No entity with id '{venue}'.", {"entity_id": venue})
    views = property_views(engine, venue)
    items = [
        v for v in views
        if (zone is None or v["zone"].lower() == zone.lower())
        and (tier is None or v["tier"] == tier)
        and (max_price_paise is None or (v["price_per_night_paise"] is not None
                                         and v["price_per_night_paise"] <= max_price_paise))
        and v["rooms_available"] >= min_rooms
        and (not accessible_only or v["accessible"])
        and (status is None or v["status"] == status)
    ]
    keys = {
        "occupancy": lambda v: (-v["occupancy"], v["property_id"]),
        "price": lambda v: (v["price_per_night_paise"] is None, v["price_per_night_paise"] or 0, v["property_id"]),
        "availability": lambda v: (-v["rooms_available"], v["property_id"]),
        "travel_time": lambda v: (v["travel_time_to_venue_sec"] or 10**9, v["property_id"]),
    }
    if sort not in keys:
        raise ApiError("INVALID_REQUEST", f"sort must be one of {sorted(keys)}.", {"sort": sort})
    items.sort(key=keys[sort])
    with engine.world_lock:
        stats = engine.generator.stats()
    total_rooms = sum(v["rooms_in_service"] for v in views)
    occupied = sum(v["rooms_occupied"] for v in views)
    return {
        "sim_time": engine.store.sim_time,
        "summary": {
            "properties": len(views),
            "rooms_in_service": total_rooms,
            "rooms_occupied": occupied,
            "rooms_available": sum(v["rooms_available"] for v in views),
            "occupancy": round(occupied / total_rooms, 4) if total_rooms else 0.0,
            "saturated": sum(1 for v in views if v["status"] == "saturated"),
            "limited": sum(1 for v in views if v["status"] == "limited"),
            "unmet_room_requests": int(round(stats["rooms_unmet"])),
            "effective_guest_capacity": round(sum(v["effective_guest_capacity"] or 0.0 for v in views), 1),
            **{k: stats.get(k) for k in (
                "attendance_total", "local_guests", "lodging_guests", "lodging_requested_guests",
                "lodging_allocated_guests", "lodging_unmet_guests", "lodging_pending_guests",
                "lodging_in_house_guests", "lodging_checked_out_guests")},
            "default_lodging_share": float(get_config().raw.get("hospitality", {}).get("default_lodging_share", 0.25)),
            "guests_per_room": float(get_config().raw.get("hospitality", {}).get("guests_per_room", 2.2)),
            "shortage_message": _shortage_message(stats, len(views)),
        },
        "hotels": items,
        "lodging_by_event": lodging_by_event(engine),
    }


def _shortage_message(stats: dict, properties: int) -> str | None:
    unmet = int(round(stats.get("lodging_unmet_guests") or 0.0))
    if unmet <= 0:
        return None
    where = f"{properties} hotels" if properties else "any hotel"
    return (f"{unmet:,} event guests could not be placed in {where} within the selected network "
            "(simulated demand against mapped or estimated capacity).")


def get_property(engine: Any, property_id: str, venue: str | None = None) -> dict[str, Any]:
    for v in property_views(engine, venue):
        if v["property_id"] == property_id:
            return v
    raise ApiError("PROPERTY_NOT_FOUND", f"No property with id '{property_id}'.", {"property_id": property_id})


def recommend(
    engine: Any,
    destination_entity_id: str | None = None,
    segment_id: str | None = None,
    max_price_paise: int | None = None,
    accessible_only: bool = False,
    rooms: int = 1,
    current_property_id: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    store = engine.store
    venue = destination_entity_id or main_venue(engine)
    if venue not in store.nodes:
        raise ApiError("ENTITY_NOT_FOUND", f"No entity with id '{venue}'.", {"entity_id": venue})
    if rooms < 1:
        raise ApiError("INVALID_REQUEST", "rooms must be >= 1.", {"rooms": rooms})
    segment = next((s for s in store.segments if s["segment_id"] == segment_id), None)
    if segment_id and not segment:
        raise ApiError("INVALID_REQUEST", f"Unknown segment '{segment_id}'.", {"segment_id": segment_id})
    if segment and segment["accessibility_constrained"]:
        accessible_only = True

    views = property_views(engine, venue)
    current = next((v for v in views if v["property_id"] == current_property_id), None)
    if current_property_id and not current:
        raise ApiError("PROPERTY_NOT_FOUND", f"No property with id '{current_property_id}'.", {"property_id": current_property_id})

    weights = dict(get_config().raw.get("hospitality", {}).get("recommendation_weights") or DEFAULT_WEIGHTS)
    for k, m in SEGMENT_EMPHASIS.get(segment_id or "", {}).items():
        weights[k] = weights.get(k, 0.0) * m
    wsum = sum(weights.values()) or 1.0
    weights = {k: v / wsum for k, v in weights.items()}

    pool = [
        v for v in views
        if v["property_id"] != current_property_id
        and v["rooms_available"] >= rooms
        and (not accessible_only or v["accessible"])
        and v["travel_time_to_venue_sec"] is not None
    ]
    excluded_budget = 0
    if max_price_paise is not None:
        # A property whose price is unknown cannot be shown to be within budget.
        within = [v for v in pool if v["price_per_night_paise"] is not None
                  and v["price_per_night_paise"] <= max_price_paise * 1.25]
        excluded_budget = len(pool) - len(within)
        pool = within
    if not pool:
        free_any = sum(v["rooms_available"] for v in views)
        if not views:
            reason = "There are no hotels in the selected network."
        elif free_any < rooms:
            reason = (f"Every hotel in the selected network is full ({sum(1 for v in views if v['status'] == 'saturated')}"
                      f" of {len(views)} saturated).")
        elif excluded_budget:
            reason = "The hotels with free rooms have no known price within the budget."
        else:
            reason = "No hotel with free rooms matches the accessibility / room-count requirements."
        return {
            "sim_time": store.sim_time, "destination_entity_id": venue, "options": [],
            "explanation": f"No room available in the selected network. {reason}",
            "current": current, "no_availability_reason": reason,
        }

    prices = [v["price_per_night_paise"] for v in pool if v["price_per_night_paise"] is not None]
    times = [v["travel_time_to_venue_sec"] for v in pool]
    pmin, pmax = (min(prices), max(prices)) if prices else (0, 1)
    tmin, tmax = min(times), max(times)
    options = []
    for v in pool:
        free_share = v["rooms_available"] / max(v["rooms_total"], 1)
        f_avail = clamp(free_share / 0.30, 0.0, 1.0)
        if v["price_per_night_paise"] is None:
            f_price = 0.5   # unknown price: neutral, never assumed cheap or expensive
        elif max_price_paise:
            over = max(0, v["price_per_night_paise"] - max_price_paise)
            f_price = clamp(1.0 - over / max(max_price_paise, 1) * 2.0, 0.0, 1.0)
        else:
            f_price = 1.0 - (v["price_per_night_paise"] - pmin) / max(pmax - pmin, 1)
        f_travel = 1.0 - (v["travel_time_to_venue_sec"] - tmin) / max(tmax - tmin, 1)
        f_transport = clamp(1.0 - v["walk_to_transport_sec"] / 900.0, 0.0, 1.0)
        load = max(v["transport_utilisation"], v["transport_forecast_utilisation"] or 0.0)
        f_cong = 0.0 if v["transport_closed"] else clamp(1.0 - load / 1.2, 0.0, 1.0)
        if v["transport_closed"]:
            f_transport *= 0.3  # the nearest station is shut: a longer walk to the next one
        factors = {"availability": f_avail, "price": f_price, "travel": f_travel,
                   "transport": f_transport, "congestion": f_cong}
        score = sum(weights[k] * factors[k] for k in factors)
        if segment_id == "premium":
            score += 0.05 * {"luxury": 1.0, "upscale": 0.6}.get(v["tier"], 0.0)
        reasons = [
            f"{v['rooms_available']} rooms free ({int(round((1 - v['occupancy']) * 100))}% of rooms)",
            f"{_rupees(v['price_per_night_paise'])} per night"
            + ("" if not max_price_paise or v["price_per_night_paise"] is None else
               (" (within budget)" if v["price_per_night_paise"] <= max_price_paise else " (above budget)")),
            f"{_minutes(v['travel_time_to_venue_sec'])} min to {store.nodes[venue]['display_name']}",
            f"{_minutes(v['walk_to_transport_sec'])} min walk to {v['transport_name'] or 'transport'}"
            + (", which is currently closed" if v["transport_closed"]
               else f", currently at {int(round(v['transport_utilisation'] * 100))}% load"),
        ]
        options.append({
            "property": v,
            "score": round(score, 4),
            "factors": {k: round(x, 3) for k, x in factors.items()},
            "travel_time_sec": v["travel_time_to_venue_sec"],
            "reasons": reasons,
        })
    options.sort(key=lambda o: (-o["score"], o["property"]["property_id"]))
    options = options[: max(1, limit)]

    top = options[0]["property"]
    if current:
        explanation = (
            f"{top['name']} recommended instead of {current['name']}: {current['name']} is "
            f"{int(round(current['occupancy'] * 100))}% occupied ({current['status']}); {top['name']} is "
            f"{int(round(top['occupancy'] * 100))}% occupied with {top['rooms_available']} rooms free, "
            f"{_minutes(top['travel_time_to_venue_sec'])} min to the venue"
        )
        if current["transport_entity_id"] != top["transport_entity_id"]:
            cur_state = "closed" if current["transport_closed"] else f"{int(round(current['transport_utilisation'] * 100))}% load"
            explanation += (
                f", and uses {top['transport_name']} ({int(round(top['transport_utilisation'] * 100))}% load) "
                f"instead of {current['transport_name']} ({cur_state})"
            )
        explanation += "."
    else:
        explanation = (
            f"{top['name']} scores highest on availability, price, travel time, transport access "
            f"and congestion for this request."
        )
    if excluded_budget:
        explanation += f" {excluded_budget} properties were excluded as well above budget."
    return {
        "sim_time": store.sim_time,
        "destination_entity_id": venue,
        "options": options,
        "explanation": explanation,
        "current": current,
    }


def saturation(engine: Any, venue: str | None = None) -> dict[str, Any]:
    """Saturated properties, each with ranked, explained alternatives."""
    views = property_views(engine, venue)
    out = []
    for v in sorted((x for x in views if x["status"] == "saturated"), key=lambda x: -x["occupancy"]):
        rec = recommend(engine, destination_entity_id=venue, current_property_id=v["property_id"], limit=3)
        out.append({"property": v, "alternatives": rec["options"], "explanation": rec["explanation"]})
    return {"sim_time": engine.store.sim_time, "saturated": out}


def cluster_options(engine: Any) -> dict[str, dict[str, Any]]:
    """Per hotel cluster entity: free rooms, rooms in service, travel time to the main
    venue, load on its transport node and mean known price — what a rebalance needs."""
    out: dict[str, dict[str, Any]] = {}
    for v in property_views(engine):
        c = out.setdefault(v["cluster_entity_id"], {"free_rooms": 0, "rooms": 0, "travel_sec": [], "load": [],
                                                    "prices": []})
        c["free_rooms"] += v["rooms_available"]
        c["rooms"] += v["rooms_in_service"]
        if v["travel_time_to_venue_sec"] is not None:
            c["travel_sec"].append(v["travel_time_to_venue_sec"])
        c["load"].append(max(v["transport_utilisation"], v["transport_forecast_utilisation"] or 0.0))
        if v["price_per_night_paise"] is not None:
            c["prices"].append(v["price_per_night_paise"])
    for c in out.values():
        c["travel_sec"] = min(c["travel_sec"]) if c["travel_sec"] else None
        c["load"] = max(c["load"]) if c["load"] else 0.0
        c["price_paise"] = int(sum(c["prices"]) / len(c["prices"])) if c["prices"] else None
        del c["prices"]
    return out


def cluster_availability(engine: Any) -> dict[str, int]:
    """Free rooms per hotel cluster entity — context for the optimiser."""
    with engine.world_lock:
        props = engine.generator.properties_state()
    out: dict[str, int] = {}
    for p in props:
        out[p["cluster_entity_id"]] = out.get(p["cluster_entity_id"], 0) + p["rooms_available"]
    return out
