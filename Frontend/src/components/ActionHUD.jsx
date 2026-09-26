/**
 * ActionHUD — the truthful "what did that action do" strip (TASK 2).
 *
 * A slim overlay docked to the bottom of the map. It never covers the graph
 * centre and never becomes a dashboard. Every number is backend-measured:
 * an executing action's `live_effect` compares the live city with its
 * do-nothing copy (sent every cycle as `intervention_effect`).
 *
 *  - EXECUTING → live utilisation vs "without action", per affected entity.
 *  - SIMULATED → cyan, `whatIfOverlay` only, "not applied to the live world".
 *  - REJECTED  → "simulation unchanged", clears itself in a few seconds.
 */
import { percent } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

function DeltaPp({ pp }) {
  if (pp === null || pp === undefined) return null;
  if (pp === 0) return <span className="text-slate-500">±0pp</span>;
  const good = pp < 0;
  return (
    <span className={good ? 'text-green-400' : 'text-orange-400'}>
      {good ? '▼' : '▲'} {Math.abs(pp)}pp
    </span>
  );
}

function WhatIfCard({ overlay, onDismiss, nodesById }) {
  const name = (id) => nodesById[id]?.display_name || id;
  const result = overlay.result || {};
  const delta = result.delta || {};
  return (
    <div className="pointer-events-auto rounded-md border border-teal-500/40 bg-surface-900/98 shadow-panel backdrop-blur select-none">
      <div className="flex items-center gap-2 px-3 py-2">
        <span className="chip border border-teal-500/40 bg-teal-500/15 font-semibold text-teal-300 font-mono text-[9px]">
          SIMULATED · WHAT-IF
        </span>
        <span className="truncate text-xs font-semibold text-slate-100">{overlay.label}</span>
        <span className="hidden flex-1 truncate text-[11px] text-teal-300/80 sm:inline">
          projection on a forked twin — not applied to the live world
        </span>
        <button
          type="button"
          onClick={onDismiss}
          className="ml-auto rounded p-0.5 text-slate-400 hover:text-slate-100"
          aria-label="Dismiss"
        >
          ✕
        </button>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-surface-700/60 px-3 py-2 text-[11px] font-mono">
        <span className="text-slate-400">
          peak util Δ{' '}
          <span className={delta.peak_utilisation_pct > 0 ? 'text-red-400 font-semibold' : 'text-emerald-400 font-semibold'}>
            {delta.peak_utilisation_pct > 0 ? '+' : ''}
            {delta.peak_utilisation_pct}%
          </span>
        </span>
        <span className="text-slate-400">
          load variance Δ{' '}
          <span className={delta.load_variance_pct > 0 ? 'text-red-400 font-semibold' : 'text-emerald-400 font-semibold'}>
            {delta.load_variance_pct > 0 ? '+' : ''}
            {delta.load_variance_pct}%
          </span>
        </span>
        {delta.new_critical_entities?.length > 0 && (
          <span className="text-slate-400 font-sans">
            new critical:{' '}
            <span className="text-red-300 font-mono">
              {delta.new_critical_entities.slice(0, 4).map(name).join(', ')}
            </span>
          </span>
        )}
      </div>
    </div>
  );
}

/** One executing action: live utilisation vs the do-nothing copy (backend-measured). */
function LiveEffectCard({ intervention, nodesById }) {
  const name = (id) => nodesById[id]?.display_name || id;
  const effects = intervention.live_effect || [];
  return (
    <div className="pointer-events-auto rounded-md border border-surface-700/80 bg-surface-900/98 px-3 py-2 text-[11px] shadow-panel backdrop-blur">
      <div className="mb-1 flex items-center gap-2">
        <span className="chip border border-emerald-500/40 bg-emerald-500/15 font-mono text-[9px] font-semibold text-emerald-300">
          EXECUTING
        </span>
        <span className="truncate text-xs font-semibold text-slate-100">{intervention.title}</span>
      </div>
      {effects.length === 0 ? (
        <p className="text-slate-400">Applied; the first measurement arrives next cycle.</p>
      ) : (
        <table className="w-full">
          <tbody>
            {effects.map((e) => (
              <tr key={e.entity_id} className="border-b border-surface-800 last:border-0">
                <td className="py-0.5 pr-2 font-medium text-slate-300">{name(e.entity_id)}</td>
                <td className="py-0.5 pr-2 font-mono tabular-nums text-slate-200">{percent(e.utilisation)}</td>
                <td className="py-0.5 pr-2 font-mono tabular-nums text-slate-400">without action {percent(e.counterfactual_utilisation)}</td>
                <td className="py-0.5 font-mono font-semibold tabular-nums">
                  <DeltaPp pp={Math.round(e.delta * 100)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function ActionHUD() {
  const trackedActions = useStore((s) => s.trackedActions);
  const interventions = useStore((s) => s.interventions);
  const whatIfOverlay = useStore((s) => s.whatIfOverlay);
  const clearWhatIfOverlay = useStore((s) => s.clearWhatIfOverlay);
  const nodesById = useStore((s) => s.nodesById);

  const executing = (interventions || []).filter((i) => i.status === 'executing');
  const rejected = trackedActions.find((a) => a.kind === 'rejected');

  if (!executing.length && !whatIfOverlay && !rejected) return null;

  return (
    <div className="pointer-events-none absolute bottom-3 left-1/2 z-20 w-[min(560px,calc(100%-1.5rem))] -translate-x-1/2 space-y-2">
      {whatIfOverlay && (
        <WhatIfCard overlay={whatIfOverlay} onDismiss={clearWhatIfOverlay} nodesById={nodesById} />
      )}
      {executing.slice(0, 3).map((i) => (
        <LiveEffectCard key={i.intervention_id} intervention={i} nodesById={nodesById} />
      ))}
      {rejected && (
        <div className="pointer-events-auto rounded-md border border-surface-700/80 bg-surface-900/98 px-3 py-2 text-xs text-slate-300 shadow-panel backdrop-blur">
          <span className="chip mr-2 border border-slate-600/50 bg-slate-800 text-[10px] font-semibold text-slate-300">
            REJECTED
          </span>
          {rejected.title} — simulation unchanged
        </div>
      )}
    </div>
  );
}
