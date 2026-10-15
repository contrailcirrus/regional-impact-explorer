import {
  installBrowserGlobalStubs,
  restoreBrowserGlobals,
} from './test_browser_globals';

// Load browser global stubs above before importing modules under test.
import 'jasmine';
import {RankingItem} from './CountryMetadata';
import {
  calculateTargetScroll,
  countRankedItems,
  filterRankingItems,
  formatRankBadgeLabel,
  formatRankingSubtitle,
  isRankingItemSelected,
  resolveRankingTotalCount,
} from './CountryRankingsPopup';
import {getRankBadgeColors} from './metrics_panel_widgets';

describe('CountryRankingsPopup Logic Unit Tests', () => {
  beforeAll(() => {
    installBrowserGlobalStubs({react: true, materialUi: true});
  });

  afterAll(() => {
    restoreBrowserGlobals();
  });

  const sampleItems: RankingItem[] = [
    {rank: 1, name: 'Switzerland', isoA2: 'CH', value: 50.2},
    {rank: 2, name: 'Germany', isoA2: 'DE', value: 45.1},
    {
      rank: 3,
      name: 'United States',
      isoA2: 'US',
      value: 40.0,
      secondaryValue: 1.2,
    },
    {rank: 170, name: 'Tuvalu', isoA2: 'TV', value: 0.1},
  ];

  describe('Search Filtering Logic', () => {
    it('returns all items when search query is empty', () => {
      expect(filterRankingItems(sampleItems, '')).toEqual(sampleItems);
      expect(filterRankingItems(sampleItems, '   ')).toEqual(sampleItems);
    });

    it('filters items by country name case-insensitively', () => {
      const filtered = filterRankingItems(sampleItems, 'switzerland');
      expect(filtered).toEqual([sampleItems[0]]);

      const filteredDe = filterRankingItems(sampleItems, 'GERMAN');
      expect(filteredDe).toEqual([sampleItems[1]]);
    });

    it('filters items by ISO code case-insensitively', () => {
      const filteredCh = filterRankingItems(sampleItems, 'ch');
      expect(filteredCh).toEqual([sampleItems[0]]);

      const filteredUs = filterRankingItems(sampleItems, 'US');
      expect(filteredUs).toEqual([sampleItems[2]]);
    });

    it('filters items by rank number', () => {
      const filtered170 = filterRankingItems(sampleItems, '170');
      expect(filtered170).toEqual([sampleItems[3]]);

      const filtered1 = filterRankingItems(sampleItems, '1');
      // rank 1 and rank 170 contain '1'
      expect(filtered1).toEqual([sampleItems[0], sampleItems[3]]);
    });

    it('returns empty array when no match is found', () => {
      const filteredNone = filterRankingItems(sampleItems, 'NonExistentXYZ');
      expect(filteredNone).toEqual([]);
    });

    it('does not throw when ranking items contain nullish name or isoA2 fields', () => {
      const malformedItems: RankingItem[] = [
        {
          rank: 1,
          name: undefined as unknown as string,
          isoA2: undefined as unknown as string,
          value: 10,
        },
        {rank: 2, name: 'ValidCountry', isoA2: 'VC', value: 5},
      ];
      expect(filterRankingItems(malformedItems, 'valid')).toEqual([
        malformedItems[1],
      ]);
    });
  });

  describe('Unranked items (no value, e.g. per-area metric without area)', () => {
    const itemsWithUnranked: RankingItem[] = [
      ...sampleItems,
      {rank: null, name: 'Singapore', isoA2: 'SG', value: null},
      {rank: null, name: 'Nullland', isoA2: 'NL', value: null},
    ];

    it('does not match a search for "null"-like text or any rank number', () => {
      expect(filterRankingItems(itemsWithUnranked, 'null')).toEqual([
        itemsWithUnranked[5],
      ]);
      expect(filterRankingItems(itemsWithUnranked, '1')).toEqual([
        sampleItems[0],
        sampleItems[3],
      ]);
      expect(filterRankingItems(itemsWithUnranked, 'singapore')).toEqual([
        itemsWithUnranked[4],
      ]);
    });

    it('excludes unranked items from the rank denominator', () => {
      expect(countRankedItems(itemsWithUnranked)).toBe(4);
      expect(countRankedItems([])).toBe(0);
    });

    it('labels the badge N/A instead of inventing a rank', () => {
      expect(formatRankBadgeLabel(7)).toBe('#7');
      expect(formatRankBadgeLabel(null)).toBe('N/A');
      expect(formatRankBadgeLabel(undefined)).toBe('N/A');
      expect(formatRankBadgeLabel(Number.NaN)).toBe('N/A');
    });

    it('gives unranked items the neutral badge, never the top or bottom band', () => {
      const grey = getRankBadgeColors(80, 177);
      expect(getRankBadgeColors(Number.NaN, 177)).toEqual(grey);
      expect(getRankBadgeColors(Number.NaN, null)).toEqual(grey);
    });
  });

  describe('FIR Multi-Country Filtering & Data Logic', () => {
    const sampleFirItems: RankingItem[] = [
      {
        rank: 1,
        name: 'LANGEN',
        firId: 'EDGG_LANGEN',
        firName: 'LANGEN',
        firIcao: 'EDGG',
        isoA2: 'DE',
        type: 'domestic',
        countryName: 'Germany',
        countries: [{name: 'Germany', isoA2: 'DE'}],
        value: 12.5,
      },
      {
        rank: 2,
        name: 'SWITZERLAND',
        firId: 'LSAS_SWITZERLAND',
        firName: 'SWITZERLAND',
        firIcao: 'LSAS',
        isoA2: 'CH',
        type: 'domestic',
        countryName: 'Switzerland',
        countries: [
          {name: 'Switzerland', isoA2: 'CH'},
          {name: 'France', isoA2: 'FR'},
          {name: 'Germany', isoA2: 'DE'},
          {name: 'Italy', isoA2: 'IT'},
        ],
        value: 10.1,
      },
    ];

    it('filters FIR items by secondary managing country name or ISO', () => {
      const matchItaly = filterRankingItems(sampleFirItems, 'Italy');
      expect(matchItaly).toEqual([sampleFirItems[1]]);

      const matchFR = filterRankingItems(sampleFirItems, 'FR');
      expect(matchFR).toEqual([sampleFirItems[1]]);
    });

    it('matches FIR by ICAO code', () => {
      const matchEDGG = filterRankingItems(sampleFirItems, 'edgg');
      expect(matchEDGG).toEqual([sampleFirItems[0]]);
    });
  });

  describe('Rank badge styling across dataset boundaries', () => {
    it('applies correct badge colors for top, middle, and bottom ranks', () => {
      const topBadge = getRankBadgeColors(1, 177);
      expect(topBadge).toEqual({
        color: '#c5221f',
        bgColor: '#fce8e6',
        hoverBgColor: '#fad2cf',
        borderColor: '#fad2cf',
      });

      const top25Badge = getRankBadgeColors(25, 177);
      expect(top25Badge).toEqual({
        color: '#c5221f',
        bgColor: '#fce8e6',
        hoverBgColor: '#fad2cf',
        borderColor: '#fad2cf',
      });

      const midBadge = getRankBadgeColors(80, 177);
      expect(midBadge).toEqual({
        color: '#5f6368',
        bgColor: '#f1f3f4',
        hoverBgColor: '#e8eaed',
        borderColor: '#dadce0',
      });

      const bottomBadge = getRankBadgeColors(160, 177);
      expect(bottomBadge).toEqual({
        color: '#137333',
        bgColor: '#e6f4ea',
        hoverBgColor: '#ceead6',
        borderColor: '#ceead6',
      });

      const lastBadge = getRankBadgeColors(177, 177);
      expect(lastBadge).toEqual({
        color: '#137333',
        bgColor: '#e6f4ea',
        hoverBgColor: '#ceead6',
        borderColor: '#ceead6',
      });

      // With a zero total only the total-independent top-25 band is coloured;
      // nothing is falsely marked green.
      expect(getRankBadgeColors(40, 0)).toEqual(midBadge);
      expect(getRankBadgeColors(10, 0)).toEqual(topBadge);
    });

    it('keeps top-25 red when the total is unknown', () => {
      const red = getRankBadgeColors(1, 177);
      expect(getRankBadgeColors(1)).toEqual(red);
      expect(getRankBadgeColors(1, null)).toEqual(red);
      expect(getRankBadgeColors(10, undefined)).toEqual(red);
      expect(getRankBadgeColors(25)).toEqual(red);
      expect(getRankBadgeColors(25, null)).toEqual(red);
    });

    it('falls back to grey for ranks above 25 or invalid when total is unknown', () => {
      const grey = getRankBadgeColors(80, 177);
      expect(getRankBadgeColors(26)).toEqual(grey);
      expect(getRankBadgeColors(210, null)).toEqual(grey);
      expect(getRankBadgeColors(0)).toEqual(grey);
      expect(getRankBadgeColors(Number.NaN)).toEqual(grey);
      expect(getRankBadgeColors(170, 177)).not.toEqual(grey);
    });

    it('names the entity count in the subtitle only when it is known', () => {
      expect(formatRankingSubtitle(180, 'countries')).toBe(
        'Full global ranking of 180 countries',
      );
      expect(formatRankingSubtitle(12, 'global FIRs')).toBe(
        'Full global ranking of 12 global FIRs',
      );
      expect(formatRankingSubtitle(null, 'countries')).toBe(
        'Full global ranking of countries',
      );
    });

    it('resolves the ranked total count excluding unranked items for both header subtitle and row badges', () => {
      const itemsWithUnranked: RankingItem[] = [
        {rank: 1, name: 'Switzerland', isoA2: 'CH', value: 50.2},
        {rank: 2, name: 'Germany', isoA2: 'DE', value: 45.1},
        {rank: null, name: 'Unranked Entity', isoA2: 'XX', value: null},
      ];

      const totalRanked = resolveRankingTotalCount(
        itemsWithUnranked,
        false,
        2024,
      );
      expect(totalRanked).toBe(2);
      expect(formatRankingSubtitle(totalRanked, 'countries')).toBe(
        'Full global ranking of 2 countries',
      );
      expect(resolveRankingTotalCount(null, false, 2099)).toBeNull();
      expect(resolveRankingTotalCount(undefined, true, 2099)).toBeNull();
    });

    it('disambiguates multi-sector FIRs sharing an ICAO code strictly by firId via isRankingItemSelected', () => {
      const kzakEast: RankingItem = {
        rank: 5,
        name: 'OAKLAND OCEANIC EAST',
        firId: 'KZAK_OAKLAND_OCEANIC_EAST',
        firName: 'OAKLAND OCEANIC EAST',
        firIcao: 'KZAK',
        isoA2: 'US',
        value: 100,
      };
      const kzakWest: RankingItem = {
        rank: 12,
        name: 'OAKLAND OCEANIC WEST',
        firId: 'KZAK_OAKLAND_OCEANIC_WEST',
        firName: 'OAKLAND OCEANIC WEST',
        firIcao: 'KZAK',
        isoA2: 'US',
        value: 50,
      };

      expect(
        isRankingItemSelected({
          item: kzakWest,
          isFirRanking: true,
          selectedCountry: 'US',
          selectedFirId: 'KZAK_OAKLAND_OCEANIC_WEST',
        }),
      ).toBeTrue();

      expect(
        isRankingItemSelected({
          item: kzakEast,
          isFirRanking: true,
          selectedCountry: 'US',
          selectedFirId: 'KZAK_OAKLAND_OCEANIC_WEST',
        }),
      ).toBeFalse();
    });

    it('matches sentinel "-99" territories strictly by name without highlighting other "-99" rows', () => {
      const kosovo: RankingItem = {
        rank: 90,
        name: 'Kosovo',
        isoA2: '-99',
        value: 2.5,
      };
      const somaliland: RankingItem = {
        rank: 130,
        name: 'Somaliland',
        isoA2: '-99',
        value: 0.4,
      };

      expect(
        isRankingItemSelected({
          item: kosovo,
          isFirRanking: false,
          selectedCountry: 'Kosovo',
        }),
      ).toBeTrue();

      expect(
        isRankingItemSelected({
          item: somaliland,
          isFirRanking: false,
          selectedCountry: 'Kosovo',
        }),
      ).toBeFalse();

      expect(
        isRankingItemSelected({
          item: somaliland,
          isFirRanking: false,
          selectedCountry: '-99',
        }),
      ).toBeFalse();
    });

    it('matches FIR ranking rows by managing country without -99 collisions', () => {
      const somalilandFir: RankingItem = {
        rank: 1,
        name: 'Mogadishu FIR',
        firId: 'HCSM_MOGADISHU',
        firIcao: 'HCSM',
        firName: 'Mogadishu FIR',
        isoA2: '-99',
        countryName: 'Somaliland',
        countries: [{isoA2: '-99', name: 'Somaliland'}],
        value: 1000,
      };
      expect(
        isRankingItemSelected({
          item: somalilandFir,
          isFirRanking: true,
          selectedCountry: '-99',
        }),
      ).toBeFalse();
      expect(
        isRankingItemSelected({
          item: somalilandFir,
          isFirRanking: true,
          selectedCountry: 'Kosovo',
        }),
      ).toBeFalse();
      expect(
        isRankingItemSelected({
          item: somalilandFir,
          isFirRanking: true,
          selectedCountry: 'Somaliland',
        }),
      ).toBeTrue();
    });
  });

  describe('Scroll Target Centering Calculation', () => {
    it('centers item in container when sufficient scroll space exists', () => {
      // itemTop = 900, itemHeight = 48, containerHeight = 400
      // 900 - 200 + 24 = 724
      expect(
        calculateTargetScroll({
          itemTop: 900,
          itemHeight: 48,
          containerHeight: 400,
        }),
      ).toBe(724);
    });

    it('clamps to 0 when item is near the top of the list', () => {
      // itemTop = 50, itemHeight = 48, containerHeight = 400
      // 50 - 200 + 24 = -126 -> clamped to 0
      expect(
        calculateTargetScroll({
          itemTop: 50,
          itemHeight: 48,
          containerHeight: 400,
        }),
      ).toBe(0);
    });

    it('falls back to matching by selectedCountry when selectedFirId is null or undefined', () => {
      const firItem: RankingItem = {
        rank: 5,
        name: 'OAKLAND OCEANIC',
        firId: 'KZAK_OAKLAND_OCEANIC',
        firName: 'OAKLAND OCEANIC',
        firIcao: 'KZAK',
        isoA2: 'US',
        countryName: 'United States',
        value: 12.5,
      };
      expect(
        isRankingItemSelected({
          item: firItem,
          isFirRanking: true,
          selectedCountry: 'United States',
          selectedFirId: null,
        }),
      ).toBeTrue();
    });
  });
});
