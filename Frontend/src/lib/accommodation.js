/**
 * Display labels for hotel capacity and availability. Pure formatting of values
 * the backend owns (rooms, sources, status) — nothing here decides occupancy,
 * capacity or saturation.
 */
const SOURCE_LABEL = {
  osm_rooms: 'OSM rooms',
  osm_capacity_rooms: 'OSM capacity:rooms',
  derived_from_osm_beds: 'Derived from OSM beds',
  derived_from_osm_capacity_beds: 'Derived from OSM capacity:beds',
  derived_from_osm_capacity_persons: 'Derived from OSM capacity:persons',
  derived_estimate: 'Derived estimate',
  catalogue: 'Demo catalogue (synthetic)',
  file: 'File data',
};

/** "OSM rooms", "Derived estimate", … — never "actual". */
export function capacitySourceLabel(source) {
  if (!source) return 'Unknown source';
  return SOURCE_LABEL[source] || source.replace(/_/g, ' ');
}

/** Mapped values read exactly; anything derived or estimated is prefixed with "~". */
export function roomCapacityLabel(rooms, confidence) {
  if (rooms === null || rooms === undefined) return '—';
  const n = Math.round(rooms).toLocaleString('en-IN');
  return `${confidence === 'high' ? '' : '~'}${n} rooms`;
}

export function confidenceLabel(confidence) {
  return { high: 'High', medium: 'Medium', low: 'Low' }[confidence] || 'Unknown';
}

/** Backend status -> operator wording ("limited" is shown as TIGHT). */
export function statusLabel(status) {
  return { available: 'AVAILABLE', limited: 'TIGHT', saturated: 'SATURATED' }[status] || String(status || '').toUpperCase();
}
