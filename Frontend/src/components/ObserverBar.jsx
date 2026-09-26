/**
 * ObserverBar — observer / judge mode controls (Phase 0).
 *
 * Lets a human watch a decision unfold at a readable pace:
 *   Play / Pause · speed presets · Step (one real cycle) · Next decision
 *   (run until the optimiser's next real proposal, then pause) · auto-pause on
 *   proposal · Reset.
 *
 * Every value shown comes from the backend's `demo_status` (GET /demo/status,
 * WS `demo_status`, resync `demo`). Speed is SIM-seconds per REAL second: "1×"
 * is real time (one 30 s cycle every 30 s), "10×" is one cycle every 3 s.
 *
 * Live mode only. The mock replay has no simulation to pause, so the controls
 * are shown disabled there rather than faked.
 */
import { useState } from 'react';
import { Pause, Play, SkipForward, StepForward, RotateCcw, Eye } from 'lucide-react';

import { api } from '../lib/api.js';
import { clock } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

// Judge presets first; 60x (the backend's default demo pace) kept as fast-forward.
const PRESETS = [0.5, 1, 2, 5, 10];
const FAST_FORWARD = 60;

function speedLabel(speed) {
  return `${speed < 1 ? speed : Math.round(speed * 10) / 10}×`;
}

function wallLabel(seconds) {
  if (seconds == null) return '—';
  if (seconds >= 60) return `${Math.round((seconds / 60) * 10) / 10} min`;
  return `${Math.round(seconds * 10) / 10} s`;
}

function reasonText(reason, nodesById) {
  if (!reason) return 'paused';
  if (reason.kind === 'intervention_proposed') {
    const name = nodesById[reason.entity_id]?.display_name || reason.entity_id || 'an entity';
    return `paused · decision needed for ${name}`;
  }
  if (reason.kind === 'step') return 'paused · stepped one cycle';
  return 'paused by operator';
}

export default function ObserverBar() {
  const { demo, mockMode, simTime, cycleNumber, nodesById, toast } = useStore();
  const [busy, setBusy] = useState(false);

  async function control(body) {
    setBusy(true);
    try {
      const status = await api.demoControl(body);
      if (status) useStore.getState().setDemo(status);
    } catch (error) {
      toast(`Control failed: ${error.message}`, 'error');
    } finally {
      setBusy(false);
    }
  }

  const live = !mockMode && !!demo;
  const paused = demo?.status === 'paused';
  const speed = demo?.speed_multiplier;
  const disabled = !live || busy;

  return (
    <div
      data-testid="observer-bar"
      className="flex w-full flex-wrap items-center gap-2 border-b border-surface-700/60 bg-surface-900/60 px-3.5 py-1 text-[11px] text-slate-300 select-none"
    >
      <span className="flex items-center gap-1 font-semibold uppercase tracking-wider text-slate-400">
        <Eye className="h-3.5 w-3.5" /> Observer
      </span>

      {!live ? (
        <span className="text-slate-400">
          {mockMode
            ? 'Recorded replay — observer controls need the live backend (npm run dev:live).'
            : 'Waiting for the live backend…'}
        </span>
      ) : (
        <>
          <span
            data-testid="observer-status"
            className={`chip border text-[10px] font-bold ${
              paused
                ? 'border-amber-500/40 bg-amber-500/10 text-amber-700'
                : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700'
            }`}
          >
            {paused ? '❚❚ ' + reasonText(demo.pause_reason, nodesById).toUpperCase() : '● PLAYING'}
          </span>

          <span className="font-mono tabular-nums" data-testid="observer-clock">
            sim {clock(simTime)} · cycle {cycleNumber}
          </span>

          <span className="h-4 w-px bg-surface-700/60" />

          <button
            type="button"
            className="btn-primary !py-0.5"
            disabled={disabled}
            onClick={() => control({ action: paused ? 'play' : 'pause' })}
            data-testid="observer-play-pause"
          >
            {paused ? <Play className="h-3 w-3" /> : <Pause className="h-3 w-3" />}
            {paused ? 'Play' : 'Pause'}
          </button>
          <button
            type="button"
            className="btn-secondary !py-0.5"
            disabled={disabled}
            onClick={() => control({ action: 'step' })}
            title="Pause and advance exactly one real 30 s simulation cycle"
            data-testid="observer-step"
          >
            <StepForward className="h-3 w-3" /> Step
          </button>
          <button
            type="button"
            className="btn-secondary !py-0.5"
            disabled={disabled}
            onClick={() => control({ action: 'next_decision' })}
            title="Run at the current speed until the optimiser proposes its next intervention, then pause"
            data-testid="observer-next-decision"
          >
            <SkipForward className="h-3 w-3" /> Next decision
          </button>

          <span className="h-4 w-px bg-surface-700/60" />

          <span className="text-slate-400">Speed</span>
          {[...PRESETS, FAST_FORWARD].map((preset) => {
            const active = speed != null && Math.abs(speed - preset) < 1e-9;
            return (
              <button
                key={preset}
                type="button"
                disabled={disabled}
                onClick={() => control({ action: 'set_speed', speed_multiplier: preset })}
                className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${
                  active
                    ? 'border-teal-500/60 bg-teal-500/15 text-teal-700 font-bold'
                    : 'border-surface-700/60 text-slate-400 hover:text-slate-200'
                } disabled:opacity-40`}
                title={preset === FAST_FORWARD ? 'Fast-forward (developer / default demo pace)' : undefined}
                data-testid={`observer-speed-${preset}`}
              >
                {preset === FAST_FORWARD ? `FF ${speedLabel(preset)}` : speedLabel(preset)}
              </button>
            );
          })}
          <span className="font-mono text-slate-400" data-testid="observer-pace">
            1 cycle = {demo.cycle_sec} sim-s ≈ {wallLabel(demo.wall_seconds_per_cycle)} real
          </span>

          <span className="h-4 w-px bg-surface-700/60" />

          <label className="flex items-center gap-1 cursor-pointer" title="Pause automatically when a real intervention is proposed">
            <input
              type="checkbox"
              disabled={disabled}
              checked={!!demo.auto_pause_on_intervention}
              onChange={(e) => control({ auto_pause_on_intervention: e.target.checked })}
              data-testid="observer-auto-pause"
            />
            Pause on proposal
          </label>

          <button
            type="button"
            className="btn-ghost !py-0.5 ml-auto"
            disabled={disabled}
            onClick={() => control({ action: 'reset' })}
            title="Restart the simulation from 14:00 with the same seed"
            data-testid="observer-reset"
          >
            <RotateCcw className="h-3 w-3" /> Reset
          </button>
        </>
      )}
    </div>
  );
}
