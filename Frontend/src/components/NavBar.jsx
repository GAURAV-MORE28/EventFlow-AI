/**
 * Product navigation + simulation controls.
 *
 * The controls call `POST /demo/control` (play/pause, speed, reset). In mock
 * mode they are disabled: a recording cannot be paused, sped up or reset from
 * here, and pretending otherwise would be a control that does nothing.
 */
import { useState } from 'react';
import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard,
  CalendarClock,
  BedDouble,
  TrainFront,
  Users,
  ListChecks,
  GitBranch,
  Smartphone,
  MessageSquare,
  BarChart3,
  Pause,
  Play,
  RotateCcw,
  Gauge,
} from 'lucide-react';

import { api } from '../lib/api.js';
import { useStore } from '../store/useStore.js';

const LINKS = [
  { to: '/', label: 'Command Centre', icon: LayoutDashboard, end: true },
  { to: '/events', label: 'Events', icon: CalendarClock },
  { to: '/accommodation', label: 'Hotels', icon: BedDouble },
  { to: '/transport', label: 'Transport', icon: TrainFront },
  { to: '/crowd', label: 'Crowd & Venues', icon: Users },
  { to: '/interventions', label: 'Interventions', icon: ListChecks },
  { to: '/whatif', label: 'What-If', icon: GitBranch },
  { to: '/attendee', label: 'Attendee', icon: Smartphone },
  { to: '/commander', label: 'Commander', icon: MessageSquare },
  { to: '/metrics', label: 'Metrics', icon: BarChart3 },
];

const SPEEDS = [1, 5, 10, 30, 60];

function SimControls() {
  const { mockMode, toast, speed, paused, setSimControl } = useStore();
  const [busy, setBusy] = useState(false);

  async function send(body, message) {
    setBusy(true);
    try {
      const result = await api.demoControl(body);
      setSimControl({ speed: result.speed_multiplier, paused: result.status === 'paused' });
      if (message) toast(message, 'success');
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  const disabled = mockMode || busy;
  const title = mockMode ? 'Recorded replay: controls need the live backend' : undefined;
  return (
    <div className="flex items-center gap-1.5" title={title}>
      <button
        type="button"
        className="btn-secondary h-7 px-2"
        disabled={disabled}
        onClick={() => send({ action: paused ? 'play' : 'pause' })}
        aria-label={paused ? 'Resume simulation' : 'Pause simulation'}
      >
        {paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
        <span>{paused ? 'Resume' : 'Pause'}</span>
      </button>
      <label className="flex items-center gap-1 text-[10px] text-slate-400">
        <Gauge className="h-3.5 w-3.5" />
        <select
          id="sim-speed"
          className="rounded border border-surface-700 bg-surface-850 px-1 py-0.5 text-[11px] text-slate-200"
          value={speed ?? 10}
          disabled={disabled}
          onChange={(e) => send({ action: 'set_speed', speed_multiplier: Number(e.target.value) })}
        >
          {SPEEDS.map((s) => (
            <option key={s} value={s}>{s}× speed</option>
          ))}
        </select>
      </label>
      <button
        type="button"
        className="btn-secondary h-7 px-2"
        disabled={disabled}
        onClick={() => send({ action: 'reset' }, 'Simulation reset to 14:00')}
        aria-label="Reset simulation"
      >
        <RotateCcw className="h-3.5 w-3.5" />
        <span>Reset</span>
      </button>
    </div>
  );
}

export default function NavBar() {
  return (
    <nav className="flex w-full items-center justify-between gap-2 border-b border-surface-700/60 bg-surface-900/70 px-3 py-1 overflow-x-auto">
      <div className="flex items-center gap-0.5">
        {LINKS.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              `flex items-center gap-1.5 whitespace-nowrap rounded px-2 py-1 text-[11px] font-medium transition-colors ${
                isActive
                  ? 'bg-teal-500/15 text-teal-700 border border-teal-500/30'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-surface-800 border border-transparent'
              }`
            }
          >
            <Icon className="h-3.5 w-3.5" />
            <span>{label}</span>
          </NavLink>
        ))}
      </div>
      <SimControls />
    </nav>
  );
}
