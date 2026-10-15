/**
 * @fileoverview Top-level Regional Contrails Dashboard view orchestrating the
 * interactive MapLibre GL JS globe/map, raster grid overlays, boundary
 * highlights, regional filter toolbar, and contrail metrics panel.
 * @jsx React.createElement
 * @jsxFrag React.Fragment
 */

import * as d3 from 'd3';
import {Box, CircularProgress, Typography} from 'material-ui';
import * as React from 'react';
import {ContrailMetricsPanel} from './ContrailMetricsPanel';
import {
  fetchAvailableYearsFromBackend,
  findCountryFeaturesForSelection,
  getAvailableYears,
  getBoundedCacheEntry,
  getCountryFirBoundaryInfo,
  getFirRecordsForCountry,
  getYearListStatus,
  guessUserCountry,
  GwpHorizon,
  resolveCountryIsoFromFeature,
  resolveCountryNameFromFeature,
  resolveCountrySelectionFromFeature,
  setBoundedCacheEntry,
  subscribeToCountryCache,
  YearListStatus,
} from './CountryMetadata';
import {
  abortAndClearInFlightGrids,
  CachedGridItem,
  cacheOrRevokeGridResult,
  clearGridCache,
  computeAntimeridianSafeCentroid,
  computeCurrentGridPrefix,
  computeHighestCachedLod,
  computeIsSequenceFullyLoaded,
  DashboardWindow,
  decodeGzipBufferIfNeeded,
  deduplicateInFlightGridRequest,
  detachGridLayerForContextRestore,
  FirFeature,
  FirFeatureCollection,
  getCachedGridEntry,
  getGridPreloadConcurrency,
  getWorldCountriesGeoJson,
  getWorldFirsGeoJson,
  GRID_LAYER_ID,
  GRID_SOURCE_ID,
  GridAvailability,
  HOUR_CODES,
  InFlightGridEntry,
  InitialCountryResolution,
  installPixelPackStreamReadWorkaround,
  isAbortMapError,
  isMatchingFirSelection,
  loadWorldCountriesGeoJson,
  loadWorldFirsGeoJson,
  mapHasLayer,
  mapHasSource,
  MapInstance,
  MapLayerMouseEvent,
  MapLibreGl,
  matchFirFeatures,
  MAX_GRID_CACHE_ENTRIES,
  MONTH_CODES,
  normalizeGridAvailability,
  normalizeRasterMetadata,
  parseGridMetaHeader,
  removeGridLayerAndSource,
  resolveYearGate,
  revokeBlobUrlIfSafe,
  selectClickedFirFeature,
  takeInitialCountryResolution,
  WorldCountriesGeoJson,
  YEARS_EMPTY_TEXT,
  YEARS_ERROR_TEXT,
  YEARS_LOADING_TEXT,
} from './dashboard_geo_utils';
import {
  getLodForZoom,
  GridRenderParams,
  GwpTonnesPerMj,
  parseGridValueMax,
  parseGwpTonnesPerMj,
  RasterMetadata,
  renderGridOnCpu,
  sameGwpTonnesPerMj,
  sharedGridGpuRenderer,
} from './geo_raster_utils';
import {
  GridDimensions,
  GridLoadProgress,
  GridStatus,
  MapControlsOverlay,
  MapLibreStatus,
  MapModeToggleBar,
  OverlayMode,
  PlaybackMode,
  reconcilePlaybackModeWithAvailability,
} from './MapControlsOverlay';
import {FIR_TYPE_DOMESTIC, FIR_TYPE_INTERNATIONAL} from './metric_formatters';
import {
  FilterState,
  reconcileSelectedYear,
  RegionalFilterControls,
} from './RegionalFilterDrawer';

/**
 * Applies an initial-country `resolution` and snaps the selected year to one
 * the backend lists. The resolution only replaces `pendingCountry`, the
 * initial selection still awaiting the country list, so it never overrides a
 * country the user picked. Returns `prev` itself if nothing changes.
 */
function reconcileFiltersWithCountryCache(
  prev: FilterState,
  pendingCountry: string | null,
  resolution: InitialCountryResolution | null,
): FilterState {
  let next = prev;
  if (
    resolution !== null &&
    resolution.kind !== 'pending' &&
    prev.country === pendingCountry
  ) {
    const country = resolution.kind === 'resolved' ? resolution.country : '';
    if (country !== prev.country) {
      next = {...next, country};
    }
  }
  const reconciledYear = reconcileSelectedYear(next.year, getAvailableYears());
  if (reconciledYear !== null && reconciledYear !== next.year) {
    next = {...next, year: reconciledYear};
  }
  return next;
}

/** MapLibre layers whose features select an FIR of the current country. */
const CLICKABLE_FIR_LAYER_IDS: readonly string[] = [
  'selected-fir-dom-highlight-fill',
  'selected-fir-int-highlight-fill',
  'selected-firs-dom-fill-click',
  'selected-firs-int-fill-click',
  'selected-firs-dom-outline',
  'selected-firs-int-outline',
];

/**
 * Shows a pointer cursor when the mouse enters `layerId`. Over the country fill
 * layer this happens only if the hovered feature resolves to a country.
 */
function showPointerCursorOnLayerEnter(
  map: MapInstance,
  layerId: string,
  event: MapLayerMouseEvent,
): void {
  const canvas = map.getCanvas();
  if (!canvas) return;
  if (layerId !== 'world-countries-fill') {
    canvas.style.cursor = 'pointer';
    return;
  }
  const feat = event.features?.[0];
  if (
    resolveCountryIsoFromFeature(feat) ||
    resolveCountryNameFromFeature(feat)
  ) {
    canvas.style.cursor = 'pointer';
  }
}

/** Restores the default map cursor. */
function clearMapCursor(map: MapInstance): void {
  const canvas = map.getCanvas();
  if (canvas) {
    canvas.style.cursor = '';
  }
}

/**
 * Returns the first country resolved from `features` that differs
 * (case-insensitively) from `currentCountry`, or null if there is none.
 */
function findNewCountrySelection(
  features: readonly FirFeature[],
  currentCountry: string | null | undefined,
): string | null {
  for (const feat of features) {
    const resolvedCountry = resolveCountrySelectionFromFeature(feat);
    if (
      resolvedCountry &&
      resolvedCountry.toUpperCase() !== (currentCountry || '').toUpperCase()
    ) {
      return resolvedCountry;
    }
  }
  return null;
}

/**
 * Reads the binary response body of `response` and decompresses it when the
 * payload begins with the gzip magic header (`0x1f 0x8b`).
 */
async function decodeResponseBuffer(response: Response): Promise<ArrayBuffer> {
  return decodeGzipBufferIfNeeded(await response.arrayBuffer());
}

/** Props for `AppErrorBoundary`. */
interface AppErrorBoundaryProps {
  readonly children: React.ReactNode;
}

/** Internal error state for `AppErrorBoundary`. */
interface AppErrorBoundaryState {
  readonly hasError: boolean;
  readonly error: Error | null;
}

/** Catches rendering errors in the metrics panel and shows a fallback alert. */
class AppErrorBoundary extends React.Component<
  AppErrorBoundaryProps,
  AppErrorBoundaryState
> {
  /** Initializes the error boundary with no captured error. */
  constructor(props: AppErrorBoundaryProps) {
    super(props);
    this.state = {hasError: false, error: null};
  }
  /**
   * Captures `error` in component state so the next render shows the fallback.
   */
  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return {hasError: true, error};
  }
  override componentDidCatch(error: Error, errorInfo: React.ErrorInfo): void {
    console.error('RegionalDashboard UI Error:', error, errorInfo);
  }
  override render(): React.ReactNode {
    if (this.state.hasError) {
      return (
        <Box
          padding="16px"
          margin="12px 0"
          style={{
            backgroundColor: '#fce8e6',
            border: '1px solid #ea4335',
            borderRadius: '8px',
            color: '#c5221f',
          }}>
          <Typography variant="subtitle1" style={{fontWeight: 600}}>
            Error rendering metrics panel
          </Typography>
          <pre style={{fontSize: '11px', whiteSpace: 'pre-wrap'}}>
            {this.state.error?.message || String(this.state.error)}
          </pre>
        </Box>
      );
    }
    return this.props.children;
  }
}

/**
 * Fetches the available years from `/years` first and renders the dashboard
 * layout only once a year is known, or displays a loading/error status view,
 * so no downstream request is ever issued without a year.
 */
export const RegionalDashboard: React.FC = () => {
  const [yearState, setYearState] = React.useState<{
    readonly status: YearListStatus;
    readonly years: readonly number[];
  }>(() => ({status: getYearListStatus(), years: getAvailableYears()}));

  const loadYears = React.useCallback(() => {
    // Fire-and-forget: the fetcher never rejects (failures set the year list
    // status to `error`), and the result arrives via subscribeToCountryCache.
    void fetchAvailableYearsFromBackend();
  }, []);

  React.useEffect(() => {
    const unsubscribe = subscribeToCountryCache(() => {
      const status = getYearListStatus();
      const years = getAvailableYears();
      setYearState((prev) =>
        prev.status === status && prev.years === years ? prev : {status, years},
      );
    });
    if (getYearListStatus() !== 'ready') {
      loadYears();
    }
    return unsubscribe;
  }, [loadYears]);

  const gate = resolveYearGate(yearState.status, yearState.years);
  if (gate.kind === 'ready') {
    return <RegionalDashboardView initialYear={gate.initialYear} />;
  }
  const message =
    gate.kind === 'loading'
      ? YEARS_LOADING_TEXT
      : gate.kind === 'error'
        ? YEARS_ERROR_TEXT
        : YEARS_EMPTY_TEXT;
  return (
    <Box
      display="flex"
      flexDirection="column"
      alignItems="center"
      justifyContent="center"
      style={{
        height: '100vh',
        gap: '12px',
        fontFamily: 'Roboto, Helvetica, Arial, sans-serif',
      }}>
      {gate.kind === 'loading' && (
        <CircularProgress size={28} style={{color: '#1a73e8'}} />
      )}
      <Typography style={{fontSize: '15px', color: '#5f6368'}}>
        {message}
      </Typography>
      {gate.kind === 'error' && (
        <button type="button" onClick={loadYears}>
          Retry
        </button>
      )}
    </Box>
  );
};

/** Props for `RegionalDashboardView`. */
interface RegionalDashboardViewProps {
  /** Initially selected year; one of the years returned by `/years`. */
  readonly initialYear: number;
}

/** Renders the interactive map, filter controls, and metrics dashboard view. */
const RegionalDashboardView: React.FC<RegionalDashboardViewProps> = ({
  initialYear,
}) => {
  // The initial country still awaiting the backend's country list: the
  // locale-based guess (e.g. `de-CH` -> CH), or '' (no country known yet) when
  // the locale has no region. Once the list arrives, a listed guess is kept and
  // anything else is replaced by the country with the best over-country CO2e
  // rank in the latest published year's country list (see
  // `takeInitialCountryResolution`). Null once resolved or after the user
  // picked a country, so the fallback never re-runs on year or GWP changes.
  const pendingCountryRef = React.useRef<string | null>(null);
  const [filters, setFilters] = React.useState<FilterState>(() => {
    const initialCountry = guessUserCountry() ?? '';
    pendingCountryRef.current = initialCountry;
    return {
      country: initialCountry,
      year: initialYear,
      gwp: 'GWP50',
    };
  });
  const gwpRef = React.useRef<GwpHorizon>(filters.gwp ?? 'GWP50');
  React.useEffect(() => {
    gwpRef.current = filters.gwp ?? 'GWP50';
  }, [filters.gwp]);

  const [countryCacheTick, setCountryCacheTick] = React.useState<number>(0);
  React.useEffect(() => {
    return subscribeToCountryCache(() => {
      setCountryCacheTick((prev) => prev + 1);
      const pendingCountry = pendingCountryRef.current;
      const resolution = takeInitialCountryResolution(
        pendingCountryRef,
        gwpRef.current,
      );
      setFilters((prev) =>
        reconcileFiltersWithCountryCache(prev, pendingCountry, resolution),
      );
    });
  }, []);

  const [selectedFirId, setSelectedFirId] = React.useState<string | null>(
    () =>
      getFirRecordsForCountry(filters.country, filters.year, filters.gwp)[0]
        ?.firId ?? null,
  );

  const selectedFirIdRef = React.useRef<string | null>(selectedFirId);
  selectedFirIdRef.current = selectedFirId;

  const currentCountryRef = React.useRef(filters.country);
  currentCountryRef.current = filters.country;
  const prevCountryForFirSyncRef = React.useRef(filters.country);
  React.useEffect(() => {
    const countryChanged = prevCountryForFirSyncRef.current !== filters.country;
    prevCountryForFirSyncRef.current = filters.country;
    currentCountryRef.current = filters.country;
    const countryFirs = getFirRecordsForCountry(
      filters.country,
      filters.year,
      filters.gwp,
    );
    if (filters.country && countryFirs.length === 0) {
      return;
    }
    const explicitMatch = selectedFirId
      ? countryFirs.find(
          (f) => f.firId?.toUpperCase() === selectedFirId.toUpperCase(),
        )
      : undefined;

    if (!explicitMatch && !countryChanged && selectedFirId) {
      return;
    }

    const matched = explicitMatch ?? countryFirs[0] ?? null;
    const nextFirId = matched?.firId ?? null;
    setSelectedFirId((prev) => (prev === nextFirId ? prev : nextFirId));
  }, [
    filters.country,
    filters.year,
    filters.gwp,
    countryCacheTick,
    selectedFirId,
  ]);

  const [overlayMode, setOverlayMode] = React.useState<OverlayMode>('impact');
  const [playbackMode, setPlaybackMode] = React.useState<PlaybackMode>('all');
  const [isPlaying, setIsPlaying] = React.useState<boolean>(true);
  const [isBufferingSequence, setIsBufferingSequence] =
    React.useState<boolean>(false);
  const [gridLoadProgress, setGridLoadProgress] =
    React.useState<GridLoadProgress>({
      loaded: 0,
      total: 1,
    });
  const [activeGridDimensions, setActiveGridDimensions] =
    React.useState<GridDimensions>({
      width: 1200,
      height: 600,
    });
  // MJ -> t CO2e factors from the displayed grid's metadata. Undefined for runs
  // published before grid_meta carried them; the legend then stays in MJ.
  const [gridGwpFactors, setGridGwpFactors] = React.useState<
    GwpTonnesPerMj | undefined
  >(undefined);
  // Colour-scale maximum of the displayed grid, read from its metadata. The
  // legend ticks are derived from it; undefined until a grid is applied.
  const [gridValueMax, setGridValueMax] = React.useState<number | undefined>(
    undefined,
  );
  const [currentMonthIdx, setCurrentMonthIdx] = React.useState<number>(0);
  const [currentHourIdx, setCurrentHourIdx] = React.useState<number>(0);

  const [gridAvailability, setGridAvailability] =
    React.useState<GridAvailability>({
      hasTotal: true,
      hasMonthly: false,
      hasHourly: false,
      monthsCount: 0,
      hoursCount: 0,
    });

  // Bumped on every MapLibre WebGL context restore so the grid effect re-adds
  // the raster layer whose texture was lost with the old context.
  const [webglRestoreCount, setWebglRestoreCount] = React.useState(0);
  const [mapLibreStatus, setMapLibreStatus] =
    React.useState<MapLibreStatus>('loading');
  const [gridStatus, setGridStatus] = React.useState<GridStatus>('loading');

  const handleSelectFir = React.useCallback((firId: string | null) => {
    setSelectedFirId(firId);
  }, []);

  const handleFilterChange = React.useCallback((newFilters: FilterState) => {
    if (newFilters.country !== pendingCountryRef.current) {
      pendingCountryRef.current = null;
    }
    if (
      currentCountryRef.current.toUpperCase() !==
      newFilters.country.toUpperCase()
    ) {
      currentCountryRef.current = newFilters.country;
      setSelectedFirId(null);
    }
    setFilters(newFilters);
  }, []);

  const handleSelectCountry = React.useCallback((countryName: string) => {
    pendingCountryRef.current = null;
    if (currentCountryRef.current.toUpperCase() !== countryName.toUpperCase()) {
      currentCountryRef.current = countryName;
      setSelectedFirId(null);
    }
    setFilters((prev) => ({...prev, country: countryName}));
  }, []);

  const [globalFirData, setGlobalFirData] =
    React.useState<FirFeatureCollection | null>(
      () =>
        getWorldFirsGeoJson() ?? {
          type: 'FeatureCollection',
          features: [],
        },
    );

  // Every year is drawn with the most recent year's FIR boundaries (the initial
  // year is the most recent one), so they are downloaded once per page load.
  // A failed request is retried on the next year change.
  React.useEffect(() => {
    let active = true;
    void loadWorldFirsGeoJson(initialYear).then((data) => {
      if (active && data) {
        setGlobalFirData(data);
      }
    });
    return () => {
      active = false;
    };
  }, [initialYear, filters.year]);

  const [worldCountriesGeoJson, setWorldCountriesGeoJson] =
    React.useState<WorldCountriesGeoJson | null>(getWorldCountriesGeoJson);

  // Every year is drawn with the most recent year's country boundaries, loaded
  // once per page load; a failed request is retried on the next year change.
  React.useEffect(() => {
    let active = true;
    void loadWorldCountriesGeoJson(initialYear).then((data) => {
      if (active && data) {
        setWorldCountriesGeoJson(data);
      }
    });
    return () => {
      active = false;
    };
  }, [initialYear, filters.year]);

  // Fetch grid availability from backend when year changes
  React.useEffect(() => {
    const abortController = new AbortController();
    const fetchGridAvailability = async (): Promise<void> => {
      try {
        const res = await fetch(
          `/grid/${encodeURIComponent(filters.year)}/availability`,
          {signal: abortController.signal},
        );
        const raw: unknown = res.ok ? await res.json() : null;
        const normalized = normalizeGridAvailability(raw);
        if (!abortController.signal.aborted && normalized) {
          setGridAvailability(normalized);
          setPlaybackMode((prev) =>
            reconcilePlaybackModeWithAvailability(prev, normalized),
          );
        }
      } catch (err: unknown) {
        if (!abortController.signal.aborted) {
          console.warn('Failed to fetch grid availability:', err);
        }
      }
    };
    void fetchGridAvailability();
    return () => {
      abortController.abort();
    };
  }, [filters.year]);

  // Animation timer for monthly / hourly playback (waits for buffering to
  // complete)
  React.useEffect(() => {
    if (playbackMode === 'all' || !isPlaying || isBufferingSequence) return;

    const delay = playbackMode === 'hourly' ? 600 : 1000;
    const timer = setInterval(() => {
      if (playbackMode === 'monthly') {
        setCurrentMonthIdx((prev) => (prev + 1) % 12);
      } else if (playbackMode === 'hourly') {
        setCurrentHourIdx((prev) => (prev + 1) % 24);
      }
    }, delay);

    return () => {
      clearInterval(timer);
    };
  }, [playbackMode, isPlaying, isBufferingSequence]);

  // Keyboard navigation shortcuts: Left/Right arrows step & pause, Spacebar
  // toggles Play/Pause
  React.useEffect(() => {
    if (playbackMode === 'all') return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (
        e.target instanceof HTMLElement &&
        (e.target.tagName === 'INPUT' ||
          e.target.tagName === 'TEXTAREA' ||
          e.target.tagName === 'SELECT' ||
          e.target.isContentEditable)
      ) {
        return;
      }

      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        setIsPlaying(false);
        if (playbackMode === 'monthly') {
          setCurrentMonthIdx((prev) => (prev - 1 + 12) % 12);
        } else if (playbackMode === 'hourly') {
          setCurrentHourIdx((prev) => (prev - 1 + 24) % 24);
        }
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        setIsPlaying(false);
        if (playbackMode === 'monthly') {
          setCurrentMonthIdx((prev) => (prev + 1) % 12);
        } else if (playbackMode === 'hourly') {
          setCurrentHourIdx((prev) => (prev + 1) % 24);
        }
      } else if (e.key === ' ' || e.code === 'Space') {
        e.preventDefault();
        setIsPlaying((prev) => !prev);
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [playbackMode]);

  const mapContainerRef = React.useRef<HTMLDivElement | null>(null);
  const mapInstanceRef = React.useRef<MapInstance | null>(null);
  const gridCacheRef = React.useRef<Map<string, CachedGridItem>>(new Map());
  // Caches parsed RasterMetadata by (year, prefix, lod). Both 'impact' and
  // 'density' overlay modes share identical grid metadata for a given
  // (year, prefix, lod), so consulting metaCacheRef avoids re-parsing the
  // X-Grid-Meta header or re-fetching /meta when toggling overlay modes.
  const metaCacheRef = React.useRef<Map<string, RasterMetadata>>(new Map());
  const inFlightGridPromisesRef = React.useRef<Map<string, InFlightGridEntry>>(
    new Map(),
  );
  const isMountedRef = React.useRef<boolean>(true);

  // Helper function to fetch & render Float16 grid into cache with in-flight
  // deduplication
  const fetchAndRenderGridData = React.useCallback(
    (
      year: number,
      prefix: string,
      mode: 'impact' | 'density',
      lod = 2,
      signal?: AbortSignal,
    ): Promise<CachedGridItem | null> => {
      if (signal?.aborted) {
        return Promise.resolve(null);
      }

      const cacheKey = `${year}_${prefix}_${mode}_lod${lod}`;
      const metaCacheKey = `${year}_${prefix}_lod${lod}`;
      const cached = getCachedGridEntry(gridCacheRef.current, cacheKey);
      if (cached !== undefined) {
        getBoundedCacheEntry(metaCacheRef.current, metaCacheKey);
        return Promise.resolve(cached);
      }

      return deduplicateInFlightGridRequest(
        inFlightGridPromisesRef.current,
        cacheKey,
        async (inFlightSignal): Promise<CachedGridItem | null> => {
          const searchParams = new URLSearchParams({
            lod: String(lod),
          });
          const query = `?${searchParams.toString()}`;
          let meta: RasterMetadata;
          let buffer: ArrayBuffer;
          try {
            const binRes = await fetch(
              `/grid/${encodeURIComponent(year)}/` +
                `${encodeURIComponent(prefix)}/binary${query}`,
              {
                signal: inFlightSignal,
                headers: {'Accept-Encoding': 'gzip'},
              },
            );
            if (!binRes.ok || inFlightSignal.aborted) return null;

            const cachedMeta = getBoundedCacheEntry(
              metaCacheRef.current,
              metaCacheKey,
            );
            const resolvedMeta =
              cachedMeta ??
              parseGridMetaHeader(
                binRes.headers?.get?.('X-Grid-Meta') ??
                  binRes.headers?.get?.('x-grid-meta'),
              );

            if (!resolvedMeta) {
              const [metaRes, binBuffer] = await Promise.all([
                fetch(
                  `/grid/${encodeURIComponent(year)}/` +
                    `${encodeURIComponent(prefix)}/meta${query}`,
                  {signal: inFlightSignal},
                ),
                decodeResponseBuffer(binRes),
              ]);
              if (!metaRes.ok || inFlightSignal.aborted) return null;
              const metaJson: unknown = await metaRes.json();
              const normalizedMeta = normalizeRasterMetadata(metaJson);
              if (inFlightSignal.aborted || !normalizedMeta) {
                return null;
              }
              meta = normalizedMeta;
              buffer = binBuffer;
            } else {
              const binBuffer = await decodeResponseBuffer(binRes);
              if (inFlightSignal.aborted) return null;
              meta = resolvedMeta;
              buffer = binBuffer;
            }
            setBoundedCacheEntry(
              metaCacheRef.current,
              metaCacheKey,
              meta,
              MAX_GRID_CACHE_ENTRIES,
            );
          } catch (networkError: unknown) {
            if (!inFlightSignal.aborted) {
              console.warn('Network error fetching grid data:', networkError);
            }
            return null;
          }

          // Only the current grid format is supported: float16 values holding
          // signed_log10 of the ref-scaled density/impact.
          if (
            meta.dtype !== 'float16' ||
            meta.encoding !== 'signed_log10_ref_scaled'
          ) {
            console.error(
              `Unsupported grid format for ${year}/${prefix} ` +
                `(dtype=${meta.dtype}, encoding=${meta.encoding}); ` +
                're-run the derive pipeline for this year.',
            );
            return null;
          }
          let float16Data: Uint16Array;
          try {
            const expectedPixels = meta.width * meta.height;
            if (
              buffer.byteLength % 2 !== 0 ||
              buffer.byteLength / 2 < expectedPixels
            ) {
              return null;
            }
            float16Data = new Uint16Array(buffer, 0, expectedPixels);
          } catch (decodeErr: unknown) {
            console.warn('Malformed binary grid buffer:', decodeErr);
            return null;
          }
          // The colour scale comes only from the published metadata. A run
          // without it cannot be coloured correctly, so show no data instead.
          const valueMax = parseGridValueMax(meta, mode);
          if (valueMax === undefined) {
            console.error(
              `Grid metadata for ${year}/${prefix} lacks a valid ` +
                `value_max_ref_scaled.${mode}; ` +
                're-run the derive pipeline for this year.',
            );
            return null;
          }

          const minLatBound = meta.bbox?.minLat ?? -90;
          const maxLatBound = meta.bbox?.maxLat ?? 90;
          const latRange = maxLatBound - minLatBound || 180;

          const renderParams: GridRenderParams = {
            meta,
            float16Data,
            mode,
            valueMax,
            minLatBound,
            latRange,
            signal: inFlightSignal,
          };

          let dataUrl = await sharedGridGpuRenderer.render(renderParams);
          if (!dataUrl && !inFlightSignal.aborted) {
            dataUrl = await renderGridOnCpu(renderParams);
          }
          if (!dataUrl) return null;
          if (inFlightSignal.aborted) {
            revokeBlobUrlIfSafe(dataUrl);
            return null;
          }

          const rawBbox = meta.bbox ?? {
            minLon: -180,
            maxLon: 180,
            minLat: -90,
            maxLat: 90,
          };
          const minLon = Math.max(-180, rawBbox.minLon ?? -180);
          const maxLon = Math.min(180, rawBbox.maxLon ?? 180);
          const minLat = Math.max(-85.051128, rawBbox.minLat ?? -90);
          const maxLat = Math.min(85.051128, rawBbox.maxLat ?? 90);

          const coords: Array<[number, number]> = [
            [minLon, maxLat],
            [maxLon, maxLat],
            [maxLon, minLat],
            [minLon, minLat],
          ];

          const result: CachedGridItem = {dataUrl, coords, meta, valueMax};
          return cacheOrRevokeGridResult(
            gridCacheRef.current,
            cacheKey,
            result,
            isMountedRef.current,
            MAX_GRID_CACHE_ENTRIES,
            lastAppliedGridUrlRef.current,
          );
        },
        signal,
      );
    },
    [],
  );

  const lastAppliedGridUrlRef = React.useRef<string | null>(null);
  const latestRequestedGridKeyRef = React.useRef<string>('');
  const currentUpdateGridAbortRef = React.useRef<AbortController | null>(null);

  const revokeUrlIfUnreferenced = React.useCallback((url: string | null) => {
    if (!url) return;
    for (const entry of gridCacheRef.current.values()) {
      if (entry.dataUrl === url) {
        return;
      }
    }
    revokeBlobUrlIfSafe(url, lastAppliedGridUrlRef.current);
  }, []);

  React.useEffect(() => {
    return () => {
      currentUpdateGridAbortRef.current?.abort();
      const prevUrl = lastAppliedGridUrlRef.current;
      lastAppliedGridUrlRef.current = null;
      revokeUrlIfUnreferenced(prevUrl);
    };
  }, [revokeUrlIfUnreferenced]);

  const lastRenderedLodRef = React.useRef<number>(0);
  const inFlightLodRef = React.useRef<number>(0);

  interface UpdateGridLayerOptions {
    readonly map: MapInstance | null;
    readonly year: number;
    readonly prefix: string;
    readonly mode: 'impact' | 'density';
  }

  // Helper function to update the MapLibre grid image layer for the active LOD.
  const updateGridLayer = React.useCallback(
    async (options: UpdateGridLayerOptions) => {
      const {map, year, prefix, mode} = options;

      if (!map || !map.style) return;
      const currentZoom =
        typeof map.getZoom === 'function' ? map.getZoom() : 1.5;
      const zoomLod = getLodForZoom(currentZoom);
      const cachedLod = computeHighestCachedLod(
        gridCacheRef.current,
        year,
        prefix,
        mode,
      );
      // Retain finer resolution only if already cached for this grid, avoiding
      // bounding-box resets on evicted LODs
      const activeLod = Math.max(zoomLod, cachedLod);
      inFlightLodRef.current = activeLod;

      const cacheKey = `${year}_${prefix}_${mode}_lod${activeLod}`;
      latestRequestedGridKeyRef.current = cacheKey;

      if (currentUpdateGridAbortRef.current) {
        currentUpdateGridAbortRef.current.abort();
      }
      const abortController = new AbortController();
      currentUpdateGridAbortRef.current = abortController;

      const safeRemoveGridLayer = () => {
        const prevUrl = lastAppliedGridUrlRef.current;
        lastAppliedGridUrlRef.current = null;
        revokeUrlIfUnreferenced(prevUrl);
        removeGridLayerAndSource(map);
      };

      const applyGridResultToMap = (
        gridItem: CachedGridItem,
        renderedLod: number,
      ) => {
        if (!map || typeof map.getSource !== 'function' || !map.style) return;
        lastRenderedLodRef.current = renderedLod;
        const prevUrl = lastAppliedGridUrlRef.current;
        if (mapHasSource(map, GRID_SOURCE_ID)) {
          const src = map.getSource(GRID_SOURCE_ID);
          if (src && src.updateImage && prevUrl !== gridItem.dataUrl) {
            lastAppliedGridUrlRef.current = gridItem.dataUrl;
            src.updateImage({
              'url': gridItem.dataUrl,
              'coordinates': gridItem.coords,
            });
            revokeUrlIfUnreferenced(prevUrl);
          }
        } else {
          lastAppliedGridUrlRef.current = gridItem.dataUrl;
          revokeUrlIfUnreferenced(prevUrl);
          map.addSource(GRID_SOURCE_ID, {
            'type': 'image',
            'url': gridItem.dataUrl,
            'coordinates': gridItem.coords,
          });
          const beforeLayerId = mapHasLayer(map, 'selected-country-outline')
            ? 'selected-country-outline'
            : undefined;
          map.addLayer(
            {
              'id': GRID_LAYER_ID,
              'type': 'raster',
              'source': GRID_SOURCE_ID,
              'paint': {
                'raster-opacity': 0.82,
                'raster-fade-duration': 0,
              },
            },
            beforeLayerId,
          );
        }
        if (gridItem.meta?.width && gridItem.meta?.height) {
          const nextW = gridItem.meta.width;
          const nextH = gridItem.meta.height;
          setActiveGridDimensions((prev) =>
            prev.width === nextW && prev.height === nextH
              ? prev
              : {width: nextW, height: nextH},
          );
        }
        // Tracked per applied grid (not per request) so the legend always
        // describes the dataset currently on the map.
        setGridValueMax(gridItem.valueMax);
        const nextFactors = parseGwpTonnesPerMj(gridItem.meta);
        setGridGwpFactors((prev) =>
          sameGwpTonnesPerMj(prev, nextFactors) ? prev : nextFactors,
        );
        setGridLoadProgress((prev) =>
          prev.loaded === 1 && prev.total === 1 ? prev : {loaded: 1, total: 1},
        );
        setGridStatus('ready');
      };

      // If not in cache, apply any cached coarser fallback LOD to avoid a blank
      // map, and always set gridStatus to 'loading' so the spinner indicates
      // the finer LOD is loading.
      let fallbackGrid: CachedGridItem | undefined;
      let fallbackLod = 0;
      if (!gridCacheRef.current.has(cacheKey)) {
        for (const lodCandidate of [2, 1, 0]) {
          const candidateKey = `${year}_${prefix}_${mode}_lod${lodCandidate}`;
          const cachedCandidate = getCachedGridEntry(
            gridCacheRef.current,
            candidateKey,
          );
          if (cachedCandidate) {
            fallbackGrid = cachedCandidate;
            fallbackLod = lodCandidate;
            break;
          }
        }
        if (fallbackGrid) {
          applyGridResultToMap(fallbackGrid, fallbackLod);
        }
        if (!(fallbackGrid && playbackMode !== 'all' && isPlaying)) {
          setGridStatus('loading');
          setGridLoadProgress((prev) =>
            prev.loaded === 0 && prev.total === 1
              ? prev
              : {loaded: 0, total: 1},
          );
        }
      }

      try {
        const fetchSignal =
          fallbackGrid && playbackMode !== 'all' && isPlaying
            ? undefined
            : abortController.signal;
        const gridResult = await fetchAndRenderGridData(
          year,
          prefix,
          mode,
          activeLod,
          fetchSignal,
        );
        if (
          latestRequestedGridKeyRef.current !== cacheKey ||
          abortController.signal.aborted
        ) {
          inFlightLodRef.current = lastRenderedLodRef.current;
          return;
        }
        if (!gridResult) {
          inFlightLodRef.current = lastRenderedLodRef.current;
          if (fallbackGrid) {
            setGridStatus('ready');
          } else {
            setGridStatus('no_data');
            safeRemoveGridLayer();
          }
          return;
        }

        inFlightLodRef.current = activeLod;
        applyGridResultToMap(gridResult, activeLod);
      } catch (err) {
        if (abortController.signal.aborted) {
          inFlightLodRef.current = lastRenderedLodRef.current;
          return;
        }
        inFlightLodRef.current = lastRenderedLodRef.current;
        console.warn('Failed to load grid data for year:', year, err);
        if (fallbackGrid) {
          setGridStatus('ready');
        } else {
          setGridStatus('no_data');
        }
      }
    },
    [fetchAndRenderGridData, revokeUrlIfUnreferenced, playbackMode, isPlaying],
  );

  const activeCountryFirInfo = React.useMemo(
    () =>
      filters.country
        ? getCountryFirBoundaryInfo(filters.country, filters.year)
        : undefined,
    [filters.country, filters.year, countryCacheTick],
  );
  const activeDomFirIds = activeCountryFirInfo?.firDomFirId;
  const activeIntFirIds = activeCountryFirInfo?.firIntFirId;

  const getSelectedFirGeoJson = React.useCallback(
    (
      countryNameOrIso: string,
      year: number,
    ): {
      readonly domestic: FirFeatureCollection;
      readonly international: FirFeatureCollection;
    } => {
      const empty = {
        domestic: {type: 'FeatureCollection' as const, features: []},
        international: {type: 'FeatureCollection' as const, features: []},
      };
      if (!countryNameOrIso) return empty;
      const boundaryInfo = getCountryFirBoundaryInfo(countryNameOrIso, year);
      if (!boundaryInfo) return empty;
      const allFeatures = globalFirData?.features || [];

      return {
        domestic: {
          type: 'FeatureCollection',
          features: matchFirFeatures(
            allFeatures,
            boundaryInfo.firDomFirId,
            FIR_TYPE_DOMESTIC,
          ),
        },
        international: {
          type: 'FeatureCollection',
          features: matchFirFeatures(
            allFeatures,
            boundaryInfo.firIntFirId,
            FIR_TYPE_INTERNATIONAL,
          ),
        },
      };
    },
    [globalFirData],
  );

  const selectedFirGeoJsons = React.useMemo(
    () => getSelectedFirGeoJson(filters.country, filters.year),
    [
      getSelectedFirGeoJson,
      filters.country,
      filters.year,
      activeDomFirIds,
      activeIntFirIds,
    ],
  );

  const getSelectedIndividualFirGeoJson = React.useCallback(
    (
      targetFirId: string | null | undefined,
      countryNameOrIso: string,
      year: number,
    ): FirFeatureCollection => {
      const cleanId = String(targetFirId ?? '')
        .toUpperCase()
        .trim();
      if (!cleanId || !countryNameOrIso) {
        return {type: 'FeatureCollection', features: []};
      }
      const allFirsObj =
        countryNameOrIso === filters.country && year === filters.year
          ? selectedFirGeoJsons
          : getSelectedFirGeoJson(countryNameOrIso, year);
      const allFeats = [
        ...allFirsObj.domestic.features,
        ...allFirsObj.international.features,
      ];
      const matched = allFeats.filter((feat: FirFeature) =>
        isMatchingFirSelection(feat, cleanId),
      );
      return {type: 'FeatureCollection', features: matched};
    },
    [getSelectedFirGeoJson, selectedFirGeoJsons, filters.country, filters.year],
  );

  // Initialize MapLibre GL JS with the inline basemap and GeoJSON boundary
  // layers.
  React.useEffect(() => {
    isMountedRef.current = true;
    if (!mapContainerRef.current) return;

    let checkInterval: ReturnType<typeof setInterval> | null = null;
    let attempts = 0;
    // Removes the canvas context listeners on unmount.
    const canvasListenersController = new AbortController();

    const initMapLibre = (maplibregl: MapLibreGl) => {
      if (mapInstanceRef.current) {
        return;
      }

      setMapLibreStatus('loading');

      const INLINE_MAP_STYLE: Record<string, unknown> = {
        'version': 8,
        'projection': {'type': 'globe'},
        'sources': {},
        'layers': [
          {
            'id': 'background',
            'type': 'background',
            'paint': {
              'background-color': '#f8fafc',
            },
          },
        ],
      };

      const map = new maplibregl.Map({
        'container': mapContainerRef.current,
        'style': INLINE_MAP_STYLE,
        'projection': {'type': 'globe'},
        'center': [15, 35],
        'zoom': 1.4,
        'attributionControl': false,
      });

      mapInstanceRef.current = map;
      map.addControl(
        new maplibregl.NavigationControl({'showCompass': false}),
        'top-right',
      );

      const mapCanvas = map.getCanvas();
      // getContext returns the context MapLibre already created, or null if it
      // fell back to WebGL1. The globe readbacks the workaround targets only
      // start once rendering begins, after this point.
      const mapGl = mapCanvas?.getContext('webgl2');
      if (mapGl) {
        installPixelPackStreamReadWorkaround(mapGl);
      }
      if (mapCanvas) {
        const listenerOptions = {signal: canvasListenersController.signal};
        mapCanvas.addEventListener(
          'webglcontextlost',
          (e: Event) => {
            e.preventDefault();
            console.warn(
              'MapLibre WebGL context lost; prevented default to allow ' +
                'restoration',
            );
          },
          listenerOptions,
        );
        mapCanvas.addEventListener(
          'webglcontextrestored',
          () => {
            detachGridLayerForContextRestore(map);
            lastAppliedGridUrlRef.current = null;
            setWebglRestoreCount((count) => count + 1);
          },
          listenerOptions,
        );
      }

      const handleMapClick = (event: MapLayerMouseEvent) => {
        // 1. First check if the user clicked an FIR within the currently
        //    selected country (including overseas/international FIR polygons
        //    that may overlap another territory).
        const firFeatures = map.queryRenderedFeatures(event.point, {
          'layers': CLICKABLE_FIR_LAYER_IDS,
        });
        const clickedFir =
          firFeatures && firFeatures.length > 0
            ? selectClickedFirFeature(firFeatures, selectedFirIdRef.current)
            : null;
        if (clickedFir && clickedFir.firId) {
          setSelectedFirId(clickedFir.firId);
          return;
        }

        // 2. Otherwise, check if the user clicked on a DIFFERENT country on the
        //    map to switch the overall dashboard view.
        const countryFeatures = map.queryRenderedFeatures(event.point, {
          'layers': ['world-countries-fill'],
        });
        const resolvedCountry = findNewCountrySelection(
          countryFeatures || [],
          currentCountryRef.current,
        );
        if (resolvedCountry) {
          pendingCountryRef.current = null;
          setFilters((prev) => ({...prev, country: resolvedCountry}));
          setSelectedFirId(null);
        }
      };

      map.on('load', () => {
        setMapLibreStatus('ready');

        // Filled by the country-boundaries effect once /countries/{year}.json
        // loads.
        const worldGeoJson = getWorldCountriesGeoJson() || {
          type: 'FeatureCollection',
          features: [],
        };

        // 1. Add World Countries Source & Layers (Landmasses & Borders)
        map.addSource('world-countries-src', {
          'type': 'geojson',
          'data': worldGeoJson,
        });
        map.addLayer({
          'id': 'world-countries-fill',
          'type': 'fill',
          'source': 'world-countries-src',
          'paint': {
            'fill-color': '#e2e8f0',
            'fill-opacity': 0.75,
          },
        });
        map.addLayer({
          'id': 'world-countries-border',
          'type': 'line',
          'source': 'world-countries-src',
          'layout': {'line-join': 'round', 'line-cap': 'round'},
          'paint': {
            'line-color': '#70757a',
            'line-width': 1.0,
          },
        });

        // 2. Add Selected Country Outline Source & Layer
        map.addSource('selected-country-src', {
          'type': 'geojson',
          'data': {'type': 'FeatureCollection', 'features': []},
        });
        map.addLayer({
          'id': 'selected-country-outline',
          'type': 'line',
          'source': 'selected-country-src',
          'layout': {'line-join': 'round', 'line-cap': 'round'},
          'paint': {
            'line-color': '#1a73e8',
            'line-width': 2.5,
          },
        });

        // 2b. Add Domestic Controlled FIRs (dashed Cyan, no fill)
        map.addSource('selected-firs-dom-src', {
          'type': 'geojson',
          'data': {'type': 'FeatureCollection', 'features': []},
        });
        map.addLayer({
          'id': 'selected-firs-dom-outline',
          'type': 'line',
          'source': 'selected-firs-dom-src',
          'layout': {'line-join': 'round', 'line-cap': 'round'},
          'paint': {
            'line-color': '#00bcd4',
            'line-width': 3.0,
            'line-offset': 3,
            'line-dasharray': [2.5, 1.5],
          },
        });

        // 2c. Add International Controlled FIRs (dashed Purple, no fill)
        map.addSource('selected-firs-int-src', {
          'type': 'geojson',
          'data': {'type': 'FeatureCollection', 'features': []},
        });
        map.addLayer({
          'id': 'selected-firs-int-outline',
          'type': 'line',
          'source': 'selected-firs-int-src',
          'layout': {'line-join': 'round', 'line-cap': 'round'},
          'paint': {
            'line-color': '#ab47bc',
            'line-width': 3.0,
            'line-offset': 3,
            'line-dasharray': [2.5, 1.5],
          },
        });

        map.addLayer({
          'id': 'selected-firs-dom-fill-click',
          'type': 'fill',
          'source': 'selected-firs-dom-src',
          'paint': {
            'fill-color': '#00bcd4',
            'fill-opacity': 0.06,
          },
        });
        map.addLayer({
          'id': 'selected-firs-int-fill-click',
          'type': 'fill',
          'source': 'selected-firs-int-src',
          'paint': {
            'fill-color': '#ab47bc',
            'fill-opacity': 0.06,
          },
        });

        // 2d. Add Highlight Fill Layer for the individually selected FIR in
        // the Controlled FIRs section (semi-transparent fill matching outline)
        map.addSource('selected-fir-highlight-src', {
          'type': 'geojson',
          'data': {'type': 'FeatureCollection', 'features': []},
        });
        map.addLayer({
          'id': 'selected-fir-dom-highlight-fill',
          'type': 'fill',
          'source': 'selected-fir-highlight-src',
          'filter': ['==', ['get', 'firType'], FIR_TYPE_DOMESTIC],
          'paint': {
            'fill-color': '#00bcd4',
            'fill-opacity': 0.45,
          },
        });
        map.addLayer({
          'id': 'selected-fir-int-highlight-fill',
          'type': 'fill',
          'source': 'selected-fir-highlight-src',
          'filter': ['==', ['get', 'firType'], FIR_TYPE_INTERNATIONAL],
          'paint': {
            'fill-color': '#ab47bc',
            'fill-opacity': 0.45,
          },
        });

        for (const layerId of [
          ...CLICKABLE_FIR_LAYER_IDS,
          'world-countries-fill',
        ]) {
          map.on('mouseenter', layerId, (event: MapLayerMouseEvent) => {
            showPointerCursorOnLayerEnter(map, layerId, event);
          });
          map.on('mouseleave', layerId, () => {
            clearMapCursor(map);
          });
        }

        map.on('click', handleMapClick);

        // Note: the initial grid layer load is triggered by the
        // `mapLibreStatus === 'ready'` React.useEffect.
      });

      map.on('error', (err: unknown) => {
        if (isAbortMapError(err)) return;
        console.warn('MapLibre event notice:', err);
      });
    };

    const tryLoad = (): boolean => {
      if (mapInstanceRef.current) {
        if (checkInterval) clearInterval(checkInterval);
        return true;
      }
      const maplibregl =
        typeof window !== 'undefined'
          ? (window as DashboardWindow).maplibregl
          : undefined;
      if (maplibregl) {
        if (checkInterval) clearInterval(checkInterval);
        initMapLibre(maplibregl);
        return true;
      } else {
        attempts++;
        if (attempts > 8) {
          if (checkInterval) clearInterval(checkInterval);
          setMapLibreStatus('error');
        }
        return false;
      }
    };

    if (!tryLoad()) {
      checkInterval = setInterval(tryLoad, 300);
    }

    return () => {
      isMountedRef.current = false;
      if (checkInterval) clearInterval(checkInterval);
      canvasListenersController.abort();
      currentUpdateGridAbortRef.current?.abort();
      abortAndClearInFlightGrids(inFlightGridPromisesRef.current);
      clearGridCache(gridCacheRef.current);
      if (mapInstanceRef.current) {
        try {
          mapInstanceRef.current.remove();
        } catch (e: unknown) {
          console.warn('Failed to clean up map instance:', e);
        }
        mapInstanceRef.current = null;
      }
    };
  }, []);

  // Push the country boundaries into the map once both are available.
  React.useEffect(() => {
    if (
      !mapInstanceRef.current ||
      mapLibreStatus !== 'ready' ||
      !worldCountriesGeoJson
    ) {
      return;
    }
    mapInstanceRef.current
      .getSource('world-countries-src')
      ?.setData?.(worldCountriesGeoJson);
  }, [mapLibreStatus, worldCountriesGeoJson]);

  const selectedCountryFeatures = React.useMemo(
    () =>
      worldCountriesGeoJson
        ? findCountryFeaturesForSelection(
            worldCountriesGeoJson.features,
            filters.country,
          )
        : [],
    [worldCountriesGeoJson, filters.country],
  );

  // Handle Selected Country Outline updates only when country or map status
  // changes
  React.useEffect(() => {
    if (!mapInstanceRef.current || mapLibreStatus !== 'ready') return;
    const map = mapInstanceRef.current;
    const selectedCountryGeoJson = {
      type: 'FeatureCollection',
      features: selectedCountryFeatures,
    };

    if (
      map &&
      typeof map.getSource === 'function' &&
      map.getSource('selected-country-src')
    ) {
      map.getSource('selected-country-src')?.setData?.(selectedCountryGeoJson);
    }
  }, [mapLibreStatus, selectedCountryFeatures]);

  // Handle Controlled FIRs Outline updates when FIR polygons change
  React.useEffect(() => {
    if (!mapInstanceRef.current || mapLibreStatus !== 'ready') return;
    const map = mapInstanceRef.current;
    if (map && typeof map.getSource === 'function') {
      if (map.getSource('selected-firs-dom-src')) {
        map
          .getSource('selected-firs-dom-src')
          ?.setData?.(selectedFirGeoJsons.domestic);
      }
      if (map.getSource('selected-firs-int-src')) {
        map
          .getSource('selected-firs-int-src')
          ?.setData?.(selectedFirGeoJsons.international);
      }
    }
  }, [mapLibreStatus, selectedFirGeoJsons]);

  // Handle Individual Selected FIR Highlight updates when FIR chip or map
  // selection changes
  React.useEffect(() => {
    if (!mapInstanceRef.current || mapLibreStatus !== 'ready') return;
    const map = mapInstanceRef.current;
    if (
      map &&
      typeof map.getSource === 'function' &&
      map.getSource('selected-fir-highlight-src')
    ) {
      const highlightGeoJson = getSelectedIndividualFirGeoJson(
        selectedFirId,
        filters.country,
        filters.year,
      );
      map.getSource('selected-fir-highlight-src')?.setData?.(highlightGeoJson);
    }
  }, [
    selectedFirId,
    filters.country,
    filters.year,
    mapLibreStatus,
    getSelectedIndividualFirGeoJson,
  ]);

  // Preload all months or hours in parallel BEFORE playing the sequence
  React.useEffect(() => {
    if (playbackMode === 'all') {
      setIsBufferingSequence(false);
      return;
    }

    const year = filters.year;
    const mode = overlayMode;
    const codes = playbackMode === 'monthly' ? MONTH_CODES : HOUR_CODES;
    const totalGrids = codes.length;

    if (
      computeIsSequenceFullyLoaded(
        gridCacheRef.current,
        year,
        playbackMode,
        mode,
      )
    ) {
      setGridLoadProgress({loaded: totalGrids, total: totalGrids});
      setIsBufferingSequence(false);
      return;
    }

    let isMounted = true;
    const abortController = new AbortController();
    setIsBufferingSequence(true);
    setGridStatus('loading');

    const currentZoom =
      mapInstanceRef.current &&
      typeof mapInstanceRef.current.getZoom === 'function'
        ? mapInstanceRef.current.getZoom()
        : 1.5;
    const targetLod = getLodForZoom(currentZoom);

    let loadedCount = codes.filter((code) => {
      const p =
        playbackMode === 'monthly'
          ? `monthly_global_${mode}_grid_${code}`
          : `hourly_global_${mode}_grid_${code}`;
      return (
        gridCacheRef.current.has(`${year}_${p}_${mode}_lod${targetLod}`) ||
        [2, 1, 0].some((l) =>
          gridCacheRef.current.has(`${year}_${p}_${mode}_lod${l}`),
        )
      );
    }).length;
    setGridLoadProgress({loaded: loadedCount, total: totalGrids});

    const loadAllInParallel = async () => {
      try {
        const concurrencyLimit = getGridPreloadConcurrency(targetLod);
        let nextIdx = 0;
        const runWorker = async (): Promise<void> => {
          while (nextIdx < codes.length && !abortController.signal.aborted) {
            const code = codes[nextIdx++];
            const p =
              playbackMode === 'monthly'
                ? `monthly_global_${mode}_grid_${code}`
                : `hourly_global_${mode}_grid_${code}`;
            const key = `${year}_${p}_${mode}_lod${targetLod}`;
            const alreadyCached = gridCacheRef.current.has(key);
            await fetchAndRenderGridData(
              year,
              p,
              mode,
              targetLod,
              abortController.signal,
            );
            if (
              !alreadyCached &&
              isMounted &&
              !abortController.signal.aborted
            ) {
              loadedCount += 1;
              setGridLoadProgress({
                loaded: Math.min(totalGrids, loadedCount),
                total: totalGrids,
              });
            }
          }
        };
        await Promise.all(
          Array.from({length: Math.min(concurrencyLimit, codes.length)}, () =>
            runWorker(),
          ),
        );
      } catch (err) {
        if (!abortController.signal.aborted) {
          console.warn('Error loading temporal sequence in parallel:', err);
        }
      } finally {
        if (isMounted && !abortController.signal.aborted) {
          setIsBufferingSequence(false);
        }
      }
    };

    // loadAllInParallel catches its own errors; cancellation is via
    // abortController in the cleanup below.
    void loadAllInParallel();

    return () => {
      isMounted = false;
      abortController.abort();
    };
  }, [playbackMode, filters.year, overlayMode, fetchAndRenderGridData]);

  // Handle grid layer updates when selected year, overlay mode, or playback
  // frame changes
  React.useEffect(() => {
    if (!mapInstanceRef.current || mapLibreStatus !== 'ready') return;
    // Wait until all months / hours are loaded in parallel before rendering
    // first frame
    if (isBufferingSequence) return;

    const map = mapInstanceRef.current;
    const prefix = computeCurrentGridPrefix(
      overlayMode,
      playbackMode,
      currentMonthIdx,
      currentHourIdx,
    );
    void updateGridLayer({
      map,
      year: filters.year,
      prefix,
      mode: overlayMode,
    });
  }, [
    filters.year,
    overlayMode,
    playbackMode,
    currentMonthIdx,
    currentHourIdx,
    isBufferingSequence,
    mapLibreStatus,
    updateGridLayer,
    webglRestoreCount,
  ]);

  // Dynamically upgrade grid LOD when map zoom crosses LOD thresholds
  // (0 -> 1 -> 2)
  const checkAndUpgradeLodRef = React.useRef<() => void>(() => {});
  React.useEffect(() => {
    checkAndUpgradeLodRef.current = () => {
      const map = mapInstanceRef.current;
      if (!map || mapLibreStatus !== 'ready' || isBufferingSequence) return;
      const zoom = typeof map.getZoom === 'function' ? map.getZoom() : 1.5;
      const newLod = getLodForZoom(zoom);
      const prefix = computeCurrentGridPrefix(
        overlayMode,
        playbackMode,
        currentMonthIdx,
        currentHourIdx,
      );
      const currentGridLod = Math.max(
        lastRenderedLodRef.current,
        inFlightLodRef.current,
        computeHighestCachedLod(
          gridCacheRef.current,
          filters.year,
          prefix,
          overlayMode,
        ),
      );
      // Only upgrade when zooming in to a finer LOD. Once loaded at a finer
      // resolution, always use that data even when zooming out instead of
      // falling back to coarse view.
      if (newLod > currentGridLod) {
        void updateGridLayer({
          map,
          year: filters.year,
          prefix,
          mode: overlayMode,
        });
      }
    };
  }, [
    mapLibreStatus,
    isBufferingSequence,
    overlayMode,
    playbackMode,
    currentMonthIdx,
    currentHourIdx,
    filters.year,
    updateGridLayer,
  ]);

  React.useEffect(() => {
    if (!mapInstanceRef.current || mapLibreStatus !== 'ready') return;
    const map = mapInstanceRef.current;
    const handleMapLodCheck = () => {
      checkAndUpgradeLodRef.current();
    };
    map.on('zoom', handleMapLodCheck);
    map.on('zoomend', handleMapLodCheck);
    map.on('moveend', handleMapLodCheck);
    return () => {
      try {
        map.off('zoom', handleMapLodCheck);
        map.off('zoomend', handleMapLodCheck);
        map.off('moveend', handleMapLodCheck);
      } catch (e: unknown) {
        console.warn('Failed to remove map event listeners:', e);
      }
    };
  }, [mapLibreStatus]);

  // When in "Yearly data" view and active grid is ready, silently preload the
  // single other "Yearly data" grid (e.g. Flight density) in the background
  React.useEffect(() => {
    if (
      mapLibreStatus !== 'ready' ||
      playbackMode !== 'all' ||
      gridStatus !== 'ready'
    ) {
      return;
    }
    let isMounted = true;
    const abortController = new AbortController();

    const preloadOtherAllGrid = async () => {
      const year = filters.year;
      const otherMode: 'impact' | 'density' =
        overlayMode === 'impact' ? 'density' : 'impact';
      const otherPrefix = `total_global_${otherMode}_grid`;
      const targetLod = lastRenderedLodRef.current;
      const otherKey = `${year}_${otherPrefix}_${otherMode}_lod${targetLod}`;

      if (
        !gridCacheRef.current.has(otherKey) &&
        isMounted &&
        !abortController.signal.aborted
      ) {
        try {
          await fetchAndRenderGridData(
            year,
            otherPrefix,
            otherMode,
            targetLod,
            abortController.signal,
          );
        } catch (err) {
          if (!abortController.signal.aborted) {
            console.warn('Failed to preload other grid:', err);
          }
        }
      }
    };

    const timer = setTimeout(() => {
      void preloadOtherAllGrid();
    }, 50);
    return () => {
      isMounted = false;
      abortController.abort();
      clearTimeout(timer);
    };
  }, [
    mapLibreStatus,
    playbackMode,
    gridStatus,
    filters.year,
    overlayMode,
    fetchAndRenderGridData,
  ]);

  // Handle Camera Transitions (Zoom to Country/FIR) for MapLibre
  const lastFlownTargetKeyRef = React.useRef<string>('');
  React.useEffect(() => {
    if (!mapInstanceRef.current || mapLibreStatus !== 'ready') return;

    const map = mapInstanceRef.current;
    const d3Lib =
      d3 ||
      (typeof window !== 'undefined'
        ? (window as DashboardWindow).d3
        : undefined);
    const hasCountryBounds = selectedCountryFeatures.length > 0;

    if (d3Lib) {
      const domFirFeatures = selectedFirGeoJsons.domestic.features || [];
      const allFirFeatures = [
        ...domFirFeatures,
        ...(selectedFirGeoJsons.international.features || []),
      ];
      const preferredFirFeatures =
        domFirFeatures.length > 0 ? domFirFeatures : allFirFeatures;
      const hasFirBounds = preferredFirFeatures.length > 0;
      const boundsKind = hasFirBounds
        ? 'FIR'
        : hasCountryBounds
          ? 'COUNTRY'
          : 'PENDING';
      const targetKey = `${filters.country}:${boundsKind}`;
      if (lastFlownTargetKeyRef.current === targetKey) {
        return;
      }

      const targetFeatureCollection = {
        type: 'FeatureCollection' as const,
        features: hasFirBounds ? preferredFirFeatures : selectedCountryFeatures,
      };

      if (targetFeatureCollection.features.length > 0) {
        try {
          const centroid = computeAntimeridianSafeCentroid(
            targetFeatureCollection,
            d3Lib.geoCentroid,
          );
          if (centroid) {
            const [centerLon, centerLat] = centroid;
            if (!Number.isNaN(centerLon) && !Number.isNaN(centerLat)) {
              lastFlownTargetKeyRef.current = targetKey;
              const currentZoom =
                typeof map.getZoom === 'function' ? map.getZoom() : 1.55;
              map.flyTo({
                'center': [centerLon, centerLat],
                'zoom': Math.max(1.55, currentZoom),
                'pitch': 0,
                'bearing': 0,
                'duration': 1200,
              });
            }
          }
        } catch (err) {
          console.warn('Could not compute geoCentroid or flyTo:', err);
        }
      }
    }
  }, [
    filters.country,
    mapLibreStatus,
    selectedCountryFeatures,
    selectedFirGeoJsons,
  ]);

  const handleToggleOverlayMode = React.useCallback(() => {
    setOverlayMode((prev) => (prev === 'impact' ? 'density' : 'impact'));
  }, []);

  const handleSelectPlaybackMode = React.useCallback((mode: PlaybackMode) => {
    setPlaybackMode(mode);
    setIsPlaying(mode !== 'all');
    if (mode === 'monthly') {
      setCurrentMonthIdx(0);
    } else if (mode === 'hourly') {
      setCurrentHourIdx(0);
    }
  }, []);

  const handleTogglePlaying = React.useCallback(() => {
    setIsPlaying((prev) => !prev);
  }, []);

  const handleSelectMonthIdx = React.useCallback((idx: number) => {
    setIsPlaying(false);
    setCurrentMonthIdx(idx);
  }, []);

  const handleSelectHourIdx = React.useCallback((idx: number) => {
    setIsPlaying(false);
    setCurrentHourIdx(idx);
  }, []);

  return (
    <Box
      style={{
        backgroundColor: '#f8f9fa',
        height: '100vh',
        width: '100vw',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        boxSizing: 'border-box',
      }}>
      {/* Top Header Row (Fixed at top) */}
      <Box
        style={{
          flex: '0 0 auto',
          width: '100%',
          backgroundColor: '#f8f9fa',
          borderBottom: '1px solid #e0e0e0',
          padding: '14px 28px',
          boxSizing: 'border-box',
          zIndex: 100,
        }}>
        <Box
          display="flex"
          alignItems="center"
          justifyContent="space-between"
          flexWrap="wrap"
          style={{width: '100%', gap: '16px'}}>
          <Typography
            style={{
              fontSize: '24px',
              fontWeight: 500,
              color: '#202124',
              letterSpacing: '-0.3px',
            }}>
            Regional contrail impact
          </Typography>

          <RegionalFilterControls
            filters={filters}
            onChange={handleFilterChange}
          />
        </Box>
      </Box>

      {/* Main Content Area: Fixed Map on Left (fills to bottom), Scrolling
         Metrics on Right */}
      <Box
        style={{
          flex: '1 1 auto',
          minHeight: 0,
          width: '100%',
          display: 'flex',
          overflow: 'hidden',
        }}>
        <Box
          display="flex"
          style={{
            width: '100%',
            height: '100%',
            padding: '16px 0px 20px 28px',
            boxSizing: 'border-box',
            gap: '24px',
            alignItems: 'stretch',
            minHeight: 0,
          }}>
          {/* LEFT COLUMN: Map Controls & WebGL Map */}
          <Box
            style={{
              flex: '0 0 38%',
              maxWidth: '40%',
              minWidth: '380px',
              height: '100%',
              display: 'flex',
              flexDirection: 'column',
              minHeight: 0,
            }}>
            <MapModeToggleBar
              overlayMode={overlayMode}
              onToggleOverlayMode={handleToggleOverlayMode}
              playbackMode={playbackMode}
              gridAvailability={gridAvailability}
              onSelectPlaybackMode={handleSelectPlaybackMode}
            />

            {/* Map Container (WebGL MapLibre container) */}
            <Box
              display="flex"
              justifyContent="center"
              style={{
                position: 'relative',
                width: '100%',
                flex: '1 1 auto',
                minHeight: 0,
                backgroundColor: '#ffffff',
                borderRadius: '12px',
                boxShadow: '0 2px 6px rgba(0,0,0,0.1)',
                border: '1px solid #ebebeb',
                overflow: 'hidden',
              }}>
              <div
                ref={mapContainerRef}
                style={{width: '100%', height: '100%'}}
              />

              <MapControlsOverlay
                playbackMode={playbackMode}
                currentMonthIdx={currentMonthIdx}
                currentHourIdx={currentHourIdx}
                isPlaying={isPlaying}
                isBufferingSequence={isBufferingSequence}
                onTogglePlaying={handleTogglePlaying}
                onSelectMonthIdx={handleSelectMonthIdx}
                onSelectHourIdx={handleSelectHourIdx}
                mapLibreStatus={mapLibreStatus}
                gridStatus={gridStatus}
                gridLoadProgress={gridLoadProgress}
                overlayMode={overlayMode}
                country={filters.country}
                year={filters.year}
                gwp={filters.gwp}
                selectedFirId={selectedFirId}
                gridValueMax={gridValueMax}
                gridGwpFactors={gridGwpFactors}
                activeGridDimensions={activeGridDimensions}
              />
            </Box>
          </Box>

          {/* RIGHT COLUMN: Metrics & Temporal Analysis (Independently
             Scrollable) */}
          <Box
            style={{
              flex: '1 1 60%',
              minWidth: '450px',
              height: '100%',
              overflowY: 'auto',
              minHeight: 0,
              display: 'flex',
              flexDirection: 'column',
              gap: '20px',
              paddingRight: '28px',
            }}>
            {/* Metrics, Explanatory Text and Country Summary */}
            <AppErrorBoundary>
              <ContrailMetricsPanel
                filters={filters}
                onSelectCountry={handleSelectCountry}
                onSelectFir={handleSelectFir}
                selectedFirId={selectedFirId}
              />
            </AppErrorBoundary>

            {/* Extra spacing at the bottom of the scrollable metrics */}
            <Box style={{height: '30px', flexShrink: 0}} />
          </Box>
        </Box>
      </Box>
    </Box>
  );
};
