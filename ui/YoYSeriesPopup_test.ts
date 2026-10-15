import {
  installBrowserGlobalStubs,
  restoreBrowserGlobals,
} from './test_browser_globals';

// Load browser global stubs above before importing modules under test.
import 'jasmine';
import {TimeSeriesPoint} from './CountryMetadata';
import {
  buildSeriesSvgPaths,
  computeSeriesScale,
  deriveYoYPercent,
  getSeriesX,
  getSeriesY,
  hasFiniteValue,
} from './YoYSeriesPopup';

describe('YoYSeriesPopup Logic Unit Tests', () => {
  beforeAll(() => {
    installBrowserGlobalStubs({react: true, materialUi: true});
  });

  afterAll(() => {
    restoreBrowserGlobals();
  });

  const samplePoints: TimeSeriesPoint[] = [
    {year: 2022, value: 1631769.2},
    {year: 2023, value: 1545125.0},
    {year: 2024, value: 1831146.2},
    {year: 2025, value: 1439349.5},
  ];

  describe('Value range and scaling logic', () => {
    it('computes correct min, max and headroom', () => {
      const scale = computeSeriesScale(samplePoints);

      expect(scale).toEqual({
        minVal: 0,
        yUpper: 2105818.13,
      });
    });

    it('handles all-zero series and all-negative series cleanly', () => {
      const zeroScale = computeSeriesScale([
        {year: 2022, value: 0},
        {year: 2023, value: 0},
      ]);
      expect(zeroScale).toEqual({
        minVal: 0,
        yUpper: 1,
      });

      const negativeScale = computeSeriesScale([
        {year: 2022, value: -20},
        {year: 2023, value: -10},
      ]);
      expect(negativeScale).toEqual({
        minVal: -20,
        yUpper: 3,
      });
    });

    it('maps Y coordinates correctly and guards against zero denominator', () => {
      const padTop = 25;
      const chartH = 120;
      const minVal = 0;
      const yUpper = 2000000;

      expect(getSeriesY({val: 0, minVal, yUpper, padTop, chartH})).toBe(
        padTop + chartH,
      );
      expect(getSeriesY({val: 2000000, minVal, yUpper, padTop, chartH})).toBe(
        padTop,
      );
      expect(getSeriesY({val: 1000000, minVal, yUpper, padTop, chartH})).toBe(
        padTop + chartH * 0.5,
      );
      expect(getSeriesY({val: 0, minVal: 0, yUpper: 0, padTop, chartH})).toBe(
        padTop + chartH,
      );
    });

    it('maps X coordinates evenly across 4 years', () => {
      const padLeft = 60;
      const chartW = 330;
      const total = 4;

      expect(getSeriesX({idx: 0, total, padLeft, chartW})).toBe(padLeft);
      expect(getSeriesX({idx: 1, total, padLeft, chartW})).toBe(padLeft + 110);
      expect(getSeriesX({idx: 2, total, padLeft, chartW})).toBe(padLeft + 220);
      expect(getSeriesX({idx: 3, total, padLeft, chartW})).toBe(
        padLeft + chartW,
      );
    });
  });

  describe('hasFiniteValue', () => {
    it('accepts finite values including zero and negatives', () => {
      expect(
        [0, -3.5, 42].map((value) => hasFiniteValue({year: 2024, value})),
      ).toEqual([true, true, true]);
    });

    it('rejects null, NaN, and infinite values', () => {
      expect(
        [null, NaN, Infinity, -Infinity].map((value) =>
          hasFiniteValue({year: 2024, value}),
        ),
      ).toEqual([false, false, false, false]);
    });
  });

  describe('YoY derivation from the raw series', () => {
    it('derives the percent change between consecutive years', () => {
      expect(deriveYoYPercent(samplePoints, 1)).toBeCloseTo(-5.3, 1);
      expect(deriveYoYPercent(samplePoints, 2)).toBeCloseTo(18.5, 1);
      expect(deriveYoYPercent(samplePoints, 3)).toBeCloseTo(-21.4, 1);
    });

    it('uses Math.abs(prev) when baseline value is negative', () => {
      const negSeries: TimeSeriesPoint[] = [
        {year: 2022, value: -10},
        {year: 2023, value: -5},
      ];
      expect(deriveYoYPercent(negSeries, 1)).toBeCloseTo(50, 1);
    });

    it('has no change to report for the baseline year', () => {
      expect(deriveYoYPercent(samplePoints, 0)).toBeNull();
    });

    it('refuses to divide by a zero baseline', () => {
      const zeroBase: TimeSeriesPoint[] = [
        {year: 2022, value: 0},
        {year: 2023, value: 10},
      ];
      expect(deriveYoYPercent(zeroBase, 1)).toBeNull();
    });

    it('returns null when points are not consecutive calendar years', () => {
      const gappedSeries: TimeSeriesPoint[] = [
        {year: 2022, value: 100},
        {year: 2024, value: 150},
      ];
      expect(deriveYoYPercent(gappedSeries, 1)).toBeNull();
    });

    it('splits SVG line and area paths into disjoint segments across missing middle years', () => {
      const gappedPoints = [
        {year: 2022, val: 100, hasVal: true, x: 60, y: 80, yoyPct: null},
        {year: 2023, val: 0, hasVal: false, x: 170, y: 145, yoyPct: null},
        {year: 2024, val: 120, hasVal: true, x: 280, y: 70, yoyPct: null},
        {year: 2025, val: 140, hasVal: true, x: 390, y: 60, yoyPct: 16.7},
      ];
      const {pathD, areaD} = buildSeriesSvgPaths(gappedPoints, 145);
      // Segment 1 (2022 alone) and Segment 2 (2024 -> 2025) must start with
      // separate M commands
      expect(pathD).toBe('M 60.0 80.0 M 280.0 70.0 L 390.0 60.0');
      expect(areaD).toBe(
        'M 280.0 70.0 L 390.0 60.0 L 390.0 145.0 L 280.0 145.0 Z',
      );
    });

    it('guards against NaN and Infinity values in computeSeriesScale, getSeriesY, and deriveYoYPercent', () => {
      const nonFiniteSeries: TimeSeriesPoint[] = [
        {year: 2022, value: Number.NaN},
        {year: 2023, value: 100},
        {year: 2024, value: Number.POSITIVE_INFINITY},
      ];
      const scale = computeSeriesScale(nonFiniteSeries);
      expect(scale).toEqual({
        minVal: 0,
        yUpper: 115,
      });

      expect(
        getSeriesY({
          val: Number.NaN,
          minVal: 0,
          yUpper: 100,
          padTop: 25,
          chartH: 120,
        }),
      ).toBe(145);
      expect(deriveYoYPercent(nonFiniteSeries, 1)).toBeNull();
      expect(deriveYoYPercent(nonFiniteSeries, 2)).toBeNull();
    });
  });
});
