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
import {
  X,
  Radio,
  TrendingUp,
  Activity,
  ArrowRight,
  GitFork,
  Layers,
  AlertTriangle
} from 'lucide-react';

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
      band: (point.upper_90 ?? 0) - (point.lower_90 ?? 0),
    })),
  ];

  return (
    <div className="h-28 mt-1.5">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -26 }}>
          <XAxis dataKey="label" tick={{ fontSize: 9, fill: '#64748B' }} tickLine={false} />
          <YAxis tick={{ fontSize: 9, fill: '#64748B' }} tickLine={false} width={38} />
          <Tooltip
            contentStyle={{
              background: '#FFFFFF',
              border: '1px solid #CBD5E1',
              borderRadius: 6,
              fontSize: 10,
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
            fillOpacity={0.18}
            isAnimationActive={false}
            name="90% interval"
          />
          <Line
            type="monotone"
            dataKey="value"
            stroke="#38BDF8"
            strokeWidth={2}
            dot={{ r: 2.5, fill: '#38BDF8' }}
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
    <ul className="space-y-1.5 font-mono text-[10px]">
      {rows.map((row) => (
        <li key={row.risk_type} className="flex items-center gap-2">
          <span className="w-20 shrink-0 capitalize text-slate-400 font-sans truncate">
            {row.risk_type}
          </span>
          <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-700/60">
            <span
              className="block h-full rounded-full bg-teal-500 transition-all"
              style={{ width: `${(row.score / max) * 100}%` }}
            />
          </span>
          <span className="w-6 text-right tabular-nums text-slate-300 font-semibold">
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
    <aside className="absolute bottom-3 left-3 top-3 z-30 w-84 max-w-[calc(100%-24px)] overflow-y-auto rounded-md border border-surface-650 bg-surface-900/98 p-3 shadow-2xl backdrop-blur select-none">
      {/* Header */}
      <div className="flex items-start justify-between gap-2 border-b border-surface-700/60 pb-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span
              className="h-2 w-2 rounded-full shrink-0"
              style={{ backgroundColor: band.hex }}
            />
            <h3 className="text-sm font-bold text-slate-100 tracking-tight truncate">
              {node?.display_name || selectedEntityId}
            </h3>
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-[10px] font-mono text-slate-400">
            <span className="uppercase tracking-wider">{node?.entity_type}</span>
            <span>·</span>
            <span>{selectedEntityId}</span>
          </div>
        </div>
        <button
          type="button"
          onClick={clearSelection}
          className="rounded p-1 text-slate-400 hover:bg-surface-750 hover:text-slate-100 transition-colors"
          aria-label="Close detail panel"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* Live State Telemetry Grid */}
      {state && (
        <div className="mt-2.5 grid grid-cols-2 gap-1.5 text-[10px]">
          <div className="rounded bg-surface-950/80 p-2 border border-surface-700/40">
            <span className="text-slate-400 text-[9px] uppercase font-bold tracking-wider">Current Load</span>
            <div className="mt-0.5 text-base font-bold font-mono tabular-nums leading-none" style={{ color: band.hex }}>
              {percent(state.utilisation)}
            </div>
          </div>
          <div className="rounded bg-surface-950/80 p-2 border border-surface-700/40">
            <span className="text-slate-400 text-[9px] uppercase font-bold tracking-wider">Risk Score</span>
            <div className="mt-0.5 flex items-baseline gap-1">
              <span className="text-base font-bold font-mono tabular-nums leading-none" style={{ color: band.hex }}>
                {state.risk_score}
              </span>
              <span className="text-[10px] font-bold uppercase" style={{ color: band.hex }}>
                {state.risk_band}
              </span>
            </div>
          </div>
          <div className="rounded bg-surface-950/80 p-2 border border-surface-700/40">
            <span className="text-slate-400 text-[9px] uppercase font-bold tracking-wider">Headcount / Cap</span>
            <div className="mt-0.5 font-mono text-xs font-semibold text-slate-200">
              {integer(state.current_count)} / {integer(node?.nominal_capacity)}
            </div>
          </div>
          <div className="rounded bg-surface-950/80 p-2 border border-surface-700/40">
            <span className="text-slate-400 text-[9px] uppercase font-bold tracking-wider">Throughput Flow</span>
            <div className="mt-0.5 font-mono text-xs font-semibold text-slate-200">
              {decimals(state.flow_rate_per_min, 1)}/min
            </div>
          </div>
        </div>
      )}

      {state && !state.is_observed && (
        <div className="mt-2 rounded border border-slate-600/30 bg-surface-850 p-2 text-[10px] text-slate-400 flex items-center gap-1.5">
          <Radio className="h-3 w-3 text-slate-400 shrink-0" />
          <span>Synthetic Twin Estimate — uninstrumented node</span>
        </div>
      )}

      {status === 'warming' && (
        <div className="mt-3 text-center p-3 rounded bg-surface-850 text-slate-400 text-xs">
          Assimilating node telemetry…
        </div>
      )}

      {/* Multi-horizon Forecast */}
      {detail?.forecast && (
        <div className="mt-3 rounded border border-surface-700/60 bg-surface-950/50 p-2.5">
          <div className="flex items-center justify-between">
            <h4 className="panel-title">Surge Forecast</h4>
            <div className="flex items-center gap-1.5 text-[9px] font-mono">
              <span className="rounded bg-surface-750 px-1.5 py-0.2 text-slate-400">
                {detail.forecast.source}
              </span>
              {detail.forecast.time_to_critical_sec !== null && (
                <span className="text-red-400 font-bold">
                  Crit in {minutes(detail.forecast.time_to_critical_sec)}
                </span>
              )}
            </div>
          </div>

          <ForecastChart
            forecast={detail.forecast}
            baseline={detail.forecast.baseline_value}
          />

          {detail.forecast.baseline_comparison && (
            <p className="mt-1 text-[9px] font-mono text-slate-400">
              MAE {decimals(detail.forecast.baseline_comparison.model_mae, 3)} vs persistence{' '}
              {decimals(detail.forecast.baseline_comparison.persistence_mae, 3)} (
              {decimals(detail.forecast.baseline_comparison.improvement_pct, 1)}% edge)
            </p>
          )}
        </div>
      )}

      {/* Risk Breakdown by component */}
      {detail?.risk_breakdown?.length > 0 && (
        <div className="mt-3 rounded border border-surface-700/60 bg-surface-950/50 p-2.5">
          <h4 className="panel-title mb-2">Risk Breakdown</h4>
          <RiskBreakdown rows={detail.risk_breakdown} />
        </div>
      )}

      {/* Graph Topology Feeds */}
      {detail && (
        <div className="mt-3 grid grid-cols-2 gap-2 text-[10px]">
          {[
            ['Inbound Feeds', detail.edges_in, 'src_entity_id'],
            ['Outbound Discharges', detail.edges_out, 'dst_entity_id'],
          ].map(([label, edges, key]) => (
            <div key={label} className="rounded border border-surface-700/50 bg-surface-950/40 p-2">
              <h4 className="panel-title mb-1">{label}</h4>
              <ul className="space-y-1">
                {edges.slice(0, 6).map((edge) => (
                  <li key={edge.edge_id}>
                    <button
                      type="button"
                      onClick={() => selectEntity(edge[key])}
                      className="truncate text-[10px] font-medium text-teal-400 hover:text-teal-200 transition-colors text-left block w-full"
                      title={edge.edge_type}
                    >
                      → {nodesById[edge[key]]?.display_name || edge[key]}
                    </button>
                  </li>
                ))}
                {edges.length === 0 && <li className="text-[10px] text-slate-600 font-mono">none</li>}
              </ul>
            </div>
          ))}
        </div>
      )}

      {/* Show Cascade Arcs Button */}
      <button
        type="button"
        onClick={showCascade}
        className="btn-secondary mt-3 w-full h-8 text-xs font-semibold gap-1.5"
      >
        <GitFork className="h-3.5 w-3.5 text-teal-400" />
        <span>Show Cascade Propagation Lines</span>
      </button>
    </aside>
  );
}
