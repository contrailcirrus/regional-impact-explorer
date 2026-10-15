import {
  installBrowserGlobalStubs,
  restoreBrowserGlobals,
} from './test_browser_globals';

// Load browser global stubs above before importing modules under test.
import 'jasmine';
import {CountryOption, clearCountryDataCache} from './CountryMetadata';
import {
  COUNTRY_LIST_ERROR_TEXT,
  COUNTRY_LIST_LOADING_TEXT,
  emptyCountryListMessage,
  filterCountryOptions,
  parseSelectedYear,
  reconcileSelectedYear,
  resolveSelectedCountryName,
} from './RegionalFilterDrawer';

describe('RegionalFilterDrawer Unit Tests', () => {
  beforeAll(() => {
    installBrowserGlobalStubs({react: true, materialUi: true});
  });

  afterAll(() => {
    restoreBrowserGlobals();
  });

  afterEach(() => {
    clearCountryDataCache();
  });

  describe('reconcileSelectedYear', () => {
    it('keeps a selection the backend still lists', () => {
      expect(reconcileSelectedYear(2031, [2030, 2031, 2032])).toBe(2031);
    });

    it('falls back to the latest listed year for an unlisted or invalid selection', () => {
      expect(reconcileSelectedYear(1999, [2030, 2032, 2031])).toBe(2032);
      expect(reconcileSelectedYear(0, [2030, 2031])).toBe(2031);
    });

    it('returns null when nothing is published', () => {
      expect(reconcileSelectedYear(2031, [])).toBeNull();
      expect(reconcileSelectedYear(0, [])).toBeNull();
    });

    it('does not mutate the year list', () => {
      const years = [2032, 2030];
      reconcileSelectedYear(0, years);
      expect(years).toEqual([2032, 2030]);
    });
  });

  describe('parseSelectedYear', () => {
    it('accepts four-digit integer years as numbers or strings', () => {
      expect(parseSelectedYear(2025)).toBe(2025);
      expect(parseSelectedYear('2025')).toBe(2025);
      expect(parseSelectedYear(' 2024 ')).toBe(2024);
    });

    it('rejects empty, whitespace, non-integer, or out-of-range values', () => {
      expect(parseSelectedYear('')).toBeNull();
      expect(parseSelectedYear('   ')).toBeNull();
      expect(parseSelectedYear(0)).toBeNull();
      expect(parseSelectedYear('0')).toBeNull();
      expect(parseSelectedYear(99)).toBeNull();
      expect(parseSelectedYear('99')).toBeNull();
      expect(parseSelectedYear(10000)).toBeNull();
      expect(parseSelectedYear(2025.5)).toBeNull();
      expect(parseSelectedYear('2025.5')).toBeNull();
      expect(parseSelectedYear(Number.NaN)).toBeNull();
      expect(parseSelectedYear(null)).toBeNull();
      expect(parseSelectedYear(undefined)).toBeNull();
    });
  });
  describe('Searchable Country Picker Logic', () => {
    const mockCountries: CountryOption[] = [
      {name: 'Germany', isoA2: 'DE'},
      {name: 'France', isoA2: 'FR'},
      {name: 'Switzerland', isoA2: 'CH'},
    ];

    it('resolves country display name from ISO code', () => {
      expect(resolveSelectedCountryName('CH', mockCountries)).toBe(
        'Switzerland',
      );
      expect(resolveSelectedCountryName('de', mockCountries)).toBe('Germany');
      expect(resolveSelectedCountryName('FR', mockCountries)).toBe('France');
    });

    it('falls back to the raw selection when country is not in list', () => {
      expect(resolveSelectedCountryName('US', mockCountries)).toBe('US');
      expect(resolveSelectedCountryName('UnknownLand', mockCountries)).toBe(
        'UnknownLand',
      );
    });

    it('shows the loading text for an unresolved selection while loading', () => {
      expect(resolveSelectedCountryName('US', [], 'loading')).toBe(
        COUNTRY_LIST_LOADING_TEXT,
      );
      expect(resolveSelectedCountryName('CH', mockCountries, 'loading')).toBe(
        'Switzerland',
      );
    });

    it('shows the list status while no country is selected', () => {
      expect(resolveSelectedCountryName('', [], 'loading')).toBe(
        COUNTRY_LIST_LOADING_TEXT,
      );
      expect(resolveSelectedCountryName('', [], 'error')).toBe(
        COUNTRY_LIST_ERROR_TEXT,
      );
    });

    it('picks the empty-picker message from the list status', () => {
      expect(emptyCountryListMessage('loading')).toBe(
        COUNTRY_LIST_LOADING_TEXT,
      );
      expect(emptyCountryListMessage('error')).toBe(COUNTRY_LIST_ERROR_TEXT);
      expect(emptyCountryListMessage('ready')).toBe('No country found');
    });

    it('filters and sorts country options alphabetically', () => {
      const allSorted = filterCountryOptions(mockCountries, '');
      expect(allSorted.map((c) => c.name)).toEqual([
        'France',
        'Germany',
        'Switzerland',
      ]);

      const filtered = filterCountryOptions(mockCountries, 'switz');
      expect(filtered).toEqual([mockCountries[2]]);

      const filteredIso = filterCountryOptions(mockCountries, 'fr');
      expect(filteredIso).toEqual([mockCountries[1]]);

      const noMatch = filterCountryOptions(mockCountries, 'XYZ123');
      expect(noMatch).toEqual([]);
    });

    it('disambiguates sentinel "-99" territories by display name rather than sharing "-99"', () => {
      const sentinelCountries: CountryOption[] = [
        ...mockCountries,
        {name: 'Kosovo', isoA2: '-99'},
        {name: 'Northern Cyprus', isoA2: '-99'},
        {name: 'Somaliland', isoA2: '-99'},
      ];
      expect(resolveSelectedCountryName('Kosovo', sentinelCountries)).toBe(
        'Kosovo',
      );
      expect(
        resolveSelectedCountryName('Northern Cyprus', sentinelCountries),
      ).toBe('Northern Cyprus');
      expect(resolveSelectedCountryName('Somaliland', sentinelCountries)).toBe(
        'Somaliland',
      );
    });
  });
});
