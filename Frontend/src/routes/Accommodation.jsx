/**
 * Accommodation — live hotel availability, saturation and recommendations.
 *
 * Occupancy is simulation state: bookings arrive through the day, a hotel
 * shortage takes rooms offline, and an approved `accommodation_rebalance`
 * moves guests between clusters. Recommendations are scored server-side on
 * availability, price, travel time, transport access and congestion; this page
 * renders the scores and reasons it is given and never re-ranks them.
 */
import { useState } from 'react';
import { BedDouble, AlertTriangle, Search, Accessibility, ArrowRight } from 'lucide-react';

import PageShell, { ErrorNote, Stat } from '../components/PageShell.jsx';
import { api } from '../lib/api.js';
import { integer, minutes, percent, rupees } from '../lib/format.js';
import { capacitySourceLabel, confidenceLabel, roomCapacityLabel, statusLabel } from '../lib/accommodation.js';
import { useLiveQuery } from '../lib/useLiveQuery.js';
import { useStore } from '../store/useStore.js';

const STATUS = {
  available: 'border-emerald-500/40 text-emerald-700 bg-emerald-500/10',
  limited: 'border-amber-500/40 text-amber-700 bg-amber-500/10',
  saturated: 'border-red-500/40 text-red-700 bg-red-500/10',
};
const SEGMENTS = ['price_sensitive', 'time_sensitive', 'accessibility_constrained', 'group', 'premium'];

function StatusChip({ status }) {
  return <span className={`chip border text-[10px] uppercase ${STATUS[status] || ''}`}>{statusLabel(status)}</span>;
}

/** Capacity with its provenance: mapped values plain, derived / estimated values marked "~". */
function Capacity({ h }) {
  return (
    <div>
      <div className="tabular-nums text-slate-100">{roomCapacityLabel(h.rooms_total, h.rooms_confidence)}</div>
      <div className="text-[10px] text-slate-400" title={h.capacity_tags ? JSON.stringify(h.capacity_tags) : 'no capacity tag mapped'}>
        {capacitySourceLabel(h.rooms_source)} · {confidenceLabel(h.rooms_confidence)} confidence
        {h.bed_capacity ? ` · ${integer(h.bed_capacity)} beds` : ''}
      </div>
    </div>
  );
}

/** Each event's accommodation demand: only its lodging share needs rooms (simulated). */
function LodgingTable({ rows }) {
  if (!rows?.length) return null;
  return (
    <section className="panel mb-3 overflow-x-auto p-3">
      <h2 className="panel-title mb-2">Accommodation demand by event (simulated)</h2>
      <table className="w-full min-w-[720px] text-left text-xs">
        <thead className="text-[10px] uppercase tracking-wider text-slate-400">
          <tr>
            <th className="pb-1">Event</th><th className="pb-1">Attendance</th><th className="pb-1">Lodging share</th>
            <th className="pb-1">Local</th><th className="pb-1">Need a room</th><th className="pb-1">Placed</th>
            <th className="pb-1">Unmet</th><th className="pb-1">Not yet checked in</th><th className="pb-1">In hotels now</th>
            <th className="pb-1">Checked out</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.event_id} className="border-t border-surface-700/40 tabular-nums">
              <td className="py-1.5 pr-2 font-semibold text-slate-100">{r.name}{r.cancelled ? ' (cancelled)' : ''}</td>
              <td className="pr-2">{integer(r.attendance)}</td>
              <td className="pr-2">{percent(r.lodging_share)} <span className="text-[10px] text-slate-400">({r.lodging_share_source})</span></td>
              <td className="pr-2">{integer(r.local_guests)}</td>
              <td className="pr-2">{integer(r.lodging_guests)}</td>
              <td className="pr-2">{integer(r.allocated_guests)}</td>
              <td className={`pr-2 ${r.unmet_guests >= 1 ? 'font-semibold text-red-600' : ''}`}>{integer(r.unmet_guests)}</td>
              <td className="pr-2">{integer(r.pending_guests)}</td>
              <td className="pr-2">{integer(r.in_house_guests)}</td>
              <td className="pr-2">{integer(r.checked_out_guests)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function OccupancyBar({ occupancy, status }) {
  const colour = status === 'saturated' ? '#DC2626' : status === 'limited' ? '#D97706' : '#0D9488';
  return (
    <div className="h-1.5 w-24 rounded bg-surface-700/40">
      <div className="h-1.5 rounded" style={{ width: `${Math.min(100, occupancy * 100)}%`, backgroundColor: colour }} />
    </div>
  );
}

function FactorBars({ factors }) {
  return (
    <div className="grid grid-cols-5 gap-1">
      {Object.entries(factors).map(([k, v]) => (
        <div key={k} title={`${k}: ${Math.round(v * 100)}/100`}>
          <div className="h-1 rounded bg-surface-700/40">
            <div className="h-1 rounded bg-teal-500" style={{ width: `${v * 100}%` }} />
          </div>
          <div className="mt-0.5 text-[9px] uppercase text-slate-400">{k}</div>
        </div>
      ))}
    </div>
  );
}

function StayOption({ option, rank }) {
  const p = option.property;
  return (
    <div className="rounded border border-surface-700/60 bg-surface-850 p-2.5">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-sm font-semibold text-slate-100">
            {rank}. {p.name} <span className="text-[10px] font-normal text-slate-400">· {p.zone}{p.tier ? ` · ${p.tier}` : ''}</span>
          </div>
          <div className="text-[11px] text-slate-400">
            {p.price_per_night_paise != null ? `${rupees(p.price_per_night_paise)}/night · ` : 'price unknown · '}
            {p.rooms_available} of {roomCapacityLabel(p.rooms_in_service, p.rooms_confidence)} free · {percent(p.occupancy)} occupied
            {' · '}<StatusChip status={p.status} />
            {p.accessible && <Accessibility className="ml-1 inline h-3 w-3 text-sky-600" />}
          </div>
        </div>
        <div className="text-right">
          <div className="text-[9px] uppercase text-slate-400">Score</div>
          <div className="font-mono text-sm font-bold text-teal-700">{option.score.toFixed(2)}</div>
        </div>
      </div>
      <div className="mt-2"><FactorBars factors={option.factors} /></div>
      <ul className="mt-2 list-disc space-y-0.5 pl-4 text-[11px] text-slate-300">
        {option.reasons.map((r) => <li key={r}>{r}</li>)}
      </ul>
    </div>
  );
}

function StayFinder({ events }) {
  const { toast, event } = useStore();
  const [form, setForm] = useState({ event_id: event?.event_id || '', segment_id: 'price_sensitive', max_price_paise: '', accessible_only: false, rooms: 1 });
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    try {
      const body = {
        event_id: form.event_id || null,
        segment_id: form.segment_id || null,
        max_price_paise: form.max_price_paise ? Number(form.max_price_paise) * 100 : null,
        accessible_only: form.accessible_only,
        rooms: Number(form.rooms) || 1,
        limit: 4,
      };
      setResult(await api.recommendStay(body));
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel p-3">
      <div className="mb-2 flex items-center gap-2">
        <Search className="h-4 w-4 text-teal-600" />
        <h2 className="panel-title">Find a stay</h2>
      </div>
      <form className="grid grid-cols-2 gap-2 text-xs md:grid-cols-6" onSubmit={submit}>
        <label className="col-span-2 flex flex-col gap-0.5">
          <span className="text-[10px] text-slate-400">Attending</span>
          <select id="stay-event" className="rounded border border-surface-700 bg-surface-850 px-1.5 py-1" value={form.event_id} onChange={(e) => setForm({ ...form, event_id: e.target.value })}>
            {events.map((ev) => <option key={ev.event_id} value={ev.event_id}>{ev.name} · {ev.venue_name}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-slate-400">Traveller</span>
          <select id="stay-segment" className="rounded border border-surface-700 bg-surface-850 px-1.5 py-1" value={form.segment_id} onChange={(e) => setForm({ ...form, segment_id: e.target.value })}>
            {SEGMENTS.map((s) => <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-slate-400">Budget (₹/night)</span>
          <input id="stay-budget" type="number" min={0} step={500} placeholder="any" className="rounded border border-surface-700 bg-surface-850 px-1.5 py-1" value={form.max_price_paise} onChange={(e) => setForm({ ...form, max_price_paise: e.target.value })} />
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-slate-400">Rooms</span>
          <input id="stay-rooms" type="number" min={1} max={50} className="rounded border border-surface-700 bg-surface-850 px-1.5 py-1" value={form.rooms} onChange={(e) => setForm({ ...form, rooms: e.target.value })} />
        </label>
        <div className="flex items-end gap-2">
          <label className="flex items-center gap-1 text-[11px] text-slate-300">
            <input id="stay-accessible" type="checkbox" checked={form.accessible_only} onChange={(e) => setForm({ ...form, accessible_only: e.target.checked })} />
            Step-free
          </label>
          <button type="submit" className="btn-primary" disabled={busy}>{busy ? 'Scoring…' : 'Recommend'}</button>
        </div>
      </form>
      {result && (
        <div className="mt-3 space-y-2">
          <p className={`text-xs ${result.options.length ? 'text-slate-300' : 'font-semibold text-red-600'}`}>{result.explanation}</p>
          <div className="grid gap-2 md:grid-cols-2">
            {result.options.map((o, i) => <StayOption key={o.property.property_id} option={o} rank={i + 1} />)}
          </div>
        </div>
      )}
    </section>
  );
}

export default function Accommodation() {
  const events = useStore((s) => s.events);
  const [filters, setFilters] = useState({ zone: '', tier: '', status: '', accessible_only: false, sort: 'occupancy' });
  const { data, error } = useLiveQuery(
    () => api.hotels({ ...filters, accessible_only: filters.accessible_only || null }),
    [JSON.stringify(filters)],
    { everyCycles: 4 },
  );
  const sat = useLiveQuery(() => api.saturation(), [], { everyCycles: 6 });
  const summary = data?.summary;
  // Zones come from the hotels of the active world (demo or generated), never a fixed list.
  const zones = [...new Set([...(data?.hotels || []).map((h) => h.zone), filters.zone].filter(Boolean))].sort();

  return (
    <PageShell
      title="Accommodation"
      subtitle="Hotels are a constrained resource: only the lodging share of each event's attendees needs a room (the rest travel from home). Guests check in before the event and check out after it; rooms free up again at check-out. Capacity is mapped from OpenStreetMap where tagged, otherwise estimated — each value says which. Occupancy, bookings and unmet demand are simulated."
    >
      <ErrorNote error={error} />
      {summary && (
        <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-6">
          <Stat label="Total hotels" value={integer(summary.properties)} sub={`${summary.saturated} saturated · ${summary.limited} tight`} />
          <Stat label="Total rooms" value={integer(summary.rooms_in_service)} sub={summary.effective_guest_capacity != null ? `${integer(summary.effective_guest_capacity)} guests` : null} />
          <Stat label="Occupied" value={integer(summary.rooms_occupied)} sub={`${percent(summary.occupancy)} of rooms`} />
          <Stat label="Free" value={integer(summary.rooms_available)} tone={summary.rooms_available ? 'text-slate-100' : 'text-red-600'} sub="bookable rooms" />
          <Stat label="Lodging demand" value={summary.lodging_guests != null ? integer(summary.lodging_guests) : '—'}
            sub={summary.attendance_total != null ? `guests of ${integer(summary.attendance_total)} attendees · ${integer(summary.local_guests)} local` : null} />
          <Stat label="Unmet demand" value={summary.lodging_unmet_guests != null ? integer(summary.lodging_unmet_guests) : integer(summary.unmet_room_requests)}
            tone={(summary.lodging_unmet_guests || summary.unmet_room_requests) ? 'text-red-600' : 'text-slate-100'}
            sub={summary.lodging_unmet_guests != null ? 'guests with no room' : 'room requests with no room'} />
        </div>
      )}
      {summary?.shortage_message && (
        <div role="alert" className="mb-3 rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs font-semibold text-red-700">
          <AlertTriangle className="mr-1 inline h-3.5 w-3.5" /> {summary.shortage_message}
        </div>
      )}
      <LodgingTable rows={data?.lodging_by_event} />

      <div className="grid gap-3 lg:grid-cols-[1fr_380px]">
        <section className="panel overflow-x-auto p-3">
          <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
            <BedDouble className="h-4 w-4 text-teal-600" />
            <h2 className="panel-title mr-2">Hotels</h2>
            <select id="f-zone" aria-label="Zone" className="rounded border border-surface-700 bg-surface-850 px-1.5 py-0.5" value={filters.zone} onChange={(e) => setFilters({ ...filters, zone: e.target.value })}>
              <option value="">All zones</option>
              {zones.map((z) => <option key={z} value={z}>{z}</option>)}
            </select>
            <select id="f-tier" aria-label="Tier" className="rounded border border-surface-700 bg-surface-850 px-1.5 py-0.5" value={filters.tier} onChange={(e) => setFilters({ ...filters, tier: e.target.value })}>
              <option value="">All tiers</option>
              {['budget', 'midscale', 'upscale', 'luxury'].map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <select id="f-status" aria-label="Status" className="rounded border border-surface-700 bg-surface-850 px-1.5 py-0.5" value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })}>
              <option value="">Any status</option>
              {['available', 'limited', 'saturated'].map((t) => <option key={t} value={t}>{statusLabel(t).toLowerCase()}</option>)}
            </select>
            <select id="f-sort" aria-label="Sort" className="rounded border border-surface-700 bg-surface-850 px-1.5 py-0.5" value={filters.sort} onChange={(e) => setFilters({ ...filters, sort: e.target.value })}>
              <option value="occupancy">Busiest first</option>
              <option value="availability">Most rooms free</option>
              <option value="price">Cheapest</option>
              <option value="travel_time">Closest to venue</option>
            </select>
            <label className="flex items-center gap-1">
              <input id="f-access" type="checkbox" checked={filters.accessible_only} onChange={(e) => setFilters({ ...filters, accessible_only: e.target.checked })} />
              Step-free only
            </label>
          </div>
          {!data && !error && <div className="skeleton h-40" />}
          <table className="w-full min-w-[720px] text-left text-xs">
            <thead className="text-[10px] uppercase tracking-wider text-slate-400">
              <tr>
                <th className="pb-1">Hotel</th><th className="pb-1">Capacity</th><th className="pb-1">Status</th><th className="pb-1">Occupancy</th>
                <th className="pb-1">Occupied / free</th><th className="pb-1">Price</th><th className="pb-1">To venue</th><th className="pb-1">Transport load</th>
              </tr>
            </thead>
            <tbody>
              {(data?.hotels || []).map((h) => (
                <tr key={h.property_id} className={`border-t border-surface-700/40 ${h.status === 'saturated' ? 'bg-red-500/[0.05]' : ''}`}>
                  <td className="py-1.5 pr-2">
                    <div className="font-semibold text-slate-100">{h.name}</div>
                    <div className="text-[10px] text-slate-400">{h.zone}{h.tier ? ` · ${h.tier}` : ''}{h.accessible ? ' · step-free' : ''}</div>
                  </td>
                  <td className="pr-2"><Capacity h={h} /></td>
                  <td className="pr-2">
                    <StatusChip status={h.status} />
                    {h.status === 'saturated' && <div className="mt-0.5 text-[10px] font-semibold text-red-600">0 rooms available</div>}
                  </td>
                  <td className="pr-2">
                    <div className="tabular-nums text-slate-200">{percent(h.occupancy)}</div>
                    <OccupancyBar occupancy={h.occupancy} status={h.status} />
                  </td>
                  <td className="pr-2 tabular-nums">
                    {integer(h.rooms_occupied)} occupied · <b>{integer(h.rooms_available)}</b> free
                    {h.event_guests != null && <div className="text-[10px] text-slate-400">{integer(h.event_guests)} event guests</div>}
                  </td>
                  <td className="pr-2 tabular-nums">{h.price_per_night_paise != null ? rupees(h.price_per_night_paise) : 'unknown'}</td>
                  <td className="pr-2 tabular-nums">{minutes(h.travel_time_to_venue_sec)}</td>
                  <td className="pr-2 tabular-nums">
                    {h.transport_name}: {percent(h.transport_utilisation)}
                    {h.transport_forecast_utilisation != null && (
                      <span className="text-[10px] text-slate-400"> → {percent(h.transport_forecast_utilisation)} in 30 min</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="panel p-3">
          <div className="mb-2 flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-red-500" />
            <h2 className="panel-title">Saturated hotels & alternatives</h2>
          </div>
          {sat.data && sat.data.saturated.length === 0 && (
            <p className="text-xs text-slate-400">No hotel is saturated right now.</p>
          )}
          <div className="space-y-2">
            {(sat.data?.saturated || []).slice(0, 4).map((s) => (
              <div key={s.property.property_id} className="rounded border border-red-500/30 bg-red-500/[0.04] p-2">
                <div className="text-xs font-semibold text-slate-100">
                  {s.property.name} · {percent(s.property.occupancy)}
                </div>
                <p className="mt-1 text-[11px] text-slate-300">{s.explanation}</p>
                <ul className="mt-1 space-y-0.5 text-[11px]">
                  {s.alternatives.map((a) => (
                    <li key={a.property.property_id} className="flex items-center gap-1 text-slate-300">
                      <ArrowRight className="h-3 w-3 text-teal-600" />
                      {a.property.name} — {a.property.rooms_available} free, {a.property.price_per_night_paise != null ? rupees(a.property.price_per_night_paise) : 'price unknown'}, {minutes(a.travel_time_sec)}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </section>
      </div>

      <div className="mt-3">
        <StayFinder events={events} />
      </div>
    </PageShell>
  );
}
