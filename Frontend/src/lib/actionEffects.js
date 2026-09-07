/**
 * Action-effect derivation — turns an approved intervention plus live simulation
 * state into a truthful "what changed" picture. Pure module, no React, no deck.
 *
 * Every value here is read from state the backend already produced:
 *   - `entities` is the live `EntityState` map (backend moved it via
 *     generator.apply_relief → tick → state_update).
 *   - the T0 snapshot is what those same entities looked like the moment the
 *     operator approved (captured client-side from the store).
 *
 * Nothing is interpolated or invented. If a target did not move, the delta is
 * ~0 and the UI says so. If no downstream entity improved, `downstreamEffects`
 * returns [] and the UI shows "no downstream change", never a decorative path.
 */

const BAND_RANK = { low: 0, moderate: 1, high: 2, critical: 3 };

// Flow-carrying edge types the cascade propagator itself follows (03 §4.2).
// `substitutes_for` is a negative transfer and is deliberately excluded.
const PROPAGATION_EDGES = new Set(['feeds', 'adjacent_to', 'serves', 'last_mile_to', 'evacuates_to']);

const DOWNSTREAM_MAX_HOPS = 2;
const DOWNSTREAM_MAX_EDGES = 6;
// A downstream node only counts as "affected" on a real move: a full band step,
// or a risk-score drop large enough not to be cycle-to-cycle noise.
const DOWNSTREAM_SCORE_DROP = 5;
// Below this the target delta is treated as "no measurable change".
export const MEASURABLE_PP = 1;

export function bandRank(band) {
  return BAND_RANK[band] ?? 0;
}

/** Freeze the fields we compare later, for the given ids. */
export function snapshotTargets(entities, ids) {
  const out = {};
  for (const id of ids || []) {
    const s = entities[id];
    if (!s) continue;
    out[id] = {
      utilisation: s.utilisation,
      risk_band: s.risk_band,
      risk_score: s.risk_score,
      is_observed: s.is_observed,
    };
  }
  return out;
}

/**
 * Per-target before/after, straight from state.
 * @returns [{ entity_id, utilFrom, utilTo, deltaPp, bandFrom, bandTo, bandChanged, improved, measurable }]
 */
export function targetDeltas(t0ByEntity, entities) {
  const rows = [];
  for (const [id, before] of Object.entries(t0ByEntity || {})) {
    const now = entities[id];
    if (!now) continue;
    const deltaPp = Math.round((now.utilisation - before.utilisation) * 100);
    const bandChanged = now.risk_band !== before.risk_band;
    rows.push({
      entity_id: id,
      utilFrom: before.utilisation,
      utilTo: now.utilisation,
      deltaPp,
      bandFrom: before.risk_band,
      bandTo: now.risk_band,
      bandChanged,
      // "improved" = utilisation fell or band eased.
      improved: deltaPp < 0 || bandRank(now.risk_band) < bandRank(before.risk_band),
      worsened: deltaPp > MEASURABLE_PP || bandRank(now.risk_band) > bandRank(before.risk_band),
      measurable: Math.abs(deltaPp) >= MEASURABLE_PP || bandChanged,
    });
  }
  return rows;
}

/** Adjacency for the propagation walk, restricted to flow edges. */
function buildAdjacency(edges) {
  const adj = new Map();
  for (const e of edges || []) {
    if (!PROPAGATION_EDGES.has(e.edge_type)) continue;
    if (!adj.has(e.src_entity_id)) adj.set(e.src_entity_id, []);
    adj.get(e.src_entity_id).push(e);
  }
  return adj;
}

/**
 * Downstream entities that genuinely eased after the action. BFS ≤2 hops along
 * flow edges from every target; a node qualifies only if its band stepped down
 * or its risk score dropped by more than cycle noise, measured against T0.
 *
 * @returns [{ entity_id, viaEdgeId, srcId, bandFrom, bandTo, scoreFrom, scoreTo, hop }]
 */
export function downstreamEffects({ edges, targetIds, t0Entities, entities }) {
  if (!edges || !targetIds?.length) return [];
  const adj = buildAdjacency(edges);
  const targets = new Set(targetIds);
  const seen = new Set(targetIds);
  const out = [];

  let frontier = targetIds.map((id) => ({ id, hop: 0 }));
  while (frontier.length && out.length < DOWNSTREAM_MAX_EDGES) {
    const next = [];
    for (const { id, hop } of frontier) {
      if (hop >= DOWNSTREAM_MAX_HOPS) continue;
      for (const edge of adj.get(id) || []) {
        const dst = edge.dst_entity_id;
        if (seen.has(dst) || targets.has(dst)) continue;
        seen.add(dst);
        next.push({ id: dst, hop: hop + 1 });

        const before = t0Entities[dst];
        const now = entities[dst];
        if (!before || !now) continue;
        const easedBand = bandRank(now.risk_band) < bandRank(before.risk_band);
        const easedScore = before.risk_score - now.risk_score >= DOWNSTREAM_SCORE_DROP;
        if (!easedBand && !easedScore) continue;

        out.push({
          entity_id: dst,
          viaEdgeId: edge.edge_id,
          srcId: id,
          bandFrom: before.risk_band,
          bandTo: now.risk_band,
          scoreFrom: before.risk_score,
          scoreTo: now.risk_score,
          hop: hop + 1,
        });
        if (out.length >= DOWNSTREAM_MAX_EDGES) break;
      }
    }
    frontier = next;
  }
  return out;
}

/**
 * Cascade prevented/reduced, from two CascadeResult snapshots for the same root.
 * `removedSteps` are downstream entities the propagator predicted at approval
 * and no longer predicts — the honest "this chain is less likely now" signal.
 */
export function cascadeDelta(t0Cascade, nowCascade) {
  const from = t0Cascade?.total_downstream_failures ?? null;
  const to = nowCascade?.total_downstream_failures ?? (t0Cascade ? 0 : null);
  const t0Ids = new Set((t0Cascade?.steps || []).slice(1).map((s) => s.entity_id));
  const nowIds = new Set((nowCascade?.steps || []).slice(1).map((s) => s.entity_id));
  const removedSteps = [...t0Ids].filter((id) => !nowIds.has(id));
  const addedSteps = [...nowIds].filter((id) => !t0Ids.has(id));
  return {
    from,
    to,
    changed: from !== null && to !== null && from !== to,
    reduced: from !== null && to !== null && to < from,
    removedSteps,
    addedSteps,
  };
}

/**
 * Lifecycle phase from the sim clock alone — no wall-clock animation timers.
 * settleDelaySec mirrors engine.py SETTLE_DELAY_SEC (900s).
 */
export function actionPhase(action, cycleNumber, cycleSec = 30, settleDelaySec = 900) {
  if (action.settled) return 'settled';
  if (action.kind === 'rejected') return 'rejected';
  const elapsedCycles = cycleNumber - action.t0.cycleNumber;
  const settleCycles = Math.ceil(settleDelaySec / cycleSec);
  if (elapsedCycles <= 0) return 'starting';
  if (elapsedCycles >= settleCycles) return 'settling';
  return 'propagating';
}

/** One-line verdict for the HUD, entirely from the live deltas. */
export function summarizeExecuted(action, deltas, downstream, cascade) {
  const measured = deltas.filter((d) => d.measurable);
  const anyImproved = deltas.some((d) => d.improved && d.measurable);
  const anyWorse = deltas.some((d) => d.worsened);

  if (action.settled) {
    const r = action.settled.realised_relief_pct;
    const cf = action.settled.counterfactual_relief_pct;
    if (r <= 0) return `Executed — congestion did not improve (realised ${r}pp)`;
    return `Executed — realised ${r}pp vs do-nothing ${cf}pp`;
  }
  if (!measured.length) {
    if (action.phase === 'starting') return 'Executed — applying relief…';
    return 'Executed — no measurable change yet at target';
  }
  const parts = [];
  if (anyImproved) parts.push('target easing');
  if (anyWorse) parts.push('target still rising');
  if (cascade?.reduced) parts.push(`cascade ${cascade.from}→${cascade.to}`);
  if (downstream?.length) parts.push(`${downstream.length} downstream easing`);
  else if (cascade && !cascade.reduced) parts.push('no downstream change');
  return `Executed — ${parts.join(' · ') || 'in progress'}`;
}
