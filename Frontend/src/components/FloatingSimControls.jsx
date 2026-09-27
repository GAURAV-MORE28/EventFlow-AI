import { useState } from 'react';
import { Pause, Play, RotateCcw, Gauge } from 'lucide-react';

import { api } from '../lib/api.js';
import { useStore } from '../store/useStore.js';

const SPEEDS = [1, 5, 10, 30, 60];

export default function FloatingSimControls() {
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
    <div
      className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-2 rounded-lg border border-surface-700/60 bg-surface-900/80 px-3 py-2 shadow-[0_0_20px_rgba(0,0,0,0.5)] backdrop-blur-md transition-all hover:bg-surface-900/95"
      title={title}
    >
      <button
        type="button"
        className="btn-secondary h-8 px-3 gap-2 border-surface-600 hover:border-teal-500/50 hover:text-teal-400"
        disabled={disabled}
        onClick={() => send({ action: paused ? 'play' : 'pause' })}
        aria-label={paused ? 'Resume simulation' : 'Pause simulation'}
      >
        {paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}
        <span className="font-semibold">{paused ? 'Resume' : 'Pause'}</span>
      </button>

      <div className="h-6 w-px bg-surface-700/60 mx-1" />

      <label className="flex items-center gap-1.5 text-xs text-slate-400">
        <Gauge className="h-4 w-4" />
        <select
          id="sim-speed"
          className="rounded border border-surface-700 bg-surface-850 px-2 py-1 text-xs font-semibold text-slate-200 outline-none focus:border-teal-500/50"
          value={speed ?? 10}
          disabled={disabled}
          onChange={(e) => send({ action: 'set_speed', speed_multiplier: Number(e.target.value) })}
        >
          {SPEEDS.map((s) => (
            <option key={s} value={s}>{s}× speed</option>
          ))}
        </select>
      </label>

      <div className="h-6 w-px bg-surface-700/60 mx-1" />

      <button
        type="button"
        className="btn-secondary h-8 px-3 gap-2 border-surface-600 hover:border-amber-500/50 hover:text-amber-400"
        disabled={disabled}
        onClick={() => send({ action: 'reset' }, 'Simulation reset to 14:00')}
        aria-label="Reset simulation"
      >
        <RotateCcw className="h-4 w-4" />
        <span className="font-semibold">Reset</span>
      </button>
    </div>
  );
}
