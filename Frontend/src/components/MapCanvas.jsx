/**
 * MapCanvas — 02_FRONTEND_CONTRACT.md §5.2, with the CascadeOverlay of §5.3
 * folded in as Deck.gl layers (they share a projection, so splitting them into
 * two canvases would only introduce drift).
 *
 * Layer order matches the contract table: entities, type icons, static feed
 * edges, cascade arcs, ETA labels.
 *
 * Deck.gl runs without a base map on purpose. A Mapbox style needs a token, and
 * a demo that dies because a token expired is a bad trade for a background
 * texture. Set `VITE_MAP_STYLE` if you want one later.
 *
 * Performance (02 §10): layers are rebuilt only when `updateTriggers` change,
 * and the arc reveal uses `requestAnimationFrame`, never `setInterval`.
 */
import DeckGL from '@deck.gl/react';
import { ArcLayer, PathLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers';
import { MapView } from '@deck.gl/core';
import { useEffect, useMemo, useRef, useState } from 'react';

import { TYPE_GLYPH, riskColor, rgba } from '../lib/colors.js';
import { minutes, percent } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

const STEP_REVEAL_MS = 400; // 02 §5.3.2 — 400ms stagger between cascade steps
const MIN_ARC_OPACITY = 0.35; // clamp so nothing is invisible

function useCascadeReveal(cascade) {
  const [revealed, setRevealed] = useState(0);
  const frame = useRef(null);

  useEffect(() => {
    if (!cascade || cascade.steps.length === 0) {
      setRevealed(0);
      return undefined;
    }
    const start = performance.now();
    const total = cascade.steps.length;
    setRevealed(0);

    // requestAnimationFrame, not setInterval: the reveal stays in step with the
    // compositor and does not drift when the tab is busy.
    const tick = (now) => {
      const elapsed = now - start;
      const count = Math.min(total, Math.floor(elapsed / STEP_REVEAL_MS) + 1);
      setRevealed(count);
      if (count < total) frame.current = requestAnimationFrame(tick);
    };
    frame.current = requestAnimationFrame(tick);

    return () => {
      if (frame.current) cancelAnimationFrame(frame.current);
    };
  }, [cascade?.root_entity_id, cascade?.generated_at, cascade?.steps.length]);

  return revealed;
}

function fitViewState(bounds, width, height) {
  if (!bounds) return { longitude: 72.8777, latitude: 19.076, zoom: 12.5, pitch: 0, bearing: 0 };
  const { min_lat, max_lat, min_lon, max_lon } = bounds;
  const longitude = (min_lon + max_lon) / 2;
  const latitude = (min_lat + max_lat) / 2;

  // Fit-to-view from `graph.bounds` — never recomputed from node coordinates.
  const lonSpan = Math.max(max_lon - min_lon, 1e-6);
  const latSpan = Math.max(max_lat - min_lat, 1e-6);
  const zoomLon = Math.log2((360 * (width || 900)) / (lonSpan * 512));
  const zoomLat = Math.log2((180 * (height || 600)) / (latSpan * 512));
  return {
    longitude,
    latitude,
    zoom: Math.min(zoomLon, zoomLat) - 0.35,
    pitch: 0,
    bearing: 0,
  };
}

export default function MapCanvas() {
  const {
    graph,
    nodesById,
    entities,
    cascades,
    activeCascadeRootId,
    selectedEntityId,
    selectEntity,
    setActiveCascadeRoot,
    simTime,
  } = useStore();

  const containerRef = useRef(null);
  const [size, setSize] = useState({ width: 900, height: 600 });
  const [hovered, setHovered] = useState(null);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return undefined;
    const observer = new ResizeObserver(([entry]) => {
      setSize({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const viewState = useMemo(
    () => fitViewState(graph.bounds, size.width, size.height),
    [graph.bounds, size.width, size.height],
  );

  const cascade = activeCascadeRootId ? cascades[activeCascadeRootId] : null;
  const revealed = useCascadeReveal(cascade);

  // Join topology to live state by entity_id. `entities` is a merge target, so a
  // node missing from the latest delta keeps its previous state automatically.
  const nodeData = useMemo(
    () =>
      graph.nodes.map((node) => {
        const state = entities[node.entity_id];
        return {
          ...node,
          utilisation: state?.utilisation ?? 0,
          risk_band: state?.risk_band ?? 'low',
          risk_score: state?.risk_score ?? 0,
          is_observed: state?.is_observed ?? false,
        };
      }),
    [graph.nodes, entities],
  );

  const feedEdges = useMemo(
    () =>
      graph.edges
        .filter((e) => e.edge_type === 'feeds')
        .map((e) => {
          const src = nodesById[e.src_entity_id];
          const dst = nodesById[e.dst_entity_id];
          if (!src || !dst) return null;
          return { ...e, path: [[src.lon, src.lat], [dst.lon, dst.lat]] };
        })
        .filter(Boolean),
    [graph.edges, nodesById],
  );

  const arcData = useMemo(() => {
    if (!cascade) return [];
    return cascade.steps
      .slice(0, revealed)
      .filter((step) => step.via_edge_id)
      .map((step) => {
        const edge = graph.edges.find((e) => e.edge_id === step.via_edge_id);
        const src = nodesById[edge?.src_entity_id];
        const dst = nodesById[step.entity_id];
        if (!src || !dst) return null;
        return {
          ...step,
          source: [src.lon, src.lat],
          target: [dst.lon, dst.lat],
          // Arc opacity encodes failure_probability, clamped so it is visible.
          opacity: Math.max(MIN_ARC_OPACITY, step.failure_probability),
        };
      })
      .filter(Boolean);
  }, [cascade, revealed, graph.edges, nodesById]);

  const etaLabels = useMemo(() => {
    if (!cascade) return [];
    return cascade.steps
      .slice(0, revealed)
      .map((step) => {
        const node = nodesById[step.entity_id];
        if (!node) return null;
        return {
          position: [node.lon, node.lat],
          text: minutes(step.eta_sec),
          isRoot: step.depth === 0,
        };
      })
      .filter(Boolean);
  }, [cascade, revealed, nodesById]);

  const layers = [
    new PathLayer({
      id: 'static-edges',
      data: feedEdges,
      getPath: (d) => d.path,
      getColor: [148, 163, 184, 51], // thin grey, 20% opacity
      getWidth: 1.5,
      widthUnits: 'pixels',
      pickable: false,
    }),
    new ScatterplotLayer({
      id: 'entities',
      data: nodeData,
      getPosition: (d) => [d.lon, d.lat],
      // Radius scales with nominal_capacity so a station reads bigger than a post.
      getRadius: (d) => 6 + Math.sqrt(d.nominal_capacity) * 0.16,
      radiusUnits: 'pixels',
      getFillColor: (d) => rgba(riskColor(d.risk_band).hex, d.is_observed ? 220 : 90),
      // Estimated entities (is_observed === false) get a visible outline instead
      // of a solid fill — Deck.gl has no dashed stroke, so the weaker fill plus a
      // bright ring carries the same "this is an estimate" signal.
      stroked: true,
      getLineColor: (d) =>
        d.is_observed ? rgba(riskColor(d.risk_band).hex, 255) : [226, 232, 240, 200],
      getLineWidth: (d) => (d.is_observed ? 1 : 2),
      lineWidthUnits: 'pixels',
      pickable: true,
      onHover: ({ object }) => setHovered(object || null),
      onClick: ({ object }) => {
        if (!object) return;
        selectEntity(object.entity_id);
        setActiveCascadeRoot(object.entity_id);
      },
      updateTriggers: {
        getFillColor: [simTime],
        getLineColor: [simTime],
        getLineWidth: [simTime],
        getRadius: [simTime],
      },
    }),
    new TextLayer({
      id: 'types',
      data: nodeData,
      getPosition: (d) => [d.lon, d.lat],
      getText: (d) => TYPE_GLYPH[d.entity_type] || '•',
      getSize: 11,
      getColor: [10, 15, 25, 220],
      getTextAnchor: 'middle',
      getAlignmentBaseline: 'center',
      pickable: false,
    }),
    new ArcLayer({
      id: 'cascade',
      data: arcData,
      getSourcePosition: (d) => d.source,
      getTargetPosition: (d) => d.target,
      getSourceColor: (d) => [249, 115, 22, Math.round(d.opacity * 255)],
      getTargetColor: (d) => [239, 68, 68, Math.round(d.opacity * 255)],
      getWidth: 3,
      getHeight: 0.5,
      widthUnits: 'pixels',
      updateTriggers: { getSourceColor: [revealed], getTargetColor: [revealed] },
    }),
    new TextLayer({
      id: 'eta',
      data: etaLabels,
      getPosition: (d) => d.position,
      getText: (d) => d.text,
      getSize: 12,
      getColor: (d) => (d.isRoot ? [252, 165, 165, 255] : [248, 250, 252, 230]),
      getPixelOffset: [0, -22], // offset above the node
      getTextAnchor: 'middle',
      fontWeight: 700,
      background: true,
      getBackgroundColor: [11, 15, 23, 190],
      backgroundPadding: [4, 2],
      updateTriggers: { getText: [revealed, simTime] },
    }),
  ];

  return (
    <div ref={containerRef} className="relative h-full w-full overflow-hidden rounded-lg bg-surface-900">
      {/* A faint grid stands in for a base map: enough spatial reference to read
          the layout, no external tiles and no token to expire mid-demo. */}
      <div
        className="pointer-events-none absolute inset-0 opacity-[0.13]"
        style={{
          backgroundImage:
            'linear-gradient(#334155 1px, transparent 1px), linear-gradient(90deg, #334155 1px, transparent 1px)',
          backgroundSize: '48px 48px',
        }}
      />

      <DeckGL
        views={new MapView({ repeat: false })}
        initialViewState={viewState}
        viewState={undefined}
        controller={{ dragRotate: false }}
        layers={layers}
        getCursor={({ isHovering }) => (isHovering ? 'pointer' : 'grab')}
      />

      {hovered && (
        <div className="pointer-events-none absolute left-3 top-3 rounded-md border border-surface-600 bg-surface-800/95 px-3 py-2 shadow-lg">
          <div className="text-sm font-semibold text-slate-100">{hovered.display_name}</div>
          <div className="mt-0.5 flex items-center gap-2 text-[11px]">
            <span className="tabular-nums text-slate-300">{percent(hovered.utilisation)}</span>
            <span style={{ color: riskColor(hovered.risk_band).hex }}>{hovered.risk_band}</span>
            {!hovered.is_observed && <span className="text-slate-500">estimated</span>}
          </div>
        </div>
      )}

      {cascade && cascade.total_downstream_failures === 0 && (
        <div className="pointer-events-none absolute bottom-3 left-1/2 -translate-x-1/2 rounded-md border border-surface-600 bg-surface-800/95 px-3 py-1.5 text-[11px] text-slate-400">
          No downstream propagation predicted.
        </div>
      )}

      <div className="pointer-events-none absolute bottom-3 right-3 flex flex-col gap-1 rounded-md border border-surface-600 bg-surface-800/90 px-2.5 py-2 text-[10px]">
        {['low', 'moderate', 'high', 'critical'].map((band) => (
          <div key={band} className="flex items-center gap-1.5">
            <span
              className="h-2 w-2 rounded-full"
              style={{ backgroundColor: riskColor(band).hex }}
            />
            <span className="capitalize text-slate-400">{band}</span>
          </div>
        ))}
        <div className="mt-1 flex items-center gap-1.5 border-t border-surface-600 pt-1">
          <span className="h-2 w-2 rounded-full border border-slate-300 bg-transparent" />
          <span className="text-slate-500">estimated</span>
        </div>
      </div>

      {selectedEntityId && (
        <div className="pointer-events-none absolute right-3 top-3 rounded bg-surface-800/90 px-2 py-1 text-[10px] text-slate-400">
          tracking {selectedEntityId}
        </div>
      )}
    </div>
  );
}
