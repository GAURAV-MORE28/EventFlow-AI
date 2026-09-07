/**
 * Entity label placement for the MapCanvas node graph.
 *
 * Pure module — no React, no deck.gl layer knowledge. Given a projected
 * viewport and a zoom tier it decides *which* of the ~66 entities show a
 * persistent name this frame and *where* the text sits relative to its dot, so
 * a dense graph stays readable without hover.
 *
 * Nothing here derives a colour or a band from a number. `labelPriority` is an
 * ordering only (the colors.js / 02 §3 rule); the caller renders the text in
 * whatever risk colour the API already returned. This module also never
 * re-sorts a list the backend owns — its ordering decides label survival under
 * crowding and nothing else.
 */
import { humanise } from './format.js';

const COMPACT_MAX_ZOOM = 12.6;
const DETAILED_MIN_ZOOM = 14.0;

// Hard label ceilings per tier. `normal` is the fit-to-view default: with ~66
// overlapping capacity-scaled dots there is no legible way to show every name
// at once, so the highest-priority set wins and the rest reveal on zoom-in.
const TIER = {
  compact: { cap: 18, size: 10 },
  normal: { cap: 34, size: 11 },
  detailed: { cap: Infinity, size: 11 },
};

// deck.gl's default Monaco/monospace face has a near-constant advance ratio, so
// a character count is an accurate width estimate — no canvas measureText.
const CHAR_ADVANCE = 0.58;
const LINE_HEIGHT = 1.2;
const BOX_PAD_X = 6;
const BOX_PAD_Y = 4;
const LABEL_GAP = 4; // node edge -> label edge
const OBSTACLE_SHRINK = 0.8; // dots block placement at 80% radius, so labels can tuck close

// Read as-is from the API string. Never keyed off risk_score.
const BAND_RANK = { critical: 400, high: 300, moderate: 150, low: 0 };

const TYPE_WEIGHT = {
  venue: 90,
  transport_node: 70,
  gate: 60,
  zone: 50,
  emergency_facility: 40,
  hotel: 25,
  parking: 25,
  transport_route: 15,
  road: 10,
};

// Candidate anchors, tried in this order. `place` drives both the deck.gl
// pixel offset and the collision box; the actual distance is the node's own
// rendered radius plus LABEL_GAP, so a big dot pushes its label clear.
const ANCHOR_DEFS = [
  { place: 'above', anchor: 'middle', baseline: 'bottom' },
  { place: 'below', anchor: 'middle', baseline: 'top' },
  { place: 'right', anchor: 'start', baseline: 'center' },
  { place: 'left', anchor: 'end', baseline: 'center' },
];

const EMPTY_SET = new Set();

/**
 * Rendered dot radius in pixels — mirrors the ScatterplotLayer `getRadius` in
 * MapCanvas.jsx (`radiusUnits: 'pixels'`). Exported so both stay in lockstep.
 */
export function nodeRadiusPx(node) {
  const cap = Number(node.nominal_capacity);
  return 6 + Math.sqrt(cap > 0 ? cap : 0) * 0.16;
}

/** `< 12.6` compact · `< 14.0` normal · `>= 14.0` detailed. */
export function labelTier(zoom) {
  if (zoom < COMPACT_MAX_ZOOM) return 'compact';
  if (zoom < DETAILED_MIN_ZOOM) return 'normal';
  return 'detailed';
}

/**
 * Ordering score — a higher score keeps its label when space runs out.
 * Combines the `risk_band` string rank, an entity-type weight, and
 * `log10(nominal_capacity)` as a tiebreak.
 */
export function labelPriority(node) {
  const band = BAND_RANK[node.risk_band] ?? 0;
  const type = TYPE_WEIGHT[node.entity_type] ?? 20;
  const capacity = Number(node.nominal_capacity) > 0 ? Math.log10(Number(node.nominal_capacity)) : 0;
  return band + type + capacity;
}

function labelName(node) {
  return node.display_name || humanise(node.entity_id);
}

/** Widest string the label can ever hold in this tier — geometry only. */
function measuredWidth(node, tier, size) {
  const chars = labelName(node).length + (tier === 'detailed' ? 6 : 0); // "  100%"
  return chars * size * CHAR_ADVANCE + BOX_PAD_X;
}

function anchorGeometry(place, nx, ny, r, w, h) {
  const gap = r + LABEL_GAP;
  switch (place) {
    case 'above':
      return { pixelOffset: [0, -gap], box: [nx - w / 2, ny - gap - h, nx + w / 2, ny - gap] };
    case 'below':
      return { pixelOffset: [0, gap], box: [nx - w / 2, ny + gap, nx + w / 2, ny + gap + h] };
    case 'right':
      return { pixelOffset: [gap, 0], box: [nx + gap, ny - h / 2, nx + gap + w, ny + h / 2] };
    case 'left':
      return { pixelOffset: [-gap, 0], box: [nx - gap - w, ny - h / 2, nx - gap, ny + h / 2] };
    default:
      return { pixelOffset: [0, 0], box: [nx, ny, nx, ny] };
  }
}

function overlaps(a, b) {
  return !(a[2] <= b[0] || b[2] <= a[0] || a[3] <= b[1] || b[3] <= a[1]);
}

/**
 * @param nodes     nodeData (topology joined to live state)
 * @param viewport  a deck.gl WebMercatorViewport for screen projection
 * @param tier      'compact' | 'normal' | 'detailed'
 * @param pinnedIds Set of entity_ids that must get a label (selected / hovered)
 * @returns [{ entity_id, position:[lon,lat], displayName, size, pixelOffset, anchor, baseline }]
 */
export function buildLabels({ nodes, viewport, tier, pinnedIds }) {
  if (!nodes || nodes.length === 0 || !viewport) return [];

  const pinned = pinnedIds || EMPTY_SET;
  const { cap, size } = TIER[tier] || TIER.normal;

  const scored = nodes
    .map((node) => {
      const [x, y] = viewport.project([node.lon, node.lat]);
      const isPinned = pinned.has(node.entity_id);
      return {
        node,
        x,
        y,
        r: nodeRadiusPx(node),
        isPinned,
        priority: isPinned ? Infinity : labelPriority(node),
      };
    })
    .filter((s) => Number.isFinite(s.x) && Number.isFinite(s.y));

  // Deterministic: priority desc, then entity_id asc so two entities in the
  // same band never trade places frame to frame.
  scored.sort((a, b) => {
    if (b.priority !== a.priority) return b.priority - a.priority;
    return a.node.entity_id < b.node.entity_id ? -1 : 1;
  });

  // Seed the occupied set with every dot, so a label is never placed on top of
  // a node marker (its own or a neighbour's).
  const placed = scored.map((s) => {
    const r = s.r * OBSTACLE_SHRINK;
    return [s.x - r, s.y - r, s.x + r, s.y + r];
  });

  const out = [];
  for (const item of scored) {
    if (out.length >= cap && !item.isPinned) continue;

    const w = measuredWidth(item.node, tier, size);
    const h = size * LINE_HEIGHT + BOX_PAD_Y;

    let chosen = null;
    for (const def of ANCHOR_DEFS) {
      const geom = anchorGeometry(def.place, item.x, item.y, item.r, w, h);
      if (!placed.some((p) => overlaps(geom.box, p))) {
        chosen = { def, geom };
        placed.push(geom.box);
        break;
      }
    }

    // A pinned entity always gets a label, even if every anchor is contested.
    if (!chosen && item.isPinned) {
      const geom = anchorGeometry('above', item.x, item.y, item.r, w, h);
      chosen = { def: ANCHOR_DEFS[0], geom };
      placed.push(geom.box);
    }
    if (!chosen) continue;

    out.push({
      entity_id: item.node.entity_id,
      position: [item.node.lon, item.node.lat],
      displayName: labelName(item.node),
      size,
      pixelOffset: chosen.geom.pixelOffset,
      anchor: chosen.def.anchor,
      baseline: chosen.def.baseline,
    });
  }

  return out;
}
