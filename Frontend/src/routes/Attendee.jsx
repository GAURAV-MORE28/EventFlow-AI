/**
 * Attendee PWA — 02_FRONTEND_CONTRACT.md §5.11. Mobile viewport, max 420px.
 *
 * Four cards: journey risk, smart route, zone recommendation, nudge.
 *
 * The design point: **the shortest route must be visibly labelled as the worse
 * option.** That contrast is the attendee-side equivalent of the certificate
 * badge, and it is why the API returns both routes rather than just the good one.
 */
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Navigation,
  Clock,
  Coins,
  Gift,
  AlertTriangle,
  CheckCircle2,
  ArrowRight,
  Shield,
  ArrowLeft,
  Sparkles,
  MapPin,
  Accessibility,
} from 'lucide-react';

import { api } from '../lib/api.js';
import { riskColor } from '../lib/colors.js';
import { humanise, minutes, rupees } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

const ATTENDEE_ID = 'att_demo_1';

function TrafficLight({ score, band }) {
  const color = riskColor(band);
  return (
    <div className="flex items-center gap-3.5">
      <div className="flex flex-col gap-1.5 rounded-full bg-surface-950 p-2 border border-surface-700/60 shadow-inner">
        {['critical', 'moderate', 'low'].map((level) => {
          const isActive =
            (level === 'critical' && (band === 'critical' || band === 'high')) ||
            (level === 'moderate' && band === 'moderate') ||
            (level === 'low' && band === 'low');
          const lvlColor = riskColor(level);
          return (
            <span
              key={level}
              className={`h-3 w-3 rounded-full transition-all duration-300 ${
                isActive ? 'scale-110 shadow-sm' : 'opacity-20'
              }`}
              style={{
                backgroundColor: lvlColor.hex,
                boxShadow: isActive ? `0 0 8px ${lvlColor.hex}80` : 'none',
              }}
            />
          );
        })}
      </div>
      <div>
        <div className="flex items-baseline gap-1.5">
          <span className="text-4xl font-extrabold font-mono tabular-nums tracking-tight" style={{ color: color.hex }}>
            {score}
          </span>
          <span className="text-xs text-slate-400 font-mono">/ 100</span>
        </div>
        <div
          className="mt-0.5 inline-flex items-center gap-1 rounded px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider"
          style={{ backgroundColor: `${color.hex}20`, color: color.hex, border: `1px solid ${color.hex}40` }}
        >
          {band} Congestion Risk
        </div>
      </div>
    </div>
  );
}

function RouteColumn({ title, route, nodesById, worse }) {
  const band = riskColor(route.predicted_crowding_band);
  return (
    <div
      className={`flex-1 rounded-md border p-3 flex flex-col justify-between transition-all ${
        worse
          ? 'border-rose-500/40 bg-rose-950/15'
          : 'border-emerald-500/40 bg-emerald-950/15 shadow-sm'
      }`}
    >
      <div>
        <div className="flex items-center justify-between gap-1">
          <div className="flex items-center gap-1.5">
            {worse ? (
              <AlertTriangle className="h-3.5 w-3.5 text-rose-400 shrink-0" />
            ) : (
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400 shrink-0" />
            )}
            <span className="text-xs font-bold text-white tracking-tight">{title}</span>
          </div>
          <span className="text-xs font-mono font-bold tabular-nums text-slate-200">
            {minutes(route.total_duration_sec)}
          </span>
        </div>

        <div className="mt-2">
          <span
            className="inline-block rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider font-mono"
            style={{ backgroundColor: `${band.hex}22`, color: band.hex, border: `1px solid ${band.hex}30` }}
          >
            {route.predicted_crowding_band}
          </span>
        </div>

        <ol className="mt-2.5 space-y-1">
          {route.legs.map((leg, index) => (
            <li key={index} className="flex items-center gap-1.5 text-[10px] text-slate-300">
              <span className="rounded bg-surface-800/80 px-1 py-0.2 text-[8px] font-mono uppercase text-slate-400">
                {leg.mode}
              </span>
              <span className="truncate">
                {nodesById[leg.to_entity_id]?.display_name || humanise(leg.to_entity_id)}
              </span>
            </li>
          ))}
        </ol>
      </div>

      {worse && (
        <div className="mt-2.5 pt-2 border-t border-rose-500/20">
          <p className="text-[10px] font-medium leading-tight text-rose-300">
            Fastest on paper — routes you through heavy crowd pinch points.
          </p>
        </div>
      )}
      {!worse && (
        <div className="mt-2.5 pt-2 border-t border-emerald-500/20">
          <p className="text-[10px] font-medium leading-tight text-emerald-300">
            Optimised for smooth throughput and minimal waiting.
          </p>
        </div>
      )}
    </div>
  );
}

function NudgeCard({ nudge, nodesById, onRespond, busy }) {
  const target = nodesById[nudge.target_entity_id];
  const resolved = nudge.status !== 'pending';

  return (
    <div className="panel p-3.5 border-sky-500/40 bg-surface-900/90 shadow-lg">
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <Sparkles className="h-4 w-4 text-sky-400 shrink-0" />
          <h3 className="text-xs font-bold text-white tracking-tight">{nudge.headline}</h3>
        </div>
        {resolved && (
          <span className="chip bg-surface-800 text-slate-400 font-mono text-[9px] uppercase border border-surface-700/60">
            {nudge.status}
          </span>
        )}
      </div>

      <p className="mt-1.5 text-[11px] leading-relaxed text-slate-300">{nudge.body}</p>

      {/* Trade-off capsule */}
      <dl className="mt-3 grid grid-cols-3 gap-1.5 rounded border border-surface-700/50 bg-surface-950/70 p-2 text-center">
        <div>
          <dt className="flex items-center justify-center gap-1 text-[9px] uppercase tracking-wider text-slate-400">
            <Clock className="h-2.5 w-2.5 text-slate-400" />
            <span>Time</span>
          </dt>
          <dd className="mt-0.5 text-xs font-bold font-mono tabular-nums text-amber-300">
            +{minutes(nudge.tradeoff.extra_travel_sec)}
          </dd>
        </div>
        <div>
          <dt className="flex items-center justify-center gap-1 text-[9px] uppercase tracking-wider text-slate-400">
            <Coins className="h-2.5 w-2.5 text-emerald-400" />
            <span>Reward</span>
          </dt>
          <dd className="mt-0.5 text-xs font-bold font-mono tabular-nums text-emerald-300">
            {rupees(nudge.tradeoff.credit_paise)}
          </dd>
        </div>
        <div>
          <dt className="flex items-center justify-center gap-1 text-[9px] uppercase tracking-wider text-slate-400">
            <Gift className="h-2.5 w-2.5 text-sky-400" />
            <span>Perk</span>
          </dt>
          <dd className="mt-0.5 text-xs font-bold text-sky-300 truncate">
            {nudge.tradeoff.perk ? nudge.tradeoff.perk.replace(/_/g, ' ') : '—'}
          </dd>
        </div>
      </dl>

      {target && (
        <div className="mt-2.5 flex items-center gap-1.5 text-[10px] text-slate-400">
          <MapPin className="h-3 w-3 text-slate-400 shrink-0" />
          <span>Destination: <strong className="text-slate-200">{target.display_name}</strong></span>
        </div>
      )}

      {!resolved && (
        <div className="mt-3 flex gap-2">
          <button
            type="button"
            className="btn-secondary flex-1 h-8 text-xs font-semibold"
            disabled={busy}
            onClick={() => onRespond(nudge, false)}
          >
            Decline
          </button>
          <button
            type="button"
            className="btn-primary flex-1 h-8 text-xs font-semibold shadow-md shadow-sky-950/40"
            disabled={busy}
            onClick={() => onRespond(nudge, true)}
          >
            Accept
          </button>
        </div>
      )}
    </div>
  );
}

export default function Attendee() {
  const { nodesById, journey, setJourney, nudges, setNudges, updateNudgeStatus, toast } =
    useStore();
  const [accessible, setAccessible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const [journeyResult, nudgeResult] = await Promise.all([
          api.journey({
            attendee_id: ATTENDEE_ID,
            segment_id: accessible ? 'accessibility_constrained' : 'price_sensitive',
            origin_entity_id: 'hotel_core_cluster',
            destination_entity_id: 'stadium_main',
          }),
          api.nudges(ATTENDEE_ID),
        ]);
        if (cancelled) return;
        setJourney(journeyResult);
        setNudges(nudgeResult.nudges);
      } catch (error) {
        if (!cancelled && !error.isWarmingUp) toast(error.message, 'error');
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accessible]);

  async function respond(nudge, accepted) {
    setBusy(true);
    updateNudgeStatus(nudge.nudge_id, accepted ? 'accepted' : 'declined');
    try {
      await api.respondToNudge(nudge.nudge_id, accepted);
      toast(accepted ? 'Thanks — your route is updated.' : 'Noted.', 'success');
    } catch (error) {
      updateNudgeStatus(nudge.nudge_id, 'pending');
      toast(error.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  const activeNudge = nudges.find((n) => n.status === 'pending') || nudges[0];
  const zoneTarget = activeNudge ? nodesById[activeNudge.target_entity_id] : null;

  return (
    <div className="min-h-screen bg-surface-950 py-4 font-sans text-slate-100">
      <div className="mx-auto w-full max-w-[420px] space-y-3 px-3">
        {/* Mobile Header Bar */}
        <header className="flex items-center justify-between border-b border-surface-700/60 pb-3">
          <div className="flex items-center gap-2">
            <span className="flex h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
            <div>
              <h1 className="text-sm font-bold text-white tracking-tight">Your Match Day Journey</h1>
              <p className="text-[10px] font-mono text-slate-400">ID: {ATTENDEE_ID}</p>
            </div>
          </div>
          <Link
            to="/"
            className="flex items-center gap-1 rounded bg-surface-850 px-2 py-1 text-[11px] font-medium text-slate-300 hover:text-white border border-surface-700/60 transition-colors"
          >
            <span>Command</span>
            <ArrowRight className="h-3 w-3" />
          </Link>
        </header>

        {/* Step-free Accessibility Toggle */}
        <div className="flex items-center justify-between rounded-md border border-surface-700/60 bg-surface-900/80 px-3 py-2">
          <div className="flex items-center gap-2">
            <Accessibility className="h-4 w-4 text-sky-400" />
            <span className="text-xs font-medium text-slate-200">Step-free routing (accessible)</span>
          </div>
          <label className="relative inline-flex cursor-pointer items-center">
            <input
              type="checkbox"
              checked={accessible}
              onChange={(event) => setAccessible(event.target.checked)}
              className="peer sr-only"
            />
            <div className="h-5 w-9 rounded-full bg-surface-700 peer-checked:bg-sky-500 peer-focus:outline-none transition-colors after:absolute after:left-[2px] after:top-[2px] after:h-4 after:w-4 after:rounded-full after:bg-white after:transition-all after:content-[''] peer-checked:after:translate-x-full"></div>
          </label>
        </div>

        {loading && !journey ? (
          <div className="space-y-3">
            <div className="skeleton h-28" />
            <div className="skeleton h-36" />
            <div className="skeleton h-28" />
          </div>
        ) : (
          journey && (
            <>
              {/* Journey Risk Card */}
              <section className="panel p-3.5">
                <div className="flex items-center justify-between">
                  <h2 className="panel-title">Real-Time Transit Risk</h2>
                  <span className="text-[9px] font-mono text-slate-400">UPDATED JUST NOW</span>
                </div>
                <div className="mt-3">
                  <TrafficLight
                    score={journey.journey_risk_score}
                    band={journey.journey_risk_band}
                  />
                </div>
                <div className="mt-3 rounded border border-surface-700/40 bg-surface-950/60 p-2.5">
                  <p className="text-[11px] leading-relaxed text-slate-300">{journey.advice}</p>
                </div>
              </section>

              {/* Smart Route Comparison */}
              <section className="panel p-3.5">
                <div className="flex items-center justify-between mb-2">
                  <h2 className="panel-title">Smart Route Intelligence</h2>
                  <span className="text-[9px] font-mono text-sky-400">AI BALANCED</span>
                </div>
                <div className="flex flex-col sm:flex-row gap-2">
                  <RouteColumn
                    title="Recommended"
                    route={journey.recommended_route}
                    nodesById={nodesById}
                    worse={false}
                  />
                  <RouteColumn
                    title="Shortest"
                    route={journey.shortest_route}
                    nodesById={nodesById}
                    worse
                  />
                </div>
              </section>
            </>
          )
        )}

        {/* Zone recommendation */}
        {zoneTarget && (
          <section className="panel p-3.5 border-emerald-500/30 bg-surface-900/80">
            <div className="flex items-center justify-between">
              <h2 className="panel-title">Zone Recommendation</h2>
              <span className="chip bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 text-[9px] font-mono">
                LOW DENSITY
              </span>
            </div>
            <div className="mt-2 flex items-center gap-1.5">
              <MapPin className="h-4 w-4 text-emerald-400 shrink-0" />
              <p className="text-sm font-bold text-white">{zoneTarget.display_name}</p>
            </div>
            <p className="mt-1 text-[11px] text-slate-300 leading-relaxed">
              Quieter right now, and the organiser is offering{' '}
              <strong className="text-emerald-300 font-mono">{rupees(activeNudge.tradeoff.credit_paise)}</strong> to spread the load.
              It costs you <strong className="text-amber-300 font-mono">+{minutes(activeNudge.tradeoff.extra_travel_sec)}</strong> extra travel time.
            </p>
          </section>
        )}

        {/* Nudge Card or Alerts */}
        {activeNudge ? (
          <NudgeCard
            nudge={activeNudge}
            nodesById={nodesById}
            onRespond={respond}
            busy={busy}
          />
        ) : (
          <section className="panel p-3.5">
            <h2 className="panel-title">Operational Alerts</h2>
            <p className="mt-2 text-xs text-slate-400">
              Nothing needs your attention. Enjoy the match.
            </p>
          </section>
        )}
      </div>
    </div>
  );
}

