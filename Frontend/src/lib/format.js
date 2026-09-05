/**
 * Formatting rules — 02_FRONTEND_CONTRACT.md §2.
 *
 * Every display transform lives here so the demo reads consistently. These
 * functions *format*; they never derive. If a value needs computing, the API
 * computed it.
 */

/** Unknown / null renders as an em dash. Never `0`, never "N/A". */
export const EM_DASH = '—';

/** `1080` -> `18 min`. Under 60s -> `<1 min`. */
export function minutes(seconds) {
  if (seconds === null || seconds === undefined) return EM_DASH;
  if (seconds < 60) return '<1 min';
  return `${Math.round(seconds / 60)} min`;
}

/** Bare minute count, for the hero number where "min" is a separate element. */
export function minutesValue(seconds) {
  if (seconds === null || seconds === undefined) return EM_DASH;
  if (seconds < 60) return '<1';
  return String(Math.round(seconds / 60));
}

/** `0.61` -> `61%`, zero decimals. */
export function percent(ratio) {
  if (ratio === null || ratio === undefined) return EM_DASH;
  return `${Math.round(ratio * 100)}%`;
}

/** `31.0` -> `31.0%`, one decimal. For fields already named `*_pct`. */
export function pct(value) {
  if (value === null || value === undefined) return EM_DASH;
  return `${value.toFixed(1)}%`;
}

/** `50000` -> `₹500`, thousands separated. */
export function rupees(paise) {
  if (paise === null || paise === undefined) return EM_DASH;
  return `₹${Math.round(paise / 100).toLocaleString('en-IN')}`;
}

/** `2026-09-04T14:32:00Z` -> `14:32` (24h, UTC — sim time is UTC by contract). */
export function clock(iso) {
  if (!iso) return EM_DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return EM_DASH;
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
}

/** Signed difference between two ISO timestamps, in seconds. */
export function secondsBetween(fromIso, toIso) {
  if (!fromIso || !toIso) return null;
  return Math.round((new Date(toIso).getTime() - new Date(fromIso).getTime()) / 1000);
}

/** Countdown like `1h 28m`, for time-to-kickoff. */
export function countdown(seconds) {
  if (seconds === null || seconds === undefined) return EM_DASH;
  if (seconds <= 0) return 'live';
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

/** Fixed decimals, em dash for null. Used for load_variance at 3 dp. */
export function decimals(value, places = 3) {
  if (value === null || value === undefined) return EM_DASH;
  return value.toFixed(places);
}

export function integer(value) {
  if (value === null || value === undefined) return EM_DASH;
  return Math.round(value).toLocaleString('en-IN');
}

/** Turn `metro_b` into `Metro B` when no display_name is available. */
export function humanise(id) {
  if (!id) return EM_DASH;
  return id
    .split('_')
    .map((part) => (part.length <= 2 ? part.toUpperCase() : part[0].toUpperCase() + part.slice(1)))
    .join(' ');
}
