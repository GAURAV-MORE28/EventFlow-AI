/**
 * EntityDetailPanel — 02_FRONTEND_CONTRACT.md §5.8.
 *
 * Sections: current state, the three-horizon forecast with 90% bands, the risk
 * breakdown by `risk_type`, inbound/outbound edges (clickable), and a button to
 * arm the cascade overlay for this entity.
 */
import { useEffect, useState } from 'react';
import {
  Area,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { api } from '../lib/api.js';
import { riskColor } from '../lib/colors.js';
import { decimals, integer, minutes, percent } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

function ForecastChart({ forecast, baseline }) {
  const data = [
    { label: 'now', value: baseline, lower: baseline, upper: baseline },
    ...forecast.points.map((point) => ({
      label: `${point.horizon_sec / 60}m`,
      value: point.predicted_utilisation,
      lower: point.lower_90,
      upper: point.upper_90,
      // Recharts stacks an Area from `lower`; the band is its height.
      band: (point.upper_90 ?? 0) - (point.lower_90 ?? 0),
    })),
  ];

  return (
    <div className="h-28">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -26 }}>
          <XAxis dataKey="label" tick={{ fontSize: 9, fill: '#64748B' }} tickLine={false} />
          <YAxis tick={{ fontSize: 9, fill: '#64748B' }} tickLine={false} width={38} />
          <Tooltip
            contentStyle={{
              background: '#111725',
              border: '1px solid #232D42',
              borderRadius: 6,
              fontSize: 11,
            }}
          />
          <Area
            dataKey="lower"
            stackId="band"
            stroke="none"
            fill="transparent"
            isAnimationActive={false}
          />
          <Area
            dataKey="band"
            stackId="band"
            stroke="none"
            fill="#38BDF8"
            fillOpacity={0.16}
            isAnimationActive={false}
            name="90% interval"
          />
          <Line
            type="monotone"
            dataKey="value"
            stroke="#38BDF8"
            strokeWidth={2}
            dot={{ r: 2 }}
            isAnimationActive={false}
            name="predicted"
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

function RiskBreakdown({ rows }) {
  const max = Math.max(100, ...rows.map((r) => r.score));
  return (
    <ul className="space-y-1">
      {rows.map((row) => (
        <li key={row.risk_type} className="flex items-center gap-2">
          <span className="w-20 shrink-0 text-[10px] capitalize text-slate-500">
            {row.risk_type}
          </span>
          <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-700">
            <span
              className="block h-full rounded-full bg-sky-500"
              style={{ width: `${(row.score / max) * 100}%` }}
            />
          </span>
          <span className="w-6 text-right text-[10px] tabular-nums text-slate-400">
            {row.score}
          </span>
        </li>
      ))}
    </ul>
  );
}

export default function EntityDetailPanel() {
  const {
    selectedEntityId,
    nodesById,
    entities,
    clearSelection,
    selectEntity,
    setActiveCascadeRoot,
    setCascade,
    toast,
  } = useStore();
  const [detail, setDetail] = useState(null);
  const [status, setStatus] = useState('idle');

  useEffect(() => {
    if (!selectedEntityId) {
      setDetail(null);
      return undefined;
    }
    let cancelled = false;
    setStatus('loading');
    api
      .entity(selectedEntityId)
      .then((result) => {
        if (cancelled) return;
        setDetail(result);
        setStatus('ready');
      })
      .catch((error) => {
        if (cancelled) return;
        // Warming up is an expected state, not an error toast (00 §4).
        setStatus(error.isWarmingUp || error.code === 'MODEL_NOT_READY' ? 'warming' : 'error');
      });
    return () => {
      cancelled = true;
    };
  }, [selectedEntityId]);

  if (!selectedEntityId) return null;

  const node = nodesById[selectedEntityId];
  const live = entities[selectedEntityId];
  const state = detail?.state || live;
  const band = riskColor(state?.risk_band);

  async function showCascade() {
    try {
      const cascade = await api.cascade(selectedEntityId);
      setCascade(cascade);
      setActiveCascadeRoot(selectedEntityId);
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  return (
    <aside className="absolute bottom-3 left-3 top-3 z-10 w-80 overflow-y-auto rounded-lg border border-surface-600 bg-surface-800/97 p-3 shadow-2xl backdrop-blur">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-slate-100">
            {node?.display_name || selectedEntityId}
          </h3>
          <p className="text-[10px] uppercase tracking-wider text-slate-500">
            {node?.entity_type}
          </p>
        </div>
        <button
          type="button"
          onClick={clearSelection}
          className="text-slate-500 hover:text-slate-200"
          aria-label="Close"
        >
          ✕
        </button>
      </div>

      {state && (
        <div className="mt-2 grid grid-cols-2 gap-2 text-[11px]">
          <div className="rounded bg-surface-900/70 px-2 py-1.5">
            <div className="text-slate-500">Utilisation</div>
            <div className="text-base font-semibold tabular-nums" style={{ color: band.hex }}>
              {percent(state.utilisation)}
            </div>
          </div>
          <div className="rounded bg-surface-900/70 px-2 py-1.5">
            <div className="text-slate-500">Risk</div>
            <div className="text-base font-semibold tabular-nums" style={{ color: band.hex }}>
              {state.risk_score}
            </div>
          </div>
          <div className="rounded bg-surface-900/70 px-2 py-1.5">
            <div className="text-slate-500">Count / capacity</div>
            <div className="tabular-nums text-slate-300">
              {integer(state.current_count)} / {integer(node?.nominal_capacity)}
            </div>
          </div>
          <div className="rounded bg-surface-900/70 px-2 py-1.5">
            <div className="text-slate-500">Flow</div>
            <div className="tabular-nums text-slate-300">
              {decimals(state.flow_rate_per_min, 1)}/min
            </div>
          </div>
        </div>
      )}

      {state && !state.is_observed && (
        <p className="mt-1.5 rounded border border-slate-500/25 bg-slate-500/10 px-2 py-1 text-[10px] text-slate-400">
          Twin estimate — no live sensor on this entity.
        </p>
      )}

      {status === 'warming' && (
        <p className="mt-3 text-[11px] text-slate-500">Warming up…</p>
      )}

      {detail?.forecast && (
        <div className="mt-3">
          <h4 className="panel-title">Forecast</h4>
          <div className="mt-0.5 flex items-center gap-2 text-[10px] text-slate-500">
            <span className="chip bg-surface-700 text-slate-400">{detail.forecast.source}</span>
            {detail.forecast.time_to_critical_sec !== null && (
              <span className="text-red-400">
                critical in {minutes(detail.forecast.time_to_critical_sec)}
              </span>
            )}
          </div>
          <ForecastChart
            forecast={detail.forecast}
            baseline={detail.forecast.baseline_value}
          />
          {detail.forecast.baseline_comparison && (
            <p className="text-[10px] text-slate-500">
              MAE {decimals(detail.forecast.baseline_comparison.model_mae, 3)} vs persistence{' '}
              {decimals(detail.forecast.baseline_comparison.persistence_mae, 3)} (
              {decimals(detail.forecast.baseline_comparison.improvement_pct, 1)}%)
            </p>
          )}
        </div>
      )}

      {detail?.risk_breakdown?.length > 0 && (
        <div className="mt-3">
          <h4 className="panel-title">Risk breakdown</h4>
          <div className="mt-1">
            <RiskBreakdown rows={detail.risk_breakdown} />
          </div>
        </div>
      )}

      {detail && (
        <div className="mt-3 grid grid-cols-2 gap-3">
          {[
            ['Inbound', detail.edges_in, 'src_entity_id'],
            ['Outbound', detail.edges_out, 'dst_entity_id'],
          ].map(([label, edges, key]) => (
            <div key={label}>
              <h4 className="panel-title">{label}</h4>
              <ul className="mt-1 space-y-0.5">
                {edges.slice(0, 6).map((edge) => (
                  <li key={edge.edge_id}>
                    <button
                      type="button"
                      onClick={() => selectEntity(edge[key])}
                      className="truncate text-[10px] text-sky-400 hover:underline"
                      title={edge.edge_type}
                    >
                      {nodesById[edge[key]]?.display_name || edge[key]}
                    </button>
                  </li>
                ))}
                {edges.length === 0 && <li className="text-[10px] text-slate-600">none</li>}
              </ul>
            </div>
          ))}
        </div>
      )}

      <button type="button" onClick={showCascade} className="btn-secondary mt-3 w-full">
        Show cascade
      </button>
    </aside>
  );
}
