/**
 * @fileoverview Searchable global country and Flight Information Region (FIR)
 * rankings modal dialog, row search filtering, selection matching, and scroll
 * positioning helpers.
 * @jsx React.createElement
 * @jsxFrag React.Fragment
 */

import {Box, CircularProgress, Typography} from 'material-ui';
import * as React from 'react';
import {
  fetchRankingsFromBackend,
  getTotalCountryCount,
  getTotalFirCount,
  isCountrySelectionMatch,
  isValidCountryIso2,
  RankingItem,
  RankingsData,
  resolveCountryIdentifier,
} from './CountryMetadata';
import {
  FIR_TYPE_INTERNATIONAL,
  formatMetricValue,
  formatSecondaryValue,
  NOT_AVAILABLE_LABEL,
} from './metric_formatters';
import {getRankBadgeColors, ModalShell} from './metrics_panel_widgets';

/**
 * Returns the popup subtitle, naming the entity count only when it is known.
 *
 * @visibleForTesting
 */
export function formatRankingSubtitle(
  total: number | null,
  noun: string,
): string {
  return total === null
    ? `Full global ranking of ${noun}`
    : `Full global ranking of ${total} ${noun}`;
}

/** Scroll and item geometry inputs for `calculateTargetScroll`. */
export interface TargetScrollOptions {
  readonly itemTop: number;
  readonly itemHeight: number;
  readonly containerHeight: number;
}

/**
 * Calculates the centered target scroll position for an item within a
 * container.
 * @visibleForTesting
 */
export function calculateTargetScroll({
  itemTop,
  itemHeight,
  containerHeight,
}: TargetScrollOptions): number {
  return Math.max(0, itemTop - containerHeight / 2 + itemHeight / 2);
}

/**
 * Filters ranking items against a search query across country names, FIR
 * codes/names, and ISO codes.
 * @visibleForTesting
 */
export function filterRankingItems(
  items: readonly RankingItem[],
  query: string,
): readonly RankingItem[] {
  if (!query.trim()) return items;
  const q = query.toLowerCase().trim();
  return items.filter(
    (item) =>
      (item.name ?? '').toLowerCase().includes(q) ||
      (item.firId && item.firId.toLowerCase().includes(q)) ||
      (item.firIcao && item.firIcao.toLowerCase().includes(q)) ||
      (item.firName && item.firName.toLowerCase().includes(q)) ||
      (item.countryName && item.countryName.toLowerCase().includes(q)) ||
      (item.countries &&
        item.countries.some(
          (c) =>
            (c.name ?? '').toLowerCase().includes(q) ||
            (c.isoA2 ?? '').toLowerCase().includes(q),
        )) ||
      (item.isoA2 ?? '').toLowerCase().includes(q) ||
      (item.rank != null && String(item.rank).includes(q)),
  );
}

/**
 * Counts the ranked items, i.e. the denominator of "#rank / total". Items
 * without a value (e.g. per-area metrics for an entity without area) are
 * listed but unranked, so they must not stretch the bottom-25 colour band.
 * @visibleForTesting
 */
export function countRankedItems(items: readonly RankingItem[]): number {
  return items.filter((item) => item.rank != null).length;
}

/**
 * Resolves the total ranked entity count for both the popup header subtitle and
 * per-row rank badge color thresholds. When `items` is loaded, only ranked
 * items (`rank != null`) are counted; otherwise falls back to the cached total
 * FIR or country count for `year` (or `null` while loading).
 * @visibleForTesting
 */
export function resolveRankingTotalCount(
  items: readonly RankingItem[] | null | undefined,
  isFirRanking: boolean,
  year: number,
): number | null {
  if (items != null) {
    return countRankedItems(items);
  }
  return isFirRanking ? getTotalFirCount(year) : getTotalCountryCount(year);
}

/**
 * Renders a rank badge: `#rank`, or N/A for an unranked item.
 * @visibleForTesting
 */
export function formatRankBadgeLabel(rank: number | null | undefined): string {
  return rank != null && Number.isFinite(rank)
    ? `#${rank}`
    : NOT_AVAILABLE_LABEL;
}

/**
 * Options for matching a ranking row against the active country or FIR
 * selection.
 */
export interface IsRankingItemSelectedOptions {
  readonly item: RankingItem;
  readonly isFirRanking: boolean;
  readonly selectedCountry?: string;
  readonly selectedFirId?: string | null;
}

/**
 * Determines whether a ranking row matches the currently selected country or
 * FIR sector.
 * @visibleForTesting
 */
export function isRankingItemSelected({
  item,
  isFirRanking,
  selectedCountry,
  selectedFirId,
}: IsRankingItemSelectedOptions): boolean {
  if (isFirRanking) {
    if (selectedFirId && item.firId) {
      return (
        item.firId.trim().toUpperCase() === selectedFirId.trim().toUpperCase()
      );
    }
    return (
      (item.countries?.some((c) =>
        isCountrySelectionMatch({
          isoA2: c.isoA2,
          name: c.name,
          selectedCountry,
        }),
      ) ??
        false) ||
      isCountrySelectionMatch({
        isoA2: item.isoA2,
        name: item.countryName,
        selectedCountry,
      })
    );
  }
  return isCountrySelectionMatch({
    isoA2: item.isoA2,
    name: item.name,
    selectedCountry,
  });
}

/** Props for the `CountryRankingsPopup` component. */
export interface CountryRankingsPopupProps {
  readonly isOpen: boolean;
  readonly onClose: () => void;
  readonly metricKey: string;
  readonly metricTitle: string;
  readonly selectedCountry: string;
  readonly selectedFirId?: string | null;
  readonly year: number;
  readonly gwp?: string;
  readonly anchorRect?: DOMRect | null;
  readonly onSelectCountry: (countryName: string) => void;
  readonly onSelectFir?: (firId: string) => void;
}

/**
 * Fetches `/rankings/{year}/{metric}` when opened, filters rows by search
 * query, scrolls the active country or FIR row into view, and invokes
 * selection callbacks when a row is clicked.
 */
export const CountryRankingsPopup: React.FC<CountryRankingsPopupProps> = ({
  isOpen,
  onClose,
  metricKey,
  metricTitle,
  selectedCountry,
  selectedFirId,
  year,
  gwp = 'GWP50',
  anchorRect,
  onSelectCountry,
  onSelectFir,
}) => {
  const [data, setData] = React.useState<RankingsData | null>(null);
  const [loading, setLoading] = React.useState<boolean>(true);
  const [searchQuery, setSearchQuery] = React.useState<string>('');

  const listContainerRef = React.useRef<HTMLDivElement | null>(null);
  const selectedRowRef = React.useRef<HTMLDivElement | null>(null);

  // Fetch rankings whenever the popup is opened or parameters change
  React.useEffect(() => {
    if (!isOpen) {
      setData(null);
      setSearchQuery('');
      return;
    }

    const abortController = new AbortController();
    setData(null);
    setLoading(true);

    void fetchRankingsFromBackend({
      metric: metricKey,
      year,
      gwp,
      signal: abortController.signal,
    })
      .then((res) => {
        if (!abortController.signal.aborted) {
          setData(res);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!abortController.signal.aborted) {
          setLoading(false);
          console.warn('Failed to load country rankings:', err);
        }
      });

    return () => {
      abortController.abort();
    };
  }, [isOpen, metricKey, year, gwp]);

  // Filter items by search query
  const filteredItems = React.useMemo(() => {
    if (!data?.items) return [];
    return filterRankingItems(data.items, searchQuery);
  }, [data?.items, searchQuery]);

  const isFirRanking = Boolean(data?.isFirRanking);

  const totalRankedCount = React.useMemo(
    () => resolveRankingTotalCount(data?.items, isFirRanking, year),
    [data?.items, isFirRanking, year],
  );

  const isSelected = React.useCallback(
    (item: RankingItem) =>
      isRankingItemSelected({
        item,
        isFirRanking,
        selectedCountry,
        selectedFirId,
      }),
    [isFirRanking, selectedFirId, selectedCountry],
  );

  const selectedItem = React.useMemo(() => {
    if (!filteredItems || filteredItems.length === 0) return null;
    return filteredItems.find(isSelected) ?? null;
  }, [filteredItems, isSelected]);

  const [, setWindowResizeEpoch] = React.useState<number>(0);

  // Recompute popup coordinates when the window resizes
  React.useEffect(() => {
    if (!isOpen) return;
    let resizeRaf: number | null = null;
    const handleResize = () => {
      const runResize = () => {
        resizeRaf = null;
        setWindowResizeEpoch((n) => n + 1);
      };
      if (typeof requestAnimationFrame === 'function') {
        if (resizeRaf != null) return;
        resizeRaf = requestAnimationFrame(runResize);
      } else {
        runResize();
      }
    };
    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
      if (resizeRaf != null && typeof cancelAnimationFrame === 'function') {
        cancelAnimationFrame(resizeRaf);
      }
    };
  }, [isOpen]);

  // Auto-scroll so selected country/FIR is in the middle of the popup list
  React.useLayoutEffect(() => {
    if (!isOpen || loading || !data) return;

    const timer = setTimeout(() => {
      if (selectedRowRef.current && listContainerRef.current) {
        const itemTop = selectedRowRef.current.offsetTop;
        const containerH = listContainerRef.current.clientHeight;
        const itemH = selectedRowRef.current.clientHeight;
        listContainerRef.current.scrollTop = calculateTargetScroll({
          itemTop,
          itemHeight: itemH,
          containerHeight: containerH,
        });
      }
    }, 50);

    return () => {
      clearTimeout(timer);
    };
  }, [isOpen, loading, data, searchQuery]);

  if (!isOpen) return null;

  // Compute position (left vs. right depending on what fits best on screen)
  const popupWidth = 460;
  let popupHeight = 580;
  let left = 0;
  let top = 0;

  if (typeof window !== 'undefined') {
    // Dynamically extend popup height up to 760px when viewport height permits
    // (leaving at least 60px margin)
    popupHeight = Math.min(760, Math.max(580, window.innerHeight - 60));

    if (anchorRect) {
      const spaceRight = window.innerWidth - anchorRect.right;
      const spaceLeft = anchorRect.left;

      if (spaceLeft >= popupWidth + 20) {
        // Preferred to the left if anchor is on the right side
        left = anchorRect.left - popupWidth - 14;
      } else if (spaceRight >= popupWidth + 20) {
        // Position to the right
        left = anchorRect.right + 14;
      } else {
        // Center on screen
        left = Math.max(16, (window.innerWidth - popupWidth) / 2);
      }

      top = Math.max(
        20,
        Math.min(window.innerHeight - popupHeight - 20, anchorRect.top - 60),
      );
    } else {
      left = Math.max(16, (window.innerWidth - popupWidth) / 2);
      top = Math.max(20, (window.innerHeight - popupHeight) / 2);
    }
  }

  const renderRankingRow = (item: RankingItem, rank: number | null) => {
    const selected = item === selectedItem;
    // NaN is not a rank, so unranked items get the neutral badge.
    const rankColors = getRankBadgeColors(rank ?? Number.NaN, totalRankedCount);
    const rankLabel = formatRankBadgeLabel(rank);
    const secondaryText = formatSecondaryValue(
      item.secondaryValue,
      data?.secondaryUnit,
    );

    return (
      <div
        key={
          isFirRanking
            ? `${item.firId || `${item.firIcao}_${item.firName}`}_${rank}`
            : `${resolveCountryIdentifier(item.isoA2, item.name)}_${rank}`
        }
        ref={selected ? selectedRowRef : undefined}
        onClick={() => {
          if (isFirRanking) {
            const alreadyInSelected = isRankingItemSelected({
              item,
              isFirRanking: true,
              selectedCountry,
            });
            const primaryCountry = item.countries?.[0];
            const targetCountry = primaryCountry
              ? resolveCountryIdentifier(
                  primaryCountry.isoA2,
                  primaryCountry.name,
                )
              : resolveCountryIdentifier(item.isoA2, item.countryName);
            if (targetCountry && !alreadyInSelected) {
              onSelectCountry(targetCountry);
            }
            if (onSelectFir && item.firId) {
              onSelectFir(item.firId);
            }
            onClose();
            return;
          }
          onSelectCountry(resolveCountryIdentifier(item.isoA2, item.name));
          onClose();
        }}
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '10px 16px',
          boxSizing: 'border-box',
          borderLeft: selected ? '4px solid #1a73e8' : '4px solid transparent',
          borderRight: 'none',
          borderTop: selected ? '1px solid #c2dbfe' : 'none',
          borderBottom: selected ? '1px solid #c2dbfe' : '1px solid #f1f3f4',
          backgroundColor: selected ? '#e8f0fe' : '#ffffff',
          cursor: 'pointer',
          transition: 'background-color 0.12s ease',
          position: selected ? 'sticky' : undefined,
          top: selected ? 0 : undefined,
          bottom: selected ? 0 : undefined,
          zIndex: selected ? 2 : undefined,
          flexShrink: 0,
        }}
        onMouseEnter={(e) => {
          if (!selected) {
            e.currentTarget.style.backgroundColor = '#f8f9fa';
          }
        }}
        onMouseLeave={(e) => {
          if (!selected) {
            e.currentTarget.style.backgroundColor = '#ffffff';
          }
        }}>
        {/* Left: Rank badge + Name/Badges */}
        <Box
          display="flex"
          alignItems="center"
          style={{gap: '10px', minWidth: 0, flex: 1}}>
          {/* Rank Badge */}
          <span
            style={{
              minWidth: '32px',
              height: '24px',
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              borderRadius: '4px',
              fontSize: '11px',
              fontWeight: 700,
              backgroundColor: selected ? '#d2e3fc' : rankColors.bgColor,
              color: selected ? '#1a73e8' : rankColors.color,
              border: `1px solid ${
                selected ? '#1a73e8' : rankColors.borderColor
              }`,
              padding: '0 4px',
              flexShrink: 0,
            }}>
            {rankLabel}
          </span>

          {/* Name & Metadata Badges */}
          <Box style={{minWidth: 0}}>
            <Box
              display="flex"
              alignItems="center"
              flexWrap="wrap"
              style={{gap: '6px'}}>
              <span
                style={{
                  fontSize: '13.5px',
                  fontWeight: selected ? 700 : 500,
                  color: selected ? '#1a73e8' : '#202124',
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                }}>
                {item.name}
              </span>
              {isFirRanking && item.firIcao && (
                <span
                  style={{
                    fontSize: '11px',
                    fontWeight: 600,
                    fontFamily: 'monospace',
                    color: selected ? '#1a73e8' : '#202124',
                    backgroundColor: selected ? '#d2e3fc' : '#f1f3f4',
                    padding: '1px 5px',
                    borderRadius: '3px',
                  }}>
                  {item.firIcao}
                </span>
              )}
              {isFirRanking && item.type && (
                <span
                  style={{
                    fontSize: '9.5px',
                    fontWeight: 600,
                    textTransform: 'uppercase',
                    padding: '1px 5px',
                    borderRadius: '3px',
                    backgroundColor:
                      item.type === FIR_TYPE_INTERNATIONAL
                        ? '#f8e8fc'
                        : '#e0f7fa',
                    color:
                      item.type === FIR_TYPE_INTERNATIONAL
                        ? '#8e24aa'
                        : '#00838f',
                  }}>
                  {item.type}
                </span>
              )}

              {/* FIR Managing Country Tags (Clickable) */}
              {isFirRanking && item.countries && item.countries.length > 0 && (
                <Box
                  display="inline-flex"
                  alignItems="center"
                  flexWrap="wrap"
                  style={{gap: '4px'}}>
                  {item.countries.map((c) => {
                    const countryKey = resolveCountryIdentifier(
                      c.isoA2,
                      c.name,
                    );
                    const isCountryActive = isCountrySelectionMatch({
                      isoA2: c.isoA2,
                      name: c.name,
                      selectedCountry,
                    });
                    return (
                      <button
                        type="button"
                        key={`${item.firId || item.firIcao}_${countryKey}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          if (!isCountryActive && countryKey) {
                            onSelectCountry(countryKey);
                          }
                          if (onSelectFir && item.firId) {
                            onSelectFir(item.firId);
                          }
                          onClose();
                        }}
                        title={
                          isValidCountryIso2(c.isoA2)
                            ? `Open ${c.name} (${c.isoA2}) for ${item.name}`
                            : `Open ${c.name} for ${item.name}`
                        }
                        style={{
                          fontSize: '11px',
                          fontWeight: 600,
                          color: isCountryActive ? '#ffffff' : '#1a73e8',
                          backgroundColor: isCountryActive
                            ? '#1a73e8'
                            : '#e8f0fe',
                          border: `1px solid ${
                            isCountryActive ? '#1a73e8' : '#c2dbfe'
                          }`,
                          padding: '1.5px 7px',
                          borderRadius: '12px',
                          cursor: 'pointer',
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: '3px',
                          transition: 'all 0.15s ease',
                          outline: 'none',
                          fontFamily: 'inherit',
                        }}
                        onMouseEnter={(e) => {
                          if (!isCountryActive) {
                            e.currentTarget.style.backgroundColor = '#d2e3fc';
                            e.currentTarget.style.borderColor = '#1a73e8';
                          }
                        }}
                        onMouseLeave={(e) => {
                          if (!isCountryActive) {
                            e.currentTarget.style.backgroundColor = '#e8f0fe';
                            e.currentTarget.style.borderColor = '#c2dbfe';
                          }
                        }}>
                        <span>{c.name}</span>
                        {isValidCountryIso2(c.isoA2) && (
                          <span style={{fontSize: '9px', opacity: 0.85}}>
                            ({c.isoA2})
                          </span>
                        )}
                      </button>
                    );
                  })}
                </Box>
              )}

              {!isFirRanking && (
                <span
                  style={{
                    fontSize: '10.5px',
                    fontWeight: 600,
                    color: selected ? '#1a73e8' : '#80868b',
                    backgroundColor: selected ? '#d2e3fc' : '#f1f3f4',
                    padding: '1px 4px',
                    borderRadius: '3px',
                  }}>
                  {item.isoA2}
                </span>
              )}

              {!isFirRanking && selected && (
                <span
                  style={{
                    fontSize: '10px',
                    fontWeight: 600,
                    color: '#1a73e8',
                    backgroundColor: '#ffffff',
                    border: '1px solid #1a73e8',
                    padding: '0 4px',
                    borderRadius: '3px',
                  }}>
                  ACTIVE
                </span>
              )}
            </Box>
          </Box>
        </Box>

        {/* Right: Value, formatted in the user's locale from the raw
           number. */}
        <Box style={{textAlign: 'right', marginLeft: '12px', flexShrink: 0}}>
          <span
            style={{
              fontSize: '13px',
              fontWeight: selected ? 700 : 600,
              color: selected ? '#1a73e8' : '#202124',
              display: 'block',
            }}>
            {item.value == null || !Number.isFinite(item.value)
              ? NOT_AVAILABLE_LABEL
              : formatMetricValue(item.value, data?.unit)}
          </span>
          {!isFirRanking && secondaryText && (
            <span
              style={{
                fontSize: '11px',
                color: '#5f6368',
                display: 'block',
                marginTop: '1px',
              }}>
              {secondaryText}
            </span>
          )}
        </Box>
      </div>
    );
  };

  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      top={top}
      left={left}
      width={popupWidth}
      height={popupHeight}
      headerPadding="16px 20px 12px 20px"
      closeButtonVariant="transparent"
      closeButtonTitle="Close ranking window (Esc)"
      closeButtonAriaLabel="Close rankings"
      headerContent={
        <>
          <Typography
            style={{
              fontSize: '16px',
              fontWeight: 600,
              color: '#202124',
              lineHeight: 1.3,
              wordBreak: 'break-word',
            }}>
            {data?.title || metricTitle}
          </Typography>
          <Typography
            style={{fontSize: '12px', color: '#5f6368', marginTop: '2px'}}>
            {formatRankingSubtitle(
              totalRankedCount,
              isFirRanking ? 'global FIRs' : 'countries',
            )}
          </Typography>
        </>
      }
      headerControls={
        <Box
          style={{
            padding: '10px 16px',
            borderBottom: '1px solid #f1f3f4',
            backgroundColor: '#ffffff',
          }}>
          <input
            type="text"
            placeholder={
              isFirRanking
                ? 'Search FIR name, ICAO, or country...'
                : 'Search country or ISO code...'
            }
            value={searchQuery}
            onChange={(e) => {
              setSearchQuery(e.target.value);
            }}
            style={{
              width: '100%',
              boxSizing: 'border-box',
              height: '36px',
              padding: '0 12px',
              fontSize: '13px',
              borderRadius: '6px',
              border: '1px solid #dadce0',
              outline: 'none',
              backgroundColor: '#f8f9fa',
              color: '#202124',
              fontFamily: 'inherit',
            }}
            onFocus={(e) => {
              e.currentTarget.style.borderColor = '#1a73e8';
              e.currentTarget.style.backgroundColor = '#ffffff';
              e.currentTarget.style.boxShadow =
                '0 0 0 2px rgba(26,115,232,0.2)';
            }}
            onBlur={(e) => {
              e.currentTarget.style.borderColor = '#dadce0';
              e.currentTarget.style.backgroundColor = '#f8f9fa';
              e.currentTarget.style.boxShadow = 'none';
            }}
          />
        </Box>
      }
      footer={
        <Box
          style={{
            padding: '8px 16px',
            backgroundColor: '#fafbfc',
            borderTop: '1px solid #ebebeb',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            fontSize: '11.5px',
            color: '#5f6368',
          }}>
          <span>
            {isFirRanking
              ? 'Click a row or country tag to open that country and ' +
                'view this FIR'
              : 'Click any country to switch view'}
          </span>
          <span style={{fontWeight: 500}}>
            {isFirRanking && (selectedItem || selectedFirId)
              ? `Selected FIR: ${selectedItem?.name || selectedFirId}`
              : selectedCountry
                ? `Selected: ${selectedCountry}`
                : ''}
          </span>
        </Box>
      }>
      {/* Scrollable list */}
      <div
        ref={listContainerRef}
        style={{
          flex: 1,
          overflowY: 'auto',
          position: 'relative',
          backgroundColor: '#ffffff',
        }}>
        {loading ? (
          <Box
            display="flex"
            flexDirection="column"
            alignItems="center"
            justifyContent="center"
            style={{height: '240px', gap: '12px'}}>
            <CircularProgress size={28} style={{color: '#1a73e8'}} />
            <Typography
              style={{fontSize: '13px', color: '#5f6368', fontWeight: 500}}>
              {isFirRanking
                ? 'Loading full global FIR ranking...'
                : 'Loading full global ranking...'}
            </Typography>
          </Box>
        ) : filteredItems.length === 0 ? (
          <Box
            display="flex"
            alignItems="center"
            justifyContent="center"
            style={{height: '200px', color: '#5f6368', fontSize: '13px'}}>
            {!searchQuery.trim()
              ? 'No ranking data available.'
              : isFirRanking
                ? `No FIRs match "${searchQuery}"`
                : `No countries match "${searchQuery}"`}
          </Box>
        ) : (
          filteredItems.map((item) => renderRankingRow(item, item.rank))
        )}
      </div>
    </ModalShell>
  );
};
