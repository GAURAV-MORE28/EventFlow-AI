/**
 * WhatIfPanel — 02_FRONTEND_CONTRACT.md §5.9.
 *
 * Poll every 1000ms while `status === "running"`, give up after 15s with a retry
 * button. Preset buttons cover the four scripted demo scenarios so nobody has to
 * build a scenario by hand on stage.
 */
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  GitBranch,
  Play,
  RotateCcw,
  CloudRain,
  Train,
  DoorClosed,
  Layers,
  CheckCircle2,
  AlertOctagon,
  ArrowRight
} from 'lucide-react';

import { api } from '../lib/api.js';
import { decimals, percent } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

const POLL_MS = 1000;
const TIMEOUT_MS = 15000;

const PRESETS = [
  {
    id: 'gate_5',
    label: 'Gate 5 Closure',
    icon: DoorClosed,
    scenarios: [{ scenario_type: 'gate_closure', params: { entity_id: 'gate_5' } }],
  },
  {
    id: 'metro_c',
    label: 'Metro C Outage',
    icon: Train,
    scenarios: [{ scenario_type: 'transport_outage', params: { entity_id: 'metro_c' } }],
  },
  {
    id: 'heavy_rain',
    label: 'Heavy Rain',
    icon: CloudRain,
    scenarios: [{ scenario_type: 'weather_rain', params: { intensity: 'heavy' } }],
  },
  {
    id: 'surge',
    label: '+20% Attendance',
    icon: Layers,
    scenarios: [{ scenario_type: 'attendance_delta', params: { delta_pct: 20 } }],
  },
];

function Delta({ value, invert = false }) {
  if (value === null || value === undefined) return <span className="text-slate-400 font-mono">—</span>;
  // Rising utilisation is bad; the arrow reads that way without a legend.
  const bad = invert ? value < 0 : value > 0;
  return (
    <span className={`font-mono font-semibold ${bad ? 'text-red-400' : 'text-emerald-400'}`}>
      {value > 0 ? '+' : ''}{value.toFixed(1)}%
    </span>
  );
}

function Comparison({ result }) {
  const rows = [
    ['Peak Utilisation', percent(result.baseline.peak_utilisation), percent(result.scenario.peak_utilisation), result.delta.peak_utilisation_pct],
    ['Load Variance', decimals(result.baseline.load_variance, 3), decimals(result.scenario.load_variance, 3), result.delta.load_variance_pct],
    ['Critical Entities', result.baseline.critical_count, result.scenario.critical_count, null],
  ];

  return (
    <div className="mt-2 space-y-2.5">
      <div className="rounded border border-surface-700/80 bg-surface-950/70 p-2 overflow-hidden">
        <table className="w-full text-[11px]">
          <thead>
            <tr className="border-b border-surface-700/60 text-slate-400 font-mono text-[9px] uppercase tracking-wider">
              <th className="text-left font-normal pb-1">Metric</th>
              <th className="text-right font-normal pb-1">Baseline</th>
              <th className="text-right font-normal pb-1 text-teal-400">Scenario</th>
              <th className="text-right font-normal pb-1">Δ Delta</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-800/60 font-mono">
            {rows.map(([label, base, scenario, delta]) => (
              <tr key={label} className="hover:bg-surface-800/40">
                <td className="py-1 text-slate-300 font-sans text-xs">{label}</td>
                <td className="py-1 text-right text-slate-400">{base}</td>
                <td className="py-1 text-right font-bold text-slate-100">{scenario}</td>
                <td className="py-1 text-right">
                  <Delta value={delta} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {result.delta.new_critical_entities.length > 0 && (
        <div className="rounded border border-red-500/30 bg-red-950/20 p-2">
          <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-red-400">
            <AlertOctagon className="h-3 w-3" />
            <span>New Critical Nodes Predicted ({result.delta.new_critical_entities.length})</span>
          </div>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {result.delta.new_critical_entities.map((id) => (
              <span
                key={id}
                className="chip border border-red-500/40 bg-red-500/15 text-red-200 text-[10px] font-mono"
              >
                {id}
              </span>
            ))}
          </div>
        </div>
      )}

      {result.candidate_interventions?.length > 0 && (
        <div className="rounded border border-surface-700/80 bg-surface-900/60 p-2">
          <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400 mb-1">
            Certified Contingency Actions
          </div>
          <ul className="space-y-1">
            {result.candidate_interventions.slice(0, 3).map((i) => (
              <li key={i.intervention_id} className="flex items-center justify-between text-[11px] gap-2">
                <span className="truncate text-slate-300">{i.title}</span>
                <span
                  className={`chip shrink-0 text-[9px] font-mono font-semibold ${
                    i.certificate?.verdict === 'UNSTABLE'
                      ? 'border border-red-500/30 bg-red-500/10 text-red-400'
                      : 'border border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
                  }`}
                >
                  {i.certificate?.verdict || 'PENDING'}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default function WhatIfPanel() {
  const { whatIf, setWhatIf, toast, setWhatIfOverlay, clearWhatIfOverlay, mockMode } = useStore();
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
    <section className="panel flex min-h-0 flex-col overflow-hidden">
      {/* Header */}
      <div className="panel-header">
        <div className="flex items-center gap-2">
          <div className="flex h-5 w-5 items-center justify-center rounded bg-teal-500/15 text-teal-400">
            <GitBranch className="h-3 w-3" />
          </div>
          <div>
            <h2 className="panel-title">What-If Simulation</h2>
            <div className="text-[9px] font-mono text-slate-400 leading-none">
              BRANCHED COUNTERFACTUAL
            </div>
          </div>
        </div>

        <Link to="/whatif" className="text-[10px] font-semibold text-teal-700 hover:underline">Open lab</Link>
        {whatIf.status === 'complete' && (
          <button
            type="button"
            onClick={() => {
              clearTimers();
              clearWhatIfOverlay();
              setWhatIf({ status: 'idle', result: null, label: null });
            }}
            className="flex items-center gap-1 rounded bg-surface-750 px-2 py-0.5 text-[10px] text-slate-400 hover:text-slate-200 border border-surface-650"
            title="Reset simulation"
          >
            <RotateCcw className="h-2.5 w-2.5" />
            <span>Reset</span>
          </button>
        )}
      </div>

      <div className="flex min-h-0 flex-1 flex-col p-2.5">
        {/* Compact selectable scenario cards */}
        <div className="grid grid-cols-2 gap-1.5">
          {PRESETS.map((preset) => {
            const Icon = preset.icon;
            const isSelected = whatIf.label === preset.label;
            const isRunning = whatIf.status === 'running';

            return (
              <button
                key={preset.label}
                type="button"
                disabled={isRunning || (mockMode && preset.id !== 'gate_5')}
                title={mockMode && preset.id !== 'gate_5' ? 'Only the recorded Gate 5 scenario is available in replay' : undefined}
                onClick={() => run(preset)}
                className={`flex items-center gap-2 rounded-md border p-1.5 text-left transition-all disabled:opacity-40 select-none ${
                  isSelected
                    ? 'border-teal-400/60 bg-teal-950/40 text-teal-200 shadow-sm shadow-teal-500/20'
                    : 'border-surface-700/80 bg-surface-850 text-slate-300 hover:border-slate-500/50 hover:bg-surface-800'
                }`}
              >
                <div
                  className={`flex h-6 w-6 shrink-0 items-center justify-center rounded ${
                    isSelected ? 'bg-teal-500/20 text-teal-300' : 'bg-surface-750 text-slate-400'
                  }`}
                >
                  <Icon className="h-3.5 w-3.5" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[11px] font-medium leading-tight">
                    {preset.label}
                  </div>
                  <div className="text-[9px] font-mono text-slate-400">
                    {isSelected && isRunning ? 'Simulating…' : 'Run scenario'}
                  </div>
                </div>
              </button>
            );
          })}
        </div>

        {/* Results / Status Area */}
        <div className="mt-2 min-h-0 flex-1 overflow-y-auto">
          {whatIf.status === 'idle' && (
            <div className="rounded border border-surface-700/60 bg-surface-900/50 p-2.5 text-center">
              <p className="text-[11px] text-slate-400 leading-relaxed">
                Execute a contingency scenario against a forked digital twin. Real-time operations and live state are never mutated.
              </p>
            </div>
          )}

          {whatIf.status === 'running' && (
            <div className="space-y-2 rounded border border-teal-500/30 bg-teal-950/20 p-3">
              <div className="flex items-center gap-2">
                <span className="h-2 w-2 rounded-full bg-teal-400 animate-ping" />
                <span className="text-xs font-semibold text-teal-300">
                  Simulating scenario: {whatIf.label}
                </span>
              </div>
              <div className="space-y-1.5">
                <div className="skeleton h-2 w-full" />
                <div className="skeleton h-2 w-4/5" />
              </div>
              <p className="text-[10px] font-mono text-slate-400">
                Assimilating topological flow shifts & cascading probability…
              </p>
            </div>
          )}

          {whatIf.status === 'failed' && (
            <div className="rounded border border-red-500/30 bg-red-950/20 p-2.5 text-[11px]">
              <div className="flex items-center gap-1.5 text-red-400 font-medium">
                <AlertOctagon className="h-3.5 w-3.5" />
                <span>Simulation failed to converge</span>
              </div>
              <button
                type="button"
                onClick={() => lastRun && run(lastRun)}
                className="btn-secondary mt-2 text-xs"
              >
                <RotateCcw className="h-3 w-3" />
                <span>Retry</span>
              </button>
            </div>
          )}

          {whatIf.status === 'complete' && whatIf.result && (
            <Comparison result={whatIf.result} />
          )}
        </div>
      </div>
    </section>
  );
}
