import 'jasmine';
import {
  computeValueYoY,
  countRankedEntities,
  extractIcaoFromFirId,
  extractRankInfo,
  formatFirDisplayName,
  formatMetricValue,
  formatMwPerSqM,
  formatPercentage,
  formatRankLabel,
  formatRatioPercentOrNotAvailable,
  formatSecondaryValue,
  formatTonnesCo2e,
  formatTonnesOrNotAvailable,
  getNumericRecordField,
  getRecordField,
  NOT_AVAILABLE_LABEL,
  parseNonNegativeInteger,
  RankedEntityRecord,
  UNIT_MEGATONNES_CO2E,
  UNIT_MILLIWATTS_PER_SQ_M,
  UNIT_RATIO_OF_NATIONAL_GHG,
  UNIT_RATIO_OF_WORLD_AVG_GHG_PER_CAPITA,
  UNIT_SQUARE_KILOMETRES,
  UNIT_TONNES_CO2E,
  UNIT_TONNES_CO2E_PER_CAPITA,
  unitDisplayLabel,
} from './metric_formatters';

describe('metric_formatters', () => {
  describe('formatTonnesCo2e and formatTonnesOrNotAvailable', () => {
    it('formats gigatonnes (>= 1,000,000,000)', () => {
      expect(formatTonnesCo2e(1_500_000_000)).toBe('1.5 Gt CO₂e');
      expect(formatTonnesCo2e(2_000_000_000)).toBe('2.0 Gt CO₂e');
      expect(formatTonnesCo2e(-1_200_000_000)).toBe('-1.2 Gt CO₂e');
    });

    it('formats megatonnes (>= 1,000,000 and < 1,000,000,000)', () => {
      expect(formatTonnesCo2e(12_345_678)).toBe('12.3 Mt CO₂e');
      expect(formatTonnesCo2e(1_000_000)).toBe('1.0 Mt CO₂e');
      expect(formatTonnesCo2e(-5_400_000)).toBe('-5.4 Mt CO₂e');
    });

    it('formats kilotonnes (>= 1,000 and < 1,000,000)', () => {
      expect(formatTonnesCo2e(45_678)).toBe('45.7 Kt CO₂e');
      expect(formatTonnesCo2e(1_000)).toBe('1.0 Kt CO₂e');
      expect(formatTonnesCo2e(-250_000)).toBe('-250.0 Kt CO₂e');
    });

    it('formats sub-tonne values (< 1) with 2 decimals', () => {
      expect(formatTonnesCo2e(0.456)).toBe('0.46 t CO₂e');
      expect(formatTonnesCo2e(0.01)).toBe('0.01 t CO₂e');
      expect(formatTonnesCo2e(0)).toBe('0.00 t CO₂e');
      expect(formatTonnesCo2e(-0.75)).toBe('-0.75 t CO₂e');
    });

    it('formats standard tonnes (1 to 999) with 1 decimal', () => {
      expect(formatTonnesCo2e(1.23)).toBe('1.2 t CO₂e');
      expect(formatTonnesCo2e(999.9)).toBe('999.9 t CO₂e');
      expect(formatTonnesCo2e(-12.5)).toBe('-12.5 t CO₂e');
    });

    it('supports custom unit suffixes and guards non-finite inputs', () => {
      expect(formatTonnesCo2e(1_000_000, 'CO₂e (GWP50)')).toBe(
        '1.0 Mt CO₂e (GWP50)',
      );
      expect(formatTonnesCo2e(500, '')).toBe('500.0 t');
      expect(formatTonnesCo2e(null)).toBe('0.0 t CO₂e');
      expect(formatTonnesCo2e(undefined)).toBe('0.0 t CO₂e');
      expect(formatTonnesCo2e(Number.NaN)).toBe('0.0 t CO₂e');
      expect(formatTonnesCo2e(Number.POSITIVE_INFINITY)).toBe('0.0 t CO₂e');
      expect(formatTonnesOrNotAvailable(undefined)).toBe(NOT_AVAILABLE_LABEL);
      expect(formatTonnesOrNotAvailable(1000)).toBe('1.0 Kt CO₂e');
    });
  });

  describe('formatPercentage and formatRatioPercentOrNotAvailable', () => {
    it('formats direct percentages', () => {
      expect(formatPercentage(15.678, false, 1)).toBe('15.7%');
      expect(formatPercentage(0.5, false, 2)).toBe('0.50%');
      expect(formatPercentage(100, false, 0)).toBe('100%');
    });

    it('formats fractional ratios as percentages', () => {
      expect(formatPercentage(0.1234, true, 1)).toBe('12.3%');
      expect(formatPercentage(0.0056, true, 2)).toBe('0.56%');
      expect(formatPercentage(1.0, true, 0)).toBe('100%');
    });

    it('defaults to non-fractional input and formats non-finite values as zero or N/A', () => {
      expect(formatPercentage(45.678, false, 2)).toBe('45.68%');
      expect(formatPercentage(45.678)).toBe('45.7%');
      expect(formatPercentage(null, false, 0)).toBe('0%');
      expect(formatPercentage(NaN, false, 1)).toBe('0.0%');
      expect(formatRatioPercentOrNotAvailable(undefined)).toBe(
        NOT_AVAILABLE_LABEL,
      );
      expect(formatRatioPercentOrNotAvailable(0.05)).toBe('5.00%');
    });
  });

  describe('formatMwPerSqM', () => {
    it('renders missing or non-finite values as N/A, not as a zero', () => {
      expect(formatMwPerSqM(null)).toBe('N/A');
      expect(formatMwPerSqM(undefined)).toBe('N/A');
      expect(formatMwPerSqM(NaN)).toBe('N/A');
      expect(formatMwPerSqM(Number.NEGATIVE_INFINITY)).toBe('N/A');
      expect(formatMetricValue(null, UNIT_MILLIWATTS_PER_SQ_M)).toBe('N/A');
    });

    it('formats 0 properly', () => {
      expect(formatMwPerSqM(0)).toBe('0.0 mW/m²');
    });

    it('formats values >= 1000 with locale formatting', () => {
      const result = formatMwPerSqM(1234.56);
      expect(result).toContain('1,234.6 mW/m²');
    });

    it('formats values >= 10 and < 1000 with 1 decimal', () => {
      expect(formatMwPerSqM(45.67)).toBe('45.7 mW/m²');
      expect(formatMwPerSqM(10.0)).toBe('10.0 mW/m²');
    });

    it('formats values >= 1 and < 10 with 2 decimals', () => {
      expect(formatMwPerSqM(3.456)).toBe('3.46 mW/m²');
      expect(formatMwPerSqM(1.0)).toBe('1.00 mW/m²');
    });

    it('formats values > 0 and < 1 with 3 decimals', () => {
      expect(formatMwPerSqM(0.1234)).toBe('0.123 mW/m²');
      expect(formatMwPerSqM(0.005)).toBe('0.005 mW/m²');
    });
  });

  describe('formatMetricValue, formatSecondaryValue, and unitDisplayLabel', () => {
    it('formats metric values, secondary values, and unit labels across all UNIT_* identifiers', () => {
      expect(formatMetricValue(12.5, UNIT_MILLIWATTS_PER_SQ_M)).toBe(
        '12.5 mW/m²',
      );
      expect(formatMetricValue(0.0425, UNIT_RATIO_OF_NATIONAL_GHG)).toBe(
        '4.25%',
      );
      expect(formatMetricValue(1000, UNIT_TONNES_CO2E)).toBe('1.0 Kt CO₂e');
      expect(formatMetricValue(1.23, UNIT_TONNES_CO2E_PER_CAPITA)).toBe(
        '1.2 t CO₂e',
      );
      expect(formatMetricValue(41285, UNIT_SQUARE_KILOMETRES)).toBe(
        '41,285 km²',
      );
      expect(formatMetricValue(1.5, UNIT_MEGATONNES_CO2E)).toBe('1.5 Mt CO₂e');
      expect(formatMetricValue(Number.NaN, UNIT_TONNES_CO2E)).toBe(
        '0.0 t CO₂e',
      );

      expect(
        formatSecondaryValue(1.25, UNIT_RATIO_OF_WORLD_AVG_GHG_PER_CAPITA),
      ).toBe('125.00% of global GHG/capita');
      expect(formatSecondaryValue(45.2, UNIT_MEGATONNES_CO2E)).toBe(
        '45.2 Mt national GHG',
      );
      expect(formatSecondaryValue(85000, UNIT_SQUARE_KILOMETRES)).toBe(
        'Area: 85,000 km²',
      );
      expect(
        formatSecondaryValue(null, UNIT_SQUARE_KILOMETRES),
      ).toBeUndefined();
      expect(
        formatSecondaryValue(Number.POSITIVE_INFINITY, UNIT_MEGATONNES_CO2E),
      ).toBeUndefined();

      expect(unitDisplayLabel(UNIT_MILLIWATTS_PER_SQ_M)).toBe('mW/m²');
      expect(unitDisplayLabel(UNIT_RATIO_OF_NATIONAL_GHG)).toBe('%');
      expect(unitDisplayLabel(UNIT_TONNES_CO2E)).toBe('t CO₂e');
      expect(unitDisplayLabel(UNIT_TONNES_CO2E_PER_CAPITA)).toBe(
        't CO₂e / person',
      );
      expect(unitDisplayLabel(UNIT_MEGATONNES_CO2E)).toBe('Mt CO₂e');
      // Energy forcing (MJ) is no longer a published unit, so it has no label.
      expect(unitDisplayLabel('megajoules')).toBe('');
      expect(unitDisplayLabel(UNIT_SQUARE_KILOMETRES)).toBe('km²');
    });
  });

  describe('computeValueYoY', () => {
    it('handles positive, negative, zero-change, and absent or non-finite values', () => {
      expect(computeValueYoY(120, 100)).toEqual({
        text: '▲ +20.0% YoY',
        direction: 'up',
        color: '#d93025',
      });
      expect(computeValueYoY(80, 100)).toEqual({
        text: '▼ -20.0% YoY',
        direction: 'down',
        color: '#188038',
      });
      expect(computeValueYoY(-50, -100)).toEqual({
        text: '▲ +50.0% YoY',
        direction: 'up',
        color: '#d93025',
      });
      expect(computeValueYoY(-150, -100)).toEqual({
        text: '▼ -50.0% YoY',
        direction: 'down',
        color: '#188038',
      });
      expect(computeValueYoY(100, 100)?.direction).toBe('neutral');
      expect(computeValueYoY(100, 0)).toBeUndefined();
      expect(computeValueYoY(100, undefined)).toBeUndefined();
    });
  });

  describe('extractRankInfo', () => {
    it('parses valid rank strings', () => {
      expect(extractRankInfo('#1 / 177 countries')).toEqual({
        rankNum: 1,
        totalCount: 177,
      });
      expect(extractRankInfo('#45 / 177')).toEqual({
        rankNum: 45,
        totalCount: 177,
      });
      expect(extractRankInfo('10/50')).toEqual({rankNum: 10, totalCount: 50});
    });

    it('returns null for invalid or loading strings', () => {
      expect(extractRankInfo('Loading...')).toBeNull();
      expect(extractRankInfo('')).toBeNull();
      expect(extractRankInfo('No rank available')).toBeNull();
    });

    it('returns null when a rank exceeds the safe integer range', () => {
      expect(extractRankInfo('#99999999999999999999 / 177')).toBeNull();
    });
  });

  describe('parseNonNegativeInteger', () => {
    it('parses strings made only of decimal digits', () => {
      expect(parseNonNegativeInteger('0')).toBe(0);
      expect(parseNonNegativeInteger('07')).toBe(7);
      expect(parseNonNegativeInteger('2025')).toBe(2025);
    });

    it('rejects inputs that parseInt or Number would silently coerce', () => {
      expect(parseNonNegativeInteger('')).toBeNull();
      expect(parseNonNegativeInteger(' ')).toBeNull();
      expect(parseNonNegativeInteger('12px')).toBeNull();
      expect(parseNonNegativeInteger('1e3')).toBeNull();
      expect(parseNonNegativeInteger('-1')).toBeNull();
      expect(parseNonNegativeInteger('0x1f')).toBeNull();
      expect(parseNonNegativeInteger('1.5')).toBeNull();
    });
  });

  describe('countRankedEntities', () => {
    it('is the row count when no rank exceeds it', () => {
      expect(countRankedEntities([])).toBe(0);
      expect(
        countRankedEntities([
          {'mean_rf_mw_per_sq_m_rank': 1},
          {'co2e_gwp50_tonnes_rank': 2},
          {},
        ]),
      ).toBe(3);
    });

    it('is the highest rank across density and every GWP horizon', () => {
      expect(countRankedEntities([{'mean_rf_mw_per_sq_m_rank': 40}])).toBe(
        40,
      );
      expect(countRankedEntities([{'co2e_gwp20_tonnes_rank': 41}])).toBe(41);
      expect(countRankedEntities([{'co2e_gwp50_tonnes_rank': 42}])).toBe(42);
      expect(
        countRankedEntities([
          {'co2e_gwp50_tonnes_rank': 10},
          {'co2e_gwp100_tonnes_rank': 43, 'mean_rf_mw_per_sq_m_rank': 7},
        ]),
      ).toBe(43);
    });

    it('ignores non-finite, non-numeric and unrelated rank values', () => {
      expect(
        countRankedEntities([
          {'mean_rf_mw_per_sq_m_rank': Number.NaN},
          {'mean_rf_mw_per_sq_m_rank': Number.POSITIVE_INFINITY},
          {'co2e_gwp50_tonnes_rank': '900'} as unknown as RankedEntityRecord,
          {'co2e_gwp50_tonnes_rank': true} as unknown as RankedEntityRecord,
          {'co2e_gwp50_tonnes_rank': null},
          // Other rank columns (e.g. per-capita) do not bound the roster.
          {
            'co2e_gwp50_tonnes_per_capita_rank': 9999,
          } as unknown as RankedEntityRecord,
        ]),
      ).toBe(6);
    });

    it('floors fractional ranks', () => {
      expect(countRankedEntities([{'mean_rf_mw_per_sq_m_rank': 12.7}])).toBe(
        12,
      );
    });
  });

  describe('formatRankLabel', () => {
    it('omits the total when it is unknown', () => {
      expect(formatRankLabel(5, null, 'countries')).toBe('#5');
    });

    it('includes a known total, including the 0 loading sentinel', () => {
      expect(formatRankLabel(5, 180, 'countries')).toBe('#5 / 180 countries');
      expect(formatRankLabel(3, 0, 'global FIRs')).toBe('#3 / 0 global FIRs');
    });
  });

  describe('extractIcaoFromFirId and formatFirDisplayName', () => {
    it('extracts 4-letter ICAO prefix from composite FIR identifiers', () => {
      expect(extractIcaoFromFirId('KZNY_NEW_YORK_OCEANIC')).toBe('KZNY');
      expect(extractIcaoFromFirId('LSAS')).toBe('LSAS');
      expect(extractIcaoFromFirId('')).toBe('');
    });

    it('returns empty string for undefined or null inputs', () => {
      expect(formatFirDisplayName(undefined, undefined)).toBe('');
      expect(formatFirDisplayName('', '')).toBe('');
    });

    it('formats firName without leading FIR prefix by prepending FIR and appending ICAO', () => {
      expect(formatFirDisplayName('SWITZERLAND', 'LSAS')).toBe(
        'FIR SWITZERLAND (LSAS)',
      );
      expect(formatFirDisplayName('SHANWICK OCEANIC', 'EGGX')).toBe(
        'FIR SHANWICK OCEANIC (EGGX)',
      );
      expect(formatFirDisplayName('SHANNON', 'EISN')).toBe(
        'FIR SHANNON (EISN)',
      );
    });

    it('avoids duplicate FIR prefix when firName already starts with FIR', () => {
      expect(formatFirDisplayName('FIR SWITZERLAND', 'LSAS')).toBe(
        'FIR SWITZERLAND (LSAS)',
      );
      expect(formatFirDisplayName('FIR: LSAS', 'LSAS')).toBe(
        'FIR: LSAS (LSAS)',
      );
    });

    it('falls back to icao when firName is missing', () => {
      expect(formatFirDisplayName(undefined, 'LSAS')).toBe('LSAS');
      expect(formatFirDisplayName('', 'EDGG')).toBe('EDGG');
    });

    it('handles firName without icao correctly', () => {
      expect(formatFirDisplayName('SWITZERLAND', undefined)).toBe(
        'FIR SWITZERLAND',
      );
    });

    it('trims surrounding whitespace on firName and icao', () => {
      expect(formatFirDisplayName('  SHANWICK OCEANIC  ', ' EGGX ')).toBe(
        'FIR SHANWICK OCEANIC (EGGX)',
      );
      expect(formatFirDisplayName('  LONDON  ', ' EGTT ')).toBe(
        'FIR LONDON (EGTT)',
      );
    });
  });

  describe('getRecordField and getNumericRecordField', () => {
    it('extracts typed and numeric fields from untrusted record objects', () => {
      const rec: Readonly<Record<string, unknown>> = {
        'a': 42,
        'b': 'hello',
        'c': Number.NaN,
      };
      expect(getRecordField(rec, 'b')).toBe('hello');
      expect(getNumericRecordField(rec, 'a')).toBe(42);
      expect(getNumericRecordField(rec, 'b')).toBeUndefined();
      expect(getNumericRecordField(rec, 'c')).toBeUndefined();
    });
  });
});
