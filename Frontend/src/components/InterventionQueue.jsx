/**
 * InterventionQueue — 02_FRONTEND_CONTRACT.md §5.5.
 *
 * Cards render in received order (`rank_score` desc). We do not re-sort, and we
 * do not filter UNSTABLE options out — the backend deliberately returns them and
 * showing them de-emphasised is the point.
 */
import { useState } from 'react';
import { Sliders, CheckCircle2 } from 'lucide-react';

import InterventionCard from './InterventionCard.jsx';
import { api } from '../lib/api.js';
import { snapshotTargets } from '../lib/actionEffects.js';
import { useStore } from '../store/useStore.js';

export default function InterventionQueue() {
  const {
    interventions,
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
    nodesById,
  } = useStore();
  const [busyId, setBusyId] = useState(null);

  const visible = interventions.filter((i) =>
    ['proposed', 'executing', 'approved'].includes(i.status),
  );

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
        toast(
          result.nudges_issued
            ? `Approved and applied — ${result.nudges_issued} affected attendees nudged`
            : 'Approved and applied to the live city',
          'success',
        );
      } else {
        await api.reject(id, 'op_demo', 'Rejected by operator');
        noteRejectedAction(intervention);
        toast('Intervention rejected', 'info');
      }
    } catch (error) {
      updateInterventionStatus(id, 'proposed');
      if (error.code === 'INTERVENTION_ALREADY_RESOLVED') {
        toast('This intervention was already resolved.', 'error');
      } else if (error.code === 'INTERVENTION_EXPIRED') {
        toast('This intervention has expired.', 'error');
      } else {
        toast(error.message, 'error');
      }
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="panel flex min-h-0 flex-col overflow-hidden">
      {/* Header */}
      <div className="panel-header">
        <div className="flex items-center gap-2">
          <div className="flex h-5 w-5 items-center justify-center rounded bg-amber-500/15 text-amber-400">
            <Sliders className="h-3 w-3" />
          </div>
          <div>
            <h2 className="panel-title">Intervention Queue</h2>
            <div className="text-[9px] font-mono text-slate-400 leading-none">
              DECISION-SUPPORT & EQUILIBRIUM
            </div>
          </div>
        </div>

        {visible.length > 0 ? (
          <span className="chip border border-amber-500/40 bg-amber-500/10 font-mono text-[10px] text-amber-300">
            {visible.length} PENDING
          </span>
        ) : (
          <span className="rounded bg-surface-750 px-2 py-0.5 font-mono text-[9px] text-slate-400 border border-surface-650">
            0 ACTIVE
          </span>
        )}
      </div>

      {visible.length === 0 ? (
        <div className="m-3 flex flex-1 flex-col items-center justify-center rounded-md border border-emerald-500/25 bg-emerald-500/[0.04] p-4 text-center">
          <CheckCircle2 className="h-6 w-6 text-emerald-400 mb-1.5" />
          <p className="text-xs font-semibold text-emerald-300">
            No Interventions Required
          </p>
          <p className="mt-0.5 text-[10px] text-slate-400">
            Equilibrium solver confirms system is within safe operating margins.
          </p>
        </div>
      ) : (
        <div className="flex-1 space-y-2 overflow-y-auto p-2">
          {visible.map((intervention) => (
            <InterventionCard
              key={intervention.intervention_id}
              intervention={intervention}
              busy={busyId === intervention.intervention_id}
              nodesById={nodesById}
              onApprove={(i) => resolve(i, 'approve')}
              onReject={(i) => resolve(i, 'reject')}
            />
          ))}
        </div>
      )}
    </section>
  );
}
