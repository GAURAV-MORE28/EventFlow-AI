"""In-process live state — the authority every read path serves from.

Postgres is the durable record and Redis is the shared cache, but the 30-second
cycle needs a single coherent snapshot it can mutate and broadcast without a
round trip, and that is this object. Reads are served from here (mirrored into
the cache); writes happen only inside the cycle or an operator action.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any, Iterable

from ..simtime import parse, shift
from ..catalog import verify_catalogue
from ..topology import verify, verify_structure

HISTORY_LIMIT = 240          # ~2 hours of sim at 30s steps
TWIN_HISTORY_LIMIT = 20      # 00 §2.8 — last 20 cycles, oldest first
ANOMALY_LIMIT = 50


class StateStore:
    def __init__(self, sim_start: str, provider: Any | None = None, demo_checks: bool = True) -> None:
        """`provider` defaults to the configured data provider; a caller that
        simulates other maps (the cascade dataset generator) injects its own and
        turns off the demo-only checks (`demo_checks=False`)."""
        self._lock = threading.RLock()

        from ..providers.data import get_data_provider

        provider = provider or get_data_provider()
        topology = provider.topology()
        (verify if demo_checks else verify_structure)(topology)
        self.nodes: dict[str, dict] = {n["entity_id"]: n for n in topology["nodes"]}
        self.edges: list[dict] = topology["edges"]
        self.segments: list[dict] = topology["segments"]
        self.bounds: dict[str, float] = topology["bounds"]
        self.properties: list[dict] = provider.properties(topology["nodes"], topology["edges"])
        verify_catalogue(self.properties, topology["nodes"])

        self.edges_by_src: dict[str, list[dict]] = {}
        self.edges_by_dst: dict[str, list[dict]] = {}
        for e in self.edges:
            self.edges_by_src.setdefault(e["src_entity_id"], []).append(e)
            self.edges_by_dst.setdefault(e["dst_entity_id"], []).append(e)

        self.sim_start = sim_start
        self.sim_time = sim_start
        self.cycle_number = 0

        self.entity_states: dict[str, dict] = {}
        self.history: dict[str, deque[float]] = {
            eid: deque(maxlen=HISTORY_LIMIT) for eid in self.nodes
        }
        self.residuals: dict[str, deque[float]] = {
            eid: deque(maxlen=HISTORY_LIMIT) for eid in self.nodes
        }
        self.risk_breakdown: dict[str, list[dict]] = {}

        # Snapshots of every forecast ever made, keyed by the cycle it was made
        # at. Validating a forecast means comparing its N-second-ahead point to
        # the actual observed N seconds later — never to the observation one
        # cycle later, which is a different, much shorter horizon and makes any
        # multi-step forecast look catastrophically wrong for no real reason.
        self.forecast_snapshots: dict[str, deque[tuple[int, float, float]]] = {
            eid: deque(maxlen=130) for eid in self.nodes
        }

        self.forecasts: dict[str, dict] = {}
        self.active_forecast_source = "persistence"
        self.pressure_timeline: list[dict] = []

        self.cascades: dict[str, dict] = {}
        self.active_cascade_source = "deterministic"
        # The cascade model's latest per-entity failure probabilities and the
        # mode they were produced under (`cascade.gnn_mode`); None when off.
        # In shadow mode nothing published or decided reads this.
        self.cascade_ml: dict[str, Any] | None = None
        # H4 fix: a root that was already an active cascade last cycle does not
        # re-fire `cascade_alert` just because the prediction refreshed — only a
        # root that newly appears does. Tracked separately from `self.cascades`
        # (which is replaced wholesale every cycle) so the diff survives it.
        self.previously_active_cascade_roots: set[str] = set()

        # Online precision/recall/lead time (03 §4.4) of the published cascade
        # and of the cascade model, one definition for both (prediction_eval.py),
        # measured against what the simulated city actually does.
        from .prediction_eval import OnlineEvaluator

        self.online_eval = OnlineEvaluator(horizon_sec=3600)

        self.interventions: dict[str, dict] = {}
        self.certificates: dict[str, dict] = {}

        self.twin_fidelity: dict[str, Any] | None = None
        self.twin_history: deque[dict] = deque(maxlen=TWIN_HISTORY_LIMIT)
        self.drift_mode_enabled = False

        self.regret_entries: list[dict] = []
        # Per-entity "what the twin's do-nothing branch predicted" at the last
        # settled intervention that targeted it — metrics.py's decision-panel
        # reductions compare *this* against the real current value, so they are
        # a genuine counterfactual instead of a before/after-the-event-ramped-up
        # temporal diff (which is what `load_variance_reduction_pct` used to be).
        self.counterfactual_utilisation: dict[str, float] = {}
        self.anomalies: deque[dict] = deque(maxlen=ANOMALY_LIMIT)
        self.nudges: dict[str, dict] = {}

        self.summary: dict[str, Any] = {
            "overall_risk_score": 0,
            "overall_risk_band": "low",
            "critical_count": 0,
            "high_count": 0,
            "load_variance": 0.0,
        }

        # Rolling telemetry the /metrics endpoint reports over.
        self.cycle_latency_ms: deque[float] = deque(maxlen=40)
        self.forecast_errors: dict[str, list[float]] = {"model": [], "persistence": []}
        self.commander_calls = 0
        self.commander_ungrounded = 0
        self.commander_tool_calls_ok = 0
        self.commander_tool_calls_total = 0
        self.unstable_caught = 0
        # 03 §5.6 "certificate accuracy": whether the certificate's predicted
        # equilibrium agreed (within 15%) with an independent twin.branch()
        # rollout of the same scenario — populated in
        # Engine._maybe_generate_interventions, read in metrics.build_metrics.
        self.certificates_scored: list[bool] = []
        # nudge answers, oldest first: {segment_id, accepted} (services/compliance.py)
        self.observed_compliance: list[dict] = []

        # Consecutive cycles each entity has spent at/above the critical line
        # (feeds the risk scorer's persistence escalation).
        self.cycles_over_critical: dict[str, int] = {}
        # Registered attendee plans (journey requests) — nudges target the
        # attendees whose routes actually pass through an intervention's source.
        self.attendees: dict[str, dict] = {}
        # Root -> cycle a proposal for it last lapsed; avoids re-proposing the
        # same thing every cycle when nobody acted on it.
        self.root_cooldown: dict[str, int] = {}
        # Settled interventions: realised effect vs the do-nothing world.
        self.settlements: list[dict] = []
        self.twin_layers: dict[str, dict] = {}
        self.band_hold: dict[str, int] = {}
        self.cascade_signature: tuple = ()
        # Live disruptions: disruption_id -> record.
        self.disruptions: dict[str, dict] = {}
        self.operations: dict = {}

    # --- helpers ------------------------------------------------------------
    def lock(self) -> threading.RLock:
        return self._lock

    def zone_ids(self) -> list[str]:
        return [e for e, n in self.nodes.items() if n["entity_type"] == "zone"]

    def capacities(self) -> dict[str, float]:
        return {e: float(n["nominal_capacity"]) for e, n in self.nodes.items()}

    def series(self) -> dict[str, list[float]]:
        return {e: list(h) for e, h in self.history.items()}

    def node_state_for_ml(self) -> dict[str, dict]:
        """The plain-dict view ML modules receive. No ORM objects, no sessions."""
        out: dict[str, dict] = {}
        for eid, node in self.nodes.items():
            st = self.entity_states.get(eid, {})
            fc = self.forecasts.get(eid) or {}
            points = {p["horizon_sec"]: p["predicted_utilisation"] for p in fc.get("points", [])}
            out[eid] = {
                "entity_id": eid,
                "entity_type": node["entity_type"],
                "display_name": node["display_name"],
                "nominal_capacity": float(node["nominal_capacity"]),
                "utilisation": float(st.get("utilisation", 0.0)),
                "current_count": float(st.get("current_count", 0.0)),
                "flow_rate_per_min": float(st.get("flow_rate_per_min", 0.0)),
                "risk_score": int(st.get("risk_score", 0)),
                "risk_band": st.get("risk_band", "low"),
                "is_observed": bool(st.get("is_observed", False)),
                "forecast_900": points.get(900),
                "forecast_1800": points.get(1800),
                "forecast_3600": points.get(3600),
                "time_to_critical_sec": fc.get("time_to_critical_sec"),
                "cycles_over_critical": int(self.cycles_over_critical.get(eid, 0)),
                "degree": len(self.edges_by_src.get(eid, [])) + len(self.edges_by_dst.get(eid, [])),
            }
        return out

    def entity_states_list(self) -> list[dict]:
        return list(self.entity_states.values())

    def changed_entities(self, previous: dict[str, dict]) -> list[dict]:
        """01 §4.1 — `state_update` carries only entities whose state actually moved."""
        out = []
        for eid, st in self.entity_states.items():
            old = previous.get(eid)
            if old is None:
                out.append(st)
                continue
            if (
                abs(old["utilisation"] - st["utilisation"]) > 1e-4
                or old["risk_band"] != st["risk_band"]
                or old["risk_score"] != st["risk_score"]
                or old["is_observed"] != st["is_observed"]
                or abs((old.get("queue_people") or 0.0) - (st.get("queue_people") or 0.0)) > 0.5
                or abs((old.get("inflow_per_min") or 0.0) - (st.get("inflow_per_min") or 0.0)) > 0.5
            ):
                out.append(st)
        return out

    def snapshot_states(self) -> dict[str, dict]:
        return {eid: dict(st) for eid, st in self.entity_states.items()}

    def interventions_by_status(self, status: str | None, limit: int = 10) -> list[dict]:
        """Already sorted by rank_score desc. Callers must not re-sort (00 rule 2)."""
        items = [i for i in self.interventions.values() if status is None or i["status"] == status]
        items.sort(key=lambda i: -float(i.get("rank_score", 0.0)))
        return items[:limit]

    def expire_interventions(self) -> list[dict]:
        now = parse(self.sim_time)
        expired = []
        for i in self.interventions.values():
            if i["status"] == "proposed" and parse(i["expires_at"]) <= now:
                i["status"] = "expired"
                expired.append(i)
        return expired

    def advance(self, dt_sec: int) -> None:
        self.sim_time = shift(self.sim_time, dt_sec)
        self.cycle_number += 1

    def reset_clock(self, sim_time: str) -> None:
        self.sim_time = sim_time
        self.cycle_number = 0

    def append_twin_history(self, fidelity: dict) -> None:
        self.twin_history.append(
            {
                "sim_time": fidelity["sim_time"],
                "assimilated_rmse": fidelity["assimilated_rmse"],
                "uncorrected_rmse": fidelity.get("uncorrected_rmse"),
            }
        )

    def twin_fidelity_payload(self) -> dict | None:
        if self.twin_fidelity is None:
            return None
        payload = dict(self.twin_fidelity)
        payload["history"] = list(self.twin_history)
        return payload

    def clear_live(self) -> None:
        """Used by `POST /demo/control {action: reset}`."""
        self.summary = {
            "overall_risk_score": 0,
            "overall_risk_band": "low",
            "critical_count": 0,
            "high_count": 0,
            "load_variance": 0.0,
        }
        self.entity_states.clear()
        for h in self.history.values():
            h.clear()
        for r in self.residuals.values():
            r.clear()
        for s in self.forecast_snapshots.values():
            s.clear()
        self.risk_breakdown.clear()
        self.forecasts.clear()
        self.pressure_timeline.clear()
        self.cascades.clear()
        self.cascade_ml = None
        self.previously_active_cascade_roots.clear()
        self.online_eval.reset()
        self.interventions.clear()
        self.certificates.clear()
        self.twin_fidelity = None
        self.twin_history.clear()
        self.regret_entries.clear()
        self.counterfactual_utilisation.clear()
        self.anomalies.clear()
        self.nudges.clear()
        self.cycle_latency_ms.clear()
        self.forecast_errors = {"model": [], "persistence": []}
        self.unstable_caught = 0
        self.certificates_scored.clear()
        self.observed_compliance.clear()
        self.commander_calls = 0
        self.commander_ungrounded = 0
        self.commander_tool_calls_ok = 0
        self.commander_tool_calls_total = 0
        self.cycles_over_critical.clear()
        self.attendees.clear()
        self.root_cooldown.clear()
        self.settlements.clear()
        self.twin_layers.clear()
        self.band_hold.clear()
        self.no_hold_until = -1
        self.cascade_signature = ()
        self.disruptions.clear()
        self.operations = {}
