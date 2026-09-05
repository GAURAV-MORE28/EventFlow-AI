/**
 * Colour tokens — 02_FRONTEND_CONTRACT.md §3.
 *
 * **Never derive a colour from a numeric threshold here.** Always key off the
 * `risk_band` / `verdict` string the API returned. The moment this file starts
 * doing `if (score > 80)`, the contract is broken and the frontend has quietly
 * become a second source of truth.
 */

export const RISK_COLORS = {
  low: { hex: '#22C55E', bg: 'bg-green-500', text: 'text-green-400', border: 'border-green-500' },
  moderate: { hex: '#EAB308', bg: 'bg-yellow-500', text: 'text-yellow-400', border: 'border-yellow-500' },
  high: { hex: '#F97316', bg: 'bg-orange-500', text: 'text-orange-400', border: 'border-orange-500' },
  critical: { hex: '#EF4444', bg: 'bg-red-500', text: 'text-red-400', border: 'border-red-500' },
};

export const VERDICT_STYLE = {
  STABLE: { hex: '#22C55E', label: 'STABLE', icon: 'shield-check', text: 'text-green-400', border: 'border-green-500' },
  CONDITIONAL: { hex: '#EAB308', label: 'CONDITIONAL', icon: 'shield-alert', text: 'text-yellow-400', border: 'border-yellow-500' },
  UNSTABLE: { hex: '#EF4444', label: 'UNSTABLE', icon: 'shield-x', text: 'text-red-400', border: 'border-red-500' },
};

const FALLBACK = { hex: '#64748B', bg: 'bg-slate-500', text: 'text-slate-400', border: 'border-slate-500' };

export function riskColor(band) {
  return RISK_COLORS[band] || FALLBACK;
}

export function verdictStyle(verdict) {
  return VERDICT_STYLE[verdict] || { ...FALLBACK, label: 'UNCERTIFIED', icon: 'shield' };
}

/** Deck.gl wants `[r, g, b, a]`, so convert the same token rather than a new one. */
export function rgba(hex, alpha = 255) {
  const n = parseInt(hex.replace('#', ''), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255, alpha];
}

/** Icon glyph per entity_type — cosmetic only, never load-bearing. */
export const TYPE_GLYPH = {
  venue: '◈',
  zone: '▣',
  transport_node: '◉',
  transport_route: '═',
  road: '━',
  hotel: '⌂',
  parking: 'P',
  gate: '▲',
  emergency_facility: '✚',
};
