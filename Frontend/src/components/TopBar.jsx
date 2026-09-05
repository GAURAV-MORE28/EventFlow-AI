/**
 * TopBar — 02_FRONTEND_CONTRACT.md §5.1.
 *
 * Consumes `state.summary`, `event`, `sim_time`. Every value is rendered as the
 * API supplied it, with one documented exception: the load-variance delta arrow
 * compares against the value five cycles ago from a local ring buffer. That is
 * display-only and 02 §5.1 allows it explicitly.
 */
import { Link } from 'react-router-dom';

import { riskColor } from '../lib/colors.js';
import { clock, countdown, decimals, secondsBetween } from '../lib/format.js';
import { loadVarianceDelta, useStore } from '../store/useStore.js';

function WsChip({ status }) {
  if (status === 'connected') return null;
  const label = status === 'offline' ? 'Offline' : 'Reconnecting…';
  return (
    <span className="chip bg-amber-500/15 text-amber-400 border border-amber-500/30">
      <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
      {label}
    </span>
  );
}

function Stat({ label, value, tone = 'text-slate-100', suffix = null }) {
  return (
    <div className="flex flex-col">
      <span className="panel-title">{label}</span>
      <span className={`text-sm font-semibold tabular-nums ${tone}`}>
        {value}
        {suffix}
      </span>
    </div>
  );
}

export default function TopBar() {
  const { event, summary, simTime, cycleNumber, wsStatus, mockMode } = useStore();
  const delta = useStore(loadVarianceDelta);
  const band = riskColor(summary.overall_risk_band);

  const toKickoff = secondsBetween(simTime, event?.start_time);

  // Down is good: variance falling means load is spreading out.
  const deltaTone =
    delta === null || Math.abs(delta) < 0.0005
      ? 'text-slate-500'
      : delta < 0
        ? 'text-green-400'
        : 'text-orange-400';
  const deltaGlyph = delta === null || Math.abs(delta) < 0.0005 ? '·' : delta < 0 ? '▼' : '▲';

  return (
    <header className="flex items-center gap-6 border-b border-surface-600 bg-surface-800 px-5 py-2.5">
      <div className="flex items-baseline gap-3">
        <h1 className="text-base font-semibold tracking-tight text-white">
          {event?.name || 'EventFlow AI'}
        </h1>
        <span className="text-[11px] uppercase tracking-widest text-slate-500">
          Command Centre
        </span>
      </div>

      <div className="flex items-center gap-6">
        <Stat label="Sim clock" value={clock(simTime)} />
        <Stat label="Kickoff in" value={countdown(toKickoff)} />
        <Stat label="Cycle" value={cycleNumber} />
      </div>

      <div
        className={`chip border ${band.border} bg-opacity-15`}
        style={{ backgroundColor: `${band.hex}22`, color: band.hex }}
      >
        <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: band.hex }} />
        <span className="font-semibold uppercase">{summary.overall_risk_band}</span>
        <span className="tabular-nums opacity-80">{summary.overall_risk_score}</span>
      </div>

      <div className="flex items-center gap-5">
        <div className="flex flex-col">
          <span className="panel-title">Load variance</span>
          <span className="flex items-baseline gap-1.5">
            <span className="text-sm font-semibold tabular-nums text-slate-100">
              {decimals(summary.load_variance, 3)}
            </span>
            <span className={`text-[11px] tabular-nums ${deltaTone}`} title="vs 5 cycles ago">
              {deltaGlyph}
              {delta !== null && Math.abs(delta) >= 0.0005 ? decimals(Math.abs(delta), 3) : ''}
            </span>
          </span>
        </div>
        <Stat
          label="Critical"
          value={summary.critical_count}
          tone={summary.critical_count > 0 ? 'text-red-400' : 'text-slate-400'}
        />
        <Stat
          label="High"
          value={summary.high_count}
          tone={summary.high_count > 0 ? 'text-orange-400' : 'text-slate-400'}
        />
      </div>

      <div className="ml-auto flex items-center gap-3">
        {mockMode && (
          <span className="chip bg-sky-500/15 text-sky-400 border border-sky-500/30">mock</span>
        )}
        <WsChip status={wsStatus} />
        <Link
          to="/metrics"
          className="text-xs text-slate-400 underline-offset-4 hover:text-slate-200 hover:underline"
        >
          Metrics
        </Link>
        <Link
          to="/attendee"
          className="text-xs text-slate-400 underline-offset-4 hover:text-slate-200 hover:underline"
        >
          Attendee
        </Link>
      </div>
    </header>
  );
}
