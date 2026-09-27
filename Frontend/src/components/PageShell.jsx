/**
 * Shared frame for the operator pages: TopBar, navigation, and a scrolling
 * content area with a page heading.
 */
import { riskColor } from '../lib/colors.js';
import { useStore } from '../store/useStore.js';
import TopBar from './TopBar.jsx';

export default function PageShell({ title, subtitle, actions = null, children }) {
  return (
    <div className="flex h-screen flex-col overflow-hidden bg-surface-950 font-sans">
      <TopBar />
      <main className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-7xl px-4 py-4 pb-24">
          <ReplayBanner />
          <header className="mb-4 flex flex-wrap items-end justify-between gap-3 border-b border-surface-700/60 pb-3">
            <div>
              <h1 className="text-lg font-bold tracking-tight text-slate-100">{title}</h1>
              {subtitle && <p className="mt-0.5 max-w-3xl text-xs text-slate-400">{subtitle}</p>}
            </div>
            {actions}
          </header>
          {children}
        </div>
      </main>
    </div>
  );
}

/**
 * Shown on every page in replay mode (`npm run dev`, no backend): the data is
 * a recording of a real simulation run, so nothing here is live and actions
 * that would change the city are disabled rather than silently faked.
 */
export function ReplayBanner({ className = 'mb-3' }) {
  const mockMode = useStore((s) => s.mockMode);
  if (!mockMode) return null;
  return (
    <div className={`rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 ${className}`}>
      <b>Recorded replay.</b> You are viewing data recorded from a real run of the simulation, not a live
      city. Actions that change the city, plan a new journey or run a new simulation are disabled. Start the
      backend and run <code>npm run dev:live</code> for the live product.
    </div>
  );
}

/** Small labelled figure used across pages. */
export function Stat({ label, value, sub = null, tone = 'text-slate-100' }) {
  return (
    <div className="panel px-3 py-2">
      <div className="text-[9px] font-semibold uppercase tracking-wider text-slate-400">{label}</div>
      <div className={`mt-0.5 text-lg font-bold tabular-nums ${tone}`}>{value}</div>
      {sub && <div className="text-[10px] text-slate-400">{sub}</div>}
    </div>
  );
}

/** Horizontal utilisation bar, coloured by the band the API returned. */
export function UtilBar({ value, band, max = 1.3 }) {
  const width = Math.max(0, Math.min(1, (value ?? 0) / max)) * 100;
  const colour = riskColor(band).hex;
  return (
    <div className="relative h-2 w-full rounded bg-surface-700/40">
      <div className="absolute inset-y-0 left-0 rounded" style={{ width: `${width}%`, backgroundColor: colour }} />
      <div className="absolute inset-y-0 w-px bg-slate-400/70" style={{ left: `${(1 / max) * 100}%` }} title="100% capacity" />
    </div>
  );
}

export function BandChip({ band }) {
  const c = riskColor(band).hex;
  return (
    <span
      className="chip border text-[10px] font-semibold uppercase"
      style={{ color: c, borderColor: `${c}55`, backgroundColor: `${c}14` }}
    >
      {band || '—'}
    </span>
  );
}

export function ErrorNote({ error }) {
  if (!error) return null;
  return (
    <div className="panel border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-700">
      {error.message}
    </div>
  );
}

