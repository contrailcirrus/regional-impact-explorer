/**
 * @fileoverview WebGL and CPU grid rendering utilities for raster layer
 * visualization.
 */

/**
 * Returns the 32-bit float bit pattern for the half-precision value with the
 * given `sign` (already shifted to bit 31), 5-bit `exponent` and 10-bit
 * `mantissa`. Subnormal halves are renormalized; Inf/NaN keep their payload.
 */
function float16PartsToFloat32Bits(
  sign: number,
  exponent: number,
  mantissa: number,
): number {
  if (exponent === 31) {
    return sign | 0x7f800000 | (mantissa << 13);
  }
  if (exponent !== 0) {
    return sign | ((exponent + 112) << 23) | (mantissa << 13);
  }
  if (mantissa === 0) {
    return sign;
  }
  let shift = 0;
  let mant = mantissa;
  while ((mant & 0x0400) === 0) {
    mant <<= 1;
    shift++;
  }
  mant &= 0x03ff;
  return sign | ((127 - 14 - shift) << 23) | (mant << 13);
}

/**
 * Precomputed table mapping 16-bit IEEE 754 half-precision bit patterns to
 * 32-bit floats.
 * @visibleForTesting
 */
export const FLOAT16_TO_FLOAT32_LUT: Float32Array = (() => {
  const lut = new Float32Array(65536);
  const u32 = new Uint32Array(1);
  const f32 = new Float32Array(u32.buffer);
  for (let i = 0; i < 65536; i++) {
    u32[0] = float16PartsToFloat32Bits(
      (i & 0x8000) << 16,
      (i >> 10) & 0x1f,
      i & 0x03ff,
    );
    lut[i] = f32[0];
  }
  return lut;
})();

/**
 * Selects the raster level-of-detail (LOD 0, 1, or 2) for a map `zoom` level.
 */
export const getLodForZoom = (zoom: number): number => {
  if (zoom < 1.8) return 0;
  if (zoom < 2.8) return 1;
  return 2;
};

/**
 * Web Mercator latitude ceiling in degrees (atan(sinh(pi)) * 180 / pi).
 * @visibleForTesting
 */
export const MAX_MERCATOR_LATITUDE = 85.05112878;

/** Grid types that carry a published colour-scale maximum. */
export type GridValueMode = 'impact' | 'density';

/** Type guard verifying that `value` is a non-null object record. */
function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === 'object' && value !== null;
}

/**
 * Reads the colour-scale maximum for `mode` from grid metadata.
 *
 * The pipeline publishes, per grid type, the largest absolute cell value over
 * all buckets under `value_max_ref_scaled`, in the reference-scaled units the
 * grids are log-encoded in (so one value serves every LOD). There is no
 * built-in fallback: returns undefined unless the value is finite and > 0,
 * and the caller must then refuse to render rather than guess a scale.
 */
export function parseGridValueMax(
  meta: {readonly valueMaxRefScaled?: unknown} | null | undefined,
  mode: GridValueMode,
): number | undefined {
  const raw = meta?.valueMaxRefScaled;
  if (!isRecord(raw)) return undefined;
  const value = raw[mode];
  if (typeof value !== 'number' || !isValidValueMax(value)) return undefined;
  return value;
}

/** Returns true when `valueMax` is a finite positive number. */
function isValidValueMax(valueMax: number): boolean {
  return Number.isFinite(valueMax) && valueMax > 0;
}

/**
 * `log10(valueMax + 1)`: the encoded value that maps to the end of the ramp.
 * @visibleForTesting
 */
export function gridLogScale(valueMax: number): number {
  return Math.log10(valueMax + 1);
}

/**
 * Reference-scaled impact values at `count` evenly spaced legend positions,
 * top (+valueMax) to bottom (-valueMax), with 0 in the middle.
 *
 * Inverts the renderer's `norm = 0.5 +/- 0.5 * |log10(|v| + 1)| / logScale`,
 * so each label sits exactly where its colour is on the bar.
 */
export function impactLegendTickValues(valueMax: number, count = 11): number[] {
  const logScale = gridLogScale(valueMax);
  const ticks: number[] = [];
  for (let k = 0; k < count; k++) {
    const signedFraction = 1 - (2 * k) / (count - 1); // +1 .. -1
    const magnitude = Math.pow(10, Math.abs(signedFraction) * logScale) - 1;
    ticks.push(
      signedFraction === 0 ? 0 : Math.sign(signedFraction) * magnitude,
    );
  }
  return ticks;
}

/**
 * Reference-scaled density values at `count` evenly spaced legend positions,
 * top (valueMax) to bottom (0). Inverts `norm = log10(v + 1) / logScale`.
 */
export function densityLegendTickValues(valueMax: number, count = 9): number[] {
  const logScale = gridLogScale(valueMax);
  const ticks: number[] = [];
  for (let k = 0; k < count; k++) {
    const fraction = 1 - k / (count - 1); // 1 .. 0
    ticks.push(Math.pow(10, fraction * logScale) - 1);
  }
  return ticks;
}

/**
 * GWP horizons published in `grid_meta.json` under `gwp_tonnes_co2e_per_mj`.
 */
export type GwpHorizonKey = 'gwp20' | 'gwp50' | 'gwp100';

/** Tonnes of CO2e per MJ of energy forcing, one factor per GWP horizon. */
export type GwpTonnesPerMj = Readonly<Record<GwpHorizonKey, number>>;

const GWP_HORIZON_KEYS: readonly GwpHorizonKey[] = ['gwp20', 'gwp50', 'gwp100'];

/**
 * Reads the MJ -> t CO2e factors that the pipeline publishes in grid metadata.
 *
 * Impact grids stay in MJ (the colour mapping is calibrated against the
 * published impact maximum in MJ); only the legend is converted. Returns
 * undefined unless every horizon has a finite, positive factor, so runs derived
 * before the factors existed fall back to the MJ legend instead of showing a
 * wrong unit.
 */
export function parseGwpTonnesPerMj(
  meta: {readonly gwpTonnesCo2ePerMj?: unknown} | null | undefined,
): GwpTonnesPerMj | undefined {
  const raw = meta?.gwpTonnesCo2ePerMj;
  if (!isRecord(raw)) return undefined;
  const gwp20 = toPositiveFactor(raw['gwp20']);
  const gwp50 = toPositiveFactor(raw['gwp50']);
  const gwp100 = toPositiveFactor(raw['gwp100']);
  if (gwp20 === undefined || gwp50 === undefined || gwp100 === undefined) {
    return undefined;
  }
  return {gwp20, gwp50, gwp100};
}

/**
 * Returns `value` when it is a finite positive number, or `undefined`
 * otherwise.
 */
function toPositiveFactor(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? value
    : undefined;
}

/** Maps a filter GWP label (e.g. `'GWP50'`) to its metadata factor key. */
export function toGwpHorizonKey(
  gwp: 'GWP20' | 'GWP50' | 'GWP100',
): GwpHorizonKey {
  switch (gwp) {
    case 'GWP20':
      return 'gwp20';
    case 'GWP50':
      return 'gwp50';
    case 'GWP100':
      return 'gwp100';
    default:
      return checkExhaustiveGwp(gwp);
  }
}

/**
 * Exhaustiveness helper that throws when an unhandled GWP horizon is
 * encountered.
 */
function checkExhaustiveGwp(value: never): never {
  throw new Error(`Unhandled GWP horizon: ${String(value)}`);
}

/** True when both factor sets are absent or hold identical values. */
export function sameGwpTonnesPerMj(
  a: GwpTonnesPerMj | undefined,
  b: GwpTonnesPerMj | undefined,
): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return GWP_HORIZON_KEYS.every((key) => a[key] === b[key]);
}

/**
 * Formats a legend tick mantissa to an integer when >= 10 or whole, otherwise 1
 * decimal place.
 */
function formatTickMantissa(value: number): string {
  return value >= 10 || Number.isInteger(value)
    ? `${Math.round(value)}`
    : value.toFixed(1);
}

/**
 * Formats one impact legend tick.
 *
 * `refMj` is the tick's reference value on the 1200x600 base grid; dividing by
 * `resolutionScale` yields the per-cell value on the grid actually shown. With
 * `tonnesPerMj` the value is converted to CO2e and given an explicit mass unit
 * (g / kg / t / kt / Mt / Gt): fine LODs put the lower ticks well below one
 * tonne per cell, so a tonnes-only formatter would print 0 for most of the
 * scale. Without it the legacy dimensionless MJ formatting (K / M / B) is kept.
 */
export function formatImpactLegendTick(
  refMj: number,
  resolutionScale: number,
  tonnesPerMj?: number,
): string {
  if (refMj === 0 || !Number.isFinite(refMj)) return '0';
  const scale =
    Number.isFinite(resolutionScale) && resolutionScale > 0
      ? resolutionScale
      : 1;
  const perCellMj = refMj / scale;
  const sign = perCellMj > 0 ? '+' : '-';

  if (tonnesPerMj === undefined) {
    const absMj = Math.abs(perCellMj);
    if (absMj >= 1e9) return `${sign}${formatTickMantissa(absMj / 1e9)}B`;
    if (absMj >= 1e6) return `${sign}${formatTickMantissa(absMj / 1e6)}M`;
    if (absMj >= 1e3) return `${sign}${formatTickMantissa(absMj / 1e3)}K`;
    return `${sign}${Math.round(absMj)}`;
  }

  const absTonnes = Math.abs(perCellMj * tonnesPerMj);
  if (absTonnes >= 1e9)
    return `${sign}${formatTickMantissa(absTonnes / 1e9)} Gt`;
  if (absTonnes >= 1e6)
    return `${sign}${formatTickMantissa(absTonnes / 1e6)} Mt`;
  if (absTonnes >= 1e3)
    return `${sign}${formatTickMantissa(absTonnes / 1e3)} kt`;
  if (absTonnes >= 1) return `${sign}${formatTickMantissa(absTonnes)} t`;
  if (absTonnes >= 1e-3)
    return `${sign}${formatTickMantissa(absTonnes * 1e3)} kg`;
  return `${sign}${formatTickMantissa(absTonnes * 1e6)} g`;
}

/** Legend title for the impact overlay; `gwpLabel` is e.g. 'GWP50'. */
export function impactLegendTitle(
  tonnesPerMj: number | undefined,
  gwpLabel: string,
): string {
  return tonnesPerMj === undefined ? 'Impact (EF MJ)' : `CO₂e (${gwpLabel})`;
}

/** Options for `formatDensityLegendTick`. */
export interface FormatDensityTickOptions {
  readonly isTop?: boolean;
}

/**
 * Formats one flight density legend tick for a given `resolutionScale`.
 *
 * `refVal` is the tick's reference value on the 1200x600 base grid; dividing
 * by `resolutionScale` yields the per-cell flight count on the displayed grid.
 */
export function formatDensityLegendTick(
  refVal: number,
  resolutionScale: number,
  options: FormatDensityTickOptions = {},
): string {
  if (refVal === 0 || !Number.isFinite(refVal)) return '0 flights';
  const scale =
    Number.isFinite(resolutionScale) && resolutionScale > 0
      ? resolutionScale
      : 1;
  const actualVal = refVal / scale;
  let formatted: string;
  if (actualVal >= 100) {
    formatted = Math.round(actualVal).toLocaleString('en-US');
  } else if (actualVal >= 1) {
    formatted = Number.isInteger(actualVal)
      ? actualVal.toString()
      : actualVal.toFixed(1);
  } else {
    formatted = actualVal.toFixed(2);
  }
  return options.isTop ? `${formatted} flights` : formatted;
}

/**
 * Converts a latitude in degrees to normalized Web Mercator v coordinate in
 * [-1, 1].
 * @visibleForTesting
 */
export const latDegToMercatorV = (latDeg: number): number => {
  const clampedLat = Math.max(
    -MAX_MERCATOR_LATITUDE,
    Math.min(MAX_MERCATOR_LATITUDE, latDeg),
  );
  const latRad = (clampedLat * Math.PI) / 180.0;
  return Math.atanh(Math.sin(latRad)) / Math.PI;
};

/**
 * Maps a normalized canvas Y coordinate [0, 1] (top to bottom) to latitude in
 * degrees within [minLatBound, minLatBound + latRange] using Web Mercator
 * projection.
 * @visibleForTesting
 */
export const mercatorYToLatDeg = (
  yMerc: number,
  minLatBound: number,
  latRange: number,
): number => {
  if (!Number.isFinite(latRange) || Math.abs(latRange) < 1e-6) {
    return Math.max(-85.05112878, Math.min(85.05112878, minLatBound));
  }
  const maxLatBound = minLatBound + latRange;
  const vMax = latDegToMercatorV(maxLatBound);
  const vMin = latDegToMercatorV(minLatBound);
  const v = vMax + (vMin - vMax) * yMerc;
  return Math.asin(Math.tanh(Math.PI * v)) * (180.0 / Math.PI);
};

/**
 * Encodes an HTMLCanvasElement to a Blob object URL (with data URL fallback),
 * returning `null` when `signal` is aborted before or during blob encoding.
 */
const canvasToUrl = async (
  canvasElement: HTMLCanvasElement,
  signal?: AbortSignal,
): Promise<string | null> => {
  if (signal?.aborted) {
    return null;
  }
  if (typeof canvasElement.toBlob === 'function') {
    try {
      const blob = await new Promise<Blob | null>((resolve) => {
        canvasElement.toBlob(resolve, 'image/webp', 0.92);
      });
      if (signal?.aborted) {
        return null;
      }
      if (blob) {
        return URL.createObjectURL(blob);
      }
    } catch (e: unknown) {
      // Fallback to synchronous data URL below
    }
  }
  if (signal?.aborted) {
    return null;
  }
  return canvasElement.toDataURL();
};

/** Geographic bounding box of a raster grid in degrees. */
export interface RasterBBox {
  readonly minLon?: number;
  readonly maxLon?: number;
  readonly minLat?: number;
  readonly maxLat?: number;
}

/**
 * Metadata sidecar (`grid_meta.json` / `*_meta.json`) describing a raster grid.
 */
export interface RasterMetadata {
  readonly width: number;
  readonly height: number;
  readonly dtype?: string;
  readonly encoding?: string;
  readonly bbox?: RasterBBox;
  readonly valueMaxRefScaled?: Readonly<Partial<Record<GridValueMode, number>>>;
  readonly gwpTonnesCo2ePerMj?: GwpTonnesPerMj;
}

/**
 * Parameters for rendering a float16 contrail raster grid to a WebMercator
 * image URL.
 */
export interface GridRenderParams {
  readonly meta: RasterMetadata;
  readonly float16Data: Uint16Array;
  readonly mode: GridValueMode;
  /** Colour-scale maximum from `parseGridValueMax`; must be finite and > 0. */
  readonly valueMax: number;
  readonly minLatBound: number;
  readonly latRange: number;
  readonly signal?: AbortSignal;
}

/**
 * Decodes a `Uint16Array` of IEEE 754 binary16 values into a `Float32Array` of
 * length `pixelCount`.
 */
function decodeFloat16Buffer(
  float16Data: Uint16Array,
  pixelCount: number,
): Float32Array {
  const out = new Float32Array(pixelCount);
  for (let i = 0; i < pixelCount; i++) {
    out[i] = FLOAT16_TO_FLOAT32_LUT[float16Data[i]];
  }
  return out;
}

/** Texture shape, format, and pixel data for one raster grid upload. */
interface GridTextureUploadOptions {
  readonly width: number;
  readonly height: number;
  readonly internalFormat: number;
  readonly texelType: number;
  readonly uploadBuffer: Uint16Array | Float32Array;
}

/** Cached WebGL uniform locations for the raster grid shader program. */
interface UniformLocations {
  readonly uWidth: WebGLUniformLocation | null;
  readonly uHeight: WebGLUniformLocation | null;
  readonly uMinLat: WebGLUniformLocation | null;
  readonly uLatRange: WebGLUniformLocation | null;
  readonly uVMax: WebGLUniformLocation | null;
  readonly uVMin: WebGLUniformLocation | null;
  readonly uMode: WebGLUniformLocation | null;
  readonly uLogScale: WebGLUniformLocation | null;
}

/** Single stop in a normalized `[0, 1]` raster colour palette. */
interface PaletteStop {
  readonly node: number;
  readonly red: number;
  readonly green: number;
  readonly blue: number;
}

const IMPACT_PALETTE_STOPS: readonly PaletteStop[] = [
  {node: 0.0, red: 0, green: 255, blue: 255},
  {node: 0.09, red: 0, green: 0, blue: 255},
  {node: 0.11, red: 0, green: 0, blue: 120},
  {node: 0.18, red: 160, green: 160, blue: 255},
  {node: 0.5, red: 255, green: 255, blue: 255},
  {node: 0.82, red: 255, green: 160, blue: 160},
  {node: 0.89, red: 120, green: 0, blue: 0},
  {node: 0.91, red: 255, green: 0, blue: 0},
  {node: 1.0, red: 255, green: 255, blue: 0},
];

const DENSITY_PALETTE_STOPS: readonly PaletteStop[] = [
  {node: 0.0, red: 255, green: 255, blue: 255},
  {node: 0.75, red: 139, green: 0, blue: 0},
  {node: 0.8, red: 255, green: 0, blue: 0},
  {node: 0.9, red: 255, green: 255, blue: 0},
  {node: 1.0, red: 255, green: 255, blue: 255},
];

function toHexChannel(value: number): string {
  return Math.max(0, Math.min(255, Math.round(value)))
    .toString(16)
    .padStart(2, '0');
}

function buildColorbarCssGradient(stops: readonly PaletteStop[]): string {
  const cssStops = [...stops]
    .reverse()
    .map((stop) => {
      const hex =
        '#' +
        toHexChannel(stop.red) +
        toHexChannel(stop.green) +
        toHexChannel(stop.blue);
      const pct = Math.round((1 - stop.node) * 100);
      return `${hex} ${pct}%`;
    })
    .join(', ');
  return `linear-gradient(to bottom, ${cssStops})`;
}

/** CSS gradient of the impact overlay colorbar, top (max) to bottom. */
export const IMPACT_COLORBAR_GRADIENT: string =
  buildColorbarCssGradient(IMPACT_PALETTE_STOPS);

/** CSS gradient of the flight density overlay colorbar, top to bottom. */
export const DENSITY_COLORBAR_GRADIENT: string = buildColorbarCssGradient(
  DENSITY_PALETTE_STOPS,
);

function formatShaderPaletteNodes(stops: readonly PaletteStop[]): string {
  return stops
    .map((stop) =>
      Number.isInteger(stop.node) ? stop.node.toFixed(1) : String(stop.node),
    )
    .join(', ');
}

function formatShaderPaletteColors(stops: readonly PaletteStop[]): string {
  return stops
    .map((stop) => {
      const red = (stop.red / 255).toFixed(6);
      const green = (stop.green / 255).toFixed(6);
      const blue = (stop.blue / 255).toFixed(6);
      return `vec3(${red}, ${green}, ${blue})`;
    })
    .join(',\n            ');
}

/** GLSL ES 3.0 vertex shader drawing a full-screen quad. */
const GRID_VERTEX_SHADER_SOURCE = `#version 300 es
        in vec2 a_pos;
        out vec2 v_uv;
        void main() {
          v_uv = a_pos * 0.5 + 0.5;
          gl_Position = vec4(a_pos, 0.0, 1.0);
        }
      `;

/**
 * GLSL ES 3.0 fragment shader sampling the float grid and applying the
 * palettes.
 */
const GRID_FRAGMENT_SHADER_SOURCE = `#version 300 es
        precision highp float;
        in vec2 v_uv;
        out vec4 outColor;
        uniform sampler2D u_tex;
        uniform float u_width;
        uniform float u_height;
        uniform float u_minLat;
        uniform float u_latRange;
        uniform float u_vMax;
        uniform float u_vMin;
        uniform int u_mode;
        uniform float u_logScale;

        const float PI = 3.141592653589793;

        float sampleTexel(int c, int r) {
          int cc = clamp(c, 0, int(u_width) - 1);
          int rr = clamp(r, 0, int(u_height) - 1);
          return texelFetch(u_tex, ivec2(cc, rr), 0).r;
        }

        vec4 sampleImpactPalette(float norm) {
          float nodes[${IMPACT_PALETTE_STOPS.length}] = float[](
            ${formatShaderPaletteNodes(IMPACT_PALETTE_STOPS)});
          vec3 colors[${IMPACT_PALETTE_STOPS.length}] = vec3[](
            ${formatShaderPaletteColors(IMPACT_PALETTE_STOPS)}
          );
          int idx = 0;
          for (int i = 0; i < ${IMPACT_PALETTE_STOPS.length - 1}; i++) {
            if (nodes[i + 1] <= norm) idx = i + 1;
          }
          int nextIdx = min(idx + 1, ${IMPACT_PALETTE_STOPS.length - 1});
          float span = max(nodes[nextIdx] - nodes[idx], 1e-6);
          float t = clamp((norm - nodes[idx]) / span, 0.0, 1.0);
          vec3 rgb = mix(colors[idx], colors[nextIdx], t);
          float distFromCenter = abs(norm - 0.5) * 2.0;
          float tAlphaLow = clamp(distFromCenter / 0.64, 0.0, 1.0);
          float tAlphaHigh = clamp((distFromCenter - 0.64) / 0.14, 0.0, 1.0);
          float alpha = clamp(
            floor(65.0 + 110.0 * tAlphaLow + 75.0 * tAlphaHigh),
            65.0, 250.0) / 255.0;
          return vec4(rgb, alpha);
        }

        vec4 sampleDensityPalette(float norm) {
          float nodes[${DENSITY_PALETTE_STOPS.length}] = float[](
            ${formatShaderPaletteNodes(DENSITY_PALETTE_STOPS)});
          vec3 colors[${DENSITY_PALETTE_STOPS.length}] = vec3[](
            ${formatShaderPaletteColors(DENSITY_PALETTE_STOPS)}
          );
          int idx = 0;
          for (int i = 0; i < ${DENSITY_PALETTE_STOPS.length - 1}; i++) {
            if (nodes[i + 1] <= norm) idx = i + 1;
          }
          int nextIdx = min(idx + 1, ${DENSITY_PALETTE_STOPS.length - 1});
          float span = max(nodes[nextIdx] - nodes[idx], 1e-6);
          float t = clamp((norm - nodes[idx]) / span, 0.0, 1.0);
          vec3 rgb = mix(colors[idx], colors[nextIdx], t);
          float alpha = clamp(floor(norm * 180.0 + 60.0), 90.0, 240.0) / 255.0;
          return vec4(rgb, alpha);
        }

        void main() {
          float yMerc = 1.0 - v_uv.y;
          float v = mix(u_vMax, u_vMin, yMerc);
          float latDeg = asin(tanh(PI * v)) * (180.0 / PI);
          int r = clamp(
            int(floor(((latDeg - u_minLat) / u_latRange) * u_height)),
            0, int(u_height) - 1);
          int c = clamp(int(floor(v_uv.x * u_width)), 0, int(u_width) - 1);
          // Texels hold signed_log10 of the ref-scaled value.
          float logVal = sampleTexel(c, r);

          if (u_mode == 0) {
            if (isnan(logVal) || isinf(logVal) || abs(logVal) < 1e-4) {
              outColor = vec4(0.0);
              return;
            }
            float norm = 0.5 +
              (logVal > 0.0 ? 0.5 : -0.5) * (abs(logVal) / u_logScale);
            norm = clamp(norm, 0.0, 1.0);
            outColor = sampleImpactPalette(norm);
          } else {
            if (isnan(logVal) || isinf(logVal) || logVal <= 0.0) {
              outColor = vec4(0.0);
              return;
            }
            float norm = clamp(logVal / u_logScale, 0.0, 1.0);
            outColor = sampleDensityPalette(norm);
          }
        }
      `;

/**
 * Shared offscreen WebGL2 renderer that projects equirectangular contrail grids
 * into Web Mercator overlay images.
 * @visibleForTesting
 */
export class SharedGridGpuRenderer {
  private canvas: HTMLCanvasElement | null = null;
  private gl: WebGL2RenderingContext | null = null;
  private prog: WebGLProgram | null = null;
  private tex: WebGLTexture | null = null;
  private quadBuf: WebGLBuffer | null = null;
  private uniformLocs: UniformLocations | null = null;
  private lastTexWidth = 0;
  private lastTexHeight = 0;
  private lastTexInternalFormat = 0;
  private renderQueue: Promise<unknown> = Promise.resolve();

  /**
   * Clears all cached WebGL handles and texture dimensions on context loss or
   * re-initialization.
   */
  private resetGlState(): void {
    this.gl = null;
    this.quadBuf = null;
    this.tex = null;
    this.prog = null;
    this.uniformLocs = null;
    this.lastTexWidth = 0;
    this.lastTexHeight = 0;
    this.lastTexInternalFormat = 0;
  }

  /**
   * Resets the GL state when `canvas` loses or restores its context while it is
   * still the active canvas. No-op in environments without DOM events.
   */
  private attachContextLossListeners(canvas: HTMLCanvasElement): void {
    if (typeof canvas.addEventListener !== 'function') return;
    canvas.addEventListener('webglcontextlost', (e: Event) => {
      if (this.canvas !== canvas) return;
      if (typeof e.preventDefault === 'function') {
        e.preventDefault();
      }
      this.resetGlState();
    });
    canvas.addEventListener('webglcontextrestored', () => {
      if (this.canvas !== canvas) return;
      this.resetGlState();
    });
  }

  /**
   * Initializes (or re-initializes) the offscreen WebGL2 context, shaders, quad
   * buffer, and texture.
   */
  private init(): boolean {
    if (typeof document === 'undefined') return false;
    if (this.gl && !this.gl.isContextLost() && this.prog && this.tex)
      return true;

    try {
      if (this.gl && !this.gl.isContextLost()) {
        if (this.quadBuf) this.gl.deleteBuffer(this.quadBuf);
        if (this.tex) this.gl.deleteTexture(this.tex);
        if (this.prog) this.gl.deleteProgram(this.prog);
      }
      this.resetGlState();
      const canvas = document.createElement('canvas');
      this.attachContextLossListeners(canvas);
      this.canvas = canvas;
      const gl = this.canvas.getContext('webgl2', {
        preserveDrawingBuffer: true,
        premultipliedAlpha: false,
        antialias: false,
        depth: false,
        stencil: false,
      });
      if (!gl) return false;
      this.gl = gl;

      const compileShader = (type: number, src: string): WebGLShader | null => {
        const s = gl.createShader(type);
        if (!s) {
          console.warn(`Failed to create WebGL shader of type ${type}`);
          return null;
        }
        gl.shaderSource(s, src);
        gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
          console.warn(
            'WebGL shader compilation failed:',
            gl.getShaderInfoLog(s),
          );
          gl.deleteShader(s);
          return null;
        }
        return s;
      };
      const vs = compileShader(gl.VERTEX_SHADER, GRID_VERTEX_SHADER_SOURCE);
      const fs = compileShader(gl.FRAGMENT_SHADER, GRID_FRAGMENT_SHADER_SOURCE);
      if (!vs || !fs) {
        if (vs) gl.deleteShader(vs);
        if (fs) gl.deleteShader(fs);
        return false;
      }
      const prog = gl.createProgram();
      if (!prog) {
        gl.deleteShader(vs);
        gl.deleteShader(fs);
        console.warn('Failed to create WebGL program');
        return false;
      }
      gl.attachShader(prog, vs);
      gl.attachShader(prog, fs);
      gl.linkProgram(prog);
      gl.detachShader(prog, vs);
      gl.detachShader(prog, fs);
      gl.deleteShader(vs);
      gl.deleteShader(fs);

      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
        console.warn('WebGL program link failed:', gl.getProgramInfoLog(prog));
        gl.deleteProgram(prog);
        return false;
      }

      this.prog = prog;
      gl.useProgram(prog);

      const quadBuf = gl.createBuffer();
      this.quadBuf = quadBuf;
      gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
      gl.bufferData(
        gl.ARRAY_BUFFER,
        new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
        gl.STATIC_DRAW,
      );
      const posLoc = gl.getAttribLocation(prog, 'a_pos');
      gl.enableVertexAttribArray(posLoc);
      gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

      this.tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this.tex);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

      this.uniformLocs = {
        uWidth: gl.getUniformLocation(prog, 'u_width'),
        uHeight: gl.getUniformLocation(prog, 'u_height'),
        uMinLat: gl.getUniformLocation(prog, 'u_minLat'),
        uLatRange: gl.getUniformLocation(prog, 'u_latRange'),
        uVMax: gl.getUniformLocation(prog, 'u_vMax'),
        uVMin: gl.getUniformLocation(prog, 'u_vMin'),
        uMode: gl.getUniformLocation(prog, 'u_mode'),
        uLogScale: gl.getUniformLocation(prog, 'u_logScale'),
      };

      return true;
    } catch (err: unknown) {
      console.warn('WebGL raster initialization failed:', err);
      return false;
    }
  }

  /**
   * Uploads `options.uploadBuffer` into the bound texture, reusing its storage
   * via `texSubImage2D` when dimensions and format are unchanged. Returns false
   * (and forgets the cached texture shape) if `texImage2D` fails.
   */
  private uploadGridTexture(
    gl: WebGL2RenderingContext,
    options: GridTextureUploadOptions,
  ): boolean {
    const {width, height, internalFormat, texelType, uploadBuffer} = options;
    if (
      this.lastTexWidth === width &&
      this.lastTexHeight === height &&
      this.lastTexInternalFormat === internalFormat
    ) {
      gl.texSubImage2D(
        gl.TEXTURE_2D,
        0,
        0,
        0,
        width,
        height,
        gl.RED,
        texelType,
        uploadBuffer,
      );
      return true;
    }
    gl.texImage2D(
      gl.TEXTURE_2D,
      0,
      internalFormat,
      width,
      height,
      0,
      gl.RED,
      texelType,
      uploadBuffer,
    );
    if (typeof gl.getError === 'function' && gl.getError() !== gl.NO_ERROR) {
      this.lastTexWidth = 0;
      this.lastTexHeight = 0;
      this.lastTexInternalFormat = 0;
      return false;
    }
    this.lastTexWidth = width;
    this.lastTexHeight = height;
    this.lastTexInternalFormat = internalFormat;
    return true;
  }

  /**
   * Queues and executes a WebGL2 render pass for `params`, resolving to a
   * blob/data URL or `null` on failure.
   */
  public render(params: GridRenderParams): Promise<string | null> {
    const job = async (): Promise<string | null> => {
      if (params.signal?.aborted) {
        return null;
      }
      if (
        !this.init() ||
        !this.gl ||
        !this.canvas ||
        !this.prog ||
        !this.tex ||
        !this.uniformLocs
      ) {
        return null;
      }
      try {
        const gl = this.gl;
        const uniformLocs = this.uniformLocs;
        const {meta, float16Data, mode, minLatBound, latRange} = params;

        if (
          !Number.isInteger(meta.width) ||
          meta.width <= 0 ||
          !Number.isInteger(meta.height) ||
          meta.height <= 0 ||
          !isValidValueMax(params.valueMax)
        ) {
          return null;
        }
        const expectedPixels = meta.width * meta.height;
        if (float16Data.length < expectedPixels) {
          return null;
        }

        const maxTextureSize =
          typeof gl.getParameter === 'function'
            ? Number(gl.getParameter(gl.MAX_TEXTURE_SIZE)) || 4096
            : 4096;
        if (meta.width > maxTextureSize || meta.height > maxTextureSize) {
          return null;
        }

        if (this.canvas.width !== meta.width) this.canvas.width = meta.width;
        if (this.canvas.height !== meta.height)
          this.canvas.height = meta.height;

        const canUploadHalfFloat =
          typeof gl.R16F === 'number' && typeof gl.HALF_FLOAT === 'number';
        const internalFormat = canUploadHalfFloat ? gl.R16F : gl.R32F;
        const texelType = canUploadHalfFloat ? gl.HALF_FLOAT : gl.FLOAT;
        const uploadBuffer = canUploadHalfFloat
          ? float16Data
          : decodeFloat16Buffer(float16Data, expectedPixels);

        gl.useProgram(this.prog);
        gl.bindTexture(gl.TEXTURE_2D, this.tex);
        if (
          !this.uploadGridTexture(gl, {
            width: meta.width,
            height: meta.height,
            internalFormat,
            texelType,
            uploadBuffer,
          })
        ) {
          return null;
        }

        const logScale = gridLogScale(params.valueMax);
        const safeLatRange =
          Number.isFinite(latRange) && Math.abs(latRange) >= 1e-6
            ? latRange
            : 180;
        const vMax = latDegToMercatorV(minLatBound + safeLatRange);
        const vMin = latDegToMercatorV(minLatBound);

        gl.uniform1f(uniformLocs.uWidth, meta.width);
        gl.uniform1f(uniformLocs.uHeight, meta.height);
        gl.uniform1f(uniformLocs.uMinLat, minLatBound);
        gl.uniform1f(uniformLocs.uLatRange, safeLatRange);
        gl.uniform1f(uniformLocs.uVMax, vMax);
        gl.uniform1f(uniformLocs.uVMin, vMin);
        gl.uniform1i(uniformLocs.uMode, mode === 'impact' ? 0 : 1);
        gl.uniform1f(uniformLocs.uLogScale, logScale);

        gl.viewport(0, 0, meta.width, meta.height);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

        if (params.signal?.aborted) {
          return null;
        }
        return await canvasToUrl(this.canvas, params.signal);
      } catch (err: unknown) {
        console.warn('WebGL raster render failed:', err);
        return null;
      }
    };

    const nextPromise = this.renderQueue.then(job, job);
    this.renderQueue = nextPromise.then(
      () => {},
      () => {},
    );
    return nextPromise;
  }
}

/** Singleton WebGL2 raster grid renderer shared across map overlay updates. */
export const sharedGridGpuRenderer = new SharedGridGpuRenderer();

const CPU_LUT_SIZE = 1024;

/**
 * Builds a 1024-entry RGBA lookup table for CPU fallback rendering of impact or
 * density grids.
 */
function buildStaticCpuColorLut(isImpact: boolean): Uint8ClampedArray {
  const palette = isImpact ? IMPACT_PALETTE_STOPS : DENSITY_PALETTE_STOPS;

  const lut = new Uint8ClampedArray(CPU_LUT_SIZE * 4);
  for (let i = 0; i < CPU_LUT_SIZE; i++) {
    const norm = i / (CPU_LUT_SIZE - 1);
    let stopIndex = 0;
    while (
      stopIndex < palette.length - 1 &&
      palette[stopIndex + 1].node <= norm
    ) {
      stopIndex++;
    }
    const lowerStop = palette[stopIndex];
    const upperStop = palette[Math.min(stopIndex + 1, palette.length - 1)];
    const span = upperStop.node - lowerStop.node || 1;
    const t = (norm - lowerStop.node) / span;
    lut[i * 4] = Math.round(lowerStop.red * (1 - t) + upperStop.red * t);
    lut[i * 4 + 1] = Math.round(
      lowerStop.green * (1 - t) + upperStop.green * t,
    );
    lut[i * 4 + 2] = Math.round(lowerStop.blue * (1 - t) + upperStop.blue * t);
    if (isImpact) {
      const distFromCenter = Math.abs(norm - 0.5) * 2;
      const tAlphaLow = Math.min(1, Math.max(0, distFromCenter / 0.64));
      const tAlphaHigh = Math.min(
        1,
        Math.max(0, (distFromCenter - 0.64) / 0.14),
      );
      lut[i * 4 + 3] = Math.min(
        250,
        Math.max(65, Math.floor(65 + 110 * tAlphaLow + 75 * tAlphaHigh)),
      );
    } else {
      lut[i * 4 + 3] = Math.min(240, Math.max(90, Math.floor(norm * 180 + 60)));
    }
  }
  return lut;
}

const IMPACT_CPU_LUT = buildStaticCpuColorLut(true);
const DENSITY_CPU_LUT = buildStaticCpuColorLut(false);

/**
 * Maps a decoded signed_log10 grid value to a colour-scale position in [0, 1]
 * (impact may fall outside before LUT clamping), or returns null for pixels
 * left transparent: non-finite values, near-zero impact, or non-positive
 * density.
 */
function normalizeCpuLogValue(
  logVal: number,
  isImpact: boolean,
  logScale: number,
): number | null {
  if (!Number.isFinite(logVal)) {
    return null;
  }
  if (isImpact) {
    if (Math.abs(logVal) < 1e-4) {
      return null;
    }
    return 0.5 + (logVal > 0 ? 0.5 : -0.5) * (Math.abs(logVal) / logScale);
  }
  if (logVal <= 0) {
    return null;
  }
  return Math.max(0, Math.min(1, logVal / logScale));
}

/**
 * Renders a contrail raster grid on a 2D CPU canvas using precomputed RGBA
 * lookup tables.
 */
export const renderGridOnCpu = async (
  params: GridRenderParams,
): Promise<string | null> => {
  if (typeof document === 'undefined' || params.signal?.aborted) return null;
  const {meta, float16Data, mode, minLatBound, latRange} = params;
  if (
    !Number.isInteger(meta.width) ||
    meta.width <= 0 ||
    !Number.isInteger(meta.height) ||
    meta.height <= 0 ||
    !isValidValueMax(params.valueMax)
  ) {
    return null;
  }
  const expectedPixels = meta.width * meta.height;
  if (float16Data.length < expectedPixels) {
    return null;
  }

  const canvas = document.createElement('canvas');
  canvas.width = meta.width;
  canvas.height = meta.height;
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  const imgData = ctx.createImageData(meta.width, meta.height);
  const pixels = imgData.data;

  const isImpact = mode === 'impact';
  const lut = isImpact ? IMPACT_CPU_LUT : DENSITY_CPU_LUT;
  const logScale = gridLogScale(params.valueMax);
  const safeLatRange =
    Number.isFinite(latRange) && Math.abs(latRange) >= 1e-6 ? latRange : 180;

  for (let canvasY = 0; canvasY < meta.height; canvasY++) {
    if ((canvasY & 63) === 0 && params.signal?.aborted) {
      return null;
    }
    const yMerc = (canvasY + 0.5) / meta.height;
    const latDeg = mercatorYToLatDeg(yMerc, minLatBound, safeLatRange);

    let r = Math.floor(((latDeg - minLatBound) / safeLatRange) * meta.height);
    r = Math.max(0, Math.min(meta.height - 1, r));
    const rowDataOffset = r * meta.width;
    let pixelIdx = canvasY * meta.width * 4;

    for (let c = 0; c < meta.width; c++, pixelIdx += 4) {
      // Values hold signed_log10 of the ref-scaled value.
      const logVal = FLOAT16_TO_FLOAT32_LUT[float16Data[rowDataOffset + c]];
      const norm = normalizeCpuLogValue(logVal, isImpact, logScale);
      if (norm === null) {
        continue;
      }

      const lutIdx =
        Math.max(
          0,
          Math.min(CPU_LUT_SIZE - 1, Math.round(norm * (CPU_LUT_SIZE - 1))),
        ) * 4;
      pixels[pixelIdx] = lut[lutIdx];
      pixels[pixelIdx + 1] = lut[lutIdx + 1];
      pixels[pixelIdx + 2] = lut[lutIdx + 2];
      pixels[pixelIdx + 3] = lut[lutIdx + 3];
    }
  }
  if (params.signal?.aborted) return null;
  ctx.putImageData(imgData, 0, 0);
  return await canvasToUrl(canvas, params.signal);
};
