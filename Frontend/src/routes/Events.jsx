/**
 * Events — the schedule that drives visitor demand.
 *
 * Create, edit, delay, cancel and delete events. Every change goes to the
 * backend (`POST /events`, `POST /events/{id}`, `DELETE /events/{id}`), which
 * changes the simulation schedule and immediately re-states the live city
 * (demand, flows, risk, cascades) and broadcasts it — the map updates from
 * that authoritative state, not from anything computed here.
 *
 * All times are exact datetimes on the simulation clock (UTC).
 */
import { useEffect, useState } from 'react';
import { CalendarClock, Clock3, Pencil, Plus, RotateCcw, Trash2, XCircle } from 'lucide-react';

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
const CATEGORIES = ['sport', 'concert', 'festival', 'conference', 'exhibition', 'event'];
const WINDOW_KEYS = ['arrival_window_start', 'arrival_window_end', 'departure_window_start', 'departure_window_end'];

// "2026-09-04T16:00:00Z" <-> date "2026-09-04" + time "16:00" (UTC, the simulation clock).
const datePart = (iso) => (iso ? iso.slice(0, 10) : '');
const timePart = (iso) => (iso ? iso.slice(11, 16) : '');
const localPart = (iso) => (iso ? iso.slice(0, 16) : '');
const toIso = (date, time) => (date && time ? `${date}T${time}:00Z` : null);
const localToIso = (v) => (v ? `${v}:00Z` : null);

function Timeline({ events, simTime }) {
  if (!events.length) return null;
  const ms = (t) => new Date(t).getTime();
  const times = events.flatMap((e) => [ms(e.arrival_window_start || e.start_time), ms(e.departure_window_end || e.end_time)]);
  if (simTime) times.push(ms(simTime));
  const t0 = Math.min(...times);
  const t1 = Math.max(...times);
  const pos = (t) => ((ms(t) - t0) / Math.max(t1 - t0, 1)) * 100;
  const now = simTime ? pos(simTime) : null;
  return (
    <div className="panel p-3">
      <div className="panel-title mb-2">Schedule (event bar; shaded = arrival and departure windows the simulation uses)</div>
      <div className="relative space-y-2">
        {events.map((e) => (
          <div key={e.event_id} className="relative h-6">
            {e.arrival_window_start && (
              <div
                className="absolute inset-y-1 rounded bg-amber-400/25"
                style={{ left: `${pos(e.arrival_window_start)}%`, width: `${Math.max(pos(e.arrival_window_end) - pos(e.arrival_window_start), 0.5)}%` }}
                title={`Arrivals ${clock(e.arrival_window_start)}–${clock(e.arrival_window_end)}`}
              />
            )}
            {e.departure_window_start && (
              <div
                className="absolute inset-y-1 rounded bg-orange-400/25"
                style={{ left: `${pos(e.departure_window_start)}%`, width: `${Math.max(pos(e.departure_window_end) - pos(e.departure_window_start), 0.5)}%` }}
                title={`Departures ${clock(e.departure_window_start)}–${clock(e.departure_window_end)}`}
              />
            )}
            <div
              className={`absolute inset-y-0 flex items-center rounded px-2 text-[10px] font-semibold ${e.status === 'cancelled' ? 'bg-red-500/15 text-red-600 line-through' : 'bg-teal-500/25 text-teal-800'}`}
              style={{ left: `${pos(e.start_time)}%`, width: `${Math.max(pos(e.end_time) - pos(e.start_time), 3)}%` }}
              title={`${e.name}: ${clock(e.start_time)}–${clock(e.end_time)}`}
            >
              <span className="truncate">{e.name}</span>
            </div>
          </div>
        ))}
        {now !== null && (
          <div className="pointer-events-none absolute inset-y-0 w-px bg-red-500" style={{ left: `${now}%` }} title={`Now ${clock(simTime)}`} />
        )}
      </div>
      <div className="mt-1 flex justify-between text-[10px] font-mono text-slate-400">
        <span>{new Date(t0).toISOString().slice(0, 16).replace('T', ' ')} UTC</span>
        <span>{new Date(t1).toISOString().slice(0, 16).replace('T', ' ')} UTC</span>
      </div>
    </div>
  );
}

function Modal({ title, children, onClose }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" role="dialog" aria-modal="true" aria-label={title}>
      <div className="panel max-h-[90vh] w-full max-w-xl overflow-y-auto p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-bold text-slate-100">{title}</h2>
          <button type="button" className="text-slate-400 hover:text-slate-100" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

function Confirm({ title, body, confirmLabel, keepLabel, onConfirm, onClose, busy }) {
  return (
    <Modal title={title} onClose={onClose}>
      <p className="mb-4 text-xs text-slate-300">{body}</p>
      <div className="flex justify-end gap-2">
        <button type="button" className="btn-secondary" onClick={onClose} disabled={busy}>{keepLabel}</button>
        <button type="button" className="btn-danger" onClick={onConfirm} disabled={busy}>{confirmLabel}</button>
      </div>
    </Modal>
  );
}

function emptyForm(simTime) {
  const d = datePart(simTime) || '2026-09-04';
  return {
    name: '', venue_entity_id: '', category: 'event', description: '',
    start_date: d, start_time: '18:30', end_date: d, end_time: '22:30',
    expected_attendance: 10000, status: 'scheduled', custom: false,
    arrival_window_start: `${d}T17:00`, arrival_window_end: `${d}T19:00`,
    departure_window_start: `${d}T22:00`, departure_window_end: `${d}T23:30`,
  };
}

function formFromEvent(e) {
  return {
    name: e.name, venue_entity_id: e.venue_entity_id, category: e.category, description: e.description || '',
    start_date: datePart(e.start_time), start_time: timePart(e.start_time),
    end_date: datePart(e.end_time), end_time: timePart(e.end_time),
    expected_attendance: e.expected_attendance, status: e.status === 'cancelled' ? 'cancelled' : 'scheduled',
    custom: e.custom_windows,
    ...Object.fromEntries(WINDOW_KEYS.map((k) => [k, localPart(e[k])])),
  };
}

function EventForm({ initial, event, venues, onSubmit, onClose, busy }) {
  const [f, setF] = useState(initial);
  const set = (k) => (ev) => setF({ ...f, [k]: ev.target.type === 'checkbox' ? ev.target.checked : ev.target.value });
  const started = event && ['live', 'egress', 'ended'].includes(event.status);
  const venueLocked = event && event.arrived > 0;
  const input = 'w-full rounded border border-surface-700 bg-surface-850 px-2 py-1 text-xs';
  const label = 'flex flex-col gap-0.5 text-[10px] text-slate-400';

  function submit(e) {
    e.preventDefault();
    onSubmit(f);
  }

  return (
    <form onSubmit={submit} className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <label className={`${label} col-span-2`}>Event name *
          <input className={input} value={f.name} onChange={set('name')} required maxLength={120} />
        </label>
        <label className={label}>Venue *
          <select className={input} value={f.venue_entity_id} onChange={set('venue_entity_id')} required disabled={venueLocked}>
            <option value="">Choose venue…</option>
            {venues.map((v) => (
              <option key={v.entity_id} value={v.entity_id}>{v.display_name} (cap. {integer(v.nominal_capacity)})</option>
            ))}
          </select>
          {venueLocked && <span className="text-[9px]">Visitors are already on their way; the venue is fixed.</span>}
        </label>
        <label className={label}>Type
          <select className={input} value={f.category} onChange={set('category')}>
            {[...new Set([...CATEGORIES, f.category])].map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label className={label}>Start date (UTC) *
          <input type="date" className={input} value={f.start_date} onChange={set('start_date')} required disabled={started} />
        </label>
        <label className={label}>Start time (UTC) *
          <input type="time" className={input} value={f.start_time} onChange={set('start_time')} required disabled={started} />
        </label>
        <label className={label}>End date (UTC) *
          <input type="date" className={input} value={f.end_date} onChange={set('end_date')} required />
        </label>
        <label className={label}>End time (UTC) *
          <input type="time" className={input} value={f.end_time} onChange={set('end_time')} required />
        </label>
        <label className={label}>Expected visitors *
          <input type="number" min={0} max={500000} step={100} className={input} value={f.expected_attendance} onChange={set('expected_attendance')} required />
        </label>
        <label className={label}>Status
          <select className={input} value={f.status} onChange={set('status')}>
            <option value="scheduled">scheduled</option>
            <option value="cancelled">cancelled</option>
          </select>
        </label>
        <label className={`${label} col-span-2`}>Description
          <input className={input} value={f.description} onChange={set('description')} maxLength={500} />
        </label>
      </div>
      {started && <p className="text-[10px] text-amber-700">The event has started: its start is fixed; end, visitors and status can change.</p>}
      <label className="flex items-center gap-2 text-xs text-slate-300">
        <input type="checkbox" checked={f.custom} onChange={set('custom')} />
        Set exact arrival and departure windows (otherwise the standard curve around start and end is used)
      </label>
      {f.custom && (
        <div className="grid grid-cols-2 gap-2">
          <label className={label}>Arrivals from
            <input type="datetime-local" className={input} value={f.arrival_window_start} onChange={set('arrival_window_start')} required />
          </label>
          <label className={label}>Arrivals until
            <input type="datetime-local" className={input} value={f.arrival_window_end} onChange={set('arrival_window_end')} required />
          </label>
          <label className={label}>Departures from
            <input type="datetime-local" className={input} value={f.departure_window_start} onChange={set('departure_window_start')} required />
          </label>
          <label className={label}>Departures until
            <input type="datetime-local" className={input} value={f.departure_window_end} onChange={set('departure_window_end')} required />
          </label>
        </div>
      )}
      <div className="flex justify-end gap-2 border-t border-surface-700/60 pt-3">
        <button type="button" className="btn-secondary" onClick={onClose} disabled={busy}>Close</button>
        <button type="submit" className="btn-primary" disabled={busy}>{event ? 'Save changes' : 'Create event'}</button>
      </div>
    </form>
  );
}

/** The request body for create, or only the changed fields for an edit. */
function payload(f, event) {
  const body = {
    name: f.name.trim(), venue_entity_id: f.venue_entity_id, category: f.category, description: f.description || null,
    start_time: toIso(f.start_date, f.start_time), end_time: toIso(f.end_date, f.end_time),
    expected_attendance: Number(f.expected_attendance), status: f.status,
  };
  for (const k of WINDOW_KEYS) body[k] = f.custom ? localToIso(f[k]) : null;
  if (!event) return body;
  const out = {};
  for (const [k, v] of Object.entries(body)) {
    if (WINDOW_KEYS.includes(k)) continue;
    if (k === 'start_time' && ['live', 'egress', 'ended'].includes(event.status)) continue;
    const cur = k === 'status' ? (event.status === 'cancelled' ? 'cancelled' : 'scheduled') : event[k] ?? null;
    if (v !== cur) out[k] = v;
  }
  if (f.custom) {
    for (const k of WINDOW_KEYS) if (body[k] !== event[k] || !event.custom_windows) out[k] = body[k];
  } else if (event.custom_windows) {
    for (const k of WINDOW_KEYS) out[k] = '';   // back to the standard curve
  }
  // A restore must come first on its own (a cancelled event cannot be edited).
  return out;
}

function EventRow({ event, busy, replay, onEdit, onChange, onCancel, onDelete }) {
  const disabled = busy || replay;
  const started = ['live', 'egress', 'ended'].includes(event.status);
  const cancelled = event.status === 'cancelled';
  const arrivedPct = event.expected_attendance ? Math.min(100, (event.arrived / event.expected_attendance) * 100) : 0;
  return (
    <tr className="border-b border-surface-700/40 align-top">
      <td className="py-2 pr-3">
        <div className="font-semibold text-slate-100">{event.name}</div>
        <div className="text-[10px] text-slate-400">{event.venue_name} · {event.category}</div>
        {event.description && <div className="max-w-[220px] truncate text-[10px] text-slate-400">{event.description}</div>}
      </td>
      <td className="py-2 pr-3">
        <span className={`chip border text-[10px] uppercase ${STATUS_STYLE[event.status] || ''}`}>{event.status}</span>
      </td>
      <td className="py-2 pr-3 font-mono text-xs tabular-nums text-slate-200">
        <div>{datePart(event.start_time)}</div>
        <div>{timePart(event.start_time)}–{timePart(event.end_time)}{datePart(event.end_time) !== datePart(event.start_time) ? ` (+${datePart(event.end_time)})` : ''}</div>
        {event.delay_sec !== 0 && (
          <div className={`text-[10px] ${event.delay_sec > 0 ? 'text-amber-600' : 'text-teal-700'}`}>
            {event.delay_sec > 0 ? '+' : '−'}{minutes(Math.abs(event.delay_sec))} vs plan ({clock(event.original_start_time)})
          </div>
        )}
        <div className="text-[10px] text-slate-400">
          arrive {timePart(event.arrival_window_start)}–{timePart(event.arrival_window_end)} · leave {timePart(event.departure_window_start)}–{timePart(event.departure_window_end)}
        </div>
      </td>
      <td className="py-2 pr-3 text-xs tabular-nums">
        <div className="text-slate-200">{integer(event.expected_attendance)} expected</div>
        <div className="mt-1 h-1.5 w-32 rounded bg-surface-700/40">
          <div className="h-1.5 rounded bg-teal-500" style={{ width: `${arrivedPct}%` }} />
        </div>
        <div className="text-[10px] text-slate-400">{integer(event.arrived)} arrived · {integer(event.inside)} inside</div>
        <div className="text-[10px] text-slate-400">{integer(event.remaining_demand)} still to come</div>
      </td>
      <td className="py-2">
        <div className="flex flex-wrap gap-1">
          <button type="button" className="btn-secondary" disabled={disabled || cancelled} onClick={onEdit}>
            <Pencil className="h-3 w-3" /> Edit
          </button>
          <button type="button" className="btn-secondary" disabled={disabled || started || cancelled} onClick={() => onChange({ delay_sec: 900 })}>
            <Clock3 className="h-3 w-3" /> Delay 15
          </button>
          <button type="button" className="btn-secondary" disabled={disabled || started || cancelled} onClick={() => onChange({ delay_sec: -900 })}>
            Bring forward 15
          </button>
          {cancelled ? (
            <button type="button" className="btn-secondary" disabled={disabled} onClick={() => onChange({ status: 'scheduled' })}>
              <RotateCcw className="h-3 w-3" /> Restore
            </button>
          ) : (
            <button type="button" className="btn-secondary" disabled={disabled} onClick={onCancel}>
              <XCircle className="h-3 w-3" /> Cancel event
            </button>
          )}
          <button type="button" className="btn-danger" disabled={disabled} onClick={onDelete}>
            <Trash2 className="h-3 w-3" /> Delete
          </button>
        </div>
      </td>
    </tr>
  );
}

export default function Events() {
  const { simTime, toast, upsertEvent, removeEvent, mockMode } = useStore();
  const { data, error, reload } = useLiveQuery(() => api.events(), [], { everyCycles: 2 });
  const [venues, setVenues] = useState([]);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(null);     // null | 'new' | event
  const [confirm, setConfirm] = useState(null);     // {kind: 'cancel'|'delete', event}
  const events = data?.events || [];

  useEffect(() => {
    api.eventVenues().then((r) => setVenues(r.venues || [])).catch(() => setVenues([]));
  }, []);

  async function run(fn, success) {
    setBusy(true);
    try {
      const result = await fn();
      toast(success(result), 'success');
      reload();
      return true;
    } catch (err) {
      toast(err.message, 'error');
      return false;
    } finally {
      setBusy(false);
    }
  }

  const change = (event, body) =>
    run(async () => {
      const updated = await api.updateEvent(event.event_id, body);
      upsertEvent(updated);
      return updated;
    }, (u) => `${u.name}: schedule updated — the live city was re-planned`);

  async function save(form) {
    if (editing === 'new') {
      const ok = await run(async () => {
        const created = await api.createEvent(payload(form, null));
        upsertEvent(created);
        return created;
      }, (c) => `${c.name} added — its visitors are now part of the demand`);
      if (ok) setEditing(null);
      return;
    }
    const event = editing;
    const body = payload(form, event);
    if (!Object.keys(body).length) {
      setEditing(null);
      return;
    }
    const ok = await change(event, body);
    if (ok) setEditing(null);
  }

  async function doConfirm() {
    const { kind, event } = confirm;
    const ok = kind === 'delete'
      ? await run(async () => {
        const r = await api.deleteEvent(event.event_id);
        removeEvent(event.event_id);
        return r;
      }, (r) => `${r.name} deleted — no new demand; ${integer(r.arrived)} visitors already here leave normally`)
      : await change(event, { status: 'cancelled' });
    if (ok) setConfirm(null);
  }

  const live = events.filter((e) => e.status === 'live').length;
  const arriving = events.filter((e) => e.status === 'arriving').length;
  const total = events.filter((e) => e.status !== 'cancelled').reduce((a, e) => a + e.expected_attendance, 0);

  return (
    <PageShell
      title="Event schedule"
      subtitle="Every event is a demand source. Creating, editing, delaying, cancelling or deleting an event re-plans arrivals across hotels, transport, roads and gates in the live simulation at once. Times are UTC on the simulation clock."
    >
      <ErrorNote error={error} />
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        <Stat label="Events" value={events.length} />
        <Stat label="Live now" value={live} tone={live ? 'text-emerald-600' : 'text-slate-100'} />
        <Stat label="Arrivals under way" value={arriving} tone={arriving ? 'text-amber-600' : 'text-slate-100'} />
        <Stat label="Expected visitors" value={integer(total)} />
      </div>
      <Timeline events={events} simTime={simTime} />
      <section className="panel mt-3 overflow-x-auto p-3">
        <div className="mb-2 flex items-center gap-2">
          <CalendarClock className="h-4 w-4 text-teal-600" />
          <h2 className="panel-title">Event schedule</h2>
          <button type="button" className="btn-primary ml-auto" disabled={mockMode || busy} onClick={() => setEditing('new')}
            title={mockMode ? 'Needs the live backend' : undefined}>
            <Plus className="h-3 w-3" /> Add event
          </button>
        </div>
        {!data && !error && <div className="skeleton h-24" />}
        <table className="w-full min-w-[900px] text-left text-sm">
          <thead className="text-[10px] uppercase tracking-wider text-slate-400">
            <tr>
              <th className="pb-1 font-semibold">Event</th>
              <th className="pb-1 font-semibold">Status</th>
              <th className="pb-1 font-semibold">Date · time (UTC)</th>
              <th className="pb-1 font-semibold">Visitors</th>
              <th className="pb-1 font-semibold">Operator actions</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <EventRow
                key={e.event_id}
                event={e}
                busy={busy}
                replay={mockMode}
                onEdit={() => setEditing(e)}
                onChange={(body) => change(e, body)}
                onCancel={() => setConfirm({ kind: 'cancel', event: e })}
                onDelete={() => setConfirm({ kind: 'delete', event: e })}
              />
            ))}
          </tbody>
        </table>
      </section>

      {editing && (
        <Modal title={editing === 'new' ? 'Add event' : `Edit ${editing.name}`} onClose={() => setEditing(null)}>
          <EventForm
            initial={editing === 'new' ? emptyForm(simTime) : formFromEvent(editing)}
            event={editing === 'new' ? null : editing}
            venues={venues}
            busy={busy}
            onSubmit={save}
            onClose={() => setEditing(null)}
          />
        </Modal>
      )}
      {confirm?.kind === 'delete' && (
        <Confirm
          title="Delete this event?"
          body="This removes the event from the active schedule and stops future demand generation. Visitors who are already in the city leave normally."
          keepLabel="Cancel"
          confirmLabel="Delete event"
          busy={busy}
          onConfirm={doConfirm}
          onClose={() => setConfirm(null)}
        />
      )}
      {confirm?.kind === 'cancel' && (
        <Confirm
          title="Cancel this event?"
          body="Future demand will stop. Existing attendees will leave according to the simulation. The event stays in the schedule as cancelled and can be restored."
          keepLabel="Keep event"
          confirmLabel="Cancel event"
          busy={busy}
          onConfirm={doConfirm}
          onClose={() => setConfirm(null)}
        />
      )}
    </PageShell>
  );
}
