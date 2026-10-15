import {
  installBrowserGlobalStubs,
  restoreBrowserGlobals,
} from './test_browser_globals';

// Load browser global stubs above before importing modules under test.
import 'jasmine';
import {
  clearCountryDataCache,
  FirRecord,
  getCountryMetrics,
  HeroScopeMetrics,
} from './CountryMetadata';
import {
  computeChipWidth,
  computeDropdownWidth,
  computeFirIndex,
  getFirTypeDescription,
  hasDisplayValue,
  HeroImpactCard,
  ModalShell,
  parseRankWithZeroDenominator,
  RankClickableBadge,
  resetTextMeasureCacheForTesting,
  resolveSettledLoadingState,
} from './metrics_panel_widgets';

let mockUseStateValue = false;
const effectCleanups: Array<() => void> = [];
const windowKeydownListeners = new Set<(e: KeyboardEvent) => void>();
const globalEnv = globalThis as unknown as Record<string, unknown>;

function configureWidgetTestGlobals(): void {
  installBrowserGlobalStubs({react: true, materialUi: true});
  globalEnv['addEventListener'] = (
    type: string,
    cb: (e: KeyboardEvent) => void,
  ) => {
    if (type === 'keydown') {
      windowKeydownListeners.add(cb);
    }
  };
  globalEnv['removeEventListener'] = (
    type: string,
    cb: (e: KeyboardEvent) => void,
  ) => {
    if (type === 'keydown') {
      windowKeydownListeners.delete(cb);
    }
  };
  Object.assign(globalEnv['React'] as Record<string, unknown>, {
    'Component': class {},
    'Fragment': 'React.Fragment',
    'createElement': (
      type: unknown,
      props: object | null,
      ...children: unknown[]
    ): unknown => {
      const mergedProps = {...(props ?? {}), children};
      if (typeof type === 'function') {
        return (type as (p: object) => unknown)(mergedProps);
      }
      return {type, props: mergedProps};
    },
    'useEffect': (effect: () => void | (() => void)) => {
      const cleanup = effect();
      if (typeof cleanup === 'function') {
        effectCleanups.push(cleanup);
      }
    },
    'useState': (initial: boolean | (() => boolean)) => [
      mockUseStateValue !== false
        ? mockUseStateValue
        : typeof initial === 'function'
          ? initial()
          : initial,
      (next: boolean | ((prev: boolean) => boolean)) => {
        mockUseStateValue =
          typeof next === 'function' ? next(mockUseStateValue) : next;
      },
    ],
  });
}

describe('metrics_panel_widgets', () => {
  beforeAll(() => {
    configureWidgetTestGlobals();
  });

  afterAll(() => {
    restoreBrowserGlobals();
  });

  afterEach(() => {
    clearCountryDataCache();
    mockUseStateValue = false;
    while (effectCleanups.length > 0) {
      effectCleanups.pop()?.();
    }
    windowKeydownListeners.clear();
  });

  describe('hasDisplayValue', () => {
    it('identifies displayable metric values vs empty or N/A placeholders', () => {
      expect(hasDisplayValue('12.3 Mt CO₂e')).toBeTrue();
      expect(hasDisplayValue(undefined)).toBeFalse();
      expect(hasDisplayValue('')).toBeFalse();
      expect(hasDisplayValue('N/A')).toBeFalse();
    });
  });

  describe('computeFirIndex', () => {
    const mockFirRecords = [
      {
        firId: 'PAZA_ANCHORAGE_OCEANIC',
        firIcao: 'PAZA',
        firName: 'ANCHORAGE OCEANIC',
        type: 'international',
      },
      {
        firId: 'KZAK_OAKLAND_OCEANIC_EAST',
        firIcao: 'KZAK',
        firName: 'OAKLAND OCEANIC EAST',
        type: 'international',
      },
      {
        firId: 'KZAK_OAKLAND_OCEANIC_WEST',
        firIcao: 'KZAK',
        firName: 'OAKLAND OCEANIC WEST',
        type: 'international',
      },
      {
        firId: 'KZNY_NEW_YORK_OCEANIC',
        firIcao: 'KZNY',
        firName: 'NEW YORK OCEANIC',
        type: 'international',
      },
      {
        firId: 'PAZA_ANCHORAGE_CONTINENTAL',
        firIcao: 'PAZA',
        firName: 'ANCHORAGE CONTINENTAL',
        type: 'domestic',
      },
    ];

    it('resolves multi-sector FIRs sharing an ICAO code strictly by firId (case-insensitive)', () => {
      expect(
        computeFirIndex(mockFirRecords, 0, 'KZAK_OAKLAND_OCEANIC_WEST'),
      ).toBe(2);
      expect(
        computeFirIndex(mockFirRecords, 0, 'kzak_oakland_oceanic_west'),
      ).toBe(2);
      expect(
        computeFirIndex(mockFirRecords, 0, 'KZAK_OAKLAND_OCEANIC_EAST'),
      ).toBe(1);
    });

    it('resolves PAZA Oceanic vs Continental strictly by firId', () => {
      expect(computeFirIndex(mockFirRecords, 4, 'PAZA_ANCHORAGE_OCEANIC')).toBe(
        0,
      );
      expect(
        computeFirIndex(mockFirRecords, 0, 'PAZA_ANCHORAGE_CONTINENTAL'),
      ).toBe(4);
    });

    it('resolves to matching firId index when selectedFirId changes externally', () => {
      expect(computeFirIndex(mockFirRecords, 2, 'KZNY_NEW_YORK_OCEANIC')).toBe(
        3,
      );
    });

    it('handles empty records or null selectedFirId gracefully', () => {
      expect(computeFirIndex([], 0, 'KZAK_OAKLAND_OCEANIC_WEST')).toBe(0);
      expect(computeFirIndex(undefined, 0, 'KZAK_OAKLAND_OCEANIC_WEST')).toBe(
        0,
      );
      expect(computeFirIndex(mockFirRecords, 3, null)).toBe(3);
      expect(computeFirIndex(mockFirRecords, 3, undefined)).toBe(3);
    });

    it('clamps out-of-bounds selectedFirIdx safely', () => {
      expect(computeFirIndex(mockFirRecords, 99, null)).toBe(0);
    });
  });

  describe('computeChipWidth and computeDropdownWidth', () => {
    let installedMockDocument = false;

    beforeEach(() => {
      resetTextMeasureCacheForTesting();
      if (typeof document === 'undefined') {
        installedMockDocument = true;
        (globalThis as unknown as Record<string, unknown>)['document'] = {
          createElement: () => null,
        };
      }
    });

    afterEach(() => {
      resetTextMeasureCacheForTesting();
      if (installedMockDocument) {
        delete (globalThis as unknown as Record<string, unknown>)['document'];
        installedMockDocument = false;
      }
    });

    it('measures with a canvas 2D context when available and caches by text', () => {
      const measureText = jasmine
        .createSpy('measureText')
        .and.returnValue({width: 100});
      spyOn(document, 'createElement').and.returnValue({
        getContext: () => ({font: '', measureText}),
      } as unknown as HTMLElement);

      expect(computeDropdownWidth(3)).toBe(Math.ceil(100 * 1.05 + 66));
      expect(computeDropdownWidth(3)).toBe(Math.ceil(100 * 1.05 + 66));
      expect(measureText).toHaveBeenCalledTimes(1);
    });

    it('re-measures after resetTextMeasureCacheForTesting', () => {
      const measureText = jasmine
        .createSpy('measureText')
        .and.returnValues({width: 100}, {width: 200});
      spyOn(document, 'createElement').and.returnValue({
        getContext: () => ({font: '', measureText}),
      } as unknown as HTMLElement);
      expect(computeDropdownWidth(3)).toBe(Math.ceil(100 * 1.05 + 66));

      resetTextMeasureCacheForTesting();

      expect(computeDropdownWidth(3)).toBe(Math.ceil(200 * 1.05 + 66));
    });

    it('computes chip width including safety margin for text and percentage', () => {
      const baseFir: FirRecord = {
        firId: 'PAZA_ANCHORAGE_CONTINENTAL',
        firName: 'ANCHORAGE CONTINENTAL',
        firIcao: 'PAZA',
        type: 'domestic',
        areaSqKm: 1000,
        co2eTonnes: 1000,
        meanRfMwPerSqM: 1.5,
      };
      const fir: FirRecord = {
        ...baseFir,
        percentOfFirTotal: '23%',
      };
      const widthWithPct = computeChipWidth(fir, 0);
      const widthNoPct = computeChipWidth(baseFir, 0);
      expect(widthWithPct).toBeGreaterThan(widthNoPct);
      expect(widthWithPct).toBeGreaterThan(50);
    });

    it('computes dropdown width with safety margin', () => {
      const dropdownWidth = computeDropdownWidth(16);
      expect(dropdownWidth).toBe(177);
      expect(dropdownWidth).toBeGreaterThan(computeDropdownWidth(2));
    });
  });

  describe('resolveSettledLoadingState', () => {
    it('returns raw CountryData unchanged when fetchSettled is false', () => {
      const raw = getCountryMetrics('United States', 2025);
      expect(raw.isLoading).toBeTrue();
      expect(raw.aboveCountryHero.absoluteValue).toBe('N/A');
      const unfinalized = resolveSettledLoadingState(raw, false);
      expect(unfinalized).toBe(raw);
      expect(unfinalized.isLoading).toBeTrue();
      expect(unfinalized.aboveCountryHero.absoluteValue).toBe('N/A');
      expect(unfinalized.totalFirCount).toBeNull();
    });

    it('clears isLoading while preserving N/A defaults when fetchSettled is true', () => {
      const raw = getCountryMetrics('United States', 2025);
      const finalized = resolveSettledLoadingState(raw, true);
      expect(finalized.isLoading).toBeFalse();
      expect(finalized.aboveCountryHero.absoluteValue).toBe('N/A');
      expect(finalized.impactDensity).toBe('N/A');
      expect(finalized.aboveCountryHero.perCapitaValue).toBe('N/A');
      expect(finalized.aboveCountryHero.nationalGhgComparison).toBe('N/A');
      expect(finalized.outgoingHero.absoluteValue).toBe('N/A');
      expect(finalized.firTotalHero.absoluteValue).toBe('N/A');
      // No roster size is invented when the totals never loaded.
      expect(finalized.totalFirCount).toBeNull();
      expect(finalized.totalCountryCount).toBeNull();
    });
  });

  describe('parseRankWithZeroDenominator', () => {
    it('extracts prefix and suffix when rank denominator is 0', () => {
      expect(parseRankWithZeroDenominator('#12 / 0 global FIRs')).toEqual({
        prefix: '#12 / ',
        suffix: ' global FIRs',
      });
      expect(parseRankWithZeroDenominator('#5 / 0 countries')).toEqual({
        prefix: '#5 / ',
        suffix: ' countries',
      });
      expect(parseRankWithZeroDenominator('#1 / 0')).toEqual({
        prefix: '#1 / ',
        suffix: '',
      });
    });

    it('returns null when rank denominator is non-zero or string is not a rank', () => {
      expect(parseRankWithZeroDenominator('#12 / 40 global FIRs')).toBeNull();
      expect(parseRankWithZeroDenominator('#10 / 200 countries')).toBeNull();
      expect(parseRankWithZeroDenominator('Loading...')).toBeNull();
      expect(parseRankWithZeroDenominator('N/A')).toBeNull();
    });
  });

  describe('getFirTypeDescription', () => {
    it('returns domestic and international info modal descriptions', () => {
      expect(getFirTypeDescription('domestic')).toBe('FIR over the country.');
      expect(getFirTypeDescription('DOMESTIC')).toBe('FIR over the country.');
      expect(getFirTypeDescription('international')).toBe(
        'FIR over international waters.',
      );
      expect(getFirTypeDescription('INTERNATIONAL')).toBe(
        'FIR over international waters.',
      );
    });
  });

  describe('HeroImpactCard', () => {
    const sampleMetrics: HeroScopeMetrics = {
      absoluteValue: '12.3 Mt CO₂e',
      absoluteRank: '#4 / 195 countries',
      absoluteRankNum: 4,
      perCapitaValue: '1.4 t CO₂e',
      perCapitaRank: '#8 / 195 countries',
      perCapitaRankNum: 8,
      perCapitaVsWorldAvg: '2.1x world average',
      populationFormatted: '8.9M people',
      nationalGhgComparison: '3.2%',
      nationalGhgRank: '#6 / 195 countries',
      nationalGhgRankNum: 6,
      nationalGhgTotal: '42.0 Mt CO₂e',
      totalCount: 195,
    };

    it('renders country variant with collapsed National GHG tile by default and expands on state toggle', () => {
      const collapsedTree = HeroImpactCard({
        title: 'Above country',
        metrics: sampleMetrics,
        variant: 'country',
      });
      const collapsedJson = JSON.stringify(collapsedTree);
      expect(collapsedJson).toContain('Above country');
      expect(collapsedJson).toContain('Expand more metrics');
      expect(collapsedJson).not.toContain('Per capita');
      expect(collapsedJson).not.toContain('More metrics');

      mockUseStateValue = true;
      const expandedTree = HeroImpactCard({
        title: 'Above country',
        metrics: sampleMetrics,
        variant: 'country',
      });
      const expandedJson = JSON.stringify(expandedTree);
      expect(expandedJson).toContain('Collapse metrics');
      expect(expandedJson).toContain('Per capita');
      expect(expandedJson).not.toContain('Expand more metrics');
    });

    it('renders fir variant with More metrics toggle instead of National GHG tile', () => {
      const firTree = HeroImpactCard({
        title: 'Total contrail warming impact',
        metrics: {
          absoluteValue: '5.0 Mt CO₂e',
          absoluteRank: '#2 / 298 global FIRs',
          absoluteRankNum: 2,
          totalCount: 298,
        },
        variant: 'fir',
        optionalMetrics: 'extra-fir-metrics',
      });
      const firJson = JSON.stringify(firTree);
      expect(firJson).toContain('Total contrail warming impact');
      expect(firJson).toContain('More metrics');
      expect(firJson).not.toContain('vs. National GHG');
      expect(firJson).not.toContain('Expand more metrics');
    });
  });

  describe('ModalShell', () => {
    it('returns null without registering keydown listeners when closed', () => {
      const onClose = jasmine.createSpy('onClose');
      const tree = ModalShell({
        isOpen: false,
        onClose,
        top: 20,
        left: 30,
        width: 460,
        height: 580,
        headerContent: 'Header title',
        children: 'Body content',
      });
      expect(tree).toBeNull();
      expect(windowKeydownListeners.size).toBe(0);
    });

    it('renders headerContent, headerControls, children, footer, and custom close button styling', () => {
      const onClose = jasmine.createSpy('onClose');
      const tree = ModalShell({
        isOpen: true,
        onClose,
        top: 40,
        left: 50,
        width: 460,
        height: 580,
        headerPadding: '16px 20px 12px 20px',
        closeButtonVariant: 'transparent',
        closeButtonTitle: 'Close ranking window (Esc)',
        closeButtonAriaLabel: 'Close rankings',
        headerContent: 'Header title',
        headerControls: 'Search bar slot',
        footer: 'Footer info slot',
        children: 'Body content',
      });
      const json = JSON.stringify(tree);
      expect(json).toContain('Header title');
      expect(json).toContain('Search bar slot');
      expect(json).toContain('Body content');
      expect(json).toContain('Footer info slot');
      expect(json).toContain('16px 20px 12px 20px');
      expect(json).toContain('Close rankings');
      expect(json).toContain('"width":20,"height":20');
      expect(json).toContain('"backgroundColor":"transparent"');
    });

    it('invokes onClose on backdrop click and stops propagation on card click', () => {
      const onClose = jasmine.createSpy('onClose');
      const tree = ModalShell({
        isOpen: true,
        onClose,
        top: 20,
        left: 30,
        width: 470,
        height: 280,
        headerContent: 'Header title',
        children: 'Body content',
      }) as unknown as {
        props: {
          onClick: () => void;
          children: Array<{
            props: {onClick?: (e: {stopPropagation: () => void}) => void};
          }>;
        };
      };

      const stopPropagation = jasmine.createSpy('stopPropagation');
      const cardNode = tree.props.children[1];
      cardNode.props.onClick?.({stopPropagation});
      expect(stopPropagation).toHaveBeenCalledTimes(1);
      expect(onClose).not.toHaveBeenCalled();

      tree.props.onClick();
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('invokes onClose on Escape keydown and unregisters listener on cleanup', () => {
      const onClose = jasmine.createSpy('onClose');
      ModalShell({
        isOpen: true,
        onClose,
        top: 20,
        left: 30,
        width: 470,
        height: 280,
        headerContent: 'Header title',
        children: 'Body content',
      });
      expect(windowKeydownListeners.size).toBe(1);

      for (const listener of windowKeydownListeners) {
        listener({key: 'Enter'} as KeyboardEvent);
      }
      expect(onClose).not.toHaveBeenCalled();

      for (const listener of windowKeydownListeners) {
        listener({key: 'Escape'} as KeyboardEvent);
      }
      expect(onClose).toHaveBeenCalledTimes(1);

      while (effectCleanups.length > 0) {
        effectCleanups.pop()?.();
      }
      expect(windowKeydownListeners.size).toBe(0);
    });
  });

  describe('RankClickableBadge', () => {
    it('renders red top-25, neutral unknown-total, and green parsed-total badges when totalCount prop is omitted', () => {
      const topRankJson = JSON.stringify(
        RankClickableBadge({
          text: '(#5)',
          rankNum: 5,
        }),
      );
      expect(topRankJson).toContain('"color":"#c5221f"');
      expect(topRankJson).toContain('"backgroundColor":"#fce8e6"');

      const nonTopRankJson = JSON.stringify(
        RankClickableBadge({
          text: '(#40)',
          rankNum: 40,
        }),
      );
      expect(nonTopRankJson).toContain('"color":"#5f6368"');
      expect(nonTopRankJson).toContain('"backgroundColor":"#f1f3f4"');

      const bottomRankJson = JSON.stringify(
        RankClickableBadge({
          text: '(#170 / 177 countries)',
          rankNum: 170,
        }),
      );
      expect(bottomRankJson).toContain('"color":"#137333"');
      expect(bottomRankJson).toContain('"backgroundColor":"#e6f4ea"');
    });
  });
});
