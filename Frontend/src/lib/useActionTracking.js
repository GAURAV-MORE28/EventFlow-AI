/**
 * Drives the action-visualisation lifecycle (TASK 2). Mounted once by
 * CommandCentre. Every value it writes is derived from live simulation state
 * the store already holds, plus on-demand reads of endpoints that already
 * exist — it starts no simulation of its own.
 *
 *   approve ──▶ store.beginExecutedAction (T0 snapshot)
 *      │
 *   each tick ──▶ recompute target deltas + downstream effects from `entities`
 *      │          re-fetch GET /cascade/{root} for the reduced-cascade signal
 *      │          (the server does not re-broadcast a shrinking cascade — H4)
 *      ▼
 *   settle  ──▶ regret_update WS event (engine B1) folds in realised vs
 *               do-nothing; GET /regret is the fallback if that never arrives.
 */
import { useEffect, useRef } from 'react';

import { api } from './api.js';
import {
  actionPhase,
  cascadeDelta,
  downstreamEffects,
  targetDeltas,
} from './actionEffects.js';
import { useStore } from '../store/useStore.js';

export function useActionTracking() {
  const trackedActions = useStore((s) => s.trackedActions);
  const entities = useStore((s) => s.entities);
  const edges = useStore((s) => s.graph.edges);
  const cascades = useStore((s) => s.cascades);
  const cycleNumber = useStore((s) => s.cycleNumber);
  const updateActionLive = useStore((s) => s.updateActionLive);
  const setActionPhase = useStore((s) => s.setActionPhase);
  const pruneActions = useStore((s) => s.pruneActions);

  const cascadeFetchedAt = useRef({});
  const regretFallbackDone = useRef({});

  const executedKey = trackedActions
    .filter((a) => a.kind === 'executed')
    .map((a) => a.id)
    .join(',');

  // Recompute the truthful before/after every time the sim clock moves.
  useEffect(() => {
    for (const action of trackedActions) {
      if (action.kind !== 'executed') continue;

      const deltas = targetDeltas(action.t0.byEntity, entities);
      const downstream = downstreamEffects({
        edges,
        targetIds: action.targetIds,
        t0Entities: action.t0.byEntity,
        entities,
      });
      const nowCascade = (action.rootId && cascades[action.rootId]) || action.live.nowCascade || null;
      const cascade =
        action.t0.cascade || nowCascade ? cascadeDelta(action.t0.cascade, nowCascade) : null;

      updateActionLive(action.id, { deltas, downstream, cascade, nowCascade });

      if (!action.settled) {
        const phase = actionPhase(action, cycleNumber);
        if (phase !== action.phase) setActionPhase(action.id, phase);
      }
    }
    // `entities`/`edges`/`cascades` are read fresh from the closure; the sim
    // clock (cycleNumber) advancing is what makes them worth re-reading.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cycleNumber, executedKey]);

  // Live "cascade reduced / prevented" read.
  useEffect(() => {
    let cancelled = false;
    for (const action of trackedActions) {
      if (action.kind !== 'executed' || action.phase === 'settled' || !action.rootId) continue;
      if (cascadeFetchedAt.current[action.id] === cycleNumber) continue;
      cascadeFetchedAt.current[action.id] = cycleNumber;

      api
        .cascade(action.rootId)
        .then((c) => {
          if (cancelled) return;
          updateActionLive(action.id, {
            nowCascade: c,
            cascade: cascadeDelta(action.t0.cascade, c),
          });
        })
        .catch(() => {});
    }
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cycleNumber, executedKey]);

  // Settlement fallback for a backend without the regret_update broadcast.
  useEffect(() => {
    for (const action of trackedActions) {
      if (action.kind !== 'executed' || action.settled) continue;
      if (regretFallbackDone.current[action.id]) continue;
      if (actionPhase(action, cycleNumber) !== 'settling') continue;

      regretFallbackDone.current[action.id] = true;
      api
        .regret()
        .then((r) => {
          const entry = (r.entries || []).find(
            (e) => e.intervention_id === action.interventionId,
          );
          if (entry) useStore.getState().appendRegret(entry, r.summary);
        })
        .catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cycleNumber, executedKey]);

  // Wall-clock pruning of settled / rejected entries.
  useEffect(() => {
    const timer = setInterval(() => pruneActions(Date.now()), 2000);
    return () => clearInterval(timer);
  }, [pruneActions]);
}
