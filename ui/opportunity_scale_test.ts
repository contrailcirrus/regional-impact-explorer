/**
 * @fileoverview Unit tests for sigmoid scaling, percentile ranking, and color
 * helpers in opportunity_scale.ts.
 */

import 'jasmine';

import {
  buildSigmoidTrackGradient,
  CELL_STROKE_COLOR,
  CELL_STROKE_WIDTH,
  clampSigmoidMidpoint,
  computePercentileRanks,
  computeSigmoid,
  computeSigmoidCongestion,
  computeSigmoidImpact,
  CONGESTION_DIAGRAM_LABELS,
  CONGESTION_SIGMOID_BAND,
  DEFAULT_CONGESTION_MIDPOINT,
  DEFAULT_IMPACT_PERCENTILE_MIDPOINT,
  deriveSigmoidSlope,
  formatBadgeFlightText,
  getCongestionColor,
  getImpactColor,
  getOpportunityScoreColor,
  HOVER_STROKE_COLOR,
  HOVER_STROKE_WIDTH,
  IMPACT_SIGMOID_BAND,
  interpolateRdYlGnHex,
  MAX_SIGMOID_MIDPOINT,
  MIN_SIGMOID_MIDPOINT,
  THRESHOLD_STROKE_COLOR,
  THRESHOLD_STROKE_WIDTH,
} from './opportunity_scale';

describe('opportunity_scale', () => {
  describe('computeSigmoidCongestion', () => {
    it('outputs near 0 at 70% congestion', () => {
      const val70 = computeSigmoidCongestion(0.7);
      expect(val70).toBeLessThan(0.05);
      expect(val70).toBeGreaterThanOrEqual(0.0);
    });

    it('outputs 0.5 at 80% congestion', () => {
      expect(computeSigmoidCongestion(0.8)).toBeCloseTo(0.5, 2);
    });

    it('outputs near 1 for congestions at and above 90%', () => {
      const val90 = computeSigmoidCongestion(0.9);
      expect(val90).toBeGreaterThan(0.95);
      expect(val90).toBeLessThanOrEqual(1.0);
      expect(computeSigmoidCongestion(0.95)).toBeGreaterThan(0.98);
      expect(computeSigmoidCongestion(1.0)).toBeGreaterThan(0.99);
    });

    it('handles zero, negative, or NaN congestion', () => {
      expect(computeSigmoidCongestion(0)).toBe(0.0);
      expect(computeSigmoidCongestion(-0.5)).toBe(0.0);
      expect(computeSigmoidCongestion(NaN)).toBe(0.0);
    });

    it('maintains strict monotonic scaling across lower and middle bands', () => {
      expect(computeSigmoidCongestion(0.5)).toBeGreaterThan(
        computeSigmoidCongestion(0.1),
      );
      expect(computeSigmoidCongestion(0.75)).toBeGreaterThan(
        computeSigmoidCongestion(0.5),
      );
      expect(computeSigmoidCongestion(0.8)).toBeGreaterThan(
        computeSigmoidCongestion(0.75),
      );
      expect(computeSigmoidCongestion(0.85)).toBeGreaterThan(
        computeSigmoidCongestion(0.8),
      );
    });

    it('maintains strict monotonic scaling across upper bands and [0, 1] bounds', () => {
      expect(computeSigmoidCongestion(0.9)).toBeGreaterThan(
        computeSigmoidCongestion(0.85),
      );
      expect(computeSigmoidCongestion(0.95)).toBeGreaterThan(
        computeSigmoidCongestion(0.9),
      );
      expect(computeSigmoidCongestion(1.0)).toBeGreaterThan(
        computeSigmoidCongestion(0.95),
      );
      expect(computeSigmoidCongestion(0)).toBe(0.0);
      expect(computeSigmoidCongestion(1.5)).toBeLessThanOrEqual(1.0);
    });
  });

  describe('Opportunity Index Color Coding & Badge Formatting', () => {
    it('returns green for low, amber for moderate, and red for high congestion', () => {
      expect(getCongestionColor(0.15)).toBe('#137333');
      expect(getCongestionColor(0.35)).toBe('#137333');
      expect(getCongestionColor(0.5)).toBe('#b06000');
      expect(getCongestionColor(0.7)).toBe('#c5221f');
      expect(getCongestionColor(1.0)).toBe('#c5221f');
    });

    it('returns green for high, amber for moderate, and red for low impact', () => {
      expect(getImpactColor(0.8)).toBe('#137333');
      expect(getImpactColor(0.5)).toBe('#137333');
      expect(getImpactColor(0.35)).toBe('#b06000');
      expect(getImpactColor(0.1)).toBe('#c5221f');
      expect(getImpactColor(0.0)).toBe('#c5221f');
    });

    it('returns green for high opportunity and red for low opportunity', () => {
      expect(getOpportunityScoreColor(0.75)).toBe('#137333');
      expect(getOpportunityScoreColor(0.5)).toBe('#137333');
      expect(getOpportunityScoreColor(0.35)).toBe('#b06000');
      expect(getOpportunityScoreColor(0.2)).toBe('#b06000');
      expect(getOpportunityScoreColor(0.15)).toBe('#c5221f');
    });

    it('labels the traffic diagram and formats hover badge status text', () => {
      expect(CONGESTION_DIAGRAM_LABELS).toEqual({
        title: 'Airspace traffic',
        unitLabel: 'Total Flight km / day',
      });
      expect(formatBadgeFlightText(6149, 0.8)).toBe(
        '6,149 total flight kilometers (80% of maximum)',
      );
      expect(formatBadgeFlightText(2795, undefined)).toBe(
        '2,795 total flight kilometers',
      );
    });
  });

  describe('Percentile-Ranked Contrail Impact Sigmoid', () => {
    it('ranks values by the fraction of the sample strictly below them', () => {
      expect(computePercentileRanks([10, 20, 30, 40])).toEqual([
        0, 0.25, 0.5, 0.75,
      ]);
      expect(computePercentileRanks([5, 5, 5, 9])).toEqual([0, 0, 0, 0.75]);
      expect(computePercentileRanks([0, 0, 0, 0])).toEqual([0, 0, 0, 0]);
      expect(computePercentileRanks([])).toEqual([]);
      expect(computePercentileRanks([Number.NaN, 10])).toEqual([0, 0.5]);
    });

    it('returns zero impact score for non-positive percentile ranks', () => {
      expect(computeSigmoidImpact(0, 0.5)).toBe(0);
      expect(computeSigmoidImpact(0, 0.0)).toBe(0);
      expect(computeSigmoidImpact(-0.1, 0.5)).toBe(0);
    });

    it('translates the congestion sigmoid without reshaping at low and default midpoints', () => {
      const cBand = CONGESTION_SIGMOID_BAND;
      expect(computeSigmoid(0.25 - cBand, 0.25, cBand)).toBeCloseTo(0.03, 6);
      expect(computeSigmoid(0.25, 0.25, cBand)).toBeCloseTo(0.5, 6);
      expect(computeSigmoid(0.25 + cBand, 0.25, cBand)).toBeCloseTo(0.97, 6);
      expect(computeSigmoid(0.5 - cBand, 0.5, cBand)).toBeCloseTo(0.03, 6);
      expect(computeSigmoid(0.5 + cBand, 0.5, cBand)).toBeCloseTo(0.97, 6);
    });

    it('translates the congestion and impact sigmoids at high midpoints', () => {
      const cBand = CONGESTION_SIGMOID_BAND;
      const iBand = IMPACT_SIGMOID_BAND;
      const defMid = DEFAULT_IMPACT_PERCENTILE_MIDPOINT;
      expect(computeSigmoid(0.9 - cBand, 0.9, cBand)).toBeCloseTo(0.03, 6);
      expect(computeSigmoid(0.9 + cBand, 0.9, cBand)).toBeCloseTo(0.97, 6);
      expect(computeSigmoid(defMid - iBand, defMid, iBand)).toBeCloseTo(
        0.03,
        6,
      );
      expect(computeSigmoid(defMid, defMid, iBand)).toBeCloseTo(0.5, 6);
      expect(computeSigmoid(defMid + iBand, defMid, iBand)).toBeCloseTo(
        0.97,
        6,
      );
    });

    it('grades contrail impact over a wider span than congestion', () => {
      expect(IMPACT_SIGMOID_BAND).toBeGreaterThan(CONGESTION_SIGMOID_BAND);
      expect(deriveSigmoidSlope(IMPACT_SIGMOID_BAND)).toBeLessThan(
        deriveSigmoidSlope(CONGESTION_SIGMOID_BAND),
      );

      const graded = (band: number) =>
        Array.from({length: 288}, (_, i) =>
          computeSigmoid(i / 287, 0.5, band),
        ).filter((v) => v > 0.03 && v < 0.97).length;
      expect(graded(CONGESTION_SIGMOID_BAND) / 288).toBeCloseTo(
        2 * CONGESTION_SIGMOID_BAND,
        1,
      );
      expect(graded(IMPACT_SIGMOID_BAND) / 288).toBeCloseTo(
        2 * IMPACT_SIGMOID_BAND,
        1,
      );
      expect(computeSigmoidImpact(0.65, 0.5)).toBeLessThan(
        computeSigmoidCongestion(0.65, 0.5),
      );
    });

    it('clamps sigmoid midpoints to [0, 1] and falls back on NaN', () => {
      expect(MIN_SIGMOID_MIDPOINT).toBe(0);
      expect(MAX_SIGMOID_MIDPOINT).toBe(1);
      expect(clampSigmoidMidpoint(0.42)).toBe(0.42);
      expect(clampSigmoidMidpoint(-0.2)).toBe(0);
      expect(clampSigmoidMidpoint(Number.NaN)).toBe(
        DEFAULT_CONGESTION_MIDPOINT,
      );
    });

    it('evaluates congestion sigmoid at boundary midpoints 0 and 1', () => {
      expect(clampSigmoidMidpoint(1.7)).toBe(1);
      expect(computeSigmoid(1, 1, CONGESTION_SIGMOID_BAND)).toBeCloseTo(0.5, 6);
      expect(computeSigmoid(0, 1, CONGESTION_SIGMOID_BAND)).toBeLessThan(1e-5);
      expect(computeSigmoid(0, 0, CONGESTION_SIGMOID_BAND)).toBeCloseTo(0.5, 6);
      expect(computeSigmoid(1, 0, CONGESTION_SIGMOID_BAND)).toBeGreaterThan(
        1 - 1e-5,
      );
    });

    it('evaluates impact sigmoid at boundary midpoints 0 and 1', () => {
      expect(computeSigmoid(1, 1, IMPACT_SIGMOID_BAND)).toBeCloseTo(0.5, 6);
      expect(computeSigmoid(0, 1, IMPACT_SIGMOID_BAND)).toBeLessThan(1e-5);
      expect(computeSigmoid(0, 0, IMPACT_SIGMOID_BAND)).toBeCloseTo(0.5, 6);
      expect(computeSigmoid(1, 0, IMPACT_SIGMOID_BAND)).toBeGreaterThan(
        1 - 1e-5,
      );
    });

    it('interpolates RdYlGn hex colors and shifts track gradients with midpoint', () => {
      expect(interpolateRdYlGnHex(0)).toBe('rgb(165, 0, 38)');
      expect(interpolateRdYlGnHex(1)).toBe('rgb(0, 104, 55)');
      expect(interpolateRdYlGnHex(Number.NaN)).toBe('rgb(165, 0, 38)');

      const band = CONGESTION_SIGMOID_BAND;
      const lowMid = buildSigmoidTrackGradient(
        0.25,
        'higher-is-better',
        band,
        11,
      );
      const highMid = buildSigmoidTrackGradient(
        0.75,
        'higher-is-better',
        band,
        11,
      );
      expect(lowMid.startsWith('linear-gradient(to right, ')).toBeTrue();
      expect(lowMid).not.toBe(highMid);
    });

    it('reverses track gradient endpoints by direction and reflects band width', () => {
      const band = CONGESTION_SIGMOID_BAND;
      const higher = buildSigmoidTrackGradient(
        0.5,
        'higher-is-better',
        band,
        3,
      );
      const lower = buildSigmoidTrackGradient(0.5, 'lower-is-better', band, 3);
      expect(higher).toContain('rgb(165, 0, 38) 0.00%');
      expect(higher).toContain('rgb(0, 104, 55) 100.00%');
      expect(lower).toContain('rgb(0, 104, 55) 0.00%');
      expect(lower).toContain('rgb(165, 0, 38) 100.00%');

      const nearMid = (b: number) =>
        buildSigmoidTrackGradient(0.5, 'higher-is-better', b, 11).split(
          ', ',
        )[7];
      expect(nearMid(IMPACT_SIGMOID_BAND)).not.toBe(
        nearMid(CONGESTION_SIGMOID_BAND),
      );
    });

    it('keeps the three cell stroke treatments visually distinct', () => {
      expect(HOVER_STROKE_COLOR).not.toBe(THRESHOLD_STROKE_COLOR);
      expect(HOVER_STROKE_COLOR).not.toBe(CELL_STROKE_COLOR);
      expect(THRESHOLD_STROKE_COLOR).not.toBe(CELL_STROKE_COLOR);

      const px = (value: string) => Number(value.replace(/px$/i, ''));
      expect(px(HOVER_STROKE_WIDTH)).toBeGreaterThan(
        px(THRESHOLD_STROKE_WIDTH),
      );
      expect(px(THRESHOLD_STROKE_WIDTH)).toBeGreaterThan(px(CELL_STROKE_WIDTH));
    });
  });
});
