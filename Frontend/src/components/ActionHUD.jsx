/**
 * ActionHUD — the truthful "what did that action do" strip (TASK 2).
 *
 * A slim overlay docked to the bottom of the map. It never covers the graph
 * centre and never becomes a dashboard. Every number is read from live
 * `entities` vs the T0 snapshot the store captured at approval, or from the
 * `regret_update` the backend sends when the intervention settles.
 *
 *  - EXECUTED  → risk-band vocabulary, real measured deltas.
 *  - SIMULATED → cyan, `whatIfOverlay` only, "not applied to the live world".
 *  - REJECTED  → "simulation unchanged", clears itself in a few seconds.
 *  - mock mode → shows the intervention's declared projection, never a
 *    fabricated measured delta (the recording cannot respond to an approval).
 */
import { riskColor, verdictStyle } from '../lib/colors.js';
import { pct, percent } from '../lib/format.js';
import { summarizeExecuted } from '../lib/actionEffects.js';
import { useStore } from '../store/useStore.js';

function BandArrow({ from, to }) {
  if (!from || !to || from === to) {
    return <span className="uppercase text-slate-400">{(to || from || '—')}</span>;
  }
  return (
    <span className="uppercase">
      <span style={{ color: riskColor(from).hex }}>{from}</span>
      <span className="text-slate-500"> → </span>
      <span style={{ color: riskColor(to).hex }}>{to}</span>
    </span>
  );
}

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

function ExecutedCard({ action, expanded, onToggle, onDismiss, nodesById, mockMode }) {
  const name = (id) => nodesById[id]?.display_name || id;
  const { deltas = [], downstream = [], cascade } = action.live;
  const settled = action.settled;

  const headline = mockMode
    ? `Executed (mock demo — live simulation not running)`
    : summarizeExecuted(action, deltas, downstream, cascade);

  return (
    <div className="pointer-events-auto rounded-md border border-surface-700/80 bg-surface-900/98 shadow-panel backdrop-blur select-none">
      <div className="flex items-center gap-2 px-3 py-2">
        <span className="chip border border-emerald-500/40 bg-emerald-500/15 font-semibold text-emerald-300 font-mono text-[9px]">
          {action.phase === 'settled' ? 'EXECUTED · SETTLED' : 'EXECUTED · DISPATCHED'}
        </span>
        {action.verdict && (
          <span
            className="chip border font-semibold font-mono text-[9px]"
            style={{
              color: verdictStyle(action.verdict).hex,
              borderColor: `${verdictStyle(action.verdict).hex}66`,
            }}
          >
            {verdictStyle(action.verdict).label}
          </span>
        )}
        <span className="truncate text-xs font-semibold text-slate-100">{action.title}</span>
        <span className="mx-1 hidden text-[11px] text-slate-500 sm:inline">·</span>
        <span className="hidden flex-1 truncate text-[11px] text-slate-300 sm:inline">
          {headline}
        </span>
        <button
          type="button"
          onClick={onToggle}
          className="ml-auto rounded p-0.5 text-slate-400 hover:text-slate-100"
          aria-label={expanded ? 'Collapse' : 'Expand'}
        >
          {expanded ? '▾' : '▸'}
        </button>
        <button
          type="button"
          onClick={onDismiss}
          className="rounded p-0.5 text-slate-400 hover:text-slate-100"
          aria-label="Dismiss"
        >
          ✕
        </button>
      </div>

      {expanded && (
        <div className="max-h-[190px] space-y-2 overflow-y-auto border-t border-surface-700/60 px-3 py-2 text-[11px]">
          {mockMode ? (
            <p className="text-slate-400">
              Projected relief{' '}
              <span className="text-slate-200 font-semibold">{pct(action.estimatedReliefPct)}</span> on{' '}
              {action.targetIds.map(name).join(', ')} (optimiser estimate). Run{' '}
              <span className="text-slate-300 font-mono">npm run dev:live</span> against the backend to see
              the measured simulation effect.
            </p>
          ) : (
            <>
              <table className="w-full">
                <tbody>
                  {deltas.map((d) => (
                    <tr key={d.entity_id} className="border-b border-surface-800 last:border-0">
                      <td className="py-1 pr-2 text-slate-300 font-medium">{name(d.entity_id)}</td>
                      <td className="py-1 pr-2 tabular-nums text-slate-400 font-mono">
                        {percent(d.utilFrom)} <span className="text-slate-600">→</span>{' '}
                        <span className="text-slate-200">{percent(d.utilTo)}</span>
                      </td>
                      <td className="py-1 pr-2 tabular-nums font-mono font-semibold">
                        <DeltaPp pp={d.deltaPp} />
                      </td>
                      <td className="py-1 text-[10px] font-mono">
                        <BandArrow from={d.bandFrom} to={d.bandTo} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <div className="text-slate-400 text-[10px]">
                <span className="text-slate-500 font-bold uppercase tracking-wider">downstream:</span>{' '}
                {downstream.length === 0 ? (
                  <span className="text-slate-500">no downstream change detected</span>
                ) : (
                  downstream.map((d, i) => (
                    <span key={d.entity_id}>
                      {i > 0 && <span className="text-slate-600"> · </span>}
                      <span className="text-slate-300">{name(d.entity_id)}</span>{' '}
                      <BandArrow from={d.bandFrom} to={d.bandTo} />
                    </span>
                  ))
                )}
              </div>

              {cascade && cascade.from !== null && (
                <div className="text-slate-400 text-[10px]">
                  <span className="text-slate-500 font-bold uppercase tracking-wider">cascade:</span>{' '}
                  <span className={cascade.reduced ? 'text-emerald-400 font-semibold' : 'text-slate-300'}>
                    {cascade.from} → {cascade.to} predicted downstream failures
                  </span>
                  {cascade.removedSteps.length > 0 && (
                    <span className="text-slate-500">
                      {' '}
                      ({cascade.removedSteps.slice(0, 4).map(name).join(', ')} no longer predicted)
                    </span>
                  )}
                </div>
              )}

              {settled && (
                <div className="rounded border border-surface-700/80 bg-surface-950/80 px-2 py-1 font-mono text-[10px]">
                  <span className="text-slate-500">settled:</span>{' '}
                  <span className="text-slate-200">
                    realised {settled.realised_relief_pct}pp
                  </span>{' '}
                  <span className="text-slate-500">vs do-nothing</span>{' '}
                  <span className="text-slate-300">{settled.counterfactual_relief_pct}pp</span>{' '}
                  <span className="text-slate-500">· regret</span>{' '}
                  <span
                    className={settled.regret > 0 ? 'text-orange-400' : 'text-emerald-400'}
                  >
                    {settled.regret > 0 ? '+' : ''}
                    {settled.regret}
                  </span>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
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

export default function ActionHUD() {
  const trackedActions = useStore((s) => s.trackedActions);
  const whatIfOverlay = useStore((s) => s.whatIfOverlay);
  const expanded = useStore((s) => s.actionHudExpanded);
  const setExpanded = useStore((s) => s.setActionHudExpanded);
  const dismissAction = useStore((s) => s.dismissAction);
  const clearWhatIfOverlay = useStore((s) => s.clearWhatIfOverlay);
  const nodesById = useStore((s) => s.nodesById);
  const mockMode = useStore((s) => s.mockMode);

  const executed = trackedActions.filter((a) => a.kind === 'executed');
  const rejected = trackedActions.find((a) => a.kind === 'rejected');

  if (!executed.length && !whatIfOverlay && !rejected) return null;

  return (
    <div className="pointer-events-none absolute bottom-3 left-1/2 z-20 w-[min(560px,calc(100%-1.5rem))] -translate-x-1/2 space-y-2">
      {whatIfOverlay && (
        <WhatIfCard overlay={whatIfOverlay} onDismiss={clearWhatIfOverlay} nodesById={nodesById} />
      )}
      {executed.map((action) => (
        <ExecutedCard
          key={action.id}
          action={action}
          expanded={expanded}
          onToggle={() => setExpanded(!expanded)}
          onDismiss={() => dismissAction(action.id)}
          nodesById={nodesById}
          mockMode={mockMode}
        />
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
