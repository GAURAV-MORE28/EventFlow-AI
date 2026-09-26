/**
 * Live board for one or more domains of the city (from `GET /overview`), with
 * the disruptions an operator can report for each entity type.
 *
 * Reporting a disruption posts to `POST /disruptions`: it changes the live
 * simulation (closures move demand to alternatives, capacity cuts create
 * queues). The twin's model is *not* told — it learns from the sensors, as it
 * would from a real incident. Clearing restores the entity.
 */
import { useState } from 'react';
import { AlertOctagon, Eye, EyeOff, X } from 'lucide-react';

import { api } from '../lib/api.js';
import { clock, minutes, percent } from '../lib/format.js';
import { useLiveQuery } from '../lib/useLiveQuery.js';
import { useStore } from '../store/useStore.js';
import { BandChip, ErrorNote, UtilBar } from './PageShell.jsx';

// What an operator can report, per entity type. Each maps to a scenario the
// simulator implements.
const ACTIONS = {
  gate: [{ label: 'Close gate', scenario_type: 'gate_closure' }],
  transport_node: [
    { label: 'Outage', scenario_type: 'transport_outage' },
    { label: 'Capacity −30%', scenario_type: 'metro_capacity_delta', params: { delta_pct: -30 } },
  ],
  transport_route: [
    { label: 'Line suspended', scenario_type: 'transport_outage' },
    { label: 'Capacity −25%', scenario_type: 'metro_capacity_delta', params: { delta_pct: -25 } },
  ],
  road: [{ label: 'Lane closed (−40%)', scenario_type: 'road_capacity_delta', params: { delta_pct: -40 } }],
  parking: [{ label: 'Capacity −50%', scenario_type: 'parking_loss', params: { delta_pct: -50 } }],
};

const DOMAIN_TITLES = {
  venues: 'Venues & gates',
  transport: 'Stations, hubs & lines',
  roads: 'Roads',
  crowd: 'Crowd zones',
  parking: 'Parking',
  hospitality: 'Hotel districts',
  emergency: 'Emergency posts',
};

export function DisruptionsPanel() {
  const { disruptions, nodesById, toast, setDisruptions, mockMode } = useStore();
  const [busy, setBusy] = useState(null);

  async function clear(id) {
    setBusy(id);
    try {
      await api.clearDisruption(id);
      const list = await api.disruptions();
      setDisruptions(list.disruptions);
      toast('Disruption cleared — the entity is back in service', 'success');
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="panel p-3">
      <div className="mb-2 flex items-center gap-2">
        <AlertOctagon className="h-4 w-4 text-red-500" />
        <h2 className="panel-title">Active disruptions</h2>
      </div>
      {!disruptions.length && <p className="text-xs text-slate-400">None. The city is running as scheduled.</p>}
      <ul className="space-y-1.5">
        {disruptions.map((d) => (
          <li key={d.disruption_id} className="flex items-center justify-between gap-2 rounded border border-red-500/30 bg-red-500/[0.04] px-2 py-1 text-xs">
            <span className="text-slate-200">
              <b>{d.scenario_type.replace(/_/g, ' ')}</b>
              {d.params?.entity_id && <> · {nodesById[d.params.entity_id]?.display_name || d.params.entity_id}</>}
              {d.params?.delta_pct !== undefined && <> · {d.params.delta_pct}%</>}
              <span className="text-slate-400"> since {clock(d.started_at)}</span>
            </span>
            <button type="button" className="btn-secondary" disabled={mockMode || busy === d.disruption_id} onClick={() => clear(d.disruption_id)}>
              <X className="h-3 w-3" /> Clear
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function EntityRow({ row, onReport, busy }) {
  const mockMode = useStore((s) => s.mockMode);
  const actions = ACTIONS[row.entity_type] || [];
  return (
    <tr className="border-t border-surface-700/40">
      <td className="py-1.5 pr-2">
        <div className="flex items-center gap-1 font-semibold text-slate-100">
          {row.display_name}
          {row.closed && <span className="chip border border-red-500/40 bg-red-500/10 text-[9px] text-red-700">CLOSED</span>}
        </div>
        <div className="flex items-center gap-1 text-[10px] text-slate-400">
          {row.is_observed ? <Eye className="h-3 w-3" /> : <EyeOff className="h-3 w-3" />}
          {row.is_observed ? 'sensor' : 'twin estimate'} · {row.entity_type.replace(/_/g, ' ')}
        </div>
      </td>
      <td className="w-40 pr-2">
        <div className="mb-0.5 flex justify-between text-[11px] tabular-nums text-slate-200">
          <span>{percent(row.utilisation)}</span>
          <span className="text-slate-400">
            {Math.round(row.current_count).toLocaleString('en-IN')} / {Math.round(row.nominal_capacity).toLocaleString('en-IN')}
          </span>
        </div>
        <UtilBar value={row.utilisation} band={row.risk_band} />
      </td>
      <td className="pr-2"><BandChip band={row.risk_band} /></td>
      <td className="pr-2 tabular-nums text-xs text-slate-300">
        {row.forecast_1800 != null ? percent(row.forecast_1800) : '—'}
      </td>
      <td className="pr-2 tabular-nums text-xs text-slate-300">
        {row.time_to_critical_sec === 0 ? 'now' : row.time_to_critical_sec != null ? minutes(row.time_to_critical_sec) : '—'}
      </td>
      <td className="pr-2 tabular-nums text-xs text-slate-300">
        {row.inflow_per_min == null ? '—' : `${Math.round(row.inflow_per_min)} / ${Math.round(row.outflow_per_min)}`}
      </td>
      <td className="pr-2 tabular-nums text-xs text-slate-300">
        {row.queue_people ? `${Math.round(row.queue_people).toLocaleString('en-IN')}` : '—'}
        {row.queue_delay_sec ? <span className="text-slate-400"> · {minutes(row.queue_delay_sec)}</span> : null}
      </td>
      <td className="py-1">
        <div className="flex flex-wrap gap-1">
          {actions.map((a) => (
            <button
              key={a.label}
              type="button"
              className="btn-secondary px-1.5 py-0.5 text-[10px]"
              disabled={busy || row.closed || mockMode}
              title={mockMode ? 'Needs the live backend' : undefined}
              onClick={() => onReport(row, a)}
            >
              {a.label}
            </button>
          ))}
        </div>
      </td>
    </tr>
  );
}

export default function DomainBoard({ domains }) {
  const { toast, setDisruptions } = useStore();
  const { data, error, reload } = useLiveQuery(() => api.overview(), [], { everyCycles: 2 });
  const [busy, setBusy] = useState(false);

  async function report(row, action) {
    setBusy(true);
    try {
      await api.createDisruption({
        scenario_type: action.scenario_type,
        params: { entity_id: row.entity_id, ...(action.params || {}) },
        label: `${action.label}: ${row.display_name}`,
      });
      const list = await api.disruptions();
      setDisruptions(list.disruptions);
      toast(`${action.label} reported at ${row.display_name}. Demand re-routes from the next cycle.`, 'success');
      reload();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <ErrorNote error={error} />
      {!data && !error && <div className="skeleton h-40" />}
      {data && domains.map((d) => (
        <section key={d} className="panel overflow-x-auto p-3">
          <h2 className="panel-title mb-1">{DOMAIN_TITLES[d]} · {data.domains[d].length}</h2>
          <table className="w-full min-w-[760px] text-left text-xs">
            <thead className="text-[10px] uppercase tracking-wider text-slate-400">
              <tr>
                <th className="pb-1">Location</th><th className="pb-1">Load</th><th className="pb-1">Risk</th>
                <th className="pb-1">In 30 min</th><th className="pb-1">Critical in</th>
                <th className="pb-1">In / out per min</th><th className="pb-1">Queued outside</th>
                <th className="pb-1">Report disruption</th>
              </tr>
            </thead>
            <tbody>
              {data.domains[d].map((row) => (
                <EntityRow key={row.entity_id} row={row} onReport={report} busy={busy} />
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  );
}
