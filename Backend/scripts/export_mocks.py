"""Generate the frontend's mock fixtures from a real seeded run.

    python -m scripts.export_mocks --out ../Frontend/src/mocks --cycles 40

02_FRONTEND_CONTRACT.md §8 requires the mocks to validate against the same
schemas the backend validates against. Generating them from the backend itself
makes that true by construction rather than by discipline — a mock cannot drift
from a contract it was produced by.

Every file here is the exact payload of the endpoint it stands in for, so
flipping `VITE_MOCK=0` is a config change, not a debugging session.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import create_all  # noqa: E402
from app.db.seed import seed_topology  # noqa: E402
from app.api import routes as R  # noqa: E402
from app.services import accommodation as ACC  # noqa: E402
from app.services.attendee import build_journey, issue_nudges, public_nudge  # noqa: E402
from app.services.commander import Commander  # noqa: E402
from app.services.engine import Engine, set_engine  # noqa: E402
from app.services.metrics import build_metrics, build_regret  # noqa: E402
from app.services.simulation import SIMULATIONS  # noqa: E402
from app.simtime import shift  # noqa: E402

logging.basicConfig(level=logging.WARNING)

# Must match `SCRIPTED` in Frontend/src/components/CommanderBar.jsx.
SCRIPTED_QUESTIONS = [
    "What is the biggest problem right now?",
    "Which hotels are nearing capacity?",
    "What happens if Gate 5 closes?",
    "When should visitors arrive?",
]


def clean(intervention: dict) -> dict:
    return {k: v for k, v in intervention.items() if not k.startswith("_")}


def write(out: Path, name: str, payload) -> None:
    (out / name).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"  {name}")


async def build(out: Path, cycles: int) -> None:
    create_all()
    engine = Engine()
    engine.commander = Commander(engine)
    seed_topology(engine.store)
    set_engine(engine)
    engine.paused = True

    store = engine.store
    state_sequence: list[dict] = []
    pressure_progression: list[dict] = []
    twin_progression: list[dict] = []

    # Drift mode on early so the mock twin history actually diverges, which is
    # what the frontend's drift toggle needs to render against.
    engine.set_drift_mode(True)

    for cycle in range(cycles):
        previous = store.snapshot_states()
        await engine.run_cycle()
        state_sequence.append(
            {
                "cycle_number": store.cycle_number,
                "sim_time": store.sim_time,
                "summary": dict(store.summary),
                "entities": store.changed_entities(previous),
            }
        )
        pressure_progression.append(
            {
                "sim_time": store.sim_time,
                "active_source": store.active_forecast_source,
                "items": [dict(i) for i in store.pressure_timeline],
            }
        )
        fidelity = store.twin_fidelity_payload()
        if fidelity:
            twin_progression.append(fidelity)

    out.mkdir(parents=True, exist_ok=True)
    print(f"writing mocks to {out}")

    write(out, "event.json", (await R.event()).model_dump())
    write(out, "events.json", (await R.events()).model_dump())
    write(out, "overview.json", (await R.overview()).model_dump())
    write(out, "hotels.json", (await R.hotels(None, None, None, None, 0, False, None, "occupancy")).model_dump())
    write(out, "saturation.json", ACC.saturation(engine))
    write(out, "disruptions.json", (await R.disruptions()).model_dump())
    busiest = ACC.hotel_list(engine)["hotels"][0]["property_id"]
    write(out, "stay_recommendation.json", ACC.recommend(
        engine, destination_entity_id="stadium_main", segment_id="price_sensitive",
        current_property_id=busiest, limit=4,
    ))

    write(out, "graph.json", {
        "nodes": list(store.nodes.values()),
        "edges": store.edges,
        "segments": store.segments,
        "bounds": store.bounds,
    })

    write(out, "state.json", engine.state_payload())
    write(out, "state_sequence.json", state_sequence)
    write(out, "pressure_timeline.json", pressure_progression)

    # The demo chain, explicitly — not whichever cascade happened to be first.
    deepest = max(store.cascades.values(), key=lambda c: (c["max_depth"], c["total_downstream_failures"]), default=None)
    demo_cascade = deepest or engine.registry.cascade.predict(
        "metro_b", store.node_state_for_ml(), store.edges, generated_at=store.sim_time
    )
    write(out, "cascade_metro_b.json", demo_cascade)
    write(out, "cascades_active.json", engine.cascade_payload())

    # 02 §8 asks for exactly two interventions: one UNSTABLE, one STABLE. Pick the
    # clearest pairing in the run so the contrast frame is guaranteed to exist.
    all_interventions = [clean(i) for i in store.interventions.values() if i.get("certificate")]
    unstable = sorted(
        (i for i in all_interventions if i["certificate"]["verdict"] == "UNSTABLE"),
        key=lambda i: -i["estimated_relief_pct"],
    )
    stable = sorted(
        (i for i in all_interventions if i["certificate"]["verdict"] in ("STABLE", "CONDITIONAL")),
        key=lambda i: -i["rank_score"],
    )
    pair = [i for i in (stable[:1] + unstable[:1]) if i]
    pair.sort(key=lambda i: -i["rank_score"])
    selected = pair or all_interventions[:2]

    # The picked pair almost certainly aged past its TTL sometime during the
    # cycles run above — InterventionQueue.jsx only renders
    # proposed|executing|approved, so an "expired" status here silently drops
    # the card and the STABLE/UNSTABLE contrast frame (02 §5.5's "single most
    # important frame in the presentation") never renders in mock mode.
    # Re-stamp both as freshly proposed, as of *this* export, so the fixture
    # always renders regardless of when in the run they happened to be picked.
    for item in selected:
        item["status"] = "proposed"
        item["created_at"] = store.sim_time
        item["expires_at"] = shift(store.sim_time, engine.intervention_ttl_sec())

    write(out, "interventions.json", {
        "sim_time": store.sim_time,
        "interventions": selected,
    })

    write(out, "twin_fidelity.json", store.twin_fidelity_payload() or {})
    write(out, "twin_fidelity_sequence.json", twin_progression)
    write(out, "metrics.json", build_metrics(engine))
    write(out, "regret.json", build_regret(engine))

    responses = {}
    for question in SCRIPTED_QUESTIONS:
        responses[question] = await engine.commander.answer(question, "sess_mock")
    write(out, "commander_responses.json", responses)

    journey = build_journey(engine, {
        "attendee_id": "att_demo_1",
        "segment_id": "price_sensitive",
        "origin_entity_id": "htl_central_budget",
        "destination_entity_id": "stadium_main",
        "priority": "balanced",
        "include_return": True,
    })
    write(out, "attendee_journey.json", journey)

    # A nudge only exists when an action touches this attendee's route, so pick
    # an intervention whose source is on it.
    on_route = set(store.attendees["att_demo_1"]["route_entities"])
    target = next(
        (i for i in store.interventions.values()
         if i["intervention_type"] != "notify_only" and set(i["target_entity_ids"][:1]) & on_route),
        None,
    )
    nudges = [public_nudge(n) for n in issue_nudges(engine, target)] if target else []
    write(out, "attendee_nudges.json", {"nudges": nudges})

    simulation = SIMULATIONS._execute(
        engine,
        [{"scenario_type": "gate_closure", "params": {"entity_id": "gate_5"}}],
        3600,
    )
    simulation.update({
        "simulation_id": "sim_mock1",
        "status": "complete",
        "label": "Gate 5 closure",
    })
    simulation["candidate_interventions"] = [clean(i) for i in simulation["candidate_interventions"]]
    write(out, "simulation.json", simulation)

    # --- weather-driven digital twin -------------------------------------------------
    # Recorded with the OFFLINE providers on purpose: a replay fixture must never
    # claim `availability: "live"`, because replaying a recording is not a live
    # observation. The synthetic reading is labelled `synthetic` and the social
    # fixture is labelled `fallback_fixture` / `is_real_post: false`.
    from app.providers import social as SOCIAL
    from app.providers import weather as WEATHER

    WEATHER.set_weather_provider(WEATHER.SyntheticWeather(seed=engine.seed))
    SOCIAL.set_social_provider(SOCIAL.FixtureSignals())
    engine.weather.driving_live = False
    engine.weather.refresh_sync()
    weather_payload = engine.weather.payload()
    weather_payload["sim_time"] = store.sim_time
    write(out, "weather.json", weather_payload)

    topics = engine.weather.topics()
    signals = SOCIAL.get_social_provider().signals(topics, window_sec=21600, limit=20)
    write(out, "social_signals.json", {**signals, "topics": topics})

    weather_sim = SIMULATIONS._execute(
        engine,
        [{"scenario_type": "weather_scenario", "params": {"rain_mm_per_hr": 25.0, "storm_duration_min": 90}}],
        3600,
    )
    weather_sim.update({
        "simulation_id": "sim_mock_weather",
        "status": "complete",
        "label": "Heavy rain 25 mm/hr",
    })
    weather_sim["candidate_interventions"] = [clean(i) for i in weather_sim["candidate_interventions"]]
    write(out, "weather_simulation.json", weather_sim)

    write(out, "health.json", {
        "status": "ok",
        "server_time": store.sim_time,
        "sim_time": store.sim_time,
        "cycle_number": store.cycle_number,
        "modules": {
            "forecaster": {"ready": True, "active_source": store.active_forecast_source},
            "cascade": {"ready": True, "active_source": store.active_cascade_source},
            "twin": {"ready": True, "ensemble_size": engine.config.raw["twin"]["ensemble_size"]},
            "equilibrium": {"ready": True},
            "commander": {"ready": True, "active_source": "deterministic"},
            "weather": engine.weather.health(),
            "social": {"ready": True, "active_source": "fixture",
                       "detail": "Offline fixture data, labelled as such — never presented as real posts."},
        },
    })

    print(
        f"\n{len(state_sequence)} cycles | "
        f"{len(pair)} paired interventions | "
        f"cascade depth {demo_cascade['max_depth']} | "
        f"twin history {len(twin_progression)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="../Frontend/src/mocks")
    parser.add_argument("--cycles", type=int, default=40)
    args = parser.parse_args()

    out = Path(args.out)
    if not out.is_absolute():
        out = (Path(__file__).resolve().parent.parent / out).resolve()

    asyncio.run(build(out, args.cycles))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
