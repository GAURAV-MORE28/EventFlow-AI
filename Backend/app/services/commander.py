"""Commander (01_BACKEND_CONTRACT.md §3.11).

Two things matter here and neither is the language model:

1. **Exactly eight tools may be registered.** `propose_action` queues and has no
   execution path — enforced at registration, not by asking a prompt nicely.
2. **The grounding validator is mandatory and runs backend-side.** Every numeric
   token in the answer must trace to a value a tool actually returned, or it is
   stripped and logged. `commander_ungrounded_rate` must read 0.0 at demo time.

The default engine is deterministic: it plans tool calls from the query, then
composes the answer from the values those tools returned. No paid API, no
per-token cost, and `ungrounded_count` is 0 by construction rather than by luck.
Setting `LOCAL_LLM_URL` swaps in a free local model (Ollama) for phrasing — the
validator runs identically over its output, which is exactly the safety net a
generative model needs.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from ..config import get_config, local_llm
from ..errors import ApiError

log = logging.getLogger("eventflow.commander")

NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

# 01 §3.11 — exactly these eight. Adding a ninth is a contract change.
ALLOWED_TOOLS = (
    "get_state",
    "get_forecast",
    "get_cascade",
    "get_interventions",
    "get_certificate",
    "run_whatif",
    "summarize_window",
    "propose_action",
)


class ToolRegistry:
    """Registration-time enforcement of the allow-list and the no-execution rule."""

    def __init__(self) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        if name not in ALLOWED_TOOLS:
            raise ValueError(
                f"'{name}' is not in the Commander's allow-list; "
                f"registering it would violate 01_BACKEND_CONTRACT.md §3.11"
            )
        self._tools[name] = fn

    def call(self, name: str, args: dict) -> Any:
        if name not in self._tools:
            raise ApiError("INVALID_REQUEST", f"Unknown tool '{name}'.")
        return self._tools[name](**args)

    def names(self) -> list[str]:
        return list(self._tools)


def _fmt_minutes(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"
    minutes = int(round(seconds / 60.0))
    return f"{minutes}"


def _collect_numbers(value: Any, out: set[str]) -> None:
    """Every number a tool returned, plus the unit conversions the answer may use."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        f = float(value)
        out.add(_canon(f))
        out.add(_canon(round(f)))
        out.add(_canon(round(f, 1)))
        out.add(_canon(round(f, 2)))
        # seconds -> minutes (00 §0: durations are seconds; the UI speaks minutes)
        if f >= 60:
            out.add(_canon(round(f / 60.0)))
            out.add(_canon(round(f / 60.0, 1)))
        # 0..1 ratio -> percentage
        if 0.0 <= f <= 1.0:
            out.add(_canon(round(f * 100)))
            out.add(_canon(round(f * 100, 1)))
        return
    if isinstance(value, str):
        for m in NUMBER_RE.findall(value):
            out.add(_canon(m.replace(",", "")))
        return
    if isinstance(value, dict):
        for v in value.values():
            _collect_numbers(v, out)
        return
    if isinstance(value, (list, tuple)):
        for v in value:
            _collect_numbers(v, out)


def _canon(value: Any) -> str:
    """Canonical numeric key so '18', '18.0' and 18 all match."""
    try:
        f = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return str(value)
    if f == int(f):
        return str(int(f))
    return f"{f:.4f}".rstrip("0").rstrip(".")


class GroundingValidator:
    """01 §3.11 steps 1-4."""

    def __init__(self, strip: bool = True) -> None:
        self.strip = strip

    def validate(self, text: str, tool_results: list[Any]) -> tuple[str, dict]:
        grounded: set[str] = set()
        for result in tool_results:
            _collect_numbers(result, grounded)

        emitted: list[str] = []
        ungrounded: list[str] = []
        for match in NUMBER_RE.findall(text):
            token = match.replace(",", "")
            emitted.append(token)
            if _canon(token) not in grounded:
                ungrounded.append(token)

        cleaned = text
        if self.strip and ungrounded:
            for token in sorted(set(ungrounded), key=len, reverse=True):
                # Remove the number and tidy the double space it leaves behind.
                cleaned = re.sub(rf"(?<!\d){re.escape(token)}(?!\d)", "—", cleaned)
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

        return cleaned, {
            "numbers_emitted": emitted,
            "numbers_grounded": [n for n in emitted if n not in ungrounded],
            "ungrounded": ungrounded,
            "ungrounded_count": len(ungrounded),
            "passed": not ungrounded,
        }


class Commander:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        cfg = get_config().raw.get("commander", {})
        self.cache_scripted = bool(cfg.get("cache_scripted_queries", True))
        self.engine_mode = cfg.get("engine", "deterministic")
        self.validator = GroundingValidator(strip=bool(cfg.get("strip_ungrounded_numbers", True)))
        self._cache: dict[str, dict] = {}

        self.tools = ToolRegistry()
        self.tools.register("get_state", self._tool_get_state)
        self.tools.register("get_forecast", self._tool_get_forecast)
        self.tools.register("get_cascade", self._tool_get_cascade)
        self.tools.register("get_interventions", self._tool_get_interventions)
        self.tools.register("get_certificate", self._tool_get_certificate)
        self.tools.register("run_whatif", self._tool_run_whatif)
        self.tools.register("summarize_window", self._tool_summarize_window)
        self.tools.register("propose_action", self._tool_propose_action)

    # --- the eight tools -------------------------------------------------------
    def _tool_get_state(self, entity_id: str = "all", entity_type: str | None = None) -> dict:
        store = self.engine.store
        if entity_type:
            return self._domain_state(entity_type)
        if entity_id == "all":
            return {"summary": store.summary, "cycle_number": store.cycle_number}
        state = store.entity_states.get(entity_id)
        if not state:
            raise ApiError("ENTITY_NOT_FOUND", f"No entity with id '{entity_id}'.", {"entity_id": entity_id})
        node = store.nodes[entity_id]
        return {**state, "nominal_capacity": node["nominal_capacity"], "display_name": node["display_name"],
                "utilisation_pct": int(round(state["utilisation"] * 100))}

    DOMAIN_TYPES = {
        "transport": ("transport_node", "transport_route"),
        "venue": ("venue", "gate"),
        "road": ("road",),
        "zone": ("zone",),
        "parking": ("parking",),
    }

    def _domain_state(self, domain: str) -> dict:
        store = self.engine.store
        if domain == "hotel":
            from .accommodation import hotel_list, recommend

            listing = hotel_list(self.engine, sort="occupancy")
            hotels = listing["hotels"]
            def row(h: dict) -> dict:
                return {"property_id": h["property_id"], "name": h["name"], "zone": h["zone"],
                        "occupancy_pct": int(round(h["occupancy"] * 100)), "rooms_available": h["rooms_available"],
                        "status": h["status"], "price_rupees": h["price_per_night_paise"] // 100,
                        "travel_min": None if h["travel_time_to_venue_sec"] is None else int(round(h["travel_time_to_venue_sec"] / 60))}
            rec = recommend(self.engine, limit=3)
            return {
                "entity_type": "hotel",
                "summary": listing["summary"],
                "nearing_capacity": [row(h) for h in hotels if h["status"] != "available"][:4],
                "most_available": [row(h) for h in sorted(hotels, key=lambda h: -h["rooms_available"])[:3]],
                "recommended": [row(o["property"]) for o in rec["options"][:2]],
            }
        types = self.DOMAIN_TYPES.get(domain, (domain,))
        rows = []
        for eid, node in store.nodes.items():
            if node["entity_type"] not in types or eid not in store.entity_states:
                continue
            st = store.entity_states[eid]
            fc = store.forecasts.get(eid) or {}
            rows.append({"entity_id": eid, "display_name": node["display_name"],
                         "utilisation_pct": int(round(st["utilisation"] * 100)), "risk_band": st["risk_band"],
                         "time_to_critical_sec": fc.get("time_to_critical_sec")})
        rows.sort(key=lambda r: -r["utilisation_pct"])
        return {"entity_type": domain, "entities": rows}

    def _tool_get_forecast(self, entity_id: str, horizon_sec: int | None = None) -> dict:
        forecast = self.engine.store.forecasts.get(entity_id)
        if not forecast:
            raise ApiError("MODEL_NOT_READY", "Forecast not yet available.", {"entity_id": entity_id})
        if horizon_sec:
            points = [p for p in forecast["points"] if p["horizon_sec"] == horizon_sec]
            return {**forecast, "points": points}
        return forecast

    def _tool_get_cascade(self, entity_id: str) -> dict:
        cascade = self.engine.store.cascades.get(entity_id)
        if not cascade:
            return {"root_entity_id": entity_id, "total_downstream_failures": 0, "steps": []}
        return cascade

    def _tool_get_interventions(self, status: str = "proposed") -> dict:
        return {"interventions": self.engine.store.interventions_by_status(status, limit=5)}

    def _tool_get_certificate(self, intervention_id: str) -> dict:
        cert = self.engine.store.certificates.get(intervention_id)
        if not cert:
            raise ApiError(
                "INTERVENTION_NOT_FOUND", f"No certificate for '{intervention_id}'.",
                {"intervention_id": intervention_id},
            )
        return cert

    def _tool_run_whatif(self, scenario_spec: dict) -> dict:
        from .simulation import SIMULATIONS

        result = SIMULATIONS.run_sync(self.engine, [scenario_spec], horizon_sec=3600, label="commander")
        names = self.engine.store.nodes
        b, sc = result["baseline"], result["scenario"]
        return {
            **result,
            "scenario_spec": scenario_spec,
            "baseline_peak_pct": int(round(b["peak_utilisation"] * 100)),
            "scenario_peak_pct": int(round(sc["peak_utilisation"] * 100)),
            "baseline_peak_name": names.get(b["peak_entity_id"] or "", {}).get("display_name", b["peak_entity_id"]),
            "scenario_peak_name": names.get(sc["peak_entity_id"] or "", {}).get("display_name", sc["peak_entity_id"]),
            "baseline_transport_pct": int(round((b.get("transport_pressure") or 0) * 100)),
            "scenario_transport_pct": int(round((sc.get("transport_pressure") or 0) * 100)),
            "new_critical_names": [names[e]["display_name"] for e in result["delta"]["new_critical_entities"] if e in names],
            "late_entries_change": int(round((sc.get("late_entries") or 0) - (b.get("late_entries") or 0))),
            "top_change_names": [c["display_name"] for c in result.get("top_changes", [])[:3]],
            "top_change_pcts": [[int(round(c["baseline_peak"] * 100)), int(round(c["scenario_peak"] * 100))]
                                for c in result.get("top_changes", [])[:3]],
        }

    def _tool_summarize_window(self, start: str | None = None, end: str | None = None, kind: str | None = None) -> dict:
        store = self.engine.store
        base = {
            "sim_time": store.sim_time,
            "cycle_number": store.cycle_number,
            "critical_count": store.summary["critical_count"],
            "high_count": store.summary["high_count"],
            "load_variance": store.summary["load_variance"],
            "interventions_proposed": len(store.interventions_by_status("proposed", limit=50)),
            "anomalies": len(store.anomalies),
        }
        if kind not in ("outlook", "arrival"):
            return base
        from .projection import PROJECTIONS

        proj = PROJECTIONS.get(self.engine)
        cfg = self.engine.config
        if kind == "outlook":
            upcoming = []
            now = proj["samples"][0]["util"]
            for eid, node in store.nodes.items():
                if node["entity_type"] == "hotel":
                    continue
                crit = cfg.thresholds_for(node["entity_type"])[1]
                if now.get(eid, 0.0) >= crit:
                    continue
                hit = next((smp for smp in proj["samples"] if smp["util"].get(eid, 0.0) >= crit), None)
                if hit:
                    peak = max(smp["util"].get(eid, 0.0) for smp in proj["samples"])
                    upcoming.append({"entity_id": eid, "display_name": node["display_name"],
                                     "minutes": int(round(hit["offset_sec"] / 60)), "peak_pct": int(round(peak * 100))})
            upcoming.sort(key=lambda r: (r["minutes"], -r["peak_pct"]))
            return {**base, "kind": "outlook", "horizon_min": int(proj["horizon_sec"] // 60), "upcoming": upcoming[:5]}

        # arrival: pressure on the primary venue's access nodes and gates, by departure window
        ev = self.engine.events.get(self.engine.events.primary_event_id)
        venue = ev["venue_entity_id"]
        gates = self.engine.generator.gates_of_venue(venue) if hasattr(self.engine.generator, "gates_of_venue") else []
        access = sorted({e["src_entity_id"] for e in store.edges if e["dst_entity_id"] in gates
                         and store.nodes[e["src_entity_id"]]["entity_type"] == "transport_node"})
        watch = gates + access
        from ..simtime import parse

        to_start = (parse(ev["start_time"]) - parse(store.sim_time)).total_seconds()
        windows = []
        for depart_min in range(0, 91, 15):
            arrive = depart_min * 60 + 1200  # ~20 minutes door to gate
            if to_start > 0 and arrive > to_start:
                break
            smp = min(proj["samples"], key=lambda x: abs(x["offset_sec"] - arrive))
            peak = max((smp["util"].get(e, 0.0) for e in watch), default=0.0)
            windows.append({"depart_in_min": depart_min, "peak_pct": int(round(peak * 100))})
        best = min(windows, key=lambda w: (w["peak_pct"], w["depart_in_min"])) if windows else None
        return {**base, "kind": "arrival", "event_name": ev["name"], "typical_trip_min": 20,
                "minutes_to_start": int(round(to_start / 60)), "windows": windows, "best": best}

    def _tool_propose_action(self, intervention_id: str) -> dict:
        """QUEUES ONLY. There is deliberately no execution path in this function.

        Approving an intervention requires `POST /interventions/{id}/approve` with
        an operator id. The model cannot reach that, by construction.
        """
        return {"queued": True, "intervention_id": intervention_id}

    # --- planning ------------------------------------------------------------------
    def _plan(self, query: str) -> tuple[list[tuple[str, dict]], dict]:
        q = query.lower()
        store = self.engine.store

        # The entity the question is about: named explicitly, else the most urgent.
        named = next((eid for eid in store.nodes if eid in q.replace(" ", "_")), None)
        if not named:
            for eid, node in store.nodes.items():
                if node["display_name"].lower() in q:
                    named = eid
                    break
        top = store.pressure_timeline[0]["entity_id"] if store.pressure_timeline else None
        subject = named or top
        # Whether the question named an entity is what separates "why is X
        # happening" (a causal answer about X) from "what's the biggest
        # problem" (a system-wide summary that happens to lead with X) — without
        # this, both fall back to the identical subject/plan/prose whenever X is
        # also the most urgent entity, which it usually is by construction.
        meta = {"subject": subject, "subject_named": named is not None, "intent": "generic"}

        plan: list[tuple[str, dict]] = [("get_state", {"entity_id": "all"})]

        # --- product questions: what-if, hotels, transport, timing, outlook -------------
        scenario = self._scenario_from_query(q, named)
        if scenario is not None:
            meta["intent"] = "whatif"
            plan.append(("run_whatif", {"scenario_spec": scenario}))
            return plan, meta
        if any(w in q for w in ("hotel", "room", "accommodation", "stay", "booking")):
            meta["intent"] = "hotels"
            plan.append(("get_state", {"entity_id": "all", "entity_type": "hotel"}))
            return plan, meta
        if any(w in q for w in ("when should", "best time", "off-peak", "off peak", "arrive", "depart", "leave")):
            meta["intent"] = "arrival"
            plan.append(("summarize_window", {"kind": "arrival"}))
            return plan, meta
        if any(w in q for w in ("next", "will become", "going to be critical", "outlook", "upcoming")):
            meta["intent"] = "outlook"
            plan.append(("summarize_window", {"kind": "outlook"}))
            return plan, meta
        if not named and any(w in q for w in ("transport", "metro", "station", "train", "bus", "transit", "line")):
            meta["intent"] = "transport"
            plan.append(("get_state", {"entity_id": "all", "entity_type": "transport"}))
            return plan, meta

        if any(w in q for w in ("do nothing", "nothing", "if we wait", "no action")):
            meta["intent"] = "do_nothing"
            plan.append(("summarize_window", {}))
            if subject:
                plan += [("get_forecast", {"entity_id": subject}), ("get_cascade", {"entity_id": subject})]
            return plan, meta

        if any(w in q for w in ("action", "intervention", "do about", "improvement", "safest", "recommend")):
            meta["intent"] = "action"
            plan.append(("get_interventions", {"status": "proposed"}))
            live = store.interventions_by_status("proposed", limit=1)
            if live:
                plan.append(("get_certificate", {"intervention_id": live[0]["intervention_id"]}))
            return plan, meta

        if "why" in q:
            meta["intent"] = "why"
        if subject:
            plan += [
                ("get_state", {"entity_id": subject}),
                ("get_forecast", {"entity_id": subject}),
                ("get_cascade", {"entity_id": subject}),
            ]
            if meta["intent"] == "generic":
                # A generic "what's wrong" question needs the system-wide count
                # too, so its answer differs from a "why is X" one even when
                # both resolve to the same most-urgent subject.
                plan.append(("summarize_window", {}))
        return plan, meta

    def _scenario_from_query(self, q: str, named: str | None) -> dict | None:
        """A what-if question, turned into a scenario spec, or None."""
        if not any(w in q for w in ("what if", "what happens if", "if we", "would happen", "impact of", "effect of", "affect")):
            return None
        store = self.engine.store
        numbers = [float(x) for x in NUMBER_RE.findall(q.replace(",", ""))]
        if "cancel" in q or "called off" in q:
            ev = next((e for e in self.engine.events.events.values()
                       if e["name"].lower() in q or e["event_id"] in q), None)
            ev = ev or self.engine.events.get(self.engine.events.primary_event_id)
            return {"scenario_type": "event_cancellation", "params": {"event_id": ev["event_id"]}}
        if named and store.nodes[named]["entity_type"] == "road" and any(w in q for w in ("close", "closure", "shut", "block")):
            return {"scenario_type": "road_closure", "params": {"entity_id": named}}
        if named and any(w in q for w in ("capacity", "reduce", "cut")) and numbers and "attendance" not in q:
            return {"scenario_type": "capacity_reduction", "params": {"entity_id": named, "delta_pct": -abs(numbers[0])}}
        if "delay" in q or "postpone" in q or "later" in q:
            ev = None
            for e in self.engine.events.events.values():
                if e["name"].lower() in q or e["event_id"] in q:
                    ev = e
            if ev is None:
                letters = [e for e in sorted(self.engine.events.events.values(), key=lambda x: x["start_time"])]
                for i, e in enumerate(letters):
                    if f"event {chr(ord('a') + i)}" in q:
                        ev = e
            ev = ev or self.engine.events.get(self.engine.events.primary_event_id)
            minutes = numbers[0] if numbers else 30.0
            if "hour" in q and minutes < 10:
                minutes *= 60
            return {"scenario_type": "event_delay", "params": {"event_id": ev["event_id"], "delay_min": minutes}}
        if named and store.nodes[named]["entity_type"] == "gate" and any(w in q for w in ("close", "closure", "shut")):
            return {"scenario_type": "gate_closure", "params": {"entity_id": named}}
        if named and store.nodes[named]["entity_type"] in ("transport_node", "transport_route") and any(
                w in q for w in ("outage", "close", "closure", "shut", "fail", "down")):
            kind = "station_closure" if store.nodes[named]["entity_type"] == "transport_node" and "clos" in q else "transport_outage"
            return {"scenario_type": kind, "params": {"entity_id": named}}
        if "rain" in q:
            return {"scenario_type": "weather_rain", "params": {"intensity": "heavy" if "heavy" in q else "moderate"}}
        if "attendance" in q or "more people" in q or "crowd" in q:
            pct = numbers[0] if numbers else 20.0
            return {"scenario_type": "attendance_delta", "params": {"delta_pct": pct}}
        if "hotel" in q and any(w in q for w in ("shortage", "offline", "fewer rooms", "lose")):
            return {"scenario_type": "hotel_shortage", "params": {"rooms_offline_pct": numbers[0] if numbers else 15.0}}
        return None

    # A cached answer older than this many cycles is stale enough that the sim
    # has moved on — re-answer rather than replay the first-ever response for
    # the rest of the run (the cache exists to dedupe rapid repeats of the same
    # question, e.g. a UI double-click, not to freeze an answer permanently).
    CACHE_STALE_AFTER_CYCLES = 4

    # --- answering ---------------------------------------------------------------------
    async def answer(self, query: str, session_id: str = "sess_demo") -> dict:
        store = self.engine.store
        cache_key = query.strip().lower()
        cached_entry = self._cache.get(cache_key) if self.cache_scripted else None
        if cached_entry and store.cycle_number - cached_entry["cycle_number"] <= self.CACHE_STALE_AFTER_CYCLES:
            cached = dict(cached_entry["payload"])
            cached["is_cached"] = True
            store.commander_calls += 1
            return cached

        plan, meta = self._plan(query)
        tool_calls: list[dict] = []
        results: list[Any] = []
        for name, args in plan:
            store.commander_tool_calls_total += 1
            try:
                result = self.tools.call(name, args)
            except ApiError as exc:
                tool_calls.append({"tool": name, "args": args, "result_digest": f"unavailable: {exc.code}"})
                continue
            store.commander_tool_calls_ok += 1
            results.append(result)
            tool_calls.append({"tool": name, "args": args, "result_digest": self._digest(name, result)})

        draft = self._compose(query, tool_calls, results, meta)
        if self.engine_mode == "local_llm":
            draft = self._phrase_with_local_llm(query, results) or draft

        text, grounding = self.validator.validate(draft, results)

        store.commander_calls += 1
        store.commander_ungrounded += grounding["ungrounded_count"]

        payload = {
            "response": text,
            "is_cached": False,
            "tool_calls": tool_calls,
            "grounding": grounding,
        }
        self._log(session_id, query, payload)
        if self.cache_scripted:
            self._cache[cache_key] = {"payload": payload, "cycle_number": store.cycle_number}
        return payload

    def _digest(self, name: str, result: Any) -> str:
        """Short, human-readable evidence line for the frontend's sources tray."""
        if name == "get_state" and "utilisation" in result:
            return f"utilisation={result['utilisation']} risk={result['risk_score']}"
        if name == "get_state":
            s = result.get("summary", {})
            return f"overall_risk={s.get('overall_risk_score')} load_variance={s.get('load_variance')}"
        if name == "get_forecast":
            return f"time_to_critical_sec={result.get('time_to_critical_sec')} source={result.get('source')}"
        if name == "get_cascade":
            return f"{result.get('total_downstream_failures', 0)} downstream failures"
        if name == "get_interventions":
            items = result.get("interventions", [])
            return f"{len(items)} proposed, top rank_score={items[0]['rank_score'] if items else None}"
        if name == "get_certificate":
            return f"verdict={result.get('verdict')} max_utilisation={result.get('max_zone_utilisation')}"
        if name == "summarize_window" and result.get("kind") == "outlook":
            return f"{len(result.get('upcoming', []))} entities projected critical within {result.get('horizon_min')} min"
        if name == "summarize_window" and result.get("kind") == "arrival":
            best = result.get("best") or {}
            return f"best departure in {best.get('depart_in_min')} min at {best.get('peak_pct')}% peak"
        if name == "summarize_window":
            return f"critical={result.get('critical_count')} high={result.get('high_count')}"
        if name == "run_whatif":
            return (f"peak {result.get('baseline_peak_pct')}% -> {result.get('scenario_peak_pct')}%, "
                    f"{len(result.get('new_critical_names', []))} newly critical")
        if name == "get_state" and result.get("entity_type") == "hotel":
            sm = result.get("summary", {})
            return f"{sm.get('saturated')} saturated, {sm.get('rooms_available')} rooms free"
        if name == "get_state" and "entities" in result:
            return f"{len(result['entities'])} {result.get('entity_type')} entities"
        return json.dumps(result)[:120]

    def _compose(self, query: str, tool_calls: list[dict], results: list[Any], meta: dict | None = None) -> str:
        """Template synthesis over tool output. Every number here came from a tool."""
        meta = meta or {}
        intent = meta.get("intent", "generic")
        state = next((r for r in results if isinstance(r, dict) and "utilisation" in r), None)
        forecast = next((r for r in results if isinstance(r, dict) and "points" in r), None)
        cascade = next((r for r in results if isinstance(r, dict) and "steps" in r), None)
        interventions = next((r for r in results if isinstance(r, dict) and "interventions" in r), None)
        certificate = next((r for r in results if isinstance(r, dict) and "verdict" in r), None)
        window = next((r for r in results if isinstance(r, dict) and "interventions_proposed" in r), None)
        summary = next(
            (r.get("summary") for r in results if isinstance(r, dict) and "summary" in r), None
        )

        parts: list[str] = []

        special = self._compose_product(intent, results)
        if special:
            return special

        # A "what's the biggest problem" question and a "why is X happening"
        # question used to compose byte-identical answers whenever X was also
        # the most urgent entity (the usual case) — both fell back to the same
        # subject and the same state+forecast narrative below with no framing
        # difference. `intent` (set from whether the query actually named an
        # entity) is what lets them differ, grounded in the same tool results.
        if intent == "generic" and window:
            parts.append(
                f"Across the event, {window['critical_count']} entities are critical and "
                f"{window['high_count']} are at high risk."
            )

        if state and forecast:
            name = state.get("display_name", state.get("entity_id"))
            ttc = forecast.get("time_to_critical_sec")
            if intent == "generic":
                if ttc is not None:
                    parts.append(
                        f"The most urgent is {name}, projected to cross critical in {_fmt_minutes(ttc)} minutes."
                    )
                else:
                    parts.append(f"The most urgent is {name}, not projected to cross critical within the forecast horizon.")
            else:
                if ttc is not None:
                    parts.append(f"{name} is projected to cross critical in {_fmt_minutes(ttc)} minutes.")
                else:
                    parts.append(f"{name} is not projected to cross critical within the forecast horizon.")
            # `flow_rate_per_min` is a signed NET rate (inbound minus outbound) —
            # it goes negative whenever an entity is currently draining, which is
            # a normal state, not a data error. Framing it as "inbound flow rate"
            # regardless of sign read as a bug to anyone reviewing this (a
            # negative "inbound" rate). The raw signed value is printed as-is
            # (not `abs()`) — the grounding validator only knows the seconds->
            # minutes and ratio->pct transforms (`_collect_numbers`), so a
            # value transformed any other way reads as ungrounded and gets
            # stripped to "—".
            rate = state["flow_rate_per_min"]
            direction = "filling" if rate >= 0 else "draining"
            parts.append(
                f"It is currently {direction}, net flow {rate} per minute, against a nominal "
                f"capacity of {int(state['nominal_capacity'])}, at {state['risk_score']} risk."
            )
            parts.append(f"The forecast source is {forecast.get('source')}.")

        if cascade and cascade.get("steps"):
            downstream = [s for s in cascade["steps"] if s["depth"] > 0]
            if downstream:
                first = downstream[0]
                last = downstream[-1]
                parts.append(
                    f"Predicted propagation reaches {first['entity_id']} in "
                    f"{_fmt_minutes(first['eta_sec'])} minutes and {last['entity_id']} in "
                    f"{_fmt_minutes(last['eta_sec'])} minutes, {len(downstream)} downstream failures in total."
                )
            else:
                parts.append("No downstream propagation is predicted from this entity.")

        if interventions is not None:
            items = interventions.get("interventions", [])
            if not items:
                parts.append("No interventions are queued; the system is nominal.")
            else:
                top = items[0]
                parts.append(
                    f"The highest ranked action is \"{top['title']}\" at rank score {top['rank_score']}, "
                    f"estimated relief {top['estimated_relief_pct']} percent."
                )
                cert = top.get("certificate")
                if cert:
                    parts.append(f"Its certificate reads {cert['verdict']}. {cert['reason']}")
                top_verdict = (top.get("certificate") or {}).get("verdict")
                # Only worth flagging when the winner is genuinely more stable —
                # "ranks below the unstable option" (when the winner is ALSO
                # UNSTABLE, just for a different reason) isn't the contrast this
                # sentence exists to make.
                if top_verdict in ("STABLE", "CONDITIONAL"):
                    unstable = [
                        i for i in items
                        if (i.get("certificate") or {}).get("verdict") == "UNSTABLE"
                        and float(i.get("estimated_relief_pct", 0.0)) > float(top.get("estimated_relief_pct", 0.0))
                    ]
                    if unstable:
                        worst = max(unstable, key=lambda i: float(i["estimated_relief_pct"]))
                        parts.append(
                            f"Note that \"{worst['title']}\" offers "
                            f"{worst['estimated_relief_pct']} percent relief but certifies UNSTABLE, "
                            f"so it ranks below the {top_verdict.lower()} option."
                        )
        elif certificate:
            parts.append(f"The certificate verdict is {certificate['verdict']}. {certificate['reason']}")

        if intent == "do_nothing" and window:
            parts.append(
                f"Doing nothing leaves {window['critical_count']} entities critical and "
                f"{window['high_count']} at high risk, with load variance {window['load_variance']}."
            )
        elif summary and not parts:
            parts.append(
                f"Overall risk is {summary['overall_risk_score']} ({summary['overall_risk_band']}), with "
                f"{summary['critical_count']} critical and {summary['high_count']} high-risk entities. "
                f"Load variance is {summary['load_variance']}."
            )

        if not parts:
            parts.append("No grounded data is available for that question yet.")
        return " ".join(parts)

    def _compose_product(self, intent: str, results: list[Any]) -> str | None:
        """Answers for the product questions. Every number is a tool value."""
        if intent == "whatif":
            r = next((x for x in results if isinstance(x, dict) and "scenario_spec" in x), None)
            if not r:
                return "The what-if simulation could not run for that question."
            spec = r["scenario_spec"]
            label = spec["scenario_type"].replace("_", " ")
            text = (
                f"Simulated {label} for the next {r['horizon_sec'] // 60} minutes against an unchanged copy of the city. "
                f"Peak utilisation goes from {r['baseline_peak_pct']} percent at {r['baseline_peak_name']} "
                f"to {r['scenario_peak_pct']} percent at {r['scenario_peak_name']}. "
            )
            if r["new_critical_names"]:
                text += "Newly critical: " + ", ".join(r["new_critical_names"]) + ". "
            else:
                text += "No additional location crosses the critical line. "
            if r["top_change_names"]:
                moves = [f"{n} {a} to {b} percent" for n, (a, b) in zip(r["top_change_names"], r["top_change_pcts"])]
                text += "Biggest changes: " + "; ".join(moves) + ". "
            text += (
                f"Average transport pressure moves from {r['baseline_transport_pct']} to "
                f"{r['scenario_transport_pct']} percent, and late entries change by {r['late_entries_change']}."
            )
            return text
        if intent == "hotels":
            r = next((x for x in results if isinstance(x, dict) and x.get("entity_type") == "hotel"), None)
            if not r:
                return None
            sm = r["summary"]
            text = (
                f"{sm['saturated']} of {sm['properties']} hotels are saturated and {sm['limited']} have limited "
                f"rooms; {sm['rooms_available']} rooms are free city-wide and {sm['unmet_room_requests']} booking "
                f"requests could not be placed. "
            )
            if r["nearing_capacity"]:
                text += "Nearing or at capacity: " + ", ".join(
                    f"{h['name']} ({h['occupancy_pct']} percent)" for h in r["nearing_capacity"]) + ". "
            if r["recommended"]:
                h = r["recommended"][0]
                text += (
                    f"Best alternative right now: {h['name']} in {h['zone']}, {h['rooms_available']} rooms free at "
                    f"Rs {h['price_rupees']} per night, {h['travel_min']} minutes to the venue."
                )
            return text
        if intent == "transport":
            r = next((x for x in results if isinstance(x, dict) and x.get("entity_type") == "transport"), None)
            if not r:
                return None
            top = r["entities"][:4]
            pressured = [e for e in top if e["risk_band"] in ("high", "critical")]
            lead = "Under pressure: " if pressured else "No transport node is under high pressure. Busiest: "
            return lead + ", ".join(
                f"{e['display_name']} at {e['utilisation_pct']} percent ({e['risk_band']})" for e in (pressured or top)
            ) + "."
        if intent == "arrival":
            r = next((x for x in results if isinstance(x, dict) and x.get("kind") == "arrival"), None)
            if not r:
                return None
            if not r.get("best"):
                if r["minutes_to_start"] <= 0:
                    return f"{r['event_name']} has already started; visitors arriving now go straight to entry."
                return (
                    f"{r['event_name']} starts in {r['minutes_to_start']} minutes and a typical trip to the gates "
                    f"takes about {r['typical_trip_min']} minutes, so visitors who have not left yet will arrive "
                    f"after the start whatever they do; they should leave immediately."
                )
            best, first = r["best"], r["windows"][0]
            if best["depart_in_min"] == 0:
                return (
                    f"Visitors should set off now: {r['event_name']} starts in {r['minutes_to_start']} minutes and "
                    f"later departures meet more pressure at the gates (now {first['peak_pct']} percent)."
                )
            return (
                f"The lowest-pressure window is to depart in {best['depart_in_min']} minutes: peak load on the "
                f"stations and gates serving {r['event_name']} falls from {first['peak_pct']} to {best['peak_pct']} "
                f"percent, still ahead of the start in {r['minutes_to_start']} minutes."
            )
        if intent == "outlook":
            r = next((x for x in results if isinstance(x, dict) and x.get("kind") == "outlook"), None)
            if not r:
                return None
            if not r["upcoming"]:
                return f"No further location is projected to turn critical in the next {r['horizon_min']} minutes."
            parts = [f"{u['display_name']} in {u['minutes']} minutes (peak {u['peak_pct']} percent)" for u in r["upcoming"]]
            return "Projected to turn critical next: " + "; ".join(parts) + "."
        return None

    def _phrase_with_local_llm(self, query: str, results: list[Any]) -> str | None:
        """Optional free/local rephrasing. The validator still gates every number."""
        target = local_llm()
        if not target:
            return None
        url, model = target
        try:
            import httpx

            prompt = (
                "You are an event operations analyst. Answer the question using ONLY the "
                "JSON facts provided. Never invent a number.\n\n"
                f"FACTS:\n{json.dumps(results)[:4000]}\n\nQUESTION: {query}\nANSWER:"
            )
            response = httpx.post(
                url, json={"model": model, "prompt": prompt, "stream": False}, timeout=8.0
            )
            response.raise_for_status()
            return (response.json().get("response") or "").strip() or None
        except Exception:
            log.warning("local LLM unavailable; using deterministic composition", exc_info=False)
            return None

    def _log(self, session_id: str, query: str, payload: dict) -> None:
        from datetime import datetime, timezone

        from ..db import models
        from ..db.base import SessionLocal

        try:
            with SessionLocal() as session:
                session.add(
                    models.CommanderLog(
                        session_id=session_id,
                        query=query,
                        response=payload["response"],
                        tool_calls=payload["tool_calls"],
                        numbers_emitted=payload["grounding"]["numbers_emitted"],
                        ungrounded_count=payload["grounding"]["ungrounded_count"],
                        passed=payload["grounding"]["passed"],
                        server_time=datetime.now(timezone.utc),
                    )
                )
                session.commit()
        except Exception:
            log.exception("commander log write failed")
