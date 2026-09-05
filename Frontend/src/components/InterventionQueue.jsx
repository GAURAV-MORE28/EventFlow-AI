/**
 * InterventionQueue — 02_FRONTEND_CONTRACT.md §5.5.
 *
 * Cards render in received order (`rank_score` desc). We do not re-sort, and we
 * do not filter UNSTABLE options out — the backend deliberately returns them and
 * showing them de-emphasised is the point.
 */
import { useState } from 'react';

import InterventionCard from './InterventionCard.jsx';
import { api } from '../lib/api.js';
import { useStore } from '../store/useStore.js';

export default function InterventionQueue() {
  const { interventions, updateInterventionStatus, toast } = useStore();
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
        const result = await api.approve(id);
        toast(`Approved — ${result.nudges_issued} nudges issued`, 'success');
      } else {
        await api.reject(id, 'op_demo', 'Rejected by operator');
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
    <section className="panel flex min-h-0 flex-col">
      <div className="flex items-center justify-between px-3 pt-2.5">
        <h2 className="panel-title">Intervention queue</h2>
        <span className="text-[11px] tabular-nums text-slate-500">{visible.length}</span>
      </div>

      {visible.length === 0 ? (
        <div className="m-3 flex flex-1 items-center justify-center rounded-md border border-green-500/25 bg-green-500/10 px-4 py-6 text-center">
          <p className="text-sm text-green-300">No interventions required. System nominal.</p>
        </div>
      ) : (
        <div className="mt-2 flex-1 space-y-2 overflow-y-auto px-2.5 pb-2.5">
          {visible.map((intervention) => (
            <InterventionCard
              key={intervention.intervention_id}
              intervention={intervention}
              busy={busyId === intervention.intervention_id}
              onApprove={(i) => resolve(i, 'approve')}
              onReject={(i) => resolve(i, 'reject')}
            />
          ))}
        </div>
      )}
    </section>
  );
}
