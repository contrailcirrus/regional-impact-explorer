import {
  installBrowserGlobalStubs,
  restoreBrowserGlobals,
} from './test_browser_globals';

// Load browser global stubs above before importing modules under test.
import 'jasmine';
import {
  clearCountryDataCache,
  CountryOption,
  setAllCountriesForTesting,
} from './CountryMetadata';
import {
  abortAndClearInFlightGrids,
  boundaryGeoJsonUrl,
  CachedGridItem,
  cacheOrRevokeGridResult,
  clearGridCache,
  computeAntimeridianSafeCentroid,
  computeCurrentGridPrefix,
  computeHighestCachedLod,
  computeIsSequenceFullyLoaded,
  decodeGzipBufferIfNeeded,
  deduplicateInFlightGridRequest,
  detachGridLayerForContextRestore,
  extractFirArea,
  extractFirIcao,
  extractFirId,
  extractFirName,
  FirFeature,
  getCachedGridEntry,
  getGridPreloadConcurrency,
  getWorldCountriesGeoJson,
  getWorldFirsGeoJson,
  InFlightGridEntry,
  installPixelPackStreamReadWorkaround,
  isAbortMapError,
  isFirFeatureCollection,
  isMatchingFirSelection,
  isWorldCountriesGeoJson,
  loadWorldCountriesGeoJson,
  loadWorldFirsGeoJson,
  mapHasLayer,
  mapHasSource,
  MAPLIBRE_NETWORK_ERROR_TEXT,
  matchFirFeatures,
  normalizeGridAvailability,
  normalizeRasterMetadata,
  parseGridMetaHeader,
  removeGridLayerAndSource,
  resetWorldCountriesGeoJsonForTesting,
  resetWorldFirsGeoJsonForTesting,
  resolveInitialCountry,
  resolveYearGate,
  revokeBlobUrlIfSafe,
  selectClickedFirFeature,
  setCachedGridEntry,
  takeInitialCountryResolution,
} from './dashboard_geo_utils';
import {
  isSelectedFirInternational,
  reconcilePlaybackModeWithAvailability,
} from './MapControlsOverlay';

describe('dashboard_geo_utils', () => {
  beforeAll(() => {
    installBrowserGlobalStubs({react: true, materialUi: true});
  });

  afterAll(() => {
    restoreBrowserGlobals();
  });

  describe('normalizeRasterMetadata & parseGridMetaHeader', () => {
    it('validates well-formed RasterMetadata objects', () => {
      expect(normalizeRasterMetadata({width: 1200, height: 600})).toEqual({
        width: 1200,
        height: 600,
      });
      expect(
        normalizeRasterMetadata({width: 1200, height: 600, dtype: 'float16'}),
      ).toEqual({width: 1200, height: 600, dtype: 'float16'});
    });

    it('rejects malformed RasterMetadata inputs', () => {
      expect(normalizeRasterMetadata(null)).toBeNull();
      expect(normalizeRasterMetadata(undefined)).toBeNull();
      expect(normalizeRasterMetadata('invalid')).toBeNull();
      expect(normalizeRasterMetadata({width: '1200', height: 600})).toBeNull();
      expect(normalizeRasterMetadata({width: 1200})).toBeNull();
    });

    it('normalizes snake_case fields in normalizeRasterMetadata', () => {
      expect(
        normalizeRasterMetadata({
          width: 1200,
          height: 600,
          dtype: 'float16',
          bbox: {
            'min_lon': -180,
            'max_lon': 180,
            'min_lat': -80,
            'max_lat': 80,
          },
          'value_max_ref_scaled': {impact: 2.5e11, density: 3e6},
          'gwp_tonnes_co2e_per_mj': {
            gwp20: 1.048618e-6,
            gwp50: 4.945187e-7,
            gwp100: 2.847392e-7,
          },
        }),
      ).toEqual({
        width: 1200,
        height: 600,
        dtype: 'float16',
        bbox: {minLon: -180, maxLon: 180, minLat: -80, maxLat: 80},
        valueMaxRefScaled: {impact: 2.5e11, density: 3e6},
        gwpTonnesCo2ePerMj: {
          gwp20: 1.048618e-6,
          gwp50: 4.945187e-7,
          gwp100: 2.847392e-7,
        },
      });
    });

    it('parses valid X-Grid-Meta JSON headers and rejects invalid ones', () => {
      expect(
        parseGridMetaHeader(
          JSON.stringify({
            width: 3600,
            height: 1800,
            dtype: 'float16',
            encoding: 'signed_log10_ref_scaled',
          }),
        ),
      ).toEqual({
        width: 3600,
        height: 1800,
        dtype: 'float16',
        encoding: 'signed_log10_ref_scaled',
      });
      expect(parseGridMetaHeader(null)).toBeNull();
      expect(parseGridMetaHeader('')).toBeNull();
      expect(parseGridMetaHeader('{invalid json')).toBeNull();
      expect(
        parseGridMetaHeader(JSON.stringify({width: -1, height: 600})),
      ).toBeNull();
    });

    it('returns non-gzipped ArrayBuffers unchanged from decodeGzipBufferIfNeeded', async () => {
      const raw = new Uint16Array([0x3c00, 0x4000, 0x0000]).buffer;
      const decoded = await decodeGzipBufferIfNeeded(raw);
      expect(decoded).toBe(raw);
    });
  });

  describe('computeCurrentGridPrefix', () => {
    it('computes total, monthly, and hourly prefixes for impact and density modes', () => {
      expect(computeCurrentGridPrefix('impact', 'all', 0, 0)).toBe(
        'total_global_impact_grid',
      );
      expect(computeCurrentGridPrefix('density', 'all', 5, 12)).toBe(
        'total_global_density_grid',
      );
      expect(computeCurrentGridPrefix('impact', 'monthly', 0, 0)).toBe(
        'monthly_global_impact_grid_01',
      );
      expect(computeCurrentGridPrefix('density', 'monthly', 11, 0)).toBe(
        'monthly_global_density_grid_12',
      );
      expect(computeCurrentGridPrefix('impact', 'hourly', 0, 0)).toBe(
        'hourly_global_impact_grid_00',
      );
      expect(computeCurrentGridPrefix('density', 'hourly', 0, 23)).toBe(
        'hourly_global_density_grid_23',
      );
    });

    it('falls back safely when monthIndex or hourIndex is out of range', () => {
      expect(computeCurrentGridPrefix('impact', 'monthly', 99, 0)).toBe(
        'monthly_global_impact_grid_01',
      );
      expect(computeCurrentGridPrefix('density', 'hourly', 0, 99)).toBe(
        'hourly_global_density_grid_00',
      );
    });
  });

  describe('computeHighestCachedLod', () => {
    it('returns the highest cached LOD (2 > 1 > 0) matching the actual cache key format', () => {
      const cache = new Map<string, unknown>();
      expect(
        computeHighestCachedLod(
          cache,
          2025,
          'total_global_impact_grid',
          'impact',
        ),
      ).toBe(0);

      cache.set('2025_total_global_impact_grid_impact_lod1', {});
      expect(
        computeHighestCachedLod(
          cache,
          2025,
          'total_global_impact_grid',
          'impact',
        ),
      ).toBe(1);

      cache.set('2025_total_global_impact_grid_impact_lod2', {});
      expect(
        computeHighestCachedLod(
          cache,
          2025,
          'total_global_impact_grid',
          'impact',
        ),
      ).toBe(2);
    });

    it('isolates cached LOD across different years and modes', () => {
      const cache = new Map<string, unknown>();
      cache.set('2025_total_global_impact_grid_impact_lod2', {});
      expect(
        computeHighestCachedLod(
          cache,
          2024,
          'total_global_impact_grid',
          'impact',
        ),
      ).toBe(0);
      expect(
        computeHighestCachedLod(
          cache,
          2025,
          'total_global_impact_grid',
          'density',
        ),
      ).toBe(0);
    });
  });

  describe('computeIsSequenceFullyLoaded', () => {
    it('checks whether all frames of a playback mode are present at any LOD', () => {
      const cache = new Map<string, unknown>();
      expect(computeIsSequenceFullyLoaded(cache, 2025, 'all', 'impact')).toBe(
        false,
      );

      cache.set('2025_total_global_impact_grid_impact_lod0', {});
      expect(computeIsSequenceFullyLoaded(cache, 2025, 'all', 'impact')).toBe(
        true,
      );

      expect(
        computeIsSequenceFullyLoaded(cache, 2025, 'monthly', 'impact'),
      ).toBe(false);
      const fullMonthlyCache = new Map<string, unknown>(
        [
          '01',
          '02',
          '03',
          '04',
          '05',
          '06',
          '07',
          '08',
          '09',
          '10',
          '11',
          '12',
        ].map((code) => [
          `2025_monthly_global_impact_grid_${code}_impact_lod1`,
          {},
        ]),
      );
      expect(
        computeIsSequenceFullyLoaded(
          fullMonthlyCache,
          2025,
          'monthly',
          'impact',
        ),
      ).toBe(true);
    });
  });

  describe('setCachedGridEntry & getCachedGridEntry (LRU eviction and URL.revokeObjectURL)', () => {
    it('evicts the least-recently-used entry when maxEntries is exceeded and revokes blob URLs', () => {
      const cache = new Map<string, CachedGridItem>();
      const revokedUrls: string[] = [];
      spyOn(URL, 'revokeObjectURL').and.callFake((url: string) => {
        revokedUrls.push(url);
      });

      const makeItem = (url: string): CachedGridItem => ({
        dataUrl: url,
        coords: [
          [-180, 85],
          [180, 85],
          [180, -85],
          [-180, -85],
        ],
        meta: {width: 10, height: 10},
        valueMax: 1,
      });

      setCachedGridEntry(
        cache,
        'k1',
        makeItem('blob:https://example.com/1'),
        2,
      );
      setCachedGridEntry(
        cache,
        'k2',
        makeItem('blob:https://example.com/2'),
        2,
      );
      expect(cache.size).toBe(2);

      // Touch 'k1' so 'k2' becomes the oldest (LRU) entry
      expect(getCachedGridEntry(cache, 'k1')?.dataUrl).toBe(
        'blob:https://example.com/1',
      );

      // Insert 'k3' with maxEntries = 2 -> should evict 'k2' and revoke its
      // blob URL
      setCachedGridEntry(
        cache,
        'k3',
        makeItem('blob:https://example.com/3'),
        2,
      );
      expect(cache.size).toBe(2);
      expect(cache.has('k1')).toBe(true);
      expect(cache.has('k2')).toBe(false);
      expect(cache.has('k3')).toBe(true);
      expect(revokedUrls).toEqual(['blob:https://example.com/2']);

      // Insert 'k4' with protectedUrl = 'blob:https://example.com/1' -> should
      // NOT revoke protectedUrl even if k1 is oldest
      setCachedGridEntry(
        cache,
        'k4',
        makeItem('blob:https://example.com/4'),
        2,
        'blob:https://example.com/1',
      );
      expect(revokedUrls).not.toContain('blob:https://example.com/1');
    });
  });

  describe('matchFirFeatures', () => {
    const sampleFeatures: FirFeature[] = [
      {
        type: 'Feature',
        properties: {
          fir_id: 'KZNY_NEW_YORK',
          fir_icao: 'KZNY',
          firname: 'NEW YORK',
        },
      },
      {
        type: 'Feature',
        properties: {
          fir_id: 'KZNY_NEW_YORK_OCEANIC',
          fir_icao: 'KZNY',
          firname: 'NEW YORK OCEANIC',
        },
      },
      {
        type: 'Feature',
        properties: {fir_id: 'LFFF_PARIS', fir_icao: 'LFFF', firname: 'PARIS'},
      },
    ];

    it('matches strictly by fir_id and stamps firType onto matched features', () => {
      const matched = matchFirFeatures(
        sampleFeatures,
        ['KZNY_NEW_YORK_OCEANIC'],
        'international',
      );
      expect(matched.length).toBe(1);
      expect(matched[0].properties?.fir_id).toBe('KZNY_NEW_YORK_OCEANIC');
      expect(matched[0].properties?.firname).toBe('NEW YORK OCEANIC');
      expect(matched[0].properties?.firType).toBe('international');
    });

    it('distinguishes domestic vs oceanic FIRs sharing an ICAO code via fir_id', () => {
      const dom = matchFirFeatures(
        sampleFeatures,
        ['KZNY_NEW_YORK'],
        'domestic',
      );
      expect(dom.length).toBe(1);
      expect(dom[0].properties?.firname).toBe('NEW YORK');
      expect(dom[0].properties?.firType).toBe('domestic');

      const intl = matchFirFeatures(
        sampleFeatures,
        ['KZNY_NEW_YORK_OCEANIC'],
        'international',
      );
      expect(intl.length).toBe(1);
      expect(intl[0].properties?.firname).toBe('NEW YORK OCEANIC');
      expect(intl[0].properties?.firType).toBe('international');
    });

    it('matches every fir_id in the list', () => {
      const matched = matchFirFeatures(
        sampleFeatures,
        ['LFFF_PARIS', 'KZNY_NEW_YORK'],
        'domestic',
      );
      expect(matched.map((feat) => feat.properties?.fir_id)).toEqual([
        'KZNY_NEW_YORK',
        'LFFF_PARIS',
      ]);
    });

    it('returns empty array when the fir_id list is missing, empty or unmatched', () => {
      expect(matchFirFeatures(sampleFeatures, undefined, 'domestic')).toEqual(
        [],
      );
      expect(matchFirFeatures(sampleFeatures, null, 'domestic')).toEqual([]);
      expect(matchFirFeatures(sampleFeatures, [], 'domestic')).toEqual([]);
      expect(
        matchFirFeatures(sampleFeatures, ['UNKNOWN_FIR'], 'domestic'),
      ).toEqual([]);
    });
  });

  describe('selectClickedFirFeature', () => {
    it('switches to a non-selected FIR when overlapping with the currently selected FIR or highlight layer', () => {
      const features: FirFeature[] = [
        {
          type: 'Feature',
          properties: {
            fir_id: 'KZNY_NEW_YORK_OCEANIC',
            fir_icao: 'KZNY',
            firname: 'NEW YORK OCEANIC',
            areasqkm: 5000000,
            firType: 'international',
          },
        },
        {
          type: 'Feature',
          properties: {
            fir_id: 'KZBW_BOSTON',
            fir_icao: 'KZBW',
            firname: 'BOSTON',
            areasqkm: 450000,
            firType: 'domestic',
          },
        },
      ];

      // Even if KZNY_NEW_YORK_OCEANIC is international, clicking where
      // KZBW_BOSTON (non-selected) is present selects KZBW_BOSTON
      const result = selectClickedFirFeature(features, 'KZNY_NEW_YORK_OCEANIC');
      expect(result).not.toBeNull();
      expect(result!.firId).toBe('KZBW_BOSTON');
      expect(result!.icao).toBe('KZBW');
      expect(result!.firName).toBe('BOSTON');
    });

    it('reads fir_id, fir_icao, firname and areasqkm', () => {
      const feat: FirFeature = {
        type: 'Feature',
        properties: {
          fir_id: 'EGTT_LONDON',
          fir_icao: 'EGTT',
          firname: 'LONDON',
          areasqkm: 250000,
        },
      };
      expect(extractFirId(feat)).toBe('EGTT_LONDON');
      expect(extractFirIcao(feat)).toBe('EGTT');
      expect(extractFirName(feat)).toBe('LONDON');
      expect(extractFirArea(feat)).toBe(250000);
      expect(isMatchingFirSelection(feat, 'EGTT_LONDON')).toBeTrue();
      expect(isMatchingFirSelection(feat, 'EGPX_SCOTTISH')).toBeFalse();
      const clicked = selectClickedFirFeature([feat], 'EGPX_SCOTTISH');
      expect(clicked?.firId).toBe('EGTT_LONDON');
      expect(clicked?.icao).toBe('EGTT');
      expect(clicked?.firName).toBe('LONDON');
    });

    it('ignores other spellings of the FIR properties', () => {
      const legacy = {
        type: 'Feature',
        properties: {
          'fir_id': 'EGTT_LONDON',
          'icaocode': 'EGTT',
          'ICAOCODE': 'EGTT',
          'AV_AIRSPAC': 'EGTT',
          'FIR_NAME': 'LONDON',
          'AV_NAME': 'LONDON',
          'area_sq_km': 250000,
          'SQKMgeod': 250000,
        },
      } as unknown as FirFeature;
      expect(extractFirIcao(legacy)).toBe('');
      expect(extractFirName(legacy)).toBe('');
      // Unknown area sorts last when picking the smallest clicked FIR.
      expect(extractFirArea(legacy)).toBe(1e9);
    });
  });

  describe('clearGridCache', () => {
    it('revokes all blob URLs in the cache and clears all entries', () => {
      const cache = new Map<string, CachedGridItem>();
      const revokedUrls: string[] = [];
      spyOn(URL, 'revokeObjectURL').and.callFake((url: string) => {
        revokedUrls.push(url);
      });

      const makeItem = (url: string): CachedGridItem => ({
        dataUrl: url,
        coords: [
          [-180, 85],
          [180, 85],
          [180, -85],
          [-180, -85],
        ],
        meta: {width: 10, height: 10},
        valueMax: 1,
      });

      cache.set('k1', makeItem('blob:https://example.com/a'));
      cache.set('k2', makeItem('data:image/png;base64,AAA'));
      cache.set('k3', makeItem('blob:https://example.com/b'));

      clearGridCache(cache);
      expect(cache.size).toBe(0);
      expect(revokedUrls).toEqual([
        'blob:https://example.com/a',
        'blob:https://example.com/b',
      ]);
    });
  });

  describe('computeAntimeridianSafeCentroid', () => {
    it('shifts negative longitudes by +360 when MultiPolygon crosses the antimeridian', () => {
      const antimeridianCollection = {
        type: 'FeatureCollection',
        features: [
          {
            type: 'Feature',
            geometry: {
              type: 'MultiPolygon',
              coordinates: [
                [
                  [
                    [175, -18],
                    [179, -18],
                    [179, -16],
                    [175, -16],
                  ],
                ],
                [
                  [
                    [-179, -18],
                    [-175, -18],
                    [-175, -16],
                    [-179, -16],
                  ],
                ],
              ],
            },
          },
        ],
      };

      const centroid = computeAntimeridianSafeCentroid(antimeridianCollection);
      expect(centroid).not.toBeNull();
      // Average of +177 and -177 across the dateline should be ~180 (or -180),
      // NEVER 0 (Prime Meridian)
      expect(Math.abs(centroid![0])).toBeGreaterThan(170);
      expect(centroid![1]).toBeCloseTo(-17, 1);
    });

    it('delegates to d3GeoCentroid when geometry does not cross the antimeridian', () => {
      const standardCollection = {
        type: 'FeatureCollection',
        features: [
          {
            type: 'Feature',
            geometry: {
              type: 'Polygon',
              coordinates: [
                [
                  [6, 46],
                  [10, 46],
                  [10, 48],
                  [6, 48],
                ],
              ],
            },
          },
        ],
      };

      const mockD3Centroid = jasmine
        .createSpy('d3GeoCentroid')
        .and.returnValue([8.2, 46.8] as [number, number]);
      const centroid = computeAntimeridianSafeCentroid(
        standardCollection,
        mockD3Centroid,
      );
      expect(mockD3Centroid).toHaveBeenCalledTimes(1);
      expect(centroid).toEqual([8.2, 46.8]);
    });

    it('delegates antimeridian-crossing geometry to d3GeoCentroid', () => {
      const crossing = {
        type: 'Polygon',
        coordinates: [
          [
            [175, -18],
            [-175, -18],
            [-175, -16],
            [175, -16],
            [175, -18],
          ],
        ],
      };
      const mockD3Centroid = jasmine
        .createSpy('d3GeoCentroid')
        .and.returnValue([179.5, -17] as [number, number]);
      expect(computeAntimeridianSafeCentroid(crossing, mockD3Centroid)).toEqual(
        [179.5, -17],
      );
      expect(mockD3Centroid).toHaveBeenCalledTimes(1);
    });

    it('rejects a d3 centroid outside the bounding box (inverted winding)', () => {
      const fijiLike = {
        type: 'MultiPolygon',
        coordinates: [
          [
            [
              [177, -16],
              [180, -16],
              [180, -18],
              [177, -18],
              [177, -16],
            ],
          ],
          [
            [
              [-180, -16],
              [-178, -16],
              [-178, -18],
              [-180, -18],
              [-180, -16],
            ],
          ],
        ],
      };
      // A counter-clockwise ring makes d3 return a point near the antipode.
      const antipodalCentroid = jasmine
        .createSpy('d3GeoCentroid')
        .and.returnValue([-0.5, 17] as [number, number]);
      const centroid = computeAntimeridianSafeCentroid(
        fijiLike,
        antipodalCentroid,
      );
      expect(Math.abs(centroid![0])).toBeGreaterThan(175);
      expect(centroid![1]).toBeCloseTo(-17, 0);
    });

    it('accepts a d3 centroid inside an antimeridian-crossing bounding box', () => {
      const fijiLike = {
        type: 'Polygon',
        coordinates: [
          [
            [177, -16],
            [-178, -16],
            [-178, -18],
            [177, -18],
            [177, -16],
          ],
        ],
      };
      const insideCentroid = jasmine
        .createSpy('d3GeoCentroid')
        .and.returnValue([-179.5, -17] as [number, number]);
      expect(computeAntimeridianSafeCentroid(fijiLike, insideCentroid)).toEqual(
        [-179.5, -17],
      );
    });

    it('falls back to vertex averaging when d3GeoCentroid is not finite', () => {
      const crossing = {
        type: 'MultiPolygon',
        coordinates: [
          [
            [
              [175, -18],
              [179, -18],
              [179, -16],
              [175, -16],
            ],
          ],
          [
            [
              [-179, -18],
              [-175, -18],
              [-175, -16],
              [-179, -16],
            ],
          ],
        ],
      };
      const nanCentroid = jasmine
        .createSpy('d3GeoCentroid')
        .and.returnValue([NaN, NaN] as [number, number]);
      const centroid = computeAntimeridianSafeCentroid(crossing, nanCentroid);
      expect(Math.abs(centroid![0])).toBeCloseTo(180, 6);
      expect(centroid![1]).toBeCloseTo(-17, 1);
    });

    it('skips the closing vertex of each ring in the fallback average', () => {
      const closedSquare = {
        type: 'Polygon',
        coordinates: [
          [
            [0, 0],
            [10, 0],
            [10, 10],
            [0, 10],
            [0, 0],
          ],
        ],
      };
      // Counting the repeated [0, 0] would pull the longitude to ~4.
      const centroid = computeAntimeridianSafeCentroid(closedSquare);
      expect(centroid![0]).toBeCloseTo(5, 6);
    });
  });

  describe('isFirFeatureCollection & normalizeGridAvailability', () => {
    it('validates FirFeatureCollection and GridAvailability objects', () => {
      expect(
        isFirFeatureCollection({type: 'FeatureCollection', features: []}),
      ).toBeTrue();
      expect(isFirFeatureCollection(null)).toBeFalse();
      expect(isFirFeatureCollection({type: 'FeatureCollection'})).toBeFalse();

      expect(
        normalizeGridAvailability({
          'has_total': true,
          'has_monthly': true,
          'has_hourly': false,
          'months_count': 12,
          'hours_count': 0,
        }),
      ).toEqual({
        hasTotal: true,
        hasMonthly: true,
        hasHourly: false,
        monthsCount: 12,
        hoursCount: 0,
      });
      expect(normalizeGridAvailability(null)).toBeNull();
      expect(normalizeGridAvailability({'has_total': 'yes'})).toBeNull();
    });
  });

  describe('loadWorldCountriesGeoJson', () => {
    const countries = {
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          properties: {name: 'Kosovo', 'iso_a2': '-99', 'iso_a3': '-99'},
          geometry: null,
        },
      ],
    };

    beforeEach(() => {
      resetWorldCountriesGeoJsonForTesting();
    });

    afterEach(() => {
      resetWorldCountriesGeoJsonForTesting();
    });

    it("fetches the requested year's /countries/{year}.json once and shares the result", async () => {
      const fetchSpy = spyOn(globalThis, 'fetch').and.resolveTo({
        ok: true,
        json: async () => countries,
      } as unknown as Response);

      expect(getWorldCountriesGeoJson()).toBeNull();
      const [first, second] = await Promise.all([
        loadWorldCountriesGeoJson(2037),
        loadWorldCountriesGeoJson(2037),
      ]);
      const third = await loadWorldCountriesGeoJson(2036);

      expect(fetchSpy).toHaveBeenCalledOnceWith('/countries/2037.json');
      expect(first).toEqual(countries as unknown as typeof first);
      expect(second).toBe(first);
      expect(third).toBe(first);
      expect(getWorldCountriesGeoJson()).toBe(first);
    });

    it('returns null on failure and retries on the next call', async () => {
      spyOn(console, 'warn');
      const fetchSpy = spyOn(globalThis, 'fetch').and.returnValues(
        Promise.resolve({ok: false, status: 404} as unknown as Response),
        Promise.resolve({
          ok: true,
          json: async () => countries,
        } as unknown as Response),
      );

      expect(await loadWorldCountriesGeoJson(2037)).toBeNull();
      expect(getWorldCountriesGeoJson()).toBeNull();
      expect(await loadWorldCountriesGeoJson(2037)).not.toBeNull();
      expect(fetchSpy).toHaveBeenCalledTimes(2);
    });

    it('rejects payloads that are not a FeatureCollection', () => {
      expect(isWorldCountriesGeoJson(countries)).toBeTrue();
      expect(
        isWorldCountriesGeoJson({type: 'Topology', objects: {}}),
      ).toBeFalse();
      expect(isWorldCountriesGeoJson(null)).toBeFalse();
    });

    it('refuses to fetch without a valid year', () => {
      const fetchSpy = spyOn(globalThis, 'fetch');
      expect(() => loadWorldCountriesGeoJson(0)).toThrowError(
        /a year is required/,
      );
      expect(fetchSpy).not.toHaveBeenCalled();
    });
  });

  describe('loadWorldFirsGeoJson', () => {
    const firs = {
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          properties: {
            'fir_id': 'LSAS_SWITZERLAND',
            'fir_icao': 'LSAS',
            firname: 'SWITZERLAND',
          },
          geometry: null,
        },
      ],
    };

    beforeEach(() => {
      resetWorldFirsGeoJsonForTesting();
    });

    afterEach(() => {
      resetWorldFirsGeoJsonForTesting();
    });

    it('fetches /firs/{year}.json once, validates HTTP status, and retries on a subsequent year change after failure', async () => {
      spyOn(console, 'warn');
      const fetchSpy = spyOn(globalThis, 'fetch').and.returnValues(
        Promise.resolve({ok: false, status: 503} as unknown as Response),
        Promise.resolve({
          ok: true,
          json: async () => firs,
        } as unknown as Response),
      );

      expect(getWorldFirsGeoJson()).toBeNull();
      expect(await loadWorldFirsGeoJson(2024)).toBeNull();
      expect(getWorldFirsGeoJson()).toBeNull();

      // Switching year after an initial failure retries with the new year's URL
      const loaded = await loadWorldFirsGeoJson(2025);
      expect(loaded).toEqual(firs as unknown as typeof loaded);
      expect(getWorldFirsGeoJson()).toBe(loaded);
      expect(fetchSpy.calls.allArgs()).toEqual([
        ['/firs/2024.json'],
        ['/firs/2025.json'],
      ]);

      // Subsequent calls reuse the cached FeatureCollection without re-fetching
      expect(await loadWorldFirsGeoJson(2026)).toBe(loaded);
      expect(fetchSpy).toHaveBeenCalledTimes(2);
    });
  });

  describe('boundaryGeoJsonUrl', () => {
    it('names the year explicitly', () => {
      expect(boundaryGeoJsonUrl('countries', 2037)).toBe(
        '/countries/2037.json',
      );
      expect(boundaryGeoJsonUrl('firs', 2037)).toBe('/firs/2037.json');
    });

    it('throws without a valid year', () => {
      expect(() => boundaryGeoJsonUrl('firs', 0)).toThrowError(
        /a year is required/,
      );
    });
  });

  describe('resolveYearGate', () => {
    it('waits while the year list is loading, even with stale years', () => {
      expect(resolveYearGate('loading', [])).toEqual({kind: 'loading'});
      expect(resolveYearGate('loading', [2030])).toEqual({kind: 'loading'});
    });

    it('starts on the latest year once years are known', () => {
      expect(resolveYearGate('ready', [2031, 2029, 2030])).toEqual({
        kind: 'ready',
        initialYear: 2031,
      });
      expect(resolveYearGate('ready', [2030])).toEqual({
        kind: 'ready',
        initialYear: 2030,
      });
    });

    it('reports that nothing is published when the list is empty', () => {
      expect(resolveYearGate('ready', [])).toEqual({kind: 'empty'});
    });

    it('reports an error when the list could not be loaded', () => {
      expect(resolveYearGate('error', [])).toEqual({kind: 'error'});
    });

    it('keeps rendering with already-loaded years after a failed refresh', () => {
      expect(resolveYearGate('error', [2030])).toEqual({
        kind: 'ready',
        initialYear: 2030,
      });
    });

    it('does not mutate the year list', () => {
      const years = [2031, 2029];
      resolveYearGate('ready', years);
      expect(years).toEqual([2031, 2029]);
    });
  });

  describe('deduplicateInFlightGridRequest', () => {
    const sampleItem: CachedGridItem = {
      dataUrl: 'blob:grid-1',
      coords: [
        [-180, 85],
        [180, 85],
        [180, -85],
        [-180, -85],
      ],
      meta: {width: 10, height: 10},
      valueMax: 1,
    };

    it('shares a single in-flight fetch across concurrent callers for the same cacheKey', async () => {
      const inFlightMap = new Map<string, InFlightGridEntry>();
      let startCalls = 0;
      const startFetch = async () => {
        startCalls++;
        return sampleItem;
      };

      const c1 = new AbortController();
      const c2 = new AbortController();
      const [res1, res2] = await Promise.all([
        deduplicateInFlightGridRequest(
          inFlightMap,
          '2024_total_impact_lod2',
          startFetch,
          c1.signal,
        ),
        deduplicateInFlightGridRequest(
          inFlightMap,
          '2024_total_impact_lod2',
          startFetch,
          c2.signal,
        ),
      ]);

      expect(startCalls).toBe(1);
      expect(res1).toBe(sampleItem);
      expect(res2).toBe(sampleItem);
      expect(inFlightMap.size).toBe(0);
    });

    it('synchronously evicts an aborted entry so an immediate retry in the same tick starts a fresh request', async () => {
      const inFlightMap = new Map<string, InFlightGridEntry>();
      const observedSignals: AbortSignal[] = [];
      let resolveFirst!: (val: CachedGridItem | null) => void;

      const firstController = new AbortController();
      const firstPromise = deduplicateInFlightGridRequest(
        inFlightMap,
        '2024_total_impact_lod2',
        (sig) => {
          observedSignals.push(sig);
          return new Promise<CachedGridItem | null>((resolve) => {
            resolveFirst = resolve;
          });
        },
        firstController.signal,
      );

      expect(observedSignals.length).toBe(1);
      expect(inFlightMap.has('2024_total_impact_lod2')).toBe(true);

      // Synchronously abort the first caller and immediately issue a retry in
      // the same tick.
      firstController.abort();
      expect(observedSignals[0].aborted).toBe(true);
      expect(inFlightMap.has('2024_total_impact_lod2')).toBe(false);

      const secondController = new AbortController();
      const secondPromise = deduplicateInFlightGridRequest(
        inFlightMap,
        '2024_total_impact_lod2',
        async (sig) => {
          observedSignals.push(sig);
          return sig.aborted ? null : sampleItem;
        },
        secondController.signal,
      );

      expect(observedSignals.length).toBe(2);
      expect(observedSignals[1].aborted).toBe(false);

      // Settle the aborted first request and verify its .finally() does not
      // evict the replacement entry.
      resolveFirst(null);
      const [firstResult, secondResult] = await Promise.all([
        firstPromise,
        secondPromise,
      ]);

      expect(firstResult).toBeNull();
      expect(secondResult).toBe(sampleItem);
      expect(inFlightMap.size).toBe(0);
    });

    it('keeps the shared request alive when only one of multiple callers aborts', async () => {
      const inFlightMap = new Map<string, InFlightGridEntry>();
      let sharedSignal: AbortSignal | undefined;

      const c1 = new AbortController();
      const c2 = new AbortController();
      const p1 = deduplicateInFlightGridRequest(
        inFlightMap,
        '2024_total_impact_lod2',
        async (sig) => {
          sharedSignal = sig;
          await Promise.resolve();
          return sig.aborted ? null : sampleItem;
        },
        c1.signal,
      );
      const p2 = deduplicateInFlightGridRequest(
        inFlightMap,
        '2024_total_impact_lod2',
        async () => sampleItem,
        c2.signal,
      );

      c1.abort();
      expect(sharedSignal?.aborted).toBe(false);

      const [res1, res2] = await Promise.all([p1, p2]);
      expect(res1).toBeNull();
      expect(res2).toBe(sampleItem);
    });

    it('aborts all in-flight grid controllers and clears the map via abortAndClearInFlightGrids', () => {
      const inFlightMap = new Map<string, InFlightGridEntry>();
      const c1 = new AbortController();
      const c2 = new AbortController();
      inFlightMap.set('2024_total_impact_lod2', {
        promise: Promise.resolve(null),
        controller: c1,
        signals: new Set(),
        hasUnabortableCaller: false,
      });
      inFlightMap.set('2024_total_density_lod2', {
        promise: Promise.resolve(null),
        controller: c2,
        signals: new Set(),
        hasUnabortableCaller: true,
      });

      abortAndClearInFlightGrids(inFlightMap);

      expect(c1.signal.aborted).toBeTrue();
      expect(c2.signal.aborted).toBeTrue();
      expect(inFlightMap.size).toBe(0);
    });

    it('revokes post-unmount blob URLs instead of inserting into gridCache via cacheOrRevokeGridResult', () => {
      const cache = new Map<string, CachedGridItem>();
      const revokeSpy = spyOn(URL, 'revokeObjectURL');

      const mountedResult = cacheOrRevokeGridResult(
        cache,
        '2024_total_impact_lod2',
        sampleItem,
        true,
      );
      expect(mountedResult).toBe(sampleItem);
      expect(cache.get('2024_total_impact_lod2')).toBe(sampleItem);
      expect(revokeSpy).not.toHaveBeenCalled();

      const unmountedBlobItem: CachedGridItem = {
        ...sampleItem,
        dataUrl: 'blob:http://localhost/post-unmount-leak',
      };
      const unmountedResult = cacheOrRevokeGridResult(
        cache,
        '2024_total_density_lod2',
        unmountedBlobItem,
        false,
      );
      expect(unmountedResult).toBeNull();
      expect(cache.has('2024_total_density_lod2')).toBeFalse();
      expect(revokeSpy).toHaveBeenCalledOnceWith(
        'blob:http://localhost/post-unmount-leak',
      );
    });
  });

  describe('getGridPreloadConcurrency', () => {
    it('limits LOD-2 sequence preloads to two workers to bound peak memory', () => {
      expect(getGridPreloadConcurrency(2)).toBe(2);
      expect(getGridPreloadConcurrency(3)).toBe(2);
    });

    it('keeps six workers for the smaller LOD 0 and LOD 1 grids', () => {
      expect(getGridPreloadConcurrency(0)).toBe(6);
      expect(getGridPreloadConcurrency(1)).toBe(6);
    });
  });

  describe('MAPLIBRE_NETWORK_ERROR_TEXT', () => {
    it(
      'provides an accurate library load error message without tile or VPN ' +
        'references',
      () => {
        expect(MAPLIBRE_NETWORK_ERROR_TEXT).toBe(
          "The MapLibre GL JS library couldn't be loaded. Please check your " +
            'network connection and reload the page.',
        );
      },
    );
  });
  describe('detachGridLayerForContextRestore', () => {
    function makeMapWithGrid(hasGrid: boolean) {
      const removed: string[] = [];
      const map = {
        getLayer: (id: string) => (hasGrid ? {id} : undefined),
        getSource: (id: string) => (hasGrid ? {id} : undefined),
        removeLayer: (id: string) => {
          removed.push(`layer:${id}`);
          return map;
        },
        removeSource: (id: string) => {
          removed.push(`source:${id}`);
          return map;
        },
      };
      return {map, removed};
    }

    it('removes the grid layer before its source', () => {
      const {map, removed} = makeMapWithGrid(true);

      detachGridLayerForContextRestore(
        map as unknown as Parameters<
          typeof detachGridLayerForContextRestore
        >[0],
      );

      expect(removed).toEqual([
        'layer:impact-grid-layer',
        'source:impact-grid-src',
      ]);
    });

    it('is a no-op when no grid is on the map', () => {
      const {map, removed} = makeMapWithGrid(false);

      detachGridLayerForContextRestore(
        map as unknown as Parameters<
          typeof detachGridLayerForContextRestore
        >[0],
      );

      expect(removed).toEqual([]);
    });

    it('logs instead of throwing when the style is mid-reload', () => {
      const warnSpy = spyOn(console, 'warn');
      const throwingMap = {
        getLayer: () => {
          throw new Error('Style is not done loading');
        },
      };

      expect(() => {
        detachGridLayerForContextRestore(
          throwingMap as unknown as Parameters<
            typeof detachGridLayerForContextRestore
          >[0],
        );
      }).not.toThrow();
      expect(warnSpy).toHaveBeenCalledWith(
        'Failed to detach grid layer after WebGL context restore:',
        jasmine.any(Error),
      );
    });
  });
  describe('installPixelPackStreamReadWorkaround', () => {
    const PIXEL_PACK_BUFFER = 0x88eb;
    const ARRAY_BUFFER = 0x8892;
    const STREAM_READ = 0x88e1;
    const STREAM_DRAW = 0x88e0;

    function makeFakeContext() {
      const calls: unknown[][] = [];
      const gl = {
        bufferData(...args: unknown[]): void {
          calls.push(args);
        },
      };
      return {gl, calls};
    }

    it('rewrites STREAM_READ pixel-pack uploads to STREAM_DRAW', () => {
      const {gl, calls} = makeFakeContext();

      installPixelPackStreamReadWorkaround(gl);
      gl.bufferData(PIXEL_PACK_BUFFER, 1024, STREAM_READ);

      expect(calls).toEqual([[PIXEL_PACK_BUFFER, 1024, STREAM_DRAW]]);
    });

    it('passes other buffer uploads through unchanged', () => {
      const {gl, calls} = makeFakeContext();

      installPixelPackStreamReadWorkaround(gl);
      gl.bufferData(ARRAY_BUFFER, 64, STREAM_READ);
      gl.bufferData(PIXEL_PACK_BUFFER, 64, STREAM_DRAW);

      expect(calls).toEqual([
        [ARRAY_BUFFER, 64, STREAM_READ],
        [PIXEL_PACK_BUFFER, 64, STREAM_DRAW],
      ]);
    });

    it('does not wrap the same context twice', () => {
      const {gl} = makeFakeContext();

      installPixelPackStreamReadWorkaround(gl);
      const patched = gl.bufferData;
      installPixelPackStreamReadWorkaround(gl);

      expect(gl.bufferData).toBe(patched);
    });

    it('leaves other contexts untouched', () => {
      const {gl: patchedGl} = makeFakeContext();
      const {gl: otherGl, calls: otherCalls} = makeFakeContext();
      const originalOther = otherGl.bufferData;

      installPixelPackStreamReadWorkaround(patchedGl);
      otherGl.bufferData(PIXEL_PACK_BUFFER, 8, STREAM_READ);

      expect(otherGl.bufferData).toBe(originalOther);
      expect(otherCalls).toEqual([[PIXEL_PACK_BUFFER, 8, STREAM_READ]]);
    });
  });

  describe('isAbortMapError', () => {
    it('detects a nested AbortError by name', () => {
      expect(isAbortMapError({error: {name: 'AbortError'}})).toBeTrue();
    });

    it('detects AbortError mentioned in the nested or top-level message', () => {
      expect(
        isAbortMapError({error: {message: 'AbortError: tile cancelled'}}),
      ).toBeTrue();
      expect(isAbortMapError({message: 'Fetch AbortError'})).toBeTrue();
    });

    it('returns false for real errors and non-object payloads', () => {
      expect(
        isAbortMapError({error: {name: 'TypeError', message: 'bad style'}}),
      ).toBeFalse();
      expect(isAbortMapError({error: 'AbortError'})).toBeFalse();
      expect(isAbortMapError('AbortError')).toBeFalse();
      expect(isAbortMapError(null)).toBeFalse();
    });
  });

  describe('MapLibre grid layer helpers', () => {
    interface FakeMap {
      style?: unknown;
      getLayer: jasmine.Spy;
      getSource: jasmine.Spy;
      removeLayer: jasmine.Spy;
      removeSource: jasmine.Spy;
    }

    function createFakeMap(): FakeMap {
      return {
        style: {},
        getLayer: jasmine.createSpy('getLayer').and.returnValue({}),
        getSource: jasmine.createSpy('getSource').and.returnValue({}),
        removeLayer: jasmine.createSpy('removeLayer'),
        removeSource: jasmine.createSpy('removeSource'),
      };
    }

    function asMap(
      fake: FakeMap,
    ): Parameters<typeof removeGridLayerAndSource>[0] {
      return fake as unknown as Parameters<typeof removeGridLayerAndSource>[0];
    }

    beforeEach(() => {
      spyOn(console, 'debug');
    });

    it('reports layers and sources as absent when the style is not loaded', () => {
      const fake = createFakeMap();
      fake.style = undefined;

      expect(mapHasLayer(asMap(fake), 'any')).toBeFalse();
      expect(mapHasSource(asMap(fake), 'any')).toBeFalse();
      expect(mapHasLayer(null, 'any')).toBeFalse();
      expect(fake.getLayer).not.toHaveBeenCalled();
    });

    it('treats a throwing lookup as absent and logs it at debug level', () => {
      const fake = createFakeMap();
      fake.getLayer.and.throwError('style torn down');

      expect(mapHasLayer(asMap(fake), 'impact-grid-layer')).toBeFalse();
      expect(console.debug).toHaveBeenCalledTimes(1);
    });

    it('removes both the grid layer and its source when present', () => {
      const fake = createFakeMap();

      removeGridLayerAndSource(asMap(fake));

      expect(fake.removeLayer).toHaveBeenCalledOnceWith('impact-grid-layer');
      expect(fake.removeSource).toHaveBeenCalledOnceWith('impact-grid-src');
    });

    it('still removes the source when removing the layer throws', () => {
      const fake = createFakeMap();
      fake.removeLayer.and.throwError('busy');

      removeGridLayerAndSource(asMap(fake));

      expect(fake.removeSource).toHaveBeenCalledOnceWith('impact-grid-src');
      expect(console.debug).toHaveBeenCalledTimes(1);
    });

    it('skips removal for absent layers and sources', () => {
      const fake = createFakeMap();
      fake.getLayer.and.returnValue(undefined);
      fake.getSource.and.returnValue(undefined);

      removeGridLayerAndSource(asMap(fake));

      expect(fake.removeLayer).not.toHaveBeenCalled();
      expect(fake.removeSource).not.toHaveBeenCalled();
    });
  });

  describe('resolveInitialCountry', () => {
    const rankedCountries: CountryOption[] = [
      {
        name: 'France',
        isoA2: 'FR',
        co2eRanks: {GWP20: 2, GWP50: 2, GWP100: 2},
      },
      {
        name: 'Kosovo',
        isoA2: '-99',
        co2eRanks: {GWP20: 1, GWP50: 3, GWP100: 3},
      },
      {
        name: 'United States',
        isoA2: 'US',
        co2eRanks: {GWP20: 3, GWP50: 1, GWP100: 1},
      },
    ];

    it('waits while the country list is loading', () => {
      expect(resolveInitialCountry('', rankedCountries, 'loading')).toEqual({
        kind: 'pending',
      });
    });

    it('picks the top over-country CO2e country when there is no guess', () => {
      expect(resolveInitialCountry('', rankedCountries, 'ready')).toEqual({
        kind: 'resolved',
        country: 'US',
      });
    });

    it('replaces a guess the backend does not list with the top country', () => {
      expect(resolveInitialCountry('ZZ', rankedCountries, 'ready')).toEqual({
        kind: 'resolved',
        country: 'US',
      });
    });

    it('keeps a guess the backend lists', () => {
      expect(resolveInitialCountry('FR', rankedCountries, 'ready')).toEqual({
        kind: 'resolved',
        country: 'FR',
      });
    });

    it('uses the active GWP horizon and name identifiers for territories', () => {
      expect(
        resolveInitialCountry('', rankedCountries, 'ready', 'GWP20'),
      ).toEqual({kind: 'resolved', country: 'Kosovo'});
    });

    it('reports no country when the list failed, is empty, or is unranked', () => {
      const unranked: CountryOption[] = [{name: 'France', isoA2: 'FR'}];
      expect(resolveInitialCountry('', [], 'error')).toEqual({
        kind: 'unavailable',
      });
      expect(resolveInitialCountry('ZZ', [], 'ready')).toEqual({
        kind: 'unavailable',
      });
      expect(resolveInitialCountry('', unranked, 'ready')).toEqual({
        kind: 'unavailable',
      });
    });
  });

  describe('takeInitialCountryResolution', () => {
    afterEach(() => {
      clearCountryDataCache();
    });

    const rankedCountries: CountryOption[] = [
      {
        name: 'Switzerland',
        isoA2: 'CH',
        co2eRanks: {GWP20: 2, GWP50: 2, GWP100: 2},
      },
      {
        name: 'United States',
        isoA2: 'US',
        co2eRanks: {GWP20: 1, GWP50: 1, GWP100: 1},
      },
    ];

    it('keeps the initial country pending while the list is loading', () => {
      clearCountryDataCache();
      const pendingRef: {current: string | null} = {current: 'ZZ'};

      expect(takeInitialCountryResolution(pendingRef)).toEqual({
        kind: 'pending',
      });
      expect(pendingRef.current).toBe('ZZ');
    });

    it('resolves an unknown guess to the top country and clears the ref', () => {
      setAllCountriesForTesting(rankedCountries);
      const pendingRef: {current: string | null} = {current: 'ZZ'};

      expect(takeInitialCountryResolution(pendingRef)).toEqual({
        kind: 'resolved',
        country: 'US',
      });
      expect(pendingRef.current).toBeNull();
    });

    it('empties the ref when no listed country is ranked', () => {
      setAllCountriesForTesting([{name: 'Switzerland', isoA2: 'CH'}]);
      const pendingRef: {current: string | null} = {current: 'ZZ'};

      expect(takeInitialCountryResolution(pendingRef)).toEqual({
        kind: 'unavailable',
      });
      expect(pendingRef.current).toBe('');
    });

    it('returns null once the user picked a country', () => {
      setAllCountriesForTesting(rankedCountries);
      const pendingRef: {current: string | null} = {current: null};

      expect(takeInitialCountryResolution(pendingRef)).toBeNull();
    });
  });

  describe('reconcilePlaybackModeWithAvailability', () => {
    it('preserves the active playback mode when its grids are available', () => {
      const allAvailable = {
        hasTotal: true,
        hasMonthly: true,
        hasHourly: true,
        monthsCount: 12,
        hoursCount: 24,
      };
      expect(reconcilePlaybackModeWithAvailability('all', allAvailable)).toBe(
        'all',
      );
      expect(
        reconcilePlaybackModeWithAvailability('monthly', allAvailable),
      ).toBe('monthly');
      expect(
        reconcilePlaybackModeWithAvailability('hourly', allAvailable),
      ).toBe('hourly');
    });

    it('falls back from monthly to all or hourly when monthly grids are absent', () => {
      expect(
        reconcilePlaybackModeWithAvailability('monthly', {
          hasTotal: true,
          hasMonthly: false,
          hasHourly: true,
          monthsCount: 0,
          hoursCount: 24,
        }),
      ).toBe('all');
      expect(
        reconcilePlaybackModeWithAvailability('monthly', {
          hasTotal: false,
          hasMonthly: false,
          hasHourly: true,
          monthsCount: 0,
          hoursCount: 24,
        }),
      ).toBe('hourly');
      expect(
        reconcilePlaybackModeWithAvailability('monthly', {
          hasTotal: false,
          hasMonthly: false,
          hasHourly: false,
          monthsCount: 0,
          hoursCount: 0,
        }),
      ).toBe('all');
    });

    it('falls back from hourly to all or monthly when hourly grids are absent', () => {
      expect(
        reconcilePlaybackModeWithAvailability('hourly', {
          hasTotal: true,
          hasMonthly: true,
          hasHourly: false,
          monthsCount: 12,
          hoursCount: 0,
        }),
      ).toBe('all');
      expect(
        reconcilePlaybackModeWithAvailability('hourly', {
          hasTotal: false,
          hasMonthly: true,
          hasHourly: false,
          monthsCount: 12,
          hoursCount: 0,
        }),
      ).toBe('monthly');
      expect(
        reconcilePlaybackModeWithAvailability('hourly', {
          hasTotal: false,
          hasMonthly: false,
          hasHourly: false,
          monthsCount: 0,
          hoursCount: 0,
        }),
      ).toBe('all');
    });

    it('falls back from all to monthly or hourly when yearly total is absent', () => {
      expect(
        reconcilePlaybackModeWithAvailability('all', {
          hasTotal: false,
          hasMonthly: true,
          hasHourly: true,
          monthsCount: 12,
          hoursCount: 24,
        }),
      ).toBe('monthly');
      expect(
        reconcilePlaybackModeWithAvailability('all', {
          hasTotal: false,
          hasMonthly: false,
          hasHourly: true,
          monthsCount: 0,
          hoursCount: 24,
        }),
      ).toBe('hourly');
      expect(
        reconcilePlaybackModeWithAvailability('all', {
          hasTotal: false,
          hasMonthly: false,
          hasHourly: false,
          monthsCount: 0,
          hoursCount: 0,
        }),
      ).toBe('all');
    });
  });

  describe('isSelectedFirInternational', () => {
    it('matches full FIR_IDs and 4-letter ICAO prefixes symmetrically', () => {
      expect(
        isSelectedFirInternational('KZAK_OAKLAND_OCEANIC', [
          'KZAK_OAKLAND_OCEANIC',
        ]),
      ).toBe(true);
      expect(isSelectedFirInternational('kzak', ['KZAK_OAKLAND_OCEANIC'])).toBe(
        true,
      );
      expect(isSelectedFirInternational('KZAK_OAKLAND_OCEANIC', ['KZAK'])).toBe(
        true,
      );
      expect(
        isSelectedFirInternational('KZLA_LOS_ANGELES', [
          'KZAK_OAKLAND_OCEANIC',
        ]),
      ).toBe(false);
      expect(isSelectedFirInternational(null, ['KZAK_OAKLAND_OCEANIC'])).toBe(
        false,
      );
      expect(
        isSelectedFirInternational('KZAK_OAKLAND_OCEANIC', undefined),
      ).toBe(false);
    });
  });

  describe('revokeBlobUrlIfSafe', () => {
    it('revokes only unprotected blob: URLs and ignores protected or non-blob URLs', () => {
      const revokedUrls: string[] = [];
      spyOn(URL, 'revokeObjectURL').and.callFake((url: string) => {
        revokedUrls.push(url);
      });

      const protectedUrl = 'blob:https://example.com/active';
      revokeBlobUrlIfSafe(null, protectedUrl);
      revokeBlobUrlIfSafe(undefined, protectedUrl);
      revokeBlobUrlIfSafe('data:image/png;base64,AAA', protectedUrl);
      revokeBlobUrlIfSafe('blob:https://example.com/active', protectedUrl);
      revokeBlobUrlIfSafe('blob:https://example.com/evicted', protectedUrl);
      revokeBlobUrlIfSafe('blob:https://example.com/standalone');

      expect(revokedUrls).toEqual([
        'blob:https://example.com/evicted',
        'blob:https://example.com/standalone',
      ]);
    });
  });
});
