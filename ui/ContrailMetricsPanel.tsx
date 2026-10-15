/**
 * @fileoverview Country, outgoing-flight, and Flight Information Region (FIR)
 * contrail impact metrics panel, FIR chip selector integration, and ranking and
 * time-series modal triggers.
 * @jsx React.createElement
 * @jsxFrag React.Fragment
 */

import {Box, CircularProgress, Typography} from 'material-ui';
import * as React from 'react';
import {
  CountryData,
  fetchCountryRecordFromBackend,
  fetchCountryTotalFromBackend,
  fetchFirsFromBackend,
  FirManagingCountry,
  FirRecord,
  getCountryMetrics,
  getCountryNameFromIso2,
  getIso2FromCountry,
  getManagingCountriesForFir,
  isCountrySelectionMatch,
  isValidCountryIso2,
  previousAvailableYear,
  resolveCountryIdentifier,
  subscribeToCountryCache,
} from './CountryMetadata';
import {CountryRankingsPopup} from './CountryRankingsPopup';
import {
  formatFirDisplayName,
  formatMwPerSqM,
  formatRankLabel,
  formatTonnesCo2e,
  NOT_AVAILABLE_LABEL,
} from './metric_formatters';
import {
  AboveCountryIcon,
  computeFirIndex,
  ControlledFirsIcon,
  FirChipSelector,
  FirTypeLabelWithModal,
  hasDisplayValue,
  HeroImpactCard,
  MetricValue,
  OutgoingIcon,
  OverflightIcon,
  PopupIcon,
  RadiativeForcingIcon,
  RankClickableBadge,
  resolveSettledLoadingState,
} from './metrics_panel_widgets';
import {FilterState} from './RegionalFilterDrawer';
import {TemporalImpactHeatmap} from './TemporalImpactHeatmap';
import {YoYSeriesPopup} from './YoYSeriesPopup';

/** Props for the ContrailMetricsPanel component. */
export interface ContrailMetricsPanelProps {
  readonly filters: FilterState;
  readonly onSelectCountry?: (countryName: string) => void;
  readonly onSelectFir?: (firId: string | null) => void;
  readonly selectedFirId?: string | null;
}

const ContrailMetricsPanelInner: React.FC<ContrailMetricsPanelProps> = ({
  filters,
  onSelectCountry,
  onSelectFir,
  selectedFirId,
}) => {
  const gwpLabel = filters.gwp || 'GWP50';

  const [data, setData] = React.useState<CountryData>(() =>
    getCountryMetrics(filters.country, filters.year, gwpLabel),
  );
  const [isFirListLoading, setIsFirListLoading] = React.useState<boolean>(true);
  const [selectedFirIdx, setSelectedFirIdx] = React.useState<number>(0);

  const [activeRanking, setActiveRanking] = React.useState<{
    readonly metricKey: string;
    readonly metricTitle: string;
    readonly selectedFirId?: string | null;
    readonly anchorRect: DOMRect | null;
  } | null>(null);

  /**
   * Opens the global country or FIR rankings popup anchored to
   * `e.currentTarget`. Omitting `firId` opens a country-scoped ranking without
   * highlighting the active FIR.
   */
  const openRanking = React.useCallback(
    (
      metricKey: string,
      metricTitle: string,
      e: React.MouseEvent<HTMLElement>,
      firId?: string | null,
    ) => {
      e.stopPropagation();
      const rect = e.currentTarget.getBoundingClientRect();
      setActiveRanking({
        metricKey,
        metricTitle,
        selectedFirId: firId ?? null,
        anchorRect: rect,
      });
    },
    [],
  );

  const closeRanking = React.useCallback(() => {
    setActiveRanking(null);
  }, []);

  const handleRankingSelectCountry = React.useCallback(
    (country: string) => {
      if (onSelectCountry) {
        onSelectCountry(country);
      }
    },
    [onSelectCountry],
  );

  const [activeYoYSeries, setActiveYoYSeries] = React.useState<{
    readonly metricKey: string;
    readonly metricTitle: string;
    readonly categoryTitle: string;
    readonly selectedFirId?: string | null;
    readonly anchorRect: DOMRect | null;
  } | null>(null);

  const openYoYSeries = React.useCallback(
    (
      metricKey: string,
      metricTitle: string,
      categoryTitle: string,
      e: React.MouseEvent<HTMLElement>,
      options: {
        readonly firId?: string | null;
      } = {},
    ) => {
      e.stopPropagation();
      const rect = e.currentTarget.getBoundingClientRect();
      setActiveYoYSeries({
        metricKey,
        metricTitle,
        categoryTitle,
        selectedFirId: options.firId ?? null,
        anchorRect: rect,
      });
    },
    [],
  );

  const closeYoYSeries = React.useCallback(() => {
    setActiveYoYSeries(null);
  }, []);

  const computedFirIdx = React.useMemo(() => {
    return computeFirIndex(
      data.controlledFirRecords,
      selectedFirIdx,
      selectedFirId,
    );
  }, [selectedFirId, selectedFirIdx, data.controlledFirRecords]);

  React.useEffect(() => {
    if (computedFirIdx !== selectedFirIdx) {
      setSelectedFirIdx(computedFirIdx);
    }
  }, [computedFirIdx, selectedFirIdx]);

  const selectedFirIdxRef = React.useRef(selectedFirIdx);
  React.useEffect(() => {
    selectedFirIdxRef.current = selectedFirIdx;
  }, [selectedFirIdx]);

  const selectedFirIdRef = React.useRef(selectedFirId);
  React.useEffect(() => {
    selectedFirIdRef.current = selectedFirId;
  }, [selectedFirId]);

  React.useEffect(() => {
    let active = true;
    let isFetchSettled = false;
    const currentIsoOrName = filters.country;
    const currentYear = filters.year;
    const currentGwp = filters.gwp || 'GWP50';
    const prevYear = previousAvailableYear(currentYear);

    setIsFirListLoading(true);
    const newMetrics = getCountryMetrics(
      currentIsoOrName,
      currentYear,
      currentGwp,
    );
    setData(newMetrics);

    const preservedIdx = computeFirIndex(
      newMetrics.controlledFirRecords,
      0,
      selectedFirIdRef.current,
    );
    setSelectedFirIdx(preservedIdx);
    selectedFirIdxRef.current = preservedIdx;
    if (onSelectFir) {
      const firs = newMetrics.controlledFirRecords || [];
      const chosen = firs[preservedIdx] || firs[0] || null;
      if (chosen != null) {
        onSelectFir(chosen.firId);
      }
    }

    // The FIR total is derived from the year's /firs list, so the FIR rank
    // denominator stays in its loading state until that list has arrived.
    const firListPromise = fetchFirsFromBackend(currentYear).finally(() => {
      if (active) {
        setIsFirListLoading(false);
      }
    });

    const hasCountry = Boolean(currentIsoOrName);
    const promises: Array<Promise<unknown>> = [
      // The country total is derived from the year's /countries list.
      fetchCountryTotalFromBackend(currentYear),
      firListPromise,
    ];
    if (hasCountry) {
      promises.push(
        fetchCountryRecordFromBackend(currentIsoOrName, currentYear),
      );
    }
    if (prevYear !== null && hasCountry) {
      promises.push(fetchCountryRecordFromBackend(currentIsoOrName, prevYear));
    }
    if (prevYear !== null) {
      promises.push(fetchFirsFromBackend(prevYear));
    }

    void Promise.allSettled(promises)
      .then(() => {
        if (!active) {
          return;
        }
        isFetchSettled = true;
        setIsFirListLoading(false);
        const refreshed = getCountryMetrics(
          currentIsoOrName,
          currentYear,
          currentGwp,
        );
        setData(resolveSettledLoadingState(refreshed, true));
        if (!onSelectFir) {
          return;
        }
        const firs = refreshed.controlledFirRecords || [];
        const curIdx = computeFirIndex(
          firs,
          selectedFirIdxRef.current ?? 0,
          selectedFirIdRef.current,
        );
        const chosen =
          (curIdx >= 0 && curIdx < firs.length ? firs[curIdx] : null) ||
          firs[0] ||
          null;
        if (chosen != null) {
          onSelectFir(chosen.firId);
        }
      })
      .catch((err: unknown) => {
        console.warn('Failed to refresh country metrics:', err);
      });

    const unsubscribe = subscribeToCountryCache(() => {
      if (active) {
        const updated = getCountryMetrics(
          currentIsoOrName,
          currentYear,
          currentGwp,
        );
        setData(resolveSettledLoadingState(updated, isFetchSettled));
      }
    });

    return () => {
      active = false;
      unsubscribe();
    };
  }, [filters.country, filters.year, filters.gwp]);

  // 0 renders as a loading spinner; null (the /firs list failed to load) drops
  // the "/ N" part instead of showing a made-up total.
  const firRankDenominator: number | null =
    data.isLoading || isFirListLoading ? 0 : data.totalFirCount;

  const selectedYear = filters.year;
  const isoCode = (
    data.isoA2 ||
    getIso2FromCountry(filters.country, selectedYear) ||
    ''
  ).toUpperCase();
  const gwpParam = (filters.gwp || 'GWP50').toLowerCase();
  const outgoingExplorerUrl = React.useMemo(() => {
    if (!isoCode) return '';
    const explorerUrl = new URL('https://explore.contrails.org/explorer');
    explorerUrl.search = new URLSearchParams({
      'time_start': `${selectedYear}-01`,
      'time_end': `${selectedYear}-12`,
      'departure_country_iso': isoCode,
      gwp: gwpParam,
    }).toString();
    return explorerUrl.toString();
  }, [isoCode, selectedYear, gwpParam]);

  const activeFir: FirRecord | null =
    data.controlledFirRecords && data.controlledFirRecords.length > 0
      ? data.controlledFirRecords[computedFirIdx] ||
        data.controlledFirRecords[0]
      : null;

  const countryDisplayName =
    getCountryNameFromIso2(filters.country, filters.year) ||
    data.name ||
    filters.country;

  const firHeadingTitle = activeFir
    ? formatFirDisplayName(activeFir.firName)
    : selectedFirId || '';

  return (
    <Box style={{width: '100%', paddingBottom: '0px'}}>
      {/* Country Heading just above the first tile */}
      <Box
        display="flex"
        alignItems="center"
        style={{gap: '10px', marginBottom: '12px'}}>
        <span
          style={{
            width: '8px',
            height: '24px',
            backgroundColor: '#1a73e8',
            borderRadius: '4px',
            flexShrink: 0,
          }}
        />
        <Typography
          style={{fontSize: '18px', fontWeight: 600, color: '#202124'}}>
          {countryDisplayName}
        </Typography>
      </Box>

      {/* Consolidated Metrics Tile (Above country, All outgoing flights, All
         controlled FIRs) */}
      <Box
        style={{
          backgroundColor: '#ffffff',
          borderRadius: '10px',
          border: '1px solid #dadce0',
          padding:
            data.controlledFirRecords &&
            data.controlledFirRecords.length > 1 &&
            activeFir
              ? '20px 20px 16px 20px'
              : '20px',
          marginBottom: '0px',
          boxShadow: '0 1px 3px rgba(0,0,0,0.06)',
        }}>
        <Box
          display="flex"
          flexWrap="wrap"
          style={{
            gap: '24px',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
          }}>
          {/* Column 1: Header and text */}
          <Box style={{flex: '1 1 240px', minWidth: '200px'}}>
            <Box display="flex" alignItems="center">
              <AboveCountryIcon />
              <Typography
                style={{
                  fontSize: '16px',
                  fontWeight: 600,
                  color: '#202124',
                }}>
                Above country
              </Typography>
            </Box>
            <Typography
              style={{fontSize: '13px', color: '#5f6368', marginTop: '6px'}}>
              Total contrail impact happening directly above{' '}
              {countryDisplayName}. Compared to other GHGs warming effects from
              contrails are short lived and hence have more regional impact.
              Explore below how the regional warming could be mitigated.
            </Typography>
          </Box>

          {/* Column 2: Non-optional metrics (HeroImpactCard with optional
             metrics toggle) */}
          <Box
            style={{flex: '1 1 380px', minWidth: '280px', maxWidth: '560px'}}>
            {/* Consolidated Hero Metric: Total contrail impact + Per capita +
               vs National GHG */}
            <HeroImpactCard
              title={`Total contrail impact (${gwpLabel})`}
              isLoading={Boolean(data.isLoading)}
              metrics={data.aboveCountryHero}
              onYoYClick={(e) => {
                openYoYSeries(
                  'co2e_gwp_tonnes',
                  `Total contrail impact (${gwpLabel})`,
                  'Above country',
                  e,
                );
              }}
              onAbsoluteRankClick={(e) => {
                openRanking(
                  'co2e_gwp_tonnes',
                  `Total contrail impact (${gwpLabel})`,
                  e,
                );
              }}
              onPerCapitaRankClick={(e) => {
                openRanking(
                  'co2e_gwp_tonnes_per_capita',
                  `Contrail impact per capita (${gwpLabel})`,
                  e,
                );
              }}
              onNationalGhgRankClick={(e) => {
                openRanking(
                  'co2e_gwp_vs_national_ghg',
                  `vs. national GHG emissions (${gwpLabel})`,
                  e,
                );
              }}
              optionalMetrics={
                <>
                  {/* Metric 3: Contrail mean radiative forcing value & rank */}
                  <Box style={{minWidth: '130px'}}>
                    <Box
                      display="flex"
                      alignItems="center"
                      style={{gap: '5px', marginBottom: '2px'}}>
                      <RadiativeForcingIcon />
                      <span
                        style={{
                          fontSize: '11.5px',
                          color: '#5f6368',
                          fontWeight: 600,
                        }}>
                        Contrail mean radiative forcing
                      </span>
                    </Box>
                    <Box
                      display="flex"
                      alignItems="baseline"
                      style={{gap: '4px'}}>
                      <MetricValue
                        value={data.impactDensity}
                        isLoading={Boolean(data.isLoading)}
                        size="14.5px"
                      />
                    </Box>
                    {!data.isLoading &&
                      hasDisplayValue(data.impactDensityRank) && (
                        <RankClickableBadge
                          text={data.impactDensityRank}
                          rankNum={data.densityRankNum}
                          totalCount={data.totalCountryCount ?? undefined}
                          onClick={(e) => {
                            openRanking(
                              'mean_rf_mw_per_sq_m',
                              'Contrail mean radiative forcing',
                              e,
                            );
                          }}
                        />
                      )}
                  </Box>

                  {/* Metric 4: Overflight fraction */}
                  <Box style={{minWidth: '130px'}}>
                    <Box
                      display="flex"
                      alignItems="center"
                      style={{gap: '5px', marginBottom: '2px'}}>
                      <OverflightIcon />
                      <span
                        style={{
                          fontSize: '11.5px',
                          color: '#5f6368',
                          fontWeight: 600,
                        }}>
                        Contrail impact from overflight
                      </span>
                    </Box>
                    <Box
                      display="flex"
                      alignItems="baseline"
                      style={{gap: '4px'}}>
                      <MetricValue
                        value={data.overflightFraction}
                        isLoading={Boolean(data.isLoading)}
                        size="14.5px"
                      />
                    </Box>
                  </Box>
                </>
              }
            />
          </Box>
        </Box>

        {/* Horizontal Divider Line separating national territory vs outgoing
           flights */}
        <Box
          style={{
            height: '1px',
            backgroundColor: '#ebebeb',
            width: '100%',
            marginTop: '20px',
            marginBottom: '16px',
          }}
        />

        <Box
          display="flex"
          flexWrap="wrap"
          style={{
            gap: '24px',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
          }}>
          {/* Column 1: Header and text */}
          <Box style={{flex: '1 1 240px', minWidth: '200px'}}>
            <Box display="flex" alignItems="center">
              <OutgoingIcon />
              <Typography
                style={{
                  fontSize: '16px',
                  fontWeight: 600,
                  color: '#202124',
                }}>
                All outgoing flights
              </Typography>
            </Box>
            <Typography
              style={{fontSize: '13px', color: '#5f6368', marginTop: '6px'}}>
              Flight plans needed before departure can incorporate contrail
              avoidance. Without such avoidance plans the global contrail impact
              from{' '}
              {outgoingExplorerUrl ? (
                <a
                  href={outgoingExplorerUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{
                    color: '#1a73e8',
                    textDecoration: 'underline',
                  }}>
                  departing flights
                </a>
              ) : (
                'departing flights'
              )}{' '}
              can be attributed to {countryDisplayName}.
            </Typography>
          </Box>

          {/* Column 2: Non-optional metrics (HeroImpactCard with optional
             metrics toggle) */}
          <Box
            style={{flex: '1 1 380px', minWidth: '280px', maxWidth: '560px'}}>
            {/* Consolidated Hero Metric: Total contrail impact + Per capita +
               vs National GHG */}
            <HeroImpactCard
              title={`Total contrail impact (${gwpLabel})`}
              isLoading={Boolean(data.isLoading)}
              metrics={data.outgoingHero}
              onYoYClick={(e) => {
                openYoYSeries(
                  'outgoing_flight_co2e_gwp_tonnes',
                  `Outgoing flights contrail impact (${gwpLabel})`,
                  'All outgoing flights',
                  e,
                );
              }}
              onAbsoluteRankClick={(e) => {
                openRanking(
                  'outgoing_flight_co2e_gwp_tonnes',
                  `Total contrail impact (${gwpLabel})`,
                  e,
                );
              }}
              onPerCapitaRankClick={(e) => {
                openRanking(
                  'outgoing_flight_co2e_gwp_tonnes_per_capita',
                  `Outgoing impact per capita (${gwpLabel})`,
                  e,
                );
              }}
              onNationalGhgRankClick={(e) => {
                openRanking(
                  'outgoing_flight_co2e_gwp_vs_national_ghg',
                  `Outgoing vs. national GHG emissions (${gwpLabel})`,
                  e,
                );
              }}
              optionalMetrics={
                outgoingExplorerUrl ? (
                  <Box style={{minWidth: '130px'}}>
                    <Box
                      display="flex"
                      alignItems="center"
                      style={{gap: '5px', marginBottom: '2px'}}>
                      <OutgoingIcon />
                      <span
                        style={{
                          fontSize: '11.5px',
                          color: '#5f6368',
                          fontWeight: 600,
                        }}>
                        Highest impact outgoing flights
                      </span>
                    </Box>
                    <Box
                      display="flex"
                      alignItems="baseline"
                      style={{gap: '4px', marginTop: '2px'}}>
                      <a
                        href={outgoingExplorerUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        style={{
                          fontSize: '13px',
                          fontWeight: 500,
                          color: '#1a73e8',
                          textDecoration: 'none',
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: '4px',
                        }}
                        onMouseEnter={(e) =>
                          (e.currentTarget.style.textDecoration = 'underline')
                        }
                        onMouseLeave={(e) =>
                          (e.currentTarget.style.textDecoration = 'none')
                        }>
                        Explore flights
                        <PopupIcon size={11} style={{marginLeft: '2px'}} />
                      </a>
                    </Box>
                  </Box>
                ) : null
              }
            />
          </Box>
        </Box>

        {/* Horizontal Divider Line separating outgoing flights vs controlled
           FIRs */}
        <Box
          style={{
            height: '1px',
            backgroundColor: '#ebebeb',
            width: '100%',
            marginTop: '20px',
            marginBottom: '16px',
          }}
        />

        {/* Metric Group 3: Metrics for All Associated FIR Borders */}
        <Box
          display="flex"
          flexWrap="wrap"
          style={{
            gap: '24px',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
          }}>
          {/* Column 1: Header and text */}
          <Box style={{flex: '1 1 240px', minWidth: '200px'}}>
            <Box display="flex" alignItems="center">
              <ControlledFirsIcon />
              <Typography
                style={{
                  fontSize: '16px',
                  fontWeight: 600,
                  color: '#202124',
                }}>
                All controlled FIRs
              </Typography>
            </Box>
            <Typography
              style={{fontSize: '13px', color: '#5f6368', marginTop: '6px'}}>
              The contrail impact within controlled international and domestic
              Flight Information Regions (FIRs) can be addressed via targeted
              contrail avoidance regulatory requirements.
            </Typography>
          </Box>

          {/* Column 2: Non-optional metrics (HeroImpactCard with optional
             metrics toggle) */}
          <Box
            style={{flex: '1 1 380px', minWidth: '280px', maxWidth: '560px'}}>
            {data.isLoading ? (
              <Box
                display="flex"
                alignItems="center"
                style={{gap: '10px', padding: '16px 0'}}>
                <CircularProgress size={20} style={{color: '#00bcd4'}} />
                <Typography
                  style={{fontSize: '14px', color: '#5f6368', fontWeight: 500}}>
                  Loading FIR metrics...
                </Typography>
              </Box>
            ) : data.controlledFirRecords &&
              data.controlledFirRecords.length > 0 ? (
              <HeroImpactCard
                title={`Total contrail impact (${gwpLabel})`}
                isLoading={Boolean(data.isLoading)}
                metrics={data.firTotalHero}
                onYoYClick={(e) => {
                  openYoYSeries(
                    'fir_total_co2e_gwp_tonnes',
                    `Controlled FIRs contrail impact (${gwpLabel})`,
                    'All controlled FIRs',
                    e,
                  );
                }}
                onAbsoluteRankClick={(e) => {
                  openRanking(
                    'fir_total_co2e_gwp_tonnes',
                    `Controlled FIRs contrail impact (${gwpLabel})`,
                    e,
                  );
                }}
                onPerCapitaRankClick={(e) => {
                  openRanking(
                    'fir_total_co2e_gwp_tonnes_per_capita',
                    `Controlled FIRs impact per capita (${gwpLabel})`,
                    e,
                  );
                }}
                onNationalGhgRankClick={(e) => {
                  openRanking(
                    'fir_total_co2e_gwp_vs_national_ghg',
                    `Controlled FIRs vs. national GHG (${gwpLabel})`,
                    e,
                  );
                }}
                optionalMetrics={
                  <>
                    {/* Metric 3: FIRs average mean radiative forcing */}
                    <Box style={{minWidth: '130px'}}>
                      <Box
                        display="flex"
                        alignItems="center"
                        style={{gap: '5px', marginBottom: '2px'}}>
                        <RadiativeForcingIcon />
                        <span
                          style={{
                            fontSize: '11.5px',
                            color: '#5f6368',
                            fontWeight: 600,
                          }}>
                          Contrail mean radiative forcing
                        </span>
                      </Box>
                      <Box
                        display="flex"
                        alignItems="baseline"
                        style={{gap: '4px'}}>
                        <MetricValue
                          value={data.firDensity}
                          isLoading={Boolean(data.isLoading)}
                          size="14.5px"
                        />
                      </Box>
                      {!data.isLoading &&
                        hasDisplayValue(data.firDensityRank) && (
                          <RankClickableBadge
                            text={data.firDensityRank}
                            rankNum={data.firDensityRankNum}
                            totalCount={data.totalCountryCount ?? undefined}
                            onClick={(e) => {
                              openRanking(
                                'fir_total_mean_rf_mw_per_sq_m',
                                'Controlled FIRs mean radiative forcing',
                                e,
                              );
                            }}
                          />
                        )}
                    </Box>

                    {/* Domestic & International FIRs impact (shown when the
                       country controls an international FIR or has non-zero
                       international FIR impact) */}
                    {data.hasInternationalFir && (
                      <>
                        <Box style={{minWidth: '130px'}}>
                          <Box
                            display="flex"
                            alignItems="center"
                            style={{gap: '5px', marginBottom: '2px'}}>
                            <ControlledFirsIcon />
                            <span
                              style={{
                                fontSize: '11.5px',
                                color: '#5f6368',
                                fontWeight: 600,
                              }}>
                              Domestic FIRs impact ({gwpLabel})
                            </span>
                          </Box>
                          <Box
                            display="flex"
                            alignItems="baseline"
                            style={{gap: '4px'}}>
                            <MetricValue
                              value={data.firDomImpact}
                              isLoading={Boolean(data.isLoading)}
                              size="14.5px"
                            />
                          </Box>
                        </Box>

                        <Box style={{minWidth: '130px'}}>
                          <Box
                            display="flex"
                            alignItems="center"
                            style={{gap: '5px', marginBottom: '2px'}}>
                            <ControlledFirsIcon />
                            <span
                              style={{
                                fontSize: '11.5px',
                                color: '#5f6368',
                                fontWeight: 600,
                              }}>
                              International FIRs impact ({gwpLabel})
                            </span>
                          </Box>
                          <Box
                            display="flex"
                            alignItems="baseline"
                            style={{gap: '4px'}}>
                            <MetricValue
                              value={data.firIntImpact}
                              isLoading={Boolean(data.isLoading)}
                              size="14.5px"
                            />
                          </Box>
                        </Box>
                      </>
                    )}
                  </>
                }
              />
            ) : (
              <Box
                display="flex"
                alignItems="center"
                justifyContent="center"
                style={{
                  height: '100%',
                  minHeight: '80px',
                  padding: '16px',
                  color: '#5f6368',
                  fontSize: '14px',
                  fontWeight: 500,
                  textAlign: 'center',
                }}>
                No FIRs found for {countryDisplayName}.
              </Box>
            )}
          </Box>
        </Box>

        {/* FIR Selector Buttons (shown in Tile 1 as part of the "All controlled
           FIRs" section) */}
        {data.controlledFirRecords &&
          data.controlledFirRecords.length > 1 &&
          activeFir && (
            <Box style={{marginTop: '16px'}}>
              <FirChipSelector
                records={data.controlledFirRecords}
                computedFirIdx={computedFirIdx}
                activeFir={activeFir}
                onSelectFir={onSelectFir}
                onSelectIdx={(idx) => {
                  setSelectedFirIdx(idx);
                }}
              />
            </Box>
          )}
      </Box>

      {/* Selected FIR Section: New Tile with Selected FIR Heading */}
      {activeFir ? (
        <>
          {/* Selected FIR Heading Bar */}
          <Box
            display="flex"
            alignItems="center"
            style={{gap: '10px', marginTop: '24px', marginBottom: '12px'}}>
            <span
              style={{
                width: '8px',
                height: '24px',
                backgroundColor: '#1a73e8',
                borderRadius: '4px',
                flexShrink: 0,
              }}
            />
            <Typography
              style={{fontSize: '18px', fontWeight: 600, color: '#202124'}}>
              {firHeadingTitle}
            </Typography>
          </Box>

          {/* New Selected FIR Tile */}
          <Box
            style={{
              backgroundColor: '#ffffff',
              borderRadius: '10px',
              border: '1px solid #dadce0',
              padding: '20px',
              marginBottom: '0px',
              boxShadow: '0 1px 3px rgba(0,0,0,0.06)',
            }}>
            {/* Section 1: All Individual FIR Metrics */}
            <Box
              display="flex"
              flexWrap="wrap"
              style={{
                gap: '24px',
                alignItems: 'flex-start',
                justifyContent: 'space-between',
              }}>
              {/* Column 1: FIR Metadata (ICAO, type, controlled by with values
                 to the right of the label) */}
              <Box
                style={{
                  flex: '1 1 240px',
                  minWidth: '200px',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: '10px',
                  paddingTop: '2px',
                }}>
                {/* Field 1: ICAO */}
                <Box display="flex" alignItems="center" style={{gap: '8px'}}>
                  <span
                    style={{
                      fontSize: '12px',
                      fontWeight: 600,
                      color: '#5f6368',
                    }}>
                    ICAO
                  </span>
                  <span
                    style={{
                      fontSize: '13px',
                      fontWeight: 600,
                      color: '#202124',
                      fontFamily: 'monospace',
                    }}>
                    {activeFir.firIcao}
                  </span>
                </Box>

                {/* Field 2: type */}
                <Box display="flex" alignItems="center" style={{gap: '8px'}}>
                  <span
                    style={{
                      fontSize: '12px',
                      fontWeight: 600,
                      color: '#5f6368',
                    }}>
                    type
                  </span>
                  <FirTypeLabelWithModal firType={activeFir.type} />
                </Box>

                {/* Field 3: Controlled by */}
                {(() => {
                  const managingCountries = getManagingCountriesForFir(
                    activeFir,
                    filters.year,
                  );
                  if (managingCountries.length === 0) return null;
                  return (
                    <Box
                      display="flex"
                      alignItems="center"
                      flexWrap="wrap"
                      style={{gap: '8px'}}>
                      <span
                        style={{
                          fontSize: '12px',
                          fontWeight: 600,
                          color: '#5f6368',
                          whiteSpace: 'nowrap',
                        }}>
                        Controlled by
                      </span>
                      {managingCountries.map((c: FirManagingCountry) => {
                        const countryKey = resolveCountryIdentifier(
                          c.isoA2,
                          c.name,
                        );
                        const isCurrentCountry = isCountrySelectionMatch({
                          isoA2: c.isoA2,
                          name: c.name,
                          selectedCountry: filters.country,
                        });
                        return (
                          <button
                            key={countryKey}
                            type="button"
                            onClick={() => {
                              if (
                                !isCurrentCountry &&
                                countryKey &&
                                onSelectCountry
                              ) {
                                onSelectCountry(countryKey);
                              }
                            }}
                            title={
                              isValidCountryIso2(c.isoA2)
                                ? `Select ${c.name} (${c.isoA2})`
                                : `Select ${c.name}`
                            }
                            style={{
                              padding: '6px 12px',
                              borderRadius: '20px',
                              border: isCurrentCountry
                                ? '2px solid #00bcd4'
                                : '1px solid #dadce0',
                              backgroundColor: isCurrentCountry
                                ? '#e0f7fa'
                                : '#ffffff',
                              color: isCurrentCountry ? '#202124' : '#5f6368',
                              fontWeight: isCurrentCountry ? 600 : 500,
                              fontSize: '12px',
                              cursor:
                                onSelectCountry && !isCurrentCountry
                                  ? 'pointer'
                                  : 'default',
                              display: 'inline-flex',
                              alignItems: 'center',
                              whiteSpace: 'nowrap',
                              minWidth: 0,
                              transition: 'all 0.15s ease',
                            }}>
                            {c.name}
                          </button>
                        );
                      })}
                    </Box>
                  );
                })()}
              </Box>

              {/* Column 2: Total contrail impact + More metrics expandable
                 section */}
              <Box
                style={{
                  flex: '1 1 380px',
                  minWidth: '280px',
                  maxWidth: '560px',
                  display: 'flex',
                  flexDirection: 'column',
                }}>
                <HeroImpactCard
                  title={`Total contrail impact (${gwpLabel})`}
                  variant="fir"
                  metrics={{
                    absoluteValue: formatTonnesCo2e(activeFir.co2eTonnes),
                    absoluteYoY: activeFir.absoluteYoY,
                    absoluteRank:
                      activeFir.co2eRank != null
                        ? formatRankLabel(
                            activeFir.co2eRank,
                            firRankDenominator,
                            'global FIRs',
                          )
                        : NOT_AVAILABLE_LABEL,
                    absoluteRankNum: activeFir.co2eRank ?? undefined,
                    totalCount: firRankDenominator ?? undefined,
                  }}
                  onYoYClick={(e) => {
                    openYoYSeries(
                      'fir_individual_co2e_gwp_tonnes',
                      `FIR contrail impact absolute (${gwpLabel})`,
                      'Selected FIR: ' +
                        formatFirDisplayName(
                          activeFir.firName,
                          activeFir.firIcao,
                        ),
                      e,
                      {firId: activeFir.firId},
                    );
                  }}
                  onAbsoluteRankClick={(e) => {
                    openRanking(
                      'fir_individual_co2e_gwp_tonnes',
                      `Individual FIRs Contrail Impact (${gwpLabel})`,
                      e,
                      activeFir.firId,
                    );
                  }}
                  optionalMetrics={
                    <>
                      {/* Metric 1 (First): Standalone X% of controlled FIRs
                         impact */}
                      <Box style={{flex: '1 1 180px', minWidth: '180px'}}>
                        <span
                          style={{
                            fontSize: '12px',
                            fontWeight: 600,
                            color: '#5f6368',
                            display: 'block',
                            marginBottom: '4px',
                          }}>
                          Contrail impact of controlled FIRs
                        </span>
                        <MetricValue
                          value={activeFir.percentOfFirTotal || '100%'}
                        />
                      </Box>

                      {/* Metric 2 (Second): Contrail mean radiative forcing */}
                      <Box style={{flex: '1 1 180px', minWidth: '180px'}}>
                        <span
                          style={{
                            fontSize: '12px',
                            fontWeight: 600,
                            color: '#5f6368',
                            display: 'block',
                            marginBottom: '4px',
                          }}>
                          Contrail mean radiative forcing
                        </span>
                        <MetricValue
                          value={formatMwPerSqM(activeFir.meanRfMwPerSqM)}
                          style={{display: 'block'}}
                        />
                        {activeFir.impactDensityRank != null && (
                          <Box style={{marginTop: '2px'}}>
                            <RankClickableBadge
                              text={formatRankLabel(
                                activeFir.impactDensityRank,
                                firRankDenominator,
                                'global FIRs',
                              )}
                              rankNum={activeFir.impactDensityRank}
                              totalCount={firRankDenominator ?? undefined}
                              onClick={(e) => {
                                openRanking(
                                  'fir_individual_mean_rf_mw_per_sq_m',
                                  'Individual FIRs Mean Radiative Forcing',
                                  e,
                                  activeFir.firId,
                                );
                              }}
                            />
                          </Box>
                        )}
                        <span
                          style={{
                            fontSize: '12px',
                            color: '#5f6368',
                            display: 'block',
                            marginTop: '2px',
                          }}>
                          (area:{' '}
                          {activeFir.areaSqKm && activeFir.areaSqKm > 0
                            ? Math.round(activeFir.areaSqKm).toLocaleString() +
                              ' km²'
                            : 'N/A'}
                          )
                        </span>
                      </Box>
                    </>
                  }
                />
              </Box>
            </Box>

            {/* Horizontal Divider Line between Section 1 and Section 2 */}
            <Box
              style={{
                height: '1px',
                backgroundColor: '#ebebeb',
                width: '100%',
                marginTop: '20px',
                marginBottom: '20px',
              }}
            />

            {/* Section 2: Temporal Analysis (dropping the FIR name from the
               title) */}
            <TemporalImpactHeatmap
              filters={filters}
              selectedFirId={activeFir.firId}
              embedded={true}
              hideTitleFirName={true}
            />
          </Box>
        </>
      ) : null}

      {/* Global Country & FIR Rankings Popup */}
      {activeRanking && (
        <CountryRankingsPopup
          isOpen={true}
          onClose={closeRanking}
          metricKey={activeRanking.metricKey}
          metricTitle={activeRanking.metricTitle}
          selectedCountry={filters.country}
          selectedFirId={activeRanking.selectedFirId}
          year={filters.year}
          gwp={gwpLabel}
          anchorRect={activeRanking.anchorRect}
          onSelectCountry={handleRankingSelectCountry}
          onSelectFir={onSelectFir}
        />
      )}

      {/* Historical YoY Multi-Year Series Popup */}
      {activeYoYSeries && (
        <YoYSeriesPopup
          isOpen={true}
          onClose={closeYoYSeries}
          metricKey={activeYoYSeries.metricKey}
          metricTitle={activeYoYSeries.metricTitle}
          categoryTitle={activeYoYSeries.categoryTitle}
          selectedCountry={filters.country}
          selectedFirId={activeYoYSeries.selectedFirId}
          year={filters.year}
          gwp={gwpLabel}
          anchorRect={activeYoYSeries.anchorRect}
        />
      )}
    </Box>
  );
};

/**
 * Subscribes to the country and FIR data cache, resolves current and
 * prior-year metrics for the selected GWP horizon, and renders the regional
 * impact cards, temporal heatmaps, and ranking/time-series popups.
 */
export const ContrailMetricsPanel = React.memo(ContrailMetricsPanelInner);
