/**
 * @fileoverview Pure scales behind the Contrail Avoidance Opportunity Index
 * heatmap: logistic (sigmoid) transforms for congestion and impact,
 * percentile ranking, cell colour palettes, hover badge status colours, and
 * slider-track gradients. No DOM, React, or d3 dependencies.
 */

/**
 * Output level at which a logistic is treated as saturated, i.e. what "≈ 1"
 * means in the slider captions. The mirrored low shoulder is 1 - this.
 */
const SIGMOID_SATURATION = 0.97;

/**
 * logit(SIGMOID_SATURATION); a band of `b` therefore needs slope `LOGIT / b`.
 */
const SIGMOID_SATURATION_LOGIT = Math.log(
  SIGMOID_SATURATION / (1 - SIGMOID_SATURATION),
);

/**
 * Converts a transition half-width into the logistic slope that reaches
 * `SIGMOID_SATURATION` exactly that far above the midpoint.
 * @visibleForTesting
 */
export function deriveSigmoidSlope(band: number): number {
  return SIGMOID_SATURATION_LOGIT / band;
}

/**
 * Transition half-width of each input sigmoid: the offset from the midpoint at
 * which the transform is saturated (~0 below, ~1 above). This is the shape
 * knob — a wider band is a shallower slope and grades more cells, a narrower
 * one is closer to a hard threshold.
 *
 * The two axes are calibrated independently. Congestion keeps the ±10% band the
 * capacity proxy was specified against. Impact is deliberately shallower: its
 * input is a percentile rank, which is uniform by construction, so a ±10% band
 * would leave only 20% of cells anywhere other than pinned at 0 or 1.
 *
 * Within an axis the slope is fixed, so moving a midpoint *translates* the
 * curve without reshaping it.
 */
export const CONGESTION_SIGMOID_BAND = 0.1;
/** Half-width of the impact-percentile sigmoid's transition band. */
export const IMPACT_SIGMOID_BAND = 0.25;

/**
 * Default congestion midpoint: 80% of the peak-traffic capacity proxy.
 */
export const DEFAULT_CONGESTION_MIDPOINT = 0.8;

/**
 * Default impact midpoint: the median (50th percentile) of daily contrail
 * impact.
 */
export const DEFAULT_IMPACT_PERCENTILE_MIDPOINT = 0.5;

/**
 * Midpoints span the whole normalized input domain. Within one band of either
 * end, that saturation shoulder falls outside [0, 1] and is unreachable by the
 * data; that is intentional, it is how a midpoint of 0 or 1 pushes every cell
 * onto one side of the transfer function.
 */
export const MIN_SIGMOID_MIDPOINT = 0.0;
/** Upper bound of the sigmoid midpoint sliders. */
export const MAX_SIGMOID_MIDPOINT = 1.0;

/**
 * Clamps a user-supplied sigmoid midpoint into the addressable range.
 */
export function clampSigmoidMidpoint(midpoint: number): number {
  if (!Number.isFinite(midpoint)) return DEFAULT_CONGESTION_MIDPOINT;
  return Math.min(
    MAX_SIGMOID_MIDPOINT,
    Math.max(MIN_SIGMOID_MIDPOINT, midpoint),
  );
}

/**
 * Evaluates a logistic at `x` for the given midpoint and transition half-width,
 * clamped to [0, 1]. At `midpoint ± band` the result is `SIGMOID_SATURATION`
 * and its mirror.
 * @visibleForTesting
 */
export function computeSigmoid(
  x: number,
  midpoint: number,
  band: number,
): number {
  if (!Number.isFinite(x)) return 0.0;
  const slope = deriveSigmoidSlope(band);
  const sigmoid =
    1.0 / (1.0 + Math.exp(-slope * (x - clampSigmoidMidpoint(midpoint))));
  return Math.min(1.0, Math.max(0.0, sigmoid));
}

/**
 * Computes a sigmoid-transformed congestion score from a raw congestion ratio
 * (daily traffic / peak capacity proxy). With the default midpoint of 0.80:
 * - At 70% traffic, output is near 0 (~0.03).
 * - At 80% traffic, output is 0.50 (inflection midpoint).
 * - At or above 90% traffic, output is near 1 (~0.97 to 1.0).
 * Moving the midpoint to X translates those three points to X-10%, X, X+10%.
 */
export function computeSigmoidCongestion(
  rawCongestion: number,
  midpoint: number = DEFAULT_CONGESTION_MIDPOINT,
): number {
  if (!Number.isFinite(rawCongestion) || rawCongestion <= 0) return 0.0;
  return computeSigmoid(rawCongestion, midpoint, CONGESTION_SIGMOID_BAND);
}

/**
 * Computes a sigmoid-transformed impact score from a percentile rank. Uses the
 * wider `IMPACT_SIGMOID_BAND`, so with the default 0.50 midpoint the 25th
 * percentile maps to ~0.03, the median to 0.50 and the 75th to ~0.97, with
 * everything between graded.
 */
export function computeSigmoidImpact(
  impactPercentile: number,
  midpoint: number = DEFAULT_IMPACT_PERCENTILE_MIDPOINT,
): number {
  if (!Number.isFinite(impactPercentile) || impactPercentile <= 0) return 0.0;
  return computeSigmoid(impactPercentile, midpoint, IMPACT_SIGMOID_BAND);
}

/**
 * Maps each value to its percentile rank: the fraction of the sample that is
 * strictly smaller than it, in [0, 1). Ties therefore share the rank of the
 * bottom of their run, so an all-zero sample yields all zeros (and, after the
 * impact sigmoid, zero normalized impact) rather than a degenerate scaling.
 * Non-finite entries are treated as 0.
 */
export function computePercentileRanks(values: readonly number[]): number[] {
  const count = values.length;
  if (count === 0) return [];
  const sanitized = values.map((v) => (Number.isFinite(v) ? v : 0));
  const sorted = [...sanitized].sort((a, b) => a - b);
  return sanitized.map((value) => {
    // Binary search for the first index whose value is >= `value`, which is
    // exactly the number of sample entries strictly below it.
    let lo = 0;
    let hi = count;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (sorted[mid] < value) {
        lo = mid + 1;
      } else {
        hi = mid;
      }
    }
    return lo / count;
  });
}

/**
 * Three-level green/amber/red hover badge colour for an opportunity score in
 * [0, 1].
 */
export function getOpportunityScoreColor(score: number): string {
  if (score >= 0.5) return '#137333'; // High opportunity = Good (Green)
  if (score >= 0.2) return '#b06000'; // Moderate opportunity (Amber)
  return '#c5221f'; // Low opportunity = Poor (Red)
}

/**
 * Three-level green/amber/red hover badge colour for a sigmoid congestion
 * value in [0, 1].
 */
export function getCongestionColor(congestion: number): string {
  // Low congestion = Favorable capacity for avoidance (Green)
  if (congestion <= 0.35) return '#137333';
  if (congestion <= 0.65) return '#b06000'; // Moderate congestion (Amber)
  // High congestion = Airspace saturated, difficult to divert (Red)
  return '#c5221f';
}

/**
 * Three-level green/amber/red hover badge colour for a normalized impact value
 * in [0, 1].
 */
export function getImpactColor(normImpact: number): string {
  // High impact = High mitigation potential (Green)
  if (normImpact >= 0.5) return '#137333';
  if (normImpact >= 0.2) return '#b06000'; // Moderate impact (Amber)
  return '#c5221f'; // Low impact = Little warming to mitigate (Red)
}

/**
 * Diagram title and unit label for the middle traffic heatmap.
 */
export const CONGESTION_DIAGRAM_LABELS = {
  title: 'Airspace traffic',
  unitLabel: 'Total Flight km / day',
} as const;

/**
 * Formats hover badge text for flight distance, appending `(Y% of maximum)`
 * from the cell's `rawCongestion` ratio (`dailyTraffic / capacityProxy`) so the
 * percentage matches the denominator used by the traffic slider.
 */
export function formatBadgeFlightText(
  distanceKm: number,
  rawCongestion?: number,
): string {
  const mainText =
    Math.round(distanceKm).toLocaleString() + ' total flight kilometers';
  if (rawCongestion !== undefined && Number.isFinite(rawCongestion)) {
    return `${mainText} (${Math.round(rawCongestion * 100)}% of maximum)`;
  }
  return mainText;
}

/**
 * ColorBrewer RdYlGn-11. Red = 0.0 (low opportunity), green = 1.0 (high
 * opportunity). Used both as the D3 fallback palette for the opportunity grid
 * and as the interpolation basis for the sigmoid slider tracks.
 */
export const RD_YL_GN_PALETTE: readonly string[] = [
  '#a50026',
  '#d73027',
  '#f46d43',
  '#fdae61',
  '#fee08b',
  '#ffffbf',
  '#d9ef8b',
  '#a6d96a',
  '#66bd63',
  '#1a9850',
  '#006837',
];

/**
 * The three stroke treatments a heatmap cell boundary can carry. They must stay
 * visually distinct: the threshold outline is persistent and marks a region,
 * while the hover stroke is transient and marks a single cell across all three
 * grids, so the hover stroke is both heavier and a different hue.
 */
export const CELL_STROKE_COLOR = '#ffffff';
/** Stroke width paired with `CELL_STROKE_COLOR`. */
export const CELL_STROKE_WIDTH = '0.5px';
/** Stroke colour of the threshold-region outline. */
export const THRESHOLD_STROKE_COLOR = '#202124';
/** Stroke width of the threshold-region outline. */
export const THRESHOLD_STROKE_WIDTH = '2px';
/** Stroke colour of the hovered cell. */
export const HOVER_STROKE_COLOR = '#174ea6';
/** Stroke width of the hovered cell. */
export const HOVER_STROKE_WIDTH = '3px';

/** Class on the non-interactive group holding the threshold outline path. */
export const THRESHOLD_OVERLAY_CLASS = 'heatmap-threshold-overlay';

/** OrRd palette for the impact and traffic grids. */
export const OR_RD_PALETTE: readonly string[] = [
  '#fff5eb',
  '#fee6ce',
  '#fdd0a2',
  '#fdae6b',
  '#fd8d3c',
  '#f16913',
  '#d94801',
  '#a63603',
  '#7f2704',
];

/** Red, green, and blue channel intensities in [0, 255]. */
interface RgbColor {
  readonly r: number;
  readonly g: number;
  readonly b: number;
}

/**
 * Parses a 6-digit `#rrggbb` hex colour string into its RGB channel components.
 */
function parseHexColor(hex: string): RgbColor {
  if (!/^#[0-9a-fA-F]{6}$/.test(hex)) {
    throw new Error(`Invalid #rrggbb colour: '${hex}'.`);
  }
  const value = Number(`0x${hex.slice(1)}`);
  return {
    r: (value >> 16) & 0xff,
    g: (value >> 8) & 0xff,
    b: value & 0xff,
  };
}

/**
 * Piecewise-linear interpolation over `RD_YL_GN_PALETTE`, matching what
 * `d3.interpolateRdYlGn` produces closely enough for a slider track while
 * avoiding a hard dependency on the d3-scale-chromatic build being present.
 * @visibleForTesting
 */
export function interpolateRdYlGnHex(t: number): string {
  const clamped = Number.isFinite(t) ? Math.min(1, Math.max(0, t)) : 0;
  const lastIdx = RD_YL_GN_PALETTE.length - 1;
  const pos = clamped * lastIdx;
  const lowIdx = Math.min(lastIdx, Math.floor(pos));
  const highIdx = Math.min(lastIdx, lowIdx + 1);
  const frac = pos - lowIdx;
  const low = parseHexColor(RD_YL_GN_PALETTE[lowIdx]);
  const high = parseHexColor(RD_YL_GN_PALETTE[highIdx]);
  const r = Math.round(low.r + frac * (high.r - low.r));
  const g = Math.round(low.g + frac * (high.g - low.g));
  const b = Math.round(low.b + frac * (high.b - low.b));
  return `rgb(${r}, ${g}, ${b})`;
}

/**
 * Whether a higher input value on a slider axis raises or lowers that axis's
 * contribution to the opportunity index. Impact is `higher-is-better`;
 * congestion is `lower-is-better` because the index multiplies by
 * (1 - congestion).
 */
export type SigmoidAxisDirection = 'higher-is-better' | 'lower-is-better';

/**
 * Builds a CSS `linear-gradient` painting a slider track with the opportunity
 * colour scale evaluated through the current sigmoid. The red-to-green
 * transition therefore slides with the midpoint, showing the transfer function
 * being edited rather than a static ramp.
 */
export function buildSigmoidTrackGradient(
  midpoint: number,
  direction: SigmoidAxisDirection,
  band: number,
  stopCount = 41,
): string {
  const steps = Math.max(2, stopCount);
  const stops: string[] = [];
  for (let i = 0; i < steps; i++) {
    const position = i / (steps - 1);
    const sigmoid = computeSigmoid(position, midpoint, band);
    const contribution =
      direction === 'higher-is-better' ? sigmoid : 1 - sigmoid;
    stops.push(
      `${interpolateRdYlGnHex(contribution)} ${(position * 100).toFixed(2)}%`,
    );
  }
  return `linear-gradient(to right, ${stops.join(', ')})`;
}
