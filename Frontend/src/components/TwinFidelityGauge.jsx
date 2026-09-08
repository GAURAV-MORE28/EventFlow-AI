/**
 * TwinFidelityGauge — 02_FRONTEND_CONTRACT.md §5.6.
 *
 * Starts expanded; user can collapse it (and it stays collapsed through store
 * updates because `collapsed` is local state, not derived from twinFidelity).
 *
 * The drift toggle demo: flip drift on → clear chart → both series restart from
 * the same point → over ~5 cycles the uncorrected line visibly diverges while
 * the assimilated one stays flat. Rehearse the timing — it matters more than
 * the styling.
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

import { api } from '../lib/api.js';
import { clock, decimals, pct } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

const MAX_POINTS = 20; // matches TwinFidelity.history length (02 §10)

export default function TwinFidelityGauge() {
  const { twinFidelity, driftModeEnabled, setDriftMode, toast } = useStore();
  const [busy, setBusy] = useState(false);
  // Starts collapsed — user must explicitly expand. Local state so store updates
  // (every sim tick) never auto-reopen a panel the user intentionally closed.
  const [collapsed, setCollapsed] = useState(true);

  async function toggleDrift() {
    const next = !driftModeEnabled;
    setBusy(true);
    setDriftMode(next); // optimistic: the switch must feel instant on stage
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
      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">Digital twin</h2>
          <button
            type="button"
            onClick={() => setCollapsed((v) => !v)}
            className="text-[11px] text-slate-500 hover:text-slate-300"
          >
            {collapsed ? 'expand' : 'collapse'}
          </button>
        </div>
        {!collapsed && (
          <div className="p-3">
            <div className="space-y-1.5">
              <div className="skeleton h-3 w-2/3" />
              <div className="skeleton h-3 w-1/3" />
            </div>
            <p className="mt-2 text-[11px] text-slate-500">Warming up…</p>
          </div>
        )}
      </section>
    );
  }

  const history = (twinFidelity.history || []).slice(-MAX_POINTS).map((point) => ({
    t: clock(point.sim_time),
    assimilated: point.assimilated_rmse,
    uncorrected: point.uncorrected_rmse,
  }));

  return (
    <section className="panel flex min-h-0 flex-col">
      <div className="panel-header">
        <h2 className="panel-title">Digital twin</h2>
        <div className="flex items-center gap-3">
          {!collapsed && (
            <label className="flex cursor-pointer items-center gap-2 text-[11px] text-slate-400">
              <span>Drift mode</span>
              <button
                type="button"
                role="switch"
                aria-checked={driftModeEnabled}
                disabled={busy}
                onClick={toggleDrift}
                className={`relative h-4 w-8 rounded-full transition-colors ${
                  driftModeEnabled ? 'bg-red-500' : 'bg-surface-600'
                }`}
              >
                <span
                  className={`absolute top-0.5 h-3 w-3 rounded-full bg-white transition-transform ${
                    driftModeEnabled ? 'translate-x-4' : 'translate-x-0.5'
                  }`}
                />
              </button>
            </label>
          )}
          <button
            type="button"
            onClick={() => setCollapsed((v) => !v)}
            className="text-[11px] text-slate-500 hover:text-slate-300"
          >
            {collapsed ? 'expand' : 'collapse'}
          </button>
        </div>
      </div>

      {!collapsed && (
        <div className="flex min-h-0 flex-1 flex-col p-3">
          <div className="flex items-center gap-4">
            <div className="rounded border border-green-500/20 bg-green-500/[0.07] px-3 py-1.5">
              <div className="text-2xl font-bold tabular-nums text-green-400">
                {twinFidelity.improvement_pct === null || twinFidelity.improvement_pct === undefined
                  ? '—'
                  : pct(twinFidelity.improvement_pct)}
              </div>
              <div className="text-[10px] uppercase tracking-[0.12em] text-green-600">
                RMSE reduction
              </div>
            </div>
            <div className="space-y-1 text-[11px]">
              <div className="flex items-baseline gap-1.5">
                <span className="text-slate-500">ensemble</span>
                <span className="tabular-nums text-slate-300">{twinFidelity.ensemble_size}</span>
              </div>
              <div className="flex items-baseline gap-1.5">
                <span className="text-slate-500">spread</span>
                <span className="tabular-nums text-slate-300">{decimals(twinFidelity.ensemble_spread, 3)}</span>
              </div>
            </div>
          </div>

          {driftModeEnabled ? (
            <>
              <div className="mt-2 h-32 min-h-0">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={history} margin={{ top: 4, right: 6, bottom: 0, left: -22 }}>
                    <CartesianGrid stroke="#232D42" strokeDasharray="3 3" />
                    <XAxis dataKey="t" tick={{ fontSize: 9, fill: '#64748B' }} tickLine={false} />
                    <YAxis tick={{ fontSize: 9, fill: '#64748B' }} tickLine={false} width={40} />
                    <Tooltip
                      contentStyle={{
                        background: '#111725',
                        border: '1px solid #232D42',
                        borderRadius: 6,
                        fontSize: 11,
                      }}
                    />
                    <Line
                      type="monotone"
                      dataKey="uncorrected"
                      name="Uncorrected ABM"
                      stroke="#EF4444"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                    />
                    <Line
                      type="monotone"
                      dataKey="assimilated"
                      name="Assimilated (EnKF)"
                      stroke="#22C55E"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              <p className="mt-1 text-[10px] leading-snug text-slate-500">
                Uncorrected agent-based model vs. Ensemble Kalman Filter assimilation.
              </p>
            </>
          ) : (
            <p className="mt-2 text-[11px] leading-snug text-slate-500">
              Assimilated RMSE{' '}
              <span className="tabular-nums text-slate-300">
                {decimals(twinFidelity.assimilated_rmse, 1)}
              </span>
              . Enable drift mode to step an uncorrected ensemble alongside it.
            </p>
          )}
        </div>
      )}
    </section>
  );
}
