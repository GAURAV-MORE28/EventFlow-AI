/**
 * Attendee PWA — 02_FRONTEND_CONTRACT.md §5.11. Mobile viewport.
 *
 * Everything here is planned by the backend against the live simulation:
 * routes are costed with the crowding the look-ahead projection expects when
 * the attendee would reach each point; departure options compare now / +15 /
 * +30 / +45 minutes; the return trip is planned for after the event ends; hotel
 * suggestions come from the accommodation recommender. Nudges arrive only when
 * an approved intervention touches this attendee's own route or hotel, and
 * accepting one changes their plan.
 */
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Navigation,
  Clock,
  Coins,
  Gift,
  CheckCircle2,
  ArrowRight,
  Sparkles,
  MapPin,
  BedDouble,
  CornerDownLeft,
  Timer,
} from 'lucide-react';

import { api } from '../lib/api.js';
import { riskColor } from '../lib/colors.js';
import { clock, humanise, minutes, percent, rupees } from '../lib/format.js';
import { useLiveQuery } from '../lib/useLiveQuery.js';
import { useStore } from '../store/useStore.js';
import MenuToggle from '../components/MenuToggle.jsx';

const SEGMENTS = [
  ['price_sensitive', 'Budget-conscious'],
  ['time_sensitive', 'In a hurry'],
  ['accessibility_constrained', 'Step-free access'],
  ['group', 'Travelling as a group'],
  ['premium', 'Premium'],
];
const PRIORITIES = [
  ['balanced', 'Balanced'],
  ['least_crowded', 'Least crowded'],
  ['fastest', 'Fastest'],
];

function RiskScore({ score, band }) {
  const color = riskColor(band);
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-4xl font-extrabold font-mono tabular-nums" style={{ color: color.hex }}>{score}</span>
      <span className="text-xs text-slate-400">/ 100</span>
      <span
        className="rounded px-2 py-0.5 text-[10px] font-bold uppercase"
        style={{ backgroundColor: `${color.hex}20`, color: color.hex, border: `1px solid ${color.hex}40` }}
      >
        {band} crowding
      </span>
    </div>
  );
}

function RouteCard({ route, nodesById, highlight = false }) {
  const band = riskColor(route.predicted_crowding_band);
  return (
    <div className={`rounded-md border p-2.5 ${highlight ? 'border-emerald-500/50 bg-emerald-500/[0.06]' : 'border-surface-700/60 bg-surface-850'}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          {highlight ? <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" /> : <Navigation className="h-3.5 w-3.5 text-slate-400" />}
          <span className="text-xs font-bold text-slate-100">{route.label}</span>
        </div>
        <span className="font-mono text-xs font-bold tabular-nums text-slate-200">{minutes(route.total_duration_sec)}</span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[10px]">
        <span className="rounded px-1.5 py-0.5 font-bold uppercase" style={{ backgroundColor: `${band.hex}22`, color: band.hex }}>
          {route.predicted_crowding_band}
        </span>
        <span className="text-slate-400">peak {percent(route.peak_utilisation)} on the way</span>
        {route.congestion_delay_sec > 0 && <span className="text-amber-600">incl. {minutes(route.congestion_delay_sec)} queueing</span>}
      </div>
      <ol className="mt-1.5 flex flex-wrap items-center gap-1 text-[10px] text-slate-300">
        {route.legs.map((leg, i) => (
          <li key={i} className="flex items-center gap-1">
            {i > 0 && <ArrowRight className="h-2.5 w-2.5 text-slate-400" />}
            <span className="rounded bg-surface-800 px-1 font-mono text-[8px] uppercase text-slate-400">{leg.mode}</span>
            <span>{nodesById[leg.to_entity_id]?.display_name || humanise(leg.to_entity_id)}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function NudgeCard({ nudge, nodesById, onRespond, busy }) {
  const target = nodesById[nudge.target_entity_id];
  const resolved = nudge.status !== 'pending';
  return (
    <div className="panel border-sky-500/40 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <Sparkles className="h-4 w-4 shrink-0 text-sky-500" />
          <h3 className="text-xs font-bold text-slate-100">{nudge.headline}</h3>
        </div>
        {resolved && <span className="chip border border-surface-700/60 bg-surface-800 font-mono text-[9px] uppercase text-slate-400">{nudge.status}</span>}
      </div>
      <p className="mt-1.5 text-[11px] leading-relaxed text-slate-300">{nudge.body}</p>
      <dl className="mt-2 grid grid-cols-3 gap-1.5 rounded border border-surface-700/50 bg-surface-900 p-2 text-center">
        <div>
          <dt className="flex items-center justify-center gap-1 text-[9px] uppercase text-slate-400"><Clock className="h-2.5 w-2.5" />Time</dt>
          <dd className="mt-0.5 font-mono text-xs font-bold text-amber-600">+{minutes(nudge.tradeoff.extra_travel_sec)}</dd>
        </div>
        <div>
          <dt className="flex items-center justify-center gap-1 text-[9px] uppercase text-slate-400"><Coins className="h-2.5 w-2.5" />Reward</dt>
          <dd className="mt-0.5 font-mono text-xs font-bold text-emerald-700">{rupees(nudge.tradeoff.credit_paise)}</dd>
        </div>
        <div>
          <dt className="flex items-center justify-center gap-1 text-[9px] uppercase text-slate-400"><Gift className="h-2.5 w-2.5" />Perk</dt>
          <dd className="mt-0.5 truncate text-xs font-bold text-sky-700">{nudge.tradeoff.perk ? nudge.tradeoff.perk.replace(/_/g, ' ') : '—'}</dd>
        </div>
      </dl>
      {target && (
        <div className="mt-2 flex items-center gap-1.5 text-[10px] text-slate-400">
          <MapPin className="h-3 w-3" /> Suggested: <strong className="text-slate-200">{target.display_name}</strong>
        </div>
      )}
      {!resolved && (
        <div className="mt-2.5 flex gap-2">
          <button type="button" className="btn-secondary h-8 flex-1" disabled={busy} onClick={() => onRespond(nudge, false)}>Decline</button>
          <button type="button" className="btn-primary h-8 flex-1" disabled={busy} onClick={() => onRespond(nudge, true)}>Accept</button>
        </div>
      )}
    </div>
  );
}

function HotelCard({ propertyId, eventId, segment }) {
  const { data } = useLiveQuery(
    () => api.recommendStay({ event_id: eventId, segment_id: segment, current_property_id: propertyId || null, limit: 3 }),
    [propertyId, eventId, segment],
    { everyCycles: 10 },
  );
  if (!data) return null;
  const current = data.current;
  const needsAlternative = !current || current.status !== 'available';
  return (
    <section className="panel p-3">
      <div className="flex items-center gap-1.5">
        <BedDouble className="h-4 w-4 text-teal-600" />
        <h2 className="panel-title">{current ? 'Your hotel' : 'Where to stay'}</h2>
      </div>
      {current && (
        <div className="mt-1.5 text-xs text-slate-300">
          <b className="text-slate-100">{current.name}</b> · {percent(current.occupancy)} occupied ·{' '}
          <span className={current.status === 'saturated' ? 'text-red-600' : current.status === 'limited' ? 'text-amber-600' : 'text-emerald-700'}>
            {current.status}
          </span>
        </div>
      )}
      {needsAlternative && data.options.length > 0 && (
        <>
          <p className="mt-1.5 text-[11px] text-slate-400">{data.explanation}</p>
          <ul className="mt-1.5 space-y-1">
            {data.options.map((o) => (
              <li key={o.property.property_id} className="rounded border border-surface-700/60 bg-surface-850 px-2 py-1 text-[11px]">
                <div className="font-semibold text-slate-100">{o.property.name} <span className="font-normal text-slate-400">· {o.property.zone}</span></div>
                <div className="text-slate-400">{o.reasons.slice(0, 3).join(' · ')}</div>
              </li>
            ))}
          </ul>
        </>
      )}
      {!needsAlternative && <p className="mt-1 text-[11px] text-slate-400">Your hotel has rooms and normal load; no change needed.</p>}
    </section>
  );
}

export default function Attendee() {
  const { nodesById, graph, events, event, toast, mockMode } = useStore();
  const [plan, setPlan] = useState({
    attendee_id: 'att_demo_1',
    segment_id: 'price_sensitive',
    origin: '',
    destination: '',
    priority: 'balanced',
    mode: 'any',
  });
  const [busy, setBusy] = useState(false);
  const hotels = useLiveQuery(() => api.hotels({ sort: 'availability' }), [], { everyCycles: 20 });
  const hotelIds = useMemo(() => new Set((hotels.data?.hotels || []).map((h) => h.property_id)), [hotels.data]);

  // Defaults come from the ACTIVE world (demo or generated): its primary event's
  // venue and one of its hotels — never hard-coded ids.
  useEffect(() => {
    setPlan((p) => {
      const destination = p.destination && nodesById[p.destination] ? p.destination : event?.venue_entity_id || '';
      const first = hotels.data?.hotels?.[0]?.property_id;
      const origin = p.origin && (hotelIds.has(p.origin) || nodesById[p.origin]) ? p.origin : first || p.origin;
      return destination === p.destination && origin === p.origin ? p : { ...p, destination, origin };
    });
  }, [event?.venue_entity_id, hotelIds, nodesById, hotels.data]);

  const originIsHotel = hotelIds.has(plan.origin);
  const request = useMemo(() => ({
    attendee_id: plan.attendee_id,
    segment_id: plan.segment_id,
    origin_entity_id: plan.origin,
    destination_entity_id: plan.destination,
    priority: plan.priority,
    transport_preference: plan.mode,
    include_return: true,
  }), [plan]);
  const journey = useLiveQuery(
    () => (request.origin_entity_id && request.destination_entity_id ? api.journey(request) : Promise.resolve(null)),
    [JSON.stringify(request)],
    { everyCycles: 10 },
  );
  const nudges = useLiveQuery(() => api.nudges(plan.attendee_id), [plan.attendee_id], { everyCycles: 2 });

  const destinations = useMemo(() => {
    const venues = events.filter((e) => e.status !== 'cancelled').map((e) => ({ id: e.venue_entity_id, label: `${e.name} · ${e.venue_name}` }));
    const seen = new Set(venues.map((v) => v.id));
    return venues.filter((v, i) => venues.findIndex((x) => x.id === v.id) === i).concat(
      graph.nodes.filter((n) => ['venue', 'zone'].includes(n.entity_type) && !seen.has(n.entity_id)).map((n) => ({ id: n.entity_id, label: n.display_name })),
    );
  }, [events, graph.nodes]);
  const origins = useMemo(() => graph.nodes.filter((n) => ['transport_node', 'parking', 'zone'].includes(n.entity_type)), [graph.nodes]);
  const eventId = events.find((e) => e.venue_entity_id === plan.destination)?.event_id;

  async function respond(nudge, accepted) {
    setBusy(true);
    try {
      const result = await api.respondToNudge(nudge.nudge_id, accepted);
      const change = result?.plan_change || {};
      if (change.new_origin_property_id) {
        setPlan((p) => ({ ...p, origin: change.new_origin_property_id }));
        toast(`Accepted — you are now staying at ${change.new_origin_name}; journey re-planned`, 'success');
      } else {
        toast(accepted ? 'Accepted — your plan is updated' : 'Noted — keeping your plan', 'success');
      }
      nudges.reload();
      journey.reload();
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  const j = journey.data;
  const pending = (nudges.data?.nudges || []).filter((n) => n.status === 'pending');
  const answered = (nudges.data?.nudges || []).filter((n) => n.status !== 'pending').slice(0, 2);
  const select = 'w-full rounded border border-surface-700 bg-surface-850 px-1.5 py-1 text-xs';

  return (
    <div className="min-h-screen bg-surface-950 py-4 font-sans text-slate-100">
      <div className="mx-auto w-full max-w-[480px] space-y-3 px-3">
        <header className="flex items-center justify-between border-b border-surface-700/60 pb-3 transition-all">
          <div className="flex items-center gap-3">
            <MenuToggle />
            <div>
              <h1 className="text-sm font-bold tracking-tight text-slate-100">Your event journey</h1>
              <p className="font-mono text-[10px] text-slate-400">ID: {plan.attendee_id}</p>
            </div>
          </div>
          <Link to="/" className="flex items-center gap-1 rounded border border-surface-700/60 bg-surface-850 px-2 py-1 text-[11px] text-slate-300">
            Operators <ArrowRight className="h-3 w-3" />
          </Link>
        </header>

        {mockMode && (
          <div className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-800">
            <b>Recorded replay:</b> this is one journey recorded from a real run. Changing the trip or answering
            a nudge needs the live backend (<code>npm run dev:live</code>).
          </div>
        )}
        <fieldset disabled={mockMode} className="panel grid grid-cols-2 gap-2 p-3 disabled:opacity-70">
          <label className="col-span-2 flex flex-col gap-0.5">
            <span className="text-[10px] text-slate-400">Starting from</span>
            <select id="att-origin" className={select} value={plan.origin} onChange={(e) => setPlan({ ...plan, origin: e.target.value })}>
              <optgroup label="Hotels">
                {(hotels.data?.hotels || []).map((h) => (
                  <option key={h.property_id} value={h.property_id}>{h.name} ({h.zone})</option>
                ))}
                {!hotels.data && plan.origin && <option value={plan.origin}>{plan.origin}</option>}
              </optgroup>
              <optgroup label="Stations, hubs, parking, zones">
                {origins.map((n) => <option key={n.entity_id} value={n.entity_id}>{n.display_name}</option>)}
              </optgroup>
            </select>
          </label>
          <label className="col-span-2 flex flex-col gap-0.5">
            <span className="text-[10px] text-slate-400">Going to</span>
            <select id="att-dest" className={select} value={plan.destination} onChange={(e) => setPlan({ ...plan, destination: e.target.value })}>
              {destinations.map((d) => <option key={d.id} value={d.id}>{d.label}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="text-[10px] text-slate-400">About you</span>
            <select id="att-segment" className={select} value={plan.segment_id} onChange={(e) => setPlan({ ...plan, segment_id: e.target.value })}>
              {SEGMENTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="text-[10px] text-slate-400">Prefer</span>
            <select id="att-priority" className={select} value={plan.priority} onChange={(e) => setPlan({ ...plan, priority: e.target.value })}>
              {PRIORITIES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="text-[10px] text-slate-400">Travel by</span>
            <select id="att-mode" className={select} value={plan.mode} onChange={(e) => setPlan({ ...plan, mode: e.target.value })}>
              {[['any', 'Any mode'], ['metro', 'Metro'], ['bus', 'Bus / shuttle'], ['car', 'Car'], ['walk', 'Walk only']].map(([v, l]) => (
                <option key={v} value={v}>{l}</option>
              ))}
            </select>
          </label>
        </fieldset>

        {pending.map((n) => (
          <NudgeCard key={n.nudge_id} nudge={n} nodesById={nodesById} onRespond={respond} busy={busy || mockMode} />
        ))}

        {journey.error && <div className="panel p-3 text-xs text-amber-700">{journey.error.message}</div>}
        {!j && !journey.error && <div className="skeleton h-40" />}
        {j && (
          <>
            <section className="panel p-3">
              <div className="flex items-center justify-between">
                <h2 className="panel-title">Journey crowding</h2>
                {j.event && <span className="text-[10px] text-slate-400">{j.event.name} starts {clock(j.event.start_time)}</span>}
              </div>
              <div className="mt-2"><RiskScore score={j.journey_risk_score} band={j.journey_risk_band} /></div>
              <p className="mt-2 text-[11px] leading-relaxed text-slate-300">{j.advice}</p>
              {j.preference_met === false && (
                <p className="mt-1 text-[11px] text-amber-700">
                  No reasonable {j.transport_preference === 'walk' ? 'walking-only' : j.transport_preference} route right now; this is the best available.
                </p>
              )}
              {j.avoided_entity_ids?.length > 0 && (
                <p className="mt-1 text-[10px] text-sky-700">
                  Avoiding {j.avoided_entity_ids.map((id) => nodesById[id]?.display_name || id).join(', ')} as you accepted.
                </p>
              )}
            </section>

            <section className="panel space-y-2 p-3">
              <h2 className="panel-title">Routes</h2>
              <RouteCard route={j.recommended_route} nodesById={nodesById} highlight />
              {j.alternatives.map((r) => <RouteCard key={r.label + r.total_duration_sec} route={r} nodesById={nodesById} />)}
            </section>

            {j.departure_options.length > 0 && (
              <section className="panel p-3">
                <div className="flex items-center gap-1.5">
                  <Timer className="h-4 w-4 text-teal-600" />
                  <h2 className="panel-title">When to leave</h2>
                </div>
                {j.departure_advice && <p className="mt-1.5 text-[11px] text-slate-300">{j.departure_advice}</p>}
                <table className="mt-2 w-full text-left text-[11px]">
                  <thead className="text-[9px] uppercase text-slate-400">
                    <tr><th>Leave</th><th>Arrive</th><th>Trip</th><th>Peak crowding</th><th /></tr>
                  </thead>
                  <tbody>
                    {j.departure_options.map((o) => (
                      <tr key={o.offset_sec} className={`border-t border-surface-700/40 ${o.recommended ? 'font-semibold text-emerald-700' : 'text-slate-300'}`}>
                        <td className="py-1 font-mono">{clock(o.depart_at)}</td>
                        <td className="font-mono">{clock(o.arrive_at)}</td>
                        <td>{minutes(o.travel_time_sec)}</td>
                        <td>{percent(o.peak_utilisation)} ({o.crowding_band})</td>
                        <td>{o.recommended ? 'best' : o.meets_event_start === false ? <span className="text-red-600">late</span> : ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            )}

            {j.return_route && (
              <section className="panel p-3">
                <div className="mb-1.5 flex items-center gap-1.5">
                  <CornerDownLeft className="h-4 w-4 text-teal-600" />
                  <h2 className="panel-title">Getting back</h2>
                  <span className="text-[10px] text-slate-400">leave {clock(j.return_route.depart_at)}</span>
                </div>
                <RouteCard route={j.return_route} nodesById={nodesById} />
              </section>
            )}
          </>
        )}

        <HotelCard propertyId={originIsHotel ? plan.origin : null} eventId={eventId} segment={plan.segment_id} />

        {!pending.length && (
          <section className="panel p-3">
            <h2 className="panel-title">Alerts</h2>
            <p className="mt-1.5 text-xs text-slate-400">
              Nothing needs your attention. You will be notified here if an operator action affects your route or hotel.
            </p>
            {answered.map((n) => (
              <p key={n.nudge_id} className="mt-1 text-[10px] text-slate-400">
                {n.headline} — <span className="uppercase">{n.status}</span>
              </p>
            ))}
          </section>
        )}
      </div>
    </div>
  );
}
