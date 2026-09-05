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

import { api } from '../lib/api.js';
import { clock, decimals, minutes } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

const EMPHASISED = new Set([
  'prediction.cascade_lead_time_sec',
  'decision.load_variance_reduction_pct',
  'decision.unstable_interventions_caught',
]);

const LABELS = {
  forecast_mae: 'Forecast MAE',
  cascade_lead_time_sec: 'Cascade lead time',
  cascade_precision: 'Cascade precision',
  cascade_recall: 'Cascade recall',
  rmse: 'Twin RMSE',
  ensemble_coverage: 'Ensemble coverage',
  peak_utilisation_reduction_pct: 'Peak utilisation reduction',
  load_variance_reduction_pct: 'Load variance reduction',
  unstable_interventions_caught: 'Unstable interventions caught',
  certificate_accuracy_pct: 'Certificate accuracy',
  cycle_latency_ms: 'Cycle latency',
  commander_ungrounded_rate: 'Commander ungrounded rate',
  tool_call_correctness: 'Tool call correctness',
};

/** Formatting is per-metric because the units genuinely differ. */
function formatValue(key, metric) {
  const v = metric.value;
  if (v === null || v === undefined) return '—';
  if (key.endsWith('_sec')) return minutes(v);
  if (key.endsWith('_ms')) return `${Math.round(v)} ms`;
  if (key.endsWith('_pct')) return `${v.toFixed(1)}%`;
  if (key === 'unstable_interventions_caught') return String(Math.round(v));
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
      className={`panel p-3 ${emphasised ? 'border-sky-500/40 bg-sky-500/[0.06]' : ''}`}
    >
      <div className="panel-title">{LABELS[metricKey] || metricKey}</div>
      <div
        className={`mt-1 font-bold tabular-nums text-slate-50 ${
          emphasised ? 'text-4xl' : 'text-2xl'
        }`}
      >
        {formatValue(metricKey, metric)}
      </div>

      <div className="mt-1 space-y-0.5 text-[11px]">
        {metric.baseline_name && (
          <div className="text-slate-500">
            vs {metric.baseline_name.replace(/_/g, ' ')}
            {baseline !== null && <span className="tabular-nums"> : {baseline}</span>}
          </div>
        )}
        {metric.improvement_pct !== null && metric.improvement_pct !== undefined && (
          <div
            className={metric.improvement_pct >= 0 ? 'text-green-400' : 'text-orange-400'}
          >
            {metric.improvement_pct >= 0 ? '▲' : '▼'}{' '}
            {Math.abs(metric.improvement_pct).toFixed(1)}% improvement
          </div>
        )}
        {metric.target !== null && metric.target !== undefined && (
          <div className="text-slate-500">
            target {metricKey.endsWith('_ms') ? `${metric.target} ms` : metric.target}
          </div>
        )}
        {metric.target_range && (
          <div className="text-slate-500">
            target {metric.target_range[0]}–{metric.target_range[1]}
          </div>
        )}
      </div>
    </div>
  );
}

function Section({ title, metrics, group }) {
  return (
    <section>
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-widest text-slate-500">
        {title}
      </h2>
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
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
      <p className="text-[11px] text-slate-500">
        No interventions have completed yet, so the ledger is empty.
      </p>
    );
  }
  const data = entries.map((entry) => ({
    t: clock(entry.sim_time),
    regret: entry.regret,
  }));
  return (
    <div className="h-40">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: -20 }}>
          <CartesianGrid stroke="#232D42" strokeDasharray="3 3" />
          <XAxis dataKey="t" tick={{ fontSize: 9, fill: '#64748B' }} tickLine={false} />
          <YAxis tick={{ fontSize: 9, fill: '#64748B' }} tickLine={false} width={38} />
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
            dataKey="regret"
            stroke="#38BDF8"
            strokeWidth={2}
            dot={{ r: 2 }}
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
  const { toast } = useStore();

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
    <div className="min-h-screen bg-surface-900 p-6">
      <header className="mb-5 flex items-baseline justify-between">
        <div>
          <h1 className="text-xl font-semibold text-white">EventFlow AI — Measured outcomes</h1>
          <p className="mt-0.5 text-[11px] text-slate-500">
            In simulation, on the seeded demo topology. Generalisation to unseen
            topologies is reported separately and never conflated with these figures.
          </p>
        </div>
        <Link to="/" className="text-xs text-slate-400 hover:text-slate-200 hover:underline">
          ← Command Centre
        </Link>
      </header>

      {!metrics && error === 'warming' && (
        <p className="text-sm text-slate-500">Warming up…</p>
      )}
      {!metrics && !error && (
        <div className="grid grid-cols-4 gap-2">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="skeleton h-24" />
          ))}
        </div>
      )}

      {metrics && (
        <div className="space-y-6">
          <Section title="Prediction" metrics={metrics.prediction} group="prediction" />
          <Section title="Digital twin" metrics={metrics.twin} group="twin" />
          <Section title="Decision quality" metrics={metrics.decision} group="decision" />
          <Section title="System" metrics={metrics.system} group="system" />

          <section>
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-widest text-slate-500">
              Regret ledger
            </h2>
            <div className="panel p-3">
              {regret?.summary && (
                <div className="mb-2 flex gap-6 text-[11px]">
                  <span className="text-slate-400">
                    entries <span className="tabular-nums text-slate-200">{regret.summary.count}</span>
                  </span>
                  <span className="text-slate-400">
                    mean |regret|{' '}
                    <span className="tabular-nums text-slate-200">
                      {decimals(regret.summary.mean_absolute_regret, 2)}
                    </span>
                  </span>
                  <span className="text-slate-400">
                    trend{' '}
                    <span
                      className={`tabular-nums ${
                        regret.summary.trend_slope <= 0 ? 'text-green-400' : 'text-orange-400'
                      }`}
                    >
                      {decimals(regret.summary.trend_slope, 3)}
                    </span>
                  </span>
                </div>
              )}
              <RegretChart entries={regret?.entries} />
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
