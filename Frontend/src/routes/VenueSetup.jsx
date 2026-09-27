/**
 * Venue & Network — the organiser's entry point:
 *
 *   venue search -> select -> monitoring radius -> build blueprint -> activate
 *
 * The map is not decoration: the blueprint drawn here (roads, stations,
 * parking, hotels, emergency posts, inferred access points) is exactly the
 * graph the EventFlow engine simulates once activated. Everything comes from
 * the backend (`/venues/search`, `/blueprints`, `/blueprints/{id}/activate`);
 * the only thing computed here is the radius ring preview before a build (the
 * built footprint replaces it) and the staged reveal of the real blueprint.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import DeckGL from '@deck.gl/react';
import { MapView, WebMercatorViewport } from '@deck.gl/core';
import { PathLayer, PolygonLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers';
import { CheckCircle2, CircleDashed, Hammer, Loader2, MapPin, Play, Search, XCircle } from 'lucide-react';

import PageShell, { Stat } from '../components/PageShell.jsx';
import { api } from '../lib/api.js';
import { TILE_ATTRIBUTION, basemapLayers } from '../lib/basemap.js';
import { integer } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

const STAGE_LABEL = {
  resolving_venue: 'Resolve venue',
  footprint: 'Monitoring footprint',
  acquiring_osm: 'Acquire OSM data (roads, transit, parking, hotels, emergency)',
  building_blueprint: 'Build blueprint (junctions, segments, access points, routes)',
  validating: 'Validate event graph',
};
// Semantic layers: colour = category; solid = sourced from OSM, hollow = inferred.
const CATEGORY = {
  venue: { label: 'Venue', rgb: [241, 245, 249] },
  gate: { label: 'Access points (inferred)', rgb: [45, 212, 191] },
  transport_node: { label: 'Transit', rgb: [56, 189, 248] },
  parking: { label: 'Parking', rgb: [167, 139, 250] },
  hotel: { label: 'Hotels', rgb: [251, 191, 36] },
  emergency_facility: { label: 'Emergency', rgb: [248, 113, 113] },
  zone: { label: 'Venue precinct (inferred)', rgb: [45, 212, 191] },
};
const REVEAL = [['road'], ['transport_node'], ['parking', 'hotel', 'emergency_facility'], ['gate', 'zone', 'venue']];
const ROAD_RGB = { motorway: [251, 146, 60], trunk: [251, 146, 60], primary: [250, 204, 21], secondary: [226, 232, 240] };

function destination(lat, lon, bearingDeg, dist) {
  const R = 6371000;
  const p1 = (lat * Math.PI) / 180;
  const l1 = (lon * Math.PI) / 180;
  const b = (bearingDeg * Math.PI) / 180;
  const d = dist / R;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [(((l2 * 180) / Math.PI + 540) % 360) - 180, (p2 * 180) / Math.PI];
}
const ring = (lat, lon, r) => Array.from({ length: 73 }, (_, i) => destination(lat, lon, (360 * (i % 72)) / 72, r));

function fit(lat, lon, r, width, height) {
  const [w] = destination(lat, lon, 270, r * 1.08);
  const [e] = destination(lat, lon, 90, r * 1.08);
  const [, n] = destination(lat, lon, 0, r * 1.08);
  const [, s] = destination(lat, lon, 180, r * 1.08);
  try {
    const v = new WebMercatorViewport({ width, height }).fitBounds([[w, s], [e, n]], { padding: 24 });
    return { longitude: v.longitude, latitude: v.latitude, zoom: v.zoom, pitch: 0, bearing: 0 };
  } catch {
    return { longitude: lon, latitude: lat, zoom: 13, pitch: 0, bearing: 0 };
  }
}

/** Animate a number towards its target (the radius ring expanding / shrinking). */
function useAnimated(target, ms = 450) {
  const [value, setValue] = useState(target);
  const from = useRef(target);
  useEffect(() => {
    const start = performance.now();
    const a = from.current;
    let raf;
    const tick = (now) => {
      const t = Math.min(1, (now - start) / ms);
      const v = a + (target - a) * (1 - (1 - t) ** 3);
      setValue(v);
      from.current = v;
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, ms]);
  return value;
}

function SetupMap({ venue, radius, blueprint, reveal, world }) {
  const ref = useRef(null);
  const [size, setSize] = useState({ width: 800, height: 600 });
  const [viewState, setViewState] = useState({ longitude: 0, latitude: 20, zoom: 1.2, pitch: 0, bearing: 0 });
  const [hovered, setHovered] = useState(null);
  const animatedRadius = useAnimated(radius);

  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const ob = new ResizeObserver(([entry]) => setSize({ width: entry.contentRect.width, height: entry.contentRect.height }));
    ob.observe(el);
    return () => ob.disconnect();
  }, []);
  const centre = blueprint ? blueprint.footprint : venue ? { center_lat: venue.lat, center_lon: venue.lon } : null;
  const focusKey = centre ? `${centre.center_lat},${centre.center_lon},${radius}` : null;
  useEffect(() => {
    if (!centre || !size.width) return;
    setViewState(fit(centre.center_lat, centre.center_lon, radius, size.width, size.height));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusKey, size.width, size.height]);

  const shown = useMemo(() => new Set(REVEAL.slice(0, reveal).flat()), [reveal]);
  const nodes = useMemo(() => (blueprint ? blueprint.nodes.filter((n) => shown.has(n.entity_type)) : []), [blueprint, shown]);
  const byId = useMemo(() => Object.fromEntries((blueprint?.nodes || []).map((n) => [n.entity_id, n])), [blueprint]);
  const roads = useMemo(
    () =>
      shown.has('road') && blueprint
        ? blueprint.edges.filter((e) => e.geometry && byId[e.src_entity_id]?.entity_type === 'road' && byId[e.dst_entity_id]?.entity_type === 'road')
        : [],
    [blueprint, byId, shown],
  );
  const routes = useMemo(() => {
    if (!blueprint || !shown.has('gate')) return [];
    const geo = {};
    for (const e of blueprint.edges) {
      if (e.geometry) {
        geo[`${e.src_entity_id}|${e.dst_entity_id}`] = e.geometry;
        geo[`${e.dst_entity_id}|${e.src_entity_id}`] = [...e.geometry].reverse();
      }
    }
    return blueprint.edges
      .filter((e) => e.via_entity_ids?.length)
      .map((e) => {
        const s = byId[e.src_entity_id];
        const d = byId[e.dst_entity_id];
        const path = [[s.lon, s.lat]];
        e.via_entity_ids.forEach((id, i) => {
          const seg = i > 0 ? geo[`${e.via_entity_ids[i - 1]}|${id}`] : null;
          if (seg) path.push(...seg.slice(1));
          else path.push([byId[id].lon, byId[id].lat]);
        });
        path.push([d.lon, d.lat]);
        return { path };
      });
  }, [blueprint, byId, shown]);

  const ringPath = blueprint
    ? blueprint.footprint.ring
    : venue
      ? ring(venue.lat, venue.lon, animatedRadius)
      : null;
  const layers = [
    ...basemapLayers(viewState, size.width, size.height, { opacity: 0.75, desaturate: 0.35 }),
    new PolygonLayer({
      id: 'footprint',
      data: ringPath ? [{ polygon: ringPath }] : [],
      getPolygon: (d) => d.polygon,
      getFillColor: [45, 212, 191, 22],
      getLineColor: [20, 184, 166, 230],
      getLineWidth: 2,
      lineWidthUnits: 'pixels',
      pickable: false,
    }),
    new PolygonLayer({
      id: 'venue-outline',
      data: blueprint?.venue_geometry ? [{ polygon: blueprint.venue_geometry }] : [],
      getPolygon: (d) => d.polygon,
      getFillColor: [15, 23, 42, 70],
      getLineColor: [15, 23, 42, 230],
      getLineWidth: 2,
      lineWidthUnits: 'pixels',
    }),
    new PathLayer({
      id: 'roads',
      data: roads,
      getPath: (d) => d.geometry,
      getColor: (d) => ROAD_RGB[byId[d.src_entity_id]?.subtype] || [100, 116, 139, 230],
      getWidth: (d) => (d.capacity_per_min > 45 ? 4 : 2.5),
      widthUnits: 'pixels',
      capRounded: true,
    }),
    new PathLayer({
      id: 'routes',
      data: routes,
      getPath: (d) => d.path,
      getColor: [13, 148, 136, 110],
      getWidth: 1.5,
      widthUnits: 'pixels',
    }),
    new ScatterplotLayer({
      id: 'road-junctions',
      data: nodes.filter((n) => n.entity_type === 'road'),
      getPosition: (d) => [d.lon, d.lat],
      getRadius: 2,
      radiusUnits: 'pixels',
      getFillColor: [30, 41, 59, 220],
      pickable: true,
      onHover: ({ object }) => setHovered(object || null),
    }),
    new ScatterplotLayer({
      id: 'entities',
      data: nodes.filter((n) => n.entity_type !== 'road'),
      getPosition: (d) => [d.lon, d.lat],
      getRadius: (d) => (d.entity_type === 'venue' ? 9 : d.entity_type === 'gate' ? 6 : 5),
      radiusUnits: 'pixels',
      // Sourced from OSM = solid; inferred / aggregated = hollow ring.
      filled: true,
      getFillColor: (d) => [...CATEGORY[d.entity_type].rgb, d.provenance?.inferred ? 40 : 235],
      stroked: true,
      getLineColor: (d) => [...(d.provenance?.inferred ? CATEGORY[d.entity_type].rgb : [15, 23, 42]), 255],
      getLineWidth: (d) => (d.provenance?.inferred ? 2.5 : 1),
      lineWidthUnits: 'pixels',
      pickable: true,
      onHover: ({ object }) => setHovered(object || null),
    }),
    new TextLayer({
      id: 'labels',
      data: nodes.filter((n) => n.entity_type === 'venue' || n.entity_type === 'gate'),
      getPosition: (d) => [d.lon, d.lat],
      getText: (d) => d.display_name,
      getSize: 11,
      getPixelOffset: [0, -14],
      getColor: [15, 23, 42, 255],
      fontWeight: 700,
      outlineWidth: 0.25,
      outlineColor: [255, 255, 255, 255],
      fontSettings: { sdf: true },
    }),
    new ScatterplotLayer({
      id: 'venue-pin',
      data: venue && !blueprint ? [venue] : [],
      getPosition: (d) => [d.lon, d.lat],
      getRadius: 8,
      radiusUnits: 'pixels',
      getFillColor: [15, 23, 42, 255],
      getLineColor: [255, 255, 255, 255],
      stroked: true,
      getLineWidth: 2,
      lineWidthUnits: 'pixels',
    }),
  ];

  return (
    <div ref={ref} className="relative h-full min-h-[520px] w-full overflow-hidden rounded-md border border-surface-700/60 bg-slate-200">
      <DeckGL
        views={new MapView({ repeat: false })}
        viewState={viewState}
        onViewStateChange={({ viewState: v }) => setViewState(v)}
        controller={{ dragRotate: false }}
        layers={layers}
      />
      <div className="pointer-events-auto absolute bottom-1 right-2 rounded bg-white/85 px-1.5 py-0.5 text-[10px] text-slate-700">
        <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer" className="underline">
          {TILE_ATTRIBUTION}
        </a>
      </div>
      {blueprint && (
        <div className="absolute left-2 top-2 rounded bg-white/90 px-2 py-1.5 text-[10px] text-slate-700 shadow">
          <div className="mb-1 font-semibold uppercase tracking-wider text-slate-500">Blueprint layers</div>
          {Object.entries(CATEGORY).map(([k, c]) => (
            <div key={k} className="flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full" style={{ backgroundColor: `rgb(${c.rgb.join(',')})` }} />
              {c.label}
            </div>
          ))}
          <div className="mt-1 flex items-center gap-1.5">
            <span className="h-0.5 w-3 bg-slate-500" /> Roads (OSM geometry)
          </div>
          <div className="flex items-center gap-1.5">
            <span className="h-0.5 w-3 bg-teal-600/60" /> Walking routes to access points
          </div>
          <div className="mt-1 text-slate-500">solid = from OSM · hollow = inferred</div>
        </div>
      )}
      {!venue && !blueprint && (
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="rounded bg-white/90 px-3 py-2 text-xs text-slate-700 shadow">
            Search for a venue to start. {world?.source === 'generated_blueprint' ? `Active: ${world.venue_name}` : ''}
          </div>
        </div>
      )}
      {hovered && (
        <div className="pointer-events-none absolute bottom-7 left-2 max-w-xs rounded bg-white/95 px-2 py-1.5 text-[11px] text-slate-800 shadow">
          <div className="font-semibold">{hovered.display_name}</div>
          <div className="text-slate-500">
            {hovered.entity_type.replace(/_/g, ' ')}
            {hovered.subtype ? ` · ${hovered.subtype.replace(/_/g, ' ')}` : ''}
          </div>
          <div>
            capacity {integer(hovered.nominal_capacity)} · <i>{hovered.capacity_source}</i> ({hovered.capacity_confidence})
          </div>
          <div className="text-slate-500">
            source {hovered.provenance?.source}
            {hovered.provenance?.source_id ? ` ${String(hovered.provenance.source_id).slice(0, 40)}` : ''}
            {hovered.provenance?.inferred ? ' · inferred' : ''}
          </div>
          {hovered.meta?.evidence && <div className="text-slate-500">{hovered.meta.evidence}</div>}
        </div>
      )}
    </div>
  );
}

function StageList({ job }) {
  if (!job) return null;
  return (
    <ul className="space-y-1 text-xs">
      {job.stages.map((s) => (
        <li key={s.stage} className="flex items-center gap-2">
          {s.status === 'done' ? (
            <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
          ) : s.status === 'running' ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-teal-400" />
          ) : s.status === 'failed' ? (
            <XCircle className="h-3.5 w-3.5 text-red-500" />
          ) : (
            <CircleDashed className="h-3.5 w-3.5 text-slate-500" />
          )}
          <span className={s.status === 'pending' ? 'text-slate-500' : 'text-slate-200'}>{STAGE_LABEL[s.stage]}</span>
          {s.ms != null && <span className="ml-auto tabular-nums text-slate-500">{(s.ms / 1000).toFixed(1)} s</span>}
        </li>
      ))}
    </ul>
  );
}

function SourceBadge({ source }) {
  const live = source === 'live_osm';
  const label = { live_osm: 'LIVE OSM DATA', osm_snapshot: 'OSM SNAPSHOT (not live)', synthetic: 'SYNTHETIC DEMO' }[source] || source;
  return (
    <span className={`chip border text-[10px] font-semibold ${live ? 'border-emerald-500/50 text-emerald-400' : 'border-amber-500/50 text-amber-400'}`}>
      ● {label}
    </span>
  );
}

const INPUT = 'rounded border border-surface-700 bg-surface-850 px-2 py-1 text-xs';
const dtLocal = (iso) => (iso ? iso.slice(0, 16) : '');

export default function VenueSetup() {
  const { mockMode, world, event, toast } = useStore();
  const navigate = useNavigate();
  const [status, setStatus] = useState(null);
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState({ loading: false, data: null, error: null });
  const [venue, setVenue] = useState(null);
  const [radiusKm, setRadiusKm] = useState(2);
  const [venueCapacity, setVenueCapacity] = useState('');
  const [job, setJob] = useState(null);
  const [buildError, setBuildError] = useState(null);
  const [blueprint, setBlueprint] = useState(null);
  const [reveal, setReveal] = useState(0);
  const [saved, setSaved] = useState([]);
  const [ev, setEv] = useState({ name: '', expected_attendance: '', lodging_pct: '', start_time: '', end_time: '' });
  const [activating, setActivating] = useState(false);

  useEffect(() => {
    if (mockMode) return;
    api.geoStatus().then((s) => { setStatus(s); setRadiusKm((s.radius_default_m || 2000) / 1000); }).catch(() => {});
    api.blueprints().then((r) => setSaved(r.blueprints)).catch(() => {});
  }, [mockMode]);
  useEffect(() => {
    if (event && !ev.start_time) setEv((e) => ({ ...e, start_time: dtLocal(event.start_time), end_time: dtLocal(event.end_time) }));
  }, [event]); // eslint-disable-line react-hooks/exhaustive-deps

  // Reveal the built blueprint by semantic layer (real data, staged).
  useEffect(() => {
    if (!blueprint) return undefined;
    setReveal(0);
    const timers = REVEAL.map((_, i) => setTimeout(() => setReveal(i + 1), 150 + i * 380));
    return () => timers.forEach(clearTimeout);
  }, [blueprint?.blueprint_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const minKm = (status?.radius_min_m ?? 250) / 1000;
  const maxKm = (status?.radius_max_m ?? 5000) / 1000;
  const radiusValid = Number.isFinite(radiusKm) && radiusKm >= minKm && radiusKm <= maxKm;

  async function runSearch(e) {
    e?.preventDefault();
    setSearch({ loading: true, data: null, error: null });
    try {
      setSearch({ loading: false, data: await api.venueSearch(query), error: null });
    } catch (error) {
      setSearch({ loading: false, data: null, error });
    }
  }

  function choose(v) {
    setVenue(v);
    setBlueprint(null);
    setJob(null);
    setBuildError(null);
    if (v.osm_capacity) setVenueCapacity(String(v.osm_capacity));
  }

  async function build() {
    setBuildError(null);
    setBlueprint(null);
    try {
      const body = { venue: { venue_id: venue.venue_id, source: venue.source, osm_type: venue.osm_type, osm_id: venue.osm_id,
        lat: venue.source === 'coordinates' ? venue.lat : undefined, lon: venue.source === 'coordinates' ? venue.lon : undefined,
        display_name: venue.source === 'coordinates' ? venue.display_name : undefined },
        radius_m: Math.round(radiusKm * 1000) };
      if (venueCapacity) body.venue_capacity = Number(venueCapacity);
      let j = await api.startBlueprint(body);
      setJob(j);
      while (j.status === 'running') {
        await new Promise((r) => setTimeout(r, 600));
        j = await api.blueprintBuild(j.build_id);
        setJob(j);
      }
      if (j.status === 'failed') {
        setBuildError(j.error);
        return;
      }
      const bp = await api.blueprint(j.blueprint_id);
      setBlueprint(bp);
      setEv((e) => ({ ...e, name: e.name || `Event at ${bp.venue.display_name}` }));
      api.blueprints().then((r) => setSaved(r.blueprints)).catch(() => {});
    } catch (error) {
      setBuildError({ code: error.code, message: error.message });
    }
  }

  async function activate(id) {
    setActivating(true);
    try {
      const evBody = {};
      if (ev.name) evBody.name = ev.name;
      if (ev.expected_attendance) evBody.expected_attendance = Number(ev.expected_attendance);
      if (ev.lodging_pct !== '') evBody.lodging_share = Number(ev.lodging_pct) / 100;
      if (ev.start_time) evBody.start_time = `${ev.start_time}:00Z`;
      if (ev.end_time) evBody.end_time = `${ev.end_time}:00Z`;
      const info = await api.activateBlueprint(id, { event: evBody });
      toast(`Network activated: ${info.venue_name} — ${info.node_count} entities, ${info.edge_count} edges.`, 'success');
      navigate('/');
    } catch (error) {
      toast(`${error.code || 'Error'}: ${error.message}`, 'error');
    } finally {
      setActivating(false);
    }
  }

  async function useDemo() {
    try {
      await api.activateSyntheticDemo();
      toast('Synthetic demo world active (illustrative data, not a real place).', 'success');
      navigate('/');
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  const s = blueprint?.summary;
  const venueNode = blueprint?.nodes.find((n) => n.entity_id === blueprint.venue_entity_id);
  return (
    <PageShell
      title="Venue & Network"
      subtitle="Pick a real venue and a monitoring radius. EventFlow queries OpenStreetMap inside that footprint, builds the operational network (roads, transit, parking, hotels, emergency, access points) and — once activated — runs its simulation on exactly that graph."
      actions={
        world && (
          <div className="flex items-center gap-2 text-xs text-slate-400">
            Active world: <span className="font-semibold text-slate-200">{world.venue_name || world.world_id}</span>
            <SourceBadge source={world.data_source} />
          </div>
        )
      }
    >
      {mockMode ? (
        <div className="panel px-3 py-3 text-sm text-slate-300">
          Venue setup queries live geospatial providers and changes the simulated world, so it needs the live backend
          (<code>npm run dev:live</code>). The recorded replay does not pretend to make those requests.
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[380px_minmax(0,1fr)]">
          <div className="space-y-3">
            <section className="panel p-3">
              <div className="panel-title mb-2 flex items-center gap-1.5"><MapPin className="h-3.5 w-3.5" /> 1 · Venue</div>
              <form onSubmit={runSearch} className="flex gap-2">
                <input
                  aria-label="Venue search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Stadium name, or lat, lon"
                  className={`${INPUT} flex-1`}
                />
                <button type="submit" className="btn-primary" disabled={search.loading || query.trim().length < 3}>
                  {search.loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />} Search
                </button>
              </form>
              {search.error && <p className="mt-2 text-xs text-red-400">{search.error.message}</p>}
              {search.data && (
                <div className="mt-2 space-y-1">
                  {search.data.results.length === 0 && (
                    <p className="text-xs text-amber-400">
                      {search.data.all_failed ? 'Venue search providers are unavailable right now. You can enter coordinates as "lat, lon".' : 'No venue found. Try another name or enter coordinates.'}
                    </p>
                  )}
                  {search.data.results.map((v) => (
                    <button
                      key={v.venue_id}
                      type="button"
                      onClick={() => choose(v)}
                      className={`w-full rounded border px-2 py-1.5 text-left text-xs ${venue?.venue_id === v.venue_id ? 'border-teal-500 bg-teal-500/10' : 'border-surface-700 hover:border-slate-500'}`}
                    >
                      <div className="font-semibold text-slate-100">{v.display_name}</div>
                      <div className="truncate text-slate-400">{v.formatted_address || `${v.lat}, ${v.lon}`}</div>
                      <div className="text-[10px] text-slate-500">
                        {v.source}{v.category ? ` · ${v.category}` : ''} · confidence {v.confidence}
                      </div>
                    </button>
                  ))}
                  {search.data.attribution?.length > 0 && (
                    <p className="text-[10px] text-slate-500">{search.data.attribution.join(' · ')}</p>
                  )}
                </div>
              )}
            </section>

            <section className="panel p-3">
              <div className="panel-title mb-2">2 · Monitoring radius</div>
              <div className="flex items-center gap-2">
                <input
                  aria-label="Radius slider"
                  type="range" min={minKm} max={maxKm} step={0.25}
                  value={Number.isFinite(radiusKm) ? radiusKm : minKm}
                  onChange={(e) => setRadiusKm(Number(e.target.value))}
                  className="flex-1"
                />
                <input
                  aria-label="Radius km"
                  type="number" step="0.25" min={minKm} max={maxKm}
                  value={radiusKm}
                  onChange={(e) => setRadiusKm(e.target.value === '' ? NaN : Number(e.target.value))}
                  className={`${INPUT} w-20`}
                />
                <span className="text-xs text-slate-400">km</span>
              </div>
              {!radiusValid && <p className="mt-1 text-xs text-red-400">Radius must be between {minKm} and {maxKm} km.</p>}
              <label className="mt-2 block text-[11px] text-slate-400">
                Venue capacity (organiser, optional)
                <input
                  aria-label="Venue capacity"
                  type="number" min="100" value={venueCapacity}
                  onChange={(e) => setVenueCapacity(e.target.value)}
                  placeholder="unknown: estimated from OSM"
                  className={`${INPUT} mt-1 w-full`}
                />
              </label>
              <button
                type="button" className="btn-primary mt-3 w-full justify-center"
                disabled={!venue || !radiusValid || job?.status === 'running'}
                onClick={build}
              >
                {job?.status === 'running' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Hammer className="h-3.5 w-3.5" />}
                Build blueprint
              </button>
              <div className="mt-2"><StageList job={job} /></div>
              {buildError && (
                <div className="mt-2 rounded border border-red-500/40 bg-red-500/10 px-2 py-1.5 text-xs text-red-300">
                  <b>{buildError.code}</b> — {buildError.message}
                </div>
              )}
            </section>

            {blueprint && (
              <section className="panel p-3">
                <div className="panel-title mb-2 flex items-center justify-between">
                  <span>3 · Blueprint</span>
                  <SourceBadge source={blueprint.metadata.data_source} />
                </div>
                <div className="grid grid-cols-3 gap-2">
                  <Stat label="Road junctions" value={s.road_nodes} />
                  <Stat label="Road segments" value={s.road_edges} />
                  <Stat label="Transit" value={s.transport_nodes} />
                  <Stat label="Hotels" value={s.hotels} />
                  <Stat label="Parking" value={s.parking} />
                  <Stat label="Emergency" value={s.emergency} />
                  <Stat label="Access points" value={s.access_points} sub={s.gate_inference.replace(/_/g, ' ')} />
                  <Stat label="Entities" value={s.total_nodes} />
                  <Stat label="Edges" value={s.total_edges} />
                </div>
                <div className="mt-2 text-[11px] text-slate-400">
                  Venue capacity {integer(venueNode?.nominal_capacity)} (<i>{venueNode?.capacity_source}</i>, {venueNode?.capacity_confidence} confidence)
                  · OSM data as of {blueprint.metadata.osm_base_timestamp || 'unknown'} · {blueprint.metadata.attribution}
                </div>
                <details className="mt-2 text-[11px] text-slate-400">
                  <summary className="cursor-pointer text-slate-300">Capacity & travel-time provenance</summary>
                  <ul className="mt-1 space-y-0.5">
                    {Object.entries(blueprint.provenance_summary.capacity_source).map(([k, v]) => (
                      <li key={k}>capacity · {k}: {v}</li>
                    ))}
                    {Object.entries(blueprint.provenance_summary.travel_time_source).map(([k, v]) => (
                      <li key={k}>travel time · {k}: {v}</li>
                    ))}
                  </ul>
                </details>
                {blueprint.warnings.length > 0 && (
                  <ul className="mt-2 list-disc space-y-0.5 pl-4 text-[11px] text-amber-400">
                    {blueprint.warnings.map((w, i) => <li key={`${i}-${w}`}>{w}</li>)}
                  </ul>
                )}
                <div className={`mt-2 text-xs ${blueprint.validation.valid ? 'text-emerald-400' : 'text-red-400'}`}>
                  {blueprint.validation.valid ? 'Event graph valid' : `Invalid: ${blueprint.validation.errors.join('; ')}`}
                </div>
              </section>
            )}

            {blueprint && (
              <section className="panel p-3">
                <div className="panel-title mb-2">4 · Activate the network</div>
                <div className="grid grid-cols-2 gap-2 text-[11px] text-slate-400">
                  <label className="col-span-2">Event name
                    <input aria-label="Event name" className={`${INPUT} mt-1 w-full`} value={ev.name} onChange={(e) => setEv({ ...ev, name: e.target.value })} />
                  </label>
                  <label>Expected attendance
                    <input aria-label="Expected attendance" type="number" className={`${INPUT} mt-1 w-full`} value={ev.expected_attendance}
                      placeholder={`${integer(Math.round((venueNode?.nominal_capacity || 0) * 0.85))} (85%)`}
                      onChange={(e) => setEv({ ...ev, expected_attendance: e.target.value })} />
                  </label>
                  <label>Need a hotel room (%)
                    <input aria-label="Lodging share percent" type="number" min="0" max="100" className={`${INPUT} mt-1 w-full`} value={ev.lodging_pct}
                      placeholder="default" onChange={(e) => setEv({ ...ev, lodging_pct: e.target.value })} />
                  </label>
                  <label>Start (UTC)
                    <input type="datetime-local" className={`${INPUT} mt-1 w-full`} value={ev.start_time} onChange={(e) => setEv({ ...ev, start_time: e.target.value })} />
                  </label>
                  <label>End (UTC)
                    <input type="datetime-local" className={`${INPUT} mt-1 w-full`} value={ev.end_time} onChange={(e) => setEv({ ...ev, end_time: e.target.value })} />
                  </label>
                </div>
                <button
                  type="button" className="btn-primary mt-3 w-full justify-center"
                  disabled={activating || !blueprint.validation.valid}
                  onClick={() => activate(blueprint.blueprint_id)}
                >
                  {activating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />} Activate network
                </button>
                <p className="mt-1 text-[10px] text-slate-500">
                  Replaces the simulated world without a restart: a new run starts at the simulation start time on this graph.
                </p>
              </section>
            )}

            <section className="panel p-3">
              <div className="panel-title mb-2">Saved blueprints</div>
              {saved.length === 0 && <p className="text-xs text-slate-500">None yet.</p>}
              <ul className="space-y-1">
                {saved.slice(0, 8).map((b) => (
                  <li key={b.blueprint_id} className="flex items-center gap-2 text-xs">
                    <span className="truncate text-slate-200">{b.venue.display_name}</span>
                    <span className="text-slate-500">{(b.radius_m / 1000).toFixed(2)} km · {b.summary.total_nodes} ent.</span>
                    {b.active ? (
                      <span className="ml-auto text-emerald-400">active</span>
                    ) : (
                      <button type="button" className="ml-auto text-teal-400 underline" onClick={() => activate(b.blueprint_id)}>
                        activate
                      </button>
                    )}
                  </li>
                ))}
              </ul>
              <button type="button" className="mt-2 text-xs text-slate-400 underline" onClick={useDemo}>
                Use the synthetic demo world (illustrative, not a real place)
              </button>
            </section>
          </div>
          <div className="h-[calc(100vh-190px)] min-h-[520px]">
            <SetupMap venue={venue} radius={radiusValid ? radiusKm * 1000 : 0} blueprint={blueprint} reveal={reveal} world={world} />
          </div>
        </div>
      )}
    </PageShell>
  );
}
