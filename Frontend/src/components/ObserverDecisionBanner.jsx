/**
 * ObserverDecisionBanner — shown when observer mode paused the simulation
 * because the optimiser queued a REAL proposal (Phase 0).
 *
 *   state → problem → recommendation → PAUSE → human decision → execution
 *
 * It shows only what the backend reported: the affected entity's current
 * utilisation and risk band, the proposed intervention, its certificate, and
 * how long it stays valid. The relief figure is labelled as the optimiser's
 * ESTIMATE. After a decision it reports the action's STATUS and the target's
 * live utilisation — it never claims the action "worked"; outcome measurement
 * is later-phase work.
 */
import { useState } from 'react';
import { PauseCircle, Check, X } from 'lucide-react';

import { api } from '../lib/api.js';
import { riskColor, verdictStyle } from '../lib/colors.js';
import { clock, percent, pct, secondsBetween } from '../lib/format.js';
import { useResolveIntervention } from '../lib/useResolveIntervention.js';
import { useStore } from '../store/useStore.js';

function wallSeconds(simSeconds, demo) {
  if (simSeconds == null || !demo) return null;
  return (simSeconds / demo.cycle_sec) * demo.wall_seconds_per_cycle;
}

function realLabel(seconds) {
  if (seconds == null) return '—';
  if (seconds >= 120) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds)} s`;
}

export default function ObserverDecisionBanner() {
  const { demo, interventions, entities, nodesById, simTime, mockMode, toast } = useStore();
  const { resolve, busyId } = useResolveIntervention();
  const [decision, setDecision] = useState(null); // { id, action, targets, title }
  const [hidden, setHidden] = useState(null); // intervention id the operator dismissed

  const reason = demo?.pause_reason;
  const waiting =
    !mockMode && demo?.status === 'paused' && reason?.kind === 'intervention_proposed';

  async function decide(intervention, action) {
    const ok = await resolve(intervention, action);
    if (!ok) return;
    setDecision({
      id: intervention.intervention_id,
      action,
      title: intervention.title,
      targets: intervention.target_entity_ids || [],
    });
    try {
      const status = await api.demoControl({ action: 'play' });
      if (status) useStore.getState().setDemo(status);
    } catch (error) {
      toast(`Could not resume: ${error.message}`, 'error');
    }
  }

  // --- after the decision: status + observed state only ------------------------
  if (!waiting && decision) {
    const live = interventions.find((i) => i.intervention_id === decision.id);
    const status = live?.status || (decision.action === 'approve' ? 'executing' : 'rejected');
    return (
      <div
        data-testid="observer-after-decision"
        className="absolute left-1/2 top-2 z-30 w-[560px] max-w-[92%] -translate-x-1/2 rounded-md border border-teal-500/40 bg-surface-950/95 p-3 text-xs shadow-panel"
      >
        <div className="flex items-center justify-between">
          <span className="font-bold text-slate-100">
            {decision.action === 'approve' ? 'Approved' : 'Rejected'}: {decision.title}
          </span>
          <button type="button" className="btn-ghost !p-0.5" onClick={() => setDecision(null)} aria-label="Dismiss">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
        <div className="mt-1 text-slate-300">
          Action status <b className="font-mono uppercase">{status}</b> · simulation{' '}
          {demo?.status === 'paused' ? 'paused' : `resumed at ${demo?.speed_multiplier}×`} · sim {clock(simTime)}
        </div>
        {decision.action === 'approve' && (
          <div className="mt-1.5 flex flex-wrap gap-2 font-mono text-[11px] text-slate-300">
            {decision.targets.map((t) => (
              <span key={t} className="rounded border border-surface-700/60 px-1.5 py-0.5">
                {nodesById[t]?.display_name || t}: now {percent(entities[t]?.utilisation)} ({entities[t]?.risk_band || '—'})
              </span>
            ))}
          </div>
        )}
        <div className="mt-1 text-[10px] text-slate-400">
          Live values only. Whether the action helped is not measured here — see the Action HUD as cycles pass.
        </div>
      </div>
    );
  }

  if (!waiting || hidden === reason.intervention_id) return null;

  const intervention = interventions.find((i) => i.intervention_id === reason.intervention_id);
  const entity = entities[reason.entity_id];
  const node = nodesById[reason.entity_id];
  const band = riskColor(entity?.risk_band);
  const cert = intervention?.certificate;
  const verdict = verdictStyle(cert?.verdict);
  const simLeft = intervention ? secondsBetween(simTime, intervention.expires_at) : null;
  const others = interventions.filter(
    (i) => i.status === 'proposed' && i.intervention_id !== reason.intervention_id,
  ).length;
  // With auto-pause on, real proposals can arrive on consecutive cycles, so the
  // next pause can replace the post-decision panel almost immediately. Keep the
  // previous decision's live STATUS visible here instead of losing it.
  const previous =
    decision && decision.id !== reason.intervention_id
      ? {
          ...decision,
          status:
            interventions.find((i) => i.intervention_id === decision.id)?.status ||
            (decision.action === 'approve' ? 'executing' : 'rejected'),
        }
      : null;

  return (
    <div
      data-testid="observer-decision"
      className="absolute left-1/2 top-2 z-30 w-[620px] max-w-[94%] -translate-x-1/2 rounded-md border-2 border-amber-500/60 bg-surface-950/95 p-3 text-xs shadow-panel"
    >
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-1.5 font-bold uppercase tracking-wider text-amber-700">
          <PauseCircle className="h-4 w-4" /> Simulation paused — decision required
        </span>
        <span className="font-mono text-[11px] text-slate-400">
          cycle {reason.cycle_number} · sim {clock(reason.sim_time)} · speed {demo.speed_multiplier}×
        </span>
      </div>

      {previous && (
        <div data-testid="observer-previous-decision" className="mt-1 text-[11px] text-slate-400">
          Previous decision: {previous.action === 'approve' ? 'approved' : 'rejected'} “{previous.title}” — status{' '}
          <b className="font-mono uppercase">{previous.status}</b>
        </div>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <span className="text-slate-400">Problem at</span>
        <b className="text-slate-100">{node?.display_name || reason.entity_id}</b>
        <span className="font-mono">load {percent(entity?.utilisation)}</span>
        <span
          className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase"
          style={{ color: band.hex, backgroundColor: `${band.hex}20`, border: `1px solid ${band.hex}40` }}
        >
          {entity?.risk_band || '—'} · {entity?.risk_score ?? '—'}
        </span>
        {!entity?.is_observed && entity && (
          <span className="text-[10px] text-slate-400">(twin estimate, not a sensor reading)</span>
        )}
      </div>

      {intervention ? (
        <div className="mt-2 rounded border border-surface-700/60 p-2">
          <div className="flex items-center justify-between gap-2">
            <b className="text-slate-100">{intervention.title}</b>
            <span className="text-[10px] font-bold" style={{ color: verdict.hex }}>
              {verdict.label}
            </span>
          </div>
          <div className="mt-0.5 text-slate-300">{intervention.description}</div>
          {cert?.reason && <div className="mt-0.5 text-[11px] text-slate-400">Certificate: {cert.reason}</div>}
          <div className="mt-1 font-mono text-[11px] text-slate-400">
            optimiser estimate {pct(intervention.estimated_relief_pct)} relief · expires in{' '}
            {simLeft == null ? '—' : `${Math.round(simLeft / 60)} sim-min`} (≈{' '}
            {realLabel(wallSeconds(simLeft, demo))} real at {demo.speed_multiplier}× — clock frozen while paused)
          </div>
        </div>
      ) : (
        <div className="mt-2 text-slate-400">Proposal {reason.intervention_id} is no longer in the queue.</div>
      )}

      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          className="btn-primary"
          disabled={!intervention || busyId === intervention?.intervention_id || intervention?.status !== 'proposed'}
          onClick={() => decide(intervention, 'approve')}
          data-testid="observer-approve"
        >
          <Check className="h-3.5 w-3.5" /> Approve &amp; resume
        </button>
        <button
          type="button"
          className="btn-danger"
          disabled={!intervention || busyId === intervention?.intervention_id || intervention?.status !== 'proposed'}
          onClick={() => decide(intervention, 'reject')}
          data-testid="observer-reject"
        >
          <X className="h-3.5 w-3.5" /> Reject &amp; resume
        </button>
        <button
          type="button"
          className="btn-ghost"
          onClick={() => setHidden(reason.intervention_id)}
          title="Hide this panel and stay paused (use the queue or Play to continue)"
        >
          Keep paused
        </button>
        {others > 0 && <span className="ml-auto text-[10px] text-slate-400">{others} other proposal(s) in the queue</span>}
      </div>
    </div>
  );
}
