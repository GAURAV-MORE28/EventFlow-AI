/**
 * TopBar — 02_FRONTEND_CONTRACT.md §5.1.
 *
 * Consumes `state.summary`, `event`, `sim_time`. Every value is rendered as the
 * API supplied it, with one documented exception: the load-variance delta arrow
 * compares against the value five cycles ago from a local ring buffer. That is
 * display-only and 02 §5.1 allows it explicitly.
 */
import { Link } from 'react-router-dom';
import {
  Activity,
  Clock,
  Layers,
  BarChart2,
  Smartphone,
  ShieldAlert,
  Radio,
  Wifi,
  WifiOff
} from 'lucide-react';

import { riskColor } from '../lib/colors.js';
import { clock, countdown, decimals, secondsBetween } from '../lib/format.js';
import { loadVarianceDelta, useStore } from '../store/useStore.js';

function WsChip({ status }) {
  if (status === 'connected') return null;
  const isOffline = status === 'offline';
  return (
    <span className="chip border border-amber-500/40 bg-amber-500/10 text-amber-300 text-[10px]">
      <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
      {isOffline ? 'OFFLINE' : 'RECONNECTING…'}
    </span>
  );
}

function MetricBlock({ label, value, sub = null, tone = 'text-slate-100', icon: Icon }) {
  return (
    <div className="flex items-center gap-2 px-2 py-0.5 rounded bg-surface-900/40 border border-surface-700/30">
      {Icon && <Icon className="h-3.5 w-3.5 text-slate-500 shrink-0" />}
      <div className="flex flex-col">
        <span className="text-[9px] font-semibold uppercase tracking-wider text-slate-400 leading-none">
          {label}
        </span>
        <div className="flex items-baseline gap-1 mt-0.5">
          <span className={`text-xs font-semibold tabular-nums leading-none ${tone}`}>
            {value}
          </span>
          {sub && <span className="text-[10px] leading-none text-slate-400">{sub}</span>}
        </div>
      </div>
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
        ? 'text-emerald-400'
        : 'text-orange-400';
  const deltaGlyph = delta === null || Math.abs(delta) < 0.0005 ? '·' : delta < 0 ? '▼' : '▲';

  return (
    <header className="flex h-12 w-full items-center justify-between border-b border-surface-700/60 bg-surface-950 px-3.5 py-1 text-slate-200 select-none">
      {/* ── Brand zone & Live Status ───────────────────────────────────────── */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <div className="flex h-6 w-6 items-center justify-center rounded border border-teal-500/40 bg-teal-500/10 shadow-sm">
            <Radio className="h-3.5 w-3.5 text-teal-400" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="text-xs font-bold tracking-tight text-black uppercase">
                {event?.name || 'EventFlow AI'}
              </span>
              <span className="rounded bg-teal-500/10 px-1.5 py-0.2 text-[9px] font-semibold uppercase tracking-widest text-teal-400 border border-teal-500/30">
                C2 OPS
              </span>
            </div>
          </div>
        </div>

        {/* Live / Sim Indicator */}
        <div className="flex items-center gap-1.5 rounded-full border border-surface-700/60 bg-surface-900/80 px-2.5 py-0.5 text-[10px] font-semibold tracking-wider">
          <span className={wsStatus === 'connected' ? 'live-dot' : 'h-1.5 w-1.5 rounded-full bg-slate-500'} />
          <span className={wsStatus === 'connected' ? 'text-emerald-400' : 'text-slate-400'}>
            {mockMode ? 'SIMULATION' : 'LIVE FEED'}
          </span>
        </div>
      </div>

      <div className="topbar-divider" />

      {/* ── Central Telemetry & Clocks ─────────────────────────────────────── */}
      <div className="flex items-center gap-2">
        <MetricBlock
          icon={Clock}
          label="Sim Time"
          value={clock(simTime)}
          sub="UTC"
        />

        <MetricBlock
          label="Kickoff In"
          value={countdown(toKickoff)}
          tone={toKickoff && toKickoff < 1800 ? 'text-amber-300' : 'text-slate-200'}
        />

        <MetricBlock
          icon={Layers}
          label="Cycle"
          value={cycleNumber}
          sub="× 30s"
        />

        <div className="topbar-divider" />

        {/* ── Overall System Risk Pill ────────────────────────────────────── */}
        <div
          className="flex items-center gap-2 rounded-md border px-2.5 py-1 transition-all"
          style={{
            borderColor: `${band.hex}44`,
            backgroundColor: `${band.hex}14`,
          }}
        >
          <span className="h-2 w-2 rounded-full" style={{ backgroundColor: band.hex }} />
          <div className="flex flex-col">
            <span className="text-[9px] font-bold uppercase tracking-wider text-slate-400 leading-none">
              Risk Level
            </span>
            <div className="flex items-baseline gap-1 mt-0.5">
              <span className="text-xs font-bold uppercase" style={{ color: band.hex }}>
                {summary.overall_risk_band}
              </span>
              <span className="text-[11px] font-mono tabular-nums opacity-90" style={{ color: band.hex }}>
                {summary.overall_risk_score}
              </span>
            </div>
          </div>
        </div>

        {/* ── Load Variance & Critical Counters ───────────────────────────── */}
        <div className="flex items-center gap-1.5 rounded bg-surface-900/40 border border-surface-700/30 px-2.5 py-1">
          <div className="flex flex-col">
            <span className="text-[9px] font-semibold uppercase tracking-wider text-slate-400 leading-none">
              Load Variance
            </span>
            <div className="flex items-baseline gap-1 mt-0.5">
              <span className="text-xs font-semibold tabular-nums text-slate-100">
                {decimals(summary.load_variance, 3)}
              </span>
              <span className={`text-[10px] tabular-nums font-medium ${deltaTone}`} title="vs 5 cycles ago">
                {deltaGlyph}
                {delta !== null && Math.abs(delta) >= 0.0005 ? decimals(Math.abs(delta), 3) : ''}
              </span>
            </div>
          </div>

          <div className="h-4 w-px bg-surface-700/50 mx-1" />

          <div className="flex items-center gap-2 text-xs tabular-nums">
            <div className="flex flex-col items-center">
              <span className="text-[8px] font-bold uppercase text-slate-500">Crit</span>
              <span className={`font-bold ${summary.critical_count > 0 ? 'text-red-400' : 'text-slate-400'}`}>
                {summary.critical_count}
              </span>
            </div>
            <div className="flex flex-col items-center">
              <span className="text-[8px] font-bold uppercase text-slate-500">High</span>
              <span className={`font-bold ${summary.high_count > 0 ? 'text-orange-400' : 'text-slate-400'}`}>
                {summary.high_count}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* ── Right Actions & Navigation ─────────────────────────────────────── */}
      <div className="flex items-center gap-2">
        {mockMode && (
          <span className="rounded border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-300">
            DEMO MOCK
          </span>
        )}
        <WsChip status={wsStatus} />

        <Link
          to="/metrics"
          className="btn btn-secondary text-xs h-7 px-2.5 gap-1.5"
          title="Open judging KPI dashboard"
        >
          <BarChart2 className="h-3.5 w-3.5 text-slate-400" />
          <span>Metrics</span>
        </Link>

        <Link
          to="/attendee"
          className="btn btn-primary text-xs h-7 px-2.5 gap-1.5"
          title="Open Attendee PWA view"
        >
          <Smartphone className="h-3.5 w-3.5" />
          <span>Attendee PWA</span>
        </Link>
      </div>
    </header>
  );
}
