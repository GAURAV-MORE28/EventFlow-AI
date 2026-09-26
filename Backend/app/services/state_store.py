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
from ..topology import build_topology, verify

HISTORY_LIMIT = 240          # ~2 hours of sim at 30s steps
TWIN_HISTORY_LIMIT = 20      # 00 §2.8 — last 20 cycles, oldest first
ANOMALY_LIMIT = 50


class StateStore:
    def __init__(self, sim_start: str) -> None:
        self._lock = threading.RLock()

        topology = build_topology()
        verify(topology)
        self.nodes: dict[str, dict] = {n["entity_id"]: n for n in topology["nodes"]}
        self.edges: list[dict] = topology["edges"]
        self.segments: list[dict] = topology["segments"]
        self.bounds: dict[str, float] = topology["bounds"]

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
        # H4 fix: a root that was already an active cascade last cycle does not
        # re-fire `cascade_alert` just because the prediction refreshed — only a
        # root that newly appears does. Tracked separately from `self.cascades`
        # (which is replaced wholesale every cycle) so the diff survives it.
        self.previously_active_cascade_roots: set[str] = set()

        # Online precision/recall for the cascade predictor (03 §4.4), measured
        # against what the generator's own downstream entities actually do —
        # never against field data, and never a formula (01 §3.10 metrics.py
        # used to synthesise these from `cascade_count` alone).
        self.cascade_pending_checks: deque[tuple[int, str]] = deque()
        self.cascade_predicted_at: dict[str, int] = {}
        self.cascade_eval = {"alerts_confirmed": 0, "alerts_false": 0, "events_caught": 0, "events_missed": 0}

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
        # Phase 1B: per settled intervention, actual vs matched-do-nothing peak
        # and zone variance from the same world model (read by metrics.py).
        self.settlements: list[dict] = []
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
        # 03 §5.6 "certificate accuracy": at settlement, whether the certified
        # projection made at approval agreed (within 15%) with the realised
        # source utilisation — written in Engine._settle_executing_interventions.
        self.certificates_scored: list[bool] = []
        self.cascade_lead_times: list[float] = []
        self.observed_compliance: list[bool] = []

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
        self.previously_active_cascade_roots.clear()
        self.cascade_pending_checks.clear()
        self.cascade_predicted_at.clear()
        self.cascade_eval = {"alerts_confirmed": 0, "alerts_false": 0, "events_caught": 0, "events_missed": 0}
        self.interventions.clear()
        self.certificates.clear()
        self.twin_fidelity = None
        self.twin_history.clear()
        self.regret_entries.clear()
        self.counterfactual_utilisation.clear()
        self.settlements.clear()
        self.anomalies.clear()
        self.nudges.clear()
        self.cycle_latency_ms.clear()
        self.forecast_errors = {"model": [], "persistence": []}
        self.unstable_caught = 0
        self.certificates_scored.clear()
        self.cascade_lead_times.clear()
        self.observed_compliance.clear()
        self.commander_calls = 0
        self.commander_ungrounded = 0
        self.commander_tool_calls_ok = 0
        self.commander_tool_calls_total = 0
