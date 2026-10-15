import 'jasmine';
import {
  DENSITY_COLORBAR_GRADIENT,
  densityLegendTickValues,
  FLOAT16_TO_FLOAT32_LUT,
  formatDensityLegendTick,
  formatImpactLegendTick,
  getLodForZoom,
  gridLogScale,
  IMPACT_COLORBAR_GRADIENT,
  impactLegendTickValues,
  impactLegendTitle,
  latDegToMercatorV,
  MAX_MERCATOR_LATITUDE,
  mercatorYToLatDeg,
  parseGridValueMax,
  parseGwpTonnesPerMj,
  renderGridOnCpu,
  sameGwpTonnesPerMj,
  SharedGridGpuRenderer,
  toGwpHorizonKey,
} from './geo_raster_utils';

describe('geo_raster_utils', () => {
  describe('grid value maximum', () => {
    const meta = (value: unknown) => ({
      width: 1,
      height: 1,
      valueMaxRefScaled: value,
    });

    it('reads the published maximum per grid type', () => {
      const m = meta({'impact': 2.5e11, 'density': 3e6});
      expect(parseGridValueMax(m, 'impact')).toBe(2.5e11);
      expect(parseGridValueMax(m, 'density')).toBe(3e6);
    });

    it('has no fallback for missing or invalid values', () => {
      expect(parseGridValueMax(undefined, 'impact')).toBeUndefined();
      expect(parseGridValueMax({}, 'impact')).toBeUndefined();
      expect(parseGridValueMax(meta(null), 'impact')).toBeUndefined();
      expect(
        parseGridValueMax(meta({'density': 3e6}), 'impact'),
      ).toBeUndefined();
      expect(
        parseGridValueMax(meta({'impact': '1e9'}), 'impact'),
      ).toBeUndefined();
      expect(parseGridValueMax(meta({'impact': 0}), 'impact')).toBeUndefined();
      expect(parseGridValueMax(meta({'impact': -1}), 'impact')).toBeUndefined();
      expect(
        parseGridValueMax(meta({'impact': Number.NaN}), 'impact'),
      ).toBeUndefined();
      expect(
        parseGridValueMax(meta({'impact': Number.POSITIVE_INFINITY}), 'impact'),
      ).toBeUndefined();
    });

    it('places impact ticks where the renderer puts their colour', () => {
      const valueMax = 1e10;
      const ticks = impactLegendTickValues(valueMax);
      expect(ticks.length).toBe(11);
      expect(ticks[0]).toBeCloseTo(valueMax, -2);
      expect(ticks[5]).toBe(0);
      expect(ticks[10]).toBeCloseTo(-valueMax, -2);
      const logScale = gridLogScale(valueMax);
      ticks.forEach((v, k) => {
        // Renderer: norm = 0.5 +/- 0.5 * log10(|v| + 1) / logScale; the bar
        // shows norm 1 at the top, so position k / 10 must equal 1 - norm.
        const logVal = Math.sign(v) * Math.log10(Math.abs(v) + 1);
        const norm = 0.5 + 0.5 * (logVal / logScale);
        expect(1 - norm).toBeCloseTo(k / 10, 9);
      });
    });

    it('places density ticks where the renderer puts their colour', () => {
      const valueMax = 2.5e6;
      const ticks = densityLegendTickValues(valueMax);
      expect(ticks.length).toBe(9);
      expect(ticks[0]).toBeCloseTo(valueMax, 3);
      expect(ticks[8]).toBe(0);
      const logScale = gridLogScale(valueMax);
      ticks.forEach((v, k) => {
        expect(1 - Math.log10(v + 1) / logScale).toBeCloseTo(k / 8, 9);
      });
    });

    it('derives CSS colorbar gradients from the raster palette stops', () => {
      expect(IMPACT_COLORBAR_GRADIENT).toBe(
        'linear-gradient(to bottom, #ffff00 0%, #ff0000 9%, #780000 11%, ' +
          '#ffa0a0 18%, #ffffff 50%, #a0a0ff 82%, #000078 89%, #0000ff 91%, ' +
          '#00ffff 100%)',
      );
      expect(DENSITY_COLORBAR_GRADIENT).toBe(
        'linear-gradient(to bottom, #ffffff 0%, #ffff00 10%, #ff0000 20%, ' +
          '#8b0000 25%, #ffffff 100%)',
      );
    });
  });
  describe('impact legend GWP conversion', () => {
    // Values published by the pipeline in grid_meta.json.
    const FACTORS = {
      gwp20: 1.048618e-6,
      gwp50: 4.945187e-7,
      gwp100: 2.847392e-7,
    };

    it('parses the factors from grid metadata', () => {
      expect(
        parseGwpTonnesPerMj({
          gwpTonnesCo2ePerMj: FACTORS,
        }),
      ).toEqual(FACTORS);
    });

    it('rejects missing, partial, non-numeric, and non-positive factors', () => {
      expect(parseGwpTonnesPerMj(undefined)).toBeUndefined();
      expect(parseGwpTonnesPerMj({})).toBeUndefined();
      expect(
        parseGwpTonnesPerMj({
          gwpTonnesCo2ePerMj: null,
        }),
      ).toBeUndefined();
      expect(
        parseGwpTonnesPerMj({
          gwpTonnesCo2ePerMj: {gwp20: 1e-6, gwp50: 5e-7},
        }),
      ).toBeUndefined();
      expect(
        parseGwpTonnesPerMj({
          gwpTonnesCo2ePerMj: {...FACTORS, gwp100: '2.8e-7'},
        }),
      ).toBeUndefined();
      expect(
        parseGwpTonnesPerMj({
          gwpTonnesCo2ePerMj: {...FACTORS, gwp50: 0},
        }),
      ).toBeUndefined();
      expect(
        parseGwpTonnesPerMj({
          gwpTonnesCo2ePerMj: {...FACTORS, gwp20: Number.NaN},
        }),
      ).toBeUndefined();
    });

    it('compares factor sets by value', () => {
      expect(sameGwpTonnesPerMj(undefined, undefined)).toBeTrue();
      expect(sameGwpTonnesPerMj(FACTORS, {...FACTORS})).toBeTrue();
      expect(sameGwpTonnesPerMj(FACTORS, undefined)).toBeFalse();
      expect(
        sameGwpTonnesPerMj(FACTORS, {...FACTORS, gwp50: 5e-7}),
      ).toBeFalse();
    });

    it('converts MJ reference ticks to CO2e with sub-tonne units', () => {
      const f = FACTORS.gwp50;
      expect(formatImpactLegendTick(228e9, 1, f)).toBe('+113 kt');
      expect(formatImpactLegendTick(1e9, 1, f)).toBe('+495 t');
      expect(formatImpactLegendTick(10e6, 1, f)).toBe('+4.9 t');
      expect(formatImpactLegendTick(100e3, 1, f)).toBe('+49 kg');
      expect(formatImpactLegendTick(1e3, 1, f)).toBe('+495 g');
      expect(formatImpactLegendTick(-1e3, 1, f)).toBe('-495 g');
      expect(formatImpactLegendTick(-228e9, 1, f)).toBe('-113 kt');
      expect(formatImpactLegendTick(0, 1, f)).toBe('0');
    });

    it('applies the grid resolution scale before converting', () => {
      // LOD2 (7200x3400) has 34x the cells of the 1200x600 reference grid.
      expect(formatImpactLegendTick(228e9, 34, FACTORS.gwp50)).toBe('+3.3 kt');
      // A non-positive or non-finite scale falls back to 1.
      expect(formatImpactLegendTick(1e9, 0, FACTORS.gwp50)).toBe('+495 t');
      expect(formatImpactLegendTick(1e9, Number.NaN, FACTORS.gwp50)).toBe(
        '+495 t',
      );
    });

    it('scales with the selected GWP horizon', () => {
      expect(formatImpactLegendTick(1e9, 1, FACTORS.gwp20)).toBe('+1.0 kt');
      expect(formatImpactLegendTick(1e9, 1, FACTORS.gwp100)).toBe('+285 t');
    });

    it('keeps the legacy MJ legend when no factor is available', () => {
      expect(formatImpactLegendTick(228e9, 1)).toBe('+228B');
      expect(formatImpactLegendTick(10e6, 1)).toBe('+10M');
      expect(formatImpactLegendTick(-1e3, 1)).toBe('-1K');
      expect(impactLegendTitle(undefined, 'GWP50')).toBe('Impact (EF MJ)');
      expect(impactLegendTitle(FACTORS.gwp100, 'GWP100')).toBe('CO₂e (GWP100)');
    });
  });

  describe('formatDensityLegendTick', () => {
    it('formats ticks across integer and decimal magnitudes', () => {
      expect(formatDensityLegendTick(25, 1)).toBe('25');
      expect(formatDensityLegendTick(12.34, 1)).toBe('12.3');
      expect(formatDensityLegendTick(3.5, 1)).toBe('3.5');
      expect(formatDensityLegendTick(0.256, 1)).toBe('0.26');
      expect(formatDensityLegendTick(340, 34)).toBe('10');
    });

    it('appends flights suffix for zero, top ticks, and guard fallbacks', () => {
      expect(formatDensityLegendTick(0, 1)).toBe('0 flights');
      expect(formatDensityLegendTick(1234.6, 1, {isTop: true})).toBe(
        '1,235 flights',
      );
      expect(formatDensityLegendTick(Number.NaN, 1)).toBe('0 flights');
      expect(
        formatDensityLegendTick(Number.POSITIVE_INFINITY, 1, {isTop: true}),
      ).toBe('0 flights');
      expect(formatDensityLegendTick(100, 0)).toBe('100');
    });
  });

  describe('FLOAT16_TO_FLOAT32_LUT', () => {
    it('decodes IEEE 754 binary16 zero, negative zero, one, and negative one', () => {
      expect(FLOAT16_TO_FLOAT32_LUT.length).toBe(65536);
      expect(Object.is(FLOAT16_TO_FLOAT32_LUT[0x0000], 0)).toBeTrue();
      expect(Object.is(FLOAT16_TO_FLOAT32_LUT[0x8000], -0)).toBeTrue();
      expect(FLOAT16_TO_FLOAT32_LUT[0x3c00]).toBe(1.0);
      expect(FLOAT16_TO_FLOAT32_LUT[0xbc00]).toBe(-1.0);
      expect(FLOAT16_TO_FLOAT32_LUT[0x4000]).toBe(2.0);
    });

    it('decodes binary16 positive/negative Infinity and NaN accurately', () => {
      expect(FLOAT16_TO_FLOAT32_LUT[0x7c00]).toBe(Number.POSITIVE_INFINITY);
      expect(FLOAT16_TO_FLOAT32_LUT[0xfc00]).toBe(Number.NEGATIVE_INFINITY);
      expect(Number.isNaN(FLOAT16_TO_FLOAT32_LUT[0x7e00])).toBeTrue();
      expect(Number.isNaN(FLOAT16_TO_FLOAT32_LUT[0xfe00])).toBeTrue();
    });

    it('decodes smallest positive subnormal binary16 number (2^-24)', () => {
      expect(FLOAT16_TO_FLOAT32_LUT[0x0001]).toBeCloseTo(Math.pow(2, -24), 12);
    });
  });

  describe('getLodForZoom', () => {
    it('returns LOD 0 below zoom 1.8, LOD 1 below 2.8, and LOD 2 at or above 2.8', () => {
      expect(getLodForZoom(0.0)).toBe(0);
      expect(getLodForZoom(1.79)).toBe(0);
      expect(getLodForZoom(1.8)).toBe(1);
      expect(getLodForZoom(2.79)).toBe(1);
      expect(getLodForZoom(2.8)).toBe(2);
      expect(getLodForZoom(5.5)).toBe(2);
    });
  });

  describe('latDegToMercatorV and mercatorYToLatDeg', () => {
    it('maps global Mercator bounds [-85.05112878, +85.05112878] to [-1, +1]', () => {
      expect(latDegToMercatorV(0)).toBeCloseTo(0, 8);
      expect(latDegToMercatorV(MAX_MERCATOR_LATITUDE)).toBeCloseTo(1.0, 5);
      expect(latDegToMercatorV(-MAX_MERCATOR_LATITUDE)).toBeCloseTo(-1.0, 5);
    });

    it('maps yMerc in [0, 1] accurately for both global and sub-global regional bounding boxes', () => {
      // Global bounds: top (0) -> +MAX_MERCATOR_LATITUDE, center (0.5) -> 0
      // deg, bottom (1) -> -MAX_MERCATOR_LATITUDE
      const globalSpan = 2 * MAX_MERCATOR_LATITUDE;
      expect(
        mercatorYToLatDeg(0, -MAX_MERCATOR_LATITUDE, globalSpan),
      ).toBeCloseTo(MAX_MERCATOR_LATITUDE, 5);
      expect(
        mercatorYToLatDeg(0.5, -MAX_MERCATOR_LATITUDE, globalSpan),
      ).toBeCloseTo(0, 6);
      expect(
        mercatorYToLatDeg(1, -MAX_MERCATOR_LATITUDE, globalSpan),
      ).toBeCloseTo(-MAX_MERCATOR_LATITUDE, 5);

      // Regional European bbox: [35 deg N, 70 deg N]
      expect(mercatorYToLatDeg(0, 35, 35)).toBeCloseTo(70, 5);
      expect(mercatorYToLatDeg(1, 35, 35)).toBeCloseTo(35, 5);
      const midLat = mercatorYToLatDeg(0.5, 35, 35);
      expect(midLat).toBeGreaterThan(52);
      expect(midLat).toBeLessThan(57);
    });

    it('returns clamped minLatBound when latRange is zero or degenerate', () => {
      expect(mercatorYToLatDeg(0.5, 42.5, 0)).toBeCloseTo(42.5, 6);
      expect(mercatorYToLatDeg(0.5, 95, 1e-8)).toBeCloseTo(
        MAX_MERCATOR_LATITUDE,
        5,
      );
    });
  });

  const f32Scratch = new Float32Array(1);
  const u32Scratch = new Uint32Array(f32Scratch.buffer);

  const encodeFloat16 = (targetValue: number): number => {
    if (Number.isNaN(targetValue)) return 0x7e00;
    if (targetValue === Number.POSITIVE_INFINITY) return 0x7c00;
    if (targetValue === Number.NEGATIVE_INFINITY) return 0xfc00;
    if (targetValue === 0) return 0;
    f32Scratch[0] = targetValue;
    const bits = u32Scratch[0];
    const sign = (bits >>> 16) & 0x8000;
    const exp = ((bits >>> 23) & 0xff) - 127 + 15;
    const mantissa = bits & 0x7fffff;
    if (exp <= 0) {
      return sign;
    }
    if (exp >= 31) {
      return sign | 0x7c00;
    }
    const rounded = (mantissa + 0x1000) >>> 13;
    return sign | ((exp << 10) + rounded);
  };

  const toFloat16Array = (values: readonly number[]): Uint16Array =>
    Uint16Array.from(values, encodeFloat16);

  describe('renderGridOnCpu', () => {
    const globalEnv = globalThis as unknown as Record<string, unknown>;
    let originalDocument: unknown;

    beforeEach(() => {
      originalDocument = globalEnv['document'];
    });

    afterEach(() => {
      globalEnv['document'] = originalDocument;
    });

    it('returns null when document is unavailable or signal is aborted', async () => {
      const controller = new AbortController();
      controller.abort();
      const res = await renderGridOnCpu({
        meta: {width: 4, height: 4},
        float16Data: new Uint16Array(16),
        mode: 'impact',
        valueMax: 1e6,
        minLatBound: -85.05112878,
        latRange: 170.10225756,
        signal: controller.signal,
      });
      expect(res).toBeNull();
    });

    it('rasterizes impact and density grids with LUT colors and masks NaN/Infinity/zero pixels', async () => {
      let lastImageData: {data: Uint8ClampedArray} | null = null;
      globalEnv['document'] = {
        createElement: () => ({
          width: 0,
          height: 0,
          getContext: () => ({
            createImageData: (w: number, h: number) => ({
              data: new Uint8ClampedArray(w * h * 4),
            }),
            putImageData: (imgData: {data: Uint8ClampedArray}) => {
              lastImageData = imgData;
            },
          }),
          toDataURL: () => 'data:image/png;base64,mock',
        }),
      };

      const grid = toFloat16Array([
        0,
        Number.NaN,
        Number.POSITIVE_INFINITY,
        1.13,
      ]);
      const urlImpact = await renderGridOnCpu({
        meta: {width: 2, height: 2},
        float16Data: grid,
        mode: 'impact',
        valueMax: 1e6,
        minLatBound: -85.05112878,
        latRange: 170.10225756,
      });
      expect(urlImpact).toBe('data:image/png;base64,mock');
      expect(lastImageData).not.toBeNull();
      const rgbaImpact = lastImageData!.data;
      // canvasY=0 maps to northern hemisphere (r=1: grid[2]=+Infinity,
      // grid[3]=1.13).
      // canvasY=1 maps to southern hemisphere (r=0: grid[0]=0, grid[1]=NaN).
      expect(rgbaImpact[3]).toBe(0);
      expect(rgbaImpact[7]).toBeGreaterThan(0);
      expect(rgbaImpact[11]).toBe(0);
      expect(rgbaImpact[15]).toBe(0);

      // Also verify density mode.
      const urlDensity = await renderGridOnCpu({
        meta: {width: 2, height: 2},
        float16Data: toFloat16Array([0.5, 2.0, 3.5, 0]),
        mode: 'density',
        valueMax: 1e6,
        minLatBound: 30,
        latRange: 30,
      });
      expect(urlDensity).toBe('data:image/png;base64,mock');
    });

    it('maps norm=0.82 to soft pink, norm=0.89 to dark red, norm=0.91 to pure red, and norm>0.91 into the yellow band', async () => {
      let lastImageData: {data: Uint8ClampedArray} | null = null;
      globalEnv['document'] = {
        createElement: () => ({
          width: 0,
          height: 0,
          getContext: () => ({
            createImageData: (w: number, h: number) => ({
              data: new Uint8ClampedArray(w * h * 4),
            }),
            putImageData: (imgData: {data: Uint8ClampedArray}) => {
              lastImageData = imgData;
            },
          }),
          toDataURL: () => 'data:image/png;base64,mock',
        }),
      };

      const valueMax = 1e10 - 1; // logScale = 10
      // norm = 0.5 + 0.5 * (logVal / 10):
      // norm = 0.82 -> logVal = 6.4 (soft pink: r=255, g=160, b=160, alpha=175)
      // norm = 0.89 -> logVal = 7.8 (dark red: r=120, g=0, b=0, alpha=250)
      // norm = 0.91 -> logVal = 8.2 (pure red: r=255, g=0, b=0, alpha=250)
      // norm = 0.955 -> logVal = 9.1 (midpoint orange: r=255, g~128, b=0)
      // norm = 1.00 -> logVal = 10.0 (yellow: r=255, g=255, b=0)
      await renderGridOnCpu({
        meta: {width: 5, height: 1},
        float16Data: toFloat16Array([6.4, 7.8, 8.2, 9.1, 10.0]),
        mode: 'impact',
        valueMax,
        minLatBound: 0,
        latRange: 10,
      });
      expect(lastImageData).not.toBeNull();
      const px = lastImageData!.data;
      // Pixel 0 (norm = 0.82): soft pink (255, ~160, ~160)
      expect(px[0]).toBe(255);
      expect(px[1]).toBeCloseTo(160, -1);
      expect(px[2]).toBeCloseTo(160, -1);
      expect(px[3]).toBeCloseTo(175, -1);
      // Pixel 1 (norm = 0.89 -> LUT index 910/1023 = 0.8895): dark red (~120,
      // ~0, ~0) at plateaued alpha (~250)
      expect(px[4]).toBeCloseTo(120, -1);
      expect(px[5]).toBeLessThanOrEqual(1);
      expect(px[6]).toBeLessThanOrEqual(1);
      expect(px[7]).toBeCloseTo(250, -1);
      // Pixel 2 (norm = 0.91): pure red (255, 0, 0) at plateaued alpha (250)
      expect(px[8]).toBe(255);
      expect(px[9]).toBeLessThanOrEqual(3);
      expect(px[10]).toBe(0);
      expect(px[11]).toBe(250);
      // Pixel 3 (norm = 0.955): orange (255, ~128, 0)
      expect(px[12]).toBe(255);
      expect(px[13]).toBeGreaterThan(120);
      expect(px[13]).toBeLessThan(136);
      expect(px[14]).toBe(0);
      // Pixel 4 (norm = 1.00): yellow (255, 255, 0)
      expect(px[16]).toBe(255);
      expect(px[17]).toBe(255);
      expect(px[18]).toBe(0);
    });
  });

  describe('SharedGridGpuRenderer', () => {
    const globalEnv = globalThis as unknown as Record<string, unknown>;
    let originalDocument: unknown;

    beforeEach(() => {
      originalDocument = globalEnv['document'];
    });

    afterEach(() => {
      globalEnv['document'] = originalDocument;
    });

    it('reuses allocated texture via texSubImage2D when dimensions match and reallocates via texImage2D when dimensions change', async () => {
      let texImage2DCalls = 0;
      let texSubImage2DCalls = 0;
      let capturedContextAttrs: Record<string, unknown> | undefined;
      const mockGl = {
        VERTEX_SHADER: 0x8b31,
        FRAGMENT_SHADER: 0x8b30,
        COMPILE_STATUS: 0x8b81,
        LINK_STATUS: 0x8b82,
        ARRAY_BUFFER: 0x8892,
        STATIC_DRAW: 0x88e4,
        FLOAT: 0x1406,
        HALF_FLOAT: 0x140b,
        TEXTURE_2D: 0x0de1,
        TEXTURE0: 0x84c0,
        TEXTURE_MIN_FILTER: 0x2801,
        TEXTURE_MAG_FILTER: 0x2800,
        TEXTURE_WRAP_S: 0x2802,
        TEXTURE_WRAP_T: 0x2803,
        NEAREST: 0x2600,
        CLAMP_TO_EDGE: 0x812f,
        R16F: 0x822d,
        RED: 0x1903,
        COLOR_BUFFER_BIT: 0x4000,
        TRIANGLE_STRIP: 0x0005,
        isContextLost: () => false,
        createShader: () => ({}),
        shaderSource: () => {},
        compileShader: () => {},
        getShaderParameter: () => true,
        getShaderInfoLog: () => '',
        deleteShader: () => {},
        createProgram: () => ({}),
        attachShader: () => {},
        detachShader: () => {},
        linkProgram: () => {},
        getProgramParameter: () => true,
        getProgramInfoLog: () => '',
        deleteProgram: () => {},
        createBuffer: () => ({}),
        bindBuffer: () => {},
        bufferData: () => {},
        getAttribLocation: () => 0,
        enableVertexAttribArray: () => {},
        vertexAttribPointer: () => {},
        createTexture: () => ({}),
        bindTexture: () => {},
        texParameteri: () => {},
        getUniformLocation: () => ({}),
        viewport: () => {},
        clearColor: () => {},
        clear: () => {},
        useProgram: () => {},
        activeTexture: () => {},
        texImage2D: () => {
          texImage2DCalls++;
        },
        texSubImage2D: () => {
          texSubImage2DCalls++;
        },
        uniform1i: () => {},
        uniform1f: () => {},
        drawArrays: () => {},
      };

      globalEnv['document'] = {
        createElement: () => ({
          width: 0,
          height: 0,
          getContext: (_type: string, attrs?: Record<string, unknown>) => {
            capturedContextAttrs = attrs;
            return mockGl;
          },
          toDataURL: () => 'data:image/png;base64,gpu_mock',
        }),
      };

      const renderer = new SharedGridGpuRenderer();
      const firstUrl = await renderer.render({
        meta: {width: 4, height: 4},
        float16Data: new Uint16Array(16),
        mode: 'impact',
        valueMax: 1e6,
        minLatBound: -85.05112878,
        latRange: 170.10225756,
      });
      expect(firstUrl).toBe('data:image/png;base64,gpu_mock');
      expect(capturedContextAttrs?.['premultipliedAlpha']).toBe(false);
      expect(texImage2DCalls).toBe(1);
      expect(texSubImage2DCalls).toBe(0);

      // Second render with identical 4x4 dimensions should call texSubImage2D
      await renderer.render({
        meta: {width: 4, height: 4},
        float16Data: new Uint16Array(16),
        mode: 'density',
        valueMax: 1e6,
        minLatBound: -85.05112878,
        latRange: 170.10225756,
      });
      expect(texImage2DCalls).toBe(1);
      expect(texSubImage2DCalls).toBe(1);

      // Third render with 8x8 dimensions should reallocate via texImage2D
      await renderer.render({
        meta: {width: 8, height: 8},
        float16Data: new Uint16Array(64),
        mode: 'impact',
        valueMax: 1e6,
        minLatBound: -85.05112878,
        latRange: 170.10225756,
      });
      expect(texImage2DCalls).toBe(2);
      expect(texSubImage2DCalls).toBe(1);
    });

    it('returns null when dimensions, buffer length, or valueMax are invalid', async () => {
      const renderer = new SharedGridGpuRenderer();
      expect(
        await renderer.render({
          meta: {width: 0, height: 4},
          float16Data: new Uint16Array(0),
          mode: 'impact',
          valueMax: 1e6,
          minLatBound: -85,
          latRange: 170,
        }),
      ).toBeNull();
      expect(
        await renderer.render({
          meta: {width: 4, height: 4},
          float16Data: new Uint16Array(8),
          mode: 'impact',
          valueMax: 1e6,
          minLatBound: -85,
          latRange: 170,
        }),
      ).toBeNull();

      const baseZeroScaleOpts = {
        meta: {width: 4, height: 4},
        float16Data: new Uint16Array(16),
        mode: 'impact' as const,
        minLatBound: -85,
        latRange: 170,
      };
      expect(
        await renderer.render({...baseZeroScaleOpts, valueMax: 0}),
      ).toBeNull();
      expect(
        await renderer.render({...baseZeroScaleOpts, valueMax: -1}),
      ).toBeNull();
      expect(
        await renderer.render({...baseZeroScaleOpts, valueMax: Number.NaN}),
      ).toBeNull();
    });

    it('uploads float16Data directly via gl.R16F + gl.HALF_FLOAT and supports float16Data in renderGridOnCpu', async () => {
      const uploadedFormats: Array<{
        method: 'texImage2D' | 'texSubImage2D';
        internalFormat?: number;
        type: number;
        data: unknown;
      }> = [];
      const mockGl = {
        VERTEX_SHADER: 0x8b31,
        FRAGMENT_SHADER: 0x8b30,
        COMPILE_STATUS: 0x8b81,
        LINK_STATUS: 0x8b82,
        ARRAY_BUFFER: 0x8892,
        STATIC_DRAW: 0x88e4,
        FLOAT: 0x1406,
        HALF_FLOAT: 0x140b,
        TEXTURE_2D: 0x0de1,
        TEXTURE0: 0x84c0,
        TEXTURE_MIN_FILTER: 0x2801,
        TEXTURE_MAG_FILTER: 0x2800,
        TEXTURE_WRAP_S: 0x2802,
        TEXTURE_WRAP_T: 0x2803,
        NEAREST: 0x2600,
        CLAMP_TO_EDGE: 0x812f,
        R16F: 0x822d,
        R32F: 0x822e,
        RED: 0x1903,
        COLOR_BUFFER_BIT: 0x4000,
        TRIANGLE_STRIP: 0x0005,
        isContextLost: () => false,
        createShader: () => ({}),
        shaderSource: () => {},
        compileShader: () => {},
        getShaderParameter: () => true,
        getShaderInfoLog: () => '',
        deleteShader: () => {},
        createProgram: () => ({}),
        attachShader: () => {},
        detachShader: () => {},
        linkProgram: () => {},
        getProgramParameter: () => true,
        getProgramInfoLog: () => '',
        deleteProgram: () => {},
        createBuffer: () => ({}),
        bindBuffer: () => {},
        bufferData: () => {},
        getAttribLocation: () => 0,
        enableVertexAttribArray: () => {},
        vertexAttribPointer: () => {},
        createTexture: () => ({}),
        bindTexture: () => {},
        texParameteri: () => {},
        getUniformLocation: () => ({}),
        viewport: () => {},
        clearColor: () => {},
        clear: () => {},
        useProgram: () => {},
        activeTexture: () => {},
        texImage2D: (
          _target: number,
          _level: number,
          internalFormat: number,
          _width: number,
          _height: number,
          _border: number,
          _format: number,
          type: number,
          data: unknown,
        ) => {
          uploadedFormats.push({
            method: 'texImage2D',
            internalFormat,
            type,
            data,
          });
        },
        texSubImage2D: (
          _target: number,
          _level: number,
          _xoffset: number,
          _yoffset: number,
          _width: number,
          _height: number,
          _format: number,
          type: number,
          data: unknown,
        ) => {
          uploadedFormats.push({method: 'texSubImage2D', type, data});
        },
        uniform1i: () => {},
        uniform1f: () => {},
        drawArrays: () => {},
      };

      let lastCpuImageData: {data: Uint8ClampedArray} | null = null;
      globalEnv['document'] = {
        createElement: () => ({
          width: 0,
          height: 0,
          getContext: (type: string) =>
            type === 'webgl2'
              ? mockGl
              : {
                  createImageData: (w: number, h: number) => ({
                    data: new Uint8ClampedArray(w * h * 4),
                  }),
                  putImageData: (imgData: {data: Uint8ClampedArray}) => {
                    lastCpuImageData = imgData;
                  },
                },
          toDataURL: () => 'data:image/png;base64,half_float_mock',
        }),
      };

      // 0x3c00 = 1.0 in IEEE 754 binary16
      const f16Grid = new Uint16Array([0x3c00, 0x3c00, 0x3c00, 0x3c00]);
      const renderer = new SharedGridGpuRenderer();

      const firstUrl = await renderer.render({
        meta: {width: 2, height: 2},
        float16Data: f16Grid,
        mode: 'impact',
        valueMax: 1e6,
        minLatBound: -85,
        latRange: 170,
      });
      expect(firstUrl).toBe('data:image/png;base64,half_float_mock');
      expect(uploadedFormats.length).toBe(1);
      expect(uploadedFormats[0]).toEqual({
        method: 'texImage2D',
        internalFormat: mockGl.R16F,
        type: mockGl.HALF_FLOAT,
        data: f16Grid,
      });

      // Second float16 render with identical dimensions reuses R16F texture via
      // texSubImage2D
      await renderer.render({
        meta: {width: 2, height: 2},
        float16Data: f16Grid,
        mode: 'density',
        valueMax: 1e6,
        minLatBound: -85,
        latRange: 170,
      });
      expect(uploadedFormats.length).toBe(2);
      expect(uploadedFormats[1]).toEqual({
        method: 'texSubImage2D',
        type: mockGl.HALF_FLOAT,
        data: f16Grid,
      });

      // CPU fallback also decodes float16Data directly via
      // FLOAT16_TO_FLOAT32_LUT
      const cpuUrl = await renderGridOnCpu({
        meta: {width: 2, height: 2},
        float16Data: f16Grid,
        mode: 'impact',
        valueMax: 1e6,
        minLatBound: -85,
        latRange: 170,
      });
      expect(cpuUrl).toBe('data:image/png;base64,half_float_mock');
      expect(lastCpuImageData).not.toBeNull();
      expect(lastCpuImageData!.data[3]).toBeGreaterThan(0);
    });

    it('uploads hoisted u_vMax and u_vMin Mercator bounds uniforms and recovers after webglcontextlost', async () => {
      const uniformValues = new Map<string, number>();
      const listeners = new Map<string, (e: Event) => void>();
      let createProgramCalls = 0;

      const mockGl = {
        VERTEX_SHADER: 0x8b31,
        FRAGMENT_SHADER: 0x8b30,
        COMPILE_STATUS: 0x8b81,
        LINK_STATUS: 0x8b82,
        ARRAY_BUFFER: 0x8892,
        STATIC_DRAW: 0x88e4,
        FLOAT: 0x1406,
        HALF_FLOAT: 0x140b,
        TEXTURE_2D: 0x0de1,
        TEXTURE0: 0x84c0,
        TEXTURE_MIN_FILTER: 0x2801,
        TEXTURE_MAG_FILTER: 0x2800,
        TEXTURE_WRAP_S: 0x2802,
        TEXTURE_WRAP_T: 0x2803,
        NEAREST: 0x2600,
        CLAMP_TO_EDGE: 0x812f,
        R16F: 0x822d,
        R32F: 0x822e,
        RED: 0x1903,
        COLOR_BUFFER_BIT: 0x4000,
        TRIANGLE_STRIP: 0x0005,
        isContextLost: () => false,
        createShader: () => ({}),
        shaderSource: () => {},
        compileShader: () => {},
        getShaderParameter: () => true,
        getShaderInfoLog: () => '',
        deleteShader: () => {},
        createProgram: () => {
          createProgramCalls++;
          return {};
        },
        attachShader: () => {},
        detachShader: () => {},
        linkProgram: () => {},
        getProgramParameter: () => true,
        getProgramInfoLog: () => '',
        deleteProgram: () => {},
        createBuffer: () => ({}),
        bindBuffer: () => {},
        bufferData: () => {},
        getAttribLocation: () => 0,
        enableVertexAttribArray: () => {},
        vertexAttribPointer: () => {},
        createTexture: () => ({}),
        bindTexture: () => {},
        texParameteri: () => {},
        getUniformLocation: (_prog: unknown, name: string) =>
          name as unknown as WebGLUniformLocation,
        viewport: () => {},
        clearColor: () => {},
        clear: () => {},
        useProgram: () => {},
        activeTexture: () => {},
        texImage2D: () => {},
        texSubImage2D: () => {},
        uniform1i: () => {},
        uniform1f: (loc: unknown, val: number) => {
          uniformValues.set(String(loc), val);
        },
        drawArrays: () => {},
      };

      globalEnv['document'] = {
        createElement: () => ({
          width: 0,
          height: 0,
          addEventListener: (type: string, cb: (e: Event) => void) => {
            listeners.set(type, cb);
          },
          getContext: () => mockGl,
          toDataURL: () => 'data:image/png;base64,context_loss_mock',
        }),
      };

      const renderer = new SharedGridGpuRenderer();
      await renderer.render({
        meta: {width: 2, height: 2},
        float16Data: new Uint16Array([0x3c00, 0x3c00, 0x3c00, 0x3c00]),
        mode: 'impact',
        valueMax: 1e6,
        minLatBound: -60,
        latRange: 120,
      });

      expect(createProgramCalls).toBe(1);
      expect(uniformValues.get('u_vMax')).toBeCloseTo(latDegToMercatorV(60), 6);
      expect(uniformValues.get('u_vMin')).toBeCloseTo(
        latDegToMercatorV(-60),
        6,
      );

      // Simulate webglcontextlost and verify preventDefault() + clean re-init
      // on next render
      let prevented = false;
      const lostHandler = listeners.get('webglcontextlost');
      expect(lostHandler).toBeDefined();
      lostHandler!({
        preventDefault: () => {
          prevented = true;
        },
      } as unknown as Event);
      expect(prevented).toBeTrue();

      const recoveredUrl = await renderer.render({
        meta: {width: 2, height: 2},
        float16Data: new Uint16Array([0x3c00, 0x3c00, 0x3c00, 0x3c00]),
        mode: 'impact',
        valueMax: 1e6,
        minLatBound: -60,
        latRange: 120,
      });
      expect(recoveredUrl).toBe('data:image/png;base64,context_loss_mock');
      expect(createProgramCalls).toBe(2);
    });
  });
});

describe('toGwpHorizonKey', () => {
  it('maps each filter GWP label to its metadata factor key', () => {
    expect(toGwpHorizonKey('GWP20')).toBe('gwp20');
    expect(toGwpHorizonKey('GWP50')).toBe('gwp50');
    expect(toGwpHorizonKey('GWP100')).toBe('gwp100');
  });
});

describe('parseGwpTonnesPerMj type narrowing', () => {
  it('rejects non-object factor payloads', () => {
    expect(parseGwpTonnesPerMj({gwpTonnesCo2ePerMj: 'bogus'})).toBeUndefined();
  });

  it('returns exactly the three horizon factors and drops extra keys', () => {
    expect(
      parseGwpTonnesPerMj({
        gwpTonnesCo2ePerMj: {gwp20: 3, gwp50: 2, gwp100: 1, gwp500: 9},
      }),
    ).toEqual({gwp20: 3, gwp50: 2, gwp100: 1});
  });
});
