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
    def _generate(self, risk_context: dict, max_candidates: int) -> list[dict]:
        root = risk_context.get("root_entity_id")
        node_state: dict[str, dict] = risk_context.get("node_state", {})
        edges: list[dict] = risk_context.get("edges", [])
        cascade: dict = risk_context.get("cascade") or {}
        root_state = node_state.get(root, {})
        root_type = root_state.get("entity_type", "zone")
        overflow = self._overflow(root_state)

        out_edges = [e for e in edges if e["src_entity_id"] == root]
        subs = [e for e in out_edges if e["edge_type"] == "substitutes_for"]
        last_mile = [e for e in out_edges if e["edge_type"] == "last_mile_to"]
        cascade_ids = [s["entity_id"] for s in cascade.get("steps", [])]

        candidates: list[dict] = []

        # reroute_transport — root is a transport node/route with a substitute.
        if root_type in ("transport_node", "transport_route") and subs:
            best = max(subs, key=lambda e: e.get("substitutability", 0.0))
            dst = best["dst_entity_id"]
            share = float(best.get("substitutability", 0.5))
            people = int(overflow)
            candidates.append(
                self._make(
                    risk_context, "reroute_transport", [root, dst],
                    title=f"Redirect {people:,} attendees to {self._name(dst, node_state)}",
                    description=(
                        f"Divert {share:.0%} of inbound flow from {self._name(root, node_state)} "
                        f"to {self._name(dst, node_state)} via platform signage and app alerts."
                    ),
                    relief=clamp(18.0 + share * 24.0, 5.0, 45.0),
                    cost_paise=120_000, delay_sec=540,
                    feasibility=clamp(0.55 + share * 0.4, 0.0, 0.98),
                )
            )

        # deploy_shuttle — a congested last-mile link.
        if last_mile:
            count = max(1, math.ceil(overflow / 400.0))
            dst = last_mile[0]["dst_entity_id"]
            candidates.append(
                self._make(
                    risk_context, "deploy_shuttle", [root, dst],
                    title=f"Deploy {count} shuttle{'s' if count > 1 else ''} on the {self._name(root, node_state)} link",
                    description=(
                        f"Run {count} additional shuttle{'s' if count > 1 else ''} between "
                        f"{self._name(root, node_state)} and {self._name(dst, node_state)} at 6-minute headway."
                    ),
                    relief=clamp(6.0 + count * 4.5, 5.0, 32.0),
                    cost_paise=45_000 * count, delay_sec=300,
                    feasibility=0.88,
                )
            )

        # stagger_entry — root is a gate, or feeds one.
        gates = [e["dst_entity_id"] for e in out_edges
                 if node_state.get(e["dst_entity_id"], {}).get("entity_type") == "gate"]
        if root_type == "gate" or gates:
            targets = gates[:2] or [root]
            candidates.append(
                self._make(
                    risk_context, "stagger_entry", targets,
                    title="Stagger entry by segment + activate zone incentive",
                    description=(
                        "Delay group and price-sensitive segments by 12 minutes at "
                        + ", ".join(self._name(g, node_state) for g in targets)
                        + "; activate a Rs 500 North-zone credit to pull demand forward."
                    ),
                    relief=clamp(22.0 + len(targets) * 4.0, 8.0, 38.0),
                    cost_paise=180_000, delay_sec=720,
                    feasibility=0.85,
                )
            )

        # gate_redistribution — >=2 gates serve the same venue.
        venue_gates = sorted(
            {e["src_entity_id"] for e in edges
             if e["edge_type"] == "feeds"
             and node_state.get(e["src_entity_id"], {}).get("entity_type") == "gate"
             and node_state.get(e["dst_entity_id"], {}).get("entity_type") == "venue"}
        )
        if len(venue_gates) >= 2:
            hot = [g for g in venue_gates if float(node_state.get(g, {}).get("utilisation", 0)) > 0.6][:2]
            cool = [g for g in venue_gates if float(node_state.get(g, {}).get("utilisation", 0)) <= 0.6][:1]
            if hot and cool:
                candidates.append(
                    self._make(
                        risk_context, "gate_redistribution", hot + cool,
                        title=f"Reassign entry share to {self._name(cool[0], node_state)}",
                        description=(
                            f"Move 20% of ticket-scan share from "
                            f"{', '.join(self._name(g, node_state) for g in hot)} to "
                            f"{self._name(cool[0], node_state)}."
                        ),
                        relief=16.5, cost_paise=25_000, delay_sec=240, feasibility=0.9,
                    )
                )

        # parking_redistribution
        hot_parking = [
            e for e, s in node_state.items()
            if s.get("entity_type") == "parking" and float(s.get("utilisation", 0)) > 0.85
        ]
        if hot_parking:
            src = hot_parking[0]
            alt = next(
                (e["dst_entity_id"] for e in edges
                 if e["src_entity_id"] == src and e["edge_type"] == "substitutes_for"), None
            )
            if alt:
                candidates.append(
                    self._make(
                        risk_context, "parking_redistribution", [src, alt],
                        title=f"Divert arrivals from {self._name(src, node_state)} to {self._name(alt, node_state)}",
                        description=(
                            f"Close {self._name(src, node_state)} to new arrivals and route them to "
                            f"{self._name(alt, node_state)} with free transfer."
                        ),
                        relief=11.0, cost_paise=30_000, delay_sec=480, feasibility=0.82,
                    )
                )

        # zone_incentive — one zone under 0.5 while another is over 0.85.
        zones = {e: s for e, s in node_state.items() if s.get("entity_type") == "zone"}
        cold = [e for e, s in zones.items() if float(s.get("utilisation", 0)) < 0.5]
        hot_zone = [e for e, s in zones.items() if float(s.get("utilisation", 0)) > 0.85]
        if cold and hot_zone:
            candidates.append(
                self._make(
                    risk_context, "zone_incentive", [hot_zone[0], cold[0]],
                    title=f"Rs 500 credit to move demand to {self._name(cold[0], node_state)}",
                    description=(
                        f"Offer a Rs 500 F&B credit and priority shuttle access for attendees who "
                        f"relocate from {self._name(hot_zone[0], node_state)} to {self._name(cold[0], node_state)}."
                    ),
                    relief=14.0, cost_paise=250_000, delay_sec=900, feasibility=0.7,
                )
            )

        # accommodation_rebalance
        hot_hotel = [
            e for e, s in node_state.items()
            if s.get("entity_type") == "hotel" and float(s.get("utilisation", 0)) > 0.88
        ]
        if hot_hotel:
            src = hot_hotel[0]
            alt = next(
                (e["dst_entity_id"] for e in edges
                 if e["src_entity_id"] == src and e["edge_type"] == "substitutes_for"), None
            )
            if alt:
                candidates.append(
                    self._make(
                        risk_context, "accommodation_rebalance", [src, alt],
                        title=f"Rebalance stays toward {self._name(alt, node_state)}",
                        description=(
                            f"Offer transfer credit for guests moving from {self._name(src, node_state)} "
                            f"to {self._name(alt, node_state)}."
                        ),
                        relief=9.0, cost_paise=320_000, delay_sec=1200, feasibility=0.55,
                    )
                )

        # emergency_corridor — an emergency facility appears in the cascade.
        emergency_hit = [
            e for e in cascade_ids
            if node_state.get(e, {}).get("entity_type") == "emergency_facility"
        ]
        if emergency_hit:
            roads = [e for e in cascade_ids if node_state.get(e, {}).get("entity_type") == "road"][:2]
            candidates.append(
                self._make(
                    risk_context, "emergency_corridor", roads + emergency_hit[:1],
                    title=f"Reserve emergency corridor to {self._name(emergency_hit[0], node_state)}",
                    description=(
                        "Clear one lane on "
                        + ", ".join(self._name(r, node_state) for r in roads)
                        + f" to guarantee access to {self._name(emergency_hit[0], node_state)}."
                    ),
                    relief=7.5, cost_paise=60_000, delay_sec=300, feasibility=0.92,
                )
            )

        # notify_only — always present as the baseline candidate.
        candidates.extend(self.fallback(risk_context, 1))

        # §6.3 — feasibility below the floor gates the candidate out entirely.
        candidates = [c for c in candidates if c["feasibility"] >= self.min_feasibility]
        return candidates[:max_candidates]

    # --- §6.3 ranking --------------------------------------------------------------
    def rank(self, interventions: list[dict]) -> list[dict]:
        """Attach rank_score and sort descending. The backend never re-sorts after this."""
        for i in interventions:
            verdict = (i.get("certificate") or {}).get("verdict")
            stability = STABILITY_FACTOR.get(verdict, 0.6)  # uncertified sits between

            relief_norm = float(i.get("estimated_relief_pct", 0.0)) / 100.0
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
        }
