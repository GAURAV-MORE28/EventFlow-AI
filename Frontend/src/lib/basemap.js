/**
 * Geographic basemap for generated worlds: OpenStreetMap raster tiles drawn as
 * deck.gl BitmapLayers under the network. No new dependency (BitmapLayer ships
 * with @deck.gl/layers).
 *
 * Tile usage policy (https://operations.osmfoundation.org/policies/tiles/):
 * only the tiles covering the current viewport are requested (capped), nothing
 * is prefetched or bulk-downloaded, and the "© OpenStreetMap contributors"
 * attribution is always visible on the map (see <MapAttribution/>).
 * `VITE_TILE_URL` points at another tile server if needed.
 */
import { BitmapLayer } from '@deck.gl/layers';
import { WebMercatorViewport } from '@deck.gl/core';

export const TILE_URL = import.meta.env?.VITE_TILE_URL || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
export const TILE_ATTRIBUTION = import.meta.env?.VITE_TILE_ATTRIBUTION || '© OpenStreetMap contributors';
const MAX_TILES = 48;

const lon2x = (lon, z) => Math.floor(((lon + 180) / 360) * 2 ** z);
const lat2y = (lat, z) => {
  const r = (Math.max(-85.05, Math.min(85.05, lat)) * Math.PI) / 180;
  return Math.floor(((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * 2 ** z);
};
const x2lon = (x, z) => (x / 2 ** z) * 360 - 180;
const y2lat = (y, z) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / 2 ** z))) * 180) / Math.PI;

/** Tiles covering the viewport: [{z, x, y, bounds:[w,s,e,n]}]. */
export function visibleTiles(viewState, width, height) {
  if (!viewState || !width || !height) return [];
  let viewport;
  try {
    viewport = new WebMercatorViewport({ ...viewState, width, height, pitch: 0, bearing: 0 });
  } catch {
    return [];
  }
  const [west, north] = viewport.unproject([0, 0]);
  const [east, south] = viewport.unproject([width, height]);
  // deck.gl zoom uses 512-px tiles; OSM raster tiles are 256 px -> one level up.
  let z = Math.max(0, Math.min(19, Math.round(viewState.zoom) + 1));
  for (; z > 0; z -= 1) {
    const n = (lon2x(east, z) - lon2x(west, z) + 1) * (lat2y(south, z) - lat2y(north, z) + 1);
    if (n <= MAX_TILES) break;
  }
  const max = 2 ** z - 1;
  const out = [];
  for (let x = Math.max(0, lon2x(west, z)); x <= Math.min(max, lon2x(east, z)); x += 1) {
    for (let y = Math.max(0, lat2y(north, z)); y <= Math.min(max, lat2y(south, z)); y += 1) {
      out.push({ z, x, y, bounds: [x2lon(x, z), y2lat(y + 1, z), x2lon(x + 1, z), y2lat(y, z)] });
    }
  }
  return out;
}

export function basemapLayers(viewState, width, height, { opacity = 0.6, desaturate = 0.5 } = {}) {
  return visibleTiles(viewState, width, height).map(
    (t) =>
      new BitmapLayer({
        id: `osm-tile-${t.z}-${t.x}-${t.y}`,
        image: TILE_URL.replace('{z}', t.z).replace('{x}', t.x).replace('{y}', t.y),
        bounds: t.bounds,
        opacity,
        desaturate,
        pickable: false,
      }),
  );
}
