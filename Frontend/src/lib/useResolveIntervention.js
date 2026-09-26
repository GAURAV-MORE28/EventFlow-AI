/**
 * Approve / reject an intervention — the ONE client path for both the
 * Intervention Queue and the observer decision banner (Phase 0), so both get
 * the same T0 snapshot, action tracking, optimistic status and rollback.
 */
import { useState } from 'react';

import { api } from './api.js';
import { snapshotTargets } from './actionEffects.js';
import { useStore } from '../store/useStore.js';

export function useResolveIntervention() {
  const {
    updateInterventionStatus,
    toast,
    entities,
    cascades,
    simTime,
    cycleNumber,
    mockMode,
    beginExecutedAction,
    noteRejectedAction,
    setActiveCascadeRoot,
  } = useStore();
  const [busyId, setBusyId] = useState(null);

  /** Returns true when the backend accepted the decision. */
  async function resolve(intervention, action) {
    const id = intervention.intervention_id;
    setBusyId(id);
    // Optimistic: flip locally, then reconcile on the `intervention_resolved`
    // WS event (02 §5.5). On failure we roll the status back.
    const optimistic = action === 'approve' ? 'executing' : 'rejected';
    updateInterventionStatus(id, optimistic);

    try {
      if (action === 'approve') {
        // Snapshot the targets' live state NOW, before apply_relief lands on the
        // next cycle — this is the T0 the ActionHUD/map diff against (TASK 2).
        const rootId = intervention.triggered_by_entity_id || intervention.target_entity_ids?.[0];
        const snapshot = snapshotTargets(entities, intervention.target_entity_ids || []);
        const result = await api.approve(id);
        beginExecutedAction({
          intervention,
          snapshot,
          t0Cascade: rootId ? cascades[rootId] || null : null,
          simTime,
          cycleNumber,
          mock: mockMode,
        });
        // Clear any active cascade overlay so the map shows a clean view.
        // The backend's apply_relief will reduce demand on the next cycle,
        // and the resulting entity-state changes will be visible as updated
        // node colours once the next state_update WS event arrives.
        setActiveCascadeRoot(null);
        toast(`Approved — ${result.nudges_issued} nudges issued`, 'success');
      } else {
        await api.reject(id, 'op_demo', 'Rejected by operator');
        noteRejectedAction(intervention);
        toast('Intervention rejected', 'info');
      }
      return true;
    } catch (error) {
      updateInterventionStatus(id, 'proposed');
      if (error.code === 'INTERVENTION_ALREADY_RESOLVED') {
        toast('This intervention was already resolved.', 'error');
      } else if (error.code === 'INTERVENTION_EXPIRED') {
        toast('This intervention has expired.', 'error');
      } else {
        toast(error.message, 'error');
      }
      return false;
    } finally {
      setBusyId(null);
    }
  }

  return { resolve, busyId };
}
