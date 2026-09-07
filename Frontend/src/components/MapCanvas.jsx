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
import { MapView, WebMercatorViewport } from '@deck.gl/core';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { TYPE_GLYPH, riskColor, rgba } from '../lib/colors.js';
import { minutes, percent } from '../lib/format.js';
import { buildLabels, labelTier, nodeRadiusPx } from '../lib/labels.js';
import { useStore } from '../store/useStore.js';

const STEP_REVEAL_MS = 400; // 02 §5.3.2 — 400ms stagger between cascade steps
const MIN_ARC_OPACITY = 0.35; // clamp so nothing is invisible
const WHATIF_RGB = [56, 189, 248]; // sky-400 — the "simulated / not live" colour

/**
 * One shared pulse phase (0..1) for the action rings and effect edges. Mirrors
 * `useCascadeReveal`: a single rAF loop, running ONLY while something is
 * animating, driving one number the layers read through `updateTriggers`.
 */
function useActionPulse(active) {
  const [phase, setPhase] = useState(0);
  const frame = useRef(null);

  useEffect(() => {
    if (!active) {
      setPhase(0);
      return undefined;
    }
    const start = performance.now();
    // ~12fps is plenty for a breathing ring and keeps React re-renders cheap.
    let last = 0;
    const tick = (now) => {
      if (now - last > 80) {
        last = now;
        setPhase((Math.sin((now - start) / 500) + 1) / 2);
      }
      frame.current = requestAnimationFrame(tick);
    };
    frame.current = requestAnimationFrame(tick);
    return () => {
      if (frame.current) cancelAnimationFrame(frame.current);
    };
  }, [active]);

  return phase;
}

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

const FALLBACK_VIEW_STATE = {
  longitude: 72.8777,
  latitude: 19.076,
  zoom: 12.5,
  pitch: 0,
  bearing: 0,
};

function fitViewState(bounds, width = 900, height = 600) {
  // Fit-to-view from `graph.bounds` — never recomputed from node coordinates.
  // deck.gl's own WebMercatorViewport.fitBounds projects the vertical axis
  // correctly; the previous hand-rolled `log2((180 * height) / ...)` treated
  // latitude as linear, under-zoomed by ~1 level, and left the 66-node graph a
  // small clump in the centre of the canvas — too tight for any label scheme.
  if (!bounds || !width || !height) return { ...FALLBACK_VIEW_STATE };
  try {
    const fitted = new WebMercatorViewport({ width, height }).fitBounds(
      [
        [bounds.min_lon, bounds.min_lat],
        [bounds.max_lon, bounds.max_lat],
      ],
      { padding: 48 },
    );
    return {
      longitude: fitted.longitude,
      latitude: fitted.latitude,
      zoom: fitted.zoom,
      pitch: 0,
      bearing: 0,
    };
  } catch {
    return { ...FALLBACK_VIEW_STATE };
  }
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
    trackedActions,
    whatIfOverlay,
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

  // Controlled view state: the label tier and the declutter projection both
  // need to read live zoom, which an uncontrolled DeckGL does not expose.
  const [viewState, setViewState] = useState(() => fitViewState(null));

  // Re-fit only when the topology or the container size changes — never on a
  // sim tick, so a pan/zoom the operator has made is not yanked back.
  useEffect(() => {
    if (!size.width || !size.height) return;
    setViewState(fitViewState(graph.bounds, size.width, size.height));
  }, [graph.bounds, size.width, size.height]);

  const handleViewStateChange = useCallback(({ viewState: next }) => {
    setViewState(next);
  }, []);

  const cascade = activeCascadeRootId ? cascades[activeCascadeRootId] : null;
  const revealed = useCascadeReveal(cascade);

  // --- action visualisation (TASK 2) ------------------------------------
  const executedActions = useMemo(
    () => trackedActions.filter((a) => a.kind === 'executed'),
    [trackedActions],
  );
  const actionPulseActive =
    executedActions.some((a) => a.phase !== 'settled') || Boolean(whatIfOverlay);
  const pulse = useActionPulse(actionPulseActive);

  // Rings sit on the entities an action is acting on. Executed = the target
  // set (white→risk); what-if = the projection's affected nodes (cyan).
  const actionRings = useMemo(() => {
    const rings = [];
    for (const action of executedActions) {
      for (const id of action.targetIds) {
        const node = nodesById[id];
        if (!node) continue;
        rings.push({ entity_id: id, position: [node.lon, node.lat], kind: 'executed', node });
      }
    }
    if (whatIfOverlay?.result) {
      const r = whatIfOverlay.result;
      const ids = new Set([
        ...(r.delta?.new_critical_entities || []),
        ...(r.scenario?.peak_entity_id ? [r.scenario.peak_entity_id] : []),
        ...((r.cascade?.steps || []).map((s) => s.entity_id)),
      ]);
      for (const id of ids) {
        const node = nodesById[id];
        if (!node) continue;
        rings.push({ entity_id: id, position: [node.lon, node.lat], kind: 'whatif', node });
      }
    }
    return rings;
  }, [executedActions, whatIfOverlay, nodesById]);

  // Effect edges — only downstream links where the neighbour genuinely eased
  // after the action (lib/actionEffects.downstreamEffects). Never decorative.
  const actionEffectEdges = useMemo(() => {
    const out = [];
    for (const action of executedActions) {
      for (const d of action.live.downstream || []) {
        const src = nodesById[d.srcId];
        const dst = nodesById[d.entity_id];
        if (!src || !dst) continue;
        out.push({ path: [[src.lon, src.lat], [dst.lon, dst.lat]] });
      }
    }
    return out;
  }, [executedActions, nodesById]);

  // The what-if projected cascade path, drawn as cyan arcs (distinct from the
  // live orange→red cascade), no reveal animation — it is a projection.
  const whatIfArcs = useMemo(() => {
    const steps = whatIfOverlay?.result?.cascade?.steps || [];
    return steps
      .filter((step) => step.via_edge_id)
      .map((step) => {
        const edge = graph.edges.find((e) => e.edge_id === step.via_edge_id);
        const src = nodesById[edge?.src_entity_id];
        const dst = nodesById[step.entity_id];
        if (!src || !dst) return null;
        return { source: [src.lon, src.lat], target: [dst.lon, dst.lat] };
      })
      .filter(Boolean);
  }, [whatIfOverlay, graph.edges, nodesById]);

  const actionTargetIds = useMemo(() => {
    const ids = new Set();
    for (const a of executedActions) a.targetIds.forEach((id) => ids.add(id));
    for (const r of actionRings) ids.add(r.entity_id);
    return ids;
  }, [executedActions, actionRings]);

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

  // --- persistent entity labels (02 §5.2) --------------------------------
  // Names visible without hover, kept readable at ~66 entities by a zoom tier
  // plus a greedy screen-space declutter. See lib/labels.js.
  const tier = labelTier(viewState.zoom);
  // Bucket zoom so a smooth pinch triggers ~4 declutter passes per level, not
  // one per animation frame.
  const zoomBucket = Math.round(viewState.zoom * 4) / 4;

  const declutterViewport = useMemo(() => {
    const b = graph.bounds;
    if (!b || !size.width || !size.height) return null;
    try {
      // Centred on the fixed graph bounds, not the live camera — placement is
      // then pan-invariant: a pan moves every label with its node but never
      // reshuffles which names survive.
      return new WebMercatorViewport({
        width: size.width,
        height: size.height,
        longitude: (b.min_lon + b.max_lon) / 2,
        latitude: (b.min_lat + b.max_lat) / 2,
        zoom: zoomBucket,
      });
    } catch {
      return null;
    }
  }, [graph.bounds, size.width, size.height, zoomBucket]);

  const pinnedIds = useMemo(() => {
    const ids = new Set();
    if (selectedEntityId) ids.add(selectedEntityId);
    if (hovered?.entity_id) ids.add(hovered.entity_id);
    // An entity an action is acting on always keeps its label.
    actionTargetIds.forEach((id) => ids.add(id));
    return ids;
  }, [selectedEntityId, hovered, actionTargetIds]);

  // A risk_band only changes when an entity crosses a threshold — rare. Keying
  // the placement memo on this string (not `nodeData`, a fresh reference every
  // tick) keeps declutter off the 30s cycle while still re-ranking when a band
  // actually shifts.
  const bandSignature = useMemo(
    () => nodeData.map((n) => `${n.entity_id}:${n.risk_band}`).join(','),
    [nodeData],
  );

  const labelData = useMemo(
    () => buildLabels({ nodes: nodeData, viewport: declutterViewport, tier, pinnedIds }),
    // `nodeData` is read for geometry and band rank but is deliberately not a
    // dependency — `bandSignature` is the stable proxy. See note above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [bandSignature, declutterViewport, tier, pinnedIds],
  );

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
      // Radius scales with nominal_capacity so a station reads bigger than a
      // post. Shared with the label placement so offsets clear the dot.
      getRadius: (d) => nodeRadiusPx(d),
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
    new TextLayer({
      id: 'entity-labels',
      data: labelData,
      getPosition: (d) => d.position,
      getText: (d) => {
        if (tier !== 'detailed') return d.displayName;
        const state = entities[d.entity_id];
        return state && state.utilisation != null
          ? `${d.displayName}  ${percent(state.utilisation)}`
          : d.displayName;
      },
      getSize: (d) => d.size,
      // Colour follows the risk_band string the API returned (colors.js rule):
      // quiet slate while low/moderate, the entity's own risk colour once it is
      // high/critical. Estimated entities dim, matching the scatterplot fill.
      getColor: (d) => {
        const state = entities[d.entity_id];
        const band = state?.risk_band ?? 'low';
        const rgb =
          band === 'high' || band === 'critical'
            ? rgba(riskColor(band).hex, 255)
            : [203, 213, 225, 255];
        const observed = state?.is_observed ?? false;
        return observed ? rgb : [rgb[0], rgb[1], rgb[2], 178];
      },
      getPixelOffset: (d) => d.pixelOffset,
      getTextAnchor: (d) => d.anchor,
      getAlignmentBaseline: (d) => d.baseline,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      fontWeight: 600,
      fontSettings: { sdf: true },
      outlineWidth: 0.22,
      outlineColor: [11, 15, 23, 255], // surface-900 halo, no per-node box
      pickable: false,
      updateTriggers: {
        getText: [tier, simTime],
        getColor: [simTime],
      },
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

    // --- action visualisation (TASK 2) --------------------------------------
    // Downstream links where the neighbour genuinely eased after the action.
    // Empty unless `downstreamEffects` found a real move — never decorative.
    new PathLayer({
      id: 'action-effect-edges',
      data: actionEffectEdges,
      getPath: (d) => d.path,
      getColor: [34, 197, 94, Math.round(120 + pulse * 110)], // green, breathing
      getWidth: 2.4,
      widthUnits: 'pixels',
      pickable: false,
      updateTriggers: { getColor: [pulse] },
    }),
    // The what-if projected cascade — cyan arcs, no reveal (it is a projection).
    new ArcLayer({
      id: 'whatif-arcs',
      data: whatIfArcs,
      getSourcePosition: (d) => d.source,
      getTargetPosition: (d) => d.target,
      getSourceColor: [...WHATIF_RGB, 150],
      getTargetColor: [...WHATIF_RGB, 210],
      getWidth: 2,
      getHeight: 0.35,
      widthUnits: 'pixels',
    }),
    // Rings mark the entities an action is acting on. Pulsing, hollow, on top —
    // so "THIS is the target" always reads, without hiding the dot or its label.
    new ScatterplotLayer({
      id: 'action-rings',
      data: actionRings,
      getPosition: (d) => d.position,
      getRadius: (d) => nodeRadiusPx(d.node) + 7 + pulse * 5,
      radiusUnits: 'pixels',
      stroked: true,
      filled: false,
      getLineColor: (d) =>
        d.kind === 'whatif'
          ? [...WHATIF_RGB, Math.round(140 + pulse * 90)]
          : [255, 255, 255, Math.round(150 + pulse * 90)],
      getLineWidth: 2.5,
      lineWidthUnits: 'pixels',
      pickable: false,
      updateTriggers: { getRadius: [pulse], getLineColor: [pulse] },
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
        viewState={viewState}
        onViewStateChange={handleViewStateChange}
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
