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

// Display wraps at 100 so the sequence reads 1 → 2 → … → 99 → 0 → 1 → …
// The mock recording is also 100 frames, so both the display and the state
// reset happen at the same boundary. The backend's internal monotonic counter
// is never touched — only the rendered number wraps.
const CYCLE_DISPLAY_MAX = 100;

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
    <header className="flex items-center gap-4 border-b border-surface-700 bg-surface-900 px-4 py-2">
      {/* ── Brand zone ─────────────────────────────────────────────────── */}
      <div className="flex items-center gap-2.5">
        <h1 className="text-sm font-semibold tracking-tight text-white">
          {event?.name || 'EventFlow AI'}
        </h1>
        {wsStatus === 'connected' && <span className="live-dot" title="Live feed active" />}
        <span className="hidden text-[10px] uppercase tracking-[0.15em] text-slate-600 xl:block">
          Command Centre
        </span>
      </div>

      <div className="topbar-divider" />

      {/* ── Time / cycle ───────────────────────────────────────────────── */}
      <div className="flex items-center gap-5">
        <Stat label="Sim clock" value={clock(simTime)} />
        <Stat label="Kickoff in" value={countdown(toKickoff)} />
        <Stat label="Cycle" value={cycleNumber % CYCLE_DISPLAY_MAX} />
      </div>

      <div className="topbar-divider" />

      {/* ── Overall risk chip ──────────────────────────────────────────── */}
      <div
        className={`chip border ${band.border} bg-opacity-15`}
        style={{ backgroundColor: `${band.hex}22`, color: band.hex }}
      >
        <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: band.hex }} />
        <span className="font-semibold uppercase">{summary.overall_risk_band}</span>
        <span className="tabular-nums opacity-80">{summary.overall_risk_score}</span>
      </div>

      <div className="topbar-divider" />

      {/* ── Load variance + alert counts ───────────────────────────────── */}
      <div className="flex items-center gap-4">
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

      {/* ── Nav / status ───────────────────────────────────────────────── */}
      <div className="ml-auto flex items-center gap-2">
        {mockMode && (
          <span className="chip border border-sky-500/30 bg-sky-500/15 text-sky-400">mock</span>
        )}
        <WsChip status={wsStatus} />
        <Link
          to="/metrics"
          className="rounded px-2 py-1 text-[11px] text-slate-400 transition-colors hover:bg-surface-700 hover:text-slate-200"
        >
          Metrics
        </Link>
        <Link
          to="/attendee"
          className="rounded border border-sky-500/40 bg-sky-500/10 px-2.5 py-1 text-[11px] font-medium text-sky-400 transition-colors hover:bg-sky-500/20 hover:text-sky-300"
        >
          Attendee PWA
        </Link>
      </div>
    </header>
  );
}
