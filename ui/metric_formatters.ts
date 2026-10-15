/**
 * @fileoverview Pure formatting, unit label, ranking, and year-over-year
 * indicator helpers for contrail metrics.
 */

/** Year-over-year directional change badge metadata. */
export interface YoYIndicator {
  readonly text: string;
  readonly direction: 'up' | 'down' | 'neutral';
  readonly color: string;
}

/**
 * Computes a relative percentage YoY indicator from current and previous
 * values.
 * @visibleForTesting
 */
export function computeValueYoY(
  current: number | null | undefined,
  previous: number | null | undefined,
): YoYIndicator | undefined {
  if (
    typeof current !== 'number' ||
    typeof previous !== 'number' ||
    !Number.isFinite(current) ||
    !Number.isFinite(previous) ||
    previous === 0
  ) {
    return undefined;
  }
  const percentChange = ((current - previous) / Math.abs(previous)) * 100;
  if (percentChange > 0.05) {
    return {
      text: `▲ +${percentChange.toFixed(1)}% YoY`,
      direction: 'up',
      color: '#d93025',
    };
  }
  if (percentChange < -0.05) {
    return {
      text: `▼ ${percentChange.toFixed(1)}% YoY`,
      direction: 'down',
      color: '#188038',
    };
  }
  return {
    text: '0.0% YoY',
    direction: 'neutral',
    color: '#5f6368',
  };
}

/** FIR airspace classification for domestic (over-land / national) sectors. */
export const FIR_TYPE_DOMESTIC = 'domestic';

/** FIR airspace classification for oceanic / international-waters sectors. */
export const FIR_TYPE_INTERNATIONAL = 'international';

/**
 * Formats an FIR name and optional ICAO code consistently (e.g.
 * `"FIR LANGEN (EDGG)"`).
 */
export function formatFirDisplayName(firName?: string, icao?: string): string {
  const clean = (firName || '').trim();
  const cleanIcao = (icao || '').trim();
  if (!clean) return cleanIcao;
  const base = clean.toUpperCase().startsWith('FIR') ? clean : `FIR ${clean}`;
  return cleanIcao ? `${base} (${cleanIcao})` : base;
}

/**
 * Extracts the leading ICAO prefix from a composite `fir_id` (`{ICAO}_{NAME}`).
 */
export function extractIcaoFromFirId(firId: string): string {
  const [icao = ''] = firId.split('_', 1);
  return icao;
}

/**
 * Reads a dynamically keyed property from a record object.
 */
export function getRecordField(
  record: Readonly<Record<string, unknown>>,
  key: string,
): unknown {
  return record[key];
}

/**
 * Reads a finite numeric property from a record object, returning `undefined`
 * when absent, nullish, or non-finite.
 */
export function getNumericRecordField(
  record: Readonly<Record<string, unknown>>,
  key: string,
): number | undefined {
  const value = getRecordField(record, key);
  return typeof value === 'number' && Number.isFinite(value)
    ? value
    : undefined;
}

/**
 * Machine-readable unit identifiers. These mirror the UNIT_* constants in
 * server/metrics.py and are matched by equality, so they must never be
 * translated or prettified. Display text comes from `unitDisplayLabel`.
 * @visibleForTesting
 */
export const UNIT_TONNES_CO2E = 'tonnes_co2e';
/** @visibleForTesting */
export const UNIT_TONNES_CO2E_PER_CAPITA = 'tonnes_co2e_per_capita';
/** Machine-readable unit identifier for radiative forcing density (`mW/m²`). */
export const UNIT_MILLIWATTS_PER_SQ_M = 'milliwatts_per_square_metre';
/** Machine-readable unit identifier for share of national GHG inventory. */
export const UNIT_RATIO_OF_NATIONAL_GHG = 'ratio_of_national_ghg';
/**
 * Machine-readable unit identifier for ratio vs. global average GHG per capita.
 */
export const UNIT_RATIO_OF_WORLD_AVG_GHG_PER_CAPITA =
  'ratio_of_world_avg_ghg_per_capita';
/** @visibleForTesting */
export const UNIT_MEGATONNES_CO2E = 'megatonnes_co2e';
/**
 * Client-only: FIR polygon areas, which never come from the metric endpoints.
 * @visibleForTesting
 */
export const UNIT_SQUARE_KILOMETRES = 'square_kilometres';

const NUMBER_FORMATTER_CACHE = new Map<number, Intl.NumberFormat>();

/**
 * Formats a finite number to `fractionDigits` decimal places in the active
 * locale using a cached `Intl.NumberFormat` instance (including locale decimal
 * and grouping separators), normalizing `-0` and sub-half-ULP magnitudes to
 * `0`.
 */
function formatNumber(value: number, fractionDigits: number): string {
  const safeDigits = Number.isFinite(fractionDigits)
    ? Math.max(0, Math.min(20, Math.trunc(fractionDigits)))
    : 0;
  let formatter = NUMBER_FORMATTER_CACHE.get(safeDigits);
  if (!formatter) {
    formatter = new Intl.NumberFormat(undefined, {
      minimumFractionDigits: safeDigits,
      maximumFractionDigits: safeDigits,
    });
    NUMBER_FORMATTER_CACHE.set(safeDigits, formatter);
  }
  if (!Number.isFinite(value)) {
    return formatter.format(0);
  }
  const normalized =
    Object.is(value, -0) || Math.abs(value) < 0.5 * Math.pow(10, -safeDigits)
      ? 0
      : value;
  return formatter.format(normalized);
}

/**
 * Formats a CO2e mass in tonnes using SI unit prefixes (`t`, `Kt`, `Mt`, `Gt`).
 *
 * Nullish or non-finite inputs coerce to `'0.0 t CO₂e'`. Callers that need to
 * distinguish missing data from zero should use `formatTonnesOrNotAvailable`.
 */
export function formatTonnesCo2e(
  tonnes: number | null | undefined,
  unitSuffix = 'CO₂e',
): string {
  if (tonnes == null || !Number.isFinite(tonnes)) {
    return `0.0 t ${unitSuffix}`.trim();
  }
  const abs = Math.abs(tonnes);
  if (abs >= 1_000_000_000) {
    const gigatonnes = formatNumber(tonnes / 1_000_000_000, 1);
    return `${gigatonnes} Gt ${unitSuffix}`.trim();
  } else if (abs >= 1_000_000) {
    return `${formatNumber(tonnes / 1_000_000, 1)} Mt ${unitSuffix}`.trim();
  } else if (abs >= 1_000) {
    return `${formatNumber(tonnes / 1_000, 1)} Kt ${unitSuffix}`.trim();
  } else if (abs < 1) {
    return `${formatNumber(tonnes, 2)} t ${unitSuffix}`.trim();
  } else {
    return `${formatNumber(tonnes, 1)} t ${unitSuffix}`.trim();
  }
}

/**
 * Formats a percentage (or fraction when `isFraction` is true) to `decimals`
 * fractional digits, coercing nullish or non-finite inputs to zero formatted to
 * `decimals` digits (`'0.0%'` by default). Callers that need to distinguish
 * missing data from zero should use `formatRatioPercentOrNotAvailable`.
 * @visibleForTesting
 */
export function formatPercentage(
  percentageOrValue: number | null | undefined,
  isFraction = false,
  decimals = 1,
): string {
  if (percentageOrValue == null || !Number.isFinite(percentageOrValue)) {
    return `${formatNumber(0, decimals)}%`;
  }
  const percentage = isFraction ? percentageOrValue * 100 : percentageOrValue;
  return `${formatNumber(percentage, decimals)}%`;
}

/**
 * Placeholder for a metric that is undefined for an entity (e.g. a per-area
 * metric for a country or FIR without area). Distinct from a genuine zero.
 */
export const NOT_AVAILABLE_LABEL = 'N/A';

/**
 * Formats a radiative forcing density value in `mW/m²`, or returns `'N/A'` when
 * nullish or non-finite.
 */
export function formatMwPerSqM(val: number | null | undefined): string {
  if (val == null || !Number.isFinite(val)) {
    return NOT_AVAILABLE_LABEL;
  }
  const abs = Math.abs(val);
  if (abs >= 10) {
    return `${formatNumber(val, 1)} mW/m²`;
  } else if (abs >= 1) {
    return `${formatNumber(val, 2)} mW/m²`;
  } else if (abs > 0) {
    return `${formatNumber(val, 3)} mW/m²`;
  }
  return `${formatNumber(0, 1)} mW/m²`;
}

/** Like `formatTonnesCo2e`, but renders a missing value as N/A instead of 0. */
export function formatTonnesOrNotAvailable(
  val: number | null | undefined,
): string {
  return val == null ? NOT_AVAILABLE_LABEL : formatTonnesCo2e(val);
}

/**
 * Renders a published ratio (e.g. share of national GHG) as `x.xx%`, or N/A.
 */
export function formatRatioPercentOrNotAvailable(
  ratio: number | undefined,
): string {
  return ratio === undefined
    ? NOT_AVAILABLE_LABEL
    : `${(ratio * 100).toFixed(2)}%`;
}

/**
 * Renders a metric value for display.
 *
 * The server sends a number and a unit identifier and does no formatting of its
 * own, so this is the single place a published figure becomes text.
 */
export function formatMetricValue(
  value: number | null | undefined,
  unit: string | null | undefined,
): string {
  switch (unit) {
    case UNIT_MILLIWATTS_PER_SQ_M:
      return formatMwPerSqM(value);
    case UNIT_RATIO_OF_NATIONAL_GHG:
    case UNIT_RATIO_OF_WORLD_AVG_GHG_PER_CAPITA:
      return formatPercentage(value, true, 2);
    case UNIT_SQUARE_KILOMETRES:
      return value == null || !Number.isFinite(value) || value < 0
        ? ''
        : `${formatNumber(Math.round(value), 0)} km²`;
    case UNIT_MEGATONNES_CO2E:
      return formatTonnesCo2e(
        value == null || !Number.isFinite(value) ? value : value * 1e6,
      );
    case UNIT_TONNES_CO2E:
    case UNIT_TONNES_CO2E_PER_CAPITA:
    default:
      return formatTonnesCo2e(value);
  }
}

/**
 * Renders the supporting figure shown beneath a ranked value. Unlike
 * `formatMetricValue`, this includes the comparison label (for example,
 * `'...% of global GHG/capita'`, `'... Mt national GHG'`, or
 * `'Area: ... km²'`).
 */
export function formatSecondaryValue(
  value: number | null | undefined,
  unit: string | null | undefined,
): string | undefined {
  if (value == null || !Number.isFinite(value)) {
    return undefined;
  }
  switch (unit) {
    case UNIT_RATIO_OF_WORLD_AVG_GHG_PER_CAPITA:
      return `${formatPercentage(value, true, 2)} of global GHG/capita`;
    case UNIT_MEGATONNES_CO2E:
      return `${formatNumber(value, 1)} Mt national GHG`;
    case UNIT_SQUARE_KILOMETRES:
      return value < 0
        ? undefined
        : `Area: ${formatNumber(Math.round(value), 0)} km²`;
    default:
      return formatMetricValue(value, unit);
  }
}

/** Axis and legend label for a unit identifier. */
export function unitDisplayLabel(unit: string | null | undefined): string {
  switch (unit) {
    case UNIT_MILLIWATTS_PER_SQ_M:
      return 'mW/m²';
    case UNIT_RATIO_OF_NATIONAL_GHG:
    case UNIT_RATIO_OF_WORLD_AVG_GHG_PER_CAPITA:
      return '%';
    case UNIT_TONNES_CO2E:
      return 't CO₂e';
    case UNIT_TONNES_CO2E_PER_CAPITA:
      return 't CO₂e / person';
    case UNIT_MEGATONNES_CO2E:
      return 'Mt CO₂e';
    case UNIT_SQUARE_KILOMETRES:
      return 'km²';
    default:
      return '';
  }
}

/** Minimal entity record shape holding optional precomputed rank columns. */
export declare interface RankedEntityRecord {
  readonly mean_rf_mw_per_sq_m_rank?: number | null;
  readonly co2e_gwp20_tonnes_rank?: number | null;
  readonly co2e_gwp50_tonnes_rank?: number | null;
  readonly co2e_gwp100_tonnes_rank?: number | null;
}

/**
 * Returns the rank columns whose maximum bounds how many entities a ranking
 * covers. The CO2e ranks are published per GWP horizon; taking the max over
 * all of them tolerates a dataset that lacks one horizon.
 */
function ranksForTotals(
  record: RankedEntityRecord,
): ReadonlyArray<number | null | undefined> {
  return [
    record.mean_rf_mw_per_sq_m_rank,
    record.co2e_gwp20_tonnes_rank,
    record.co2e_gwp50_tonnes_rank,
    record.co2e_gwp100_tonnes_rank,
  ];
}

/**
 * Returns how many entities the rankings in `records` cover.
 *
 * The pipeline can publish a rank that exceeds the row count (for example when
 * a ranked entity has no row of its own), so the result is the larger of the
 * row count and the highest finite rank returned by `ranksForTotals`.
 *
 * @visibleForTesting
 */
export function countRankedEntities(
  records: readonly RankedEntityRecord[],
): number {
  let total = records.length;
  for (const record of records) {
    for (const rank of ranksForTotals(record)) {
      if (typeof rank === 'number' && Number.isFinite(rank) && rank > total) {
        total = Math.floor(rank);
      }
    }
  }
  return total;
}

/**
 * Formats a rank as `#rank / total noun`, or just `#rank` when the total is
 * not known yet. A total of 0 is kept: the metrics panel renders `/ 0` as a
 * loading spinner.
 *
 * @visibleForTesting
 */
export function formatRankLabel(
  rank: number,
  total: number | null,
  noun: string,
): string {
  return total === null ? `#${rank}` : `#${rank} / ${total} ${noun}`;
}

/**
 * Parses a string consisting solely of ASCII decimal digits into a number, or
 * returns null for anything else (including the empty string, signs,
 * whitespace, and trailing characters that `parseInt` would silently ignore).
 */
export function parseNonNegativeInteger(text: string): number | null {
  if (!/^\d+$/.test(text)) return null;
  const value = Number(text);
  return Number.isSafeInteger(value) ? value : null;
}

/**
 * Extracts 1-based rank and total entity count from a formatted rank string.
 */
export function extractRankInfo(
  text: string,
): {readonly rankNum: number; readonly totalCount: number} | null {
  if (!text) return null;
  const match = text.match(/#?(\d+)\s*\/\s*(\d+)/);
  if (!match) return null;
  const [, rawRank = '', rawTotal = ''] = match;
  const rankNum = parseNonNegativeInteger(rawRank);
  const totalCount = parseNonNegativeInteger(rawTotal);
  if (rankNum === null || totalCount === null) return null;
  return {rankNum, totalCount};
}

/** Full month names (`'January'`..`'December'`) for timeline and UI labels. */
export const MONTH_FULL_NAMES: readonly string[] = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
];

/**
 * Abbreviated 3-letter month names (`'Jan'`..`'Dec'`) for heatmap axes and
 * timeline buttons.
 */
export const MONTH_SHORT_NAMES: readonly string[] = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
];
