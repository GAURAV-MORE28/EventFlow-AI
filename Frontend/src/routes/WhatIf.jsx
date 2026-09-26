/**
 * What-If lab — compose scenarios, simulate them against a copy of the live
 * city, compare, and (optionally) apply one to the live city.
 *
 * `POST /simulate` forks the live simulator twice (baseline and scenario) and
 * runs both forward; everything shown here is that comparison. Applying a
 * scenario is a separate, explicit action: a disruption via `POST
 * /disruptions`, or a schedule change via `POST /events/{id}`.
 */
import { useEffect, useRef, useState } from 'react';
import { Line, LineChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { Plus, Play, Trash2, Zap } from 'lucide-react';

import PageShell, { ErrorNote } from '../components/PageShell.jsx';
import { api } from '../lib/api.js';
import { integer, minutes, percent } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

const TYPES = {
  attendance_delta: { label: 'Attendance change', fields: ['event_all', 'delta_pct'] },
  gate_closure: { label: 'Gate closure', fields: ['entity:gate'] },
  transport_outage: { label: 'Transport outage', fields: ['entity:transport_node,transport_route'] },
  metro_capacity_delta: { label: 'Transport capacity change', fields: ['entity:transport_node,transport_route', 'delta_pct'] },
  road_capacity_delta: { label: 'Road capacity change', fields: ['entity:road', 'delta_pct'] },
  parking_loss: { label: 'Parking capacity loss', fields: ['entity:parking', 'delta_pct'] },
  weather_rain: { label: 'Rain', fields: ['intensity'] },
  hotel_shortage: { label: 'Hotel rooms offline', fields: ['rooms_offline_pct'] },
  concurrent_event: { label: 'Pop-up concurrent event', fields: ['entity:venue,zone', 'attendance', 'start_offset_min', 'duration_min'] },
  event_delay: { label: 'Event delay', fields: ['event', 'delay_min'] },
};
const DEFAULTS = { delta_pct: -20, rooms_offline_pct: 20, attendance: 8000, start_offset_min: 30, duration_min: 120, delay_min: 30, intensity: 'heavy' };
const METRICS = [
  ['peak_utilisation', 'Peak utilisation', percent],
  ['avg_utilisation', 'Mean peak utilisation', percent],
  ['critical_count', 'Locations reaching critical', (v) => integer(v)],
  ['transport_pressure', 'Transport pressure', percent],
  ['road_pressure', 'Road pressure', percent],
  ['venue_pressure', 'Venue & gate pressure', percent],
  ['hotel_pressure', 'Hotel occupancy', percent],
  ['queued_people', 'Queued people (end)', integer],
  ['late_entries', 'Late entries', integer],
  ['rooms_unmet', 'Unplaced room requests', integer],
  ['unmet_demand', 'Unmet demand (people)', integer],
  ['avg_travel_time_sec', 'Mean trip time', minutes],
];

function Field({ field, scenario, setParam, entities, events }) {
  const [name, arg] = field.split(':');
  const p = scenario.params;
  const cls = 'rounded border border-surface-700 bg-surface-850 px-1.5 py-1 text-xs';
  if (name === 'entity') {
    const types = arg.split(',');
    const list = entities.filter((e) => types.includes(e.entity_type));
    const key = scenario.scenario_type === 'concurrent_event' ? 'venue_entity_id' : 'entity_id';
    return (
      <select aria-label="Location" className={cls} value={p[key] || ''} onChange={(e) => setParam(key, e.target.value)}>
        <option value="">Choose location…</option>
        {list.map((e) => <option key={e.entity_id} value={e.entity_id}>{e.display_name}</option>)}
      </select>
    );
  }
  if (name === 'event' || name === 'event_all') {
    return (
      <select aria-label="Event" className={cls} value={p.event_id || ''} onChange={(e) => setParam('event_id', e.target.value || undefined)}>
        {name === 'event_all' && <option value="">All events</option>}
        {events.map((ev) => <option key={ev.event_id} value={ev.event_id}>{ev.name}</option>)}
      </select>
    );
  }
  if (name === 'intensity') {
    return (
      <select aria-label="Intensity" className={cls} value={p.intensity || 'heavy'} onChange={(e) => setParam('intensity', e.target.value)}>
        {['light', 'moderate', 'heavy'].map((i) => <option key={i} value={i}>{i}</option>)}
      </select>
    );
  }
  const labels = { delta_pct: 'change %', rooms_offline_pct: '% rooms offline', attendance: 'attendance', start_offset_min: 'starts in (min)', duration_min: 'duration (min)', delay_min: 'delay (min)' };
  return (
    <label className="flex items-center gap-1 text-[10px] text-slate-400">
      {labels[name]}
      <input type="number" className={`${cls} w-20`} value={p[name] ?? ''} onChange={(e) => setParam(name, Number(e.target.value))} />
    </label>
  );
}

function newScenario(type = 'gate_closure') {
  const params = {};
  for (const f of TYPES[type].fields) {
    const n = f.split(':')[0];
    if (DEFAULTS[n] !== undefined) params[n] = DEFAULTS[n];
  }
  if (type === 'event_delay') params.event_id = 'evt_demo';
  return { scenario_type: type, params };
}

export default function WhatIf() {
  const { graph, events, toast, setWhatIfOverlay, setDisruptions, upsertEvent, nodesById } = useStore();
  const [scenarios, setScenarios] = useState([newScenario()]);
  const [horizon, setHorizon] = useState(3600);
  const [status, setStatus] = useState('idle');
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const timer = useRef(null);
  useEffect(() => () => clearInterval(timer.current), []);
  const entities = [...graph.nodes].sort((a, b) => a.display_name.localeCompare(b.display_name));
  const name = (id) => nodesById[id]?.display_name || id;

  function update(i, patch) {
    setScenarios((list) => list.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  }

  async function run() {
    clearInterval(timer.current);
    setStatus('running');
    setError(null);
    setResult(null);
    try {
      const accepted = await api.simulate(scenarios, scenarios.map((s) => TYPES[s.scenario_type].label).join(' + '), horizon);
      const started = Date.now();
      timer.current = setInterval(async () => {
        try {
          const r = await api.simulation(accepted.simulation_id);
          if (r.status === 'complete') {
            clearInterval(timer.current);
            setResult(r);
            setStatus('complete');
            setWhatIfOverlay({ label: r.label, result: r });
          } else if (r.status === 'failed' || Date.now() - started > 30000) {
            clearInterval(timer.current);
            setStatus('failed');
          }
        } catch (err) {
          clearInterval(timer.current);
          setStatus('failed');
          setError(err);
        }
      }, 800);
    } catch (err) {
      setStatus('failed');
      setError(err);
    }
  }

  async function applyLive() {
    try {
      for (const s of scenarios) {
        if (s.scenario_type === 'event_delay') {
          const ev = await api.updateEvent(s.params.event_id, { delay_sec: Math.round((s.params.delay_min || 0) * 60) });
          upsertEvent(ev);
        } else {
          await api.createDisruption({ scenario_type: s.scenario_type, params: s.params, label: TYPES[s.scenario_type].label });
        }
      }
      const list = await api.disruptions();
      setDisruptions(list.disruptions);
      toast('Scenario applied to the live city', 'success');
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  const b = result?.baseline;
  const sc = result?.scenario;
  const timeline = (result?.timeline || []).map((p) => ({
    t: `+${Math.round(p.offset_sec / 60)}m`,
    baseline: Math.round(p.baseline_peak * 100),
    scenario: Math.round(p.scenario_peak * 100),
  }));

  return (
    <PageShell
      title="What-If lab"
      subtitle="Simulate disruptions, demand changes and schedule changes against two copies of the live city: one unchanged, one with your scenario. The live city is untouched until you choose to apply."
    >
      <section className="panel p-3">
        <div className="space-y-2">
          {scenarios.map((s, i) => (
            <div key={i} className="flex flex-wrap items-center gap-2 rounded border border-surface-700/60 bg-surface-850 p-2">
              <select
                aria-label="Scenario type"
                className="rounded border border-surface-700 bg-surface-850 px-1.5 py-1 text-xs font-semibold"
                value={s.scenario_type}
                onChange={(e) => update(i, newScenario(e.target.value))}
              >
                {Object.entries(TYPES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
              </select>
              {TYPES[s.scenario_type].fields.map((f) => (
                <Field
                  key={f}
                  field={f}
                  scenario={s}
                  entities={entities}
                  events={events}
                  setParam={(k, v) => update(i, { params: { ...s.params, [k]: v } })}
                />
              ))}
              {scenarios.length > 1 && (
                <button type="button" className="btn-ghost ml-auto" onClick={() => setScenarios((l) => l.filter((_, j) => j !== i))} aria-label="Remove scenario">
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          ))}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button type="button" className="btn-secondary" onClick={() => setScenarios((l) => [...l, newScenario('weather_rain')])}>
            <Plus className="h-3.5 w-3.5" /> Combine another change
          </button>
          <label className="flex items-center gap-1 text-xs text-slate-400">
            Horizon
            <select aria-label="Horizon" className="rounded border border-surface-700 bg-surface-850 px-1.5 py-1 text-xs" value={horizon} onChange={(e) => setHorizon(Number(e.target.value))}>
              {[1800, 3600, 5400, 7200].map((h) => <option key={h} value={h}>{h / 60} min</option>)}
            </select>
          </label>
          <button type="button" className="btn-primary" disabled={status === 'running'} onClick={run}>
            <Play className="h-3.5 w-3.5" /> {status === 'running' ? 'Simulating…' : 'Simulate'}
          </button>
          {status === 'complete' && (
            <button type="button" className="btn-secondary border-red-500/40 text-red-700" onClick={applyLive}>
              <Zap className="h-3.5 w-3.5" /> Apply to live city
            </button>
          )}
        </div>
        {status === 'failed' && <p className="mt-2 text-xs text-red-600">The simulation did not complete. Check the scenario and try again.</p>}
        <ErrorNote error={error} />
      </section>

      {result && b && sc && (
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          <section className="panel overflow-x-auto p-3">
            <h2 className="panel-title mb-2">Baseline vs scenario ({Math.round((result.horizon_sec || horizon) / 60)} min)</h2>
            <table className="w-full text-left text-xs">
              <thead className="text-[10px] uppercase tracking-wider text-slate-400">
                <tr><th className="pb-1">Metric</th><th className="pb-1 text-right">Baseline</th><th className="pb-1 text-right">Scenario</th></tr>
              </thead>
              <tbody>
                {METRICS.map(([key, label, fmt]) => {
                  const worse = (sc[key] ?? 0) > (b[key] ?? 0);
                  const same = Math.abs((sc[key] ?? 0) - (b[key] ?? 0)) < 1e-6;
                  return (
                    <tr key={key} className="border-t border-surface-700/40">
                      <td className="py-1 text-slate-300">{label}</td>
                      <td className="text-right tabular-nums text-slate-400">{fmt(b[key])}</td>
                      <td className={`text-right font-semibold tabular-nums ${same ? 'text-slate-200' : worse ? 'text-red-600' : 'text-emerald-700'}`}>{fmt(sc[key])}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="mt-2 text-[11px] text-slate-400">
              Peak location: {name(b.peak_entity_id)} → {name(sc.peak_entity_id)}.
              {result.delta.new_critical_entities.length > 0 && (
                <span className="text-red-600"> Newly critical: {result.delta.new_critical_entities.map(name).join(', ')}.</span>
              )}
              {result.delta.resolved_critical_entities?.length > 0 && (
                <span className="text-emerald-700"> No longer critical: {result.delta.resolved_critical_entities.map(name).join(', ')}.</span>
              )}
            </p>
          </section>

          <section className="panel p-3">
            <h2 className="panel-title mb-2">Peak utilisation over time (%)</h2>
            <div className="h-48">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={timeline} margin={{ top: 6, right: 12, bottom: 0, left: -18 }}>
                  <CartesianGrid stroke="#E2E8F0" strokeDasharray="3 3" />
                  <XAxis dataKey="t" tick={{ fontSize: 9, fill: '#64748B' }} />
                  <YAxis tick={{ fontSize: 9, fill: '#64748B' }} />
                  <Tooltip contentStyle={{ fontSize: 10 }} />
                  <Legend wrapperStyle={{ fontSize: 10 }} />
                  <Line type="monotone" dataKey="baseline" stroke="#64748B" dot={false} isAnimationActive={false} />
                  <Line type="monotone" dataKey="scenario" stroke="#0D9488" strokeWidth={2} dot={false} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </section>

          <section className="panel overflow-x-auto p-3">
            <h2 className="panel-title mb-2">Biggest changes</h2>
            {!result.top_changes?.length && <p className="text-xs text-slate-400">No location's peak moves by 2 points or more.</p>}
            <table className="w-full text-left text-xs">
              <tbody>
                {(result.top_changes || []).map((c) => (
                  <tr key={c.entity_id} className="border-t border-surface-700/40">
                    <td className="py-1 text-slate-200">{c.display_name}</td>
                    <td className="text-[10px] text-slate-400">{c.entity_type.replace(/_/g, ' ')}</td>
                    <td className="text-right tabular-nums text-slate-400">{percent(c.baseline_peak)}</td>
                    <td className={`text-right font-semibold tabular-nums ${c.scenario_peak > c.baseline_peak ? 'text-red-600' : 'text-emerald-700'}`}>{percent(c.scenario_peak)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="panel p-3">
            <h2 className="panel-title mb-2">Contingency actions for this scenario</h2>
            {!result.candidate_interventions?.length && <p className="text-xs text-slate-400">No action is needed or none would help.</p>}
            <ul className="space-y-1.5">
              {result.candidate_interventions.map((i) => (
                <li key={i.intervention_id} className="rounded border border-surface-700/60 p-2 text-xs">
                  <div className="flex justify-between gap-2">
                    <span className="font-semibold text-slate-100">{i.title}</span>
                    <span className="font-mono text-[10px] text-slate-400">{i.certificate?.verdict}</span>
                  </div>
                  <div className="text-[11px] text-slate-400">
                    Simulated relief {i.estimated_relief_pct.toFixed(1)}%
                    {i.evaluation && <> · peak {Math.round(i.evaluation.root_peak_before * 100)}% → {Math.round(i.evaluation.root_peak_after * 100)}%</>}
                  </div>
                </li>
              ))}
            </ul>
          </section>
        </div>
      )}
    </PageShell>
  );
}
