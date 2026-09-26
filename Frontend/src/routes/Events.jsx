/**
 * Events — the schedule that drives visitor demand.
 *
 * Every control posts to `POST /events/{id}`; the backend reshapes arrivals in
 * the live simulation (and the twin's model), so a delay here moves pressure
 * on stations, roads and gates over the following cycles.
 */
import { useState } from 'react';
import { CalendarClock, Clock3, Users, XCircle, RotateCcw } from 'lucide-react';

import PageShell, { ErrorNote, Stat } from '../components/PageShell.jsx';
import { api } from '../lib/api.js';
import { clock, integer, minutes } from '../lib/format.js';
import { useLiveQuery } from '../lib/useLiveQuery.js';
import { useStore } from '../store/useStore.js';

const STATUS_STYLE = {
  scheduled: 'border-slate-400/40 text-slate-400',
  arriving: 'border-amber-500/50 text-amber-600',
  live: 'border-emerald-500/50 text-emerald-600',
  egress: 'border-orange-500/50 text-orange-600',
  ended: 'border-slate-400/40 text-slate-500',
  cancelled: 'border-red-500/50 text-red-600',
};

function Timeline({ events, simTime }) {
  if (!events.length) return null;
  const times = events.flatMap((e) => [new Date(e.start_time).getTime() - 7200e3, new Date(e.end_time).getTime() + 3600e3]);
  const t0 = Math.min(...times);
  const t1 = Math.max(...times);
  const pos = (t) => ((new Date(t).getTime() - t0) / (t1 - t0)) * 100;
  const now = simTime ? pos(simTime) : null;
  return (
    <div className="panel p-3">
      <div className="panel-title mb-2">Schedule overlap (arrival window shaded)</div>
      <div className="relative space-y-2">
        {events.map((e) => (
          <div key={e.event_id} className="relative h-6">
            <div
              className="absolute inset-y-1 rounded bg-amber-400/20"
              style={{ left: `${pos(new Date(new Date(e.start_time).getTime() - 7200e3).toISOString())}%`, width: `${pos(e.start_time) - pos(new Date(new Date(e.start_time).getTime() - 7200e3).toISOString())}%` }}
            />
            <div
              className={`absolute inset-y-0 flex items-center rounded px-2 text-[10px] font-semibold ${e.status === 'cancelled' ? 'bg-red-500/15 text-red-600 line-through' : 'bg-teal-500/20 text-teal-800'}`}
              style={{ left: `${pos(e.start_time)}%`, width: `${Math.max(pos(e.end_time) - pos(e.start_time), 4)}%` }}
              title={`${e.name}: ${clock(e.start_time)}–${clock(e.end_time)}`}
            >
              <span className="truncate">{e.name}</span>
            </div>
          </div>
        ))}
        {now !== null && now >= 0 && now <= 100 && (
          <div className="pointer-events-none absolute inset-y-0 w-px bg-red-500" style={{ left: `${now}%` }} title={`Now ${clock(simTime)}`} />
        )}
      </div>
      <div className="mt-1 flex justify-between text-[10px] font-mono text-slate-400">
        <span>{clock(new Date(t0).toISOString())}</span>
        <span>{clock(new Date(t1).toISOString())}</span>
      </div>
    </div>
  );
}

function EventRow({ event, onChange, busy }) {
  const [attendance, setAttendance] = useState(event.expected_attendance);
  const started = ['live', 'egress', 'ended'].includes(event.status);
  const cancelled = event.status === 'cancelled';
  const arrivedPct = event.expected_attendance ? Math.min(100, (event.arrived / event.expected_attendance) * 100) : 0;
  return (
    <tr className="border-b border-surface-700/40 align-top">
      <td className="py-2 pr-3">
        <div className="font-semibold text-slate-100">{event.name}</div>
        <div className="text-[10px] text-slate-400">{event.venue_name} · {event.category}</div>
      </td>
      <td className="py-2 pr-3">
        <span className={`chip border text-[10px] uppercase ${STATUS_STYLE[event.status] || ''}`}>{event.status}</span>
      </td>
      <td className="py-2 pr-3 font-mono text-xs tabular-nums text-slate-200">
        {clock(event.start_time)}–{clock(event.end_time)}
        {event.delay_sec !== 0 && (
          <div className={`text-[10px] ${event.delay_sec > 0 ? 'text-amber-600' : 'text-teal-700'}`}>
            {event.delay_sec > 0 ? '+' : '−'}{minutes(Math.abs(event.delay_sec))} vs plan ({clock(event.original_start_time)})
          </div>
        )}
      </td>
      <td className="py-2 pr-3 text-xs tabular-nums">
        <div className="text-slate-200">{integer(event.arrived)} / {integer(event.expected_attendance)} arrived</div>
        <div className="mt-1 h-1.5 w-32 rounded bg-surface-700/40">
          <div className="h-1.5 rounded bg-teal-500" style={{ width: `${arrivedPct}%` }} />
        </div>
        <div className="text-[10px] text-slate-400">{integer(event.inside)} inside the venue</div>
      </td>
      <td className="py-2">
        <div className="flex flex-wrap gap-1">
          <button type="button" className="btn-secondary" disabled={busy || started || cancelled} onClick={() => onChange({ delay_sec: 900 })}>
            <Clock3 className="h-3 w-3" /> Delay 15 min
          </button>
          <button type="button" className="btn-secondary" disabled={busy || started || cancelled} onClick={() => onChange({ delay_sec: 1800 })}>
            Delay 30 min
          </button>
          <button type="button" className="btn-secondary" disabled={busy || started || cancelled} onClick={() => onChange({ delay_sec: -900 })}>
            Bring forward 15
          </button>
          {cancelled ? (
            <button type="button" className="btn-secondary" disabled={busy} onClick={() => onChange({ status: 'scheduled' })}>
              <RotateCcw className="h-3 w-3" /> Restore
            </button>
          ) : (
            <button type="button" className="btn-danger" disabled={busy} onClick={() => onChange({ status: 'cancelled' })}>
              <XCircle className="h-3 w-3" /> Cancel
            </button>
          )}
        </div>
        <form
          className="mt-1.5 flex items-center gap-1"
          onSubmit={(e) => {
            e.preventDefault();
            onChange({ expected_attendance: Number(attendance) });
          }}
        >
          <Users className="h-3 w-3 text-slate-400" />
          <input
            id={`att-${event.event_id}`}
            type="number"
            min={0}
            step={500}
            value={attendance}
            onChange={(e) => setAttendance(e.target.value)}
            className="w-24 rounded border border-surface-700 bg-surface-850 px-1.5 py-0.5 text-xs tabular-nums"
            aria-label="Expected attendance"
          />
          <button type="submit" className="btn-secondary" disabled={busy || cancelled || Number(attendance) === event.expected_attendance}>
            Update attendance
          </button>
        </form>
      </td>
    </tr>
  );
}

export default function Events() {
  const { simTime, toast, upsertEvent } = useStore();
  const { data, error, reload } = useLiveQuery(() => api.events(), [], { everyCycles: 2 });
  const [busyId, setBusyId] = useState(null);
  const events = data?.events || [];

  async function change(event, body) {
    setBusyId(event.event_id);
    try {
      const updated = await api.updateEvent(event.event_id, body);
      upsertEvent(updated);
      toast(`${updated.name}: schedule updated — demand model re-planned`, 'success');
      reload();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusyId(null);
    }
  }

  const live = events.filter((e) => e.status === 'live').length;
  const arriving = events.filter((e) => e.status === 'arriving').length;
  const total = events.filter((e) => e.status !== 'cancelled').reduce((a, e) => a + e.expected_attendance, 0);

  return (
    <PageShell
      title="Event schedule"
      subtitle="Every event is a demand source. Delays, cancellations and attendance changes re-plan arrivals across hotels, transport, roads and gates in the live simulation."
    >
      <ErrorNote error={error} />
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        <Stat label="Events today" value={events.length} />
        <Stat label="Live now" value={live} tone={live ? 'text-emerald-600' : 'text-slate-100'} />
        <Stat label="Arrivals under way" value={arriving} tone={arriving ? 'text-amber-600' : 'text-slate-100'} />
        <Stat label="Expected visitors" value={integer(total)} />
      </div>
      <Timeline events={events} simTime={simTime} />
      <section className="panel mt-3 overflow-x-auto p-3">
        <div className="mb-2 flex items-center gap-2">
          <CalendarClock className="h-4 w-4 text-teal-600" />
          <h2 className="panel-title">Events</h2>
        </div>
        {!data && !error && <div className="skeleton h-24" />}
        <table className="w-full min-w-[760px] text-left text-sm">
          <thead className="text-[10px] uppercase tracking-wider text-slate-400">
            <tr>
              <th className="pb-1 font-semibold">Event</th>
              <th className="pb-1 font-semibold">Status</th>
              <th className="pb-1 font-semibold">Time (UTC)</th>
              <th className="pb-1 font-semibold">Visitors</th>
              <th className="pb-1 font-semibold">Operator actions</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <EventRow key={`${e.event_id}-${e.expected_attendance}`} event={e} busy={busyId === e.event_id} onChange={(body) => change(e, body)} />
            ))}
          </tbody>
        </table>
      </section>
    </PageShell>
  );
}
