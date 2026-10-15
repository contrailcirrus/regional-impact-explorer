/**
 * @fileoverview Pure MapLibre layer/source helpers, LRU grid cache and
 * in-flight request deduplication, GeoJSON boundary loaders, antimeridian-safe
 * centroid calculation, FIR feature matching, initial-country resolution, and
 * year-gate helpers for `RegionalDashboard`.
 */

import * as d3 from 'd3';
import {
  CountryListStatus,
  CountryOption,
  GeoFeatureLike,
  GeoFeatureProperties,
  getAllCountries,
  getCountryListStatus,
  getLatestYear,
  GwpHorizon,
  isRecordObject,
  requireYear,
  resolveCountryIdentifier,
  topCountryByCo2eRank,
  YearListStatus,
} from './CountryMetadata';
import {
  parseGridValueMax,
  parseGwpTonnesPerMj,
  RasterMetadata,
} from './geo_raster_utils';
import {FIR_TYPE_DOMESTIC} from './metric_formatters';

/** Minimal MapLibre event shape. */
export interface MapLibreEvent {
  readonly target: MapInstance;
  readonly type: string;
  readonly [property: string]: unknown;
}

/** Minimal MapLibre layer mouse event shape. */
export interface MapLayerMouseEvent extends MapLibreEvent {
  readonly point: {readonly x: number; readonly y: number};
  readonly lngLat: {readonly lng: number; readonly lat: number};
  readonly features?: ReadonlyArray<Record<string, unknown>>;
}

/** Minimal MapLibre `Map` instance interface used by the dashboard. */
export interface MapInstance {
  readonly style?: unknown;
  getCanvas(): HTMLCanvasElement | null;
  getZoom(): number;
  getCenter(): {readonly lng: number; readonly lat: number};
  addControl(control: unknown, position?: string): void;
  on(type: 'error', listener: (err: unknown) => void): this;
  on(type: string, listener: (event: MapLayerMouseEvent) => void): this;
  on(type: string, listener: () => void): this;
  on(
    type: string,
    layerId: string,
    listener: (event: MapLayerMouseEvent) => void,
  ): this;
  on(type: string, layerId: string, listener: () => void): this;
  off(type: string, listener: (event: MapLayerMouseEvent) => void): this;
  off(type: string, listener: () => void): this;
  off(
    type: string,
    layerId: string,
    listener: (event: MapLayerMouseEvent) => void,
  ): this;
  off(type: string, layerId: string, listener: () => void): this;
  addSource(id: string, source: Record<string, unknown>): this;
  getSource(id: string):
    | {
        setData(data: unknown): void;
        updateImage?(options: {
          url: string;
          coordinates: Array<[number, number]> | number[][];
        }): void;
      }
    | undefined;
  removeSource(id: string): this;
  addLayer(layer: Record<string, unknown>, beforeId?: string): this;
  getLayer(id: string): unknown;
  removeLayer(id: string): this;
  setPaintProperty(layerId: string, name: string, value: unknown): this;
  setLayoutProperty(layerId: string, name: string, value: unknown): this;
  setFeatureState(
    feature: {source: string; id?: string | number},
    state: Record<string, unknown>,
  ): void;
  removeFeatureState(
    target: {source: string; id?: string | number},
    key?: string,
  ): void;
  flyTo(options: Record<string, unknown>): this;
  easeTo(options: Record<string, unknown>): this;
  fitBounds(bounds: unknown, options?: Record<string, unknown>): this;
  remove(): void;
  queryRenderedFeatures(
    geometry?: unknown,
    options?: Record<string, unknown>,
  ): FirFeature[];
}

/** MapLibre ids of the raster source/layer that display the heatmap grid. */
export const GRID_SOURCE_ID = 'impact-grid-src';
/** MapLibre layer id for the heatmap raster overlay. */
export const GRID_LAYER_ID = 'impact-grid-layer';

/**
 * Removes the heatmap raster layer and source after the map's WebGL context is
 * restored. The image texture lived in the lost context, so MapLibre has
 * nothing to redraw; the caller re-runs `updateGridLayer`, which re-adds the
 * source from the grid cache. Blob URLs are intentionally not revoked here:
 * the grid cache still owns them and the re-added source reuses them.
 * @visibleForTesting
 */
export function detachGridLayerForContextRestore(map: MapInstance): void {
  try {
    if (map.getLayer(GRID_LAYER_ID)) {
      map.removeLayer(GRID_LAYER_ID);
    }
    if (map.getSource(GRID_SOURCE_ID)) {
      map.removeSource(GRID_SOURCE_ID);
    }
  } catch (e: unknown) {
    console.warn('Failed to detach grid layer after WebGL context restore:', e);
  }
}

/**
 * True when `map` has a loaded style containing layer `id`. MapLibre's
 * `getLayer` throws while a style is being torn down (unmount or `setStyle`);
 * that is treated as "absent".
 * @visibleForTesting
 */
export function mapHasLayer(
  map: MapInstance | null | undefined,
  id: string,
): boolean {
  if (!map?.style) return false;
  try {
    return Boolean(map.getLayer(id));
  } catch (e: unknown) {
    console.debug(`getLayer('${id}') threw during style teardown:`, e);
    return false;
  }
}

/**
 * True when `map` has a loaded style containing source `id`; see
 * `mapHasLayer` for why lookups may throw.
 * @visibleForTesting
 */
export function mapHasSource(
  map: MapInstance | null | undefined,
  id: string,
): boolean {
  if (!map?.style) return false;
  try {
    return Boolean(map.getSource(id));
  } catch (e: unknown) {
    console.debug(`getSource('${id}') threw during style teardown:`, e);
    return false;
  }
}

/**
 * Removes the heatmap raster layer and its source if present. Each removal is
 * attempted independently so a failed layer removal still lets the source be
 * cleaned up; failures (expected during unmount/style reload) are logged at
 * debug level. Does not revoke blob URLs.
 * @visibleForTesting
 */
export function removeGridLayerAndSource(map: MapInstance): void {
  if (mapHasLayer(map, GRID_LAYER_ID)) {
    try {
      map.removeLayer(GRID_LAYER_ID);
    } catch (e: unknown) {
      console.debug('Failed to remove grid layer:', e);
    }
  }
  if (mapHasSource(map, GRID_SOURCE_ID)) {
    try {
      map.removeSource(GRID_SOURCE_ID);
    } catch (e: unknown) {
      console.debug('Failed to remove grid source:', e);
    }
  }
}

/** Minimal global `maplibregl` constructor surface. */
export declare interface MapLibreGl {
  readonly Map: new (options: Record<string, unknown>) => MapInstance;
  readonly NavigationControl: new (
    options?: Record<string, unknown>,
  ) => unknown;
}

/**
 * Properties of a feature in the published `firs.geojson`. The pipeline
 * normalizes every FIR to exactly these names; no other spellings are read.
 */
export declare interface FirFeatureProperties extends GeoFeatureProperties {
  readonly fir_id?: string;
  readonly fir_icao?: string;
  readonly firname?: string;
  readonly type?: string;
  /** Set by `matchFirFeatures`; not part of the published file. */
  readonly firType?: string;
  readonly fir_to_country_iso2s?: string;
  readonly areasqkm?: number;
}

/** A single FIR GeoJSON feature. */
export interface FirFeature extends GeoFeatureLike {
  readonly id?: string | number;
  readonly type?: string;
  readonly properties?: FirFeatureProperties;
  readonly geometry?: unknown;
}

/** GeoJSON FeatureCollection of FIR features. */
export interface FirFeatureCollection {
  readonly type: 'FeatureCollection';
  readonly features: readonly FirFeature[];
}

/** Cached rendered grid item in the LRU cache. */
export interface CachedGridItem {
  readonly dataUrl: string;
  readonly coords: Array<[number, number]>;
  readonly meta: RasterMetadata;
  /** Colour-scale maximum the image was rendered with (from `meta`). */
  readonly valueMax: number;
}

/** Maximum number of rendered grid entries kept in the LRU cache. */
export const MAX_GRID_CACHE_ENTRIES = 120;

/**
 * Looks up `key` in `cache` and refreshes its LRU position when present.
 * @visibleForTesting
 */
export function getCachedGridEntry(
  cache: Map<string, CachedGridItem>,
  key: string,
): CachedGridItem | undefined {
  const existing = cache.get(key);
  if (existing !== undefined) {
    // Refresh LRU order
    cache.delete(key);
    cache.set(key, existing);
  }
  return existing;
}

/**
 * Revokes `url` via `URL.revokeObjectURL` when it is a `blob:` URL and does
 * not match `protectedUrl`.
 * @visibleForTesting
 */
export function revokeBlobUrlIfSafe(
  url: string | null | undefined,
  protectedUrl?: string | null,
): void {
  if (
    url &&
    url !== protectedUrl &&
    url.startsWith('blob:') &&
    typeof URL !== 'undefined' &&
    typeof URL.revokeObjectURL === 'function'
  ) {
    URL.revokeObjectURL(url);
  }
}

/**
 * Inserts `value` under `key` into `cache`, evicting the oldest entries whose
 * `dataUrl` does not match `protectedUrl` (and revoking their `blob:` URLs)
 * when `cache.size >= maxEntries`.
 * @visibleForTesting
 */
export function setCachedGridEntry(
  cache: Map<string, CachedGridItem>,
  key: string,
  value: CachedGridItem,
  maxEntries: number = MAX_GRID_CACHE_ENTRIES,
  protectedUrl?: string | null,
): void {
  const prev = cache.get(key);
  if (prev !== undefined) {
    if (prev.dataUrl !== value.dataUrl) {
      revokeBlobUrlIfSafe(prev.dataUrl, protectedUrl);
    }
    cache.delete(key);
  }
  while (cache.size >= maxEntries) {
    let keyToEvict: string | undefined;
    for (const [candidateKey, candidateVal] of cache) {
      if (!protectedUrl || candidateVal.dataUrl !== protectedUrl) {
        keyToEvict = candidateKey;
        break;
      }
    }
    if (keyToEvict === undefined) {
      keyToEvict = cache.keys().next().value;
    }
    if (keyToEvict === undefined) break;
    const evicted = cache.get(keyToEvict);
    cache.delete(keyToEvict);
    revokeBlobUrlIfSafe(evicted?.dataUrl, protectedUrl);
  }
  cache.set(key, value);
}

/**
 * Revokes all `blob:` URLs in `cache` and clears the map.
 * @visibleForTesting
 */
export function clearGridCache(cache: Map<string, CachedGridItem>): void {
  for (const entry of cache.values()) {
    revokeBlobUrlIfSafe(entry.dataUrl);
  }
  cache.clear();
}

/**
 * Inserts `value` into `cache` when `isMounted` is true; otherwise immediately
 * revokes `value.dataUrl` (if it is a `blob:` URL) and returns `null` so
 * post-unmount async resolutions cannot repopulate `gridCacheRef` or leak
 * ObjectURLs.
 * @visibleForTesting
 */
export function cacheOrRevokeGridResult(
  cache: Map<string, CachedGridItem>,
  key: string,
  value: CachedGridItem,
  isMounted: boolean,
  maxEntries: number = MAX_GRID_CACHE_ENTRIES,
  protectedUrl?: string | null,
): CachedGridItem | null {
  if (!isMounted) {
    revokeBlobUrlIfSafe(value.dataUrl, protectedUrl);
    return null;
  }
  setCachedGridEntry(cache, key, value, maxEntries, protectedUrl);
  return value;
}

/**
 * Tracks a deduplicated in-flight grid fetch and its attached caller signals.
 */
export interface InFlightGridEntry {
  promise: Promise<CachedGridItem | null>;
  readonly controller: AbortController;
  readonly signals: Set<AbortSignal>;
  hasUnabortableCaller: boolean;
}

/**
 * Aborts every active `InFlightGridEntry` in `inFlightMap` and clears the map.
 * Invoked when the dashboard unmounts so in-flight grid fetches and renders do
 * not continue after teardown.
 * @visibleForTesting
 */
export function abortAndClearInFlightGrids(
  inFlightMap: Map<string, InFlightGridEntry>,
): void {
  for (const entry of inFlightMap.values()) {
    entry.controller.abort();
  }
  inFlightMap.clear();
}

/**
 * Coordinates in-flight grid requests by cache key, ensuring that when all
 * attached caller signals abort, the in-flight entry is synchronously evicted
 * from `inFlightMap` before aborting its controller so an immediate retry in
 * the same tick does not attach to a dead request.
 * @visibleForTesting
 */
export function deduplicateInFlightGridRequest(
  inFlightMap: Map<string, InFlightGridEntry>,
  cacheKey: string,
  startFetch: (signal: AbortSignal) => Promise<CachedGridItem | null>,
  callerSignal?: AbortSignal,
): Promise<CachedGridItem | null> {
  if (callerSignal?.aborted) {
    return Promise.resolve(null);
  }

  const signalListeners: Array<{sig: AbortSignal; fn: () => void}> = [];

  const registerSignalAbortCheck = (
    entry: InFlightGridEntry,
    signal?: AbortSignal,
  ) => {
    if (!signal) {
      entry.hasUnabortableCaller = true;
      return;
    }
    entry.signals.add(signal);
    const onAbort = () => {
      const allCallersAborted =
        !entry.hasUnabortableCaller &&
        Array.from(entry.signals).every((s) => s.aborted);
      if (!allCallersAborted) {
        return;
      }
      if (inFlightMap.get(cacheKey) === entry) {
        inFlightMap.delete(cacheKey);
      }
      entry.controller.abort();
    };
    signalListeners.push({sig: signal, fn: onAbort});
    signal.addEventListener('abort', onAbort, {once: true});
  };

  const cleanupSignalListeners = () => {
    for (const {sig, fn} of signalListeners) {
      sig.removeEventListener('abort', fn);
    }
  };

  const inFlight = inFlightMap.get(cacheKey);
  if (inFlight !== undefined && !inFlight.controller.signal.aborted) {
    registerSignalAbortCheck(inFlight, callerSignal);
    return inFlight.promise
      .then((res) => (callerSignal?.aborted ? null : res))
      .finally(cleanupSignalListeners);
  }
  if (inFlight !== undefined) {
    inFlightMap.delete(cacheKey);
  }

  const controller = new AbortController();
  const entry: InFlightGridEntry = {
    promise: Promise.resolve(null),
    controller,
    signals: new Set<AbortSignal>(),
    hasUnabortableCaller: !callerSignal,
  };
  registerSignalAbortCheck(entry, callerSignal);

  const fetchPromise = startFetch(controller.signal).finally(() => {
    if (inFlightMap.get(cacheKey) === entry) {
      inFlightMap.delete(cacheKey);
    }
    cleanupSignalListeners();
  });

  entry.promise = fetchPromise;
  inFlightMap.set(cacheKey, entry);
  return fetchPromise.then((res) => (callerSignal?.aborted ? null : res));
}

/** Normalizes camelCase or snake_case bounding-box fields, or `undefined`. */
function normalizeRasterBBox(
  value: unknown,
): RasterMetadata['bbox'] | undefined {
  if (!isRecordObject(value)) return undefined;
  const minLon =
    typeof value['minLon'] === 'number' && Number.isFinite(value['minLon'])
      ? value['minLon']
      : typeof value['min_lon'] === 'number' &&
          Number.isFinite(value['min_lon'])
        ? value['min_lon']
        : undefined;
  const maxLon =
    typeof value['maxLon'] === 'number' && Number.isFinite(value['maxLon'])
      ? value['maxLon']
      : typeof value['max_lon'] === 'number' &&
          Number.isFinite(value['max_lon'])
        ? value['max_lon']
        : undefined;
  const minLat =
    typeof value['minLat'] === 'number' && Number.isFinite(value['minLat'])
      ? value['minLat']
      : typeof value['min_lat'] === 'number' &&
          Number.isFinite(value['min_lat'])
        ? value['min_lat']
        : undefined;
  const maxLat =
    typeof value['maxLat'] === 'number' && Number.isFinite(value['maxLat'])
      ? value['maxLat']
      : typeof value['max_lat'] === 'number' &&
          Number.isFinite(value['max_lat'])
        ? value['max_lat']
        : undefined;
  return {
    ...(minLon !== undefined ? {minLon} : {}),
    ...(maxLon !== undefined ? {maxLon} : {}),
    ...(minLat !== undefined ? {minLat} : {}),
    ...(maxLat !== undefined ? {maxLat} : {}),
  };
}

/**
 * Normalizes a raw `/grid/.../meta` or `X-Grid-Meta` JSON object into a
 * `RasterMetadata` with camelCase `bbox` bounds, or returns `null` if invalid.
 * @visibleForTesting
 */
export function normalizeRasterMetadata(value: unknown): RasterMetadata | null {
  if (!isRecordObject(value)) return null;
  if (
    typeof value['width'] !== 'number' ||
    !Number.isInteger(value['width']) ||
    value['width'] <= 0 ||
    typeof value['height'] !== 'number' ||
    !Number.isInteger(value['height']) ||
    value['height'] <= 0
  ) {
    return null;
  }
  const dtype = typeof value['dtype'] === 'string' ? value['dtype'] : undefined;
  const encoding =
    typeof value['encoding'] === 'string' ? value['encoding'] : undefined;
  const normalizedBBox = normalizeRasterBBox(value['bbox']);
  const rawValueMax =
    value['valueMaxRefScaled'] ?? value['value_max_ref_scaled'];
  const impactMax = parseGridValueMax(
    {valueMaxRefScaled: rawValueMax},
    'impact',
  );
  const densityMax = parseGridValueMax(
    {valueMaxRefScaled: rawValueMax},
    'density',
  );
  const valueMaxRefScaled =
    impactMax !== undefined || densityMax !== undefined
      ? {
          ...(impactMax !== undefined ? {impact: impactMax} : {}),
          ...(densityMax !== undefined ? {density: densityMax} : {}),
        }
      : undefined;
  const rawGwp = value['gwpTonnesCo2ePerMj'] ?? value['gwp_tonnes_co2e_per_mj'];
  const gwpTonnesCo2ePerMj = parseGwpTonnesPerMj({gwpTonnesCo2ePerMj: rawGwp});
  return {
    width: value['width'],
    height: value['height'],
    ...(dtype !== undefined ? {dtype} : {}),
    ...(encoding !== undefined ? {encoding} : {}),
    ...(normalizedBBox !== undefined ? {bbox: normalizedBBox} : {}),
    ...(valueMaxRefScaled !== undefined ? {valueMaxRefScaled} : {}),
    ...(gwpTonnesCo2ePerMj !== undefined ? {gwpTonnesCo2ePerMj} : {}),
  };
}

/**
 * Parses and validates the JSON payload of an `X-Grid-Meta` HTTP response
 * header, returning `null` if the header is missing, malformed JSON, or not a
 * valid `RasterMetadata` object.
 * @visibleForTesting
 */
export function parseGridMetaHeader(
  headerValue: string | null | undefined,
): RasterMetadata | null {
  if (!headerValue) return null;
  try {
    const parsed: unknown = JSON.parse(headerValue);
    return normalizeRasterMetadata(parsed);
  } catch {
    return null;
  }
}

/**
 * Decompresses a binary grid buffer when it starts with the 2-byte gzip magic
 * header (`0x1f 0x8b`) and `DecompressionStream` is available in the runtime;
 * otherwise returns `rawBuf` unchanged.
 * @visibleForTesting
 */
export async function decodeGzipBufferIfNeeded(
  rawBuf: ArrayBuffer,
): Promise<ArrayBuffer> {
  const bytes = new Uint8Array(rawBuf);
  if (
    bytes.byteLength >= 2 &&
    bytes[0] === 0x1f &&
    bytes[1] === 0x8b &&
    typeof DecompressionStream !== 'undefined'
  ) {
    const ds = new DecompressionStream('gzip');
    const stream = new Blob([rawBuf]).stream().pipeThrough(ds);
    return await new Response(stream).arrayBuffer();
  }
  return rawBuf;
}

/**
 * Checks whether `value` is a GeoJSON `FirFeatureCollection`.
 * @visibleForTesting
 */
export function isFirFeatureCollection(
  value: unknown,
): value is FirFeatureCollection {
  return (
    typeof value === 'object' &&
    value !== null &&
    'features' in value &&
    Array.isArray(value.features)
  );
}

/** Normalized `/grid/{year}/availability` state. */
export declare interface GridAvailability {
  readonly hasTotal: boolean;
  readonly hasMonthly: boolean;
  readonly hasHourly: boolean;
  readonly monthsCount: number;
  readonly hoursCount: number;
}

/**
 * Normalizes a raw `/grid/{year}/availability` response (or already-normalized
 * `GridAvailability`) into camelCase fields, returning `null` when required
 * boolean flags are missing.
 * @visibleForTesting
 */
export function normalizeGridAvailability(
  value: unknown,
): GridAvailability | null {
  if (!isRecordObject(value)) return null;
  const hasTotal =
    typeof value['hasTotal'] === 'boolean'
      ? value['hasTotal']
      : typeof value['has_total'] === 'boolean'
        ? value['has_total']
        : null;
  const hasMonthly =
    typeof value['hasMonthly'] === 'boolean'
      ? value['hasMonthly']
      : typeof value['has_monthly'] === 'boolean'
        ? value['has_monthly']
        : null;
  const hasHourly =
    typeof value['hasHourly'] === 'boolean'
      ? value['hasHourly']
      : typeof value['has_hourly'] === 'boolean'
        ? value['has_hourly']
        : null;
  if (hasTotal === null || hasMonthly === null || hasHourly === null) {
    return null;
  }
  const monthsCount =
    typeof value['monthsCount'] === 'number' &&
    Number.isFinite(value['monthsCount'])
      ? value['monthsCount']
      : typeof value['months_count'] === 'number' &&
          Number.isFinite(value['months_count'])
        ? value['months_count']
        : 0;
  const hoursCount =
    typeof value['hoursCount'] === 'number' &&
    Number.isFinite(value['hoursCount'])
      ? value['hoursCount']
      : typeof value['hours_count'] === 'number' &&
          Number.isFinite(value['hours_count'])
        ? value['hours_count']
        : 0;
  return {
    hasTotal,
    hasMonthly,
    hasHourly,
    monthsCount,
    hoursCount,
  };
}

/**
 * True for MapLibre `error` events caused by aborted tile/source requests
 * (`{error: {name: 'AbortError'}}` or a message mentioning `AbortError`),
 * which are expected during style/source swaps and not worth logging.
 * @visibleForTesting
 */
export function isAbortMapError(err: unknown): boolean {
  if (!isRecordObject(err)) return false;
  const nested = isRecordObject(err['error']) ? err['error'] : undefined;
  const name = String(nested?.['name'] || '');
  const message = String(nested?.['message'] || err['message'] || '');
  return name === 'AbortError' || message.includes('AbortError');
}

/** Slack, in degrees, when checking a centroid against a bounding box. */
const CENTROID_BOUNDS_TOLERANCE_DEG = 1e-6;

/**
 * True if `point` lies inside the longitude/latitude bounding box of
 * `coords`. For shapes crossing the antimeridian, negative longitudes are
 * shifted by +360 so the box is contiguous.
 */
function isWithinCoordinateBounds(
  point: readonly [number, number],
  coords: ReadonlyArray<readonly [number, number]>,
  crossesAntimeridian: boolean,
): boolean {
  const unwrap = (lon: number) =>
    crossesAntimeridian && lon < 0 ? lon + 360 : lon;
  let minLon = Infinity;
  let maxLon = -Infinity;
  let minLat = Infinity;
  let maxLat = -Infinity;
  for (const [lon, lat] of coords) {
    const unwrapped = unwrap(lon);
    minLon = Math.min(minLon, unwrapped);
    maxLon = Math.max(maxLon, unwrapped);
    minLat = Math.min(minLat, lat);
    maxLat = Math.max(maxLat, lat);
  }
  const tol = CENTROID_BOUNDS_TOLERANCE_DEG;
  const lon = unwrap(point[0]);
  return (
    lon >= minLon - tol &&
    lon <= maxLon + tol &&
    point[1] >= minLat - tol &&
    point[1] <= maxLat + tol
  );
}

/** True for a finite GeoJSON position `[lon, lat, ...]`. */
function isFinitePosition(node: unknown): node is [number, number] {
  return (
    Array.isArray(node) &&
    node.length >= 2 &&
    typeof node[0] === 'number' &&
    typeof node[1] === 'number' &&
    Number.isFinite(node[0]) &&
    Number.isFinite(node[1])
  );
}

/**
 * Computes a geographic centroid `[lon, lat]` that is safe for shapes
 * straddling the antimeridian (+/- 180 deg longitude, e.g. US, Russia, Fiji,
 * New Zealand, KZAK Oakland Oceanic).
 *
 * `d3GeoCentroid` computes an area-weighted centroid on the sphere, where
 * +/-180 deg is not a discontinuity, so it is used for every shape when given.
 * d3 expects clockwise exterior rings; a counter-clockwise (RFC 7946) ring is
 * read as its complement and yields a point near the antipode. A d3 result
 * outside the shape's longitude/latitude bounding box is therefore rejected,
 * like a non-finite one, and the vertices are averaged as 3D unit vectors
 * instead; each ring's closing vertex is skipped so it is not counted twice.
 * @visibleForTesting
 */
export function computeAntimeridianSafeCentroid(
  featureOrCollection: unknown,
  d3GeoCentroid?: (obj: d3.ExtendedFeatureCollection) => [number, number],
): [number, number] | null {
  const coords: Array<[number, number]> = [];
  const collectCoords = (node: unknown): void => {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) {
      if (isFinitePosition(node)) {
        coords.push([node[0], node[1]]);
        return;
      }
      const first = node[0];
      const last = node[node.length - 1];
      const isClosedRing =
        node.length > 1 &&
        isFinitePosition(first) &&
        isFinitePosition(last) &&
        first[0] === last[0] &&
        first[1] === last[1];
      const children = isClosedRing ? node.slice(0, -1) : node;
      for (const child of children) {
        collectCoords(child);
      }
      return;
    }
    if ('features' in node && Array.isArray(node.features)) {
      for (const f of node.features) {
        collectCoords(f);
      }
    } else if ('geometry' in node && node.geometry) {
      collectCoords(node.geometry);
    } else if ('coordinates' in node && node.coordinates) {
      collectCoords(node.coordinates);
    }
  };

  collectCoords(featureOrCollection);
  if (coords.length === 0) return null;

  let minLon = Infinity;
  let maxLon = -Infinity;
  for (const [lon] of coords) {
    if (lon < minLon) minLon = lon;
    if (lon > maxLon) maxLon = lon;
  }

  const crossesAntimeridian = maxLon - minLon > 180;
  if (d3GeoCentroid) {
    // The input is untyped GeoJSON; d3.geoCentroid tolerates any GeoJSON object
    // and the coordinate walk above has already confirmed it holds positions.
    const c = d3GeoCentroid(
      featureOrCollection as d3.ExtendedFeatureCollection,
    );
    if (
      c &&
      Number.isFinite(c[0]) &&
      Number.isFinite(c[1]) &&
      isWithinCoordinateBounds(c, coords, crossesAntimeridian)
    ) {
      return [c[0], c[1]];
    }
  }

  let sumX = 0;
  let sumY = 0;
  let sumZ = 0;
  for (const [rawLon, rawLat] of coords) {
    const lonRad = (rawLon * Math.PI) / 180.0;
    const latRad = (rawLat * Math.PI) / 180.0;
    const cosLat = Math.cos(latRad);
    sumX += cosLat * Math.cos(lonRad);
    sumY += cosLat * Math.sin(lonRad);
    sumZ += Math.sin(latRad);
  }
  const n = coords.length;
  const x = sumX / n;
  const y = sumY / n;
  const z = sumZ / n;
  const hyp = Math.hypot(x, y);
  if (hyp > 1e-6 || Math.abs(z) > 1e-6) {
    const avgLon = (Math.atan2(y, x) * 180.0) / Math.PI;
    const avgLat = (Math.atan2(z, hyp) * 180.0) / Math.PI;
    return [avgLon, avgLat];
  }

  let sumLon = 0;
  let sumLat = 0;
  for (const [rawLon, lat] of coords) {
    const lon = crossesAntimeridian && rawLon < 0 ? rawLon + 360 : rawLon;
    sumLon += lon;
    sumLat += lat;
  }
  let avgLon = sumLon / coords.length;
  if (avgLon > 180) avgLon -= 360;
  if (avgLon < -180) avgLon += 360;
  const avgLat = sumLat / coords.length;
  return [avgLon, avgLat];
}

/** A single country polygon feature in `countries.geojson`. */
export interface WorldCountryFeature extends GeoFeatureLike {
  readonly type: 'Feature';
  readonly id?: string | number;
  readonly properties: GeoFeatureProperties;
  readonly geometry: unknown;
}

/** GeoJSON FeatureCollection of world country boundaries. */
export interface WorldCountriesGeoJson {
  readonly type: 'FeatureCollection';
  readonly features: readonly WorldCountryFeature[];
}

/** @visibleForTesting */
export function isWorldCountriesGeoJson(
  value: unknown,
): value is WorldCountriesGeoJson {
  return (
    typeof value === 'object' &&
    value !== null &&
    'type' in value &&
    value.type === 'FeatureCollection' &&
    'features' in value &&
    Array.isArray(value.features)
  );
}

/**
 * URL of a year's published country or FIR boundaries.
 * @visibleForTesting
 */
export function boundaryGeoJsonUrl(
  kind: 'countries' | 'firs',
  year: number,
): string {
  const cleanYear = requireYear(year, 'boundaryGeoJsonUrl');
  return `/${kind}/${encodeURIComponent(cleanYear)}.json`;
}

/**
 * Creates a singleton boundary GeoJSON loader for `/{kind}/{year}.json`.
 *
 * The dashboard loads boundaries only for the most recent published year and
 * draws every year with them, so switching years never re-downloads the same
 * geometries. Each run publishes its own copy of the boundaries; runs derived
 * from different boundary files are still drawn with the most recent year's.
 * Once any call resolves, later calls reuse the cached `FeatureCollection`.
 * Failed requests clear `inFlightPromise` so subsequent calls can retry.
 */
function createBoundaryGeoJsonLoader<T>(
  kind: 'countries' | 'firs',
  isValid: (value: unknown) => value is T,
): {
  readonly getCached: () => T | null;
  readonly load: (year: number) => Promise<T | null>;
  readonly resetForTesting: () => void;
} {
  let cached: T | null = null;
  let inFlightPromise: Promise<T | null> | null = null;
  return {
    getCached: () => cached,
    load: (year: number): Promise<T | null> => {
      const url = boundaryGeoJsonUrl(kind, year);
      if (inFlightPromise) return inFlightPromise;
      inFlightPromise = fetch(url)
        .then((response) => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return response.json();
        })
        .then((data: unknown) => {
          if (!isValid(data)) {
            throw new Error('response is not a GeoJSON FeatureCollection');
          }
          cached = data;
          return data;
        })
        .catch((err: unknown) => {
          console.warn(`Failed to load ${kind}.json:`, err);
          inFlightPromise = null;
          return null;
        });
      return inFlightPromise;
    },
    resetForTesting: (): void => {
      cached = null;
      inFlightPromise = null;
    },
  };
}

const worldCountriesGeoJsonLoader = createBoundaryGeoJsonLoader(
  'countries',
  isWorldCountriesGeoJson,
);

const worldFirsGeoJsonLoader = createBoundaryGeoJsonLoader(
  'firs',
  isFirFeatureCollection,
);

/**
 * Country boundaries, or null until `loadWorldCountriesGeoJson` resolves.
 * @visibleForTesting
 */
export function getWorldCountriesGeoJson(): WorldCountriesGeoJson | null {
  return worldCountriesGeoJsonLoader.getCached();
}

/**
 * Fetches the country boundaries published with `year`'s run.
 *
 * The dashboard passes the most recent published year. The request is made at
 * most once per page load and its result is shared by every caller across
 * years, so every year is drawn with the most recent year's boundaries. A
 * failed request is not cached, so a later call can retry it.
 * @visibleForTesting
 */
export function loadWorldCountriesGeoJson(
  year: number,
): Promise<WorldCountriesGeoJson | null> {
  return worldCountriesGeoJsonLoader.load(year);
}

/**
 * Clears the memoized country boundaries so tests can re-fetch.
 * @visibleForTesting
 */
export function resetWorldCountriesGeoJsonForTesting(): void {
  worldCountriesGeoJsonLoader.resetForTesting();
}

/**
 * FIR boundaries, or null until `loadWorldFirsGeoJson` resolves.
 * @visibleForTesting
 */
export function getWorldFirsGeoJson(): FirFeatureCollection | null {
  return worldFirsGeoJsonLoader.getCached();
}

/**
 * Fetches the FIR boundaries published with `year`'s run.
 *
 * The dashboard passes the most recent published year. The request is made at
 * most once per page load and its result is shared by every caller across
 * years, so every year is drawn with the most recent year's boundaries. A
 * failed request is not cached, so a later call can retry it.
 * @visibleForTesting
 */
export function loadWorldFirsGeoJson(
  year: number,
): Promise<FirFeatureCollection | null> {
  return worldFirsGeoJsonLoader.load(year);
}

/**
 * Clears the memoized FIR boundaries so tests can re-fetch.
 * @visibleForTesting
 */
export function resetWorldFirsGeoJsonForTesting(): void {
  worldFirsGeoJsonLoader.resetForTesting();
}

/** Browser `window` with the CDN `maplibregl` global and an optional `d3`. */
export interface DashboardWindow extends Window {
  readonly maplibregl?: MapLibreGl;
  readonly d3?: typeof d3;
}

/** `gl.PIXEL_PACK_BUFFER`. */
const GL_PIXEL_PACK_BUFFER = 0x88eb;
/** `gl.STREAM_READ`. */
const GL_STREAM_READ = 0x88e1;
/** `gl.STREAM_DRAW`. */
const GL_STREAM_DRAW = 0x88e0;

/** Minimal `bufferData` surface the workaround wraps. */
export interface BufferDataTarget {
  bufferData(
    target: number,
    srcDataOrSize: unknown,
    usage?: number,
    ...rest: unknown[]
  ): void;
}

/** Contexts already patched by `installPixelPackStreamReadWorkaround`. */
const PATCHED_PIXEL_PACK_CONTEXTS = new WeakSet<object>();

/**
 * Rewrites `bufferData(PIXEL_PACK_BUFFER, ..., STREAM_READ)` to `STREAM_DRAW`
 * on a single WebGL2 context. This keeps ANGLE from creating and discarding
 * speculative CPU shadow copies during MapLibre's globe error-measurement
 * readbacks.
 *
 * The override is installed as an own property of `gl` rather than on
 * `WebGL2RenderingContext.prototype`, so it only affects the map's context and
 * not every WebGL2 context on the page (e.g. the offscreen grid renderer).
 * Idempotent per context.
 * @visibleForTesting
 */
export function installPixelPackStreamReadWorkaround(
  gl: BufferDataTarget,
): void {
  if (PATCHED_PIXEL_PACK_CONTEXTS.has(gl)) return;
  const originalBufferData = gl.bufferData.bind(gl);
  gl.bufferData = (
    target: number,
    srcDataOrSize: unknown,
    usage?: number,
    ...rest: unknown[]
  ): void => {
    const effectiveUsage =
      target === GL_PIXEL_PACK_BUFFER && usage === GL_STREAM_READ
        ? GL_STREAM_DRAW
        : usage;
    originalBufferData(target, srcDataOrSize, effectiveUsage, ...rest);
  };
  PATCHED_PIXEL_PACK_CONTEXTS.add(gl);
}

/** Two-digit month codes ('01'..'12') used in monthly grid keys. */
export const MONTH_CODES = [
  '01',
  '02',
  '03',
  '04',
  '05',
  '06',
  '07',
  '08',
  '09',
  '10',
  '11',
  '12',
];

/** Two-digit hour codes ('00'..'23') used in hourly grid keys. */
export const HOUR_CODES = Array.from({length: 24}, (_, i) =>
  String(i).padStart(2, '0'),
);

/** @visibleForTesting */
export function computeCurrentGridPrefix(
  mode: 'impact' | 'density',
  play: 'all' | 'monthly' | 'hourly',
  monthIndex: number,
  hourIndex: number,
): string {
  const metricStr = mode === 'impact' ? 'impact' : 'density';
  if (play === 'monthly') {
    const code = MONTH_CODES[monthIndex] ?? '01';
    return `monthly_global_${metricStr}_grid_${code}`;
  }
  if (play === 'hourly') {
    const code = HOUR_CODES[hourIndex] ?? '00';
    return `hourly_global_${metricStr}_grid_${code}`;
  }
  return `total_global_${metricStr}_grid`;
}

/** @visibleForTesting */
export function computeIsSequenceFullyLoaded(
  cache: ReadonlyMap<string, unknown>,
  year: number,
  play: 'all' | 'monthly' | 'hourly',
  mode: 'impact' | 'density',
): boolean {
  const hasAnyLod = (baseKey: string) =>
    [2, 1, 0].some((l) => cache.has(`${baseKey}_lod${l}`));

  if (play === 'all') {
    const key = `${year}_total_global_${mode}_grid_${mode}`;
    return hasAnyLod(key);
  }
  if (play === 'monthly') {
    return MONTH_CODES.every((mCode) =>
      hasAnyLod(`${year}_monthly_global_${mode}_grid_${mCode}_${mode}`),
    );
  }
  if (play === 'hourly') {
    return HOUR_CODES.every((hCode) =>
      hasAnyLod(`${year}_hourly_global_${mode}_grid_${hCode}_${mode}`),
    );
  }
  return false;
}

/** @visibleForTesting */
export function computeHighestCachedLod(
  cache: ReadonlyMap<string, unknown>,
  year: number,
  prefix: string,
  mode: 'impact' | 'density',
): number {
  for (const lod of [2, 1]) {
    if (cache.has(`${year}_${prefix}_${mode}_lod${lod}`)) {
      return lod;
    }
  }
  return 0;
}

/**
 * Maximum number of grids fetched and rendered concurrently while preloading
 * a monthly or hourly sequence. An LOD-2 grid decodes to ~52 MB of float16
 * data plus GPU/canvas readback buffers, so six parallel LOD-2 workers can
 * spike peak memory by several hundred MB; LOD 2 is therefore limited to two
 * workers while the much smaller LOD 0/1 grids keep six.
 * @visibleForTesting
 */
export function getGridPreloadConcurrency(lod: number): number {
  return lod >= 2 ? 2 : 6;
}

/** @visibleForTesting */
export function extractFirId(feat: FirFeature | null | undefined): string {
  return String(feat?.properties?.fir_id || '').trim();
}

/** @visibleForTesting */
export function extractFirIcao(feat: FirFeature | null | undefined): string {
  return String(feat?.properties?.fir_icao || '').trim();
}

/** @visibleForTesting */
export function extractFirName(feat: FirFeature | null | undefined): string {
  return String(feat?.properties?.firname || '').trim();
}

/** @visibleForTesting */
export function extractFirArea(feat: FirFeature | null | undefined): number {
  const raw = feat?.properties?.areasqkm;
  return typeof raw === 'number' && Number.isFinite(raw) ? raw : 1e9;
}

/** @visibleForTesting */
export function matchFirFeatures(
  allFeatures: readonly FirFeature[],
  firIds?: readonly string[] | null,
  firType: string = FIR_TYPE_DOMESTIC,
): FirFeature[] {
  const targetIds = new Set((firIds ?? []).map((id) => id.toUpperCase()));
  if (targetIds.size === 0) return [];
  return allFeatures
    .filter((feat: FirFeature) =>
      targetIds.has(extractFirId(feat).toUpperCase()),
    )
    .map((feat: FirFeature) => ({
      ...feat,
      properties: {...feat.properties, firType},
    }));
}

/**
 * Returns true when `feat`'s `fir_id` matches `selectedFirId`
 * case-insensitively.
 */
export function isMatchingFirSelection(
  feat: FirFeature | null | undefined,
  selectedFirId: string | null | undefined,
): boolean {
  if (!feat) return false;
  const featId = extractFirId(feat).toUpperCase();
  const targetId = String(selectedFirId ?? '')
    .toUpperCase()
    .trim();
  return Boolean(featId && targetId && featId === targetId);
}

/** @visibleForTesting */
export function selectClickedFirFeature(
  features: readonly FirFeature[],
  currentSelectedFirId?: string | null,
): {
  firId: string;
  icao: string;
  firName: string | null;
  feature: FirFeature;
} | null {
  if (!features || features.length === 0) return null;
  const validFeatures = features.filter((f) => Boolean(extractFirId(f)));
  if (validFeatures.length === 0) return null;
  const sorted = [...validFeatures].sort(
    (a, b) => extractFirArea(a) - extractFirArea(b),
  );
  const nonSelectedCandidates = sorted.filter(
    (f) => !isMatchingFirSelection(f, currentSelectedFirId),
  );
  const pool =
    nonSelectedCandidates.length > 0 ? nonSelectedCandidates : sorted;
  const chosen = pool[0];
  const firId = extractFirId(chosen);
  const icao = extractFirIcao(chosen);
  const firName = extractFirName(chosen) || null;
  return {firId, icao: icao || firName || '', firName, feature: chosen};
}

/**
 * How the initial country selection resolves against the country list:
 * - `pending`: the list is still loading, keep the current selection;
 * - `resolved`: select `country`;
 * - `unavailable`: the list failed, is empty, or ranks no country, so no
 *   country can be chosen yet and the selection becomes empty.
 */
export type InitialCountryResolution =
  | {readonly kind: 'pending'}
  | {readonly kind: 'resolved'; readonly country: string}
  | {readonly kind: 'unavailable'};

/**
 * Resolves the initial country selection once the backend's country list is
 * known. A locale guess (an ISO alpha-2 code, or '' when there is none) is kept
 * if `countries` contains it; otherwise the country with the best over-country
 * CO2e rank for `gwp` in `countries` is chosen (`topCountryByCo2eRank`).
 * @visibleForTesting
 */
export function resolveInitialCountry(
  guess: string,
  countries: readonly CountryOption[],
  listStatus: CountryListStatus,
  gwp: GwpHorizon = 'GWP50',
): InitialCountryResolution {
  if (listStatus === 'loading') return {kind: 'pending'};
  const guessUpper = guess.trim().toUpperCase();
  if (
    guessUpper &&
    countries.some((c) => c.isoA2.trim().toUpperCase() === guessUpper)
  ) {
    return {kind: 'resolved', country: guessUpper};
  }
  const top = topCountryByCo2eRank(countries, gwp);
  return top
    ? {kind: 'resolved', country: resolveCountryIdentifier(top.isoA2, top.name)}
    : {kind: 'unavailable'};
}

/**
 * Resolves the pending initial country (see `resolveInitialCountry`) against
 * `getAllCountries()`, the picker's country list. That list is the latest
 * published year's `/countries` response (it is only replaced by the latest
 * requested year), so the CO2e ranks come from the latest published year, not
 * necessarily the selected one. Updates `pendingCountryRef`: cleared once a
 * country is chosen, and emptied when none can be chosen yet so that a later
 * list can still choose the top-ranked country.
 *
 * Returns null when no initial country is pending, i.e. it was already chosen
 * or the user picked a country.
 * @visibleForTesting
 */
export function takeInitialCountryResolution(
  pendingCountryRef: {current: string | null},
  gwp: GwpHorizon = 'GWP50',
): InitialCountryResolution | null {
  const pendingCountry = pendingCountryRef.current;
  if (pendingCountry === null) return null;
  const resolution = resolveInitialCountry(
    pendingCountry,
    getAllCountries(),
    getCountryListStatus(),
    gwp,
  );
  if (resolution.kind === 'resolved') {
    pendingCountryRef.current = null;
  } else if (resolution.kind === 'unavailable') {
    pendingCountryRef.current = '';
  }
  return resolution;
}

/** What the dashboard shell renders for a given year-list state. */
export type YearGate =
  | {readonly kind: 'loading'}
  | {readonly kind: 'error'}
  | {readonly kind: 'empty'}
  | {readonly kind: 'ready'; readonly initialYear: number};

/**
 * Decides whether the dashboard can render. Every data request names a year and
 * the backend has no default, so nothing renders until `/years` answered with
 * at least one year; the latest one becomes the initial selection.
 * @visibleForTesting
 */
export function resolveYearGate(
  status: YearListStatus,
  years: readonly number[],
): YearGate {
  if (status === 'loading') return {kind: 'loading'};
  const initialYear = getLatestYear(years);
  if (initialYear !== null) {
    return {kind: 'ready', initialYear};
  }
  return status === 'error' ? {kind: 'error'} : {kind: 'empty'};
}

/** Text shown while `/years` is loading. */
export const YEARS_LOADING_TEXT = 'Loading available years…';
/** Text shown when `/years` failed. */
export const YEARS_ERROR_TEXT =
  'Could not load the available years from the server.';
/** Text shown when the server has no published year. */
export const YEARS_EMPTY_TEXT = 'No data has been published yet.';
/**
 * Text shown when the MapLibre GL JS library fails to load.
 * @visibleForTesting
 */
export const MAPLIBRE_NETWORK_ERROR_TEXT =
  "The MapLibre GL JS library couldn't be loaded. Please check your " +
  'network connection and reload the page.';
