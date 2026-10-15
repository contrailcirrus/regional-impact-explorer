import 'jasmine';
import {
  clearCountryDataCache,
  setAllCountriesForTesting,
  setCountryDataInCache,
} from './CountryMetadata';
import {MONTH_SHORT_NAMES} from './metric_formatters';
import {
  CONGESTION_SIGMOID_BAND,
  DEFAULT_CONGESTION_MIDPOINT,
  DEFAULT_IMPACT_PERCENTILE_MIDPOINT,
  formatBadgeFlightText,
  IMPACT_SIGMOID_BAND,
  MAX_SIGMOID_MIDPOINT,
  MIN_SIGMOID_MIDPOINT,
} from './opportunity_scale';
import {
  buildHeatmapCells,
  buildTemporalApiUrl,
  buildTemporalCsvContent,
  buildTemporalCsvFilename,
  buildThresholdOutlinePath,
  cacheTemporalResponse,
  clearTemporalCacheForTesting,
  computeHasTemporalData,
  computeHighlightBoundaryEdges,
  computeOpportunityData,
  DEFAULT_HOURS,
  formatHourOfDay,
  formatSigmoidCalibration,
  formatTemporalNoDataText,
  getDaysInMonth,
  getHeatmapCellClassName,
  getOpportunityFootnoteText,
  getTemporalCacheEntry,
  HEATMAP_ZERO_IMPACT_FILL,
  normalizeTemporalData,
  resolveDisplayTitle,
  resolveHeatmapCellFill,
  resolveHourLabels,
  resolveTargetFir,
  resolveTemporalCsvFilenameOptions,
  resolveTemporalEntityLabel,
  resolveTemporalImpactMatrix,
  shouldHighlightCell,
} from './temporal_heatmap_model';

describe('temporal_heatmap_model Data Contracts & Matrix Logic', () => {
  afterEach(() => {
    clearTemporalCacheForTesting();
    clearCountryDataCache();
  });

  it('verifies 12 calendar month labels and ordering', () => {
    expect(MONTH_SHORT_NAMES).toEqual([
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
    ]);
  });

  it('verifies 24 hour grid time strings from 00:00 to 23:00', () => {
    expect(DEFAULT_HOURS.length).toBe(24);
    expect(DEFAULT_HOURS[0]).toBe('00:00');
    expect(DEFAULT_HOURS[12]).toBe('12:00');
    expect(DEFAULT_HOURS[23]).toBe('23:00');
  });

  describe('formatHourOfDay', () => {
    it('appends minutes to a bare backend hour label', () => {
      expect(formatHourOfDay('05')).toBe('05:00');
    });

    it('leaves a DEFAULT_HOURS fallback label unchanged', () => {
      expect(DEFAULT_HOURS.map(formatHourOfDay)).toEqual(DEFAULT_HOURS);
    });
  });

  describe('resolveHourLabels', () => {
    it('strips :00 from DEFAULT_HOURS when temporalData is null', () => {
      const labels = resolveHourLabels(null);
      expect(labels.length).toBe(24);
      expect(labels[0]).toBe('00');
      expect(labels[12]).toBe('12');
      expect(labels[23]).toBe('23');
    });

    it('strips :00 from a 24-element temporalData.hours array', () => {
      const customHours = Array.from(
        {length: 24},
        (_, h) => `${String(h).padStart(2, '0')}:00`,
      );
      const labels = resolveHourLabels({
        year: 2025,
        isoA2: 'FR',
        name: 'France',
        months: [],
        hours: customHours,
        co2eGwp50Tonnes: [],
        totalFlightDistanceKm: [],
        contrailFlightDistanceKm: [],
      });
      expect(labels.length).toBe(24);
      expect(labels[0]).toBe('00');
      expect(labels[23]).toBe('23');
    });

    it('falls back to DEFAULT_HOUR_LABELS when hours length is not 24', () => {
      const labels = resolveHourLabels({
        year: 2025,
        isoA2: 'FR',
        name: 'France',
        months: [],
        hours: ['00:00', '01:00'],
        co2eGwp50Tonnes: [],
        totalFlightDistanceKm: [],
        contrailFlightDistanceKm: [],
      });
      expect(labels.length).toBe(24);
      expect(labels[0]).toBe('00');
      expect(labels[23]).toBe('23');
    });

    it('trims padded hour strings and falls back to DEFAULT_HOUR_LABELS for empty entries', () => {
      const hoursWithBlanks = Array.from(
        {length: 24},
        (_, h) => `${String(h).padStart(2, '0')}:00`,
      );
      hoursWithBlanks[0] = ' 00:00 ';
      hoursWithBlanks[5] = '';
      hoursWithBlanks[6] = ':00';
      const labels = resolveHourLabels({
        year: 2025,
        isoA2: 'FR',
        name: 'France',
        months: [],
        hours: hoursWithBlanks,
        co2eGwp50Tonnes: [],
        totalFlightDistanceKm: [],
        contrailFlightDistanceKm: [],
      });
      expect(labels[0]).toBe('00');
      expect(labels[5]).toBe('05');
      expect(labels[6]).toBe('06');
    });
  });

  it('validates and normalizes backend payloads via normalizeTemporalData', () => {
    const payload = {
      year: 2025,
      'iso_a2': 'DE',
      name: 'Germany',
      months: [],
      hours: [],
      'co2e_gwp50_tonnes': [[1]],
      'total_flight_distance_km': [[2]],
      'contrail_flight_distance_km': [[1]],
    };
    expect(normalizeTemporalData(null)).toBeNull();
    expect(normalizeTemporalData({})).toBeNull();
    expect(normalizeTemporalData(payload)).toEqual({
      year: 2025,
      isoA2: 'DE',
      name: 'Germany',
      months: [],
      hours: [],
      co2eGwp50Tonnes: [[1]],
      totalFlightDistanceKm: [[2]],
      contrailFlightDistanceKm: [[1]],
    });
    // Both flight distance matrices are part of the only supported format.
    expect(
      normalizeTemporalData({
        ...payload,
        'total_flight_distance_km': undefined,
      }),
    ).toBeNull();
    expect(
      normalizeTemporalData({...payload, 'contrail_flight_distance_km': null}),
    ).toBeNull();
    // Rejects matrices with non-numeric elements or 1D rows.
    expect(
      normalizeTemporalData({...payload, 'co2e_gwp50_tonnes': [1, 2]}),
    ).toBeNull();
    expect(
      normalizeTemporalData({...payload, 'co2e_gwp50_tonnes': [['bad']]}),
    ).toBeNull();
    // Filters non-string elements from months/hours.
    expect(
      normalizeTemporalData({
        ...payload,
        months: ['Jan', 2, null, 'Feb'],
        hours: ['00', undefined, '01'],
      }),
    ).toEqual({
      year: 2025,
      isoA2: 'DE',
      name: 'Germany',
      months: ['Jan', 'Feb'],
      hours: ['00', '01'],
      co2eGwp50Tonnes: [[1]],
      totalFlightDistanceKm: [[2]],
      contrailFlightDistanceKm: [[1]],
    });
  });

  describe('explicit years', () => {
    const payload = {
      'iso_a2': 'DE',
      name: 'Germany',
      months: [],
      hours: [],
      'co2e_gwp50_tonnes': [[1]],
      'total_flight_distance_km': [[2]],
      'contrail_flight_distance_km': [[1]],
    };

    it('rejects temporal payloads with missing, zero, or out-of-range years', () => {
      expect(normalizeTemporalData(payload)).toBeNull();
      expect(normalizeTemporalData({...payload, year: 0})).toBeNull();
      expect(normalizeTemporalData({...payload, year: 25})).toBeNull();
      expect(normalizeTemporalData({...payload, year: 20255})).toBeNull();
    });

    it('rejects non-integer or string years and accepts four-digit integer years', () => {
      expect(normalizeTemporalData({...payload, year: 'abcd'})).toBeNull();
      expect(normalizeTemporalData({...payload, year: '2025'})).toBeNull();
      expect(normalizeTemporalData({...payload, year: 2025.5})).toBeNull();
      expect(normalizeTemporalData({...payload, year: null})).toBeNull();
      expect(normalizeTemporalData({...payload, year: 2037})).not.toBeNull();
    });

    it('throws in computeOpportunityData on a zero, non-integer, or out-of-range data year', () => {
      const matrix = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 10),
      );
      for (const badYear of [0, Number.NaN, 203, 20371]) {
        expect(() =>
          computeOpportunityData({
            year: badYear,
            co2eGwp50Tonnes: matrix,
            totalFlightDistanceKm: matrix,
          }),
        ).toThrowError(/computeOpportunityData: a year is required/);
      }
    });

    it('builds temporal URLs only with a valid year', () => {
      expect(buildTemporalApiUrl({year: 2037, country: 'FR'})).toBe(
        '/temporal/2037?iso=FR',
      );
      expect(() => buildTemporalApiUrl({year: 0, country: 'FR'})).toThrowError(
        /buildTemporalApiUrl: a year is required/,
      );
    });

    it('refuses to build a temporal URL without a country or FIR', () => {
      expect(() => buildTemporalApiUrl({year: 2037})).toThrowError(
        /buildTemporalApiUrl: a country or FIR is required/,
      );
      expect(() =>
        buildTemporalApiUrl({year: 2037, country: ' ', targetFir: null}),
      ).toThrowError(/a country or FIR is required/);
    });

    it('names CSV exports only with a valid year', () => {
      expect(buildTemporalCsvFilename({firIcao: 'EDGG', year: 2037})).toBe(
        'fir_EDGG_2037.csv',
      );
      expect(() =>
        buildTemporalCsvFilename({firIcao: 'EDGG', year: 0}),
      ).toThrowError(/buildTemporalCsvFilename: a year is required/);
    });

    it('uses the dataset year for day counts in CSV exports', () => {
      const matrix = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 29),
      );
      const build = (year: number) =>
        buildTemporalCsvContent({
          temporalData: {
            year,
            co2eGwp50Tonnes: matrix,
            totalFlightDistanceKm: matrix,
          },
        });
      // February has 29 days in a leap year, so the per-day rate differs.
      expect(build(2036)).not.toEqual(build(2037));
      expect(() => build(0)).toThrowError(
        /buildTemporalCsvContent: a year is required/,
      );
    });
  });

  describe('FIR Selection and Fallback Resolution', () => {
    it('resolves explicit selected FIR when matched in country controlled FIRs', () => {
      const mockControlledFirs = [
        {firId: 'LFBB_BORDEAUX', firName: 'BORDEAUX', firIcao: 'LFBB'},
        {firId: 'LFRR_BREST', firName: 'BREST', firIcao: 'LFRR'},
      ];
      const targetFir = resolveTargetFir('LFRR_BREST', mockControlledFirs);
      expect(targetFir).toEqual({
        firId: 'LFRR_BREST',
        firName: 'BREST',
        icao: 'LFRR',
      });
    });

    it('defaults to #1 FIR when selectedFirId is null', () => {
      const mockControlledFirs = [
        {firId: 'LFBB_BORDEAUX', firName: 'BORDEAUX', firIcao: 'LFBB'},
        {firId: 'LFRR_BREST', firName: 'BREST', firIcao: 'LFRR'},
      ];
      const targetFir = resolveTargetFir(null, mockControlledFirs);
      expect(targetFir).toEqual({
        firId: 'LFBB_BORDEAUX',
        firName: 'BORDEAUX',
        icao: 'LFBB',
      });
    });

    it('falls back to #1 FIR when selectedFirId does not belong to the selected country', () => {
      const mockControlledFirs = [
        {firId: 'EDGG_LANGEN', firName: 'LANGEN', firIcao: 'EDGG'},
        {firId: 'EDWW_BREMEN', firName: 'BREMEN', firIcao: 'EDWW'},
      ];
      const targetFir = resolveTargetFir('LFBB_BORDEAUX', mockControlledFirs);
      expect(targetFir).toEqual({
        firId: 'EDGG_LANGEN',
        firName: 'LANGEN',
        icao: 'EDGG',
      });
    });

    it('falls back to null target FIR when the country has no FIRs', () => {
      expect(resolveTargetFir(null, [])).toBeNull();
    });

    it('constructs correct API URL with fir_id query parameter without redundant iso', () => {
      const url = buildTemporalApiUrl({
        year: 2025,
        country: 'FR',
        targetFir: {firId: 'LFBB_BORDEAUX', icao: 'LFBB', firName: 'BORDEAUX'},
      });
      expect(url).toBe('/temporal/2025?fir_id=LFBB_BORDEAUX');
    });

    it('omits invalid non-ISO country parameter when targetFir is provided', () => {
      const url = buildTemporalApiUrl({
        year: 2025,
        country: 'Somaliland',
        targetFir: {
          firId: 'HCSM_MOGADISHU',
          icao: 'HCSM',
          firName: 'MOGADISHU',
        },
      });
      expect(url).toBe('/temporal/2025?fir_id=HCSM_MOGADISHU');
    });

    it('constructs correct country fallback API URL when no FIR is targeted', () => {
      const url = buildTemporalApiUrl({
        year: 2025,
        country: 'FR',
        targetFir: null,
      });
      expect(url).toBe('/temporal/2025?iso=FR');
    });

    it('resolves country display names to ISO alpha-2 codes in buildTemporalApiUrl when known', () => {
      setAllCountriesForTesting([{name: 'France', isoA2: 'FR'}]);
      const url = buildTemporalApiUrl({
        year: 2025,
        country: 'France',
        targetFir: null,
      });
      expect(url).toBe('/temporal/2025?iso=FR');
      setAllCountriesForTesting([]);

      // Also resolves via year-scoped PER_COUNTRY_DATA_MAP when ALL_COUNTRIES_SORTED is empty.
      setCountryDataInCache('France_2025', {name: 'France', 'iso_a2': 'FR'});
      const yearScopedUrl = buildTemporalApiUrl({
        year: 2025,
        country: 'France',
        targetFir: null,
      });
      expect(yearScopedUrl).toBe('/temporal/2025?iso=FR');
      clearCountryDataCache();
    });

    it('formats displayTitle and header according to isFir response flag', () => {
      const firTitle = resolveDisplayTitle({
        temporalDataName: 'BORDEAUX (LFBB)',
        targetFir: {firId: 'LFBB_BORDEAUX', icao: 'LFBB', firName: 'BORDEAUX'},
      });
      expect(firTitle).toBe('BORDEAUX (LFBB)');

      const fallbackTitle = resolveDisplayTitle({
        temporalDataName: 'France',
        country: 'France',
      });
      expect(fallbackTitle).toBe('France');
    });

    it('disambiguates sectors sharing an ICAO code strictly by firId', () => {
      const mockControlledFirs = [
        {
          firId: 'PAZA_ANCHORAGE_OCEANIC',
          firName: 'ANCHORAGE OCEANIC',
          firIcao: 'PAZA',
        },
        {
          firId: 'KZAK_OAKLAND_OCEANIC_EAST',
          firName: 'OAKLAND OCEANIC EAST',
          firIcao: 'KZAK',
        },
        {
          firId: 'KZAK_OAKLAND_OCEANIC_WEST',
          firName: 'OAKLAND OCEANIC WEST',
          firIcao: 'KZAK',
        },
        {
          firId: 'KZNY_NEW_YORK_OCEANIC',
          firName: 'NEW YORK OCEANIC',
          firIcao: 'KZNY',
        },
      ];

      const targetFir = resolveTargetFir(
        'KZAK_OAKLAND_OCEANIC_WEST',
        mockControlledFirs,
      );
      expect(targetFir).toEqual({
        firId: 'KZAK_OAKLAND_OCEANIC_WEST',
        icao: 'KZAK',
        firName: 'OAKLAND OCEANIC WEST',
      });

      const url = buildTemporalApiUrl({
        year: 2025,
        country: 'US',
        targetFir,
      });
      expect(url).toBe('/temporal/2025?fir_id=KZAK_OAKLAND_OCEANIC_WEST');
    });

    it('does not poison FIR cache entry when falling back to country temporal data', () => {
      clearTemporalCacheForTesting();
      const firUrl = '/temporal/2025?fir_id=LFBB_BORDEAUX';
      const fallbackCountryUrl = '/temporal/2025?iso=FR';
      const countryPayload = {
        year: 2025,
        isoA2: 'FR',
        name: 'France',
        isFir: false,
        months: ['202501'],
        hours: ['00:00'],
        co2eGwp50Tonnes: [[10]],
        totalFlightDistanceKm: [[100]],
        contrailFlightDistanceKm: [[20]],
      };

      cacheTemporalResponse(firUrl, fallbackCountryUrl, countryPayload);

      expect(getTemporalCacheEntry(firUrl)).toBeUndefined();
      expect(getTemporalCacheEntry(fallbackCountryUrl)).toEqual(countryPayload);

      clearTemporalCacheForTesting();
      // Even when the backend responds directly on the FIR URL with a non-FIR
      // fallback payload (`isFir: false`), the FIR cache key stays clean.
      cacheTemporalResponse(firUrl, null, countryPayload);
      expect(getTemporalCacheEntry(firUrl)).toBeUndefined();

      const firPayload = {
        ...countryPayload,
        name: 'FIR BORDEAUX (LFBB)',
        isFir: true,
        firId: 'LFBB_BORDEAUX',
        firIcao: 'LFBB',
        firName: 'BORDEAUX',
      };
      cacheTemporalResponse(firUrl, null, firPayload);
      expect(getTemporalCacheEntry(firUrl)).toEqual(firPayload);
      clearTemporalCacheForTesting();
    });

    it('does not misattribute country fallback payloads to targetFir in titles, subtitles, or CSV filenames', () => {
      const targetFir = {
        firId: 'LFBB_BORDEAUX',
        icao: 'LFBB',
        firName: 'BORDEAUX',
      };
      const countryFallbackData = {
        year: 2025,
        name: 'France',
        isFir: false,
      };

      expect(
        resolveDisplayTitle({
          temporalDataName: undefined,
          isFirData: false,
          targetFir,
          country: 'France',
        }),
      ).toBe('France');

      expect(
        resolveTemporalEntityLabel({
          temporalData: countryFallbackData,
          targetFir,
          selectedFirId: 'LFBB_BORDEAUX',
          country: 'France',
        }),
      ).toBe('France');

      const csvOpts = resolveTemporalCsvFilenameOptions({
        temporalData: countryFallbackData,
        targetFir,
        selectedFirId: 'LFBB_BORDEAUX',
        fallbackYear: 2025,
      });
      expect(csvOpts).toEqual({
        firIcao: null,
        firName: 'France',
        year: 2025,
      });
      expect(buildTemporalCsvFilename(csvOpts)).toBe('fir_france_2025.csv');

      const firData = {
        year: 2025,
        name: 'FIR BORDEAUX (LFBB)',
        isFir: true,
        firIcao: 'LFBB',
        firName: 'BORDEAUX',
      };
      expect(
        resolveTemporalEntityLabel({
          temporalData: firData,
          targetFir,
          selectedFirId: 'LFBB_BORDEAUX',
          country: 'France',
        }),
      ).toBe('FIR BORDEAUX (LFBB)');
      expect(
        buildTemporalCsvFilename(
          resolveTemporalCsvFilenameOptions({
            temporalData: firData,
            targetFir,
            selectedFirId: 'LFBB_BORDEAUX',
            fallbackYear: 2025,
          }),
        ),
      ).toBe('fir_LFBB_bordeaux_2025.csv');
    });
  });

  describe('Contrail Avoidance Opportunity Index Calculation', () => {
    it('calculates correct days in month including leap years via getDaysInMonth', () => {
      // Standard year 2025
      expect(getDaysInMonth(2025, 0)).toBe(31); // Jan
      expect(getDaysInMonth(2025, 1)).toBe(28); // Feb (non-leap)
      expect(getDaysInMonth(2025, 3)).toBe(30); // Apr
      expect(getDaysInMonth(2025, 6)).toBe(31); // Jul
      expect(getDaysInMonth(2025, 11)).toBe(31); // Dec

      // Leap year 2024
      expect(getDaysInMonth(2024, 1)).toBe(29); // Feb (leap)
    });

    it('computes sustained peak capacity proxy and opportunity score accurately', () => {
      const year = 2025;
      const hoursCount = 24;
      const monthsCount = 12;

      // Mock traffic: 100 flights off-peak (night), 1000 flights peak (daytime
      // 08:00-20:00)
      const segmentCount: number[][] = Array.from(
        {length: hoursCount},
        (_, h) =>
          Array.from({length: monthsCount}, () =>
            h >= 8 && h <= 20 ? 1000 : 100,
          ),
      );

      // Mock impact: baseline 200 tonnes, top 25% hours = 500 tonnes, off-peak
      // hour 5 = 10 tonnes
      const co2eImpact: number[][] = Array.from({length: hoursCount}, (_, h) =>
        Array.from({length: monthsCount}, () =>
          h === 5 ? 10 : h <= 3 || h >= 22 ? 500 : 200,
        ),
      );

      const temporalData = {
        year,
        totalFlightDistanceKm: segmentCount,
        co2eGwp50Tonnes: co2eImpact,
      };

      const opportunityResult = computeOpportunityData(temporalData);
      const oppScores = opportunityResult.matrix;

      // Feb (28 days) has the highest daily impact of the 500-tonne group, so
      // it sits in the top percentile and saturates the impact sigmoid.
      const offPeakTopImpactScore = oppScores[2][1];
      expect(offPeakTopImpactScore).toBeGreaterThan(0.9);

      // Jan (31 days) is the smallest member of that same group, landing on the
      // 0.75 percentile: well above the 0.50 default midpoint, so it saturates
      // too.
      const offPeakMidpointImpactScore = oppScores[2][0];
      expect(offPeakMidpointImpactScore).toBeGreaterThan(0.9);

      const peakHighImpactScore = oppScores[12][0];
      expect(peakHighImpactScore).toBeCloseTo(0.0, 1);

      const offPeakLowImpactScore = oppScores[5][0];
      expect(offPeakLowImpactScore).toBeLessThan(0.05);

      // All scores must be bounded in [0.0, 1.0]
      const allScores = oppScores.flat();
      expect(
        allScores.every((score) => score >= 0.0 && score <= 1.0),
      ).toBeTrue();
    });

    it('resolves GWP20 and GWP100 impact matrices when available', () => {
      const mockTemporal = {
        year: 2025,
        co2eGwp20Tonnes: [[20]],
        co2eGwp50Tonnes: [[50]],
        co2eGwp100Tonnes: [[100]],
        totalFlightDistanceKm: [[1]],
      };
      expect(resolveTemporalImpactMatrix(mockTemporal, 'GWP20')).toEqual([
        [20],
      ]);
      expect(resolveTemporalImpactMatrix(mockTemporal, 'GWP50')).toEqual([
        [50],
      ]);
      expect(resolveTemporalImpactMatrix(mockTemporal, 'GWP100')).toEqual([
        [100],
      ]);
    });

    it('generates consistent cross-matrix synchronized hover class names', () => {
      expect(getHeatmapCellClassName(14, 6)).toBe('heatmap-cell cell-h-14-m-6');
    });

    it('ranks impact from the CO2e matrix and ignores non-finite values', () => {
      const seg = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 100),
      );
      const co2e = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 200),
      );
      co2e[0][0] = 800;
      co2e[1][0] = Number.NaN;
      const opp = computeOpportunityData({
        year: 2025,
        totalFlightDistanceKm: seg,
        co2eGwp50Tonnes: co2e,
      });
      expect(opp.matrix[0][0]).toBeGreaterThan(0.5);
      expect(Number.isFinite(opp.matrix[1][0])).toBeTrue();
    });

    it('ignores a legacy prop_sum_ef_mj matrix and uses the selected GWP horizon', () => {
      const seg = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 100),
      );
      const co2e = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 200),
      );
      co2e[5][3] = 900;
      // The EF matrix peaks in a different cell; it must have no influence.
      const ef = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 1),
      );
      ef[0][0] = 1e9;
      const legacyPayload = {
        year: 2025,
        totalFlightDistanceKm: seg,
        co2eGwp100Tonnes: co2e,
        'prop_sum_ef_mj': ef,
      };
      const opp = computeOpportunityData(legacyPayload, 'GWP100');
      expect(opp.matrix[5][3]).toBeGreaterThan(opp.matrix[0][0]);
    });
  });

  describe('Airspace Traffic Matrices', () => {
    it('normalizes both impact and count modes by calendar days in month via buildHeatmapCells', () => {
      const daysIn2025 = Array.from({length: 12}, (_, m) =>
        getDaysInMonth(2025, m),
      );
      // Set raw monthly values equal to 10 * daysInMonth (310 in Jan, 280 in
      // Feb, 300 in Apr)
      const matrix = Array.from({length: 24}, () =>
        daysIn2025.map((d) => 10 * d),
      );
      const hourLabels = Array.from({length: 24}, (_, i) =>
        String(i).padStart(2, '0'),
      );
      const temporalData = {
        totalFlightDistanceKm: matrix,
        contrailFlightDistanceKm: matrix,
      };

      const impactRes = buildHeatmapCells({
        matrix,
        activeImpactMatrix: matrix,
        temporalData,
        daysInMonths: daysIn2025,
        hourLabels,
        monthLabels: MONTH_SHORT_NAMES,
        mode: 'impact',
      });
      // Both January (31 days) and February (28 days) must normalize to 10.0
      // tCO2e/day
      expect(impactRes.cells[0].val).toBeCloseTo(10, 6);
      expect(impactRes.cells[1].val).toBeCloseTo(10, 6);
      expect(impactRes.maxVal).toBeCloseTo(10, 6);

      const countRes = buildHeatmapCells({
        matrix,
        activeImpactMatrix: matrix,
        temporalData,
        daysInMonths: daysIn2025,
        hourLabels,
        monthLabels: MONTH_SHORT_NAMES,
        mode: 'count',
      });
      expect(countRes.cells[0].val).toBeCloseTo(10, 6);
      expect(countRes.cells[1].val).toBeCloseTo(10, 6);
    });

    it('derives congestion from totalFlightDistanceKm, not contrail distance', () => {
      const h = 14;
      const m = 5;

      // Peak total traffic at [14][5] (2500 vs 500), but off-peak contrails (50
      // vs 500)
      const totalDistance = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 500),
      );
      totalDistance[h][m] = 2500;
      const contrailDistance = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 500),
      );
      contrailDistance[h][m] = 50;

      // The full payload also carries contrail distance; it must not feed
      // congestion.
      const mockData = {
        year: 2025,
        totalFlightDistanceKm: totalDistance,
        contrailFlightDistanceKm: contrailDistance,
        co2eGwp50Tonnes: Array.from({length: 24}, () =>
          Array.from({length: 12}, () => 50),
        ),
      };
      const opp = computeOpportunityData(mockData);
      // The total-traffic peak defines the capacity proxy.
      expect(opp.rawCongestionMatrix[h][m]).toBeCloseTo(1.0, 6);
      expect(opp.congestionMatrix[h][m]).toBeGreaterThan(
        opp.congestionMatrix[0][0],
      );
    });

    it('caps single-cell peak rawCongestion at 100% of maximum', () => {
      // A single outlier peak cell (1 out of 288 < 5%) must define 100% of
      // maximum so neither rawCongestion nor the badge percentage ever exceeds
      // 100%.
      const singlePeakGrid = Array.from({length: 24}, () =>
        Array.from({length: 12}, (_, m) => 100 * getDaysInMonth(2025, m)),
      );
      singlePeakGrid[10][5] = 400 * getDaysInMonth(2025, 5);
      const opp = computeOpportunityData({
        year: 2025,
        totalFlightDistanceKm: singlePeakGrid,
        co2eGwp50Tonnes: singlePeakGrid,
      });
      expect(opp.rawCongestionMatrix[10][5]).toBeCloseTo(1.0, 6);
      expect(opp.rawCongestionMatrix[0][0]).toBeCloseTo(0.25, 6);
      expect(formatBadgeFlightText(400, opp.rawCongestionMatrix[10][5])).toBe(
        '400 total flight kilometers (100% of maximum)',
      );
      expect(formatBadgeFlightText(100, opp.rawCongestionMatrix[0][0])).toBe(
        '100 total flight kilometers (25% of maximum)',
      );
    });
  });

  describe('Percentile-Ranked Contrail Impact Sigmoid', () => {
    it('assigns zero impact and zero opportunity score to zero-contrail and net-cooling cells', () => {
      const co2eGrid = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 0),
      );
      // One net-cooling cell (-500 t) and one warming cell (+1000 t); all other
      // 286 cells have 0 contrail warming.
      co2eGrid[0][0] = -500;
      co2eGrid[12][6] = 1000;
      const segmentGrid = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 100),
      );
      const opp = computeOpportunityData({
        year: 2025,
        totalFlightDistanceKm: segmentGrid,
        co2eGwp50Tonnes: co2eGrid,
      });

      expect(opp.impactPercentileMatrix[0][0]).toBe(0);
      expect(opp.normImpactMatrix[0][0]).toBe(0);
      expect(opp.matrix[0][0]).toBe(0);

      // Zero-contrail cells must not be pushed above percentile 0 by the
      // net-cooling cell at [0][0].
      expect(opp.impactPercentileMatrix[1][1]).toBe(0);
      expect(opp.normImpactMatrix[1][1]).toBe(0);
      expect(opp.matrix[1][1]).toBe(0);

      // The warming cell at [12][6] sits above all 287 non-warming cells.
      expect(opp.impactPercentileMatrix[12][6]).toBeCloseTo(287 / 288, 6);
      expect(opp.normImpactMatrix[12][6]).toBeGreaterThan(0.9);
    });

    it('re-derives the opportunity index when a midpoint moves, with no refetch input', () => {
      const allImpacts = Array.from({length: 288}, (_, i) => i + 1);
      const co2eGrid = Array.from({length: 24}, (_, h) =>
        Array.from(
          {length: 12},
          (_, m) => allImpacts[h * 12 + m] * getDaysInMonth(2025, m),
        ),
      );
      const segmentGrid = Array.from({length: 24}, () =>
        Array.from({length: 12}, (_, m) => 20 * getDaysInMonth(2025, m)),
      );
      const temporalData = {
        year: 2025,
        totalFlightDistanceKm: segmentGrid,
        co2eGwp50Tonnes: co2eGrid,
      };

      // Median cell: percentile 0.5. That is the default midpoint, so it sits
      // exactly on the inflection; raising the midpoint to 0.75 puts it exactly
      // one IMPACT_SIGMOID_BAND below, i.e. on the low saturation shoulder.
      const atDefault = computeOpportunityData(temporalData, 'GWP50', {});
      expect(atDefault.normImpactMatrix[12][0]).toBeCloseTo(0.5, 2);

      const atThreeQuarters = computeOpportunityData(temporalData, 'GWP50', {
        impactMidpoint: 0.75,
      });
      expect(atThreeQuarters.normImpactMatrix[12][0]).toBeCloseTo(0.03, 2);

      // Raw percentiles are midpoint-invariant; only the sigmoid moves.
      expect(atThreeQuarters.impactPercentileMatrix[12][0]).toBe(
        atDefault.impactPercentileMatrix[12][0],
      );
    });

    it('translates the congestion response when the congestion midpoint moves', () => {
      const segmentGrid = Array.from({length: 24}, (_, h) =>
        Array.from(
          {length: 12},
          (_, m) => (h >= 8 && h <= 20 ? 100 : 60) * getDaysInMonth(2025, m),
        ),
      );
      const co2eGrid = Array.from({length: 24}, () =>
        Array.from({length: 12}, (_, m) => 100 * getDaysInMonth(2025, m)),
      );
      const temporalData = {
        year: 2025,
        totalFlightDistanceKm: segmentGrid,
        co2eGwp50Tonnes: co2eGrid,
      };

      // Off-peak cells sit at 60/100 = 0.6 of the capacity proxy.
      const atDefault = computeOpportunityData(temporalData, 'GWP50', {});
      expect(atDefault.rawCongestionMatrix[0][0]).toBeCloseTo(0.6, 6);
      expect(atDefault.congestionMatrix[0][0]).toBeLessThan(0.01);

      const atSixty = computeOpportunityData(temporalData, 'GWP50', {
        congestionMidpoint: 0.6,
      });
      expect(atSixty.rawCongestionMatrix[0][0]).toBeCloseTo(0.6, 6);
      expect(atSixty.congestionMatrix[0][0]).toBeCloseTo(0.5, 2);
    });

    it('outlines only the cells on the favourable side of a midpoint', () => {
      const belowRule = {
        source: 'rawCongestion',
        comparison: 'below',
        threshold: 0.8,
      } as const;
      const aboveRule = {
        source: 'impactPercentile',
        comparison: 'above',
        threshold: 0.75,
      } as const;
      const base = {
        hourIdx: 0,
        monthIdx: 0,
        hourStr: '00:00',
        monthStr: 'Jan',
        val: 0,
        dailyCo2e: 0,
        dailyFlightDistanceKm: 0,
      };

      expect(
        shouldHighlightCell({...base, rawCongestion: 0.5}, belowRule),
      ).toBeTrue();
      expect(
        shouldHighlightCell({...base, rawCongestion: 0.8}, belowRule),
      ).toBeFalse();
      expect(
        shouldHighlightCell({...base, rawCongestion: 0.9}, belowRule),
      ).toBeFalse();

      expect(
        shouldHighlightCell({...base, impactPercentile: 0.9}, aboveRule),
      ).toBeTrue();
      expect(
        shouldHighlightCell({...base, impactPercentile: 0.75}, aboveRule),
      ).toBeFalse();

      // Missing or absent rule values are never outlined.
      expect(shouldHighlightCell(base, belowRule)).toBeFalse();
      expect(
        shouldHighlightCell({...base, rawCongestion: Number.NaN}, belowRule),
      ).toBeFalse();
      expect(
        shouldHighlightCell({...base, rawCongestion: 0.1}, undefined),
      ).toBeFalse();
    });

    it('selects either all 288 cells or 0 cells when either slider is at minimum (0.0) or maximum (1.0)', () => {
      const daysInMonths = Array.from({length: 12}, (_, m) =>
        getDaysInMonth(2025, m),
      );
      // Include 0-impact / 0-traffic cells and a distinct maximum cell so
      // impactPercentile spans [0, 287/288] and rawCongestion spans [0, 1.0].
      const segmentGrid = Array.from({length: 24}, (_, h) =>
        Array.from({length: 12}, (_, m) => (h * 12 + m) * daysInMonths[m]),
      );
      const co2eGrid = Array.from({length: 24}, (_, h) =>
        Array.from({length: 12}, (_, m) => (h * 12 + m) * 10 * daysInMonths[m]),
      );
      const temporalData = {
        year: 2025,
        totalFlightDistanceKm: segmentGrid,
        contrailFlightDistanceKm: segmentGrid,
        co2eGwp50Tonnes: co2eGrid,
      };
      const opp = computeOpportunityData(temporalData, 'GWP50');
      const {cells} = buildHeatmapCells({
        matrix: co2eGrid,
        activeImpactMatrix: co2eGrid,
        temporalData,
        daysInMonths,
        hourLabels: DEFAULT_HOURS,
        monthLabels: MONTH_SHORT_NAMES,
        mode: 'impact',
        scoreMatrix: opp.matrix,
        congestionMatrix: opp.congestionMatrix,
        normImpactMatrix: opp.normImpactMatrix,
        rawCongestionMatrix: opp.rawCongestionMatrix,
        impactPercentileMatrix: opp.impactPercentileMatrix,
      });
      expect(cells.length).toBe(288);
      // Confirm the grid actually contains the boundary values 0 and 1.
      expect(cells.some((c) => c.impactPercentile === 0)).toBeTrue();
      expect(cells.some((c) => c.rawCongestion === 0)).toBeTrue();
      expect(cells.some((c) => c.rawCongestion === 1)).toBeTrue();

      // 1. Impact percentile slider at minimum (0.0): all 288 cells selected
      const impactMinRule = {
        source: 'impactPercentile',
        comparison: 'above',
        threshold: MIN_SIGMOID_MIDPOINT,
      } as const;
      expect(
        cells.filter((c) => shouldHighlightCell(c, impactMinRule)).length,
      ).toBe(288);
      // Full 24x12 rectangle perimeter has 2 * (24 + 12) = 72 boundary edges
      // and 0 interior holes.
      expect(computeHighlightBoundaryEdges(cells, impactMinRule).length).toBe(
        72,
      );

      // 2. Impact percentile slider at maximum (1.0): 0 cells selected
      const impactMaxRule = {
        source: 'impactPercentile',
        comparison: 'above',
        threshold: MAX_SIGMOID_MIDPOINT,
      } as const;
      expect(
        cells.filter((c) => shouldHighlightCell(c, impactMaxRule)).length,
      ).toBe(0);
      expect(computeHighlightBoundaryEdges(cells, impactMaxRule)).toEqual([]);

      // 3. Congestion slider at minimum (0.0): 0 cells selected
      const congestionMinRule = {
        source: 'rawCongestion',
        comparison: 'below',
        threshold: MIN_SIGMOID_MIDPOINT,
      } as const;
      expect(
        cells.filter((c) => shouldHighlightCell(c, congestionMinRule)).length,
      ).toBe(0);
      expect(computeHighlightBoundaryEdges(cells, congestionMinRule)).toEqual(
        [],
      );

      // 4. Congestion slider at maximum (1.0): all 288 cells selected
      //    (including the 100% peak cell)
      const congestionMaxRule = {
        source: 'rawCongestion',
        comparison: 'below',
        threshold: MAX_SIGMOID_MIDPOINT,
      } as const;
      expect(
        cells.filter((c) => shouldHighlightCell(c, congestionMaxRule)).length,
      ).toBe(288);
      expect(
        computeHighlightBoundaryEdges(cells, congestionMaxRule).length,
      ).toBe(72);
    });

    it('strokes all four sides of an isolated cell and drops shared borders', () => {
      const rule = {
        source: 'impactPercentile',
        comparison: 'above',
        threshold: 0.5,
      } as const;
      const makeCell = (
        hourIdx: number,
        monthIdx: number,
        selected: boolean,
      ) => ({
        hourIdx,
        monthIdx,
        hourStr: `${String(hourIdx).padStart(2, '0')}:00`,
        monthStr: MONTH_SHORT_NAMES[monthIdx],
        val: 0,
        dailyCo2e: 0,
        dailyFlightDistanceKm: 0,
        impactPercentile: selected ? 0.9 : 0.1,
      });
      const sidesFor = (
        edges: ReadonlyArray<{hourIdx: number; monthIdx: number; side: string}>,
        h: number,
        m: number,
      ) =>
        edges
          .filter((e) => e.hourIdx === h && e.monthIdx === m)
          .map((e) => e.side)
          .sort();

      // A lone selected cell keeps all four sides.
      const isolated = computeHighlightBoundaryEdges(
        [makeCell(0, 0, true), makeCell(0, 1, false), makeCell(1, 0, false)],
        rule,
      );
      expect(sidesFor(isolated, 0, 0)).toEqual([
        'bottom',
        'left',
        'right',
        'top',
      ]);

      // Two horizontally adjacent cells drop the border they share.
      const pair = computeHighlightBoundaryEdges(
        [makeCell(0, 0, true), makeCell(0, 1, true)],
        rule,
      );
      expect(pair.length).toBe(6);
      expect(sidesFor(pair, 0, 0)).toEqual(['bottom', 'left', 'top']);
      expect(sidesFor(pair, 0, 1)).toEqual(['bottom', 'right', 'top']);
    });

    it('strokes only the outer perimeter of a 3x3 block and ignores unselected cells', () => {
      const rule = {
        source: 'impactPercentile',
        comparison: 'above',
        threshold: 0.5,
      } as const;
      const makeCell = (
        hourIdx: number,
        monthIdx: number,
        selected: boolean,
      ) => ({
        hourIdx,
        monthIdx,
        hourStr: `${String(hourIdx).padStart(2, '0')}:00`,
        monthStr: MONTH_SHORT_NAMES[monthIdx],
        val: 0,
        dailyCo2e: 0,
        dailyFlightDistanceKm: 0,
        impactPercentile: selected ? 0.9 : 0.1,
      });
      const sidesFor = (
        edges: ReadonlyArray<{hourIdx: number; monthIdx: number; side: string}>,
        h: number,
        m: number,
      ) =>
        edges
          .filter((e) => e.hourIdx === h && e.monthIdx === m)
          .map((e) => e.side)
          .sort();

      // A fully enclosed cell contributes nothing; a solid 3x3 block is drawn
      // as its 12-segment perimeter rather than 36 per-cell sides.
      const block = Array.from({length: 3}, (_, h) =>
        Array.from({length: 3}, (_, m) => makeCell(h, m, true)),
      ).flat();
      const blockEdges = computeHighlightBoundaryEdges(block, rule);
      expect(sidesFor(blockEdges, 1, 1)).toEqual([]);
      expect(blockEdges.length).toBe(12);

      // Unselected cells and a missing rule never contribute edges.
      expect(computeHighlightBoundaryEdges(block, undefined)).toEqual([]);
      expect(
        computeHighlightBoundaryEdges(
          [makeCell(0, 0, false), makeCell(0, 1, false)],
          rule,
        ),
      ).toEqual([]);
    });

    it('derives the calibration and footnote copy from the live midpoints', () => {
      const band = CONGESTION_SIGMOID_BAND;
      expect(formatSigmoidCalibration(0.75, band)).toBe(
        '65% ≈ 0 · 75% = 0.5 · 85% ≈ 1',
      );
      expect(formatSigmoidCalibration(0.5, band)).toBe(
        '40% ≈ 0 · 50% = 0.5 · 60% ≈ 1',
      );
      // Within one band of an end, that shoulder is off the input domain and is
      // dropped rather than reported at a value it never reaches.
      expect(formatSigmoidCalibration(0.0, band)).toBe('0% = 0.5 · 10% ≈ 1');
      expect(formatSigmoidCalibration(1.0, band)).toBe('90% ≈ 0 · 100% = 0.5');
      expect(formatSigmoidCalibration(0.1, band)).toBe(
        '0% ≈ 0 · 10% = 0.5 · 20% ≈ 1',
      );
      // The impact axis reports its own, wider band.
      expect(formatSigmoidCalibration(0.5, IMPACT_SIGMOID_BAND)).toBe(
        '25% ≈ 0 · 50% = 0.5 · 75% ≈ 1',
      );

      const footnote = getOpportunityFootnoteText(
        DEFAULT_CONGESTION_MIDPOINT,
        DEFAULT_IMPACT_PERCENTILE_MIDPOINT,
      );
      expect(footnote).toContain('currently centred at 80%');
      expect(footnote).toContain('currently centred at 50%');
      expect(footnote).toContain('percentile rank');

      const moved = getOpportunityFootnoteText(0.6, 0.4);
      expect(moved).toContain('currently centred at 60%');
      expect(moved).toContain('currently centred at 40%');
    });

    it('computes opportunity scores from impact percentile rank, resisting peak outlier compression', () => {
      // 288 strictly increasing daily impacts with a 10x outlier spread: the
      // percentile ranking is invariant to that spread, so the top cell
      // saturates without flattening the middle of the distribution.
      const allImpacts = Array.from(
        {length: 288},
        (_, i) => (i + 1) * (200 / 216.25),
      );

      const co2eGrid = Array.from({length: 24}, (_, h) =>
        Array.from(
          {length: 12},
          (_, m) => allImpacts[h * 12 + m] * getDaysInMonth(2025, m),
        ),
      );

      // Off-peak night traffic = 20 flights/day; peak daytime traffic
      // (08:00-20:00) = 100 flights/day (establishing P95 capacity proxy = 100)
      const segmentGrid = Array.from({length: 24}, (_, h) =>
        Array.from(
          {length: 12},
          (_, m) => (h >= 8 && h <= 20 ? 100 : 20) * getDaysInMonth(2025, m),
        ),
      );

      const opportunityResult = computeOpportunityData({
        year: 2025,
        totalFlightDistanceKm: segmentGrid,
        co2eGwp50Tonnes: co2eGrid,
      });

      // Off-peak high impact cell receives high score (> 0.90)
      expect(opportunityResult.matrix[23][11]).toBeGreaterThan(0.9);
      // Peak traffic cell receives lower score
      expect(opportunityResult.matrix[12][0]).toBeLessThan(
        opportunityResult.matrix[23][11],
      );
    });

    it('normalizes buildHeatmapCells metrics by days in month including dailyFlightDistanceKm', () => {
      const daysInMonths = Array.from({length: 12}, (_, m) =>
        getDaysInMonth(2025, m),
      );
      const matrix = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 310),
      );
      const co2eMatrix = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 620),
      );
      const segmentMatrix = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 3100),
      );

      const result = buildHeatmapCells({
        matrix,
        mode: 'impact',
        activeImpactMatrix: co2eMatrix,
        temporalData: {
          totalFlightDistanceKm: segmentMatrix,
        },
        daysInMonths,
        hourLabels: DEFAULT_HOURS,
        monthLabels: MONTH_SHORT_NAMES,
      });

      // January (monthIdx = 0) has 31 days
      const janHour0 = result.cells.find(
        (c) => c.hourIdx === 0 && c.monthIdx === 0,
      );
      expect(janHour0).toBeDefined();
      expect(janHour0?.val).toBeCloseTo(10, 5);
      expect(janHour0?.dailyCo2e).toBeCloseTo(20, 5);
      expect(janHour0?.dailyFlightDistanceKm).toBeCloseTo(100, 5);
    });
  });

  describe('Temporal CSV Export', () => {
    it('formats CSV filename with FIR ICAO code, lowercase whitespace-stripped FIR name, and year', () => {
      expect(
        buildTemporalCsvFilename({
          firIcao: 'LSAS',
          firName: 'FIR SWITZERLAND',
          year: 2025,
        }),
      ).toBe('fir_LSAS_switzerland_2025.csv');

      expect(
        buildTemporalCsvFilename({
          firIcao: 'KZAK',
          firName: 'FIR OAKLAND OCEANIC WEST',
          year: 2025,
        }),
      ).toBe('fir_KZAK_oaklandoceanicwest_2025.csv');

      expect(
        buildTemporalCsvFilename({
          firIcao: 'LFBB',
          firName: 'Bordeaux FIR (LFBB)',
          year: 2024,
        }),
      ).toBe('fir_LFBB_bordeaux_2024.csv');

      expect(
        buildTemporalCsvFilename({
          firIcao: 'LFBB',
          firName: null,
          year: 2025,
        }),
      ).toBe('fir_LFBB_2025.csv');
    });

    it('generates 288 month-major CSV rows with daily impact, flight km, and opportunity index', () => {
      const co2eGwp20 = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 620),
      );
      const co2eGwp50 = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 310),
      );
      const co2eGwp100 = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 155),
      );
      const totalDistanceKm = Array.from({length: 24}, () =>
        Array.from({length: 12}, () => 3100),
      );
      const opportunityMatrix = Array.from({length: 24}, (_, h) =>
        Array.from({length: 12}, (_, m) => Number(((h + m) / 100).toFixed(2))),
      );

      const csvGwp50 = buildTemporalCsvContent({
        temporalData: {
          year: 2025,
          co2eGwp20Tonnes: co2eGwp20,
          co2eGwp50Tonnes: co2eGwp50,
          co2eGwp100Tonnes: co2eGwp100,
          totalFlightDistanceKm: totalDistanceKm,
        },
        gwp: 'GWP50',
        opportunityMatrix,
      });

      const lines = csvGwp50.trim().split('\n');
      // 1 header line + 12 months * 24 hours = 289 lines
      expect(lines.length).toBe(289);
      expect(lines[0]).toBe(
        'month,local hour,contrail t CO2e GWP50 / day,' +
          'flight kilometers / day,contrail opportunity index',
      );

      // First data row: Jan 2025 (31 days), 00:00 -> 310/31 = 10, 3100/31 =
      // 100, opp = 0
      expect(lines[1]).toBe('202501,00:00,10,100,0');
      // 24th data row: Jan 2025 (31 days), 23:00 -> opp = 0.23
      expect(lines[24]).toBe('202501,23:00,10,100,0.23');
      // 25th data row: Feb 2025 (28 days), 00:00 -> 310/28, 3100/28, opp = 0.01
      expect(lines[25]).toBe(`202502,00:00,${310 / 28},${3100 / 28},0.01`);
      // Last data row: Dec 2025 (31 days), 23:00 -> opp = (23+11)/100 = 0.34
      expect(lines[288]).toBe('202512,23:00,10,100,0.34');

      // Verify the GWP20 header and impact column
      const csvGwp20 = buildTemporalCsvContent({
        temporalData: {
          year: 2025,
          co2eGwp20Tonnes: co2eGwp20,
          co2eGwp50Tonnes: co2eGwp50,
          totalFlightDistanceKm: totalDistanceKm,
        },
        gwp: 'GWP20',
        opportunityMatrix,
      });
      const gwp20Lines = csvGwp20.trim().split('\n');
      expect(gwp20Lines[0]).toBe(
        'month,local hour,contrail t CO2e GWP20 / day,' +
          'flight kilometers / day,contrail opportunity index',
      );
      // Jan 2025 (31 days), 00:00 -> 620/31 = 20, 3100/31 = 100, opp = 0
      expect(gwp20Lines[1]).toBe('202501,00:00,20,100,0');
    });
  });

  describe('SVG Outline Path & State Helpers', () => {
    it('builds SVG threshold outline paths from cell boundary edges', () => {
      const edges = [
        {
          hourIdx: 0,
          monthIdx: 0,
          hourStr: '00',
          monthStr: 'Jan',
          side: 'top' as const,
        },
        {
          hourIdx: 0,
          monthIdx: 0,
          hourStr: '00',
          monthStr: 'Jan',
          side: 'bottom' as const,
        },
        {
          hourIdx: 0,
          monthIdx: 0,
          hourStr: '00',
          monthStr: 'Jan',
          side: 'left' as const,
        },
        {
          hourIdx: 0,
          monthIdx: 0,
          hourStr: '00',
          monthStr: 'Jan',
          side: 'right' as const,
        },
      ];
      const path = buildThresholdOutlinePath(
        edges,
        () => 10,
        () => 20,
        30,
        12,
      );
      expect(path).toBe('M10,20H40M10,32H40M10,20V32M40,20V32');
    });

    it('formats the temporal no-data message for the requested year', () => {
      const text = formatTemporalNoDataText(2025);
      expect(text).toBe(
        'Temporal density exports for 2025 are not yet available.',
      );
    });

    it('reuses precomputed rawCongestionMatrix and impactPercentileMatrix references in computeOpportunityData', () => {
      const segmentGrid = Array.from({length: 24}, (_, h) =>
        Array.from(
          {length: 12},
          (_, m) => (h >= 8 && h <= 20 ? 100 : 40) * getDaysInMonth(2025, m),
        ),
      );
      const co2eGrid = Array.from({length: 24}, (_, h) =>
        Array.from(
          {length: 12},
          (_, m) => (h * 12 + m + 1) * getDaysInMonth(2025, m),
        ),
      );
      const temporalData = {
        year: 2025,
        totalFlightDistanceKm: segmentGrid,
        co2eGwp50Tonnes: co2eGrid,
      };

      const baseline = computeOpportunityData(temporalData, 'GWP50');
      const fromScratch = computeOpportunityData(temporalData, 'GWP50', {
        congestionMidpoint: 0.65,
        impactMidpoint: 0.35,
      });
      const withPrecomputed = computeOpportunityData(temporalData, 'GWP50', {
        congestionMidpoint: 0.65,
        impactMidpoint: 0.35,
        rawCongestionMatrix: baseline.rawCongestionMatrix,
        impactPercentileMatrix: baseline.impactPercentileMatrix,
      });

      expect(withPrecomputed.rawCongestionMatrix).toBe(
        baseline.rawCongestionMatrix,
      );
      expect(withPrecomputed.impactPercentileMatrix).toBe(
        baseline.impactPercentileMatrix,
      );
      expect(withPrecomputed.matrix).toEqual(fromScratch.matrix);
      expect(withPrecomputed.congestionMatrix).toEqual(
        fromScratch.congestionMatrix,
      );
      expect(withPrecomputed.normImpactMatrix).toEqual(
        fromScratch.normImpactMatrix,
      );
    });

    it('detects non-empty temporal datasets via computeHasTemporalData', () => {
      const baseResponse = {
        year: 2024,
        isoA2: 'CH',
        name: 'Switzerland',
        months: ['2024-01', '2024-02'],
        hours: ['00:00'],
        contrailFlightDistanceKm: [[0, 0]],
      };
      expect(computeHasTemporalData(null, 'GWP50')).toBeFalse();
      expect(
        computeHasTemporalData(
          {
            ...baseResponse,
            co2eGwp50Tonnes: [[0, 0]],
            totalFlightDistanceKm: [[0, 0]],
          },
          'GWP50',
        ),
      ).toBeFalse();
      expect(
        computeHasTemporalData(
          {
            ...baseResponse,
            co2eGwp50Tonnes: [[0, 12.5]],
            totalFlightDistanceKm: [[0, 0]],
          },
          'GWP50',
        ),
      ).toBeTrue();
      expect(
        computeHasTemporalData(
          {
            ...baseResponse,
            co2eGwp50Tonnes: [[-20, 12.5]],
            totalFlightDistanceKm: [[0, 0]],
          },
          'GWP50',
        ),
      ).toBeTrue();
      expect(
        computeHasTemporalData(
          {
            ...baseResponse,
            co2eGwp50Tonnes: [[-20, 0]],
            totalFlightDistanceKm: [[0, 0]],
          },
          'GWP50',
        ),
      ).toBeTrue();
    });

    it('renders zero and negative impact/count cells with HEATMAP_ZERO_IMPACT_FILL via resolveHeatmapCellFill', () => {
      const colorScale = (v: number) => `scaled(${v})`;
      const baseCell = {
        hourIdx: 0,
        monthIdx: 0,
        monthStr: 'Jan',
        hourStr: '00:00',
        val: 0,
        dailyCo2e: 0,
        dailyFlightDistanceKm: 0,
        score: 0,
      };
      expect(resolveHeatmapCellFill(baseCell, 'impact', colorScale)).toBe(
        HEATMAP_ZERO_IMPACT_FILL,
      );
      expect(
        resolveHeatmapCellFill(
          {...baseCell, val: -12.5, dailyCo2e: -12.5},
          'impact',
          colorScale,
        ),
      ).toBe(HEATMAP_ZERO_IMPACT_FILL);
      expect(
        resolveHeatmapCellFill(
          {...baseCell, val: 25, dailyCo2e: 25},
          'impact',
          colorScale,
        ),
      ).toBe('scaled(25)');
      expect(resolveHeatmapCellFill(baseCell, 'count', colorScale)).toBe(
        HEATMAP_ZERO_IMPACT_FILL,
      );
      expect(
        resolveHeatmapCellFill(
          {...baseCell, val: 150, dailyFlightDistanceKm: 150},
          'count',
          colorScale,
        ),
      ).toBe('scaled(150)');
      expect(
        resolveHeatmapCellFill(
          {...baseCell, score: 0.75},
          'opportunity',
          colorScale,
        ),
      ).toBe('scaled(0.75)');
      expect(
        resolveHeatmapCellFill(
          {...baseCell, score: undefined},
          'opportunity',
          colorScale,
        ),
      ).toBe('scaled(0)');
    });
  });
});
