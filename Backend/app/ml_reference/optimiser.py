"""InterventionOptimiser (03_ML_CONTRACT.md §6).

Rule-based candidate templates, deliberately not learned: RL would need
interaction data we do not have and would produce policies we cannot explain,
which is unacceptable under a human-approval gate (§6.2).

The ranking is where the certificate earns its keep. `stability_factor` maps
UNSTABLE to 0.15, so a 34%-relief unstable option scores below a 31%-relief
stable one. That arithmetic *is* the demo (§6.3).
"""
from __future__ import annotations

import logging
import math
from typing import Any

from .common import clamp, stable_unit

log = logging.getLogger("eventflow.ml.optimiser")

STABILITY_FACTOR = {"STABLE": 1.0, "CONDITIONAL": 0.6, "UNSTABLE": 0.15}
MAX_COST_PAISE = 500_000


class InterventionOptimiser:
    def __init__(self, config: dict) -> None:
        self.config = config or {}
        self.max_candidates = int(self.config.get("max_candidates", 5))
        self.min_feasibility = float(self.config.get("min_feasibility", 0.3))
        self.seed = int(self.config.get("seed", 42))
        self.ttl_sec = int(self.config.get("intervention_ttl_sec", 900))

    def ready(self) -> bool:
        return True

    # --- 03 §6.1 ---------------------------------------------------------------
    def generate(self, risk_context: dict, max_candidates: int | None = None) -> list[dict]:
        try:
            return self._generate(risk_context, max_candidates or self.max_candidates)
        except Exception:
            log.exception("candidate generation failed")
            return self.fallback(risk_context, max_candidates or self.max_candidates)

    def fallback(self, risk_context: dict, max_candidates: int = 1) -> list[dict]:
        root = risk_context.get("root_entity_id", "unknown")
        return [
            self._make(
                risk_context, "notify_only", [root],
                title="Notify operations team",
                description=f"Escalate {root} to the operations desk for manual assessment.",
                relief=4.0, cost_paise=0, delay_sec=0, feasibility=1.0,
            )
        ]

    # --- §6.2 candidate templates -----------------------------------------------
    # Each candidate carries `_action`: the concrete, executable parameters the
    # simulator applies on approval (source -> destination and the share of
    # visitors asked to move; gates to stagger; service capacity to add). The
    # text an operator reads is generated from the same parameters, so the card
    # always describes exactly what approving it does.
    def _generate(self, risk_context: dict, max_candidates: int) -> list[dict]:
        root = risk_context.get("root_entity_id")
        node_state: dict[str, dict] = risk_context.get("node_state", {})
        edges: list[dict] = risk_context.get("edges", [])
        closed = set(risk_context.get("closed") or [])
        root_state = node_state.get(root, {})
        root_type = root_state.get("entity_type", "zone")

        def util(e: str) -> float:
            st = node_state.get(e, {})
            return max(float(st.get("utilisation", 0.0)), float(st.get("forecast_1800") or 0.0))

        def outs(e: str, etype: str | None = None) -> list[dict]:
            return [x for x in edges if x["src_entity_id"] == e and (etype is None or x["edge_type"] == etype)]

        def ins(e: str, etype: str | None = None) -> list[dict]:
            return [x for x in edges if x["dst_entity_id"] == e and (etype is None or x["edge_type"] == etype)]

        def of_type(e: str) -> str:
            return node_state.get(e, {}).get("entity_type", "")

        venue_gates: dict[str, list[str]] = risk_context.get("venue_gates") or {}
        candidates: list[dict] = []

        # Which gates and stations does this problem ultimately sit behind?
        gates: list[str] = []
        stations: list[str] = []
        if root_type == "gate":
            gates = [root]
        elif root_type == "transport_node":
            stations = [root]
            gates = [x["dst_entity_id"] for x in outs(root) if of_type(x["dst_entity_id"]) == "gate"]
        elif root_type == "transport_route":
            stations = sorted((x["dst_entity_id"] for x in outs(root, "feeds")), key=lambda e: -util(e))[:1]
        elif root_type == "road":
            gates = [x["src_entity_id"] for x in ins(root, "adjacent_to") if of_type(x["src_entity_id"]) == "gate"]
        elif root_type == "emergency_facility":
            roads = [x["src_entity_id"] for x in ins(root, "evacuates_to") if of_type(x["src_entity_id"]) == "road"]
            for r in roads:
                gates += [x["src_entity_id"] for x in ins(r, "adjacent_to") if of_type(x["src_entity_id"]) == "gate"]
        elif root_type == "venue":
            gates = sorted(venue_gates.get(root, []), key=lambda g: -util(g))[:2]
        elif root_type == "parking":
            gates = [x["dst_entity_id"] for x in outs(root, "serves") if of_type(x["dst_entity_id"]) == "gate"]
        gates = sorted((g for g in dict.fromkeys(gates) if g not in closed), key=lambda g: -util(g))
        if not stations and gates:
            # The busiest open station feeding the hottest gate.
            feeders = [x["src_entity_id"] for g in gates for x in ins(g, "feeds") if of_type(x["src_entity_id"]) == "transport_node"]
            stations = sorted(dict.fromkeys(feeders), key=lambda e: -util(e))[:1]
        stations = [st for st in stations if st not in closed]

        # reroute_transport: move riders from a pressured station to its best substitute.
        for st in stations:
            subs = [x for x in outs(st, "substitutes_for") if x["dst_entity_id"] not in closed]
            if not subs:
                continue
            best = min(subs, key=lambda x: (util(x["dst_entity_id"]) - 0.3 * x.get("substitutability", 0.0)))
            dst = best["dst_entity_id"]
            frac = round(clamp(0.2 + 0.3 * float(best.get("substitutability", 0.5)), 0.15, 0.5), 2)
            candidates.append(self._make(
                risk_context, "reroute_transport", [st, dst],
                title=f"Redirect {frac:.0%} of riders from {self._name(st, node_state)} to {self._name(dst, node_state)}",
                description=(
                    f"Platform signage and app alerts ask {frac:.0%} of inbound riders to use "
                    f"{self._name(dst, node_state)} (currently {util(dst):.0%} loaded) instead of "
                    f"{self._name(st, node_state)} ({util(st):.0%})."
                ),
                relief=12.0, cost_paise=120_000, delay_sec=480,
                feasibility=clamp(0.55 + 0.4 * float(best.get("substitutability", 0.5)), 0.0, 0.98),
                action={"source": st, "destination": dst, "fraction": frac},
            ))

        # transport_redistribution: spread a pressured station's riders over every
        # open alternative (other stations, bus hubs), weighted by spare capacity.
        for st in stations:
            subs = [x for x in outs(st, "substitutes_for") if x["dst_entity_id"] not in closed]
            spare = [(x["dst_entity_id"], max(0.0, 0.95 - util(x["dst_entity_id"]))
                      * float(node_state.get(x["dst_entity_id"], {}).get("nominal_capacity", 1000.0)))
                     for x in subs]
            spare = [(d, s) for d, s in spare if s > 0]
            if len(spare) < 2:
                continue
            spare.sort(key=lambda x: -x[1])
            spare = spare[:3]
            frac = 0.4
            names = ", ".join(self._name(d, node_state) for d, _ in spare)
            candidates.append(self._make(
                risk_context, "transport_redistribution", [st] + [d for d, _ in spare],
                title=f"Spread {frac:.0%} of {self._name(st, node_state)} riders across {len(spare)} alternatives",
                description=(
                    f"Journey-planner and platform announcements split {frac:.0%} of inbound riders for "
                    f"{self._name(st, node_state)} ({util(st):.0%}) over {names}, in proportion to their spare capacity."
                ),
                relief=14.0, cost_paise=150_000, delay_sec=540,
                feasibility=0.8,
                action={"source": st, "destinations": [[d, round(s, 1)] for d, s in spare], "fraction": frac},
            ))

        # deploy_shuttle: extra clearance capacity at the pressured station.
        for st in stations:
            overflow = max(0.0, util(st) - 0.8) * float(node_state.get(st, {}).get("nominal_capacity", 1000.0))
            count = int(clamp(math.ceil(overflow / 250.0), 2, 8))
            candidates.append(self._make(
                risk_context, "deploy_shuttle", [st],
                title=f"Deploy {count} shuttles at {self._name(st, node_state)}",
                description=(
                    f"Run {count} extra shuttles from {self._name(st, node_state)} at 5-minute headway, "
                    f"adding about {count * 12} passengers per minute of clearance capacity."
                ),
                relief=8.0, cost_paise=45_000 * count, delay_sec=300, feasibility=0.88,
                action={"node": st, "capacity_per_min": float(count * 12)},
            ))

        # gate_redistribution: part of a hot gate's demand to the coolest gate of its venue.
        for g in gates[:1]:
            venue = next((v for v, gs in venue_gates.items() if g in gs), None)
            others = [x for x in venue_gates.get(venue, []) if x != g and x not in closed]
            if not others:
                continue
            cool = min(others, key=util)
            if util(cool) >= util(g):
                continue
            frac = 0.3
            candidates.append(self._make(
                risk_context, "gate_redistribution", [g, cool],
                title=f"Reassign {frac:.0%} of {self._name(g, node_state)} entries to {self._name(cool, node_state)}",
                description=(
                    f"Stewards and app alerts direct {frac:.0%} of arrivals heading for "
                    f"{self._name(g, node_state)} ({util(g):.0%}) to {self._name(cool, node_state)} ({util(cool):.0%})."
                ),
                relief=15.0, cost_paise=25_000, delay_sec=240, feasibility=0.9,
                action={"sources": [g], "destination": cool, "fraction": frac},
            ))

        # stagger_entry: hold back part of the arrival wave at the pressured gates.
        if gates:
            targets = gates[:2]
            candidates.append(self._make(
                risk_context, "stagger_entry", targets,
                title=f"Stagger entry at {', '.join(self._name(g, node_state) for g in targets)}",
                description=(
                    "Ask 35% of arrivals for these gates (group and price-sensitive ticket holders first) to "
                    "arrive 12 minutes later, with a food-and-drink credit for doing so."
                ),
                relief=12.0, cost_paise=180_000, delay_sec=720, feasibility=0.85,
                action={"gates": targets, "fraction": 0.35, "delay_min": 12.0},
            ))

        # parking_redistribution: a filling lot and its substitute.
        lots = [root] if root_type == "parking" else [
            e for e, st in node_state.items() if st.get("entity_type") == "parking" and util(e) > 0.85
        ]
        for lot in lots[:1]:
            alts = [x["dst_entity_id"] for x in outs(lot, "substitutes_for") if x["dst_entity_id"] not in closed]
            if not alts:
                continue
            alt = min(alts, key=util)
            candidates.append(self._make(
                risk_context, "parking_redistribution", [lot, alt],
                title=f"Divert arriving cars from {self._name(lot, node_state)} to {self._name(alt, node_state)}",
                description=(
                    f"Variable message signs send 50% of cars heading for {self._name(lot, node_state)} "
                    f"({util(lot):.0%} full) to {self._name(alt, node_state)} ({util(alt):.0%})."
                ),
                relief=10.0, cost_paise=30_000, delay_sec=480, feasibility=0.82,
                action={"source": lot, "destination": alt, "fraction": 0.5},
            ))

        # zone_incentive: a crowded zone and a quieter neighbour or substitute.
        if root_type == "zone":
            subs = [x["dst_entity_id"] for x in outs(root, "substitutes_for")]
            subs += [x["dst_entity_id"] for x in outs(root, "adjacent_to") if of_type(x["dst_entity_id"]) == "zone"]
            subs = [z for z in dict.fromkeys(subs) if z not in closed]
            if subs:
                cold = min(subs, key=util)
                candidates.append(self._make(
                    risk_context, "zone_incentive", [root, cold],
                    title=f"Rs 500 credit to move crowds to {self._name(cold, node_state)}",
                    description=(
                        f"Offer a Rs 500 food-and-drink credit to visitors who move from "
                        f"{self._name(root, node_state)} ({util(root):.0%}) to {self._name(cold, node_state)} ({util(cold):.0%})."
                    ),
                    relief=12.0, cost_paise=250_000, delay_sec=600, feasibility=0.7,
                    action={"source": root, "destination": cold, "fraction": 0.35},
                ))

        # accommodation_rebalance: a saturated hotel cluster and the cluster with most free rooms.
        availability: dict[str, int] = risk_context.get("hotel_availability") or {}
        options: dict[str, dict] = risk_context.get("hotel_options") or {}
        if root_type == "hotel":
            def rank(c: str, free: int) -> tuple:
                # Free rooms are the hard constraint (> 20); then closer to the venue, a less
                # loaded station, and (only when known) cheaper. Deterministic tie-break on id.
                o = options.get(c) or {}
                travel = o.get("travel_sec") if o.get("travel_sec") is not None else 3600
                price = o.get("price_paise")
                return (-(min(free, 400) / 400.0) + travel / 3600.0 + max(0.0, (o.get("load") or 0.0) - 0.7)
                        + (0.0 if price is None else price / 2_000_000.0), c)
            alts = sorted(((c, n) for c, n in availability.items() if c != root and n > 20), key=lambda x: rank(*x))
            if alts:
                dst, free = alts[0]
                candidates.append(self._make(
                    risk_context, "accommodation_rebalance", [root, dst],
                    title=f"Offer transfers from {self._name(root, node_state)} to {self._name(dst, node_state)}",
                    description=(
                        f"Offer event guests booked in {self._name(root, node_state)} ({util(root):.0%} occupied) a free "
                        f"transfer and credit to {self._name(dst, node_state)}, which has {free} rooms free."
                    ),
                    relief=10.0, cost_paise=320_000, delay_sec=1200, feasibility=0.6,
                    action={"source": root, "destination": dst, "fraction": 0.25},
                ))

        # emergency_corridor: an emergency post downstream of crowded roads.
        cascade_ids = [s["entity_id"] for s in (risk_context.get("cascade") or {}).get("steps", [])]
        emergency = [root] if root_type == "emergency_facility" else [
            e for e in cascade_ids if node_state.get(e, {}).get("entity_type") == "emergency_facility"
        ]
        for ef in emergency[:1]:
            roads = [x["src_entity_id"] for x in ins(ef, "evacuates_to") if of_type(x["src_entity_id"]) == "road"][:2]
            if not roads:
                continue
            candidates.append(self._make(
                risk_context, "emergency_corridor", roads + [ef],
                title=f"Reserve emergency corridor to {self._name(ef, node_state)}",
                description=(
                    "Clear one lane on " + ", ".join(self._name(r, node_state) for r in roads)
                    + f" so ambulances reach {self._name(ef, node_state)} regardless of crowding."
                ),
                relief=8.0, cost_paise=60_000, delay_sec=300, feasibility=0.92,
                action={"roads": roads},
            ))

        # event_delay: push back the start of an event whose arrival wave is behind
        # this problem, while most of its crowd has not yet set off.
        venues = {root} if root_type in ("venue", "zone") else set()
        for g in gates:
            venues |= {v for v, gs in venue_gates.items() if g in gs}
        for ev in risk_context.get("events") or []:
            if ev.get("venue") not in venues or ev.get("cancelled"):
                continue
            if not (25.0 <= float(ev.get("minutes_to_start", -1)) <= 150.0) or float(ev.get("arrived_share", 1.0)) > 0.6:
                continue
            delay = 20
            candidates.append(self._make(
                risk_context, "event_delay", [ev["venue"]],
                title=f"Delay {ev.get('name', ev['event_id'])} by {delay} minutes",
                description=(
                    f"Announce a {delay}-minute later start for {ev.get('name', ev['event_id'])} "
                    f"({float(ev.get('arrived_share', 0.0)):.0%} of visitors have arrived). The arrival "
                    f"wave flattens and the egress moves with it; every visitor gets the new time."
                ),
                relief=15.0, cost_paise=400_000, delay_sec=delay * 60, feasibility=0.75,
                action={"event_id": ev["event_id"], "delay_min": float(delay)},
            ))
            break

        # notify_only: the baseline when no physical action applies (it moves nobody).
        if not candidates:
            candidates.extend(self.fallback(risk_context, 1))

        # §6.3: feasibility below the floor gates the candidate out entirely.
        candidates = [c for c in candidates if c["feasibility"] >= self.min_feasibility]
        return candidates[:max_candidates]

    # --- §6.3 ranking --------------------------------------------------------------
    def rank(self, interventions: list[dict]) -> list[dict]:
        """Attach rank_score and sort descending. The backend never re-sorts after this."""
        for i in interventions:
            verdict = (i.get("certificate") or {}).get("verdict")
            stability = STABILITY_FACTOR.get(verdict, 0.6)  # uncertified sits between

            relief_norm = max(0.0, float(i.get("estimated_relief_pct", 0.0))) / 100.0
            # The simulated evaluation is the physical check: an action that pushes
            # another entity over the critical line is worth less, whatever the
            # certificate says.
            if (i.get("evaluation") or {}).get("new_critical_entities"):
                stability *= 0.5
            cost_norm = 0.5 + 0.5 * (float(i.get("estimated_cost_paise", 0)) / MAX_COST_PAISE)
            delay_norm = 0.5 + 0.5 * (float(i.get("estimated_delay_sec", 0)) / 1800.0)

            i["rank_score"] = round(
                clamp((relief_norm * stability) / max(cost_norm * delay_norm, 0.05), 0.0, 1.0), 4
            )
        interventions.sort(key=lambda i: -i["rank_score"])
        return interventions

    # --- helpers -----------------------------------------------------------------
    def _overflow(self, state: dict) -> float:
        cap = float(state.get("nominal_capacity", 1000.0)) or 1000.0
        util = float(state.get("forecast_1800") or state.get("utilisation", 0.0))
        return max(200.0, (util - 0.9) * cap if util > 0.9 else 0.08 * cap)

    def _name(self, entity_id: str, node_state: dict[str, dict]) -> str:
        return node_state.get(entity_id, {}).get("display_name") or entity_id

    def _make(
        self,
        risk_context: dict,
        intervention_type: str,
        targets: list[str],
        *,
        title: str,
        description: str,
        relief: float,
        cost_paise: int,
        delay_sec: int,
        feasibility: float,
        action: dict | None = None,
    ) -> dict:
        root = risk_context.get("root_entity_id")
        sim_time = risk_context.get("sim_time")
        key = f"{root}|{intervention_type}|{'.'.join(targets)}"
        iid = "int_" + f"{int(stable_unit(self.seed, 'int', key, sim_time or '') * 0xFFFFF):05x}"
        return {
            "intervention_id": iid,
            "intervention_type": intervention_type,
            "status": "proposed",
            "target_entity_ids": targets,
            "triggered_by_entity_id": root,
            "title": title,
            "description": description,
            "estimated_relief_pct": round(float(relief), 1),
            "estimated_cost_paise": int(cost_paise),
            "estimated_delay_sec": int(delay_sec),
            "feasibility": round(float(feasibility), 2),
            "rank_score": 0.0,          # set by rank(), after certification
            "certificate": None,
            "created_at": sim_time,
            "expires_at": None,          # backend stamps this from sim_time + ttl
            "_ttl_sec": self.ttl_sec,
            "_action": dict(action or {}),
        }
