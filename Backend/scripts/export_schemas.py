"""Export JSON Schema files from the Pydantic models.

    python -m scripts.export_schemas --out ../contracts/schemas/

Both sides validate against these same files (00_SHARED_CONTRACT.md, "Schema
validation in CI"): the backend through Pydantic, the frontend through
`npm run validate:mocks`. If a mock diverges from the contract, the build fails.
That is the whole mechanism.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import schemas as S  # noqa: E402

# The required schema list from 00_SHARED_CONTRACT.md, plus the endpoint
# envelopes the frontend mocks are actually shaped like.
EXPORTS: dict[str, type] = {
    "entity": S.Entity,
    "graph_edge": S.GraphEdge,
    "entity_state": S.EntityState,
    "forecast": S.Forecast,
    "cascade_result": S.CascadeResult,
    "certificate": S.Certificate,
    "intervention": S.Intervention,
    "twin_fidelity": S.TwinFidelity,
    "regret_entry": S.RegretEntry,
    "nudge": S.Nudge,
    "segment": S.Segment,
    "error_envelope": S.ErrorEnvelope,
    "ws_message": S.WsMessage,
    # Endpoint payloads — what the mock files actually contain.
    "graph_response": S.GraphResponse,
    "state_response": S.StateResponse,
    "pressure_timeline_response": S.PressureTimelineResponse,
    "pressure_timeline_frame": S.PressureTimelineFrame,
    "intervention_list_response": S.InterventionListResponse,
    "metrics_response": S.MetricsResponse,
    "commander_response": S.CommanderResponse,
    "journey_response": S.JourneyResponse,
    "event_response": S.EventResponse,
    "regret_response": S.RegretResponse,
    "active_cascades_response": S.ActiveCascadesResponse,
    "forecast_response": S.ForecastResponse,
    "health_response": S.HealthResponse,
    "simulation_result": S.SimulationResult,
    "nudge_list_response": S.NudgeListResponse,
    "event_list_response": S.EventListResponse,
    "overview_response": S.OverviewResponse,
    "hotel_list_response": S.HotelListResponse,
    "stay_recommendation_response": S.StayRecommendationResponse,
    "saturation_response": S.SaturationResponse,
    "disruption_list_response": S.DisruptionListResponse,
    # venue -> radius -> footprint -> blueprint -> event graph (additive)
    "venue_search_response": S.VenueSearchResponse,
    "venue": S.Venue,
    "blueprint_job": S.BlueprintJob,
    "blueprint": S.Blueprint,
    "blueprint_list_response": S.BlueprintListResponse,
    "world_info": S.WorldInfo,
    "geospatial_status": S.GeospatialStatus,
    # weather-driven digital twin (additive)
    "weather_response": S.WeatherResponse,
    "weather_impact": S.WeatherImpact,
    "weather_conditions": S.WeatherConditions,
    "social_signals_response": S.SocialSignalsResponse,
    "public_signal": S.PublicSignal,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="../contracts/schemas", help="output directory")
    args = parser.parse_args()

    out = Path(args.out)
    if not out.is_absolute():
        out = (Path(__file__).resolve().parent.parent / out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    for name, model in EXPORTS.items():
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["title"] = name
        (out / f"{name}.json").write_text(
            json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    print(f"wrote {len(EXPORTS)} schemas to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
