/**
 * @fileoverview Pure data model, normalization, cell construction, and
 * threshold outline calculation helpers for the 24x12 temporal contrail impact
 * and avoidance opportunity heatmaps.
 */

import {
  getBoundedCacheEntry,
  getIso2FromCountry,
  isRecordObject,
  isValidYear,
  requireYear,
  setBoundedCacheEntry,
} from './CountryMetadata';
import {extractIcaoFromFirId, formatFirDisplayName} from './metric_formatters';
import {
  clampSigmoidMidpoint,
  computePercentileRanks,
  computeSigmoidCongestion,
  computeSigmoidImpact,
  CONGESTION_SIGMOID_BAND,
  DEFAULT_CONGESTION_MIDPOINT,
  DEFAULT_IMPACT_PERCENTILE_MIDPOINT,
  IMPACT_SIGMOID_BAND,
  MAX_SIGMOID_MIDPOINT,
  MIN_SIGMOID_MIDPOINT,
} from './opportunity_scale';

/**
 * Backend response shape from `/temporal/{year}` containing 24x12 month-hour
 * matrices of contrail CO2e impact and flight distance.
 */
export declare interface TemporalDataResponse {
  readonly year: number;
  readonly isoA2: string;
  readonly name: string;
  readonly firId?: string;
  readonly firIcao?: string;
  readonly firName?: string;
  readonly isFir?: boolean;
  readonly months: readonly string[];
  readonly hours: readonly string[];
  readonly co2eGwp20Tonnes?: ReadonlyArray<readonly number[]>;
  readonly co2eGwp50Tonnes: ReadonlyArray<readonly number[]>;
  readonly co2eGwp100Tonnes?: ReadonlyArray<readonly number[]>;
  readonly totalFlightDistanceKm: ReadonlyArray<readonly number[]>;
  readonly contrailFlightDistanceKm: ReadonlyArray<readonly number[]>;
}

/**
 * Type guard verifying that `value` is a 2D array of numbers (`number[][]`).
 */
function isNumberMatrix(
  value: unknown,
): value is ReadonlyArray<readonly number[]> {
  return (
    Array.isArray(value) &&
    value.every(
      (row) =>
        Array.isArray(row) && row.every((cell) => typeof cell === 'number'),
    )
  );
}

/**
 * Reads the first matching key from `raw` that holds a valid 2D number matrix.
 */
function readNumberMatrix(
  raw: Readonly<Record<string, unknown>>,
  camelKey: string,
  snakeKey: string,
): ReadonlyArray<readonly number[]> | undefined {
  if (isNumberMatrix(raw[camelKey])) return raw[camelKey];
  if (isNumberMatrix(raw[snakeKey])) return raw[snakeKey];
  return undefined;
}

/**
 * Extracts a `readonly string[]` from `value`, filtering out any non-string
 * elements.
 */
function readStringArray(value: unknown): readonly string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string');
}

/**
 * Validates and normalizes a `/temporal/{year}` response payload into
 * `TemporalDataResponse`, accepting either raw wire `snake_case` keys or
 * already-normalized `camelCase` keys, or returns `null` when malformed.
 * @visibleForTesting
 */
export function normalizeTemporalData(
  candidate: unknown,
): TemporalDataResponse | null {
  if (!isRecordObject(candidate)) return null;
  if (!isValidYear(candidate['year'])) {
    return null;
  }
  const co2eGwp50Tonnes = readNumberMatrix(
    candidate,
    'co2eGwp50Tonnes',
    'co2e_gwp50_tonnes',
  );
  const totalFlightDistanceKm = readNumberMatrix(
    candidate,
    'totalFlightDistanceKm',
    'total_flight_distance_km',
  );
  const contrailFlightDistanceKm = readNumberMatrix(
    candidate,
    'contrailFlightDistanceKm',
    'contrail_flight_distance_km',
  );
  if (
    co2eGwp50Tonnes === undefined ||
    totalFlightDistanceKm === undefined ||
    contrailFlightDistanceKm === undefined
  ) {
    return null;
  }

  const isoA2 =
    typeof candidate['isoA2'] === 'string'
      ? candidate['isoA2']
      : typeof candidate['iso_a2'] === 'string'
        ? candidate['iso_a2']
        : '';
  const name = typeof candidate['name'] === 'string' ? candidate['name'] : '';
  const firId =
    typeof candidate['firId'] === 'string'
      ? candidate['firId']
      : typeof candidate['fir_id'] === 'string'
        ? candidate['fir_id']
        : undefined;
  const firIcao =
    typeof candidate['firIcao'] === 'string'
      ? candidate['firIcao']
      : typeof candidate['fir_icao'] === 'string'
        ? candidate['fir_icao']
        : undefined;
  const firName =
    typeof candidate['firName'] === 'string'
      ? candidate['firName']
      : typeof candidate['fir_name'] === 'string'
        ? candidate['fir_name']
        : typeof candidate['firname'] === 'string'
          ? candidate['firname']
          : undefined;
  const isFir =
    typeof candidate['isFir'] === 'boolean'
      ? candidate['isFir']
      : typeof candidate['is_fir'] === 'boolean'
        ? candidate['is_fir']
        : undefined;
  const months = readStringArray(candidate['months']);
  const hours = readStringArray(candidate['hours']);
  const co2eGwp20Tonnes = readNumberMatrix(
    candidate,
    'co2eGwp20Tonnes',
    'co2e_gwp20_tonnes',
  );
  const co2eGwp100Tonnes = readNumberMatrix(
    candidate,
    'co2eGwp100Tonnes',
    'co2e_gwp100_tonnes',
  );

  return {
    year: candidate['year'],
    isoA2,
    name,
    ...(firId !== undefined ? {firId} : {}),
    ...(firIcao !== undefined ? {firIcao} : {}),
    ...(firName !== undefined ? {firName} : {}),
    ...(isFir !== undefined ? {isFir} : {}),
    months,
    hours,
    ...(co2eGwp20Tonnes !== undefined ? {co2eGwp20Tonnes} : {}),
    co2eGwp50Tonnes,
    ...(co2eGwp100Tonnes !== undefined ? {co2eGwp100Tonnes} : {}),
    totalFlightDistanceKm,
    contrailFlightDistanceKm,
  };
}

/**
 * Datum for a single `(hourIdx, monthIdx)` cell in the 24x12 temporal heatmap.
 */
export interface HeatmapCell {
  readonly hourIdx: number;
  readonly monthIdx: number;
  readonly hourStr: string;
  readonly monthStr: string;
  readonly val: number;
  readonly score?: number;
  readonly congestion?: number;
  readonly normImpact?: number;
  /** Pre-sigmoid congestion: daily traffic / peak capacity proxy. */
  readonly rawCongestion?: number;
  /** Pre-sigmoid impact: percentile rank of this cell's daily impact. */
  readonly impactPercentile?: number;
  readonly dailyCo2e: number;
  readonly dailyFlightDistanceKm: number;
}

/**
 * Default 24-hour axis labels (`00:00` through `23:00`).
 * @visibleForTesting
 */
export const DEFAULT_HOURS: readonly string[] = Array.from(
  {length: 24},
  (_, h) => `${String(h).padStart(2, '0')}:00`,
);

const DEFAULT_HOUR_LABELS: readonly string[] = DEFAULT_HOURS.map(
  (hStr) => hStr.split(':')[0],
);

/**
 * Minimum hourly flight-distance capacity floor (km/day) for congestion
 * normalization.
 */
const MIN_HOURLY_CAPACITY_FLOOR = 5;

/**
 * Formats an hour-axis label as `HH:00`, appending `:00` when given a bare
 * hour string (such as `'05'`) and preserving labels that already include `:`.
 * @visibleForTesting
 */
export function formatHourOfDay(hourLabel: string): string {
  return hourLabel.includes(':') ? hourLabel : `${hourLabel}:00`;
}

/**
 * Resolves the 24 two-digit hour-axis labels (`'00'` through `'23'`) from a
 * temporal response, stripping any `:00` suffix from `temporalData.hours` and
 * falling back to `DEFAULT_HOUR_LABELS` when `temporalData` is `null` or
 * `temporalData.hours.length !== 24`.
 */
export function resolveHourLabels(
  temporalData: TemporalDataResponse | null,
): readonly string[] {
  if (temporalData?.hours && temporalData.hours.length === 24) {
    return temporalData.hours.map(
      (hStr, idx) =>
        hStr.trim().split(':')[0].trim() || DEFAULT_HOUR_LABELS[idx],
    );
  }
  return DEFAULT_HOUR_LABELS;
}

/**
 * Returns the number of days in a given month.
 * @visibleForTesting
 */
export function getDaysInMonth(year: number, monthIndex: number): number {
  return new Date(year, monthIndex + 1, 0).getDate();
}

/**
 * Resolved target FIR identity used for temporal API requests and header
 * labels.
 */
export declare interface TargetFir {
  readonly firId: string;
  readonly icao: string;
  readonly firName?: string;
}

/**
 * Minimal FIR record candidate inspected when resolving the active target FIR.
 */
export declare interface FirCandidate {
  readonly firId: string;
  readonly firName?: string;
  readonly firIcao?: string;
}

/**
 * Resolves the target FIR for temporal queries from `selectedFirId` and the
 * country's controlled FIR records.
 * @visibleForTesting
 */
export function resolveTargetFir(
  selectedFirId: string | null | undefined,
  firRecords: readonly FirCandidate[],
): TargetFir | null {
  if (selectedFirId) {
    const selIdClean = selectedFirId.trim().toUpperCase();
    const matched = firRecords.find(
      (r) => r.firId != null && r.firId.trim().toUpperCase() === selIdClean,
    );
    if (matched) {
      return {
        firId: matched.firId,
        icao: matched.firIcao || extractIcaoFromFirId(matched.firId),
        firName: matched.firName,
      };
    }
    if (firRecords.length === 0) {
      return {
        firId: selectedFirId,
        icao: extractIcaoFromFirId(selectedFirId),
      };
    }
  }

  if (firRecords.length === 0) return null;
  const first = firRecords[0];
  return first?.firId
    ? {
        firId: first.firId,
        icao: first.firIcao || extractIcaoFromFirId(first.firId),
        firName: first.firName,
      }
    : null;
}

/**
 * Formats the subtitle shown when temporal density exports are unavailable for
 * a year.
 * @visibleForTesting
 */
export function formatTemporalNoDataText(year: number): string {
  return `Temporal density exports for ${year} are not yet available.`;
}

/** Parameters for constructing the `/temporal/{year}` backend endpoint URL. */
export interface BuildTemporalApiUrlOptions {
  readonly year: number;
  readonly country?: string;
  readonly targetFir?: TargetFir | null;
}

/**
 * Builds the `/temporal/{year}` request URL with `fir_id` (for FIR queries) or
 * `iso` (for country queries). Throws when neither a valid 2-letter country ISO
 * code nor a FIR ID can be resolved: the backend has no aggregate over all
 * countries, so such a request is a caller bug.
 * @visibleForTesting
 */
export function buildTemporalApiUrl(
  options: BuildTemporalApiUrlOptions,
): string {
  const yearParam = requireYear(options.year, 'buildTemporalApiUrl');
  const params = new URLSearchParams();

  const firId = options.targetFir?.firId?.trim() ?? '';
  if (firId) {
    params.set('fir_id', firId);
  } else {
    const country = (options.country ?? '').trim();
    const resolvedIso = /^[A-Za-z]{2}$/.test(country)
      ? country.toUpperCase()
      : country
        ? getIso2FromCountry(country, yearParam)
        : '';
    if (resolvedIso) {
      params.set('iso', resolvedIso);
    }
  }
  if (!params.has('iso') && !params.has('fir_id')) {
    throw new Error('buildTemporalApiUrl: a country or FIR is required.');
  }

  return `/temporal/${encodeURIComponent(yearParam)}?${params.toString()}`;
}

/**
 * Resolves the display title for the temporal heatmap header.
 * @visibleForTesting
 */
export function resolveDisplayTitle(options: {
  readonly temporalDataName?: string;
  readonly isFirData?: boolean;
  readonly targetFir?: TargetFir | null;
  readonly country?: string;
}): string {
  if (options.temporalDataName) return options.temporalDataName;
  if (options.isFirData !== false && options.targetFir) {
    return formatFirDisplayName(
      options.targetFir.firName,
      options.targetFir.icao,
    );
  }
  return options.country ?? '';
}

/** Entity metadata subset from a `/temporal/{year}` response. */
export declare interface TemporalEntityMetadata {
  readonly name?: string;
  readonly isFir?: boolean;
  readonly firIcao?: string;
  readonly firName?: string;
}

/**
 * Resolves the human-readable entity label for the temporal heatmap subtitle.
 * When the backend response explicitly indicates country fallback data
 * (`isFir === false`), avoids misattributing the matrix to `targetFir`.
 * @visibleForTesting
 */
export function resolveTemporalEntityLabel(options: {
  readonly temporalData?: TemporalEntityMetadata | null;
  readonly targetFir?: TargetFir | null;
  readonly selectedFirId?: string | null;
  readonly country?: string;
}): string {
  const {temporalData, targetFir, selectedFirId, country} = options;
  if (temporalData?.isFir === false) {
    return temporalData.name || country || '';
  }
  const raw = temporalData?.firName || targetFir?.firName;
  const icao =
    temporalData?.firIcao ||
    targetFir?.icao ||
    (selectedFirId ? extractIcaoFromFirId(selectedFirId) : undefined);
  if (raw) {
    return formatFirDisplayName(raw, icao ?? undefined);
  }
  if (icao) {
    return `FIR ${icao}`;
  }
  return 'this FIR';
}

/** Temporal dataset fields inspected when resolving the CSV export filename. */
export declare interface TemporalCsvDatasetMetadata {
  readonly year: number;
  readonly name?: string;
  readonly isFir?: boolean;
  readonly firIcao?: string;
  readonly firName?: string;
}

/**
 * Resolves CSV filename parameters from the loaded temporal dataset and active
 * selection, ensuring country fallback datasets (`isFir === false`) are
 * not exported under the target FIR's ICAO code or name.
 * @visibleForTesting
 */
export function resolveTemporalCsvFilenameOptions(options: {
  readonly temporalData: TemporalCsvDatasetMetadata;
  readonly targetFir?: TargetFir | null;
  readonly selectedFirId?: string | null;
  readonly fallbackYear: number;
}): BuildTemporalCsvFilenameOptions {
  const {temporalData, targetFir, selectedFirId, fallbackYear} = options;
  const year = isValidYear(temporalData.year)
    ? temporalData.year
    : fallbackYear;
  if (temporalData.isFir === false) {
    return {
      firIcao: null,
      firName: temporalData.name ?? null,
      year,
    };
  }
  return {
    firIcao:
      temporalData.firIcao ||
      targetFir?.icao ||
      (selectedFirId ? extractIcaoFromFirId(selectedFirId) : undefined),
    firName: temporalData.firName || targetFir?.firName || temporalData.name,
    year,
  };
}

/**
 * Computed Contrail Avoidance Opportunity Index matrices and normalization
 * intermediates.
 */
export interface OpportunityDataResult {
  readonly matrix: ReadonlyArray<readonly number[]>;
  readonly congestionMatrix: ReadonlyArray<readonly number[]>;
  readonly normImpactMatrix: ReadonlyArray<readonly number[]>;
  /** Pre-sigmoid congestion ratios (daily traffic / peak capacity proxy). */
  readonly rawCongestionMatrix: ReadonlyArray<readonly number[]>;
  /** Pre-sigmoid impact percentile ranks in [0, 1). */
  readonly impactPercentileMatrix: ReadonlyArray<readonly number[]>;
  readonly maxScore: number;
}

/**
 * Slider-controlled inflection points and optional precomputed baseline
 * matrices for `computeOpportunityData`.
 */
export interface OpportunityMidpoints {
  /** Congestion ratio at which the congestion sigmoid outputs 0.5. */
  readonly congestionMidpoint?: number;
  /** Impact percentile at which the impact sigmoid outputs 0.5. */
  readonly impactMidpoint?: number;
  /**
   * Optional precomputed 24x12 pre-sigmoid congestion ratios; when provided
   * alongside `impactPercentileMatrix`, `computeOpportunityData` skips
   * recomputing and sorting the 288-cell baseline matrices during slider
   * drags and preserves their array references in the returned result.
   */
  readonly rawCongestionMatrix?: ReadonlyArray<readonly number[]>;
  /** Optional precomputed 24x12 pre-sigmoid impact percentile ranks. */
  readonly impactPercentileMatrix?: ReadonlyArray<readonly number[]>;
}

/**
 * Minimal temporal dataset shape required to compute opportunity matrices and
 * CSV rows.
 */
export declare interface TemporalDatasetForOpportunity {
  readonly year: number;
  readonly months?: readonly string[];
  readonly hours?: readonly string[];
  readonly co2eGwp20Tonnes?: ReadonlyArray<readonly number[]>;
  readonly co2eGwp50Tonnes?: ReadonlyArray<readonly number[]>;
  readonly co2eGwp100Tonnes?: ReadonlyArray<readonly number[]>;
  readonly totalFlightDistanceKm: ReadonlyArray<readonly number[]>;
}

/** Options for formatting a temporal CSV export filename. */
export interface BuildTemporalCsvFilenameOptions {
  readonly firIcao?: string | null;
  readonly firName?: string | null;
  readonly year: number;
}

/**
 * Builds the CSV export filename
 * `fir_{FIR ICAO code}_{lowercase_stripped_fir_name}_{year}.csv`.
 * @visibleForTesting
 */
export function buildTemporalCsvFilename(
  options: BuildTemporalCsvFilenameOptions,
): string {
  const icao = (options.firIcao ?? '').trim();
  const year = requireYear(options.year, 'buildTemporalCsvFilename');
  const cleanedName = (options.firName ?? '')
    .replace(/\s*\([^)]*\)\s*$/, '')
    .replace(/^FIR\s+/i, '')
    .replace(/\s+FIR$/i, '')
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '');

  if (icao && cleanedName && cleanedName !== icao.toLowerCase()) {
    return `fir_${icao}_${cleanedName}_${year}.csv`;
  }
  if (icao) {
    return `fir_${icao}_${year}.csv`;
  }
  if (cleanedName) {
    return `fir_${cleanedName}_${year}.csv`;
  }
  return `fir_${year}.csv`;
}

/** Options for building temporal CSV export content. */
export interface BuildTemporalCsvContentOptions {
  readonly temporalData: TemporalDatasetForOpportunity;
  readonly gwp?: string;
  readonly opportunityMatrix?: ReadonlyArray<readonly number[]>;
}

/**
 * Builds the CSV string for the contrail impact, airspace traffic, and contrail
 * avoidance opportunity matrices (288 month-hour rows in month-major order).
 * @visibleForTesting
 */
export function buildTemporalCsvContent(
  options: BuildTemporalCsvContentOptions,
): string {
  const {temporalData} = options;
  const normalizedGwp = (options.gwp || 'GWP50')
    .trim()
    .toUpperCase()
    .replace(/\s+/g, '');
  const gwpLabel =
    normalizedGwp === 'GWP20' || normalizedGwp === 'GWP100'
      ? normalizedGwp
      : 'GWP50';

  const yearNum = requireYear(temporalData.year, 'buildTemporalCsvContent');
  const daysInMonths = Array.from({length: 12}, (_, m) =>
    getDaysInMonth(yearNum, m),
  );

  const impactMatrix = resolveTemporalImpactMatrix(temporalData, gwpLabel);
  const opportunityMatrix =
    options.opportunityMatrix ??
    computeOpportunityData(temporalData, gwpLabel).matrix;

  const header = [
    'month',
    'local hour',
    `contrail t CO2e ${gwpLabel} / day`,
    'flight kilometers / day',
    'contrail opportunity index',
  ].join(',');

  const lines: string[] = [header];

  for (let m = 0; m < 12; m++) {
    const rawMonth = temporalData.months?.[m];
    const monthStr =
      rawMonth && /^\d{6}$/.test(rawMonth)
        ? rawMonth
        : `${yearNum}${String(m + 1).padStart(2, '0')}`;
    const days = daysInMonths[m] || 30;

    for (let h = 0; h < 24; h++) {
      const rawHour = temporalData.hours?.[h];
      const hourStr =
        rawHour && /^\d{2}:\d{2}$/.test(rawHour)
          ? rawHour
          : (DEFAULT_HOURS[h] ?? `${String(h).padStart(2, '0')}:00`);

      const rawImpact = impactMatrix?.[h]?.[m] ?? 0;
      const safeImpact = Number.isFinite(rawImpact) ? rawImpact : 0;
      const dailyImpact = days > 0 ? safeImpact / days : 0;

      const rawTraffic = temporalData.totalFlightDistanceKm[h]?.[m] ?? 0;
      const safeTraffic = Number.isFinite(rawTraffic) ? rawTraffic : 0;
      const dailyTrafficKm = days > 0 ? safeTraffic / days : 0;

      const rawOpportunity = opportunityMatrix?.[h]?.[m] ?? 0;
      const safeOpportunity = Number.isFinite(rawOpportunity)
        ? rawOpportunity
        : 0;

      lines.push(
        `${monthStr},${hourStr},${dailyImpact},` +
          `${dailyTrafficKm},${safeOpportunity}`,
      );
    }
  }

  return `${lines.join('\n')}\n`;
}

/**
 * Selects the 24x12 contrail CO2e impact matrix for the requested `gwp`
 * horizon.
 * @visibleForTesting
 */
export function resolveTemporalImpactMatrix(
  temporalData: TemporalDatasetForOpportunity | null | undefined,
  gwp = 'GWP50',
): ReadonlyArray<readonly number[]> {
  if (!temporalData) return [];
  if (gwp === 'GWP20' && temporalData.co2eGwp20Tonnes) {
    return temporalData.co2eGwp20Tonnes;
  }
  if (gwp === 'GWP100' && temporalData.co2eGwp100Tonnes) {
    return temporalData.co2eGwp100Tonnes;
  }
  return temporalData.co2eGwp50Tonnes ?? [];
}

/** Returns a 24x12 matrix initialized to zeros. */
function zeroMatrix(): number[][] {
  return Array.from({length: 24}, () => Array.from({length: 12}, () => 0));
}

/**
 * Computes the Contrail Avoidance Opportunity Index, (1 - congestion) x impact,
 * where both factors are logistic transforms of a normalized input:
 * - congestion: daily traffic / peak capacity proxy, sigmoid at
 *   `congestionMidpoint`.
 * - impact: percentile rank of daily contrail impact, sigmoid at
 *   `impactMidpoint`.
 * Both factors are in [0, 1], so the index is too.
 * @visibleForTesting
 */
export function computeOpportunityData(
  temporalData: TemporalDatasetForOpportunity | null | undefined,
  gwp = 'GWP50',
  midpoints: OpportunityMidpoints = {},
): OpportunityDataResult {
  const congestionMidpoint = clampSigmoidMidpoint(
    midpoints.congestionMidpoint ?? DEFAULT_CONGESTION_MIDPOINT,
  );
  const impactMidpoint = clampSigmoidMidpoint(
    midpoints.impactMidpoint ?? DEFAULT_IMPACT_PERCENTILE_MIDPOINT,
  );

  if (!temporalData) {
    return {
      matrix: zeroMatrix(),
      congestionMatrix: zeroMatrix(),
      normImpactMatrix: zeroMatrix(),
      rawCongestionMatrix: zeroMatrix(),
      impactPercentileMatrix: zeroMatrix(),
      maxScore: 1.0,
    };
  }

  let rawCongestionMatrix: ReadonlyArray<readonly number[]>;
  let impactPercentileMatrix: ReadonlyArray<readonly number[]>;

  if (midpoints.rawCongestionMatrix && midpoints.impactPercentileMatrix) {
    rawCongestionMatrix = midpoints.rawCongestionMatrix;
    impactPercentileMatrix = midpoints.impactPercentileMatrix;
  } else {
    const yearNum = requireYear(temporalData.year, 'computeOpportunityData');
    const daysInMonths = Array.from({length: 12}, (_, m) =>
      getDaysInMonth(yearNum, m),
    );
    const dailyRates: number[] = [];
    const dailyTrafficMatrix: number[][] = zeroMatrix();

    for (let h = 0; h < 24; h++) {
      for (let m = 0; m < 12; m++) {
        const rawSegs = temporalData.totalFlightDistanceKm[h]?.[m] ?? 0;
        const segs = Number.isFinite(rawSegs) ? rawSegs : 0;
        const days = daysInMonths[m] || 30;
        const rate = days > 0 ? segs / days : 0;
        dailyTrafficMatrix[h][m] = rate;
        if (rate > 0) {
          dailyRates.push(rate);
        }
      }
    }

    let maxDailyRate = 0;
    for (const rate of dailyRates) {
      if (rate > maxDailyRate) maxDailyRate = rate;
    }
    const capacityProxy = Math.max(
      MIN_HOURLY_CAPACITY_FLOOR,
      maxDailyRate > 0 ? maxDailyRate : 1,
    );

    // Impact is ranked by percentile below, and CO2e is a linear scaling of
    // energy forcing, so any selected GWP horizon yields the same percentile
    // ranks.
    const impactMatrix = resolveTemporalImpactMatrix(temporalData, gwp);
    const allDailyImpacts: number[] = [];
    for (let h = 0; h < 24; h++) {
      for (let m = 0; m < 12; m++) {
        const rawImpVal = impactMatrix?.[h]?.[m] ?? 0;
        const rawImp = Number.isFinite(rawImpVal) ? rawImpVal : 0;
        const days = daysInMonths[m] || 30;
        allDailyImpacts.push(Math.max(0, rawImp / days));
      }
    }
    // Percentile ranks are independent of the midpoint; only the sigmoid
    // applied to them moves when the slider moves.
    const impactPercentiles = computePercentileRanks(allDailyImpacts);

    const computedRawCongestion: number[][] = zeroMatrix();
    const computedImpactPercentiles: number[][] = zeroMatrix();
    for (let h = 0; h < 24; h++) {
      for (let m = 0; m < 12; m++) {
        computedRawCongestion[h][m] =
          capacityProxy > 0 ? dailyTrafficMatrix[h][m] / capacityProxy : 0;
        computedImpactPercentiles[h][m] = impactPercentiles[h * 12 + m] ?? 0;
      }
    }
    rawCongestionMatrix = computedRawCongestion;
    impactPercentileMatrix = computedImpactPercentiles;
  }

  const oppMatrix: number[][] = zeroMatrix();
  const congestionMatrix: number[][] = zeroMatrix();
  const normImpactMatrix: number[][] = zeroMatrix();
  let maxScore = 0;

  for (let h = 0; h < 24; h++) {
    for (let m = 0; m < 12; m++) {
      const rawCongestion = rawCongestionMatrix[h]?.[m] ?? 0;
      const congestion = computeSigmoidCongestion(
        rawCongestion,
        congestionMidpoint,
      );
      const impactPercentile = impactPercentileMatrix[h]?.[m] ?? 0;
      const normImpact = computeSigmoidImpact(impactPercentile, impactMidpoint);
      const score = (1.0 - congestion) * normImpact;
      oppMatrix[h][m] = score;
      congestionMatrix[h][m] = congestion;
      normImpactMatrix[h][m] = normImpact;
      if (score > maxScore) maxScore = score;
    }
  }

  return {
    matrix: oppMatrix,
    congestionMatrix,
    normImpactMatrix,
    rawCongestionMatrix,
    impactPercentileMatrix,
    maxScore: maxScore > 0 ? maxScore : 1.0,
  };
}

/**
 * Returns the CSS class name assigned to the `<rect>` at `(hourIdx, monthIdx)`.
 * @visibleForTesting
 */
export function getHeatmapCellClassName(
  hourIdx: number,
  monthIdx: number,
): string {
  return `heatmap-cell cell-h-${hourIdx}-m-${monthIdx}`;
}

/**
 * Declarative rule for outlining the cells that sit on the favourable side of a
 * slider's midpoint: below it for congestion, above it for impact percentile.
 */
export interface CellHighlightRule {
  readonly source: 'rawCongestion' | 'impactPercentile';
  readonly comparison: 'below' | 'above';
  readonly threshold: number;
}

/**
 * Whether a cell falls on the favourable side of the slider threshold.
 * Cells with no value for the rule's source are never highlighted.
 * @visibleForTesting
 */
export function shouldHighlightCell(
  cell: HeatmapCell,
  rule: CellHighlightRule | undefined,
): boolean {
  if (!rule) return false;
  const value =
    rule.source === 'rawCongestion'
      ? cell.rawCongestion
      : cell.impactPercentile;
  if (value === undefined || !Number.isFinite(value)) return false;
  if (rule.comparison === 'below') {
    if (rule.threshold <= MIN_SIGMOID_MIDPOINT) return false;
    if (rule.threshold >= MAX_SIGMOID_MIDPOINT) return true;
    return value < rule.threshold;
  }
  if (rule.threshold <= MIN_SIGMOID_MIDPOINT) return true;
  if (rule.threshold >= MAX_SIGMOID_MIDPOINT) return false;
  return value > rule.threshold;
}

/**
 * One side of one cell, in grid index space. `top` is the side facing
 * `hourIdx - 1`; the hour scale runs downward, so `top` is the smaller y.
 */
export interface CellEdge {
  readonly hourIdx: number;
  readonly monthIdx: number;
  readonly hourStr: string;
  readonly monthStr: string;
  readonly side: 'top' | 'bottom' | 'left' | 'right';
}

/**
 * Reduces the highlighted cells to the outline of the region they form: an
 * edge is emitted only where the neighbouring cell is *not* highlighted, or
 * where there is no neighbour (the grid border).
 *
 * Outlining every highlighted cell individually turns a large selection into a
 * mesh of boxes, which reads as noise rather than as a region. Dropping the
 * shared edges means a cell fully surrounded by highlighted neighbours
 * contributes no stroke at all.
 * @visibleForTesting
 */
export function computeHighlightBoundaryEdges(
  cells: readonly HeatmapCell[],
  rule: CellHighlightRule | undefined,
): CellEdge[] {
  if (!rule) return [];

  const cellKey = (hourIdx: number, monthIdx: number) =>
    `${hourIdx},${monthIdx}`;
  const highlighted = new Set<string>();
  for (const cell of cells) {
    if (shouldHighlightCell(cell, rule)) {
      highlighted.add(cellKey(cell.hourIdx, cell.monthIdx));
    }
  }

  const edges: CellEdge[] = [];
  for (const cell of cells) {
    const {hourIdx, monthIdx, hourStr, monthStr} = cell;
    if (!highlighted.has(cellKey(hourIdx, monthIdx))) continue;
    const base = {hourIdx, monthIdx, hourStr, monthStr};
    if (!highlighted.has(cellKey(hourIdx - 1, monthIdx))) {
      edges.push({...base, side: 'top'});
    }
    if (!highlighted.has(cellKey(hourIdx + 1, monthIdx))) {
      edges.push({...base, side: 'bottom'});
    }
    if (!highlighted.has(cellKey(hourIdx, monthIdx - 1))) {
      edges.push({...base, side: 'left'});
    }
    if (!highlighted.has(cellKey(hourIdx, monthIdx + 1))) {
      edges.push({...base, side: 'right'});
    }
  }
  return edges;
}

/** Flight-distance matrix required when flattening 24x12 heatmap cells. */
export interface TemporalFlightDistanceMatrices {
  readonly totalFlightDistanceKm: ReadonlyArray<readonly number[]>;
}

/**
 * Options for flattening a 24x12 temporal matrix into calendar-normalized
 * `HeatmapCell`s.
 */
export interface BuildHeatmapCellsOptions {
  readonly matrix: ReadonlyArray<readonly number[]>;
  readonly activeImpactMatrix: ReadonlyArray<readonly number[]>;
  readonly temporalData: TemporalFlightDistanceMatrices;
  readonly daysInMonths: readonly number[];
  readonly hourLabels: readonly string[];
  readonly monthLabels: readonly string[];
  readonly mode: 'opportunity' | 'impact' | 'count';
  readonly scoreMatrix?: ReadonlyArray<readonly number[]>;
  readonly congestionMatrix?: ReadonlyArray<readonly number[]>;
  readonly normImpactMatrix?: ReadonlyArray<readonly number[]>;
  readonly rawCongestionMatrix?: ReadonlyArray<readonly number[]>;
  readonly impactPercentileMatrix?: ReadonlyArray<readonly number[]>;
}

/**
 * Flattens a 24x12 temporal matrix into calendar-normalized HeatmapCells. Both
 * `impact` and `count` modes normalize monthly cell sums by `daysInMonths[m]`
 * so 31-day months and 28/29-day February are compared on an equal daily-rate
 * basis.
 * @visibleForTesting
 */
export function buildHeatmapCells(options: BuildHeatmapCellsOptions): {
  readonly cells: HeatmapCell[];
  readonly maxVal: number;
} {
  const {
    matrix,
    activeImpactMatrix,
    temporalData,
    daysInMonths,
    hourLabels,
    monthLabels,
    mode,
    scoreMatrix,
    congestionMatrix,
    normImpactMatrix,
    rawCongestionMatrix,
    impactPercentileMatrix,
  } = options;

  const cells: HeatmapCell[] = [];
  let maxVal = 0;
  for (let h = 0; h < 24; h++) {
    const matrixRow = matrix?.[h];
    const co2eRow = activeImpactMatrix?.[h];
    const totalDistRow = temporalData.totalFlightDistanceKm[h];
    const scoreRow = scoreMatrix?.[h];
    const congestionRow = congestionMatrix?.[h];
    const normImpactRow = normImpactMatrix?.[h];
    const rawCongestionRow = rawCongestionMatrix?.[h];
    const impactPercentileRow = impactPercentileMatrix?.[h];

    for (let m = 0; m < 12; m++) {
      const rawCellVal = matrixRow ? matrixRow[m] || 0 : 0;
      const rawVal = Number.isFinite(rawCellVal) ? rawCellVal : 0;
      const days = daysInMonths[m] || 30;
      const val =
        mode === 'count' || mode === 'impact' ? rawVal / days : rawVal;
      if (val > maxVal) maxVal = val;
      const rawCo2eVal = co2eRow ? co2eRow[m] || 0 : 0;
      const co2e = Number.isFinite(rawCo2eVal) ? rawCo2eVal : 0;
      const rawTotalDist = totalDistRow ? totalDistRow[m] || 0 : 0;
      const totalFlightDistanceKm = Number.isFinite(rawTotalDist)
        ? rawTotalDist
        : 0;
      const score = scoreRow?.[m] ?? 0;
      const congestion = congestionRow?.[m] ?? 0;
      const normImpact = normImpactRow?.[m] ?? 0;
      const rawCongestion = rawCongestionRow?.[m];
      const impactPercentile = impactPercentileRow?.[m];
      cells.push({
        hourIdx: h,
        monthIdx: m,
        hourStr: hourLabels[h] || h.toString().padStart(2, '0'),
        monthStr: monthLabels[m] || `M${m + 1}`,
        val,
        score,
        congestion,
        rawCongestion,
        impactPercentile,
        normImpact,
        dailyCo2e: co2e / days,
        dailyFlightDistanceKm: totalFlightDistanceKm / days,
      });
    }
  }

  return {
    cells,
    maxVal: maxVal > 0 ? maxVal : 1,
  };
}

const TEMPORAL_RESPONSE_CACHE = new Map<string, TemporalDataResponse>();
const MAX_TEMPORAL_CACHE_ENTRIES = 100;

/**
 * Reads an entry from the bounded temporal response cache, refreshing its LRU
 * position on a cache hit.
 */
export function getTemporalCacheEntry(
  key: string,
): TemporalDataResponse | undefined {
  return getBoundedCacheEntry(TEMPORAL_RESPONSE_CACHE, key);
}

/**
 * Stores a validated temporal response in the bounded session cache.
 * When a FIR-scoped request falls back to a country URL (or returns
 * a non-FIR fallback payload), avoids poisoning the FIR-specific cache key with
 * country-wide data.
 * @visibleForTesting
 */
export function cacheTemporalResponse(
  requestUrl: string,
  usedFallbackUrl: string | null,
  value: TemporalDataResponse,
): void {
  if (usedFallbackUrl) {
    setBoundedCacheEntry(
      TEMPORAL_RESPONSE_CACHE,
      usedFallbackUrl,
      value,
      MAX_TEMPORAL_CACHE_ENTRIES,
    );
    return;
  }
  if (value.isFir === false && requestUrl.includes('fir_id=')) {
    return;
  }
  setBoundedCacheEntry(
    TEMPORAL_RESPONSE_CACHE,
    requestUrl,
    value,
    MAX_TEMPORAL_CACHE_ENTRIES,
  );
}

/**
 * Clears all entries from the temporal response cache in unit tests.
 * @visibleForTesting
 */
export function clearTemporalCacheForTesting(): void {
  TEMPORAL_RESPONSE_CACHE.clear();
}

/**
 * SVG layout geometry for a single heatmap column. Shared by the D3 render and
 * by the sliders, which are inset to line up with the grid area rather than the
 * full SVG (which also carries the axis gutters and the colorbar legend).
 */
export const HEATMAP_MARGIN = {
  top: 40,
  right: 78,
  bottom: 68,
  left: 60,
} as const;

/** Width in SVG user units of the 12-month cell grid area. */
export const HEATMAP_GRID_WIDTH = 250;

/**
 * Height in SVG user units of the 24-hour cell grid area (12px per hourly row).
 */
export const HEATMAP_GRID_HEIGHT = 288;

/** Total viewBox width in SVG user units of a single heatmap column. */
export const HEATMAP_TOTAL_WIDTH =
  HEATMAP_GRID_WIDTH + HEATMAP_MARGIN.left + HEATMAP_MARGIN.right;

/** Formats a normalized [0, 1] midpoint as an integer percentage string. */
export function formatMidpointPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

/**
 * Describes where the ~0 / 0.5 / ~1 points of a sigmoid sit for a given
 * midpoint and transition half-width. Midpoints span the full [0, 1] range, so
 * near either end a shoulder can fall outside the input domain and be
 * unreachable by any cell; those terms are omitted rather than clamped, since a
 * clamped one would misreport the value the sigmoid actually takes there.
 * @visibleForTesting
 */
export function formatSigmoidCalibration(
  midpoint: number,
  band: number,
): string {
  const clamped = clampSigmoidMidpoint(midpoint);
  const low = clamped - band;
  const high = clamped + band;
  const terms: string[] = [];
  if (low >= 0) terms.push(`${formatMidpointPercent(low)} ≈ 0`);
  terms.push(`${formatMidpointPercent(clamped)} = 0.5`);
  if (high <= 1) terms.push(`${formatMidpointPercent(high)} ≈ 1`);
  return terms.join(' · ');
}

/**
 * Footnote describing how the opportunity index is currently calibrated. Both
 * inflection points are user-controlled, so the copy is derived from the live
 * midpoints rather than stating a fixed calibration. The two axes have
 * different transition widths, so each reports its own.
 * @visibleForTesting
 */
export function getOpportunityFootnoteText(
  congestionMidpoint: number,
  impactMidpoint: number,
): string {
  const congestion = clampSigmoidMidpoint(congestionMidpoint);
  const impact = clampSigmoidMidpoint(impactMidpoint);
  const congestionCalibration = formatSigmoidCalibration(
    congestion,
    CONGESTION_SIGMOID_BAND,
  );
  const impactCalibration = formatSigmoidCalibration(
    impact,
    IMPACT_SIGMOID_BAND,
  );
  return (
    '* Contrail Avoidance Opportunity Index highlights hours with high ' +
    'contrail warming potential occurring during low-congestion traffic ' +
    'windows (Score = [1 - Congestion] × Normalized Impact). Both factors ' +
    'are logistic transforms of a normalized input, and the slider under ' +
    'each grid sets that transform\u2019s inflection point. Congestion is ' +
    'daily traffic as a share of a peak-capacity proxy, currently centred ' +
    `at ${formatMidpointPercent(congestion)} (${congestionCalibration}); ` +
    'contrail impact is the percentile rank of a cell\u2019s daily impact, ' +
    `currently centred at ${formatMidpointPercent(impact)} ` +
    `(${impactCalibration}). The impact transform is the shallower of the ` +
    'two, so it grades across a wider span rather than acting as a ' +
    'near-hard threshold. Cells on the favourable side of a midpoint - ' +
    'below it for congestion, above it for impact - are outlined in the ' +
    'grid above that slider. In the hover status badge, component scores ' +
    'are color-coded: Congestion is green when low (favorable airspace ' +
    'capacity) and red when high (airspace saturated); Contrail Impact is ' +
    'green when high (large warming mitigation potential) and red when low.'
  );
}

/**
 * Snapshot of the currently hovered heatmap cell for the header status badge.
 */
export interface HoveredCellInfo {
  readonly month: string;
  readonly hour: string;
  readonly hourIdx: number;
  readonly monthIdx: number;
  readonly dailyCo2e: number;
  readonly dailyFlightDistanceKm: number;
  readonly score?: number;
  readonly congestion?: number;
  readonly rawCongestion?: number;
  readonly normImpact?: number;
}

function hasMatchingFiniteCell(
  matrix: ReadonlyArray<readonly number[]> | undefined,
  predicate: (val: number) => boolean,
): boolean {
  for (const row of matrix ?? []) {
    for (const val of row ?? []) {
      if (Number.isFinite(val) && predicate(val)) {
        return true;
      }
    }
  }
  return false;
}

/**
 * Determines whether a temporal dataset contains any non-zero contrail impact
 * or positive flight distance values for the given GWP horizon.
 * @visibleForTesting
 */
export function computeHasTemporalData(
  temporalData: TemporalDataResponse | null,
  gwp: string,
): boolean {
  if (!temporalData) return false;
  const impactMatrix = resolveTemporalImpactMatrix(temporalData, gwp);
  return (
    hasMatchingFiniteCell(impactMatrix, (v) => v !== 0) ||
    hasMatchingFiniteCell(temporalData.totalFlightDistanceKm, (v) => v > 0)
  );
}

/**
 * Builds the SVG path data string (`M...H...` / `M...V...`) for the
 * boundary edges of the highlighted threshold region.
 * @visibleForTesting
 */
export function buildThresholdOutlinePath(
  boundaryEdges: readonly CellEdge[],
  xPos: (monthStr: string) => number | undefined,
  yPos: (hourStr: string) => number | undefined,
  cellWidth: number,
  cellHeight: number,
): string {
  return boundaryEdges
    .map((edge: CellEdge) => {
      const x = xPos(edge.monthStr) ?? 0;
      const y = yPos(edge.hourStr) ?? 0;
      switch (edge.side) {
        case 'top':
          return `M${x},${y}H${x + cellWidth}`;
        case 'bottom':
          return `M${x},${y + cellHeight}H${x + cellWidth}`;
        case 'left':
          return `M${x},${y}V${y + cellHeight}`;
        case 'right':
          return `M${x + cellWidth},${y}V${y + cellHeight}`;
        default: {
          const unhandledSide: never = edge.side;
          throw new Error(`Unhandled cell edge side: ${String(unhandledSide)}`);
        }
      }
    })
    .join('');
}

/**
 * Neutral warm-white fill color used for zero or net-cooling cells in
 * `'impact'` and `'count'` heatmap grids.
 * @visibleForTesting
 */
export const HEATMAP_ZERO_IMPACT_FILL = '#fffdfa';

/**
 * Resolves the SVG fill color for a heatmap cell, using
 * `HEATMAP_ZERO_IMPACT_FILL` (`#fffdfa`) for non-positive values in `'impact'`
 * and `'count'` modes so net-cooling (`val < 0`) or zero cells are never passed
 * to the positive `[0, maxVal]` sequential color scale.
 */
export function resolveHeatmapCellFill(
  cell: HeatmapCell,
  mode: 'opportunity' | 'impact' | 'count',
  colorScale: (val: number) => string,
): string {
  if (mode === 'opportunity') {
    const score = cell.score ?? 0;
    return colorScale(Number.isFinite(score) ? score : 0);
  }
  return !Number.isFinite(cell.val) || cell.val <= 0
    ? HEATMAP_ZERO_IMPACT_FILL
    : colorScale(cell.val);
}
