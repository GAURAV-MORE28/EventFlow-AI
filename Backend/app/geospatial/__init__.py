"""Venue → radius → footprint → OSM acquisition → blueprint → event graph.

The pipeline that turns an organiser's venue and monitoring radius into the
topology the existing EventFlow engine simulates:

    venues.py      VenueProvider: search / resolve a real venue (coordinates, OSM, Google)
    footprint.py   radius validation and the monitoring footprint (circle, bounds, ring)
    overpass.py    GeoDataProvider: focused OSM/Overpass acquisition inside the footprint
    blueprint.py   BlueprintBuilder: raw OSM data → Blueprint (nodes, edges, provenance)
    validation.py  generic graph validation (replaces demo-only checks for generated worlds)
    service.py     build jobs, persistence and activation into the engine

Nothing here imports the engine; the engine consumes a Blueprint's topology.
"""
