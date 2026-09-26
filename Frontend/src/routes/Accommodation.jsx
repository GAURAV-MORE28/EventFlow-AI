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
import { useLiveQuery } from '../lib/useLiveQuery.js';
import { useStore } from '../store/useStore.js';

const STATUS = {
  available: 'border-emerald-500/40 text-emerald-700 bg-emerald-500/10',
  limited: 'border-amber-500/40 text-amber-700 bg-amber-500/10',
  saturated: 'border-red-500/40 text-red-700 bg-red-500/10',
};
const SEGMENTS = ['price_sensitive', 'time_sensitive', 'accessibility_constrained', 'group', 'premium'];

function StatusChip({ status }) {
  return <span className={`chip border text-[10px] uppercase ${STATUS[status] || ''}`}>{status}</span>;
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
            {rank}. {p.name} <span className="text-[10px] font-normal text-slate-400">· {p.zone} · {p.tier}</span>
          </div>
          <div className="text-[11px] text-slate-400">
            {rupees(p.price_per_night_paise)}/night · {p.rooms_available} rooms free · {percent(p.occupancy)} occupied
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
          <p className="text-xs text-slate-300">{result.explanation}</p>
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
  const zones = ['North', 'Core', 'East', 'South', 'West', 'Airport'];

  return (
    <PageShell
      title="Accommodation"
      subtitle="Live room availability across the city's hotels, driven by event bookings in the simulation. Saturated properties are paired with ranked, explained alternatives."
    >
      <ErrorNote error={error} />
      {summary && (
        <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-6">
          <Stat label="Occupancy" value={percent(summary.occupancy)} />
          <Stat label="Rooms free" value={integer(summary.rooms_available)} sub={`of ${integer(summary.rooms_in_service)} in service`} />
          <Stat label="Saturated" value={summary.saturated} tone={summary.saturated ? 'text-red-600' : 'text-slate-100'} sub={`of ${summary.properties} hotels`} />
          <Stat label="Limited" value={summary.limited} tone={summary.limited ? 'text-amber-600' : 'text-slate-100'} />
          <Stat label="Unplaced bookings" value={integer(summary.unmet_room_requests)} tone={summary.unmet_room_requests ? 'text-red-600' : 'text-slate-100'} sub="requests with no room" />
          <Stat label="Hotels listed" value={data.hotels.length} />
        </div>
      )}

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
              {['available', 'limited', 'saturated'].map((t) => <option key={t} value={t}>{t}</option>)}
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
                <th className="pb-1">Hotel</th><th className="pb-1">Status</th><th className="pb-1">Occupancy</th>
                <th className="pb-1">Free</th><th className="pb-1">Price</th><th className="pb-1">To venue</th><th className="pb-1">Transport load</th>
              </tr>
            </thead>
            <tbody>
              {(data?.hotels || []).map((h) => (
                <tr key={h.property_id} className="border-t border-surface-700/40">
                  <td className="py-1.5 pr-2">
                    <div className="font-semibold text-slate-100">{h.name}</div>
                    <div className="text-[10px] text-slate-400">{h.zone} · {h.tier}{h.accessible ? ' · step-free' : ''}</div>
                  </td>
                  <td className="pr-2"><StatusChip status={h.status} /></td>
                  <td className="pr-2">
                    <div className="tabular-nums text-slate-200">{percent(h.occupancy)}</div>
                    <OccupancyBar occupancy={h.occupancy} status={h.status} />
                  </td>
                  <td className="pr-2 tabular-nums">{h.rooms_available} / {h.rooms_in_service}</td>
                  <td className="pr-2 tabular-nums">{rupees(h.price_per_night_paise)}</td>
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
                      {a.property.name} — {a.property.rooms_available} free, {rupees(a.property.price_per_night_paise)}, {minutes(a.travel_time_sec)}
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
