/**
 * WhatIfPanel — 02_FRONTEND_CONTRACT.md §5.9.
 *
 * Poll every 1000ms while `status === "running"`, give up after 15s with a retry
 * button. Preset buttons cover the four scripted demo scenarios so nobody has to
 * build a scenario by hand on stage.
 */
import { useEffect, useRef, useState } from 'react';

import { api } from '../lib/api.js';
import { decimals, percent } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

const POLL_MS = 1000;
const TIMEOUT_MS = 15000;

const PRESETS = [
  {
    label: 'Blue line −15%',
    scenarios: [
      { scenario_type: 'metro_capacity_delta', params: { entity_id: 'line_blue', delta_pct: -15 } },
    ],
  },
  {
    label: 'Heavy rain',
    scenarios: [{ scenario_type: 'weather_rain', params: { intensity: 'heavy' } }],
  },
  {
    label: 'Gate 3 closure',
    scenarios: [{ scenario_type: 'gate_closure', params: { entity_id: 'gate_3' } }],
  },
  {
    label: 'Combined',
    scenarios: [
      { scenario_type: 'metro_capacity_delta', params: { entity_id: 'line_blue', delta_pct: -15 } },
      { scenario_type: 'weather_rain', params: { intensity: 'heavy' } },
    ],
  },
];

function Delta({ value, invert = false }) {
  if (value === null || value === undefined) return <span className="text-slate-500">—</span>;
  // Rising utilisation is bad; the arrow reads that way without a legend.
  const bad = invert ? value < 0 : value > 0;
  return (
    <span className={bad ? 'text-red-400' : 'text-green-400'}>
      {value > 0 ? '▲' : '▼'} {Math.abs(value).toFixed(1)}%
    </span>
  );
}

function Comparison({ result }) {
  const rows = [
    ['Peak utilisation', percent(result.baseline.peak_utilisation), percent(result.scenario.peak_utilisation), result.delta.peak_utilisation_pct],
    ['Load variance', decimals(result.baseline.load_variance, 3), decimals(result.scenario.load_variance, 3), result.delta.load_variance_pct],
    ['Critical entities', result.baseline.critical_count, result.scenario.critical_count, null],
  ];

  return (
    <div className="mt-2 space-y-2">
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-slate-500">
            <th className="text-left font-normal" />
            <th className="text-right font-normal">Baseline</th>
            <th className="text-right font-normal">Scenario</th>
            <th className="text-right font-normal">Δ</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, base, scenario, delta]) => (
            <tr key={label} className="border-t border-surface-700">
              <td className="py-1 text-slate-400">{label}</td>
              <td className="py-1 text-right tabular-nums text-slate-300">{base}</td>
              <td className="py-1 text-right tabular-nums font-semibold text-slate-100">
                {scenario}
              </td>
              <td className="py-1 text-right tabular-nums">
                <Delta value={delta} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {result.delta.new_critical_entities.length > 0 && (
        <div>
          <span className="panel-title">New critical</span>
          <div className="mt-1 flex flex-wrap gap-1">
            {result.delta.new_critical_entities.map((id) => (
              <span
                key={id}
                className="chip border border-red-500/30 bg-red-500/15 text-red-300"
              >
                {id}
              </span>
            ))}
          </div>
        </div>
      )}

      {result.candidate_interventions?.length > 0 && (
        <div>
          <span className="panel-title">Candidate actions</span>
          <ul className="mt-1 space-y-0.5">
            {result.candidate_interventions.slice(0, 3).map((i) => (
              <li key={i.intervention_id} className="flex items-baseline gap-1.5 text-[11px]">
                <span
                  className={
                    i.certificate?.verdict === 'UNSTABLE' ? 'text-red-400' : 'text-green-400'
                  }
                >
                  {i.certificate?.verdict || '—'}
                </span>
                <span className="truncate text-slate-400">{i.title}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default function WhatIfPanel() {
  const { whatIf, setWhatIf, toast, setWhatIfOverlay, clearWhatIfOverlay } = useStore();
  const timers = useRef({ poll: null, timeout: null });
  const [lastRun, setLastRun] = useState(null);

  function clearTimers() {
    if (timers.current.poll) clearInterval(timers.current.poll);
    if (timers.current.timeout) clearTimeout(timers.current.timeout);
    timers.current = { poll: null, timeout: null };
  }

  useEffect(() => clearTimers, []);
  // The map overlay is this panel's to own — drop it when the panel unmounts.
  useEffect(() => () => clearWhatIfOverlay(), [clearWhatIfOverlay]);

  async function run(preset) {
    clearTimers();
    clearWhatIfOverlay();
    setLastRun(preset);
    setWhatIf({ status: 'running', result: null, label: preset.label });

    try {
      const accepted = await api.simulate(preset.scenarios, preset.label);

      timers.current.poll = setInterval(async () => {
        try {
          const result = await api.simulation(accepted.simulation_id);
          if (result.status === 'complete') {
            clearTimers();
            setWhatIf({ status: 'complete', result, label: preset.label });
            // Show the projection on the map in the distinct "SIMULATED" style.
            // This never touches `store.entities` — it is a forked-twin result.
            setWhatIfOverlay({ label: preset.label, result });
          } else if (result.status === 'failed') {
            clearTimers();
            setWhatIf({ status: 'failed', result: null, label: preset.label });
          }
        } catch (error) {
          clearTimers();
          setWhatIf({ status: 'failed', result: null, label: preset.label });
          toast(error.message, 'error');
        }
      }, POLL_MS);

      timers.current.timeout = setTimeout(() => {
        clearTimers();
        setWhatIf({ status: 'failed', result: null, label: preset.label });
      }, TIMEOUT_MS);
    } catch (error) {
      setWhatIf({ status: 'failed', result: null, label: preset.label });
      toast(error.message, 'error');
    }
  }

  return (
    <section className="panel flex min-h-0 flex-col p-3">
      <h2 className="panel-title">What if</h2>

      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {PRESETS.map((preset) => (
          <button
            key={preset.label}
            type="button"
            disabled={whatIf.status === 'running'}
            onClick={() => run(preset)}
            className={`rounded-full border px-2.5 py-1 text-[11px] transition-colors disabled:opacity-40 ${
              whatIf.label === preset.label
                ? 'border-sky-500/50 text-sky-300'
                : 'border-surface-600 text-slate-400 hover:border-sky-500/40 hover:text-sky-300'
            }`}
          >
            {preset.label}
          </button>
        ))}
      </div>

      <div className="mt-2 min-h-0 flex-1 overflow-y-auto">
        {whatIf.status === 'idle' && (
          <p className="text-[11px] text-slate-500">
            Run a scenario against a forked twin. The live run is never touched.
          </p>
        )}

        {whatIf.status === 'running' && (
          <div className="space-y-1.5">
            <div className="skeleton h-3 w-2/3" />
            <div className="skeleton h-3 w-1/2" />
            <div className="skeleton h-3 w-3/4" />
            <p className="text-[11px] text-slate-500">Simulating {whatIf.label}…</p>
          </div>
        )}

        {whatIf.status === 'failed' && (
          <div className="text-[11px]">
            <p className="text-red-400">Simulation did not complete.</p>
            <button
              type="button"
              onClick={() => lastRun && run(lastRun)}
              className="btn-secondary mt-1.5"
            >
              Retry
            </button>
          </div>
        )}

        {whatIf.status === 'complete' && whatIf.result && <Comparison result={whatIf.result} />}
      </div>
    </section>
  );
}
