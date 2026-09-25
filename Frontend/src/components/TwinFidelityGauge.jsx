/**
 * TwinFidelityGauge — 02_FRONTEND_CONTRACT.md §5.6.
 *
 * Starts collapsed; user can expand/collapse.
 * Shows intentional operational indicators even when collapsed:
 *   - Live synchronization indicator
 *   - Twin fidelity (RMSE reduction %)
 *   - Last synchronization time (sim_time)
 *   - Simulation ensemble status
 */
import { useState } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  Cpu,
  RefreshCw,
  GitCompare,
  TrendingDown,
  ChevronDown,
  ChevronRight,
  ShieldCheck,
  Maximize2,
  Minimize2
} from 'lucide-react';

import { api } from '../lib/api.js';
import { clock, decimals, pct } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

const MAX_POINTS = 20;

export default function TwinFidelityGauge() {
  const { twinFidelity, driftModeEnabled, setDriftMode, toast } = useStore();
  const [busy, setBusy] = useState(false);
  const [collapsed, setCollapsed] = useState(true);

  async function toggleDrift() {
    const next = !driftModeEnabled;
    setBusy(true);
    setDriftMode(next);
    try {
      const result = await api.driftMode(next);
      setDriftMode(result.drift_mode_enabled);
    } catch (error) {
      setDriftMode(!next);
      toast(error.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  if (!twinFidelity) {
    return (
      <section className="panel flex min-h-0 flex-col overflow-hidden">
        <div className="panel-header">
          <div className="flex items-center gap-2">
            <div className="flex h-5 w-5 items-center justify-center rounded bg-teal-500/15 text-teal-400">
              <Cpu className="h-3 w-3" />
            </div>
            <div>
              <h2 className="panel-title">Digital Twin</h2>
              <div className="text-[9px] font-mono text-slate-400 leading-none">
                ENKF STATE ASSIMILATION
              </div>
            </div>
          </div>
          <span className="text-[10px] font-mono text-slate-400">INITIALIZING…</span>
        </div>
        <div className="p-3">
          <div className="space-y-1.5">
            <div className="skeleton h-2.5 w-2/3" />
            <div className="skeleton h-2.5 w-1/3" />
          </div>
        </div>
      </section>
    );
  }

  const history = (twinFidelity.history || []).slice(-MAX_POINTS).map((point) => ({
    t: clock(point.sim_time),
    assimilated: point.assimilated_rmse,
    uncorrected: point.uncorrected_rmse,
  }));

  const lastSync = twinFidelity.sim_time ? clock(twinFidelity.sim_time) : '—';
  const fidelityPct =
    twinFidelity.improvement_pct !== null && twinFidelity.improvement_pct !== undefined
      ? pct(twinFidelity.improvement_pct)
      : '—';

  return (
    <section className="panel flex min-h-0 flex-col overflow-hidden">
      {/* Header with high information density */}
      <div className="panel-header">
        <div className="flex items-center gap-2">
          <div className="flex h-5 w-5 items-center justify-center rounded bg-emerald-500/15 text-emerald-400">
            <Cpu className="h-3 w-3" />
          </div>
          <div>
            <h2 className="panel-title">Digital Twin</h2>
            <div className="text-[9px] font-mono text-slate-400 leading-none">
              ENKF STATE ASSIMILATION
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {/* Live Sync Status Indicator */}
          <span className="flex items-center gap-1 rounded bg-emerald-500/10 px-2 py-0.5 text-[9px] font-mono font-medium text-emerald-300 border border-emerald-500/20">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
            SYNCED {lastSync}
          </span>

          <button
            type="button"
            onClick={() => setCollapsed((v) => !v)}
            className="rounded p-1 text-slate-400 hover:bg-surface-700 hover:text-slate-200 transition-colors"
            title={collapsed ? 'Expand digital twin' : 'Collapse digital twin'}
          >
            {collapsed ? <Maximize2 className="h-3 w-3" /> : <Minimize2 className="h-3 w-3" />}
          </button>
        </div>
      </div>

      {/* Collapsed Intentional Summary Bar */}
      {collapsed && (
        <div
          onClick={() => setCollapsed(false)}
          className="flex items-center justify-between px-3 py-2 bg-surface-900/40 hover:bg-surface-800/40 cursor-pointer transition-colors border-t border-surface-700/40 text-[10px]"
        >
          <div className="flex items-center gap-3">
            <div>
              <span className="text-slate-400 text-[9px] uppercase font-bold tracking-wider">Fidelity: </span>
              <span className="font-mono font-bold text-emerald-400">{fidelityPct}</span>
            </div>
            <span className="text-slate-600">·</span>
            <div>
              <span className="text-slate-400 text-[9px] uppercase font-bold tracking-wider">Ensemble: </span>
              <span className="font-mono text-slate-200">N={twinFidelity.ensemble_size}</span>
            </div>
            <span className="text-slate-600">·</span>
            <div>
              <span className="text-slate-400 text-[9px] uppercase font-bold tracking-wider">RMSE: </span>
              <span className="font-mono text-slate-200">{decimals(twinFidelity.assimilated_rmse, 1)}</span>
            </div>
          </div>
          <span className="text-teal-400 text-[9px] font-mono hover:underline">
            Expand telemetry →
          </span>
        </div>
      )}

      {/* Expanded Detailed Telemetry View */}
      {!collapsed && (
        <div className="flex min-h-0 flex-1 flex-col p-3 overflow-y-auto">
          {/* Top Metric Cards */}
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-3 rounded-md border border-emerald-500/30 bg-emerald-950/20 px-3 py-2">
              <div className="flex flex-col">
                <span className="text-[9px] font-bold uppercase tracking-wider text-emerald-400">
                  RMSE Reduction
                </span>
                <span className="text-xl font-bold font-mono tabular-nums text-emerald-300">
                  {fidelityPct}
                </span>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2 text-[10px] font-mono flex-1">
              <div className="rounded bg-surface-900/60 p-1.5 border border-surface-700/40">
                <span className="text-slate-400 text-[9px] uppercase font-sans">Ensemble Size</span>
                <div className="mt-0.5 font-bold text-slate-200">{twinFidelity.ensemble_size}</div>
              </div>
              <div className="rounded bg-surface-900/60 p-1.5 border border-surface-700/40">
                <span className="text-slate-400 text-[9px] uppercase font-sans">Ensemble Spread</span>
                <div className="mt-0.5 font-bold text-slate-200">{decimals(twinFidelity.ensemble_spread, 4)}</div>
              </div>
            </div>
          </div>

          {/* Drift Mode Controller */}
          <div className="mt-2.5 flex items-center justify-between rounded border border-surface-700/60 bg-surface-900/40 px-2.5 py-1.5">
            <div className="flex flex-col">
              <span className="text-xs font-medium text-slate-200">EnKF Drift Divergence</span>
              <span className="text-[10px] text-slate-400">Compare uncorrected ABM vs assimilation</span>
            </div>

            <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-300">
              <span className="font-mono text-[10px] text-slate-400">
                {driftModeEnabled ? 'ACTIVE' : 'OFF'}
              </span>
              <button
                type="button"
                role="switch"
                aria-checked={driftModeEnabled}
                disabled={busy}
                onClick={toggleDrift}
                className={`relative h-4 w-8 rounded-full transition-colors ${
                  driftModeEnabled ? 'bg-red-500' : 'bg-surface-700'
                }`}
              >
                <span
                  className={`absolute top-0.5 h-3 w-3 rounded-full bg-white transition-transform ${
                    driftModeEnabled ? 'translate-x-4' : 'translate-x-0.5'
                  }`}
                />
              </button>
            </label>
          </div>

          {/* Chart or Status Explanation */}
          {driftModeEnabled ? (
            <div className="mt-2 flex-1 min-h-[110px]">
              <div className="h-28 min-h-0">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={history} margin={{ top: 4, right: 6, bottom: 0, left: -22 }}>
                    <CartesianGrid stroke="#E2E8F0" strokeDasharray="3 3" />
                    <XAxis dataKey="t" tick={{ fontSize: 9, fill: '#64748B' }} tickLine={false} />
                    <YAxis tick={{ fontSize: 9, fill: '#64748B' }} tickLine={false} width={38} />
                    <Tooltip
                      contentStyle={{
                        background: '#FFFFFF',
                        border: '1px solid #E2E8F0',
                        borderRadius: 6,
                        fontSize: 10,
                      }}
                    />
                    <Line
                      type="monotone"
                      dataKey="uncorrected"
                      name="Uncorrected ABM"
                      stroke="#EF4444"
                      strokeWidth={1.75}
                      dot={false}
                      isAnimationActive={false}
                    />
                    <Line
                      type="monotone"
                      dataKey="assimilated"
                      name="Assimilated (EnKF)"
                      stroke="#22C55E"
                      strokeWidth={1.75}
                      dot={false}
                      isAnimationActive={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              <div className="mt-1 flex items-center justify-between text-[9px] font-mono text-slate-400">
                <span className="flex items-center gap-1">
                  <span className="h-1.5 w-1.5 rounded-full bg-red-500" />
                  Uncorrected Divergence
                </span>
                <span className="flex items-center gap-1">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                  Assimilated EnKF
                </span>
              </div>
            </div>
          ) : (
            <div className="mt-2 rounded bg-surface-900/40 p-2 border border-surface-700/40 text-[11px] leading-relaxed text-slate-400">
              Assimilated RMSE is{' '}
              <span className="font-mono font-semibold text-slate-200">
                {decimals(twinFidelity.assimilated_rmse, 1)}
              </span>
              . Toggle drift mode to simulate an uncorrected ensemble alongside it and view realtime divergence.
            </div>
          )}
        </div>
      )}
    </section>
  );
}
