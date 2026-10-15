import 'jasmine';
import {
  buildHeroScopeMetrics,
  clearCountryDataCache,
  clearTotalCountryCounts,
  CountryOption,
  fetchAvailableYearsFromBackend,
  fetchCountriesFromBackend,
  fetchCountryRecordFromBackend,
  fetchCountryTotalFromBackend,
  fetchFirsFromBackend,
  fetchRankingsFromBackend,
  fetchTimeSeriesFromBackend,
  findCountryFeaturesForSelection,
  GeoFeatureLike,
  getAllCountries,
  getAvailableYears,
  getCachedYearsForTesting,
  getCountryDataFromCache,
  getCountryListStatus,
  getCountryMetrics,
  getCountryNameFromIso2,
  getCountryRecord,
  getFirDataForTesting,
  getFirRecordsForCountry,
  getIso2FromCountry,
  getLatestYear,
  getManagingCountriesForFir,
  getTotalCountryCount,
  getTotalFirCount,
  getYearListStatus,
  guessUserCountry,
  isCountrySelectionMatch,
  isValidCountryIso2,
  MAX_CACHED_YEARS,
  parseYearsResponse,
  previousAvailableYear,
  RawCountryRecord,
  requireYear,
  resolveCountryIdentifier,
  resolveCountryIsoFromFeature,
  resolveCountryNameFromFeature,
  resolveCountrySelectionFromFeature,
  setAllCountriesForTesting,
  setAvailableYearsForTesting,
  setBoundedCacheEntry,
  setCountryDataInCache,
  setFirDataForTesting,
  setFirDataWithoutInvalidationForTesting,
  subscribeToCountryCache,
  topCountryByCo2eRank,
} from './CountryMetadata';
import {
  extractRankInfo,
  formatMwPerSqM,
  NOT_AVAILABLE_LABEL,
  UNIT_MILLIWATTS_PER_SQ_M,
  UNIT_TONNES_CO2E,
} from './metric_formatters';

describe('CountryMetadata Unit Tests', () => {
  afterEach(() => {
    clearCountryDataCache();
    clearTotalCountryCounts();
  });

  describe('guessUserCountry', () => {
    it('returns the region subtag of the first language that has one', () => {
      expect(guessUserCountry(['de-CH'])).toBe('CH');
      expect(guessUserCountry(['en', 'fr-be'])).toBe('BE');
      expect(guessUserCountry(['zh-Hant-TW'])).toBe('TW');
      expect(guessUserCountry(['pt_BR'])).toBe('BR');
    });

    it('returns null when no language names a country', () => {
      expect(guessUserCountry([])).toBeNull();
      expect(guessUserCountry(['de'])).toBeNull();
      expect(guessUserCountry(['es-419'])).toBeNull();
      expect(guessUserCountry(['zh-Hant'])).toBeNull();
    });
  });

  describe('topCountryByCo2eRank', () => {
    const ranked = (
      name: string,
      isoA2: string,
      gwp50: number | null,
    ): CountryOption => ({
      name,
      isoA2,
      co2eRanks: {GWP20: null, GWP50: gwp50, GWP100: gwp50 === null ? null : 1},
    });

    it('picks the country with the lowest over-country CO2e rank', () => {
      const countries = [
        ranked('France', 'FR', 3),
        ranked('Germany', 'DE', 1),
        ranked('Spain', 'ES', 2),
      ];
      expect(topCountryByCo2eRank(countries, 'GWP50')?.isoA2).toBe('DE');
    });

    it('reads the rank of the requested GWP horizon', () => {
      const countries = [
        ranked('France', 'FR', 2),
        {
          ...ranked('Spain', 'ES', 1),
          co2eRanks: {GWP20: 1, GWP50: 5, GWP100: 9},
        },
      ];
      expect(topCountryByCo2eRank(countries, 'GWP20')?.isoA2).toBe('ES');
      expect(topCountryByCo2eRank(countries, 'GWP50')?.isoA2).toBe('FR');
    });

    it('skips null, zero, and non-finite ranks and options without ranks', () => {
      const countries: CountryOption[] = [
        ranked('Austria', 'AT', null),
        ranked('Belgium', 'BE', 0),
        ranked('Chile', 'CL', Number.NaN),
        ranked('Denmark', 'DK', Number.NEGATIVE_INFINITY),
        {name: 'Estonia', isoA2: 'EE'},
        ranked('Finland', 'FI', 7),
      ];
      expect(topCountryByCo2eRank(countries, 'GWP50')?.isoA2).toBe('FI');
    });

    it('breaks rank ties by country name', () => {
      const countries = [
        ranked('Switzerland', 'CH', 1),
        ranked('Austria', 'AT', 1),
        ranked('Germany', 'DE', 1),
      ];
      expect(topCountryByCo2eRank(countries, 'GWP50')?.isoA2).toBe('AT');
    });

    it('returns null for an empty list or when no country is ranked', () => {
      expect(topCountryByCo2eRank([], 'GWP50')).toBeNull();
      expect(
        topCountryByCo2eRank([ranked('Austria', 'AT', null)], 'GWP50'),
      ).toBeNull();
    });
  });

  describe('country list status', () => {
    it('starts loading, becomes ready once the list is set, resets on clear, and returns distinct array references', () => {
      clearCountryDataCache();
      const initialRef = getAllCountries();
      expect(getCountryListStatus()).toBe('loading');
      expect(initialRef.length).toBe(0);
      setAllCountriesForTesting([{name: 'Switzerland', isoA2: 'CH'}]);
      const populatedRef = getAllCountries();
      expect(getCountryListStatus()).toBe('ready');
      expect(populatedRef).not.toBe(initialRef);
      expect(populatedRef).toEqual([{name: 'Switzerland', isoA2: 'CH'}]);
      clearCountryDataCache();
      const clearedRef = getAllCountries();
      expect(getCountryListStatus()).toBe('loading');
      expect(clearedRef).not.toBe(populatedRef);
      expect(clearedRef).toEqual([]);
    });

    it('becomes error when every /countries request fails', async () => {
      clearCountryDataCache();
      spyOn(globalThis, 'fetch').and.rejectWith(new Error('offline'));
      await fetchCountriesFromBackend(2025);
      expect(getAllCountries().length).toBe(0);
      expect(getCountryListStatus()).toBe('error');
    });

    it('fetches the same-origin API without forcing credentials', async () => {
      clearCountryDataCache();
      const fetchSpy = spyOn(globalThis, 'fetch').and.rejectWith(
        new Error('offline'),
      );
      await fetchCountriesFromBackend(2025);
      const init = fetchSpy.calls.mostRecent().args[1];
      expect(init?.credentials).toBeUndefined();
    });

    it('is set when a year is re-requested while its first request is in flight', async () => {
      clearCountryDataCache();
      const okJson = (body: unknown) =>
        ({
          ok: true,
          status: 200,
          json: async () => body,
        }) as unknown as Response;
      const resolvers = new Map<string, (r: Response) => void>();
      const fetchSpy = spyOn(globalThis, 'fetch').and.callFake(
        (input: RequestInfo | URL) => {
          const url = String(input);
          if (!url.startsWith('/countries')) {
            return Promise.resolve({
              ok: false,
              status: 404,
            } as unknown as Response);
          }
          return new Promise<Response>((resolve) => {
            resolvers.set(url, resolve);
          });
        },
      );

      // 2025 -> 2024 -> back to 2025 before the first 2025 response arrives.
      const first2025 = fetchCountriesFromBackend(2025);
      const only2024 = fetchCountriesFromBackend(2024);
      const repeat2025 = fetchCountriesFromBackend(2025);
      const countryUrls = fetchSpy.calls
        .allArgs()
        .map((args) => String(args[0]))
        .filter((u) => u.startsWith('/countries'));
      // The repeat reuses the in-flight request.
      expect(countryUrls).toEqual(['/countries/2025', '/countries/2024']);

      resolvers.get('/countries/2025')!(
        okJson([{name: 'France', 'iso_a2': 'FR'}]),
      );
      await Promise.all([first2025, repeat2025]);
      expect(getAllCountries().map((c) => c.isoA2)).toEqual(['FR']);
      expect(getCountryListStatus()).toBe('ready');

      // The stale 2024 response must not overwrite the 2025 list.
      resolvers.get('/countries/2024')!(
        okJson([{name: 'Germany', 'iso_a2': 'DE'}]),
      );
      expect((await only2024).map((c) => c.isoA2)).toEqual(['DE']);
      expect(getAllCountries().map((c) => c.isoA2)).toEqual(['FR']);
    });

    it('shares one /countries request between the total and the picker', async () => {
      clearCountryDataCache();
      clearTotalCountryCounts();
      let resolve2025: ((r: Response) => void) | undefined;
      const fetchSpy = spyOn(globalThis, 'fetch').and.callFake(
        (input: RequestInfo | URL) => {
          if (String(input) !== '/countries/2025') {
            return Promise.resolve({
              ok: false,
              status: 404,
            } as unknown as Response);
          }
          return new Promise<Response>((resolve) => {
            resolve2025 = resolve;
          });
        },
      );

      // The metrics panel asks for the total before the picker loads the list.
      const total = fetchCountryTotalFromBackend(2025);
      const picker = fetchCountriesFromBackend(2025);
      resolve2025!({
        ok: true,
        status: 200,
        json: async () => [{name: 'France', 'iso_a2': 'FR'}],
      } as unknown as Response);
      await Promise.all([total, picker]);

      const countryCalls = fetchSpy.calls
        .allArgs()
        .filter((args) => String(args[0]) === '/countries/2025');
      expect(countryCalls.length).toBe(1);
      // The picker still publishes the list from the shared request.
      expect(getAllCountries().map((c) => c.isoA2)).toEqual(['FR']);
    });

    it('keeps a newer in-flight request when an older one settles after a cache clear', async () => {
      clearCountryDataCache();
      clearTotalCountryCounts();
      const resolvers: Array<(r: Response) => void> = [];
      const fetchSpy = spyOn(globalThis, 'fetch').and.callFake(
        (input: RequestInfo | URL) => {
          if (String(input) !== '/countries/2025') {
            return Promise.resolve({
              ok: false,
              status: 404,
            } as unknown as Response);
          }
          return new Promise<Response>((resolve) => {
            resolvers.push(resolve);
          });
        },
      );
      const franceResponse = () =>
        ({
          ok: true,
          status: 200,
          json: async () => [{name: 'France', 'iso_a2': 'FR'}],
        }) as unknown as Response;
      const countryCallCount = () =>
        fetchSpy.calls
          .allArgs()
          .filter((args) => String(args[0]) === '/countries/2025').length;

      const stale = fetchCountriesFromBackend(2025);
      clearCountryDataCache();
      const fresh = fetchCountriesFromBackend(2025);
      expect(countryCallCount()).toBe(2);

      // The stale request settling must not evict the fresh one, so a later
      // caller still shares it.
      resolvers[0](franceResponse());
      await stale;
      const sharer = fetchCountriesFromBackend(2025);
      expect(countryCallCount()).toBe(2);

      resolvers[1](franceResponse());
      await Promise.all([fresh, sharer]);
      expect(getAllCountries().map((c) => c.isoA2)).toEqual(['FR']);
    });

    it('reports one error when a shared /countries request fails', async () => {
      clearCountryDataCache();
      clearTotalCountryCounts();
      spyOn(console, 'warn');
      let reject2025: ((err: Error) => void) | undefined;
      spyOn(globalThis, 'fetch').and.callFake((input: RequestInfo | URL) => {
        if (String(input) !== '/countries/2025') {
          return Promise.resolve({
            ok: false,
            status: 404,
          } as unknown as Response);
        }
        return new Promise<Response>((_, reject) => {
          reject2025 = reject;
        });
      });
      const listener = jasmine.createSpy<() => void>('countryCacheListener');
      const unsubscribe = subscribeToCountryCache(listener);

      const total = fetchCountryTotalFromBackend(2025);
      const picker = fetchCountriesFromBackend(2025);
      reject2025!(new Error('offline'));
      await Promise.all([total, picker]);
      unsubscribe();

      expect(getCountryListStatus()).toBe('error');
      expect(getAllCountries()).toEqual([]);
      expect(listener).toHaveBeenCalledTimes(1);
    });
  });

  describe('resolveCountryIsoFromFeature', () => {
    it('returns null for falsy features', () => {
      expect(resolveCountryIsoFromFeature(null)).toBeNull();
      expect(resolveCountryIsoFromFeature(undefined)).toBeNull();
    });

    it('resolves country ISO from properties.iso_a2', () => {
      expect(resolveCountryIsoFromFeature({properties: {'iso_a2': 'FR'}})).toBe(
        'FR',
      );
      expect(
        resolveCountryIsoFromFeature({properties: {'iso_a2': ' it '}}),
      ).toBe('IT');
    });

    it('returns null for sentinel or missing codes, with no name lookup', () => {
      expect(
        resolveCountryIsoFromFeature({properties: {'iso_a2': '-99'}}),
      ).toBeNull();
      expect(
        resolveCountryIsoFromFeature({properties: {name: 'France'}}),
      ).toBeNull();
      expect(
        resolveCountryIsoFromFeature({'id': 756} as unknown as GeoFeatureLike),
      ).toBeNull();
    });
  });

  describe('resolveCountryNameFromFeature', () => {
    it('returns null for falsy features', () => {
      expect(resolveCountryNameFromFeature(null)).toBeNull();
      expect(resolveCountryNameFromFeature(undefined)).toBeNull();
    });

    it('prefers the backend name for the feature ISO code', () => {
      setAllCountriesForTesting([{name: 'Czechia', isoA2: 'CZ'}]);
      expect(
        resolveCountryNameFromFeature({
          properties: {'iso_a2': 'CZ', name: 'Czech Rep.'},
        }),
      ).toBe('Czechia');
    });

    it('falls back to the feature name, then the ISO code', () => {
      expect(
        resolveCountryNameFromFeature({
          properties: {'iso_a2': 'FR', name: 'France'},
        }),
      ).toBe('France');
      expect(
        resolveCountryNameFromFeature({properties: {'iso_a2': 'FR'}}),
      ).toBe('FR');
    });

    it('uses the feature name for sentinel-coded territories', () => {
      expect(
        resolveCountryNameFromFeature({
          properties: {'iso_a2': '-99', name: 'Kosovo'},
        }),
      ).toBe('Kosovo');
    });
  });

  describe('resolveCountryIdentifier and isCountrySelectionMatch', () => {
    it('resolves valid ISO-A2 codes, sentinel territory names, and empty inputs', () => {
      expect(resolveCountryIdentifier('CH', 'Switzerland')).toBe('CH');
      expect(resolveCountryIdentifier('-99', 'Somaliland')).toBe('Somaliland');
      expect(resolveCountryIdentifier('-99', 'N. Cyprus')).toBe('N. Cyprus');
      expect(resolveCountryIdentifier('-99', '')).toBe('');
    });

    it('matches countries by name or valid ISO-A2 without colliding on sentinel -99', () => {
      expect(
        isCountrySelectionMatch({
          isoA2: '-99',
          name: 'Somaliland',
          selectedCountry: 'Somaliland',
        }),
      ).toBeTrue();
      expect(
        isCountrySelectionMatch({
          isoA2: '-99',
          name: 'N. Cyprus',
          selectedCountry: 'Somaliland',
        }),
      ).toBeFalse();
      expect(
        isCountrySelectionMatch({
          isoA2: '-99',
          name: 'Somaliland',
          selectedCountry: '-99',
        }),
      ).toBeFalse();
      expect(
        isCountrySelectionMatch({
          isoA2: 'CH',
          name: 'Switzerland',
          selectedCountry: 'CH',
        }),
      ).toBeTrue();
      expect(
        isCountrySelectionMatch({
          isoA2: 'CH',
          name: 'Switzerland',
          selectedCountry: 'Switzerland',
        }),
      ).toBeTrue();
    });
  });

  describe('getCountryRecord', () => {
    beforeEach(() => {
      setCountryDataInCache('CH_2025', {
        name: 'Switzerland',
        'iso_a2': 'CH',
        'year': 2025,
      });
      setCountryDataInCache('DE_2024', {
        name: 'Germany',
        'iso_a2': 'DE',
        'year': 2024,
      });
      setCountryDataInCache('FR_2025', {
        name: 'France',
        'iso_a2': 'FR',
        'year': 2025,
      });
    });

    it('returns null for empty input', () => {
      expect(getCountryRecord('', 2025)).toBeNull();
    });

    it('finds records by key with case insensitivity', () => {
      expect(getCountryRecord('CH', 2025)).toEqual({
        name: 'Switzerland',
        'iso_a2': 'CH',
        'year': 2025,
      });
      expect(getCountryRecord('ch', 2025)).toEqual({
        name: 'Switzerland',
        'iso_a2': 'CH',
        'year': 2025,
      });
      expect(getCountryRecord('DE', 2024)).toEqual({
        name: 'Germany',
        'iso_a2': 'DE',
        'year': 2024,
      });
      expect(getCountryRecord('FR', 2025)).toEqual({
        name: 'France',
        'iso_a2': 'FR',
        'year': 2025,
      });
    });

    it('returns null if no record matches', () => {
      expect(getCountryRecord('UNKNOWN', 2025)).toBeNull();
    });
  });

  describe('getCountryMetrics', () => {
    it('returns loading state when country record is not in PER_COUNTRY_DATA_MAP', () => {
      const metrics = getCountryMetrics('NonExistent_Country_XYZ', 2025);
      expect(metrics.isLoading).toBeTrue();
      expect(metrics.impactDensity).toBe(NOT_AVAILABLE_LABEL);
      expect(metrics.impactDensityRank).toBe(NOT_AVAILABLE_LABEL);
      expect(metrics.aboveCountryHero.absoluteValue).toBe(NOT_AVAILABLE_LABEL);
      expect(metrics.controlledFirRecords.length).toBe(0);
    });

    it('computes metrics and formatters accurately for an existing record', () => {
      const mockRecord2025: RawCountryRecord = {
        name: 'Switzerland',
        'iso_a2': 'CH',
        'mean_rf_mw_per_sq_m_rank': 5,
        'co2e_gwp50_tonnes_rank': 12,
        'mean_rf_mw_per_sq_m': 45.67,
        'co2e_gwp50_tonnes': 1500000,
        'in_country_impact': 0.25,
        'co2e_gwp50_vs_national_ghg': 0.035,
        'ghg_mtco2e': 45.0,
        'co2e_gwp50_tonnes_per_capita': 0.17,
        population: 8700000,
        'total_flight_impact': 1000,
        'outgoing_flight_impact': 400,
        'outgoing_flight_impact_rank': 8,
        'outgoing_flight_co2e_gwp50_tonnes': 600000,
        'outgoing_flight_co2e_gwp50_tonnes_per_capita': 0.068,
        'outgoing_flight_co2e_gwp50_vs_national_ghg': 0.013,
        'segment_count': 25000,
        'fir_total_co2e_gwp50_tonnes': 2000000,
        'fir_total_co2e_gwp50_tonnes_rank': 10,
        'fir_total_fir_id': ['LSAS_SWITZERLAND'],
        'fir_total_firname': ['SWITZERLAND'],
        'fir_total_fir_icao': ['LSAS'],
        'fir_total_mean_rf_mw_per_sq_m': 52.3,
        'fir_total_co2e_gwp50_tonnes_per_capita': 0.23,
        'fir_total_co2e_gwp50_vs_national_ghg': 0.044,
        'fir_dom_co2e_gwp50_tonnes': 1800000,
        'fir_int_co2e_gwp50_tonnes': 200000,
        'co2e_gwp50_per_capita_vs_world_avg': 0.0283,
        'outgoing_flight_co2e_gwp50_per_capita_vs_world_avg': 0.0113,
        'fir_total_co2e_gwp50_per_capita_vs_world_avg': 0.0383,
      };

      setCountryDataInCache('CH_2025', mockRecord2025);

      const metrics = getCountryMetrics('CH', 2025, 'GWP50');

      expect(metrics).toEqual(
        jasmine.objectContaining({
          isoA2: 'CH',
          // /countries has not loaded, so the total is unknown and omitted.
          impactDensityRank: '#5',
          impactDensity: '45.7 mW/m²',
          overflightFraction: '75.0%',
          firDensity: '52.3 mW/m²',
          hasInternationalFir: true,
        }),
      );
      expect(metrics.aboveCountryHero).toEqual(
        jasmine.objectContaining({
          absoluteValue: '1.5 Mt CO₂e',
          absoluteRank: '#12',
          nationalGhgComparison: '3.50%',
          perCapitaValue: '0.17 t CO₂e',
          populationFormatted: '8.7M population',
        }),
      );
      expect(metrics.outgoingHero).toEqual(
        jasmine.objectContaining({
          absoluteValue: '600.0 Kt CO₂e',
          absoluteRank: '#8',
        }),
      );
      expect(metrics.firTotalHero).toEqual(
        jasmine.objectContaining({
          absoluteValue: '2.0 Mt CO₂e',
          absoluteRank: '#10',
        }),
      );
    });

    it('shows no YoY when the backend does not list the previous year', () => {
      setAvailableYearsForTesting([2025]);
      setCountryDataInCache('DE_2025', {
        name: 'Germany',
        'iso_a2': 'DE',
        'co2e_gwp50_tonnes': 3000000,
      });
      setCountryDataInCache('DE_2024', {
        name: 'Germany',
        'iso_a2': 'DE',
        'co2e_gwp50_tonnes': 2500000,
      });
      expect(
        getCountryMetrics('DE', 2025, 'GWP50').aboveCountryHero.absoluteYoY,
      ).toBeUndefined();
      setAvailableYearsForTesting([2024, 2025]);
      expect(
        getCountryMetrics('DE', 2025, 'GWP50').aboveCountryHero.absoluteYoY,
      ).toBeDefined();
    });

    it('calculates YoY comparisons when previous year record is present', () => {
      // YoY needs the backend to list the previous year.
      setAvailableYearsForTesting([2024, 2025]);
      const mock2025 = {
        name: 'Germany',
        'iso_a2': 'DE',
        'mean_rf_mw_per_sq_m_rank': 4,
        'mean_rf_mw_per_sq_m': 60.0,
        'co2e_gwp50_tonnes': 3000000,
        'co2e_gwp50_tonnes_per_capita': 0.036,
        'outgoing_flight_co2e_gwp50_tonnes': 1200000,
        'outgoing_flight_impact_rank': 5,
        'fir_total_co2e_gwp50_tonnes': 3500000,
        'fir_total_co2e_gwp50_tonnes_rank': 3,
        'fir_total_mean_rf_mw_per_sq_m': 58.0,
      };

      const mock2024 = {
        name: 'Germany',
        'iso_a2': 'DE',
        'mean_rf_mw_per_sq_m_rank': 6,
        'mean_rf_mw_per_sq_m': 50.0,
        'co2e_gwp50_tonnes': 2500000,
        'co2e_gwp50_tonnes_per_capita': 0.03,
        'outgoing_flight_co2e_gwp50_tonnes': 1000000,
        'outgoing_flight_impact_rank': 7,
        'fir_total_co2e_gwp50_tonnes': 3000000,
        'fir_total_co2e_gwp50_tonnes_rank': 4,
        'fir_total_mean_rf_mw_per_sq_m': 50.0,
      };

      setCountryDataInCache('DE_2025', mock2025);
      setCountryDataInCache('DE_2024', mock2024);

      const metrics = getCountryMetrics('DE', 2025, 'GWP50');

      // Absolute impact increased from 2.5M to 3.0M (+20%)
      expect(metrics.aboveCountryHero.absoluteYoY).toEqual({
        text: '▲ +20.0% YoY',
        direction: 'up',
        color: '#d93025',
      });

      // Outgoing impact increased from 1.0M to 1.2M (+20%)
      expect(metrics.outgoingHero.absoluteYoY).toEqual({
        text: '▲ +20.0% YoY',
        direction: 'up',
        color: '#d93025',
      });
    });
  });

  describe('buildHeroScopeMetrics', () => {
    it('formats values, ranks, and YoY for a scope prefix and respects fallback options', () => {
      const item: RawCountryRecord = {
        'fir_total_rank': 7,
        'fir_total_co2e_gwp50_tonnes_per_capita': 0.25,
        'fir_total_co2e_gwp50_tonnes_per_capita_rank': 11,
        'fir_total_co2e_gwp50_per_capita_vs_world_avg': 0.04,
        'fir_total_co2e_gwp50_vs_national_ghg': 0.05,
        'fir_total_co2e_gwp50_vs_national_ghg_rank': 6,
      };
      const prevItem: RawCountryRecord = {
        'fir_total_co2e_gwp50_tonnes': 100,
      };
      const firScope = buildHeroScopeMetrics({
        item,
        prevItem,
        prefix: 'fir_total_',
        gwpStr: 'gwp50',
        totalCountries: 195,
        populationFormatted: '8.7M population',
        nationalGhgTotal: '45.0 Mt CO₂e',
        fallbackRankField: 'fir_total_rank',
        allowFallbackRankWithoutTonnes: true,
        defaultZeroTonnes: true,
        defaultNotAvailableRank: true,
      });

      expect(firScope).toEqual({
        absoluteValue: '0.00 t CO₂e',
        absoluteYoY: {
          text: '▼ -100.0% YoY',
          direction: 'down',
          color: '#188038',
        },
        absoluteRank: '#7 / 195 countries',
        absoluteRankNum: 7,
        perCapitaValue: '0.25 t CO₂e',
        perCapitaRank: '#11 / 195 countries',
        perCapitaRankNum: 11,
        perCapitaVsWorldAvg: '4.00% of global GHG/capita',
        populationFormatted: '8.7M population',
        nationalGhgComparison: '5.00%',
        nationalGhgRank: '#6 / 195 countries',
        nationalGhgRankNum: 6,
        nationalGhgTotal: '45.0 Mt CO₂e',
        totalCount: 195,
      });
    });
  });

  describe('fetchRankingsFromBackend without a reachable backend', () => {
    it('returns null instead of ranking the locally cached records', async () => {
      spyOn(globalThis, 'fetch').and.rejectWith(new Error('Network offline'));
      setAllCountriesForTesting([
        {name: 'Austria', isoA2: 'AT'},
        {name: 'Belgium', isoA2: 'BE'},
      ]);

      setCountryDataInCache('AT_2025', {
        name: 'Austria',
        'iso_a2': 'AT',
        'mean_rf_mw_per_sq_m': 35.0,
        'mean_rf_mw_per_sq_m_rank': 2,
        'co2e_gwp50_tonnes': 500000,
        'co2e_gwp50_tonnes_rank': 4,
      });
      setCountryDataInCache('BE_2025', {
        name: 'Belgium',
        'iso_a2': 'BE',
        'mean_rf_mw_per_sq_m': 55.0,
        'mean_rf_mw_per_sq_m_rank': 1,
        'co2e_gwp50_tonnes': 800000,
        'co2e_gwp50_tonnes_rank': 2,
      });

      const rankings = await fetchRankingsFromBackend({
        metric: 'mean_rf_mw_per_sq_m',
        year: 2025,
        gwp: 'GWP50',
      });
      expect(rankings).toBeNull();
    });

    it('logs the HTTP status and returns null on a non-2xx response', async () => {
      spyOn(globalThis, 'fetch').and.resolveTo({
        ok: false,
        status: 404,
      } as unknown as Response);
      const warnSpy = spyOn(console, 'warn');

      const rankings = await fetchRankingsFromBackend({
        metric: 'unknown_metric',
        year: 2025,
      });

      expect(rankings).toBeNull();
      expect(warnSpy).toHaveBeenCalledWith(
        'Failed to fetch rankings from ' +
          '/rankings/2025/unknown_metric?gwp=gwp50: HTTP 404',
      );
    });

    it('logs and returns null when the body is not a rankings payload', async () => {
      spyOn(globalThis, 'fetch').and.resolveTo({
        ok: true,
        status: 200,
        json: async () => ({unexpected: true}),
      } as unknown as Response);
      const warnSpy = spyOn(console, 'warn');

      const rankings = await fetchRankingsFromBackend({
        metric: 'co2e_tonnes',
        year: 2025,
      });

      expect(rankings).toBeNull();
      expect(warnSpy).toHaveBeenCalledWith(
        'Malformed rankings response from /rankings/2025/co2e_tonnes?gwp=gwp50',
      );
    });

    it('does not log when the caller aborts the request', async () => {
      const controller = new AbortController();
      spyOn(globalThis, 'fetch').and.callFake(async () => {
        controller.abort();
        throw new DOMException('Aborted', 'AbortError');
      });
      const warnSpy = spyOn(console, 'warn');

      const rankings = await fetchRankingsFromBackend({
        metric: 'co2e_tonnes',
        year: 2025,
        signal: controller.signal,
      });

      expect(rankings).toBeNull();
      expect(warnSpy).not.toHaveBeenCalled();
    });
  });

  describe('Explicit years', () => {
    const okJson = (body: unknown) =>
      ({ok: true, status: 200, json: async () => body}) as unknown as Response;

    describe('year list state', () => {
      it('starts empty and loading, with no latest year', () => {
        expect(getAvailableYears()).toEqual([]);
        expect(getYearListStatus()).toBe('loading');
        expect(getLatestYear()).toBeNull();
      });

      it('returns to empty and loading when the cache is cleared', () => {
        setAvailableYearsForTesting([2030]);
        clearCountryDataCache();
        expect(getAvailableYears()).toEqual([]);
        expect(getYearListStatus()).toBe('loading');
        expect(getLatestYear()).toBeNull();
      });

      it('takes any 4-digit year the backend reports, sorted ascending', () => {
        setAvailableYearsForTesting([1990, 2099, 2050]);
        expect(getAvailableYears()).toEqual([1990, 2050, 2099]);
        expect(getLatestYear()).toBe(2099);
      });

      it('marks an empty list set for testing as ready, without inventing a year', () => {
        setAvailableYearsForTesting([]);
        expect(getYearListStatus()).toBe('ready');
        expect(getAvailableYears()).toEqual([]);
        expect(getLatestYear()).toBeNull();
      });
    });

    describe('parseYearsResponse', () => {
      it('sorts and de-duplicates years', () => {
        expect(parseYearsResponse({years: [2031, 2030, 2031]})).toEqual([
          2030, 2031,
        ]);
      });

      it('accepts an empty list', () => {
        expect(parseYearsResponse({years: []})).toEqual([]);
      });

      it('rejects non-object bodies or non-array years fields', () => {
        expect(parseYearsResponse(null)).toBeNull();
        expect(parseYearsResponse('x')).toBeNull();
        expect(parseYearsResponse({})).toBeNull();
        expect(parseYearsResponse({years: 'x'})).toBeNull();
      });

      it('rejects years arrays with non-integer or out-of-range entries', () => {
        expect(parseYearsResponse({years: ['2030']})).toBeNull();
        expect(parseYearsResponse({years: [999]})).toBeNull();
        expect(parseYearsResponse({years: [2030.5]})).toBeNull();
        expect(parseYearsResponse({years: [2030, null]})).toBeNull();
      });

      it('ignores extra fields such as a legacy default_year', () => {
        expect(
          parseYearsResponse({years: [2030], 'default_year': 1999}),
        ).toEqual([2030]);
      });
    });

    describe('fetchAvailableYearsFromBackend', () => {
      it('treats an empty list as ready with no years', async () => {
        spyOn(globalThis, 'fetch').and.resolveTo(okJson({years: []}));
        expect(await fetchAvailableYearsFromBackend()).toEqual([]);
        expect(getYearListStatus()).toBe('ready');
        expect(getLatestYear()).toBeNull();
      });

      it('marks the list as errored on an HTTP error', async () => {
        spyOn(globalThis, 'fetch').and.resolveTo({
          ok: false,
          status: 503,
        } as unknown as Response);
        expect(await fetchAvailableYearsFromBackend()).toEqual([]);
        expect(getYearListStatus()).toBe('error');
      });

      it('marks the list as errored on a network failure', async () => {
        spyOn(globalThis, 'fetch').and.rejectWith(new Error('offline'));
        await fetchAvailableYearsFromBackend();
        expect(getYearListStatus()).toBe('error');
      });

      it('marks the list as errored on a malformed body', async () => {
        spyOn(globalThis, 'fetch').and.resolveTo(okJson({years: ['2030']}));
        await fetchAvailableYearsFromBackend();
        expect(getYearListStatus()).toBe('error');
        expect(getAvailableYears()).toEqual([]);
      });

      it('recovers from an error on retry', async () => {
        const fetchSpy = spyOn(globalThis, 'fetch');
        fetchSpy.and.rejectWith(new Error('offline'));
        await fetchAvailableYearsFromBackend();
        expect(getYearListStatus()).toBe('error');
        fetchSpy.and.resolveTo(okJson({years: [2030]}));
        expect(await fetchAvailableYearsFromBackend()).toEqual([2030]);
        expect(getYearListStatus()).toBe('ready');
      });

      it('keeps loaded years when a later refresh fails', async () => {
        const fetchSpy = spyOn(globalThis, 'fetch');
        fetchSpy.and.resolveTo(okJson({years: [2030, 2031]}));
        await fetchAvailableYearsFromBackend();
        fetchSpy.and.rejectWith(new Error('offline'));
        expect(await fetchAvailableYearsFromBackend()).toEqual([2030, 2031]);
        expect(getYearListStatus()).toBe('ready');
      });

      it('shares one request between concurrent callers', async () => {
        const fetchSpy = spyOn(globalThis, 'fetch').and.resolveTo(
          okJson({years: [2030]}),
        );
        const [a, b] = await Promise.all([
          fetchAvailableYearsFromBackend(),
          fetchAvailableYearsFromBackend(),
        ]);
        expect(a).toEqual([2030]);
        expect(b).toEqual([2030]);
        expect(fetchSpy).toHaveBeenCalledTimes(1);
      });

      it('does not notify listeners when nothing changed', async () => {
        spyOn(globalThis, 'fetch').and.resolveTo(okJson({years: [2030]}));
        await fetchAvailableYearsFromBackend();
        let notified = 0;
        const unsubscribe = subscribeToCountryCache(() => {
          notified++;
        });
        await fetchAvailableYearsFromBackend();
        unsubscribe();
        expect(notified).toBe(0);
      });
    });

    describe('previousAvailableYear', () => {
      it('returns the calendar year before when it is available', () => {
        setAvailableYearsForTesting([2040, 2041]);
        expect(previousAvailableYear(2041)).toBe(2040);
      });

      it('never skips a missing year', () => {
        setAvailableYearsForTesting([2038, 2041]);
        expect(previousAvailableYear(2041)).toBeNull();
      });

      it('returns null for the earliest year, bad input, or no years', () => {
        setAvailableYearsForTesting([2040, 2041]);
        expect(previousAvailableYear(2040)).toBeNull();
        expect(previousAvailableYear(0)).toBeNull();
        expect(previousAvailableYear(Number.NaN)).toBeNull();
        setAvailableYearsForTesting([]);
        expect(previousAvailableYear(2041)).toBeNull();
      });
    });

    describe('requireYear', () => {
      it('returns the valid year', () => {
        expect(requireYear(2030, 'ctx')).toBe(2030);
      });

      it('throws on an invalid or missing year', () => {
        expect(() => requireYear(0, 'myCaller')).toThrowError(
          /myCaller: a year is required/,
        );
        expect(() => requireYear(Number.NaN, 'myCaller')).toThrowError(
          /myCaller: a year is required/,
        );
        expect(() => requireYear(undefined, 'myCaller')).toThrowError(
          /myCaller: a year is required/,
        );
        expect(() => requireYear(null, 'myCaller')).toThrowError(
          /myCaller: a year is required/,
        );
      });
    });

    describe('fetchers never fall back to a year', () => {
      it('fetchCountriesFromBackend rejects an invalid year without calling the backend', async () => {
        const fetchSpy = spyOn(globalThis, 'fetch');
        await expectAsync(fetchCountriesFromBackend(0)).toBeRejectedWithError(
          /fetchCountriesFromBackend: a year is required/,
        );
        expect(fetchSpy).not.toHaveBeenCalled();
      });

      it('fetchFirsFromBackend rejects an invalid year without calling the backend', async () => {
        const fetchSpy = spyOn(globalThis, 'fetch');
        await expectAsync(
          fetchFirsFromBackend(Number.NaN),
        ).toBeRejectedWithError(/fetchFirsFromBackend: a year is required/);
        expect(fetchSpy).not.toHaveBeenCalled();
      });

      it('fetchCountryTotalFromBackend rejects an invalid year without calling the backend', async () => {
        const fetchSpy = spyOn(globalThis, 'fetch');
        await expectAsync(
          fetchCountryTotalFromBackend(0),
        ).toBeRejectedWithError(
          /fetchCountryTotalFromBackend: a year is required/,
        );
        expect(fetchSpy).not.toHaveBeenCalled();
      });

      it('fetchCountryRecordFromBackend rejects an invalid year without calling the backend', async () => {
        const fetchSpy = spyOn(globalThis, 'fetch');
        await expectAsync(
          fetchCountryRecordFromBackend('CH', 0),
        ).toBeRejectedWithError(
          /fetchCountryRecordFromBackend: a year is required/,
        );
        expect(fetchSpy).not.toHaveBeenCalled();
      });

      it('fetchRankingsFromBackend rejects an invalid year without calling the backend', async () => {
        const fetchSpy = spyOn(globalThis, 'fetch');
        await expectAsync(
          fetchRankingsFromBackend({metric: 'co2e_gwp_tonnes', year: 0}),
        ).toBeRejectedWithError(/fetchRankingsFromBackend: a year is required/);
        expect(fetchSpy).not.toHaveBeenCalled();
      });

      it('puts the requested year in every request URL', async () => {
        const fetchSpy = spyOn(globalThis, 'fetch').and.resolveTo({
          ok: false,
          status: 404,
        } as unknown as Response);
        await fetchCountryTotalFromBackend(2037);
        await fetchFirsFromBackend(2037);
        await fetchCountryRecordFromBackend('CH', 2037);
        await fetchRankingsFromBackend({
          metric: 'co2e_gwp_tonnes',
          year: 2037,
        });
        const urls = fetchSpy.calls.allArgs().map((args) => String(args[0]));
        expect(urls).toEqual([
          '/countries/2037',
          '/firs/2037',
          '/country/2037/CH',
          '/rankings/2037/co2e_gwp_tonnes?gwp=gwp50',
        ]);
      });

      it('also fetches the previous year only when the backend lists it', async () => {
        const fetchSpy = spyOn(globalThis, 'fetch').and.resolveTo({
          ok: false,
          status: 404,
        } as unknown as Response);
        setAvailableYearsForTesting([2036, 2037]);
        await fetchCountriesFromBackend(2037);
        let urls = fetchSpy.calls.allArgs().map((args) => String(args[0]));
        expect(urls).toContain('/countries/2036');

        fetchSpy.calls.reset();
        setAvailableYearsForTesting([2035, 2037]);
        await fetchCountriesFromBackend(2037);
        urls = fetchSpy.calls.allArgs().map((args) => String(args[0]));
        expect(urls.every((u) => u.endsWith('/2037'))).toBeTrue();
      });
    });

    describe('synchronous getters without a year', () => {
      it('find no record instead of assuming a year', () => {
        setCountryDataInCache('CH_2025', {name: 'Switzerland', 'iso_a2': 'CH'});
        expect(getCountryRecord('CH', 0)).toBeNull();
        expect(getCountryRecord('CH', Number.NaN)).toBeNull();
        expect(getCountryRecord('CH', 2025)).not.toBeNull();
      });

      it('return no FIRs instead of assuming a year', () => {
        expect(getFirRecordsForCountry('CH', 0)).toEqual([]);
      });
    });
  });

  describe('Async Backend Fetchers', () => {
    describe('fetchAvailableYearsFromBackend', () => {
      it('fetches the available years from /years', async () => {
        spyOn(globalThis, 'fetch').and.resolveTo({
          ok: true,
          json: async () => ({years: [2022, 2023, 2024, 2025]}),
        } as unknown as Response);

        const years = await fetchAvailableYearsFromBackend();
        expect(years).toEqual([2022, 2023, 2024, 2025]);
        expect(getAvailableYears()).toEqual([2022, 2023, 2024, 2025]);
        expect(getYearListStatus()).toBe('ready');
        expect(getLatestYear()).toBe(2025);
      });
    });

    describe('fetchCountriesFromBackend', () => {
      it('handles successful backend response and populates country cache', async () => {
        const mockRows = [
          {name: 'France', 'iso_a2': 'FR', 'co2e_gwp50_tonnes_rank': 1},
          {name: 'Spain', 'iso_a2': 'ES', 'co2e_gwp50_tonnes_rank': 2},
        ];
        spyOn(globalThis, 'fetch').and.resolveTo({
          ok: true,
          json: async () => mockRows,
        } as unknown as Response);

        const res = await fetchCountriesFromBackend(2025);
        expect(res.length).toBeGreaterThanOrEqual(2);
        expect(getCountryDataFromCache('FR_2025')).toEqual(mockRows[0]);
        expect(getCountryDataFromCache('ES_2025')).toEqual(mockRows[1]);
      });

      it('returns empty array on network failure', async () => {
        spyOn(globalThis, 'fetch').and.rejectWith(new Error('Network offline'));

        const res = await fetchCountriesFromBackend(2025);
        expect(res).toEqual([]);
      });
    });

    describe('fetchCountryRecordFromBackend', () => {
      it('caches and returns country record', async () => {
        const mockRecord = {
          name: 'Italy',
          'iso_a2': 'IT',
          'mean_rf_mw_per_sq_m': 22.0,
        };
        spyOn(globalThis, 'fetch').and.resolveTo({
          ok: true,
          json: async () => mockRecord,
        } as unknown as Response);

        const res = await fetchCountryRecordFromBackend('IT', 2025);
        expect(res).toEqual(mockRecord);
        expect(getCountryDataFromCache('IT_2025')).toEqual(mockRecord);
      });

      it('returns null on 404 or network failure', async () => {
        spyOn(globalThis, 'fetch').and.resolveTo({
          ok: false,
          status: 404,
        } as unknown as Response);

        const res = await fetchCountryRecordFromBackend('UNKNOWN', 2025);
        expect(res).toBeNull();
      });
    });

    describe('fetchRankingsFromBackend', () => {
      it('returns backend data on success', async () => {
        const mockRankings = {
          metric: 'mean_rf_mw_per_sq_m',
          title: 'Mean RF',
          'year': 2025,
          gwp: 'gwp50',
          unit: UNIT_MILLIWATTS_PER_SQ_M,
          items: [{rank: 1, name: 'Switzerland', 'iso_a2': 'CH', value: 50.0}],
        };
        spyOn(globalThis, 'fetch').and.resolveTo({
          ok: true,
          json: async () => mockRankings,
        } as unknown as Response);

        const res = await fetchRankingsFromBackend({
          metric: 'mean_rf_mw_per_sq_m',
          year: 2025,
          gwp: 'GWP50',
        });
        expect(res).toEqual({
          metric: 'mean_rf_mw_per_sq_m',
          title: 'Mean RF',
          year: 2025,
          gwp: 'gwp50',
          unit: UNIT_MILLIWATTS_PER_SQ_M,
          items: [{rank: 1, name: 'Switzerland', isoA2: 'CH', value: 50.0}],
        });
      });

      it('rejects rankings payloads with a missing or invalid year', async () => {
        const fetchSpy = spyOn(globalThis, 'fetch').and.resolveTo({
          ok: true,
          status: 200,
          json: async () => ({
            metric: 'mean_rf_mw_per_sq_m',
            title: 'Mean RF',
            year: '2025',
            gwp: 'gwp50',
            items: [
              {rank: 1, name: 'Switzerland', 'iso_a2': 'CH', value: 50.0},
            ],
          }),
        } as unknown as Response);
        spyOn(console, 'warn');

        expect(
          await fetchRankingsFromBackend({
            metric: 'mean_rf_mw_per_sq_m',
            year: 2025,
          }),
        ).toBeNull();
        expect(fetchSpy).toHaveBeenCalledTimes(1);
      });
    });
  });

  describe('GWP Variations in getCountryMetrics', () => {
    beforeEach(() => {
      const gwpRecord: RawCountryRecord = {
        name: 'Sweden',
        'iso_a2': 'SE',
        'co2e_gwp20_tonnes': 3000000,
        'co2e_gwp20_tonnes_per_capita': 0.3,
        'co2e_gwp20_vs_national_ghg': 0.06,
        'outgoing_flight_co2e_gwp20_tonnes': 1500000,
        'outgoing_flight_co2e_gwp20_tonnes_per_capita': 0.15,
        'outgoing_flight_co2e_gwp20_vs_national_ghg': 0.03,
        'fir_total_co2e_gwp20_tonnes': 3500000,
        'co2e_gwp100_tonnes': 1000000,
        'co2e_gwp100_tonnes_per_capita': 0.1,
        'co2e_gwp100_vs_national_ghg': 0.02,
        'outgoing_flight_co2e_gwp100_tonnes': 500000,
        'outgoing_flight_co2e_gwp100_tonnes_per_capita': 0.05,
        'outgoing_flight_co2e_gwp100_vs_national_ghg': 0.01,
        'fir_total_co2e_gwp100_tonnes': 1200000,
        'mean_rf_mw_per_sq_m': 30.0,
        population: 10000000,
        'ghg_mtco2e': 50.0,
      };
      setCountryDataInCache('SE_2025', gwpRecord);
    });

    it('calculates metrics for GWP20 accurately', () => {
      const metrics = getCountryMetrics('SE', 2025, 'GWP20');
      expect(metrics.aboveCountryHero.absoluteValue).toBe('3.0 Mt CO₂e');
      expect(metrics.aboveCountryHero.perCapitaValue).toBe('0.30 t CO₂e');
      expect(metrics.aboveCountryHero.nationalGhgComparison).toBe('6.00%');
      expect(metrics.outgoingHero.absoluteValue).toBe('1.5 Mt CO₂e');
      expect(metrics.firTotalHero.absoluteValue).toBe('3.5 Mt CO₂e');
    });

    it('calculates metrics for GWP100 accurately', () => {
      const metrics = getCountryMetrics('SE', 2025, 'GWP100');
      expect(metrics.aboveCountryHero.absoluteValue).toBe('1.0 Mt CO₂e');
      expect(metrics.aboveCountryHero.perCapitaValue).toBe('0.10 t CO₂e');
      expect(metrics.aboveCountryHero.nationalGhgComparison).toBe('2.00%');
      expect(metrics.outgoingHero.absoluteValue).toBe('500.0 Kt CO₂e');
      expect(metrics.firTotalHero.absoluteValue).toBe('1.2 Mt CO₂e');
    });
  });

  describe('getCountryNameFromIso2', () => {
    beforeEach(() => {
      setAllCountriesForTesting([
        {name: 'Switzerland', isoA2: 'CH'},
        {name: 'Germany', isoA2: 'DE'},
        {name: 'France', isoA2: 'FR'},
        {name: 'United States', isoA2: 'US'},
      ]);
    });

    it('returns the ISO code unchanged before the backend list is loaded', () => {
      clearCountryDataCache();
      expect(getCountryNameFromIso2('CH')).toBe('CH');
    });

    it('resolves ISO-A2 code to country name', () => {
      expect(getCountryNameFromIso2('CH')).toBe('Switzerland');
      expect(getCountryNameFromIso2('DE')).toBe('Germany');
      expect(getCountryNameFromIso2('FR')).toBe('France');
      expect(getCountryNameFromIso2('US')).toBe('United States');
    });

    it('returns the input string if not a recognized ISO code', () => {
      expect(getCountryNameFromIso2('Switzerland')).toBe('Switzerland');
      expect(getCountryNameFromIso2('Germany')).toBe('Germany');
    });

    it('handles whitespace and casing', () => {
      expect(getCountryNameFromIso2(' ch ')).toBe('Switzerland');
      expect(getCountryNameFromIso2('de')).toBe('Germany');
    });

    it('returns empty string for empty input', () => {
      expect(getCountryNameFromIso2('')).toBe('');
    });

    it('populates name in getCountryMetrics', () => {
      setCountryDataInCache('SE_2025', {name: 'Sweden', 'iso_a2': 'SE'});
      const metrics = getCountryMetrics('SE', 2025, 'GWP50');
      expect(metrics.name).toBe('Sweden');
    });
  });

  describe('YoY calculation for hero metrics', () => {
    beforeEach(() => {
      const rec2025 = {
        name: 'Switzerland',
        'iso_a2': 'CH',
        'co2e_gwp50_tonnes': 1_400_000,
        'mean_rf_mw_per_sq_m': 1200.0,
        'outgoing_flight_co2e_gwp50_tonnes': 3_300_000,
        'fir_total_co2e_gwp50_tonnes': 1_300_000,
        'mean_rf_mw_per_sq_m_rank': 24,
      };
      const rec2024 = {
        name: 'Switzerland',
        'iso_a2': 'CH',
        'co2e_gwp50_tonnes': 1_600_000,
        'mean_rf_mw_per_sq_m': 1350.0,
        'outgoing_flight_co2e_gwp50_tonnes': 3_500_000,
        'fir_total_co2e_gwp50_tonnes': 1_450_000,
        'mean_rf_mw_per_sq_m_rank': 22,
      };
      setCountryDataInCache('CH_2025', rec2025);
      setCountryDataInCache('CH_2024', rec2024);
    });

    it('computes YoY badges for country hero metrics', () => {
      // YoY needs the backend to list the previous year.
      setAvailableYearsForTesting([2024, 2025]);
      const metrics = getCountryMetrics('CH', 2025, 'GWP50');
      expect(metrics.aboveCountryHero.absoluteYoY).toEqual({
        text: '▼ -12.5% YoY',
        direction: 'down',
        color: '#188038',
      });

      expect(metrics.outgoingHero.absoluteYoY).toEqual({
        text: '▼ -5.7% YoY',
        direction: 'down',
        color: '#188038',
      });

      expect(metrics.firTotalHero.absoluteYoY).toEqual({
        text: '▼ -10.3% YoY',
        direction: 'down',
        color: '#188038',
      });
    });
  });

  describe('Entity totals derived from /countries/{year} and /firs/{year}', () => {
    const okJson = (body: unknown) =>
      ({ok: true, json: async () => body}) as unknown as Response;

    it('has no country total until /countries/{year} has loaded', () => {
      expect(getTotalCountryCount(2025)).toBeNull();
    });

    it('derives the country total from the loaded /countries/{year} list', async () => {
      spyOn(globalThis, 'fetch').and.resolveTo(
        okJson([
          {name: 'France', 'iso_a2': 'FR', 'co2e_gwp50_tonnes_rank': 1},
          // A rank beyond the row count means some ranked rows are absent.
          {name: 'Spain', 'iso_a2': 'ES', 'co2e_gwp50_tonnes_rank': 7},
        ]),
      );
      await fetchCountriesFromBackend(2025);
      expect(getTotalCountryCount(2025)).toBe(7);
      expect(getTotalCountryCount(2024)).toBeNull();
    });

    it('keeps the country total per year', async () => {
      const fetchSpy = spyOn(globalThis, 'fetch');
      fetchSpy.and.resolveTo(okJson([{name: 'France', 'iso_a2': 'FR'}]));
      await fetchCountryTotalFromBackend(2024);
      fetchSpy.and.resolveTo(
        okJson([
          {name: 'France', 'iso_a2': 'FR'},
          {name: 'Spain', 'iso_a2': 'ES'},
        ]),
      );
      await fetchCountryTotalFromBackend(2025);
      expect(getTotalCountryCount(2024)).toBe(1);
      expect(getTotalCountryCount(2025)).toBe(2);
    });

    it('fetchCountryTotalFromBackend requests /countries/{year} once per year', async () => {
      const fetchSpy = spyOn(globalThis, 'fetch').and.resolveTo(
        okJson([
          {name: 'France', 'iso_a2': 'FR', 'mean_rf_mw_per_sq_m_rank': 3},
        ]),
      );
      expect(await fetchCountryTotalFromBackend(2025)).toBe(3);
      expect(await fetchCountryTotalFromBackend(2025)).toBe(3);
      const urls = fetchSpy.calls.allArgs().map((args) => String(args[0]));
      expect(urls).toEqual(['/countries/2025']);
    });

    it('fetchCountryTotalFromBackend does not change the country picker list', async () => {
      spyOn(globalThis, 'fetch').and.resolveTo(
        okJson([{name: 'France', 'iso_a2': 'FR'}]),
      );
      await fetchCountryTotalFromBackend(2025);
      expect(getAllCountries()).toEqual([]);
      expect(getCountryListStatus()).toBe('loading');
    });

    it('fetchCountryTotalFromBackend reuses the picker request in flight', async () => {
      let resolveFetch: (r: Response) => void = () => {};
      const fetchSpy = spyOn(globalThis, 'fetch').and.returnValue(
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
      );
      const picker = fetchCountriesFromBackend(2025);
      const total = fetchCountryTotalFromBackend(2025);
      resolveFetch(okJson([{name: 'France', 'iso_a2': 'FR'}]));
      await picker;
      expect(await total).toBe(1);
      const countryUrls = fetchSpy.calls
        .allArgs()
        .map((args) => String(args[0]))
        .filter((u) => u.startsWith('/countries'));
      expect(countryUrls).toEqual(['/countries/2025']);
      // The picker's own request still populates the picker.
      expect(getCountryListStatus()).toBe('ready');
    });

    it('returns null, not a made-up total, when /countries/{year} fails', async () => {
      spyOn(globalThis, 'fetch').and.rejectWith(new Error('Network error'));
      expect(await fetchCountryTotalFromBackend(2025)).toBeNull();
      expect(getTotalCountryCount(2025)).toBeNull();
    });

    it('has no FIR total until /firs/{year} has loaded, then derives it', async () => {
      expect(getTotalFirCount(2025)).toBeNull();
      spyOn(globalThis, 'fetch').and.resolveTo(
        okJson([
          {'fir_id': 'AAAA_ONE', 'co2e_gwp50_tonnes_rank': 1},
          {'fir_id': 'BBBB_TWO', 'mean_rf_mw_per_sq_m_rank': 5},
        ]),
      );
      await fetchFirsFromBackend(2025);
      expect(getTotalFirCount(2025)).toBe(5);
      expect(getTotalFirCount(2024)).toBeNull();
    });

    it('recomputes the FIR total when the FIR list is replaced', () => {
      setFirDataForTesting(2025, [{'fir_id': 'A'}, {'fir_id': 'B'}]);
      expect(getTotalFirCount(2025)).toBe(2);
      setFirDataForTesting(2025, [{'fir_id': 'A'}]);
      expect(getTotalFirCount(2025)).toBe(1);
      clearCountryDataCache();
      expect(getTotalFirCount(2025)).toBeNull();
    });

    it('returns a null FIR total when /firs/{year} fails', async () => {
      spyOn(globalThis, 'fetch').and.rejectWith(new Error('Network error'));
      await fetchFirsFromBackend(2025);
      expect(getTotalFirCount(2025)).toBeNull();
    });

    it('rank labels show the total once /countries/{year} has loaded', async () => {
      spyOn(globalThis, 'fetch').and.resolveTo(
        okJson([
          {
            name: 'Switzerland',
            'iso_a2': 'CH',
            'mean_rf_mw_per_sq_m_rank': 5,
            'co2e_gwp50_tonnes_rank': 12,
          },
          {name: 'France', 'iso_a2': 'FR', 'mean_rf_mw_per_sq_m_rank': 180},
        ]),
      );
      await fetchCountriesFromBackend(2025);
      const metrics = getCountryMetrics('CH', 2025, 'GWP50');
      expect(metrics.totalCountryCount).toBe(180);
      expect(metrics.impactDensityRank).toBe('#5 / 180 countries');
      expect(metrics.aboveCountryHero.absoluteRank).toBe('#12 / 180 countries');
      expect(extractRankInfo(metrics.impactDensityRank)).toEqual({
        rankNum: 5,
        totalCount: 180,
      });
    });

    it('loading country data has unknown totals', () => {
      const loading = getCountryMetrics('CH', 2025, 'GWP50');
      expect(loading.isLoading).toBeTrue();
      expect(loading.totalCountryCount).toBeNull();
      expect(loading.totalFirCount).toBeNull();
    });

    it('never requests the removed meta endpoints', async () => {
      const fetchSpy = spyOn(globalThis, 'fetch').and.resolveTo(
        okJson([{name: 'France', 'iso_a2': 'FR', 'fir_id': 'A'}]),
      );
      setAvailableYearsForTesting([2024, 2025]);
      await fetchCountriesFromBackend(2025);
      await fetchCountryTotalFromBackend(2025);
      await fetchFirsFromBackend(2025);
      const urls = fetchSpy.calls.allArgs().map((args) => String(args[0]));
      expect(urls.length).toBeGreaterThan(0);
      expect(urls.some((u) => u.includes('_meta'))).toBeFalse();
    });
  });

  describe('year-scoped cache eviction', () => {
    const jsonResponse = (body: unknown) =>
      ({ok: true, status: 200, json: async () => body}) as unknown as Response;

    beforeEach(() => {
      clearCountryDataCache();
    });

    it('evicts the least recently used year beyond MAX_CACHED_YEARS', () => {
      expect(MAX_CACHED_YEARS).toBe(6);
      setFirDataForTesting(2015, [{'fir_id': 'FIR_2015'}]);
      setFirDataForTesting(2016, [{'fir_id': 'FIR_2016'}]);
      setFirDataForTesting(2017, [{'fir_id': 'FIR_2017'}]);
      setFirDataForTesting(2018, [{'fir_id': 'FIR_2018'}]);
      setFirDataForTesting(2019, [{'fir_id': 'FIR_2019'}]);
      setFirDataForTesting(2020, [{'fir_id': 'FIR_2020'}]);
      setFirDataForTesting(2021, [{'fir_id': 'FIR_2021'}]);

      expect(getCachedYearsForTesting()).toEqual([
        2016, 2017, 2018, 2019, 2020, 2021,
      ]);
      expect(getFirDataForTesting(2015)).toBeUndefined();
      expect(getTotalFirCount(2015)).toBeNull();
      expect(getFirDataForTesting(2016)).toEqual([{'fir_id': 'FIR_2016'}]);
    });

    it('keeps a year that was served from cache before the limit was hit', async () => {
      setFirDataForTesting(2015, [{'fir_id': 'STILL_ON_SCREEN'}]);
      setFirDataForTesting(2016, [{'fir_id': 'EVICTED'}]);
      const fetchSpy = spyOn(globalThis, 'fetch');

      await fetchFirsFromBackend(2015);
      setFirDataForTesting(2017, [{'fir_id': 'FIR_2017'}]);
      setFirDataForTesting(2018, [{'fir_id': 'FIR_2018'}]);
      setFirDataForTesting(2019, [{'fir_id': 'FIR_2019'}]);
      setFirDataForTesting(2020, [{'fir_id': 'FIR_2020'}]);
      setFirDataForTesting(2021, [{'fir_id': 'FIR_2021'}]);

      expect(fetchSpy).not.toHaveBeenCalled();
      expect(getFirDataForTesting(2015)).toEqual([
        {'fir_id': 'STILL_ON_SCREEN'},
      ]);
      expect(getFirDataForTesting(2016)).toBeUndefined();
    });

    it('evicts country records and ranked totals of the evicted year', async () => {
      spyOn(globalThis, 'fetch').and.callFake(async (input) =>
        jsonResponse([
          {
            'iso_a2': 'CH',
            'name': 'Switzerland',
            'mean_rf_mw_per_sq_m_rank': 1,
            'year': Number(String(input).split('/').pop()),
          },
        ]),
      );

      await fetchCountriesFromBackend(2015);
      await fetchCountriesFromBackend(2016);
      await fetchCountriesFromBackend(2017);
      await fetchCountriesFromBackend(2018);
      await fetchCountriesFromBackend(2019);
      await fetchCountriesFromBackend(2020);
      await fetchCountriesFromBackend(2021);

      expect(getCountryDataFromCache('CH_2015')).toBeUndefined();
      expect(getTotalCountryCount(2015)).toBeNull();
      expect(getCountryDataFromCache('CH_2016')?.['year']).toBe(2016);
      expect(getTotalCountryCount(2016)).toBe(1);
    });

    it('forgets the LRU order when the cache is cleared', () => {
      setFirDataForTesting(2024, [{'fir_id': 'A'}]);

      clearCountryDataCache();

      expect(getCachedYearsForTesting()).toEqual([]);
    });
  });

  describe('GWP horizon strict resolution (no silent GWP50 fallback)', () => {
    it('resolves FIR records for requested GWP horizon without falling back to GWP50', () => {
      setFirDataForTesting(2025, [
        {
          'fir_id': 'LSAS_SWITZERLAND',
          'fir_icao': 'LSAS',
          firname: 'SWITZERLAND',
          type: 'domestic',
          'fir_to_country_iso2s': ['CH'],
          'area_sq_km': 41285,
          'mean_rf_mw_per_sq_m': 1200.0,
          'co2e_gwp20_tonnes': 2_800_000,
          'co2e_gwp50_tonnes': 1_400_000,
          'co2e_gwp100_tonnes': 810_000,
        },
      ]);
      const records20 = getFirRecordsForCountry('CH', 2025, 'GWP20');
      const records50 = getFirRecordsForCountry('CH', 2025, 'GWP50');
      const records100 = getFirRecordsForCountry('CH', 2025, 'GWP100');

      expect(records20.length).toBeGreaterThan(0);
      expect(records50.length).toBeGreaterThan(0);
      expect(records100.length).toBeGreaterThan(0);

      const r20 = records20[0];
      const r50 = records50[0];
      const r100 = records100[0];

      expect(r20.co2eTonnes).toBeDefined();
      expect(r50.co2eTonnes).toBeDefined();
      expect(r100.co2eTonnes).toBeDefined();

      // GWP20 impact is higher than GWP50, which is higher than GWP100
      expect(r20.co2eTonnes ?? 0).toBeGreaterThan(r50.co2eTonnes ?? 0);
      expect(r50.co2eTonnes ?? 0).toBeGreaterThan(r100.co2eTonnes ?? 0);

      // co2eTonnes must match the specific GWP column, not GWP50
      expect(r20.co2eTonnes).toBe(2_800_000);
      expect(r50.co2eTonnes).toBe(1_400_000);
      expect(r100.co2eTonnes).toBe(810_000);
    });

    it('getCountryMetrics strictly resolves requested GWP and does not fall back to GWP50 when fields are absent', () => {
      const mockMissingGwp20: RawCountryRecord = {
        name: 'Testland',
        'iso_a2': 'TL',
        'mean_rf_mw_per_sq_m_rank': 1,
        'co2e_gwp50_tonnes_rank': 1,
        // A legacy energy-forcing rank must not stand in for a missing CO2e
        // rank.
        'prop_sum_ef_mj_rank': 1,
        'mean_rf_mw_per_sq_m': 10.0,
        // Only GWP50 provided
        'co2e_gwp50_tonnes': 500000,
        'co2e_gwp50_vs_national_ghg': 0.05,
        'co2e_gwp50_tonnes_per_capita': 0.1,
        'fir_total_co2e_gwp50_tonnes': 500000,
        'fir_total_co2e_gwp50_tonnes_rank': 1,
        // GWP20 and GWP100 explicitly absent
      };

      setCountryDataInCache('Testland_2025', mockMissingGwp20);
      setCountryDataInCache('TL_2025', mockMissingGwp20);

      // Querying GWP50 should return the 500 Kt figure
      const metrics50 = getCountryMetrics('Testland', 2025, 'GWP50');
      expect(metrics50.aboveCountryHero.absoluteValue).toBe('500.0 Kt CO₂e');
      expect(metrics50.aboveCountryHero.nationalGhgComparison).toBe('5.00%');
      expect(metrics50.firTotalHero.absoluteValue).toBe('500.0 Kt CO₂e');

      // Querying GWP20 MUST NOT fall back to GWP50
      const metrics20 = getCountryMetrics('Testland', 2025, 'GWP20');
      expect(metrics20.aboveCountryHero.absoluteValue).toBe('0.00 t CO₂e');
      expect(metrics20.aboveCountryHero.nationalGhgComparison).toBe('N/A');
      expect(metrics20.aboveCountryHero.perCapitaValue).toBe('N/A');
      expect(metrics20.firTotalHero.absoluteValue).toBe('0.00 t CO₂e');
      expect(metrics20.firTotalHero.absoluteRank).toBe('N/A');
      // No CO2e rank for this horizon: no rank, and no EF-rank fallback.
      expect(metrics20.aboveCountryHero.absoluteRank).toBeUndefined();
      expect(metrics50.aboveCountryHero.absoluteRank).toBe('#1');

      // Querying GWP100 MUST NOT fall back to GWP50
      const metrics100 = getCountryMetrics('Testland', 2025, 'GWP100');
      expect(metrics100.aboveCountryHero.absoluteValue).toBe('0.00 t CO₂e');
      expect(metrics100.aboveCountryHero.nationalGhgComparison).toBe('N/A');
      expect(metrics100.firTotalHero.absoluteValue).toBe('0.00 t CO₂e');
    });

    it('getCountryMetrics correctly reflects distinct GWP20, GWP50, and GWP100 values when present', () => {
      const mockMultiGwp: RawCountryRecord = {
        name: 'GwpCountry',
        'iso_a2': 'GC',
        'mean_rf_mw_per_sq_m_rank': 2,
        'co2e_gwp20_tonnes_rank': 3,
        'co2e_gwp50_tonnes_rank': 3,
        'co2e_gwp100_tonnes_rank': 3,
        'mean_rf_mw_per_sq_m': 20.0,
        'co2e_gwp20_tonnes': 2100000,
        'co2e_gwp50_tonnes': 1000000,
        'co2e_gwp100_tonnes': 580000,
        'co2e_gwp20_vs_national_ghg': 0.21,
        'co2e_gwp50_vs_national_ghg': 0.1,
        'co2e_gwp100_vs_national_ghg': 0.058,
        'co2e_gwp20_tonnes_per_capita': 0.42,
        'co2e_gwp50_tonnes_per_capita': 0.2,
        'co2e_gwp100_tonnes_per_capita': 0.116,
        'fir_total_co2e_gwp20_tonnes': 2500000,
        'fir_total_co2e_gwp50_tonnes': 1200000,
        'fir_total_co2e_gwp100_tonnes': 700000,
      };

      setCountryDataInCache('GwpCountry_2025', mockMultiGwp);
      setCountryDataInCache('GC_2025', mockMultiGwp);

      const m20 = getCountryMetrics('GwpCountry', 2025, 'GWP20');
      const m50 = getCountryMetrics('GwpCountry', 2025, 'GWP50');
      const m100 = getCountryMetrics('GwpCountry', 2025, 'GWP100');

      expect(m20.aboveCountryHero.absoluteValue).toBe('2.1 Mt CO₂e');
      expect(m50.aboveCountryHero.absoluteValue).toBe('1.0 Mt CO₂e');
      expect(m100.aboveCountryHero.absoluteValue).toBe('580.0 Kt CO₂e');

      expect(m20.aboveCountryHero.nationalGhgComparison).toBe('21.00%');
      expect(m50.aboveCountryHero.nationalGhgComparison).toBe('10.00%');
      expect(m100.aboveCountryHero.nationalGhgComparison).toBe('5.80%');

      expect(m20.aboveCountryHero.perCapitaValue).toBe('0.42 t CO₂e');
      expect(m50.aboveCountryHero.perCapitaValue).toBe('0.20 t CO₂e');
      expect(m100.aboveCountryHero.perCapitaValue).toBe('0.12 t CO₂e');

      expect(m20.firTotalHero.absoluteValue).toBe('2.5 Mt CO₂e');
      expect(m50.firTotalHero.absoluteValue).toBe('1.2 Mt CO₂e');
      expect(m100.firTotalHero.absoluteValue).toBe('700.0 Kt CO₂e');
    });
  });

  describe('Domain Math and Edge Cases', () => {
    it('bounds FIR percentOfFirTotal to [0%, 100%] and preserves hasInternationalFir when a FIR is net-cooling', () => {
      setFirDataForTesting(2025, [
        {
          fir_id: 'FIR_DOM_WARMING',
          fir_icao: 'FIRD',
          firname: 'DOMESTIC WARMING',
          type: 'domestic',
          fir_to_country_iso2s: ['NO'],
          co2e_gwp50_tonnes: 100_000,
        },
        {
          fir_id: 'FIR_INT_COOLING',
          fir_icao: 'FIRI',
          firname: 'OCEANIC COOLING',
          type: 'international',
          fir_to_country_iso2s: ['NO'],
          co2e_gwp50_tonnes: -20_000,
        },
      ]);
      setCountryDataInCache('NO_2025', {
        name: 'Norway',
        iso_a2: 'NO',
        fir_dom_co2e_gwp50_tonnes: 100_000,
        fir_int_co2e_gwp50_tonnes: -20_000,
        fir_total_co2e_gwp50_tonnes: 80_000,
      });

      const firs = getFirRecordsForCountry('NO', 2025, 'GWP50');
      expect(firs.length).toBe(2);
      expect(firs[0].percentOfFirTotal).toBe('100%');
      expect(firs[1].percentOfFirTotal).toBe('0%');

      const metrics = getCountryMetrics('NO', 2025, 'GWP50');
      expect(metrics.hasInternationalFir).toBeTrue();
    });

    it('clamps overflightFraction to [0, 100] when in_country_impact is negative or > 1', () => {
      setCountryDataInCache('CH_2025', {
        name: 'Switzerland',
        'iso_a2': 'CH',
        'in_country_impact': -0.2,
      });
      expect(getCountryMetrics('CH', 2025).overflightFraction).toBe('100.0%');

      setCountryDataInCache('CH_2025', {
        name: 'Switzerland',
        'iso_a2': 'CH',
        'in_country_impact': 1.5,
      });
      expect(getCountryMetrics('CH', 2025).overflightFraction).toBe('0.0%');

      setCountryDataInCache('CH_2025', {
        name: 'Switzerland',
        'iso_a2': 'CH',
        'in_country_impact': null,
      });
      expect(getCountryMetrics('CH', 2025).overflightFraction).toBe(
        NOT_AVAILABLE_LABEL,
      );

      setCountryDataInCache('CH_2025', {
        name: 'Switzerland',
        'iso_a2': 'CH',
      });
      expect(getCountryMetrics('CH', 2025).overflightFraction).toBe(
        NOT_AVAILABLE_LABEL,
      );
    });

    it('notifies subscribers via subscribeToCountryCache when cache updates', () => {
      let notifications = 0;
      const unsubscribe = subscribeToCountryCache(() => {
        notifications++;
      });
      setCountryDataInCache('CH_2025', {name: 'Switzerland', 'iso_a2': 'CH'});
      expect(notifications).toBe(1);
      unsubscribe();
      setCountryDataInCache('DE_2025', {name: 'Germany', 'iso_a2': 'DE'});
      expect(notifications).toBe(1);
    });

    it('empties the country list and returns to loading on clearCountryDataCache', () => {
      setAllCountriesForTesting([{name: 'Switzerland', isoA2: 'CH'}]);
      clearCountryDataCache();
      expect(getAllCountries().length).toBe(0);
      expect(getCountryListStatus()).toBe('loading');
    });

    it('returns null from both backend fetchers when the backend is unreachable', async () => {
      spyOn(globalThis, 'fetch').and.rejectWith(new Error('Offline'));
      setCountryDataInCache('CH_2025', {
        name: 'Switzerland',
        'iso_a2': 'CH',
        'co2e_gwp50_tonnes': 4_200,
        'co2e_gwp50_tonnes_rank': 15,
        'mean_rf_mw_per_sq_m': 1150.5,
      });

      // Locally cached country records must not be reassembled into ranking or
      // series shapes. The backend owns those endpoints.
      const co2eRankings = await fetchRankingsFromBackend({
        metric: 'co2e_gwp_tonnes',
        year: 2025,
        gwp: 'GWP50',
      });
      expect(co2eRankings).toBeNull();

      const rfSeries = await fetchTimeSeriesFromBackend('mean_rf_mw_per_sq_m', {
        iso: 'CH',
      });
      expect(rfSeries).toBeNull();
    });

    it('populates numeric rank fields in getCountryMetrics', () => {
      setCountryDataInCache('CH_2025', {
        name: 'Switzerland',
        'iso_a2': 'CH',
        'co2e_gwp50_tonnes': 2_000_000,
        'co2e_gwp50_tonnes_rank': 12,
        'co2e_gwp50_tonnes_per_capita': 0.22,
        'co2e_gwp50_tonnes_per_capita_rank': 18,
        'co2e_gwp50_vs_national_ghg': 0.045,
        'co2e_gwp50_vs_national_ghg_rank': 9,
        'outgoing_flight_co2e_gwp50_tonnes': 1_500_000,
        'outgoing_flight_co2e_gwp50_tonnes_rank': 14,
        'outgoing_flight_co2e_gwp50_tonnes_per_capita': 0.17,
        'outgoing_flight_co2e_gwp50_tonnes_per_capita_rank': 21,
        'outgoing_flight_co2e_gwp50_vs_national_ghg': 0.034,
        'outgoing_flight_co2e_gwp50_vs_national_ghg_rank': 11,
        'fir_total_co2e_gwp50_tonnes': 2_200_000,
        'fir_total_co2e_gwp50_tonnes_rank': 10,
        'fir_total_co2e_gwp50_tonnes_per_capita': 0.25,
        'fir_total_co2e_gwp50_tonnes_per_capita_rank': 16,
        'fir_total_co2e_gwp50_vs_national_ghg': 0.05,
        'fir_total_co2e_gwp50_vs_national_ghg_rank': 8,
      });
      const metrics = getCountryMetrics('CH', 2025, 'GWP50');
      expect(metrics.aboveCountryHero.absoluteRankNum).toBe(12);
      expect(metrics.aboveCountryHero.perCapitaRankNum).toBe(18);
      expect(metrics.aboveCountryHero.nationalGhgRankNum).toBe(9);
      expect(metrics.outgoingHero.absoluteRankNum).toBe(14);
      expect(metrics.outgoingHero.perCapitaRankNum).toBe(21);
      expect(metrics.outgoingHero.nationalGhgRankNum).toBe(11);
      expect(metrics.firTotalHero.absoluteRankNum).toBe(10);
      expect(metrics.firTotalHero.perCapitaRankNum).toBe(16);
      expect(metrics.firTotalHero.nationalGhgRankNum).toBe(8);
      expect(
        (metrics as unknown as Record<string, unknown>)['totalCountries'],
      ).toBeUndefined();
    });

    it('fetchFirsFromBackend loads and indexes FIR records from /firs/{year}', async () => {
      spyOn(globalThis, 'fetch').and.resolveTo({
        ok: true,
        json: async () => [
          {
            'fir_id': 'LSAS_SWITZERLAND',
            'fir_icao': 'LSAS',
            firname: 'SWITZERLAND',
            type: 'domestic',
            'fir_to_country_iso2s': ['CH'],
            'area_sq_km': 41285,
            'mean_rf_mw_per_sq_m': 1250.0,
            'co2e_gwp50_tonnes': 1_500_000,
            'co2e_gwp50_tonnes_rank': 4,
            'co2e_gwp100_tonnes': 860_000,
            'co2e_gwp100_tonnes_rank': 5,
            // Legacy EF rank: must be ignored in favour of the CO2e rank.
            'prop_sum_ef_mj_rank': 99,
          },
        ],
      } as unknown as Response);

      const loaded = await fetchFirsFromBackend(2025);
      expect(loaded.length).toBe(1);
      const chFirs = getFirRecordsForCountry('CH', 2025, 'GWP50');
      expect(chFirs.length).toBe(1);
      expect(chFirs[0].firId).toBe('LSAS_SWITZERLAND');
      expect(chFirs[0].firIcao).toBe('LSAS');
      expect(chFirs[0].co2eTonnes).toBe(1_500_000);
      // The FIR rank follows the selected GWP horizon's CO2e rank column.
      expect(chFirs[0].co2eRank).toBe(4);
      const chFirs100 = getFirRecordsForCountry('CH', 2025, 'GWP100');
      expect(chFirs100[0].co2eRank).toBe(5);
      // A horizon without a rank column yields no rank rather than a fallback.
      const chFirs20 = getFirRecordsForCountry('CH', 2025, 'GWP20');
      expect(chFirs20[0].co2eRank).toBeUndefined();
    });

    it('does not cache backend failures and recovers once the backend returns', async () => {
      let shouldFail = true;
      spyOn(globalThis, 'fetch').and.callFake(async () => {
        if (shouldFail) {
          throw new Error('Temporary 503');
        }
        return {
          ok: true,
          json: async () => ({
            metric: 'fir_total_co2e_gwp_tonnes',
            'metric_title': 'Total CO₂e (All FIRs)',
            unit: UNIT_TONNES_CO2E,
            'entity_name': 'Switzerland',
            'iso_a2': 'CH',
            'fir_icao': null,
            points: [{'year': 2025, value: 99_000_000}],
          }),
        } as unknown as Response;
      });

      const failedSeries = await fetchTimeSeriesFromBackend(
        'fir_total_co2e_gwp_tonnes',
        {
          iso: 'CH',
        },
      );
      expect(failedSeries).toBeNull();

      // The failed attempt must not be cached, so the next call retries the
      // backend.
      shouldFail = false;
      const recoveredSeries = await fetchTimeSeriesFromBackend(
        'fir_total_co2e_gwp_tonnes',
        {
          iso: 'CH',
        },
      );
      expect(recoveredSeries?.points.find((p) => p.year === 2025)?.value).toBe(
        99_000_000,
      );
    });

    it('matches FIR YoY records strictly by fir_id and bounds Map entries via setBoundedCacheEntry', () => {
      // YoY needs the backend to list the previous year.
      setAvailableYearsForTesting([2024, 2025]);
      setFirDataForTesting(2024, [
        {
          'fir_id': 'KZAK_OAKLAND_OCEANIC_WEST',
          'fir_icao': 'KZAK',
          firname: 'OAKLAND OCEANIC WEST',
          type: 'international',
          'fir_to_country_iso2s': ['US'],
          'area_sq_km': 1000000,
          'mean_rf_mw_per_sq_m': 100.0,
          'co2e_gwp50_tonnes': 1_000_000,
          'co2e_gwp50_tonnes_rank': 5,
        },
        {
          'fir_id': 'KZAK_OAKLAND_OCEANIC_EAST',
          'fir_icao': 'KZAK',
          firname: 'OAKLAND OCEANIC EAST',
          type: 'international',
          'fir_to_country_iso2s': ['US'],
          'area_sq_km': 2000000,
          'mean_rf_mw_per_sq_m': 200.0,
          'co2e_gwp50_tonnes': 2_000_000,
          'co2e_gwp50_tonnes_rank': 2,
        },
      ]);
      setFirDataForTesting(2025, [
        {
          'fir_id': 'KZAK_OAKLAND_OCEANIC_WEST',
          'fir_icao': 'KZAK',
          firname: 'OAKLAND OCEANIC WEST',
          type: 'international',
          'fir_to_country_iso2s': ['US'],
          'area_sq_km': 1000000,
          'mean_rf_mw_per_sq_m': 150.0,
          'co2e_gwp50_tonnes': 1_200_000,
          'co2e_gwp50_tonnes_rank': 4,
        },
      ]);

      const usFirs = getFirRecordsForCountry('US', 2025, 'GWP50');
      expect(usFirs.length).toBe(1);
      expect(usFirs[0].firId).toBe('KZAK_OAKLAND_OCEANIC_WEST');
      expect(usFirs[0].absoluteYoY?.text).toBe('▲ +20.0% YoY');

      const map = new Map<string, number>();
      setBoundedCacheEntry(map, 'a', 1, 2);
      setBoundedCacheEntry(map, 'b', 2, 2);
      setBoundedCacheEntry(map, 'c', 3, 2);
      expect(map.size).toBe(2);
      expect(map.has('a')).toBeFalse();
      expect(map.get('b')).toBe(2);
      expect(map.get('c')).toBe(3);
    });

    it('fetches available years from /years and notifies cache listeners', async () => {
      spyOn(globalThis, 'fetch').and.resolveTo({
        ok: true,
        json: async () => ({years: [2024, 2025]}),
      } as unknown as Response);
      let notified = 0;
      const unsubscribe = subscribeToCountryCache(() => {
        notified++;
      });

      const years = await fetchAvailableYearsFromBackend();
      unsubscribe();
      expect(notified).toBeGreaterThan(0);
      expect(years).toEqual([2024, 2025]);
      expect(getAvailableYears()).toEqual([2024, 2025]);
      expect(getLatestYear()).toBe(2025);

      setAvailableYearsForTesting([2025]);
      expect(getAvailableYears()).toEqual([2025]);
      expect(getLatestYear()).toBe(2025);
    });

    it('suppresses legacy positive ranks in getCountryMetrics when underlying metric values are null', () => {
      const legacyNullRecord: RawCountryRecord = {
        name: 'Kosovo',
        'iso_a2': '-99',
        population: null as unknown as number,
        'ghg_mtco2e': null as unknown as number,
        'mean_rf_mw_per_sq_m': 12.0,
        'mean_rf_mw_per_sq_m_rank': 8,
        'co2e_gwp50_tonnes': 100_000,
        'co2e_gwp50_tonnes_rank': 9,
        'co2e_gwp50_tonnes_per_capita': null as unknown as number,
        'co2e_gwp50_tonnes_per_capita_rank': 10,
        'co2e_gwp50_vs_national_ghg': null as unknown as number,
        'co2e_gwp50_vs_national_ghg_rank': 10,
        'outgoing_flight_co2e_gwp50_tonnes': 0,
        'outgoing_flight_co2e_gwp50_tonnes_rank': 10,
        'outgoing_flight_co2e_gwp50_tonnes_per_capita':
          null as unknown as number,
        'outgoing_flight_co2e_gwp50_tonnes_per_capita_rank': 10,
        'outgoing_flight_co2e_gwp50_vs_national_ghg': null as unknown as number,
        'outgoing_flight_co2e_gwp50_vs_national_ghg_rank': 10,
        'fir_total_mean_rf_mw_per_sq_m': null as unknown as number,
        'fir_total_mean_rf_mw_per_sq_m_rank': 10,
        'fir_total_impact_density_sq_km': 0,
        'fir_total_co2e_gwp50_tonnes_per_capita': null as unknown as number,
        'fir_total_co2e_gwp50_tonnes_per_capita_rank': 10,
        'fir_total_co2e_gwp50_vs_national_ghg': null as unknown as number,
        'fir_total_co2e_gwp50_vs_national_ghg_rank': 10,
      };
      setCountryDataInCache('Kosovo_2025', legacyNullRecord);

      const metrics = getCountryMetrics('Kosovo', 2025, 'GWP50');
      // Finite territorial metrics keep their ranks
      expect(metrics.densityRankNum).toBe(8);
      expect(metrics.aboveCountryHero.absoluteRankNum).toBe(9);
      expect(metrics.outgoingHero.absoluteRankNum).toBe(10);

      // Missing/null metrics must not surface legacy positive ranks
      expect(metrics.aboveCountryHero.perCapitaRankNum).toBeUndefined();
      expect(metrics.aboveCountryHero.perCapitaRank).toBeUndefined();
      expect(metrics.aboveCountryHero.nationalGhgRankNum).toBeUndefined();
      expect(metrics.aboveCountryHero.nationalGhgRank).toBeUndefined();
      expect(metrics.outgoingHero.perCapitaRankNum).toBeUndefined();
      expect(metrics.outgoingHero.perCapitaRank).toBeUndefined();
      expect(metrics.outgoingHero.nationalGhgRankNum).toBeUndefined();
      expect(metrics.outgoingHero.nationalGhgRank).toBeUndefined();
      expect(metrics.firTotalHero.absoluteRankNum).toBeUndefined();
      expect(metrics.firTotalHero.absoluteRank).toBe('N/A');
      expect(metrics.firDensityRankNum).toBeUndefined();
      expect(metrics.firDensityRank).toBeUndefined();
      expect(metrics.firTotalHero.perCapitaRankNum).toBeUndefined();
      expect(metrics.firTotalHero.perCapitaRank).toBeUndefined();
      expect(metrics.firTotalHero.nationalGhgRankNum).toBeUndefined();
      expect(metrics.firTotalHero.nationalGhgRank).toBeUndefined();
    });

    it('shows N/A for per-area metrics of a country and FIRs without area', () => {
      // The pipeline publishes null (not 0.0) per-area values and ranks when
      // the area is 0, e.g. a country missing from the boundary file.
      setCountryDataInCache('SG_2025', {
        name: 'Singapore',
        'iso_a2': 'SG',
        'area_sq_km': 0,
        'mean_rf_mw_per_sq_m': null,
        'impact_density_sq_km': null,
        'mean_rf_mw_per_sq_m_rank': null as unknown as number,
        'co2e_gwp50_tonnes': 1_000,
        'co2e_gwp50_tonnes_rank': 3,
        'co2e_gwp50_tonnes_sq_km': null,
        'fir_total_mean_rf_mw_per_sq_m': 4.5,
        'fir_total_impact_density_sq_km': 1.0,
        'fir_dom_mean_rf_mw_per_sq_m': 4.5,
        'fir_dom_impact_density_sq_km': 1.0,
        'fir_int_mean_rf_mw_per_sq_m': null,
        'fir_int_impact_density_sq_km': null,
      });
      setFirDataForTesting(2025, [
        {
          'fir_id': 'NOAREA',
          'fir_icao': 'NOAR',
          firname: 'NO AREA',
          'fir_to_country_iso2s': ['SG'],
          type: 'international',
          'area_sq_km': 0,
          'co2e_gwp50_tonnes': 500,
          'co2e_gwp50_tonnes_rank': 1,
          'mean_rf_mw_per_sq_m': null,
          'impact_density_sq_km': null,
          'mean_rf_mw_per_sq_m_rank': null as unknown as number,
        },
      ]);

      const metrics = getCountryMetrics('SG', 2025, 'GWP50');
      expect(metrics.impactDensity).toBe('N/A');
      expect(metrics.densityRankNum).toBeUndefined();
      expect(metrics.impactDensityRank).toBe('N/A');
      // Area-independent metrics are unaffected.
      expect(metrics.aboveCountryHero.absoluteRankNum).toBe(3);

      const [fir] = getFirRecordsForCountry('SG', 2025, 'GWP50');
      expect(fir.meanRfMwPerSqM).toBeUndefined();
      expect(fir.impactDensityRank).toBeUndefined();
      expect(formatMwPerSqM(fir.meanRfMwPerSqM)).toBe('N/A');
      expect(fir.co2eRank).toBe(1);
    });

    it('shows N/A for per-capita and vs-national-GHG metrics the pipeline leaves null', () => {
      // Shape published for a country with no population, national GHG or
      // area: the pipeline emits null ratios rather than a guessed value.
      setCountryDataInCache('ZZ_2025', {
        name: 'Nowhere',
        'iso_a2': 'ZZ',
        population: 0,
        'ghg_mtco2e': 0,
        'total_flight_impact': 10,
        'outgoing_flight_impact': 5,
        'co2e_gwp50_tonnes': 1_000,
        'co2e_gwp50_tonnes_per_capita': null,
        'co2e_gwp50_vs_national_ghg': null,
        'outgoing_flight_co2e_gwp50_tonnes': 400,
        'outgoing_flight_co2e_gwp50_tonnes_per_capita': null,
        'outgoing_flight_co2e_gwp50_vs_national_ghg': null,
        'fir_total_co2e_gwp50_tonnes': 2_000,
        'fir_total_co2e_gwp50_tonnes_per_capita': null,
        'fir_total_co2e_gwp50_vs_national_ghg': null,
      });

      const metrics = getCountryMetrics('ZZ', 2025, 'GWP50');
      expect(metrics.aboveCountryHero.absoluteValue).toBe('1.0 Kt CO₂e');
      expect(metrics.aboveCountryHero.perCapitaValue).toBe('N/A');
      expect(metrics.aboveCountryHero.nationalGhgComparison).toBe('N/A');
      expect(metrics.outgoingHero.absoluteValue).toBe('400.0 t CO₂e');
      expect(metrics.outgoingHero.perCapitaValue).toBe('N/A');
      expect(metrics.outgoingHero.nationalGhgComparison).toBe('N/A');
      expect(metrics.firTotalHero.absoluteValue).toBe('2.0 Kt CO₂e');
      expect(metrics.firTotalHero.perCapitaValue).toBe('N/A');
      expect(metrics.firTotalHero.nationalGhgComparison).toBe('N/A');
    });

    it('shows N/A for vs-national-GHG when only the population is known', () => {
      // A country with population and national GHG but no boundary area has
      // a null country-level ratio, even though a client could compute one.
      setCountryDataInCache('MC_2025', {
        name: 'Monaco',
        'iso_a2': 'MC',
        population: 38_000,
        'ghg_mtco2e': 0.1,
        'co2e_gwp50_tonnes': 0,
        'co2e_gwp50_tonnes_per_capita': null,
        'co2e_gwp50_vs_national_ghg': null,
        'outgoing_flight_co2e_gwp50_tonnes': 1_000,
        'outgoing_flight_co2e_gwp50_tonnes_per_capita': 1_000 / 38_000,
        'outgoing_flight_co2e_gwp50_vs_national_ghg': 0.01,
      });

      const metrics = getCountryMetrics('MC', 2025, 'GWP50');
      expect(metrics.aboveCountryHero.nationalGhgComparison).toBe('N/A');
      expect(metrics.aboveCountryHero.perCapitaValue).toBe('N/A');
      expect(metrics.outgoingHero.perCapitaValue).toBe('0.03 t CO₂e');
      expect(metrics.outgoingHero.nationalGhgComparison).toBe('1.00%');
    });

    it('reads the FIR per-capita value published for the selected GWP', () => {
      setCountryDataInCache('NL_2025', {
        name: 'Netherlands',
        'iso_a2': 'NL',
        population: 10_000_000,
        'fir_total_co2e_gwp20_tonnes': 4_000_000,
        'fir_total_co2e_gwp50_tonnes': 2_000_000,
        // Deliberately not tonnes / population, to prove it is read as-is.
        'fir_total_co2e_gwp20_tonnes_per_capita': 0.5,
        'fir_total_co2e_gwp50_tonnes_per_capita': 0.25,
        'fir_total_co2e_gwp50_vs_national_ghg': 0.04,
      });

      expect(
        getCountryMetrics('NL', 2025, 'GWP50').firTotalHero.perCapitaValue,
      ).toBe('0.25 t CO₂e');
      expect(
        getCountryMetrics('NL', 2025, 'GWP20').firTotalHero.perCapitaValue,
      ).toBe('0.50 t CO₂e');
      expect(
        getCountryMetrics('NL', 2025, 'GWP50').firTotalHero
          .nationalGhgComparison,
      ).toBe('4.00%');
    });

    it('does not derive outgoing CO2e or mean RF from other fields', () => {
      setCountryDataInCache('XX_2025', {
        name: 'Derivedland',
        'iso_a2': 'XX',
        'co2e_gwp50_tonnes': 1_000_000,
        'total_flight_impact': 10,
        'outgoing_flight_impact': 5,
        // Energy density alone is not converted into mean RF.
        'mean_rf_mw_per_sq_m': null,
        'impact_density_sq_km': 31_536,
      });

      const metrics = getCountryMetrics('XX', 2025, 'GWP50');
      expect(metrics.outgoingHero.absoluteValue).toBe('N/A');
      expect(metrics.impactDensity).toBe('N/A');
    });
  });

  describe('Natural Earth world-map features', () => {
    const feature = (name: string, isoA2: string): GeoFeatureLike => ({
      'type': 'Feature',
      'properties': {
        'name': name,
        'iso_a2': isoA2,
        'iso_a3': isoA2 === '-99' ? '-99' : `${isoA2}X`,
      },
      'geometry': null,
    });
    const kosovo = feature('Kosovo', '-99');
    const northCyprus = feature('N. Cyprus', '-99');
    const northMacedonia = feature('North Macedonia', 'MK');
    const cyprus = feature('Cyprus', 'CY');
    const germany = feature('Germany', 'DE');
    const features = [kosovo, northCyprus, northMacedonia, cyprus, germany];

    it('resolves sentinel "-99" territories by name, not via name aliases', () => {
      expect(resolveCountryIsoFromFeature(kosovo)).toBeNull();
      expect(resolveCountryNameFromFeature(kosovo)).toBe('Kosovo');
      expect(resolveCountrySelectionFromFeature(kosovo)).toBe('Kosovo');
      expect(resolveCountrySelectionFromFeature(northCyprus)).toBe('N. Cyprus');
      expect(resolveCountrySelectionFromFeature(germany)).toBe('DE');
    });

    it('finds the features for ISO codes, names, and sentinel names', () => {
      expect(findCountryFeaturesForSelection(features, 'de')).toEqual([
        germany,
      ]);
      expect(findCountryFeaturesForSelection(features, 'Germany')).toEqual([
        germany,
      ]);
      expect(findCountryFeaturesForSelection(features, 'Kosovo')).toEqual([
        kosovo,
      ]);
      expect(findCountryFeaturesForSelection(features, 'N. Cyprus')).toEqual([
        northCyprus,
      ]);
      expect(findCountryFeaturesForSelection(features, 'MK')).toEqual([
        northMacedonia,
      ]);
      expect(findCountryFeaturesForSelection(features, 'CY')).toEqual([cyprus]);
      expect(findCountryFeaturesForSelection(features, '')).toEqual([]);
    });

    it('validates ISO alpha-2 codes and resolves canonical country identifiers', () => {
      expect(isValidCountryIso2('CH')).toBeTrue();
      expect(isValidCountryIso2('de')).toBeTrue();
      expect(isValidCountryIso2('-99')).toBeFalse();
      expect(isValidCountryIso2('')).toBeFalse();
      expect(isValidCountryIso2(null)).toBeFalse();

      expect(resolveCountryIdentifier('ch', 'Switzerland')).toBe('CH');
      expect(resolveCountryIdentifier('-99', 'Kosovo')).toBe('Kosovo');
      expect(resolveCountryIdentifier('-99', 'Northern Cyprus')).toBe(
        'Northern Cyprus',
      );
      expect(resolveCountryIdentifier('', 'Somaliland')).toBe('Somaliland');
    });

    it('caches multiple "-99" territories by name without colliding on "-99"', async () => {
      spyOn(globalThis, 'fetch').and.resolveTo({
        ok: true,
        status: 200,
        json: async () => [
          {
            name: 'Kosovo',
            'iso_a2': '-99',
            'mean_rf_mw_per_sq_m_rank': 90,
            'co2e_gwp50_tonnes': 10_000,
          },
          {
            name: 'Somaliland',
            'iso_a2': '-99',
            'mean_rf_mw_per_sq_m_rank': 130,
            'co2e_gwp50_tonnes': 2_000,
          },
        ],
      } as unknown as Response);

      await fetchCountriesFromBackend(2025);

      expect(getCountryRecord('Kosovo', 2025)?.name).toBe('Kosovo');
      expect(getCountryRecord('KOSOVO', 2025)?.name).toBe('Kosovo');
      expect(getCountryRecord('Somaliland', 2025)?.name).toBe('Somaliland');
      expect(getCountryRecord('-99', 2025)).toBeNull();
      expect(getCountryNameFromIso2('-99')).toBe('-99');
      expect(getCountryNameFromIso2('Kosovo')).toBe('Kosovo');
    });

    it('skips /country/{year}/{iso} requests for empty, "-99", and non-ISO territory names', async () => {
      const fetchSpy = spyOn(globalThis, 'fetch');

      expect(await fetchCountryRecordFromBackend('', 2025)).toBeNull();
      expect(await fetchCountryRecordFromBackend('-99', 2025)).toBeNull();
      expect(await fetchCountryRecordFromBackend('Kosovo', 2025)).toBeNull();

      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('piggybacks on in-flight /countries/{year} batch request instead of firing a duplicate /country request', async () => {
      setAllCountriesForTesting([
        {name: 'France', isoA2: 'FR'},
        {name: 'Kosovo', isoA2: '-99'},
      ]);
      expect(getIso2FromCountry('France')).toBe('FR');
      expect(getIso2FromCountry('Kosovo')).toBe('');

      const fetchSpy = spyOn(globalThis, 'fetch').and.callFake(async (url) => {
        expect(String(url)).toContain('/countries/2025');
        return {
          ok: true,
          status: 200,
          json: async () => [
            {
              name: 'France',
              'iso_a2': 'FR',
              'co2e_gwp50_tonnes': 500_000,
            },
            {
              name: 'Kosovo',
              'iso_a2': '-99',
              'co2e_gwp50_tonnes': 10_000,
            },
          ],
        } as unknown as Response;
      });

      const batchPromise = fetchCountryTotalFromBackend(2025);
      const [frRecord, kosovoRecord] = await Promise.all([
        fetchCountryRecordFromBackend('France', 2025),
        fetchCountryRecordFromBackend('Kosovo', 2025),
        batchPromise,
      ]);

      expect(frRecord?.name).toBe('France');
      expect(kosovoRecord?.name).toBe('Kosovo');
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    });

    it('recomputes FIR YoY indicators and managing country names when previous-year FIRs and country metadata arrive after an initial getFirRecordsForCountry call', () => {
      setAvailableYearsForTesting([2024, 2025]);
      setFirDataForTesting(2025, [
        {
          fir_id: 'LSAS_SWITZERLAND',
          fir_icao: 'LSAS',
          firname: 'SWITZERLAND FIR',
          type: 'domestic',
          fir_to_country_iso2s: ['CH'],
          co2e_gwp50_tonnes: 120_000,
          mean_rf_mw_per_sq_m: 12.0,
          area_sq_km: 41_285,
          segment_count: 1000,
        },
      ]);

      const beforePrevYear = getFirRecordsForCountry('CH', 2025, 'GWP50');
      expect(beforePrevYear.length).toBe(1);
      expect(beforePrevYear[0].absoluteYoY).toBeUndefined();
      expect(beforePrevYear[0].countries).toEqual([{isoA2: 'CH', name: 'CH'}]);

      // Populating FIR rows for 2024 without calling
      // setFirDataForTesting tests that memoKey itself distinguishes
      // hasPrevFirs ('0' vs '1') rather than relying solely on cache clears.
      setFirDataWithoutInvalidationForTesting(2024, [
        {
          fir_id: 'LSAS_SWITZERLAND',
          fir_icao: 'LSAS',
          firname: 'SWITZERLAND FIR',
          type: 'domestic',
          fir_to_country_iso2s: ['CH'],
          co2e_gwp50_tonnes: 100_000,
          mean_rf_mw_per_sq_m: 10.0,
          area_sq_km: 41_285,
          segment_count: 900,
        },
      ]);

      const afterPrevYear = getFirRecordsForCountry('CH', 2025, 'GWP50');
      expect(afterPrevYear[0].absoluteYoY).toEqual({
        text: '▲ +20.0% YoY',
        direction: 'up',
        color: '#d93025',
      });

      setAllCountriesForTesting([{name: 'Switzerland', isoA2: 'CH'}]);
      const afterCountriesLoaded = getFirRecordsForCountry('CH', 2025, 'GWP50');
      expect(afterCountriesLoaded[0].countries).toEqual([
        {isoA2: 'CH', name: 'Switzerland'},
      ]);

      // Also verify that a country with 0 FIRs in a loaded year returns the
      // memoized empty array reference on repeat calls.
      const zeroFirsFirst = getFirRecordsForCountry('LI', 2025, 'GWP50');
      const zeroFirsSecond = getFirRecordsForCountry('LI', 2025, 'GWP50');
      expect(zeroFirsFirst).toEqual([]);
      expect(zeroFirsSecond).toBe(zeroFirsFirst);
    });

    it('coalesces subscribeToCountryCache notifications across current and previous year country and FIR batch loads in fetchCountriesFromBackend', async () => {
      setAvailableYearsForTesting([2024, 2025]);
      spyOn(globalThis, 'fetch').and.callFake(
        async (input: RequestInfo | URL) => {
          const url = String(input);
          if (url.endsWith('/countries/2025')) {
            return {
              ok: true,
              status: 200,
              json: async () => [
                {
                  name: 'Switzerland',
                  'iso_a2': 'CH',
                  'mean_rf_mw_per_sq_m_rank': 1,
                },
              ],
            } as unknown as Response;
          }
          if (url.endsWith('/countries/2024')) {
            return {
              ok: true,
              status: 200,
              json: async () => [
                {
                  name: 'Switzerland',
                  'iso_a2': 'CH',
                  'mean_rf_mw_per_sq_m_rank': 2,
                },
              ],
            } as unknown as Response;
          }
          if (url.endsWith('/firs/2025') || url.endsWith('/firs/2024')) {
            return {
              ok: true,
              status: 200,
              json: async () => [
                {
                  'fir_id': 'LSAS_SWITZERLAND',
                  'fir_icao': 'LSAS',
                  firname: 'SWITZERLAND FIR',
                  type: 'domestic',
                  'fir_to_country_iso2s': ['CH'],
                },
              ],
            } as unknown as Response;
          }
          return {ok: false, status: 404} as unknown as Response;
        },
      );

      const listener = jasmine.createSpy<() => void>('cacheListener');
      const unsubscribe = subscribeToCountryCache(listener);
      try {
        const options = await fetchCountriesFromBackend(2025);
        expect(options.length).toBe(1);
        expect(options[0].isoA2).toBe('CH');
        expect(listener).toHaveBeenCalledTimes(1);
      } finally {
        unsubscribe();
      }
    });
  });
  describe('Countries without controlled FIRs', () => {
    beforeEach(() => {
      setAllCountriesForTesting([{name: 'Switzerland', isoA2: 'CH'}]);
      setFirDataForTesting(2025, [
        {
          'fir_id': 'LSAS_SWITZERLAND',
          'fir_icao': 'LSAS',
          firname: 'SWITZERLAND',
          type: 'domestic',
          'fir_to_country_iso2s': ['CH'],
          'area_sq_km': 41285,
          'mean_rf_mw_per_sq_m': 1200.0,
          'co2e_gwp50_tonnes': 1_400_000,
        },
      ]);
    });

    it('returns empty FIR array for countries without associated FIRs like Cameroon', () => {
      const cmRecords = getFirRecordsForCountry('Cameroon', 2025);
      expect(cmRecords).toEqual([]);

      const cmIsoRecords = getFirRecordsForCountry('CM', 2025);
      expect(cmIsoRecords).toEqual([]);
    });

    it('returns populated FIR records for countries with associated FIRs', () => {
      const swissRecords = getFirRecordsForCountry('Switzerland', 2025);
      expect(swissRecords).toEqual([
        jasmine.objectContaining({
          firId: 'LSAS_SWITZERLAND',
          firIcao: 'LSAS',
          firName: 'SWITZERLAND',
          type: 'domestic',
        }),
      ]);
    });
  });

  describe('getManagingCountriesForFir', () => {
    beforeEach(() => {
      setAllCountriesForTesting([
        {name: 'Belgium', isoA2: 'BE'},
        {name: 'Germany', isoA2: 'DE'},
        {name: 'Luxembourg', isoA2: 'LU'},
        {name: 'Netherlands', isoA2: 'NL'},
        {name: 'Switzerland', isoA2: 'CH'},
      ]);
    });

    it('returns explicit countries array when present on FirRecord', () => {
      const mockFir = {
        firId: 'LSAS_SWITZERLAND',
        firName: 'SWITZERLAND',
        firIcao: 'LSAS',
        type: 'domestic',
        countries: [{isoA2: 'CH', name: 'Switzerland'}],
        co2eTonnes: 100,
        meanRfMwPerSqM: 50,
        areaSqKm: 1000,
        percentOfFirTotal: '100%',
      };
      const countries = getManagingCountriesForFir(mockFir);
      expect(countries).toEqual([{isoA2: 'CH', name: 'Switzerland'}]);
    });

    it('resolves countries from the fir_to_country_iso2s list when countries array is missing', () => {
      const mockFir = {
        'fir_id': 'EDYY_MAASTRICHT_UAC',
        firname: 'MAASTRICHT UAC',
        'fir_icao': 'EDYY',
        type: 'international',
        'fir_to_country_iso2s': ['BE', 'DE', 'LU', 'NL'],
      };
      const countries = getManagingCountriesForFir(mockFir);
      expect(countries.map((c) => c.isoA2)).toEqual(['BE', 'DE', 'LU', 'NL']);
      expect(countries.map((c) => c.name)).toEqual([
        'Belgium',
        'Germany',
        'Luxembourg',
        'Netherlands',
      ]);
    });

    it('returns empty array for null or undefined FIR', () => {
      expect(getManagingCountriesForFir(null)).toEqual([]);
      expect(getManagingCountriesForFir(undefined)).toEqual([]);
    });

    it('upgrades fallback ISO names on pre-populated fir.countries using year-scoped records while preserving custom names', () => {
      clearCountryDataCache();
      setCountryDataInCache('CH_2025', {
        name: 'Switzerland (2025)',
        'iso_a2': 'CH',
      });
      const mockFir = {
        firId: 'LSAS_SWITZERLAND',
        firName: 'SWITZERLAND',
        firIcao: 'LSAS',
        type: 'domestic',
        countries: [
          {isoA2: 'CH', name: 'CH'},
          {isoA2: 'FR', name: 'Custom France Label'},
        ],
        co2eTonnes: 100,
        meanRfMwPerSqM: 50,
        areaSqKm: 1000,
        percentOfFirTotal: '100%',
      };
      expect(getManagingCountriesForFir(mockFir, 2025)).toEqual([
        {isoA2: 'CH', name: 'Switzerland (2025)'},
        {isoA2: 'FR', name: 'Custom France Label'},
      ]);
    });

    it('refreshes CACHED_YEARS_LRU order on synchronous getCountryRecord and getFirRecordsForCountry reads', () => {
      clearCountryDataCache();
      setAvailableYearsForTesting([2023, 2024, 2025]);
      setFirDataForTesting(2023, [{'fir_id': 'FIR_2023'}]);
      setFirDataForTesting(2024, [
        {
          'fir_id': 'LSAS_SWITZERLAND',
          'fir_icao': 'LSAS',
          firname: 'SWITZERLAND',
          'fir_to_country_iso2s': ['CH'],
          'co2e_gwp50_tonnes': 90,
        },
      ]);
      setFirDataForTesting(2025, [
        {
          'fir_id': 'LSAS_SWITZERLAND',
          'fir_icao': 'LSAS',
          firname: 'SWITZERLAND',
          'fir_to_country_iso2s': ['CH'],
          'co2e_gwp50_tonnes': 100,
        },
      ]);
      setCountryDataInCache('CH_2023', {name: 'Switzerland', 'iso_a2': 'CH'});
      expect(getCachedYearsForTesting()).toEqual([2023, 2024, 2025]);

      // Synchronous read of 2023 moves 2023 to the most-recently-used end.
      expect(getCountryRecord('CH', 2023)).not.toBeNull();
      expect(getCachedYearsForTesting()).toEqual([2024, 2025, 2023]);

      // Synchronous FIR read for 2025 (with prevYear 2024) touches both 2024 and 2025 on memo miss.
      const firs = getFirRecordsForCountry('CH', 2025, 'GWP50');
      expect(firs.length).toBe(1);
      expect(getCachedYearsForTesting()).toEqual([2023, 2024, 2025]);

      // Move 2023 back to most-recently-used, then verify a memoized hit for
      // 2025 still refreshes 2024 and 2025 in CACHED_YEARS_LRU.
      expect(getCountryRecord('CH', 2023)).not.toBeNull();
      expect(getCachedYearsForTesting()).toEqual([2024, 2025, 2023]);
      const memoizedFirs = getFirRecordsForCountry('CH', 2025, 'GWP50');
      expect(memoizedFirs).toBe(firs);
      expect(getCachedYearsForTesting()).toEqual([2023, 2024, 2025]);
    });
  });

  describe('getCountryMetrics loading state', () => {
    it('returns isLoading true and NOT_AVAILABLE_LABEL placeholders when country is not cached', () => {
      clearCountryDataCache();
      const data = getCountryMetrics('NonExistentCountry', 2025);
      expect(data.isLoading).toBeTrue();
      expect(data.aboveCountryHero.absoluteValue).toBe(NOT_AVAILABLE_LABEL);
      expect(data.firTotalHero.absoluteValue).toBe(NOT_AVAILABLE_LABEL);
      expect(data.firDensityRank).toBeUndefined();
    });
  });
});
