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

import { api } from '../lib/api.js';
import { riskColor } from '../lib/colors.js';
import { humanise, minutes, rupees } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

const ATTENDEE_ID = 'att_demo_1';

function TrafficLight({ score, band }) {
  const color = riskColor(band);
  return (
    <div className="flex items-center gap-3">
      <div className="flex flex-col gap-1 rounded-full bg-surface-900 p-1.5">
        {['critical', 'moderate', 'low'].map((level) => (
          <span
            key={level}
            className="h-3 w-3 rounded-full transition-opacity"
            style={{
              backgroundColor: riskColor(level).hex,
              opacity:
                (level === 'critical' && (band === 'critical' || band === 'high')) ||
                (level === 'moderate' && band === 'moderate') ||
                (level === 'low' && band === 'low')
                  ? 1
                  : 0.16,
            }}
          />
        ))}
      </div>
      <div>
        <div className="text-4xl font-bold tabular-nums" style={{ color: color.hex }}>
          {score}
        </div>
        <div className="text-[11px] uppercase tracking-widest" style={{ color: color.hex }}>
          {band} risk
        </div>
      </div>
    </div>
  );
}

function RouteColumn({ title, route, nodesById, worse }) {
  const band = riskColor(route.predicted_crowding_band);
  return (
    <div
      className={`flex-1 rounded-md border p-2 ${
        worse ? 'border-red-500/40 bg-red-500/[0.07]' : 'border-green-500/40 bg-green-500/[0.07]'
      }`}
    >
      <div className="flex items-baseline justify-between">
        <span className="text-[11px] font-semibold text-slate-200">{title}</span>
        <span className="text-[11px] tabular-nums text-slate-300">
          {minutes(route.total_duration_sec)}
        </span>
      </div>

      <div
        className="mt-1 inline-flex rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase"
        style={{ backgroundColor: `${band.hex}22`, color: band.hex }}
      >
        {route.predicted_crowding_band}
      </div>

      <ol className="mt-1.5 space-y-0.5">
        {route.legs.map((leg, index) => (
          <li key={index} className="truncate text-[10px] text-slate-400">
            <span className="text-slate-600">{leg.mode}</span>{' '}
            {nodesById[leg.to_entity_id]?.display_name || humanise(leg.to_entity_id)}
          </li>
        ))}
      </ol>

      {worse && (
        <p className="mt-1.5 text-[10px] font-medium text-red-300">
          Fastest on paper — routes you through the crowd.
        </p>
      )}
    </div>
  );
}

function NudgeCard({ nudge, nodesById, onRespond, busy }) {
  const target = nodesById[nudge.target_entity_id];
  const resolved = nudge.status !== 'pending';

  return (
    <div className="panel p-3">
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-semibold text-slate-100">{nudge.headline}</h3>
        {resolved && <span className="chip bg-surface-700 text-slate-400">{nudge.status}</span>}
      </div>
      <p className="mt-1 text-[11px] leading-relaxed text-slate-400">{nudge.body}</p>

      {/* The trade-off is stated plainly rather than sold. An honest ask gets
          higher real compliance than a hidden cost does. */}
      <dl className="mt-2 grid grid-cols-3 gap-2 rounded bg-surface-900/70 p-2 text-center">
        <div>
          <dt className="text-[9px] uppercase tracking-wider text-slate-500">Extra time</dt>
          <dd className="text-xs font-semibold tabular-nums text-orange-300">
            +{minutes(nudge.tradeoff.extra_travel_sec)}
          </dd>
        </div>
        <div>
          <dt className="text-[9px] uppercase tracking-wider text-slate-500">Credit</dt>
          <dd className="text-xs font-semibold tabular-nums text-green-300">
            {rupees(nudge.tradeoff.credit_paise)}
          </dd>
        </div>
        <div>
          <dt className="text-[9px] uppercase tracking-wider text-slate-500">Perk</dt>
          <dd className="text-xs font-semibold text-sky-300">
            {nudge.tradeoff.perk ? nudge.tradeoff.perk.replace(/_/g, ' ') : '—'}
          </dd>
        </div>
      </dl>

      {target && (
        <p className="mt-1.5 text-[10px] text-slate-500">
          Destination: {target.display_name}
        </p>
      )}

      {!resolved && (
        <div className="mt-2 flex gap-2">
          <button
            type="button"
            className="btn-secondary flex-1"
            disabled={busy}
            onClick={() => onRespond(nudge, false)}
          >
            Decline
          </button>
          <button
            type="button"
            className="btn-primary flex-1"
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
    <div className="min-h-screen bg-surface-900 py-4">
      <div className="mx-auto w-full max-w-[420px] space-y-3 px-3">
        <header className="flex items-center justify-between">
          <div>
            <h1 className="text-base font-semibold text-white">Your journey</h1>
            <p className="text-[10px] text-slate-500">{ATTENDEE_ID}</p>
          </div>
          <Link to="/" className="text-[11px] text-slate-500 hover:text-slate-300">
            Command Centre →
          </Link>
        </header>

        <label className="flex cursor-pointer items-center justify-between rounded-md border border-surface-600 bg-surface-800 px-3 py-2">
          <span className="text-[11px] text-slate-300">Step-free routing</span>
          <input
            type="checkbox"
            checked={accessible}
            onChange={(event) => setAccessible(event.target.checked)}
            className="h-4 w-4 accent-sky-500"
          />
        </label>

        {loading && !journey ? (
          <div className="space-y-3">
            <div className="skeleton h-24" />
            <div className="skeleton h-32" />
            <div className="skeleton h-28" />
          </div>
        ) : (
          journey && (
            <>
              <section className="panel p-3">
                <h2 className="panel-title">Journey risk</h2>
                <div className="mt-2">
                  <TrafficLight
                    score={journey.journey_risk_score}
                    band={journey.journey_risk_band}
                  />
                </div>
                <p className="mt-2 text-[11px] leading-relaxed text-slate-400">{journey.advice}</p>
              </section>

              <section className="panel p-3">
                <h2 className="panel-title">Smart route</h2>
                <div className="mt-2 flex gap-2">
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

        {zoneTarget && (
          <section className="panel p-3">
            <h2 className="panel-title">Zone recommendation</h2>
            <p className="mt-1.5 text-sm font-medium text-slate-100">{zoneTarget.display_name}</p>
            <p className="mt-0.5 text-[11px] text-slate-400">
              Quieter right now, and the organiser is offering{' '}
              {rupees(activeNudge.tradeoff.credit_paise)} to spread the load. It costs you{' '}
              {minutes(activeNudge.tradeoff.extra_travel_sec)} extra.
            </p>
          </section>
        )}

        {activeNudge ? (
          <NudgeCard
            nudge={activeNudge}
            nodesById={nodesById}
            onRespond={respond}
            busy={busy}
          />
        ) : (
          <section className="panel p-3">
            <h2 className="panel-title">Alerts</h2>
            <p className="mt-1.5 text-[11px] text-slate-400">
              Nothing needs your attention. Enjoy the match.
            </p>
          </section>
        )}
      </div>
    </div>
  );
}
