/**
 * Interventions — the live queue plus the full decision history.
 *
 * The queue is the same store-driven component as the Command Centre (approve
 * and reject hit the real endpoints). History comes from
 * `GET /interventions?status=all`, in the backend's rank order; settled
 * actions show their realised relief against the do-nothing counterfactual
 * from the regret ledger.
 */
import { useState } from 'react';

import InterventionQueue from '../components/InterventionQueue.jsx';
import PressureTimeline from '../components/PressureTimeline.jsx';
import PageShell, { ErrorNote, Stat } from '../components/PageShell.jsx';
import { api } from '../lib/api.js';
import { clock, integer, pct, rupees } from '../lib/format.js';
import { useLiveQuery } from '../lib/useLiveQuery.js';
import { useStore } from '../store/useStore.js';

const STATUSES = ['all', 'proposed', 'executing', 'completed', 'rejected', 'expired'];

export default function Interventions() {
  const nodesById = useStore((s) => s.nodesById);
  const operations = useStore((s) => s.operations);
  const [status, setStatus] = useState('all');
  const { data, error } = useLiveQuery(() => api.interventions(status, 100), [status], { everyCycles: 2 });
  const regret = useLiveQuery(() => api.regret(), [], { everyCycles: 4 });
  const ledger = Object.fromEntries((regret.data?.entries || []).map((e) => [e.intervention_id, e]));
  const items = data?.interventions || [];
  const name = (id) => nodesById[id]?.display_name || id;

  return (
    <PageShell
      title="Interventions"
      subtitle="Every proposal is simulated on a copy of the live city before it reaches you, certified by the equilibrium solver, and ranked. Approved actions change the city; 15 minutes later they are measured against a do-nothing counterfactual."
    >
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        <Stat label="Settled actions" value={regret.data?.summary?.count ?? '—'} />
        <Stat label="Mean |regret|" value={regret.data?.summary ? regret.data.summary.mean_absolute_regret.toFixed(1) : '—'} sub="predicted vs realised relief, pp" />
        <Stat label="Visitors redirected" value={integer(operations?.diverted_people)} />
        <Stat label="Attendee compliance" value={operations ? `${Math.round(operations.compliance * 100)}%` : '—'} sub="updated by nudge answers" />
      </div>
      <div className="grid gap-3 lg:grid-cols-[380px_1fr]">
        <div className="flex flex-col gap-3">
          <div className="h-[560px]"><InterventionQueue /></div>
          <div className="h-[320px]"><PressureTimeline /></div>
        </div>
        <section className="panel overflow-x-auto p-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <h2 className="panel-title mr-2">Decision history</h2>
            {STATUSES.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setStatus(s)}
                className={`rounded px-2 py-0.5 text-[10px] uppercase ${status === s ? 'bg-teal-500/15 text-teal-800 border border-teal-500/30' : 'text-slate-400 border border-transparent hover:text-slate-200'}`}
              >
                {s}
              </button>
            ))}
          </div>
          <ErrorNote error={error} />
          <table className="w-full min-w-[900px] text-left text-xs [&_td]:align-top">
            <thead className="text-[10px] uppercase tracking-wider text-slate-400">
              <tr>
                <th className="pb-1">Proposed</th><th className="pb-1">Action</th><th className="pb-1">Trigger</th>
                <th className="pb-1">Status</th><th className="pb-1">Predicted</th><th className="pb-1">Realised</th>
                <th className="pb-1">Verdict</th><th className="pb-1">Cost</th>
              </tr>
            </thead>
            <tbody>
              {items.map((i) => {
                const entry = ledger[i.intervention_id];
                return (
                  <tr key={i.intervention_id} className="border-t border-surface-700/40 align-top">
                    <td className="py-1.5 pr-2 font-mono">{clock(i.created_at)}</td>
                    <td className="pr-2">
                      <div className="font-semibold text-slate-100">{i.title}</div>
                      <div className="text-[10px] text-slate-400">{i.intervention_type.replace(/_/g, ' ')}</div>
                    </td>
                    <td className="pr-2">{name(i.triggered_by_entity_id)}</td>
                    <td className="pr-2 uppercase">{i.status}</td>
                    <td className="pr-2 tabular-nums">{pct(i.estimated_relief_pct)}</td>
                    <td className="pr-2 tabular-nums">
                      {entry ? (
                        <span className={entry.realised_relief_pct > 0 ? 'text-emerald-700' : 'text-red-600'}>
                          {pct(entry.realised_relief_pct)}
                        </span>
                      ) : '—'}
                    </td>
                    <td className="pr-2">{i.certificate?.verdict || '—'}</td>
                    <td className="pr-2 tabular-nums">{rupees(i.estimated_cost_paise)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!items.length && data && <p className="mt-2 text-xs text-slate-400">No interventions with this status yet.</p>}
        </section>
      </div>
    </PageShell>
  );
}
