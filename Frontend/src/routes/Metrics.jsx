/**
 * MetricsPanel — 02_FRONTEND_CONTRACT.md §5.10.
 *
 * Every metric renders as **value · baseline name · improvement**. Never a bare
 * number: a bare number proves nothing, and this is the judging slide.
 *
 * Three metrics are boxed and larger, per the contract:
 *   prediction.cascade_lead_time_sec
 *   decision.load_variance_reduction_pct
 *   decision.unstable_interventions_caught
 */
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
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
  ArrowLeft,
  Activity,
  Award,
  BarChart3,
  TrendingDown,
  TrendingUp,
  ShieldCheck,
  Cpu,
  Layers,
} from 'lucide-react';

import { api } from '../lib/api.js';
import { clock, decimals, minutes } from '../lib/format.js';
import { useStore } from '../store/useStore.js';
import { ReplayBanner } from '../components/PageShell.jsx';

const EMPHASISED = new Set([
  'prediction.cascade_lead_time_sec',
  'decision.load_variance_reduction_pct',
  'decision.unstable_interventions_caught',
]);

const LABELS = {
  forecast_mae: 'Forecast MAE',
  cascade_lead_time_sec: 'Cascade Lead Time',
  cascade_precision: 'Cascade Precision',
  cascade_recall: 'Cascade Recall',
  rmse: 'Digital Twin RMSE',
  ensemble_coverage: 'Ensemble Coverage',
  peak_utilisation_reduction_pct: 'Peak Utilisation Reduction',
  load_variance_reduction_pct: 'Load Variance Reduction',
  unstable_interventions_caught: 'Unstable Interventions Caught',
  certificate_accuracy_pct: 'Certificate Accuracy',
  cycle_latency_ms: 'Cycle Latency',
  commander_ungrounded_rate: 'Commander Ungrounded Rate',
  tool_call_correctness: 'Tool Call Correctness',
  capacity_utilisation: 'Mean Capacity Utilisation',
  peak_congestion: 'Peak Congestion',
  critical_locations: 'Critical Locations',
  queued_people: 'People Queued Now',
  avg_travel_time_sec: 'Mean Trip Time',
  late_entries: 'Entered After Start',
  unmet_room_requests: 'Unplaced Room Requests',
  rooms_available: 'Hotel Rooms Free',
  saturated_hotels: 'Saturated Hotels',
  visitors_redirected: 'Visitors Redirected',
  interventions_settled: 'Interventions Settled',
  mean_realised_relief_pct: 'Mean Realised Relief',
  attendee_compliance: 'Attendee Compliance',
};

const COUNTS = new Set([
  'critical_locations', 'queued_people', 'late_entries', 'unmet_room_requests', 'rooms_available',
  'saturated_hotels', 'visitors_redirected', 'interventions_settled', 'unstable_interventions_caught',
]);
const RATIOS = new Set(['capacity_utilisation', 'peak_congestion', 'attendee_compliance', 'ensemble_coverage']);

/** Formatting is per-metric because the units genuinely differ. */
function formatValue(key, metric) {
  const v = metric.value;
  if (v === null || v === undefined) return '—';
  if (key.endsWith('_sec')) return minutes(v);
  if (key.endsWith('_ms')) return `${Math.round(v)} ms`;
  if (key.endsWith('_pct')) return `${v.toFixed(1)}%`;
  if (COUNTS.has(key)) return Math.round(v).toLocaleString('en-IN');
  if (RATIOS.has(key)) return `${Math.round(v * 100)}%`;
  if (key === 'rmse') return decimals(v, 1);
  return decimals(v, 3);
}

function formatBaseline(key, metric) {
  if (metric.baseline === null || metric.baseline === undefined) return null;
  if (key.endsWith('_sec')) return minutes(metric.baseline);
  if (key === 'rmse') return decimals(metric.baseline, 1);
  return decimals(metric.baseline, 3);
}

function MetricCard({ metricKey, metric, emphasised }) {
  const baseline = formatBaseline(metricKey, metric);

  return (
    <div
      className={`panel p-3.5 transition-all duration-150 flex flex-col justify-between ${
        emphasised
          ? 'border-sky-500/50 bg-gradient-to-b from-sky-950/20 to-surface-900 shadow-lg shadow-sky-950/20'
          : 'hover:border-surface-650'
      }`}
    >
      <div>
        <div className="flex items-center justify-between">
          <span className="text-[10px] font-bold uppercase tracking-wider text-slate-400">
            {LABELS[metricKey] || metricKey}
          </span>
          {emphasised && (
            <span className="flex h-1.5 w-1.5 rounded-full bg-sky-400 ring-4 ring-sky-400/20" />
          )}
        </div>

        <div
          className={`mt-2 font-mono font-bold tabular-nums tracking-tight text-slate-100 ${
            emphasised ? 'text-3xl' : 'text-2xl'
          }`}
        >
          {formatValue(metricKey, metric)}
        </div>
      </div>

      <div className="mt-3 pt-2.5 border-t border-surface-700/50 space-y-1 text-[11px] font-mono">
        {metric.baseline_name && (
          <div className="text-slate-400 flex items-center justify-between text-[10px]">
            <span className="text-slate-400 font-sans">Baseline:</span>
            <span className="text-slate-300 font-mono">
              {metric.baseline_name.replace(/_/g, ' ')}
              {baseline !== null && ` (${baseline})`}
            </span>
          </div>
        )}

        {metric.improvement_pct !== null && metric.improvement_pct !== undefined && (
          <div className="flex items-center justify-between">
            <span className="text-slate-400 text-[10px] font-sans">Lift:</span>
            <span
              className={`inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-bold ${
                metric.improvement_pct >= 0
                  ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30'
                  : 'bg-amber-500/15 text-amber-400 border border-amber-500/30'
              }`}
            >
              {metric.improvement_pct >= 0 ? (
                <TrendingUp className="h-3 w-3" />
              ) : (
                <TrendingDown className="h-3 w-3" />
              )}
              {Math.abs(metric.improvement_pct).toFixed(1)}%
            </span>
          </div>
        )}

        {metric.target !== null && metric.target !== undefined && (
          <div className="text-slate-400 flex items-center justify-between text-[10px]">
            <span className="text-slate-400 font-sans">Target:</span>
            <span className="text-slate-300">
              {metricKey.endsWith('_ms') ? `${metric.target} ms` : metric.target}
            </span>
          </div>
        )}

        {metric.sample_size !== null && metric.sample_size !== undefined && (
          <div className="text-slate-400 flex items-center justify-between text-[10px]">
            <span className="text-slate-400 font-sans">Measured over:</span>
            <span className="text-slate-300">{metric.sample_size} {metric.sample_size === 1 ? 'event' : 'events'}</span>
          </div>
        )}

        {metric.target_range && (
          <div className="text-slate-400 flex items-center justify-between text-[10px]">
            <span className="text-slate-400 font-sans">Range:</span>
            <span className="text-slate-300">
              {metric.target_range[0]} – {metric.target_range[1]}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

const SECTION_ICONS = {
  prediction: Activity,
  twin: Cpu,
  decision: ShieldCheck,
  system: Layers,
};

function Section({ title, metrics, group }) {
  const IconComponent = SECTION_ICONS[group] || BarChart3;

  return (
    <section>
      <div className="mb-2.5 flex items-center gap-2">
        <IconComponent className="h-3.5 w-3.5 text-sky-400" />
        <h2 className="text-xs font-bold uppercase tracking-[0.14em] text-slate-300">
          {title}
        </h2>
        <span className="h-px flex-1 bg-surface-700/50" />
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2.5">
        {Object.entries(metrics).map(([key, metric]) => (
          <MetricCard
            key={key}
            metricKey={key}
            metric={metric}
            emphasised={EMPHASISED.has(`${group}.${key}`)}
          />
        ))}
      </div>
    </section>
  );
}

function RegretChart({ entries }) {
  if (!entries || entries.length === 0) {
    return (
      <div className="flex h-36 items-center justify-center rounded border border-surface-700/40 bg-surface-950/40 text-xs text-slate-400 font-mono">
        No interventions have completed yet — ledger awaiting simulation execution.
      </div>
    );
  }
  const data = entries.map((entry) => ({
    t: clock(entry.sim_time),
    regret: entry.regret,
  }));
  return (
    <div className="h-44 mt-2">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: -20 }}>
          <CartesianGrid stroke="#E2E8F0" strokeDasharray="3 3" />
          <XAxis dataKey="t" tick={{ fontSize: 9, fill: '#64748B' }} tickLine={false} />
          <YAxis tick={{ fontSize: 9, fill: '#64748B' }} tickLine={false} width={38} />
          <Tooltip
            contentStyle={{
              background: '#FFFFFF',
              border: '1px solid #CBD5E1',
              borderRadius: 6,
              fontSize: 10,
              fontFamily: 'monospace',
            }}
          />
          <Line
            type="monotone"
            dataKey="regret"
            stroke="#38BDF8"
            strokeWidth={2}
            dot={{ r: 2.5, fill: '#38BDF8' }}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export default function Metrics() {
  const [metrics, setMetrics] = useState(null);
  const [regret, setRegret] = useState(null);
  const [error, setError] = useState(null);
  const { toast, mockMode } = useStore();

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [m, r] = await Promise.all([api.metrics(), api.regret()]);
        if (cancelled) return;
        setMetrics(m);
        setRegret(r);
      } catch (err) {
        if (cancelled) return;
        if (err.isWarmingUp) setError('warming');
        else {
          setError(err.message);
          toast(err.message, 'error');
        }
      }
    }
    load();
    const handle = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="min-h-screen bg-surface-950 p-4 md:p-6 font-sans">
      <div className="mx-auto max-w-7xl">
        <ReplayBanner />
        {/* Header Bar */}
        <header className="mb-6 flex flex-wrap items-center justify-between gap-4 border-b border-surface-700/60 pb-4">
          <div>
            <div className="flex items-center gap-2">
              <span className="flex h-2 w-2 rounded-full bg-emerald-400" />
              <span className="text-[10px] font-mono uppercase tracking-[0.2em] text-slate-400">
                BENCHMARKS & DRIFT ASSESSMENT
              </span>
            </div>
            <h1 className="mt-1 text-xl font-bold tracking-tight text-slate-100">
              EventFlow AI — Measured Operational Outcomes
            </h1>
            <p className="mt-0.5 text-xs text-slate-400 max-w-2xl">
              Every value is computed from the running simulation: forecasts against what happened,
              the twin against ground truth, and approved actions against a do-nothing counterfactual.
            </p>
          </div>

          <Link
            to="/"
            className="btn-secondary h-8 px-3 text-xs font-semibold gap-1.5"
          >
            <ArrowLeft className="h-3.5 w-3.5 text-sky-400" />
            <span>Return to Command Centre</span>
          </Link>
        </header>

        {!metrics && error === 'warming' && (
          <div className="p-8 text-center panel">
            <Activity className="h-6 w-6 text-sky-400 mx-auto animate-spin mb-2" />
            <p className="text-sm font-semibold text-slate-100">Warming Telemetry Pipeline…</p>
            <p className="text-xs text-slate-400 mt-1">
              Accumulating cycle frames to compute empirical variance and RMSE deltas.
            </p>
          </div>
        )}

        {!metrics && !error && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="skeleton h-28" />
            ))}
          </div>
        )}

        {metrics && (
          <div className="space-y-6">
            {metrics.operations && Object.keys(metrics.operations).length > 0 && (
              <Section title={mockMode ? 'City Operations (recorded)' : 'City Operations (live)'} metrics={metrics.operations} group="operations" />
            )}
            <Section title="Prediction Horizon & Cascades" metrics={metrics.prediction} group="prediction" />
            <Section title="EnKF Digital Twin Assimilation" metrics={metrics.twin} group="twin" />
            <Section title="Decision Quality & Interventions" metrics={metrics.decision} group="decision" />
            <Section title="System Architecture & Commander" metrics={metrics.system} group="system" />

            {/* Regret Ledger Panel */}
            <section className="panel p-4">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-surface-700/60 pb-3">
                <div className="flex items-center gap-2">
                  <BarChart3 className="h-4 w-4 text-sky-400" />
                  <h3 className="panel-title">Cumulative Regret Ledger</h3>
                </div>

                {regret?.summary && (
                  <div className="flex flex-wrap items-center gap-4 text-xs font-mono">
                    <div className="flex items-center gap-1.5">
                      <span className="text-slate-400">Total Entries:</span>
                      <span className="rounded bg-surface-800 px-1.5 py-0.5 font-bold text-slate-200 border border-surface-700/50">
                        {regret.summary.count}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <span className="text-slate-400">Mean |Regret|:</span>
                      <span className="rounded bg-surface-800 px-1.5 py-0.5 font-bold text-slate-200 border border-surface-700/50">
                        {decimals(regret.summary.mean_absolute_regret, 2)}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <span className="text-slate-400">Drift Slope:</span>
                      <span
                        className={`rounded px-1.5 py-0.5 font-bold border ${
                          regret.summary.trend_slope <= 0
                            ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30'
                            : 'bg-amber-500/15 text-amber-400 border-amber-500/30'
                        }`}
                      >
                        {decimals(regret.summary.trend_slope, 3)}
                      </span>
                    </div>
                  </div>
                )}
              </div>

              <RegretChart entries={regret?.entries} />
            </section>
          </div>
        )}
      </div>
    </div>
  );
}

