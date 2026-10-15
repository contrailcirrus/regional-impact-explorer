/**
 * @fileoverview Data caches, backend fetchers, boundary feature resolvers, and
 * metric aggregation helpers for country and Flight Information Region (FIR)
 * contrail statistics.
 */

import {
  computeValueYoY,
  countRankedEntities,
  FIR_TYPE_DOMESTIC,
  FIR_TYPE_INTERNATIONAL,
  formatMwPerSqM,
  formatPercentage,
  formatRankLabel,
  formatRatioPercentOrNotAvailable,
  formatTonnesCo2e,
  formatTonnesOrNotAvailable,
  getNumericRecordField,
  getRecordField,
  NOT_AVAILABLE_LABEL,
  YoYIndicator,
} from './metric_formatters';

/**
 * Options for `fetchBackendJson`.
 */
interface FetchBackendJsonOptions<T> {
  /**
   * Converts the decoded JSON body into the expected shape, returning null when
   * the body is malformed.
   */
  readonly parse: (body: unknown) => T | null;
  /** Human-readable resource name used in log messages, e.g. `'rankings'`. */
  readonly description: string;
  readonly signal?: AbortSignal;
}

/**
 * Fetches a same-origin backend path and returns the parsed body, or null when
 * the request fails, returns a non-2xx status, has a malformed body, or is
 * aborted. Every failure except an abort is logged with `description`.
 */
async function fetchBackendJson<T>(
  path: string,
  options: FetchBackendJsonOptions<T>,
): Promise<T | null> {
  const {parse, description, signal} = options;
  if (signal?.aborted) return null;
  // Same-origin API, so the default credentials mode ('same-origin') applies.
  const init: RequestInit = {};
  if (signal) init.signal = signal;

  let body: unknown;
  try {
    const response = await fetch(path, init);
    if (!response.ok) {
      console.warn(
        `Failed to fetch ${description} from ${path}: HTTP ${response.status}`,
      );
      return null;
    }
    body = await response.json();
  } catch (e: unknown) {
    if (!signal?.aborted) {
      console.warn(`Failed to fetch ${description} from ${path}:`, e);
    }
    return null;
  }

  const parsed = parse(body);
  if (parsed === null) {
    console.warn(`Malformed ${description} response from ${path}`);
  }
  return parsed;
}

/** Adapts a type guard to the `parse` contract of `fetchBackendJson`. */
function parseWith<T>(
  isExpected: (body: unknown) => body is T,
): (body: unknown) => T | null {
  return (body) => (isExpected(body) ? body : null);
}

/**
 * Returns true when `year` is a four-digit integer calendar year (`1000..9999`).
 */
export function isValidYear(year: unknown): year is number {
  return (
    typeof year === 'number' &&
    Number.isInteger(year) &&
    year >= 1000 &&
    year <= 9999
  );
}

/**
 * Years with published data reported by the backend's `/years` endpoint,
 * sorted ascending. Empty until the first successful response: the frontend
 * never assumes a year.
 */
let AVAILABLE_YEARS: readonly number[] = [];

/**
 * Lifecycle of the year list: `loading` until `/years` answers, `ready` once a
 * well-formed response arrived (the list may be empty when nothing is
 * published), `error` when the request failed or the response was malformed.
 */
export type YearListStatus = 'loading' | 'ready' | 'error';
let YEAR_LIST_STATUS: YearListStatus = 'loading';

/** Returns the list of available years (sorted chronologically ascending). */
export function getAvailableYears(): readonly number[] {
  return AVAILABLE_YEARS;
}

/** Returns the current state of the backend-provided year list. */
export function getYearListStatus(): YearListStatus {
  return YEAR_LIST_STATUS;
}

/**
 * Returns the most recent available year in `years` (defaulting to
 * `AVAILABLE_YEARS`), or null while the year list is empty. This is only the
 * initial selection of the year dropdown; every request names its year
 * explicitly.
 */
export function getLatestYear(
  years: readonly number[] = AVAILABLE_YEARS,
): number | null {
  return years.length > 0 ? Math.max(...years) : null;
}

/**
 * Returns true if the given year is available in the active data source.
 */
function isYearAvailable(year: number): boolean {
  return AVAILABLE_YEARS.includes(year);
}

/**
 * Returns the calendar year before `year` when the backend has data for it, or
 * null otherwise. Year-over-year comparisons use this instead of a hardcoded
 * range, and never skip a missing year.
 */
export function previousAvailableYear(year: number): number | null {
  if (!isValidYear(year)) return null;
  const prev = year - 1;
  return isYearAvailable(prev) ? prev : null;
}

/**
 * Returns `year` when it is a valid four-digit integer year, or throws. Every
 * backend request is year-scoped and the backend has no default year, so an
 * invalid or missing year is a caller bug rather than something to paper over.
 */
export function requireYear(
  year: number | null | undefined,
  context: string,
): number {
  if (!isValidYear(year)) {
    throw new Error(
      `${context}: a year is required (choose one returned by /years).`,
    );
  }
  return year;
}

/** Managing country metadata for a Flight Information Region. */
export interface FirManagingCountry {
  readonly name: string;
  readonly isoA2: string;
}

/** Raw FIR record payload returned by `/firs/{year}`. */
export declare interface RawFirRecord {
  readonly fir_id?: string | null;
  readonly firname?: string | null;
  readonly fir_icao?: string | null;
  readonly type?: string | null;
  /** ISO alpha-2 codes of the controlling countries; the first is primary. */
  readonly fir_to_country_iso2s?: readonly string[] | null;
  readonly countries?: readonly unknown[];
  readonly area_sq_km?: number | null;
  readonly segment_count?: number | null;
  readonly contrail_segment_count?: number | null;
  readonly impact_density_sq_km?: number | null;
  readonly mean_rf_mw_per_sq_m?: number | null;
  readonly mean_rf_mw_per_sq_m_rank?: number | null;
  readonly co2e_gwp20_tonnes?: number | null;
  readonly co2e_gwp20_tonnes_rank?: number | null;
  readonly co2e_gwp50_tonnes?: number | null;
  readonly co2e_gwp50_tonnes_rank?: number | null;
  readonly co2e_gwp100_tonnes?: number | null;
  readonly co2e_gwp100_tonnes_rank?: number | null;
  /**
   * Fraction of the all-FIR total (signed, identical for every GWP horizon);
   * null when the total is zero.
   */
  readonly co2e_tonnes_share?: number | null;
  readonly co2e_tonnes?: number | null;
  readonly [property: string]: unknown;
}

/** Normalized FIR record with computed ranks and YoY indicators. */
export interface FirRecord {
  readonly firId: string;
  readonly firName: string;
  readonly firIcao: string;
  readonly type:
    | typeof FIR_TYPE_DOMESTIC
    | typeof FIR_TYPE_INTERNATIONAL
    | string;
  readonly countries?: readonly FirManagingCountry[];
  readonly co2eTonnes: number;
  readonly meanRfMwPerSqM?: number;
  readonly areaSqKm: number;
  /**
   * Global rank by absolute CO2e for the GWP horizon the record was built for.
   */
  readonly co2eRank?: number;
  readonly impactDensityRank?: number;
  readonly absoluteYoY?: YoYIndicator;
  readonly percentOfFirTotal?: string;
}

/**
 * Structured metric bundle for a hero scope card ("Above country",
 * "All outgoing flights", "All controlled FIRs", or an individual FIR).
 */
export interface HeroScopeMetrics {
  readonly absoluteValue: string;
  readonly absoluteYoY?: YoYIndicator;
  readonly absoluteRank?: string;
  readonly absoluteRankNum?: number;
  readonly perCapitaValue?: string;
  readonly perCapitaRank?: string;
  readonly perCapitaRankNum?: number;
  readonly perCapitaVsWorldAvg?: string;
  readonly populationFormatted?: string;
  readonly nationalGhgComparison?: string;
  readonly nationalGhgRank?: string;
  readonly nationalGhgRankNum?: number;
  readonly nationalGhgTotal?: string;
  readonly totalCount?: number;
}

/**
 * Formatted country metrics and associated FIR records for the metrics panel.
 */
export interface CountryData {
  readonly isLoading?: boolean;
  readonly isoA2?: string;
  readonly name?: string;
  readonly aboveCountryHero: HeroScopeMetrics;
  readonly outgoingHero: HeroScopeMetrics;
  readonly firTotalHero: HeroScopeMetrics;
  readonly impactDensityRank: string;
  readonly densityRankNum?: number;
  readonly impactDensity: string;
  /**
   * Percentage of contrail impact attributable to overflights
   * (`(1 - in_country_impact) * 100`), or `'N/A'` (`NOT_AVAILABLE_LABEL`) when
   * `in_country_impact` is missing or `null` (the backend pipeline emits `null`
   * when a territory has zero total contrail warming).
   */
  readonly overflightFraction: string;
  readonly firDensity: string;
  readonly firDensityRank?: string;
  readonly firDensityRankNum?: number;
  readonly firDomImpact: string;
  readonly firIntImpact: string;
  readonly hasInternationalFir: boolean;
  readonly controlledFirRecords: readonly FirRecord[];
  /**
   * FIRs ranked in the year, or null until `/firs/{year}` has loaded for it.
   */
  readonly totalFirCount: number | null;
  /**
   * Countries ranked in the year, or null until `/countries/{year}` has
   * loaded.
   */
  readonly totalCountryCount: number | null;
}

/**
 * Raw country record payload returned by `/countries/{year}` or
 * `/country/{year}/{iso}`.
 */
export declare interface RawCountryRecord {
  readonly name?: string;
  readonly iso_a2?: string;
  // Per-area metrics are null when the country (or FIR scope) has no area.
  readonly mean_rf_mw_per_sq_m?: number | null;
  readonly mean_rf_mw_per_sq_m_rank?: number;
  readonly impact_density_sq_km?: number | null;
  readonly in_country_impact?: number | null;
  readonly ghg_mtco2e?: number;
  readonly population?: number;
  readonly total_flight_impact?: number;
  readonly outgoing_flight_impact?: number;
  readonly outgoing_flight_impact_rank?: number;
  readonly segment_count?: number;
  readonly contrail_segment_count?: number;
  readonly fir_total_segment_count?: number;
  // FIR identity lists: one entry per attributed FIR, index-aligned across
  // `_fir_id`, `_firname` and `_fir_icao`. Empty for a scope without FIRs and
  // null for a country that controls no FIR.
  readonly fir_total_fir_id?: readonly string[] | null;
  readonly fir_total_firname?: readonly string[] | null;
  readonly fir_total_fir_icao?: readonly string[] | null;
  readonly fir_total_mean_rf_mw_per_sq_m?: number | null;
  readonly fir_total_impact_density_sq_km?: number | null;
  readonly fir_total_area_sq_km?: number;
  readonly fir_total_rank?: number;
  readonly fir_total_mean_rf_mw_per_sq_m_rank?: number;
  readonly fir_dom_mean_rf_mw_per_sq_m?: number | null;
  readonly fir_dom_impact_density_sq_km?: number | null;
  readonly fir_dom_fir_id?: readonly string[] | null;
  readonly fir_dom_fir_icao?: readonly string[] | null;
  readonly fir_dom_firname?: readonly string[] | null;
  readonly fir_int_mean_rf_mw_per_sq_m?: number | null;
  readonly fir_int_impact_density_sq_km?: number | null;
  readonly fir_int_fir_id?: readonly string[] | null;
  readonly fir_int_fir_icao?: readonly string[] | null;
  readonly fir_int_firname?: readonly string[] | null;
  readonly co2e_gwp20_tonnes?: number;
  readonly co2e_gwp50_tonnes?: number;
  readonly co2e_gwp100_tonnes?: number;
  readonly co2e_gwp20_tonnes_sq_km?: number | null;
  readonly co2e_gwp50_tonnes_sq_km?: number | null;
  readonly co2e_gwp100_tonnes_sq_km?: number | null;
  // Null when the population / national GHG denominator is unknown (N/A).
  readonly co2e_gwp20_vs_national_ghg?: number | null;
  readonly co2e_gwp50_vs_national_ghg?: number | null;
  readonly co2e_gwp100_vs_national_ghg?: number | null;
  readonly co2e_gwp20_tonnes_per_capita?: number | null;
  readonly co2e_gwp50_tonnes_per_capita?: number | null;
  readonly co2e_gwp100_tonnes_per_capita?: number | null;
  readonly outgoing_flight_co2e_gwp20_tonnes?: number;
  readonly outgoing_flight_co2e_gwp50_tonnes?: number;
  readonly outgoing_flight_co2e_gwp100_tonnes?: number;
  readonly fir_total_co2e_gwp20_tonnes?: number;
  readonly fir_total_co2e_gwp50_tonnes?: number;
  readonly fir_total_co2e_gwp100_tonnes?: number;
  readonly fir_total_co2e_gwp20_tonnes_rank?: number;
  readonly fir_total_co2e_gwp50_tonnes_rank?: number;
  readonly fir_total_co2e_gwp100_tonnes_rank?: number;
  readonly fir_total_co2e_gwp20_tonnes_per_capita?: number | null;
  readonly fir_total_co2e_gwp50_tonnes_per_capita?: number | null;
  readonly fir_total_co2e_gwp100_tonnes_per_capita?: number | null;
  readonly fir_total_co2e_gwp20_vs_national_ghg?: number | null;
  readonly fir_total_co2e_gwp50_vs_national_ghg?: number | null;
  readonly fir_total_co2e_gwp100_vs_national_ghg?: number | null;
  readonly fir_dom_co2e_gwp20_tonnes?: number;
  readonly fir_dom_co2e_gwp50_tonnes?: number;
  readonly fir_dom_co2e_gwp100_tonnes?: number;
  readonly fir_int_co2e_gwp20_tonnes?: number;
  readonly fir_int_co2e_gwp50_tonnes?: number;
  readonly fir_int_co2e_gwp100_tonnes?: number;
  readonly [property: string]: unknown;
}

const FIR_DATA_BY_YEAR_MAP = new Map<number, readonly RawFirRecord[]>();

/**
 * Returns the cached raw FIR rows for `year`, or undefined when the year was
 * never loaded or has been evicted.
 * @visibleForTesting
 */
export function getFirDataForTesting(
  year: number,
): readonly RawFirRecord[] | undefined {
  return FIR_DATA_BY_YEAR_MAP.get(year);
}

/**
 * Writes raw FIR rows for `year` WITHOUT touching the year LRU, the derived
 * FIR indexes, or memo caches, so tests can verify that memo keys alone
 * distinguish cache states. Prefer `setFirDataForTesting` otherwise.
 * @visibleForTesting
 */
export function setFirDataWithoutInvalidationForTesting(
  year: number,
  records: readonly RawFirRecord[],
): void {
  FIR_DATA_BY_YEAR_MAP.set(year, records);
}

/**
 * Replaces the cached raw FIR rows for `year`, invalidates derived FIR
 * indexes, and notifies cache subscribers.
 * @visibleForTesting
 */
export function setFirDataForTesting(
  year: number,
  records: readonly RawFirRecord[],
): void {
  touchCachedYear(year);
  FIR_DATA_BY_YEAR_MAP.set(year, records);
  FIR_ROWS_BY_YEAR_AND_ISO.clear();
  FIR_ROW_BY_YEAR_AND_KEY.clear();
  FIR_RECORDS_MEMO_CACHE.clear();
  notifyCountryCacheListeners();
}

/**
 * Cached map of loaded country records indexed by composite key (e.g.,
 * `${ISO}_${year}`).
 */
const PER_COUNTRY_DATA_MAP = new Map<string, RawCountryRecord>();

/** Memoization cache for `getFirRecordsForCountry`. */
const FIR_RECORDS_MEMO_CACHE = new Map<string, FirRecord[]>();

/** Memoization cache for `fetchRankingsFromBackend`. */
const RANKINGS_RESPONSE_CACHE = new Map<string, RankingsData>();

/** Memoization cache for `fetchTimeSeriesFromBackend`. */
const TIMESERIES_RESPONSE_CACHE = new Map<string, TimeSeriesData>();

const MAX_RESPONSE_CACHE_ENTRIES = 100;

/**
 * Maximum number of years whose country and FIR snapshots stay in memory.
 *
 * The country map, FIR rows, the two derived FIR indexes, and the ranked
 * country totals are all keyed by year. Six years covers the displayed year
 * plus its YoY baseline with headroom; an evicted year is refetched the next
 * time it is selected.
 * @visibleForTesting
 */
export const MAX_CACHED_YEARS = 6;

/** Years with cached snapshots, least recently used first. */
const CACHED_YEARS_LRU = new Set<number>();

/**
 * Marks `year` as most recently used and evicts the least recently used
 * year(s) beyond `MAX_CACHED_YEARS`. Callers must touch a year whenever they
 * store or serve that year's snapshot, so a year that is still on screen is
 * never the eviction victim.
 */
function touchCachedYear(year: number): void {
  CACHED_YEARS_LRU.delete(year);
  CACHED_YEARS_LRU.add(year);
  while (CACHED_YEARS_LRU.size > MAX_CACHED_YEARS) {
    const oldestYear = CACHED_YEARS_LRU.values().next().value;
    if (oldestYear === undefined) break;
    CACHED_YEARS_LRU.delete(oldestYear);
    evictYearFromCaches(oldestYear);
  }
}

/** Drops every year-scoped cache entry for `year`. */
function evictYearFromCaches(year: number): void {
  const keySuffix = `_${year}`;
  for (const key of [...PER_COUNTRY_DATA_MAP.keys()]) {
    if (key.endsWith(keySuffix)) {
      PER_COUNTRY_DATA_MAP.delete(key);
    }
  }
  FIR_DATA_BY_YEAR_MAP.delete(year);
  FIR_ROWS_BY_YEAR_AND_ISO.delete(year);
  FIR_ROW_BY_YEAR_AND_KEY.delete(year);
  TOTAL_COUNTRIES_BY_YEAR.delete(year);
  // Memo keys embed both the year and whether its predecessor is loaded, so a
  // targeted delete would miss entries for `year + 1`; clearing is simpler.
  FIR_RECORDS_MEMO_CACHE.clear();
}

/**
 * Returns the cached years, least recently used first.
 * @visibleForTesting
 */
export function getCachedYearsForTesting(): readonly number[] {
  return [...CACHED_YEARS_LRU];
}

/**
 * Reads an entry from an LRU-bounded `Map`, promoting the key to
 * most-recently-used position on a cache hit.
 */
export function getBoundedCacheEntry<K, V>(
  cache: Map<K, V>,
  key: K,
): V | undefined {
  const existing = cache.get(key);
  if (existing !== undefined) {
    cache.delete(key);
    cache.set(key, existing);
  }
  return existing;
}

/**
 * Inserts or refreshes an entry in an LRU-bounded `Map`, evicting the oldest
 * entries when size exceeds `maxEntries`.
 * @visibleForTesting
 */
export function setBoundedCacheEntry<K, V>(
  cache: Map<K, V>,
  key: K,
  value: V,
  maxEntries: number = MAX_RESPONSE_CACHE_ENTRIES,
): void {
  if (cache.has(key)) {
    cache.delete(key);
  }
  cache.set(key, value);
  while (cache.size > maxEntries) {
    const oldestKey = cache.keys().next().value;
    if (oldestKey === undefined) break;
    cache.delete(oldestKey);
  }
}

/**
 * Subscribers notified when `PER_COUNTRY_DATA_MAP` or `ALL_COUNTRIES_SORTED`
 * updates.
 */
const COUNTRY_CACHE_LISTENERS = new Set<() => void>();

/**
 * Invokes all registered country cache subscribers, isolating listener
 * exceptions.
 */
function notifyCountryCacheListeners(): void {
  for (const listener of COUNTRY_CACHE_LISTENERS) {
    try {
      listener();
    } catch (e: unknown) {
      console.warn('Error in country cache listener:', e);
    }
  }
}

/**
 * Subscribes a callback to updates of the country data cache.
 * Returns an unsubscribe function.
 */
export function subscribeToCountryCache(listener: () => void): () => void {
  COUNTRY_CACHE_LISTENERS.add(listener);
  return () => {
    COUNTRY_CACHE_LISTENERS.delete(listener);
  };
}

/**
 * Retrieves a cached country record by composite key.
 * @visibleForTesting
 */
export function getCountryDataFromCache(
  key: string,
): RawCountryRecord | undefined {
  return PER_COUNTRY_DATA_MAP.get(key);
}

/**
 * Inserts or updates a cached country record by composite key.
 * @visibleForTesting
 */
export function setCountryDataInCache(
  key: string,
  item: RawCountryRecord,
): void {
  PER_COUNTRY_DATA_MAP.set(key, item);
  FIR_RECORDS_MEMO_CACHE.clear();
  notifyCountryCacheListeners();
}

/** GWP horizon of the dashboard's GWP selector. */
export type GwpHorizon = 'GWP20' | 'GWP50' | 'GWP100';

/**
 * Over-country (territorial) CO2e ranks of a country row, one per GWP horizon:
 * `co2e_{gwp}_tonnes_rank` from `/countries/{year}`, where 1 is the largest
 * impact. Null when the backend did not rank the country.
 */
export type Co2eRanks = Readonly<Record<GwpHorizon, number | null>>;

/** Country selector option entry. */
export interface CountryOption {
  readonly name: string;
  readonly isoA2: string;
  /**
   * Over-country CO2e ranks; absent when the option was not built from data.
   */
  readonly co2eRanks?: Co2eRanks;
}

/**
 * Returns the over-country CO2e rank of `option` for `gwp`, or null when it is
 * missing, non-finite, or not positive.
 */
function co2eRankOf(option: CountryOption, gwp: GwpHorizon): number | null {
  const rank = option.co2eRanks?.[gwp];
  return typeof rank === 'number' && Number.isFinite(rank) && rank > 0
    ? rank
    : null;
}

/**
 * Returns the country with the best (lowest) over-country CO2e rank
 * (`co2e_{gwp}_tonnes_rank`, the territorial impact rather than departures or
 * controlled FIRs) for `gwp`. Unranked options are skipped and ties go to the
 * alphabetically first name. Returns null when no option is ranked.
 */
export function topCountryByCo2eRank(
  countries: readonly CountryOption[],
  gwp: GwpHorizon = 'GWP50',
): CountryOption | null {
  let best: CountryOption | null = null;
  let bestRank = Number.POSITIVE_INFINITY;
  for (const option of countries) {
    const rank = co2eRankOf(option, gwp);
    if (rank === null || rank > bestRank) continue;
    if (rank < bestRank || !best || option.name.localeCompare(best.name) < 0) {
      best = option;
      bestRank = rank;
    }
  }
  return best;
}

let ALL_COUNTRIES_SORTED: readonly CountryOption[] = [];
const ALL_COUNTRIES_BY_LOWER_NAME = new Map<string, CountryOption>();
const ALL_COUNTRIES_BY_UPPER_ISO2 = new Map<string, CountryOption>();

/**
 * Lifecycle of the country list, which comes only from the backend's
 * `/countries/{year}` endpoint: `loading` until the first response, `ready`
 * once a non-empty list arrived, `error` when the request failed or returned an
 * empty list.
 */
export type CountryListStatus = 'loading' | 'ready' | 'error';
let COUNTRY_LIST_STATUS: CountryListStatus = 'loading';

/** Returns the current state of the backend-provided country list. */
export function getCountryListStatus(): CountryListStatus {
  return COUNTRY_LIST_STATUS;
}

/** Options for `setCountryListStatus`. */
interface SetCountryListStatusOptions {
  readonly notify?: boolean;
}

/**
 * Updates `COUNTRY_LIST_STATUS` and optionally notifies subscribers when the
 * status changes.
 */
function setCountryListStatus(
  status: CountryListStatus,
  options: SetCountryListStatusOptions = {},
): boolean {
  const notify = options.notify ?? true;
  if (COUNTRY_LIST_STATUS !== status) {
    COUNTRY_LIST_STATUS = status;
    if (notify) {
      notifyCountryCacheListeners();
    }
    return true;
  }
  return false;
}

/**
 * Returns true when `iso2` is a standard 2-letter ISO 3166-1 alpha-2 code
 * (rejecting sentinel values such as `"-99"`).
 * @visibleForTesting
 */
export function isValidCountryIso2(iso2?: string | null): iso2 is string {
  return typeof iso2 === 'string' && /^[A-Za-z]{2}$/.test(iso2.trim());
}

/**
 * Resolves the canonical selection identifier for a country option or ranking
 * row: its uppercase 2-letter ISO code when valid, its trimmed display name
 * for sentinel-coded territories (such as `iso_a2 = "-99"`), or `''` when
 * neither is present.
 */
export function resolveCountryIdentifier(
  iso2?: string | null,
  name?: string | null,
): string {
  if (isValidCountryIso2(iso2)) {
    return iso2.trim().toUpperCase();
  }
  const cleanName = (name ?? '').trim();
  if (cleanName) return cleanName;
  return '';
}

/** Options for `isCountrySelectionMatch`. */
export interface CountrySelectionMatchOptions {
  readonly isoA2?: string | null;
  readonly name?: string | null;
  readonly selectedCountry?: string | null;
}

/**
 * Returns true when a country entry (`isoA2`, `name`) matches `selectedCountry`
 * by display name or by a valid 2-letter ISO code, never matching sentinel
 * codes such as `"-99"` across distinct territories.
 */
export function isCountrySelectionMatch({
  isoA2,
  name,
  selectedCountry,
}: CountrySelectionMatchOptions): boolean {
  const cleanSelected = (selectedCountry ?? '').trim().toLowerCase();
  if (!cleanSelected) return false;
  const cleanName = (name ?? '').trim().toLowerCase();
  if (cleanName !== '' && cleanName === cleanSelected) return true;
  return (
    isValidCountryIso2(isoA2) && isoA2.trim().toLowerCase() === cleanSelected
  );
}

/**
 * Rebuilds the case-insensitive country name and ISO alpha-2 lookup maps from
 * `ALL_COUNTRIES_SORTED`.
 */
function rebuildCountryLookupMaps(): void {
  ALL_COUNTRIES_BY_LOWER_NAME.clear();
  ALL_COUNTRIES_BY_UPPER_ISO2.clear();
  for (const opt of ALL_COUNTRIES_SORTED) {
    if (opt.name) {
      ALL_COUNTRIES_BY_LOWER_NAME.set(opt.name.trim().toLowerCase(), opt);
    }
    if (isValidCountryIso2(opt.isoA2)) {
      ALL_COUNTRIES_BY_UPPER_ISO2.set(opt.isoA2.trim().toUpperCase(), opt);
    }
  }
}

/**
 * Clears all entries in the country data cache, including the country list,
 * which returns to the `loading` state until it is fetched again.
 * @visibleForTesting
 */
export function clearCountryDataCache(): void {
  PER_COUNTRY_DATA_MAP.clear();
  FIR_DATA_BY_YEAR_MAP.clear();
  FIR_ROWS_BY_YEAR_AND_ISO.clear();
  FIR_ROW_BY_YEAR_AND_KEY.clear();
  FIR_RECORDS_MEMO_CACHE.clear();
  RANKINGS_RESPONSE_CACHE.clear();
  TIMESERIES_RESPONSE_CACHE.clear();
  TOTAL_COUNTRIES_BY_YEAR.clear();
  CACHED_YEARS_LRU.clear();
  AVAILABLE_YEARS = [];
  YEAR_LIST_STATUS = 'loading';
  inFlightAvailableYearsRequest = null;
  ALL_COUNTRIES_SORTED = [];
  rebuildCountryLookupMaps();
  COUNTRY_LIST_STATUS = 'loading';
  latestCountriesYear = null;
  // A request started before the clear must not be joined afterwards.
  IN_FLIGHT_COUNTRIES_REQUESTS.clear();
  notifyCountryCacheListeners();
}

/**
 * Test-only utility to set the available years without hitting the backend.
 * Marks the year list `ready`, as a successful `/years` response would.
 * @visibleForTesting
 */
export function setAvailableYearsForTesting(years: readonly number[]): void {
  AVAILABLE_YEARS = [...years].sort((a, b) => a - b);
  YEAR_LIST_STATUS = 'ready';
  FIR_RECORDS_MEMO_CACHE.clear();
  notifyCountryCacheListeners();
}

/**
 * Returns a readonly view of all sorted countries.
 */
export function getAllCountries(): readonly CountryOption[] {
  return ALL_COUNTRIES_SORTED;
}

/**
 * Test-only utility to populate the country cache without mutating globals
 * directly.
 * @visibleForTesting
 */
export function setAllCountriesForTesting(
  countries: readonly CountryOption[],
): void {
  ALL_COUNTRIES_SORTED = [...countries];
  rebuildCountryLookupMaps();
  FIR_RECORDS_MEMO_CACHE.clear();
  COUNTRY_LIST_STATUS = 'ready';
  notifyCountryCacheListeners();
}

/**
 * Guesses the user's country from the region subtag of their preferred
 * languages (e.g. `de-CH` -> `CH`), without any lookup table.
 *
 * Returns null when no language carries an alphabetic region subtag. The
 * result is only a guess about the user's locale, not their location, and
 * callers should drop it if the backend has no row for that country.
 */
export function guessUserCountry(
  languages: readonly string[] = typeof navigator !== 'undefined'
    ? navigator.languages?.length
      ? navigator.languages
      : navigator.language
        ? [navigator.language]
        : []
    : [],
): string | null {
  for (const tag of languages) {
    // BCP 47: language[-script][-region]...; a region is 2 letters (ISO 3166-1)
    // or 3 digits (UN M.49, e.g. es-419), and only the former is a country.
    const subtags = (tag || '').split(/[-_]/).slice(1);
    for (const subtag of subtags) {
      if (subtag.length === 4) continue; // script, e.g. zh-Hant-TW
      if (/^[A-Za-z]{2}$/.test(subtag)) return subtag.toUpperCase();
      break;
    }
  }
  return null;
}

/**
 * Properties of a country boundary feature as published by the pipeline
 * (`countries.geojson`): `iso_a2` is required, `name` is the display name.
 * Territories without an ISO code carry a sentinel `iso_a2` such as `-99`.
 */
export declare interface GeoFeatureProperties {
  readonly iso_a2?: string;
  readonly iso_a3?: string;
  readonly name?: string;
  readonly [property: string]: unknown;
}

/**
 * Minimal structural shape for a GeoJSON feature carrying country properties.
 */
export interface GeoFeatureLike {
  readonly properties?: GeoFeatureProperties | null;
  readonly [property: string]: unknown;
}

/**
 * Extracts the trimmed `properties.name` string from a GeoJSON feature, or null
 * if absent.
 */
function getFeatureName(
  feat: GeoFeatureLike | null | undefined,
): string | null {
  const name = feat?.properties?.name;
  return typeof name === 'string' && name.trim() ? name.trim() : null;
}

/**
 * Returns the feature's ISO alpha-2 code, or null for sentinel/missing codes.
 */
export function resolveCountryIsoFromFeature(
  feat: GeoFeatureLike | null | undefined,
): string | null {
  const iso = feat?.properties?.iso_a2;
  if (typeof iso === 'string' && /^[A-Za-z]{2}$/.test(iso.trim())) {
    return iso.trim().toUpperCase();
  }
  return null;
}

/**
 * Returns the display name for a feature: the backend's name for its ISO code
 * when loaded, otherwise the feature's own `name`.
 */
export function resolveCountryNameFromFeature(
  feat: GeoFeatureLike | null | undefined,
): string | null {
  if (!feat) return null;
  const sentinelName = getSentinelFeatureName(feat);
  if (sentinelName) return sentinelName;
  const iso = resolveCountryIsoFromFeature(feat);
  if (iso) {
    const fromBackend = getCountryNameFromIso2(iso);
    if (fromBackend && fromBackend !== iso) return fromBackend;
  }
  return getFeatureName(feat) ?? iso;
}

/**
 * Returns the feature's name when it carries a sentinel ISO code.
 *
 * Natural Earth assigns `iso_a2 = "-99"` to territories without an ISO code
 * (Kosovo, N. Cyprus, Somaliland). The backend keys those entities by name, so
 * the name is the only identifier that selects the right one.
 */
function getSentinelFeatureName(
  feat: GeoFeatureLike | null | undefined,
): string | null {
  const iso = feat?.properties?.iso_a2;
  if (typeof iso !== 'string' || !iso.trim().startsWith('-')) return null;
  return getFeatureName(feat);
}

/**
 * Resolves the value a world-map country feature contributes to the country
 * filter: its ISO alpha-2 code, or its name for sentinel-coded territories.
 */
export function resolveCountrySelectionFromFeature(
  feat: GeoFeatureLike | null | undefined,
): string | null {
  return getSentinelFeatureName(feat) ?? resolveCountryIsoFromFeature(feat);
}

/**
 * Returns the world-map features that represent the given country filter
 * value (an ISO alpha-2 code or a country name).
 */
export function findCountryFeaturesForSelection<T extends GeoFeatureLike>(
  features: readonly T[],
  selection: string,
): T[] {
  const trimmed = (selection || '').trim();
  if (!trimmed) return [];
  const upper = trimmed.toUpperCase();

  // Sentinel territories first: they have no ISO code to match on.
  const sentinelMatches = features.filter(
    (feat) => getSentinelFeatureName(feat)?.toUpperCase() === upper,
  );
  if (sentinelMatches.length > 0) return sentinelMatches;

  const selectedIso = getIso2FromCountry(trimmed);
  return features.filter((feat) => {
    if (getSentinelFeatureName(feat)) return false;
    const iso = resolveCountryIsoFromFeature(feat);
    if (!iso) return false;
    return (
      iso === upper ||
      (selectedIso !== '' && iso === selectedIso) ||
      getFeatureName(feat)?.toUpperCase() === upper
    );
  });
}

/**
 * Looks up a cached raw country record by ISO alpha-2 code or country name for
 * `year`, returning `null` when not yet loaded.
 * @visibleForTesting
 */
export function getCountryRecord(
  isoOrName: string,
  year: number,
): RawCountryRecord | null {
  if (!isoOrName || !isValidYear(year)) return null;

  const resolvedIso = getIso2FromCountry(isoOrName);
  const baseCandidates =
    resolvedIso && resolvedIso !== isoOrName
      ? [isoOrName, resolvedIso]
      : [isoOrName];

  const keysToTry: string[] = [];
  for (const candidate of baseCandidates) {
    keysToTry.push(
      `${candidate}_${year}`,
      `${candidate.toUpperCase()}_${year}`,
      `${candidate.toLowerCase()}_${year}`,
    );
  }
  for (const k of keysToTry) {
    const record = PER_COUNTRY_DATA_MAP.get(k);
    if (record !== undefined) {
      touchCachedYear(year);
      return record;
    }
  }

  return null;
}

/**
 * Normalized summary of a country's domestic and international FIR boundaries.
 */
export interface CountryFirBoundaryInfo {
  readonly firDomFirId?: readonly string[] | null;
  readonly firIntFirId?: readonly string[] | null;
  readonly hasDomesticFir: boolean;
  readonly hasInternationalFir: boolean;
}

/**
 * Returns normalized domestic and international FIR boundary identifiers and
 * presence flags for `countryNameOrIso` in `year`, or `undefined` when the
 * country record is not yet loaded.
 */
export function getCountryFirBoundaryInfo(
  countryNameOrIso: string,
  year: number,
): CountryFirBoundaryInfo | undefined {
  const rec = getCountryRecord(countryNameOrIso, year);
  if (!rec) return undefined;
  return {
    firDomFirId: rec.fir_dom_fir_id,
    firIntFirId: rec.fir_int_fir_id,
    // The FIR identity lists are index-aligned, so the IDs alone tell whether
    // a scope has any FIR.
    hasDomesticFir: (rec.fir_dom_fir_id?.length ?? 0) > 0,
    hasInternationalFir: (rec.fir_int_fir_id?.length ?? 0) > 0,
  };
}

/**
 * Resolves an ISO alpha-2 code (or country name) to its human-readable country
 * name from loaded country records or the country option catalog.
 */
export function getCountryNameFromIso2(iso2: string, year?: number): string {
  if (!iso2) return '';
  const cleanIso = iso2.trim();
  const cleanIsoUpper = cleanIso.toUpperCase();
  const rec =
    year !== undefined
      ? (getCountryRecord(cleanIsoUpper, year) ??
        getCountryRecord(cleanIso, year))
      : null;
  if (rec?.name) {
    return rec.name;
  }
  const found =
    ALL_COUNTRIES_BY_UPPER_ISO2.get(cleanIsoUpper) ??
    ALL_COUNTRIES_BY_LOWER_NAME.get(cleanIso.toLowerCase());
  if (found) return found.name;
  return cleanIso;
}

/**
 * Resolves a country name or ISO alpha-2 code to an uppercase two-letter ISO
 * alpha-2 code when known, or returns an empty string otherwise.
 */
export function getIso2FromCountry(nameOrIso: string, year?: number): string {
  if (!nameOrIso) return '';
  const target = nameOrIso.trim();
  const upper = target.toUpperCase();
  const rec =
    year !== undefined
      ? (getCountryRecord(upper, year) ?? getCountryRecord(target, year))
      : null;
  if (rec?.iso_a2 && /^[A-Za-z]{2}$/.test(rec.iso_a2)) {
    return rec.iso_a2.toUpperCase();
  }
  const matchOpt =
    ALL_COUNTRIES_BY_UPPER_ISO2.get(upper) ??
    ALL_COUNTRIES_BY_LOWER_NAME.get(target.toLowerCase());
  if (matchOpt && /^[A-Za-z]{2}$/.test(matchOpt.isoA2)) {
    return matchOpt.isoA2.toUpperCase();
  }
  return /^[A-Z]{2}$/.test(upper) ? upper : '';
}

/**
 * Normalizes raw managing-country wire entries (`iso_a2` / `isoA2`) into
 * `FirManagingCountry` objects, skipping non-object or empty entries.
 */
function normalizeManagingCountries(
  countries: unknown,
): readonly FirManagingCountry[] {
  if (!Array.isArray(countries) || countries.length === 0) return [];
  const normalized: FirManagingCountry[] = [];
  for (const entry of countries) {
    if (!isRecordObject(entry)) continue;
    const name = typeof entry['name'] === 'string' ? entry['name'] : '';
    const isoA2 =
      typeof entry['isoA2'] === 'string'
        ? entry['isoA2']
        : typeof entry['iso_a2'] === 'string'
          ? entry['iso_a2']
          : '';
    if (!name && !isoA2) continue;
    normalized.push({name, isoA2});
  }
  return normalized;
}

/**
 * Resolves the display name for a managing-country entry, preserving an
 * explicit non-ISO `existingName` and otherwise looking up `isoA2` in `year`.
 */
function resolveManagingCountryDisplayName(
  isoA2: string,
  existingName: string,
  year?: number,
): FirManagingCountry {
  const trimmedExisting = existingName.trim();
  if (
    trimmedExisting &&
    trimmedExisting.toUpperCase() !== isoA2.trim().toUpperCase()
  ) {
    return {isoA2, name: existingName};
  }
  const resolvedName = isValidCountryIso2(isoA2)
    ? getCountryNameFromIso2(isoA2, year)
    : '';
  return {
    isoA2,
    name: resolvedName || existingName || isoA2,
  };
}

const FIR_ROWS_BY_YEAR_AND_ISO = new Map<number, Map<string, RawFirRecord[]>>();
const FIR_ROW_BY_YEAR_AND_KEY = new Map<number, Map<string, RawFirRecord>>();

/**
 * Builds and caches per-ISO and per-FIR-ID lookup maps for the raw FIR rows of
 * `year`.
 */
function getFirIndexForYear(year: number): {
  readonly byIso: Map<string, RawFirRecord[]>;
  readonly byKey: Map<string, RawFirRecord>;
} {
  let byIso = FIR_ROWS_BY_YEAR_AND_ISO.get(year);
  let byKey = FIR_ROW_BY_YEAR_AND_KEY.get(year);
  if (byIso && byKey) {
    return {byIso, byKey};
  }
  byIso = new Map<string, RawFirRecord[]>();
  byKey = new Map<string, RawFirRecord>();
  const allRows: readonly RawFirRecord[] = FIR_DATA_BY_YEAR_MAP.get(year) ?? [];
  for (const r of allRows) {
    const firId = (r.fir_id || '').trim().toUpperCase();
    if (!firId) continue;
    byKey.set(firId, r);
    for (const iso of r.fir_to_country_iso2s ?? []) {
      let bucket = byIso.get(iso);
      if (!bucket) {
        bucket = [];
        byIso.set(iso, bucket);
      }
      bucket.push(r);
    }
  }
  if (allRows.length > 0) {
    FIR_ROWS_BY_YEAR_AND_ISO.set(year, byIso);
    FIR_ROW_BY_YEAR_AND_KEY.set(year, byKey);
  }
  return {byIso, byKey};
}

/**
 * Returns the sorted FIR records managed by `countryNameOrIso` for `year` and
 * `gwp`, enriched with YoY indicators and relative share percentages.
 */
export function getFirRecordsForCountry(
  countryNameOrIso: string,
  year: number,
  gwp?: 'GWP20' | 'GWP50' | 'GWP100' | string,
): FirRecord[] {
  const isoStr = getIso2FromCountry(countryNameOrIso);
  const gwpStr = (gwp || 'GWP50').toLowerCase();
  if ((!isoStr && !countryNameOrIso) || !isValidYear(year)) return [];

  const hasCurrentFirs = Boolean(FIR_DATA_BY_YEAR_MAP.get(year)?.length);
  const prevYear = previousAvailableYear(year);
  const hasPrevFirs =
    prevYear !== null && Boolean(FIR_DATA_BY_YEAR_MAP.get(prevYear)?.length);
  const memoKey =
    `${isoStr || countryNameOrIso}_${year}_` +
    `${gwpStr}_${hasPrevFirs ? '1' : '0'}`;
  if (hasCurrentFirs) {
    if (prevYear !== null && hasPrevFirs) {
      touchCachedYear(prevYear);
    }
    touchCachedYear(year);
    const cachedFirs = getBoundedCacheEntry(FIR_RECORDS_MEMO_CACHE, memoKey);
    if (cachedFirs !== undefined) {
      return cachedFirs;
    }
  }

  const prevDataByFirKey: Map<string, RawFirRecord> | undefined =
    prevYear !== null && hasPrevFirs
      ? getFirIndexForYear(prevYear).byKey
      : undefined;

  const candidateRows = isoStr
    ? (getFirIndexForYear(year).byIso.get(isoStr) ?? [])
    : [];
  const matchedFirs: FirRecord[] = [];
  const seenKey = new Set<string>();

  for (const r of candidateRows) {
    const firId = (r.fir_id || '').trim().toUpperCase();
    const icao = (r.fir_icao || '').toUpperCase();
    if (firId && !seenKey.has(firId)) {
      seenKey.add(firId);
      const rTonnesVal = getNumericRecordField(r, `co2e_${gwpStr}_tonnes`) ?? 0;
      const rMeanRf = r.mean_rf_mw_per_sq_m ?? undefined;

      let absoluteYoY: YoYIndicator | undefined;

      const prevR = prevDataByFirKey?.get(firId);
      if (prevR) {
        const prevAbs = getNumericRecordField(prevR, `co2e_${gwpStr}_tonnes`);
        absoluteYoY = computeValueYoY(rTonnesVal, prevAbs);
      }

      const managingCountries: readonly FirManagingCountry[] =
        r.countries && r.countries.length > 0
          ? normalizeManagingCountries(r.countries)
          : (r.fir_to_country_iso2s ?? []).map((cIso) => ({
              isoA2: cIso,
              name: getCountryNameFromIso2(cIso, year) || cIso,
            }));

      matchedFirs.push({
        firId,
        firName: r.firname || icao,
        firIcao: icao,
        type: r.type || 'domestic',
        countries: managingCountries,
        co2eTonnes: rTonnesVal,
        meanRfMwPerSqM: rMeanRf,
        areaSqKm: r.area_sq_km || 0,
        co2eRank: getNumericRecordField(r, `co2e_${gwpStr}_tonnes_rank`),
        impactDensityRank: r.mean_rf_mw_per_sq_m_rank ?? undefined,
        absoluteYoY,
        percentOfFirTotal: '0%',
      });
    }
  }

  matchedFirs.sort((a, b) => b.co2eTonnes - a.co2eTonnes);
  const totalMatchedPositiveTonnes = matchedFirs.reduce(
    (sum, f) => sum + Math.max(0, f.co2eTonnes),
    0,
  );
  const result =
    totalMatchedPositiveTonnes > 0
      ? matchedFirs.map((f) => {
          const fTonnes = Math.max(0, f.co2eTonnes);
          const formattedPercent = formatPercentage(
            fTonnes / totalMatchedPositiveTonnes,
            true,
            0,
          );
          return {
            ...f,
            percentOfFirTotal: formattedPercent,
          };
        })
      : matchedFirs;
  if (hasCurrentFirs) {
    setBoundedCacheEntry(FIR_RECORDS_MEMO_CACHE, memoKey, result);
  }
  return result;
}

/**
 * Returns the managing countries for a given FIR record, using `year` to
 * upgrade ISO-only fallback names when year-scoped country records are loaded.
 */
export function getManagingCountriesForFir(
  fir: FirRecord | RawFirRecord | null | undefined,
  year?: number,
): readonly FirManagingCountry[] {
  if (!fir) return [];
  if (fir.countries && fir.countries.length > 0) {
    return normalizeManagingCountries(fir.countries).map((c) =>
      resolveManagingCountryDisplayName(c.isoA2, c.name, year),
    );
  }
  const isoList: unknown =
    'fir_to_country_iso2s' in fir ? fir['fir_to_country_iso2s'] : undefined;
  if (!Array.isArray(isoList)) return [];
  return isoList
    .filter((cIso): cIso is string => typeof cIso === 'string')
    .map((cIso) => resolveManagingCountryDisplayName(cIso, '', year));
}

const LOADING_HERO_SCOPE: HeroScopeMetrics = {
  absoluteValue: NOT_AVAILABLE_LABEL,
  perCapitaValue: NOT_AVAILABLE_LABEL,
  perCapitaVsWorldAvg: '',
  populationFormatted: NOT_AVAILABLE_LABEL,
  nationalGhgComparison: NOT_AVAILABLE_LABEL,
  nationalGhgTotal: NOT_AVAILABLE_LABEL,
};

/**
 * Options for `buildHeroScopeMetrics`.
 * @visibleForTesting
 */
export interface BuildHeroScopeOptions {
  readonly item: RawCountryRecord;
  readonly prevItem: RawCountryRecord | null;
  readonly prefix: '' | 'outgoing_flight_' | 'fir_total_';
  readonly gwpStr: string;
  readonly totalCountries: number | null;
  readonly populationFormatted: string;
  readonly nationalGhgTotal: string;
  readonly fallbackRankField?: string;
  readonly allowFallbackRankWithoutTonnes?: boolean;
  readonly defaultZeroTonnes?: boolean;
  readonly defaultNotAvailableRank?: boolean;
}

/**
 * Derives formatted values, global ranks, and YoY indicators for one of the
 * three country-level hero scopes (`""`, `"outgoing_flight_"`, or
 * `"fir_total_"`).
 * @visibleForTesting
 */
export function buildHeroScopeMetrics(
  options: BuildHeroScopeOptions,
): HeroScopeMetrics {
  const {
    item,
    prevItem,
    prefix,
    gwpStr,
    totalCountries,
    populationFormatted,
    nationalGhgTotal,
    fallbackRankField,
    allowFallbackRankWithoutTonnes = false,
    defaultZeroTonnes = false,
    defaultNotAvailableRank = false,
  } = options;

  const tonnesKey = `${prefix}co2e_${gwpStr}_tonnes`;
  const rawTonnes = getNumericRecordField(item, tonnesKey);
  const tonnesForFormat = defaultZeroTonnes ? (rawTonnes ?? 0) : rawTonnes;

  const rawAbsRank =
    getNumericRecordField(item, `${tonnesKey}_rank`) ??
    (fallbackRankField
      ? getNumericRecordField(item, fallbackRankField)
      : undefined);
  const hasTonnesMetric = allowFallbackRankWithoutTonnes
    ? rawTonnes !== undefined ||
      (getRecordField(item, tonnesKey) !== null &&
        fallbackRankField !== undefined &&
        getRecordField(item, fallbackRankField) != null)
    : getRecordField(item, tonnesKey) !== null;
  const absoluteRankNum =
    hasTonnesMetric && rawAbsRank !== undefined && rawAbsRank > 0
      ? rawAbsRank
      : undefined;

  const perCapita = getNumericRecordField(
    item,
    `${prefix}co2e_${gwpStr}_tonnes_per_capita`,
  );
  const rawPerCapitaRank = getNumericRecordField(
    item,
    `${prefix}co2e_${gwpStr}_tonnes_per_capita_rank`,
  );
  const perCapitaRankNum =
    perCapita !== undefined &&
    rawPerCapitaRank !== undefined &&
    rawPerCapitaRank > 0
      ? rawPerCapitaRank
      : undefined;
  const perCapitaVsWorldAvg = getNumericRecordField(
    item,
    `${prefix}co2e_${gwpStr}_per_capita_vs_world_avg`,
  );

  const nationalGhgVal = getNumericRecordField(
    item,
    `${prefix}co2e_${gwpStr}_vs_national_ghg`,
  );
  const rawNationalGhgRank = getNumericRecordField(
    item,
    `${prefix}co2e_${gwpStr}_vs_national_ghg_rank`,
  );
  const nationalGhgRankNum =
    nationalGhgVal !== undefined &&
    rawNationalGhgRank !== undefined &&
    rawNationalGhgRank > 0
      ? rawNationalGhgRank
      : undefined;

  const prevTonnes = prevItem
    ? getNumericRecordField(prevItem, tonnesKey)
    : undefined;
  const absoluteYoY = prevItem
    ? computeValueYoY(tonnesForFormat, prevTonnes)
    : undefined;

  return {
    absoluteValue: defaultZeroTonnes
      ? formatTonnesCo2e(tonnesForFormat ?? 0)
      : formatTonnesOrNotAvailable(tonnesForFormat),
    absoluteYoY,
    absoluteRank:
      absoluteRankNum !== undefined
        ? formatRankLabel(absoluteRankNum, totalCountries, 'countries')
        : defaultNotAvailableRank
          ? NOT_AVAILABLE_LABEL
          : undefined,
    absoluteRankNum,
    perCapitaValue: formatTonnesOrNotAvailable(perCapita),
    perCapitaRank:
      perCapitaRankNum !== undefined
        ? formatRankLabel(perCapitaRankNum, totalCountries, 'countries')
        : undefined,
    perCapitaRankNum,
    perCapitaVsWorldAvg:
      perCapitaVsWorldAvg !== undefined
        ? `${(perCapitaVsWorldAvg * 100).toFixed(2)}% of global GHG/capita`
        : '',
    populationFormatted,
    nationalGhgComparison: formatRatioPercentOrNotAvailable(nationalGhgVal),
    nationalGhgRank:
      nationalGhgRankNum !== undefined
        ? formatRankLabel(nationalGhgRankNum, totalCountries, 'countries')
        : undefined,
    nationalGhgRankNum,
    nationalGhgTotal,
    totalCount: totalCountries ?? undefined,
  };
}

/**
 * Builds a placeholder `CountryData` object with `isLoading: true` while
 * backend records are being fetched.
 */
function getLoadingCountryData(
  countryNameOrIso: string,
  year: number,
  gwp: 'GWP20' | 'GWP50' | 'GWP100' = 'GWP50',
): CountryData {
  return {
    isLoading: true,
    isoA2: '',
    name: '',
    aboveCountryHero: LOADING_HERO_SCOPE,
    outgoingHero: LOADING_HERO_SCOPE,
    firTotalHero: {...LOADING_HERO_SCOPE, absoluteRank: NOT_AVAILABLE_LABEL},
    impactDensityRank: NOT_AVAILABLE_LABEL,
    impactDensity: NOT_AVAILABLE_LABEL,
    overflightFraction: NOT_AVAILABLE_LABEL,
    firDensity: NOT_AVAILABLE_LABEL,
    firDensityRank: undefined,
    firDomImpact: NOT_AVAILABLE_LABEL,
    firIntImpact: NOT_AVAILABLE_LABEL,
    hasInternationalFir: false,
    controlledFirRecords: getFirRecordsForCountry(countryNameOrIso, year, gwp),
    totalFirCount: null,
    totalCountryCount: null,
  };
}

/**
 * Formats and computes all country- and FIR-level metrics for
 * `countryNameOrIso` in `year` at the selected `gwp` horizon, returning a
 * loading skeleton (`isLoading: true`) when the country record has not yet
 * arrived.
 */
export function getCountryMetrics(
  countryNameOrIso: string,
  year: number,
  gwp: 'GWP20' | 'GWP50' | 'GWP100' = 'GWP50',
): CountryData {
  const item = getCountryRecord(countryNameOrIso, year);
  if (!item) {
    return getLoadingCountryData(countryNameOrIso, year, gwp);
  }

  const gwpStr = gwp.toLowerCase();
  const rawDensityRankCandidate = getNumericRecordField(
    item,
    'mean_rf_mw_per_sq_m_rank',
  );
  // Strict `!== null` is intentional: an explicit JSON `null` from the backend
  // marks an unranked metric (e.g. missing area), whereas `undefined` occurs
  // when a partial record provides `mean_rf_mw_per_sq_m_rank` without raw RF
  // fields.
  const rawDensityRankNum =
    item['mean_rf_mw_per_sq_m'] !== null &&
    item['impact_density_sq_km'] !== null &&
    rawDensityRankCandidate !== undefined &&
    rawDensityRankCandidate > 0
      ? rawDensityRankCandidate
      : undefined;
  const countryMeanRf = item.mean_rf_mw_per_sq_m ?? undefined;
  const rawInCountryImpact = item.in_country_impact;
  const overflightFraction =
    typeof rawInCountryImpact === 'number' &&
    Number.isFinite(rawInCountryImpact)
      ? formatPercentage(
          Math.min(100, Math.max(0, (1 - rawInCountryImpact) * 100)),
        )
      : NOT_AVAILABLE_LABEL;
  const ghgMt = item.ghg_mtco2e ?? 0;
  const nationalGhgTotal = formatTonnesCo2e(ghgMt * 1_000_000);
  const pop = item.population ?? 0;
  const populationFormatted =
    pop >= 1_000_000
      ? `${(pop / 1_000_000).toFixed(1)}M population`
      : pop >= 1_000
        ? `${(pop / 1_000).toFixed(1)}K population`
        : `${Math.round(pop)} population`;

  const firTotalMeanRf = item.fir_total_mean_rf_mw_per_sq_m ?? undefined;
  const totalCountries = getTotalCountryCount(year);
  const matchedFirs = getFirRecordsForCountry(countryNameOrIso, year, gwp);
  const prevYear = previousAvailableYear(year);
  const prevItem =
    prevYear !== null ? getCountryRecord(countryNameOrIso, prevYear) : null;

  const sharedScopeOpts = {
    item,
    prevItem,
    gwpStr,
    totalCountries,
    populationFormatted,
    nationalGhgTotal,
  };
  const aboveCountryHero = buildHeroScopeMetrics({
    ...sharedScopeOpts,
    prefix: '',
    defaultZeroTonnes: true,
  });
  const outgoingHero = buildHeroScopeMetrics({
    ...sharedScopeOpts,
    prefix: 'outgoing_flight_',
    fallbackRankField: 'outgoing_flight_impact_rank',
  });
  const firTotalHero = buildHeroScopeMetrics({
    ...sharedScopeOpts,
    prefix: 'fir_total_',
    fallbackRankField: 'fir_total_rank',
    allowFallbackRankWithoutTonnes: true,
    defaultZeroTonnes: true,
    defaultNotAvailableRank: true,
  });

  const firDomTonnes =
    getNumericRecordField(item, `fir_dom_co2e_${gwpStr}_tonnes`) ?? 0;
  const firIntTonnes =
    getNumericRecordField(item, `fir_int_co2e_${gwpStr}_tonnes`) ?? 0;
  const rawFirDensityRank = getNumericRecordField(
    item,
    'fir_total_mean_rf_mw_per_sq_m_rank',
  );
  const firDensityRankNum =
    firTotalMeanRf !== undefined &&
    rawFirDensityRank !== undefined &&
    rawFirDensityRank > 0
      ? rawFirDensityRank
      : undefined;

  return {
    isLoading: false,
    aboveCountryHero,
    outgoingHero,
    firTotalHero,
    densityRankNum: rawDensityRankNum,
    firDensityRankNum,
    impactDensityRank:
      rawDensityRankNum !== undefined
        ? formatRankLabel(rawDensityRankNum, totalCountries, 'countries')
        : NOT_AVAILABLE_LABEL,
    impactDensity: formatMwPerSqM(countryMeanRf),
    overflightFraction,
    firDensity: formatMwPerSqM(firTotalMeanRf),
    firDensityRank:
      firDensityRankNum !== undefined && firDensityRankNum > 0
        ? formatRankLabel(firDensityRankNum, totalCountries, 'countries')
        : undefined,
    firDomImpact: formatTonnesCo2e(firDomTonnes),
    firIntImpact: formatTonnesCo2e(firIntTonnes),
    hasInternationalFir:
      firIntTonnes !== 0 ||
      matchedFirs.some((f) => f.type?.toLowerCase() === FIR_TYPE_INTERNATIONAL),
    controlledFirRecords: matchedFirs,
    totalFirCount: getTotalFirCount(year),
    totalCountryCount: totalCountries,
    isoA2: item.iso_a2 ? String(item.iso_a2).toUpperCase() : '',
    name: item.name ?? '',
  };
}

let inFlightAvailableYearsRequest: Promise<readonly number[]> | null = null;

/**
 * Validates a `/years` response body and returns its years sorted ascending, or
 * null when the body is malformed.
 * @visibleForTesting
 */
export function parseYearsResponse(data: unknown): number[] | null {
  if (!isRecordObject(data) || !Array.isArray(data['years'])) {
    return null;
  }
  const years: unknown[] = data['years'];
  if (!years.every(isValidYear)) {
    return null;
  }
  return [...new Set(years)].sort((a, b) => a - b);
}

/**
 * Updates `YEAR_LIST_STATUS` and `AVAILABLE_YEARS`, notifying listeners when
 * either changes.
 */
function setYearList(status: YearListStatus, years: readonly number[]): void {
  const changed =
    status !== YEAR_LIST_STATUS ||
    years.length !== AVAILABLE_YEARS.length ||
    years.some((y, idx) => y !== AVAILABLE_YEARS[idx]);
  YEAR_LIST_STATUS = status;
  AVAILABLE_YEARS = years;
  if (changed) {
    FIR_RECORDS_MEMO_CACHE.clear();
    notifyCountryCacheListeners();
  }
}

/**
 * Queries `/years` on the backend to discover which calendar years exist in the
 * active data source, updating `AVAILABLE_YEARS` and the year list status and
 * notifying listeners. An empty list is a valid answer (nothing published yet)
 * and marks the list `ready`; a failed or malformed response marks it `error`,
 * unless an earlier response already made it `ready`, in which case the loaded
 * years are kept.
 */
export async function fetchAvailableYearsFromBackend(): Promise<
  readonly number[]
> {
  if (inFlightAvailableYearsRequest) {
    return inFlightAvailableYearsRequest;
  }

  const requestPromise = (async (): Promise<readonly number[]> => {
    const years = await fetchBackendJson('/years', {
      parse: parseYearsResponse,
      description: 'available years',
    });
    if (years !== null) {
      setYearList('ready', years);
      return AVAILABLE_YEARS;
    }
    // A failed refresh must not hide years that already loaded.
    setYearList(
      YEAR_LIST_STATUS === 'ready' ? 'ready' : 'error',
      AVAILABLE_YEARS,
    );
    return AVAILABLE_YEARS;
  })();

  inFlightAvailableYearsRequest = requestPromise;
  try {
    return await requestPromise;
  } finally {
    inFlightAvailableYearsRequest = null;
  }
}

/**
 * Returns the elements of `body` that satisfy `isValidItem`, or null when
 * `body` is not an array or no element qualifies (an empty list is treated as a
 * failed load so callers keep any previously loaded data).
 */
function parseNonEmptyRecordList<T>(
  body: unknown,
  isValidItem: (value: unknown) => value is T,
): T[] | null {
  if (!Array.isArray(body)) return null;
  const records = body.filter(isValidItem);
  return records.length > 0 ? records : null;
}

/** Type guard verifying that `val` is a raw country record object. */
function isRawCountryRecord(val: unknown): val is RawCountryRecord {
  return isRecordObject(val) && ('name' in val || 'iso_a2' in val);
}

/**
 * Options controlling global country list updates and listener notifications
 * during `/countries/{year}` fetches.
 */
interface FetchCountriesOptions {
  readonly updateSortedGlobals?: boolean;
  readonly notifyListeners?: boolean;
}

/** Result of a single-year `/countries/{year}` fetch. */
interface CountriesFetchOutcome {
  readonly options: readonly CountryOption[];
  readonly didUpdateCache: boolean;
}

/** Shared result of one `/countries/{year}` request. */
interface CountriesRequestResult {
  readonly options: readonly CountryOption[];
  /** False when the request failed or returned no usable records. */
  readonly loaded: boolean;
}

/**
 * In-flight `/countries/{year}` requests keyed by year only, so every caller
 * for a year shares one request whether or not it updates the picker list.
 */
const IN_FLIGHT_COUNTRIES_REQUESTS = new Map<
  number,
  Promise<CountriesRequestResult>
>();
/**
 * Year whose country list should populate the shared picker state. Only a
 * response for this year may write it, so a slow response for a year the user
 * has since left cannot overwrite the newer one. Keyed by year rather than by
 * request: a repeat request for a year that is still in flight reuses that
 * request, and both would return the same list anyway.
 */
let latestCountriesYear: number | null = null;

/**
 * Requests `/countries/{year}` and fills the per-year record caches. Does not
 * touch the shared picker list; see `fetchSingleYearCountries`.
 */
async function requestYearCountries(
  year: number,
): Promise<CountriesRequestResult> {
  const typedData = await fetchBackendJson(
    `/countries/${encodeURIComponent(year)}`,
    {
      parse: (body) => parseNonEmptyRecordList(body, isRawCountryRecord),
      description: 'countries',
    },
  );
  if (typedData === null) {
    return {options: [], loaded: false};
  }
  touchCachedYear(year);
  TOTAL_COUNTRIES_BY_YEAR.set(year, countRankedEntities(typedData));
  for (const item of typedData) {
    const cName = (item.name || '').trim();
    if (isValidCountryIso2(item.iso_a2)) {
      const cleanIso = item.iso_a2.trim();
      PER_COUNTRY_DATA_MAP.set(`${cleanIso}_${year}`, item);
      PER_COUNTRY_DATA_MAP.set(`${cleanIso.toUpperCase()}_${year}`, item);
    }
    if (cName) {
      PER_COUNTRY_DATA_MAP.set(`${cName}_${year}`, item);
      PER_COUNTRY_DATA_MAP.set(`${cName.toUpperCase()}_${year}`, item);
      PER_COUNTRY_DATA_MAP.set(`${cName.toLowerCase()}_${year}`, item);
    }
  }
  const countryOptions: CountryOption[] = typedData
    .filter((item: RawCountryRecord) => Boolean(item.name && item.iso_a2))
    .sort((a: RawCountryRecord, b: RawCountryRecord) =>
      String(a.name || '').localeCompare(String(b.name || '')),
    )
    .map((item: RawCountryRecord) => ({
      name: item.name || '',
      isoA2: item.iso_a2 || '',
      co2eRanks: {
        GWP20: getNumericRecordField(item, 'co2e_gwp20_tonnes_rank') ?? null,
        GWP50: getNumericRecordField(item, 'co2e_gwp50_tonnes_rank') ?? null,
        GWP100: getNumericRecordField(item, 'co2e_gwp100_tonnes_rank') ?? null,
      },
    }));
  FIR_RECORDS_MEMO_CACHE.clear();
  return {options: countryOptions, loaded: true};
}

/**
 * Fetches and caches country records for `year` from `/countries/{year}`,
 * sharing one request among concurrent callers for the same year. Callers
 * with `updateSortedGlobals` set publish the result to the picker list if the
 * year is still the newest one requested when the response arrives.
 */
async function fetchSingleYearCountries(
  year: number,
  options: FetchCountriesOptions = {},
): Promise<CountriesFetchOutcome> {
  const updateSortedGlobals = options.updateSortedGlobals ?? true;
  const notifyListeners = options.notifyListeners ?? true;
  if (updateSortedGlobals) {
    latestCountriesYear = year;
  }
  let request = IN_FLIGHT_COUNTRIES_REQUESTS.get(year);
  if (request === undefined) {
    request = requestYearCountries(year);
    IN_FLIGHT_COUNTRIES_REQUESTS.set(year, request);
  }
  try {
    const result = await request;
    let didUpdateCache = result.loaded;
    if (updateSortedGlobals && latestCountriesYear === year) {
      if (result.loaded) {
        ALL_COUNTRIES_SORTED = [...result.options];
        rebuildCountryLookupMaps();
        COUNTRY_LIST_STATUS = 'ready';
      } else if (ALL_COUNTRIES_SORTED.length === 0) {
        // Keep an earlier year's list if there is one; only report an error
        // when the picker would otherwise have nothing to show.
        didUpdateCache = setCountryListStatus('error', {notify: false});
      }
    }
    if (notifyListeners && didUpdateCache) {
      notifyCountryCacheListeners();
    }
    return {options: result.options, didUpdateCache};
  } finally {
    // Compare by identity: if `clearCountryDataCache` ran while this request
    // was in flight, the entry may now be a newer request for the same year.
    if (IN_FLIGHT_COUNTRIES_REQUESTS.get(year) === request) {
      IN_FLIGHT_COUNTRIES_REQUESTS.delete(year);
    }
  }
}

/** Type guard verifying that `val` is a raw FIR record object. */
function isRawFirRecord(val: unknown): val is RawFirRecord {
  return (
    isRecordObject(val) &&
    ('fir_id' in val || 'fir_icao' in val || 'firname' in val)
  );
}

/**
 * Options controlling cache notification behavior when fetching FIR records.
 */
export interface FetchFirsOptions {
  readonly notifyListeners?: boolean;
}

/** Result of a single-year `/firs/{year}` fetch. */
interface FirsFetchOutcome {
  readonly firs: readonly RawFirRecord[];
  readonly didUpdateCache: boolean;
  notified: boolean;
}

const IN_FLIGHT_FIRS_REQUESTS = new Map<number, Promise<FirsFetchOutcome>>();

/**
 * Fetches and caches FIR records for `yearParam` from `/firs/{year}`,
 * deduplicating concurrent requests.
 */
async function fetchFirsFromBackendInternal(
  yearParam: number,
  options: FetchFirsOptions = {},
): Promise<FirsFetchOutcome> {
  const year = requireYear(yearParam, 'fetchFirsFromBackend');
  const notifyListeners = options.notifyListeners ?? true;
  const existing = FIR_DATA_BY_YEAR_MAP.get(year);
  if (existing && existing.length > 0) {
    touchCachedYear(year);
    return {firs: existing, didUpdateCache: false, notified: false};
  }
  const inFlight = IN_FLIGHT_FIRS_REQUESTS.get(year);
  if (inFlight) {
    const outcome = await inFlight;
    if (notifyListeners && outcome.didUpdateCache && !outcome.notified) {
      outcome.notified = true;
      notifyCountryCacheListeners();
    }
    return outcome;
  }

  const requestPromise = (async (): Promise<FirsFetchOutcome> => {
    const typedFirs = await fetchBackendJson(
      `/firs/${encodeURIComponent(year)}`,
      {
        parse: (body) => parseNonEmptyRecordList(body, isRawFirRecord),
        description: 'FIRs',
      },
    );
    if (typedFirs !== null) {
      touchCachedYear(year);
      FIR_DATA_BY_YEAR_MAP.set(year, typedFirs);
      FIR_ROWS_BY_YEAR_AND_ISO.delete(year);
      FIR_ROW_BY_YEAR_AND_KEY.delete(year);
      FIR_RECORDS_MEMO_CACHE.clear();
      return {firs: typedFirs, didUpdateCache: true, notified: false};
    }
    return {firs: [], didUpdateCache: false, notified: false};
  })();

  IN_FLIGHT_FIRS_REQUESTS.set(year, requestPromise);
  try {
    const outcome = await requestPromise;
    if (notifyListeners && outcome.didUpdateCache && !outcome.notified) {
      outcome.notified = true;
      notifyCountryCacheListeners();
    }
    return outcome;
  } finally {
    IN_FLIGHT_FIRS_REQUESTS.delete(year);
  }
}

/**
 * Fetches FIR summary records for `yearParam` from the backend, populating
 * `FIR_DATA_BY_YEAR_MAP` and deduplicating concurrent requests for the same
 * year.
 */
export async function fetchFirsFromBackend(
  yearParam: number,
  options: FetchFirsOptions = {},
): Promise<readonly RawFirRecord[]> {
  const outcome = await fetchFirsFromBackendInternal(yearParam, options);
  return outcome.firs;
}

/**
 * Fetches country and FIR records for `yearParam` (and its previous available
 * year when present) in parallel, coalescing cache listener notifications into
 * a single callback invocation once the batch completes.
 */
export async function fetchCountriesFromBackend(
  yearParam: number,
): Promise<readonly CountryOption[]> {
  const year = requireYear(yearParam, 'fetchCountriesFromBackend');
  const prevYear = previousAvailableYear(year);
  if (prevYear !== null) {
    const [currentCountries, prevCountries, currentFirs, prevFirs] =
      await Promise.all([
        fetchSingleYearCountries(year, {
          updateSortedGlobals: true,
          notifyListeners: false,
        }),
        fetchSingleYearCountries(prevYear, {
          updateSortedGlobals: false,
          notifyListeners: false,
        }),
        fetchFirsFromBackendInternal(year, {notifyListeners: false}),
        fetchFirsFromBackendInternal(prevYear, {notifyListeners: false}),
      ]);
    if (
      currentCountries.didUpdateCache ||
      prevCountries.didUpdateCache ||
      (currentFirs.didUpdateCache && !currentFirs.notified) ||
      (prevFirs.didUpdateCache && !prevFirs.notified)
    ) {
      currentFirs.notified = true;
      prevFirs.notified = true;
      notifyCountryCacheListeners();
    }
    return currentCountries.options;
  }
  const [currentCountries, currentFirs] = await Promise.all([
    fetchSingleYearCountries(year, {
      updateSortedGlobals: true,
      notifyListeners: false,
    }),
    fetchFirsFromBackendInternal(year, {notifyListeners: false}),
  ]);
  if (
    currentCountries.didUpdateCache ||
    (currentFirs.didUpdateCache && !currentFirs.notified)
  ) {
    currentFirs.notified = true;
    notifyCountryCacheListeners();
  }
  return currentCountries.options;
}

/**
 * Ranked-country totals per year, derived from each year's `/countries` list.
 */
const TOTAL_COUNTRIES_BY_YEAR = new Map<number, number>();

/**
 * Returns how many countries are ranked in `year`, or null until that year's
 * `/countries` list has loaded. There is no hardcoded fallback.
 */
export function getTotalCountryCount(year: number): number | null {
  return TOTAL_COUNTRIES_BY_YEAR.get(year) ?? null;
}

/**
 * Clears cached per-year ranked country totals in unit tests.
 * @visibleForTesting
 */
export function clearTotalCountryCounts(): void {
  TOTAL_COUNTRIES_BY_YEAR.clear();
}

/**
 * Ensures the ranked-country total for `year` is known and returns it, or null
 * if `/countries` could not be loaded.
 *
 * Reuses an in-flight `/countries` request for the year when there is one and
 * never touches the picker's sorted list, so calling it from the metrics panel
 * cannot supersede the country picker's own request.
 */
export async function fetchCountryTotalFromBackend(
  yearParam: number,
): Promise<number | null> {
  const year = requireYear(yearParam, 'fetchCountryTotalFromBackend');
  if (!TOTAL_COUNTRIES_BY_YEAR.has(year)) {
    await fetchSingleYearCountries(year, {updateSortedGlobals: false});
  }
  return getTotalCountryCount(year);
}

/** Memoized `countRankedEntities` per loaded FIR list. */
const TOTAL_FIRS_BY_RECORDS = new WeakMap<readonly RawFirRecord[], number>();

/**
 * Returns how many FIRs are ranked in `year`, or null until that year's
 * `/firs` list has loaded. There is no hardcoded fallback.
 */
export function getTotalFirCount(year: number): number | null {
  const records = FIR_DATA_BY_YEAR_MAP.get(year);
  if (!records || records.length === 0) {
    return null;
  }
  let total = TOTAL_FIRS_BY_RECORDS.get(records);
  if (total === undefined) {
    total = countRankedEntities(records);
    TOTAL_FIRS_BY_RECORDS.set(records, total);
  }
  return total;
}

/**
 * Fetches a single country's raw metric record for `yearParam` from
 * `/country/{year}/{iso}`, or returns the cached record when already loaded.
 */
export async function fetchCountryRecordFromBackend(
  rawIsoOrName: string,
  yearParam: number,
): Promise<RawCountryRecord | null> {
  const year = requireYear(yearParam, 'fetchCountryRecordFromBackend');

  const rawTarget = rawIsoOrName ? rawIsoOrName.trim() : '';
  // `-99` is shared by several territories, so it identifies no single row.
  if (!rawTarget || rawTarget === '-99') {
    return null;
  }

  const existingRecord = getCountryRecord(rawTarget, year);
  if (existingRecord) {
    return existingRecord;
  }

  const inFlightCountries = IN_FLIGHT_COUNTRIES_REQUESTS.get(year);
  if (inFlightCountries) {
    try {
      await inFlightCountries;
    } catch {
      // Fall through to individual country lookup if batch fetch failed.
    }
    const afterBatchRecord = getCountryRecord(rawTarget, year);
    if (afterBatchRecord) {
      return afterBatchRecord;
    }
  }

  const target = getIso2FromCountry(rawTarget) || rawTarget.toUpperCase();
  if (!isValidCountryIso2(target)) {
    return getCountryRecord(rawTarget, year);
  }

  const cachedRecord = PER_COUNTRY_DATA_MAP.get(`${target}_${year}`);
  if (cachedRecord) {
    return cachedRecord;
  }

  const item = await fetchBackendJson(
    `/country/${encodeURIComponent(year)}/${encodeURIComponent(target)}`,
    {parse: parseWith(isRawCountryRecord), description: 'country record'},
  );
  if (item === null) return null;

  const cName = (item.name || '').trim();
  const cleanIso = isValidCountryIso2(item.iso_a2) ? item.iso_a2.trim() : null;
  const recordKey = cleanIso
    ? `${cleanIso.toUpperCase()}_${year}`
    : cName
      ? `${cName.toUpperCase()}_${year}`
      : `${target}_${year}`;
  touchCachedYear(year);
  const hadPrevious = PER_COUNTRY_DATA_MAP.has(recordKey);
  if (cleanIso) {
    PER_COUNTRY_DATA_MAP.set(`${cleanIso}_${year}`, item);
    PER_COUNTRY_DATA_MAP.set(`${cleanIso.toUpperCase()}_${year}`, item);
  }
  if (cName) {
    PER_COUNTRY_DATA_MAP.set(`${cName}_${year}`, item);
    PER_COUNTRY_DATA_MAP.set(`${cName.toUpperCase()}_${year}`, item);
    PER_COUNTRY_DATA_MAP.set(`${cName.toLowerCase()}_${year}`, item);
  }
  if (!hadPrevious) {
    notifyCountryCacheListeners();
  }
  return item;
}

/** Ranked country or FIR entry returned by `/rankings/{year}/{metric}`. */
export declare interface RankingItem {
  /**
   * 1-based rank among entries with a value; null when `value` is null (e.g. a
   * per-area metric for an entity without area). Unranked entries come last.
   */
  readonly rank: number | null;
  readonly name: string;
  readonly isoA2: string;
  readonly firId?: string;
  readonly firIcao?: string;
  readonly firName?: string;
  readonly type?: string;
  readonly countryName?: string;
  readonly countries?: readonly FirManagingCountry[];
  readonly value?: number | null;
  /**
   * Supporting figure, interpreted with `RankingsData.secondaryUnit`. This
   * used to be a pre-rendered English string built by the server; it is a raw
   * number now so the client can format it in the user's locale.
   */
  readonly secondaryValue?: number;
}

/** Response payload returned by `/rankings/{year}/{metric}`. */
export declare interface RankingsData {
  readonly metric: string;
  readonly title: string;
  readonly year: number;
  readonly gwp: string;
  /** A UNIT_* identifier, not display text. */
  readonly unit?: string;
  readonly secondaryUnit?: string;
  readonly isFirRanking?: boolean;
  readonly items: readonly RankingItem[];
}

/** Options for `fetchRankingsFromBackend`. */
export interface FetchRankingsOptions {
  readonly metric: string;
  readonly year: number;
  readonly gwp?: string;
  readonly signal?: AbortSignal;
}

/** Type guard verifying that `value` is a non-null object record. */
export function isRecordObject(
  value: unknown,
): value is Readonly<Record<string, unknown>> {
  return typeof value === 'object' && value !== null;
}

/**
 * Normalizes a raw ranking entry from `/rankings/{year}/{metric}` into
 * `RankingItem`, or returns `null` when invalid.
 */
function normalizeRankingItem(raw: unknown): RankingItem | null {
  if (!isRecordObject(raw)) return null;
  const rank = typeof raw['rank'] === 'number' ? raw['rank'] : null;
  const name = typeof raw['name'] === 'string' ? raw['name'] : '';
  const isoA2 =
    typeof raw['isoA2'] === 'string'
      ? raw['isoA2']
      : typeof raw['iso_a2'] === 'string'
        ? raw['iso_a2']
        : '';
  const firId =
    typeof raw['firId'] === 'string'
      ? raw['firId']
      : typeof raw['fir_id'] === 'string'
        ? raw['fir_id']
        : undefined;
  const firIcao =
    typeof raw['firIcao'] === 'string'
      ? raw['firIcao']
      : typeof raw['fir_icao'] === 'string'
        ? raw['fir_icao']
        : undefined;
  const firName =
    typeof raw['firName'] === 'string'
      ? raw['firName']
      : typeof raw['firname'] === 'string'
        ? raw['firname']
        : typeof raw['fir_name'] === 'string'
          ? raw['fir_name']
          : undefined;
  const type = typeof raw['type'] === 'string' ? raw['type'] : undefined;
  const countryName =
    typeof raw['countryName'] === 'string'
      ? raw['countryName']
      : typeof raw['country_name'] === 'string'
        ? raw['country_name']
        : undefined;
  const countries = Array.isArray(raw['countries'])
    ? normalizeManagingCountries(raw['countries'])
    : undefined;
  const value =
    typeof raw['value'] === 'number' || raw['value'] === null
      ? raw['value']
      : undefined;
  const secondaryValue =
    typeof raw['secondaryValue'] === 'number'
      ? raw['secondaryValue']
      : typeof raw['secondary_value'] === 'number'
        ? raw['secondary_value']
        : undefined;

  return {
    rank,
    name,
    isoA2,
    ...(firId !== undefined ? {firId} : {}),
    ...(firIcao !== undefined ? {firIcao} : {}),
    ...(firName !== undefined ? {firName} : {}),
    ...(type !== undefined ? {type} : {}),
    ...(countryName !== undefined ? {countryName} : {}),
    ...(countries !== undefined ? {countries} : {}),
    ...(value !== undefined ? {value} : {}),
    ...(secondaryValue !== undefined ? {secondaryValue} : {}),
  };
}

/**
 * Validates and normalizes a `/rankings/{year}/{metric}` response body into
 * `RankingsData`, or returns `null` when malformed.
 */
function normalizeRankingsData(value: unknown): RankingsData | null {
  if (!isRecordObject(value)) return null;
  if (
    typeof value['metric'] !== 'string' ||
    !isValidYear(value['year']) ||
    !Array.isArray(value['items'])
  ) {
    return null;
  }
  const items: RankingItem[] = [];
  for (const rawItem of value['items']) {
    const normalized = normalizeRankingItem(rawItem);
    if (normalized !== null) {
      items.push(normalized);
    }
  }
  const unit = typeof value['unit'] === 'string' ? value['unit'] : undefined;
  const secondaryUnit =
    typeof value['secondaryUnit'] === 'string'
      ? value['secondaryUnit']
      : typeof value['secondary_unit'] === 'string'
        ? value['secondary_unit']
        : undefined;
  const isFirRanking =
    typeof value['isFirRanking'] === 'boolean'
      ? value['isFirRanking']
      : typeof value['is_fir_ranking'] === 'boolean'
        ? value['is_fir_ranking']
        : undefined;
  return {
    metric: value['metric'],
    title: typeof value['title'] === 'string' ? value['title'] : '',
    year: value['year'],
    gwp: typeof value['gwp'] === 'string' ? value['gwp'] : '',
    ...(unit !== undefined ? {unit} : {}),
    ...(secondaryUnit !== undefined ? {secondaryUnit} : {}),
    ...(isFirRanking !== undefined ? {isFirRanking} : {}),
    items,
  };
}

/**
 * Fetches ranked country or FIR rows for `options.metric` and `options.year`.
 */
export async function fetchRankingsFromBackend(
  options: FetchRankingsOptions,
): Promise<RankingsData | null> {
  const metric = options.metric;
  const year = requireYear(options.year, 'fetchRankingsFromBackend');
  const gwp = options.gwp ?? 'GWP50';
  const signal = options.signal;

  if (signal?.aborted) {
    return null;
  }

  const gwpLower = gwp.toLowerCase();
  const cacheKey = `${metric}_${year}_${gwpLower}`;
  const cachedRankings = RANKINGS_RESPONSE_CACHE.get(cacheKey);
  if (cachedRankings) {
    return cachedRankings;
  }

  const searchParams = new URLSearchParams({
    gwp: gwpLower,
  });
  // The backend is the authoritative source for rankings, both per-country and
  // per-FIR. On failure we surface "no data" rather than recomputing the
  // ranking from a second, client-side copy of the dataset.
  const data = await fetchBackendJson(
    `/rankings/${encodeURIComponent(year)}/${encodeURIComponent(metric)}` +
      `?${searchParams.toString()}`,
    {parse: normalizeRankingsData, description: 'rankings', signal},
  );
  if (data === null || signal?.aborted) return null;
  setBoundedCacheEntry(RANKINGS_RESPONSE_CACHE, cacheKey, data);
  return data;
}

/** Single annual data point in a historical metric series. */
export interface TimeSeriesPoint {
  readonly year: number;
  readonly value: number | null;
}

/** Multi-year time series payload returned by `/series/{metric}`. */
export declare interface TimeSeriesData {
  readonly metric: string;
  readonly metricTitle: string;
  readonly unit?: string | null;
  readonly isoA2?: string | null;
  readonly countryName?: string | null;
  readonly firId?: string | null;
  readonly firIcao?: string | null;
  readonly firName?: string | null;
  readonly gwp: string;
  readonly points: readonly TimeSeriesPoint[];
}

/**
 * Reads a nullable string field from `record` by `camelKey` or fallback
 * `snakeKeys`.
 */
function readNullableStringField(
  record: Readonly<Record<string, unknown>>,
  camelKey: string,
  ...snakeKeys: string[]
): string | null | undefined {
  for (const key of [camelKey, ...snakeKeys]) {
    const candidate = record[key];
    if (typeof candidate === 'string' || candidate === null) {
      return candidate;
    }
  }
  return undefined;
}

/**
 * Validates and normalizes a `/series/{metric}` response body into
 * `TimeSeriesData`, or returns `null` when malformed.
 */
function normalizeTimeSeriesData(value: unknown): TimeSeriesData | null {
  if (!isRecordObject(value)) return null;
  if (typeof value['metric'] !== 'string' || !Array.isArray(value['points'])) {
    return null;
  }
  const points: TimeSeriesPoint[] = [];
  for (const pt of value['points']) {
    if (isRecordObject(pt) && isValidYear(pt['year'])) {
      points.push({
        year: pt['year'],
        value: typeof pt['value'] === 'number' ? pt['value'] : null,
      });
    }
  }
  const metricTitle =
    typeof value['metricTitle'] === 'string'
      ? value['metricTitle']
      : typeof value['metric_title'] === 'string'
        ? value['metric_title']
        : '';
  const unit = readNullableStringField(value, 'unit');
  const isoA2 = readNullableStringField(value, 'isoA2', 'iso_a2');
  const countryName = readNullableStringField(
    value,
    'countryName',
    'country_name',
  );
  const firId = readNullableStringField(value, 'firId', 'fir_id');
  const firIcao = readNullableStringField(value, 'firIcao', 'fir_icao');
  const firName = readNullableStringField(
    value,
    'firName',
    'firname',
    'fir_name',
  );
  return {
    metric: value['metric'],
    metricTitle,
    ...(unit !== undefined ? {unit} : {}),
    ...(isoA2 !== undefined ? {isoA2} : {}),
    ...(countryName !== undefined ? {countryName} : {}),
    ...(firId !== undefined ? {firId} : {}),
    ...(firIcao !== undefined ? {firIcao} : {}),
    ...(firName !== undefined ? {firName} : {}),
    gwp: typeof value['gwp'] === 'string' ? value['gwp'] : '',
    points,
  };
}

/** Options for `fetchTimeSeriesFromBackend`. */
export declare interface FetchTimeSeriesOptions {
  readonly iso?: string | null;
  readonly firId?: string | null;
  readonly gwp?: string;
  readonly signal?: AbortSignal;
}

/** Fetches historical annual values for `metric` across available years. */
export async function fetchTimeSeriesFromBackend(
  metric: string,
  options: FetchTimeSeriesOptions = {},
): Promise<TimeSeriesData | null> {
  const {iso, firId, gwp = 'GWP50', signal} = options;
  if (signal?.aborted) {
    return null;
  }
  const cacheKey = `${metric}_${iso ?? ''}_${firId ?? ''}_${gwp.toLowerCase()}`;
  const cachedSeries = TIMESERIES_RESPONSE_CACHE.get(cacheKey);
  if (cachedSeries) {
    return cachedSeries;
  }

  const params = new URLSearchParams();
  if (iso) params.set('iso', iso);
  if (firId) params.set('fir_id', firId);
  if (gwp) params.set('gwp', gwp);

  // The backend is the authoritative source for time series data. On failure
  // we surface "no data" rather than recomputing the series from a second,
  // client-side copy of the dataset.
  const data = await fetchBackendJson(
    `/series/${encodeURIComponent(metric)}?${params.toString()}`,
    {parse: normalizeTimeSeriesData, description: 'time series', signal},
  );
  if (data === null || signal?.aborted) return null;
  setBoundedCacheEntry(TIMESERIES_RESPONSE_CACHE, cacheKey, data);
  return data;
}
