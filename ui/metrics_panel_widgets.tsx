/**
 * @fileoverview Reusable UI widgets, SVG icons, text-width measurement helpers,
 * and FIR selection helpers for the contrail metrics panel.
 * @jsx React.createElement
 * @jsxFrag React.Fragment
 */

import {Box, CircularProgress, IconButton} from 'material-ui';
import * as React from 'react';
import {
  CountryData,
  FirRecord,
  HeroScopeMetrics,
  setBoundedCacheEntry,
} from './CountryMetadata';
import {
  extractRankInfo,
  FIR_TYPE_INTERNATIONAL,
  NOT_AVAILABLE_LABEL,
  YoYIndicator,
} from './metric_formatters';

/** Text, background, hover, and border colors for a rank badge. */
export interface RankBadgeColors {
  readonly color: string;
  readonly bgColor: string;
  readonly hoverBgColor: string;
  readonly borderColor: string;
}

const NEUTRAL_RANK_BADGE_COLORS: RankBadgeColors = {
  color: '#5f6368',
  bgColor: '#f1f3f4',
  hoverBgColor: '#e8eaed',
  borderColor: '#dadce0',
};

const RED_RANK_BADGE_COLORS: RankBadgeColors = {
  color: '#c5221f', // Red for top 25 high impact; needs no total
  bgColor: '#fce8e6',
  hoverBgColor: '#fad2cf',
  borderColor: '#fad2cf',
};

const GREEN_RANK_BADGE_COLORS: RankBadgeColors = {
  color: '#137333', // Green for bottom 25 low impact
  bgColor: '#e6f4ea',
  hoverBgColor: '#ceead6',
  borderColor: '#ceead6',
};

/**
 * Calculates UI color styling for ranking badges based on position and total
 * item count.
 *
 * Ranks 1-25 are red regardless of the total. The bottom-25 green band needs
 * a known positive total; without one (still loading, or the list failed to
 * load) every other rank is neutral grey, so no roster size is assumed.
 */
export function getRankBadgeColors(
  rankNum: number | null | undefined,
  totalCount?: number | null,
): RankBadgeColors {
  if (rankNum == null || !Number.isFinite(rankNum) || rankNum < 1) {
    return NEUTRAL_RANK_BADGE_COLORS;
  }
  if (rankNum <= 25) {
    return RED_RANK_BADGE_COLORS;
  }
  if (totalCount == null || !Number.isFinite(totalCount) || totalCount <= 0) {
    return NEUTRAL_RANK_BADGE_COLORS;
  }
  if (rankNum >= Math.max(26, totalCount - 25 + 1)) {
    return GREEN_RANK_BADGE_COLORS;
  }
  return NEUTRAL_RANK_BADGE_COLORS;
}

/** Props for the shared `ModalShell` popup dialog container. */
export interface ModalShellProps {
  readonly isOpen: boolean;
  readonly onClose: () => void;
  readonly top: number;
  readonly left: number;
  readonly width: number;
  readonly height: number;
  readonly headerContent: React.ReactNode;
  readonly headerControls?: React.ReactNode;
  readonly footer?: React.ReactNode;
  readonly headerPadding?: string;
  readonly closeButtonVariant?: 'filled' | 'transparent';
  readonly closeButtonTitle?: string;
  readonly closeButtonAriaLabel?: string;
  readonly children: React.ReactNode;
}

const MODAL_OVERLAY_STYLE: React.CSSProperties = {
  position: 'fixed',
  top: 0,
  left: 0,
  right: 0,
  bottom: 0,
  zIndex: 1300,
  pointerEvents: 'auto',
};

const MODAL_BACKDROP_STYLE: React.CSSProperties = {
  position: 'absolute',
  top: 0,
  left: 0,
  right: 0,
  bottom: 0,
  backgroundColor: 'rgba(32, 33, 36, 0.25)',
  backdropFilter: 'blur(2px)',
};

const MODAL_CARD_BASE_STYLE: React.CSSProperties = {
  position: 'fixed',
  maxHeight: 'calc(100vh - 40px)',
  maxWidth: 'calc(100vw - 32px)',
  backgroundColor: '#ffffff',
  borderRadius: '12px',
  boxShadow: '0 8px 32px rgba(0,0,0,0.22), 0 2px 8px rgba(0,0,0,0.12)',
  border: '1px solid #dadce0',
  display: 'flex',
  flexDirection: 'column',
  overflow: 'hidden',
  animation: 'fadeInScale 0.18s ease-out',
};

const MODAL_HEADER_BASE_STYLE: React.CSSProperties = {
  borderBottom: '1px solid #ebebeb',
  backgroundColor: '#fafbfc',
  display: 'flex',
  alignItems: 'flex-start',
  justifyContent: 'space-between',
  gap: '12px',
};

const CLOSE_BUTTON_FILLED_STYLE: React.CSSProperties = {
  color: '#5f6368',
  padding: '4px',
  marginTop: '-2px',
  marginRight: '-4px',
  backgroundColor: '#f1f3f4',
  borderRadius: '50%',
};

const CLOSE_BUTTON_TRANSPARENT_STYLE: React.CSSProperties = {
  color: '#5f6368',
  padding: '6px',
  marginTop: '-4px',
  marginRight: '-6px',
  backgroundColor: 'transparent',
  borderRadius: '50%',
};

function useEscapeToClose(isOpen: boolean, onClose: () => void): void {
  React.useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [isOpen, onClose]);
}

const ModalHeader: React.FC<{
  readonly headerContent: React.ReactNode;
  readonly headerPadding: string;
  readonly closeButtonVariant: 'filled' | 'transparent';
  readonly closeButtonTitle: string;
  readonly closeButtonAriaLabel: string;
  readonly onClose: () => void;
}> = ({
  headerContent,
  headerPadding,
  closeButtonVariant,
  closeButtonTitle,
  closeButtonAriaLabel,
  onClose,
}) => {
  const isTransparent = closeButtonVariant === 'transparent';
  const iconSize = isTransparent ? 20 : 18;
  return (
    <Box style={{...MODAL_HEADER_BASE_STYLE, padding: headerPadding}}>
      <Box style={{flex: 1, minWidth: 0}}>{headerContent}</Box>
      <IconButton
        onClick={onClose}
        size="small"
        title={closeButtonTitle}
        aria-label={closeButtonAriaLabel}
        style={
          isTransparent
            ? CLOSE_BUTTON_TRANSPARENT_STYLE
            : CLOSE_BUTTON_FILLED_STYLE
        }>
        <svg
          width={iconSize}
          height={iconSize}
          viewBox="0 0 24 24"
          fill="currentColor">
          <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
        </svg>
      </IconButton>
    </Box>
  );
};

/**
 * Shared floating modal dialog shell providing Escape-to-close keyboard
 * handling, a blurred click-to-dismiss backdrop, a positioned card frame, and a
 * header bar with a close button.
 */
export const ModalShell: React.FC<ModalShellProps> = ({
  isOpen,
  onClose,
  top,
  left,
  width,
  height,
  headerContent,
  headerControls,
  footer,
  headerPadding = '14px 18px 12px 18px',
  closeButtonVariant = 'filled',
  closeButtonTitle = 'Close (Esc)',
  closeButtonAriaLabel = 'Close dialog',
  children,
}) => {
  useEscapeToClose(isOpen, onClose);
  if (!isOpen) return null;

  return (
    <Box style={MODAL_OVERLAY_STYLE} onClick={onClose}>
      <Box style={MODAL_BACKDROP_STYLE} />
      <div
        onClick={(e) => {
          e.stopPropagation();
        }}
        style={{
          ...MODAL_CARD_BASE_STYLE,
          top: `${top}px`,
          left: `${left}px`,
          width: `${width}px`,
          height: `${height}px`,
        }}>
        <ModalHeader
          headerContent={headerContent}
          headerPadding={headerPadding}
          closeButtonVariant={closeButtonVariant}
          closeButtonTitle={closeButtonTitle}
          closeButtonAriaLabel={closeButtonAriaLabel}
          onClose={onClose}
        />
        {headerControls}
        {children}
        {footer}
      </div>
    </Box>
  );
};

let textMeasureCanvas: HTMLCanvasElement | null = null;
const TEXT_WIDTH_CACHE = new Map<string, number>();

/**
 * Returns a shared 2D canvas context for measuring text widths, or null in
 * non-DOM environments.
 */
function getTextMeasureContext(): CanvasRenderingContext2D | null {
  if (typeof document === 'undefined') return null;
  if (!textMeasureCanvas) {
    textMeasureCanvas = document.createElement('canvas');
  }
  return textMeasureCanvas.getContext('2d');
}

/** Measures and caches the pixel width of `text` rendered in `font`. */
function measureTextWidth(text: string, font: string): number {
  const cacheKey = `${font}::${text}`;
  const cached = TEXT_WIDTH_CACHE.get(cacheKey);
  if (cached !== undefined) {
    return cached;
  }
  const ctx = getTextMeasureContext();
  const width = ctx
    ? ((ctx.font = font), ctx.measureText(text).width)
    : text.length * 7.5;
  setBoundedCacheEntry(TEXT_WIDTH_CACHE, cacheKey, width, 500);
  return width;
}

/**
 * Drops the cached measurement canvas and memoized text widths so each test
 * measures against the `document` it installs.
 * @visibleForTesting
 */
export function resetTextMeasureCacheForTesting(): void {
  textMeasureCanvas = null;
  TEXT_WIDTH_CACHE.clear();
}

/**
 * Computes the pixel width of a FIR selector chip based on its label text.
 * @visibleForTesting
 */
export function computeChipWidth(fir: FirRecord, index: number): number {
  const percentStr = fir.percentOfFirTotal ? ` (${fir.percentOfFirTotal})` : '';
  const displayName =
    `#${index + 1} ${fir.firName || fir.firIcao}` + percentStr;
  const textWidth = measureTextWidth(
    displayName,
    '600 12px Roboto, "Helvetica Neue", Arial, sans-serif',
  );
  return Math.ceil(textWidth * 1.05 + 38);
}

/**
 * Computes the pixel width of the FIR overflow dropdown button.
 * @visibleForTesting
 */
export function computeDropdownWidth(
  overflowCount: number,
  selectedLabel?: string,
): number {
  const label = selectedLabel || `(${overflowCount} more FIRs)`;
  const textWidth = measureTextWidth(
    label,
    '600 12px Roboto, "Helvetica Neue", Arial, sans-serif',
  );
  return Math.ceil(textWidth * 1.05 + 66);
}

/**
 * Returns the hover modal explanation text for a FIR type ('domestic' vs
 * 'international').
 *
 * @visibleForTesting
 */
export function getFirTypeDescription(
  firType: string | undefined | null,
): string {
  return (firType ?? '').toLowerCase() === FIR_TYPE_INTERNATIONAL
    ? 'FIR over international waters.'
    : 'FIR over the country.';
}

/**
 * Badge displaying the FIR airspace type ('Domestic' or 'International') with a
 * hover tooltip.
 */
export const FirTypeLabelWithModal: React.FC<{readonly firType: string}> = ({
  firType,
}) => {
  const [isHovered, setIsHovered] = React.useState(false);
  const isInternational = firType.toLowerCase() === FIR_TYPE_INTERNATIONAL;
  const description = getFirTypeDescription(firType);

  return (
    <span
      style={{
        position: 'relative',
        display: 'inline-flex',
        alignItems: 'center',
      }}
      onMouseEnter={() => {
        setIsHovered(true);
      }}
      onMouseLeave={() => {
        setIsHovered(false);
      }}
      onFocus={() => {
        setIsHovered(true);
      }}
      onBlur={() => {
        setIsHovered(false);
      }}>
      <span
        tabIndex={0}
        aria-describedby={isHovered ? 'fir-type-info-modal' : undefined}
        style={{
          display: 'inline-block',
          fontSize: '11px',
          fontWeight: 600,
          textTransform: 'uppercase',
          padding: '2px 8px',
          borderRadius: '4px',
          backgroundColor: isInternational ? '#f8e8fc' : '#e0f7fa',
          color: isInternational ? '#8e24aa' : '#00838f',
          border: `1px solid ${isInternational ? '#ab47bc' : '#00bcd4'}`,
          width: 'fit-content',
          cursor: 'help',
        }}>
        {firType}
      </span>
      {isHovered && (
        <div
          id="fir-type-info-modal"
          role="tooltip"
          style={{
            position: 'absolute',
            bottom: 'calc(100% + 6px)',
            left: '50%',
            transform: 'translateX(-50%)',
            backgroundColor: '#ffffff',
            color: '#202124',
            border: '1px solid #dadce0',
            borderRadius: '6px',
            padding: '6px 10px',
            fontSize: '12px',
            fontWeight: 500,
            whiteSpace: 'nowrap',
            boxShadow: '0 4px 12px rgba(0,0,0,0.12)',
            zIndex: 1000,
            pointerEvents: 'none',
          }}>
          {description}
        </div>
      )}
    </span>
  );
};

/** Props for the responsive `FirChipSelector` bar. */
export interface FirChipSelectorProps {
  readonly records: readonly FirRecord[];
  readonly computedFirIdx: number;
  readonly activeFir: FirRecord;
  readonly onSelectFir?: (firId: string | null) => void;
  readonly onSelectIdx: (idx: number) => void;
}

/**
 * Responsive horizontal chip bar with overflow `<select>` dropdown for FIR
 * selection.
 */
export const FirChipSelector: React.FC<FirChipSelectorProps> = ({
  records,
  computedFirIdx,
  activeFir,
  onSelectFir,
  onSelectIdx,
}) => {
  const containerRef = React.useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = React.useState<number>(0);

  React.useEffect(() => {
    if (!containerRef.current) return;
    const el = containerRef.current;
    setContainerWidth(el.clientWidth);

    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setContainerWidth(entry.contentRect.width);
      }
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
    };
  }, []);

  const totalRecords = records.length;
  const GAP = 8;

  const {visibleLimit, hasOverflow} = React.useMemo(() => {
    if (totalRecords <= 1) {
      return {visibleLimit: totalRecords, hasOverflow: false};
    }

    const availableWidth =
      containerWidth > 0
        ? containerWidth
        : typeof window !== 'undefined'
          ? Math.max(400, window.innerWidth * 0.55 - 60)
          : 650;
    const chipWidths = records.map((fir, idx) => computeChipWidth(fir, idx));
    const sumAllChips = chipWidths.reduce((sum, w) => sum + w, 0);
    const totalWidthAll = sumAllChips + (totalRecords - 1) * GAP;

    if (totalWidthAll <= availableWidth) {
      return {visibleLimit: totalRecords, hasOverflow: false};
    }

    let chipsWidth = sumAllChips;
    for (let k = totalRecords - 1; k >= 1; k--) {
      chipsWidth -= chipWidths[k];
      const overflowCount = totalRecords - k;

      if (overflowCount === 1 && totalWidthAll <= availableWidth + 24) {
        return {visibleLimit: totalRecords, hasOverflow: false};
      }

      const isOverflowSelected = computedFirIdx >= k;
      const selectedFir = isOverflowSelected
        ? records[computedFirIdx]
        : undefined;
      const selectedPercentStr = selectedFir?.percentOfFirTotal
        ? ` (${selectedFir.percentOfFirTotal})`
        : '';
      const selectedLabel = selectedFir
        ? `#${computedFirIdx + 1} ` +
          `${selectedFir.firName || selectedFir.firIcao}${selectedPercentStr}`
        : undefined;
      const dropdownWidth = computeDropdownWidth(overflowCount, selectedLabel);

      const neededWidth = chipsWidth + k * GAP + dropdownWidth;

      if (neededWidth <= availableWidth) {
        return {visibleLimit: k, hasOverflow: true};
      }
    }

    return {visibleLimit: 1, hasOverflow: totalRecords > 1};
  }, [records, totalRecords, containerWidth, computedFirIdx]);

  const visibleFirs = records.slice(0, visibleLimit);
  const overflowFirs = hasOverflow ? records.slice(visibleLimit) : [];
  const isOverflowSelected = hasOverflow && computedFirIdx >= visibleLimit;
  const overflowCount = overflowFirs.length;

  return (
    <div
      ref={containerRef}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: `${GAP}px`,
        marginBottom: '0px',
        flexWrap: 'nowrap',
        overflowX: 'hidden',
        width: '100%',
        maxWidth: '100%',
        paddingBottom: '0px',
      }}>
      {visibleFirs.map((fir, idx) => {
        const isSelected = idx === computedFirIdx;
        const isInt = fir.type === FIR_TYPE_INTERNATIONAL;
        const percentStr = fir.percentOfFirTotal
          ? ` (${fir.percentOfFirTotal})`
          : '';
        const firLabel = `#${idx + 1} ${fir.firName || fir.firIcao}`;
        const displayName = `${firLabel}${percentStr}`;
        return (
          <button
            key={fir.firId || `${fir.firIcao}_${fir.firName}_${idx}`}
            onClick={() => {
              onSelectIdx(idx);
              onSelectFir?.(fir.firId);
            }}
            title={displayName}
            style={{
              padding: '6px 12px',
              borderRadius: '20px',
              border: isSelected
                ? isInt
                  ? '2px solid #ab47bc'
                  : '2px solid #00bcd4'
                : '1px solid #dadce0',
              backgroundColor: isSelected
                ? isInt
                  ? '#f8e8fc'
                  : '#e0f7fa'
                : '#ffffff',
              color: isSelected ? '#202124' : '#5f6368',
              fontWeight: isSelected ? 600 : 500,
              fontSize: '12px',
              cursor: 'pointer',
              display: 'inline-flex',
              alignItems: 'center',
              whiteSpace: 'nowrap',
              flexShrink: 1,
              minWidth: 0,
              transition: 'all 0.15s ease',
            }}>
            <span
              style={{
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                minWidth: 0,
              }}>
              {firLabel}
            </span>
            {percentStr && (
              <span
                style={{
                  whiteSpace: 'nowrap',
                  flexShrink: 0,
                  marginLeft: '4px',
                }}>
                {percentStr.trim()}
              </span>
            )}
          </button>
        );
      })}

      {hasOverflow && (
        <div
          style={{
            position: 'relative',
            display: 'inline-flex',
            alignItems: 'center',
            flexShrink: 0,
          }}>
          <select
            value={isOverflowSelected ? String(computedFirIdx) : ''}
            onChange={(e) => {
              const idx = Number(e.target.value);
              if (!Number.isNaN(idx) && records[idx]) {
                onSelectIdx(idx);
                onSelectFir?.(records[idx].firId);
              }
            }}
            style={{
              appearance: 'none',
              'WebkitAppearance': 'none',
              padding: '6px 28px 6px 12px',
              borderRadius: '20px',
              border: isOverflowSelected
                ? activeFir.type === FIR_TYPE_INTERNATIONAL
                  ? '2px solid #ab47bc'
                  : '2px solid #00bcd4'
                : '1px solid #dadce0',
              backgroundColor: isOverflowSelected
                ? activeFir.type === FIR_TYPE_INTERNATIONAL
                  ? '#f8e8fc'
                  : '#e0f7fa'
                : '#ffffff',
              color: isOverflowSelected ? '#202124' : '#5f6368',
              fontWeight: isOverflowSelected ? 600 : 500,
              fontSize: '12px',
              cursor: 'pointer',
              outline: 'none',
              fontFamily: 'inherit',
              whiteSpace: 'nowrap',
              maxWidth: '320px',
              textOverflow: 'ellipsis',
              transition: 'all 0.15s ease',
            }}>
            <option value="" disabled>
              {`(${overflowCount} more FIRs)`}
            </option>
            {overflowFirs.map((fir, offset) => {
              const actualIdx = visibleLimit + offset;
              const percentStr = fir.percentOfFirTotal
                ? ` (${fir.percentOfFirTotal})`
                : '';
              return (
                <option
                  key={
                    fir.firId || `${fir.firIcao}_${fir.firName}_${actualIdx}`
                  }
                  value={String(actualIdx)}>
                  #{actualIdx + 1} {fir.firName || fir.firIcao}
                  {percentStr}
                </option>
              );
            })}
          </select>
          <span
            style={{
              position: 'absolute',
              right: '10px',
              pointerEvents: 'none',
              fontSize: '9px',
              color: isOverflowSelected ? '#202124' : '#5f6368',
            }}>
            ▼
          </span>
        </div>
      )}
    </div>
  );
};

/**
 * Renders a formatted metric value or an inline loading spinner when
 * `isLoading` is true.
 */
export const MetricValue: React.FC<{
  value: React.ReactNode;
  isLoading?: boolean;
  icon?: React.ReactNode;
  size?: string;
  style?: React.CSSProperties;
}> = ({value, isLoading = false, icon, size = '18px', style}) => {
  if (isLoading || value == null) {
    return (
      <Box
        display="inline-flex"
        alignItems="center"
        style={{gap: '6px', height: '27px', ...style}}>
        {icon}
        <CircularProgress size={14} style={{color: '#1a73e8'}} />
        <span style={{fontSize: '13px', color: '#80868b', fontWeight: 400}}>
          Loading...
        </span>
      </Box>
    );
  }
  return (
    <span
      style={{
        fontSize: size,
        fontWeight: 500,
        color: '#202124',
        display: icon ? 'inline-flex' : undefined,
        alignItems: icon ? 'center' : undefined,
        ...style,
      }}>
      {icon}
      {value}
    </span>
  );
};

/** Person SVG icon for per-capita metrics. */
const PersonIcon: React.FC = () => (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="#5f6368"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{verticalAlign: 'middle', marginRight: '6px'}}>
    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
    <circle cx="12" cy="7" r="4" />
  </svg>
);

/** Factory SVG icon for national GHG comparison metrics. */
const FactoryIcon: React.FC = () => (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="#5f6368"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{verticalAlign: 'middle', marginRight: '6px'}}>
    <path d="M2 22h20M4 22V10l6 4V10l6 4V7l4-3v14" />
    <circle cx="18" cy="2.5" r="1.5" fill="#5f6368" stroke="none" />
    <circle cx="14.5" cy="4" r="1.1" fill="#5f6368" stroke="none" />
  </svg>
);

/** Renders a map-pin SVG icon for "Above country" metrics. */
export const AboveCountryIcon: React.FC = () => (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="#5f6368"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{verticalAlign: 'middle', marginRight: '6px', flexShrink: 0}}>
    <path d="M12 2a7 7 0 0 0-7 7c0 5.25 7 13 7 13s7-7.75 7-13a7 7 0 0 0-7-7z" />
    <circle cx="12" cy="9" r="2.5" />
  </svg>
);

/** Departure arrow SVG icon for "Departing from country" metrics. */
export const OutgoingIcon: React.FC = () => (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="#5f6368"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{verticalAlign: 'middle', marginRight: '6px', flexShrink: 0}}>
    <line x1="2" y1="18" x2="22" y2="18" strokeDasharray="3 3" />
    <path d="M 5 18 V 11 A 5 5 0 0 1 10 6 H 19" />
    <polyline points="14 2 19 6 14 10" />
  </svg>
);

/** Globe SVG icon for "Controlled FIRs" metrics. */
export const ControlledFirsIcon: React.FC = () => (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="#5f6368"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{verticalAlign: 'middle', marginRight: '6px', flexShrink: 0}}>
    <circle cx="12" cy="12" r="9" />
    <line x1="3" y1="12" x2="21" y2="12" />
    <ellipse cx="12" cy="12" rx="4.5" ry="9" />
  </svg>
);

/** Overflight arrow SVG icon for overflight share metrics. */
export const OverflightIcon: React.FC = () => (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="#5f6368"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{verticalAlign: 'middle', marginRight: '6px'}}>
    <line x1="2" y1="18" x2="22" y2="18" strokeDasharray="3 3" />
    <line x1="3" y1="7" x2="20" y2="7" />
    <polyline points="15 3 20 7 15 11" />
  </svg>
);

/** Sun/radiation SVG icon for radiative forcing metrics. */
export const RadiativeForcingIcon: React.FC = () => (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="#5f6368"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{verticalAlign: 'middle', marginRight: '6px'}}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
  </svg>
);

/** External-link/popup SVG icon used inside clickable rank and YoY badges. */
export const PopupIcon: React.FC<{
  size?: number;
  style?: React.CSSProperties;
}> = ({size = 10, style}) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2.5"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{opacity: 0.85, flexShrink: 0, ...style}}>
    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
    <polyline points="15 3 21 3 21 9" />
    <line x1="10" y1="14" x2="21" y2="3" />
  </svg>
);

/**
 * Splits a formatted rank string whose denominator is 0 (e.g.,
 * "#12 / 0 global FIRs" or "#5 / 0 countries") into its prefix ("#12 / ")
 * and suffix (" global FIRs") so RankClickableBadge can render an inline
 * CircularProgress spinner in place of the 0 denominator while metadata is
 * loading.
 * @visibleForTesting
 */
export function parseRankWithZeroDenominator(
  displayText: string,
): {readonly prefix: string; readonly suffix: string} | null {
  const match = displayText.match(/^(#?\d+\s*\/\s*)0(\b.*)$/);
  if (!match) {
    return null;
  }
  return {
    prefix: match[1],
    suffix: match[2],
  };
}

/**
 * Interactive rank badge that opens the global country or FIR rankings popup on
 * click. When `rankNum` or `totalCount` is omitted, falls back to parsing the
 * rank and denominator from `text`.
 */
export const RankClickableBadge: React.FC<{
  readonly text: string;
  readonly rankNum?: number;
  readonly totalCount?: number;
  readonly onClick?: (event: React.MouseEvent<HTMLElement>) => void;
  readonly tooltip?: string;
  readonly style?: React.CSSProperties;
}> = ({
  text,
  rankNum,
  totalCount,
  onClick,
  tooltip = 'Click to view full global ranking',
  style,
}) => {
  const displayText = text.replace(/^\(/, '').replace(/\)$/, '').trim();
  const zeroDenomParts = parseRankWithZeroDenominator(displayText);
  const parsedRankInfo =
    rankNum == null || totalCount == null ? extractRankInfo(displayText) : null;
  const colors = getRankBadgeColors(
    rankNum ?? parsedRankInfo?.rankNum,
    totalCount ?? parsedRankInfo?.totalCount,
  );

  return (
    <span
      onClick={(event) => {
        if (onClick) {
          event.stopPropagation();
          onClick(event);
        }
      }}
      title={tooltip}
      style={{
        fontSize: '11px',
        fontWeight: 600,
        color: colors.color,
        backgroundColor: colors.bgColor,
        border: `1px solid ${colors.borderColor}`,
        display: 'inline-flex',
        alignItems: 'center',
        gap: '4px',
        marginTop: '3px',
        marginBottom: '1px',
        cursor: onClick ? 'pointer' : 'default',
        textDecoration: 'none',
        padding: '1.5px 6px',
        borderRadius: '4px',
        transition: 'background-color 0.15s ease, border-color 0.15s ease',
        ...style,
      }}
      onMouseEnter={(event) => {
        if (onClick) {
          event.currentTarget.style.backgroundColor = colors.hoverBgColor;
          event.currentTarget.style.textDecoration = 'underline';
        }
      }}
      onMouseLeave={(event) => {
        if (onClick) {
          event.currentTarget.style.backgroundColor = colors.bgColor;
          event.currentTarget.style.textDecoration = 'none';
        }
      }}>
      {zeroDenomParts ? (
        <>
          <span>{zeroDenomParts.prefix}</span>
          <CircularProgress
            size={9}
            thickness={5}
            aria-label="Loading rank denominator"
            style={{color: 'currentColor', margin: '0 1px', flexShrink: 0}}
          />
          <span>{zeroDenomParts.suffix}</span>
        </>
      ) : (
        displayText
      )}
      {onClick && <PopupIcon size={9.5} />}
    </span>
  );
};

/**
 * Interactive Year-over-Year delta badge that opens the multi-year trend popup
 * on click.
 */
const YoYBadge: React.FC<{
  readonly yoy: YoYIndicator;
  readonly onClick?: (event: React.MouseEvent<HTMLElement>) => void;
  readonly tooltip?: string;
  readonly style?: React.CSSProperties;
}> = ({
  yoy,
  onClick,
  tooltip = 'Click to view historical multi-year trend',
  style,
}) => {
  const isClickable = Boolean(onClick);
  const bgColor =
    yoy.direction === 'up'
      ? '#fce8e6'
      : yoy.direction === 'down'
        ? '#e6f4ea'
        : '#f1f3f4';
  const hoverBgColor =
    yoy.direction === 'up'
      ? '#fad2cf'
      : yoy.direction === 'down'
        ? '#ceead6'
        : '#e8eaed';

  return (
    <span
      onClick={onClick}
      title={isClickable ? tooltip : undefined}
      style={{
        fontSize: '11px',
        fontWeight: 600,
        color: yoy.color,
        backgroundColor: bgColor,
        padding: '2px 6px',
        borderRadius: '4px',
        cursor: isClickable ? 'pointer' : 'default',
        transition: 'background-color 0.15s ease',
        userSelect: 'none',
        display: 'inline-flex',
        alignItems: 'center',
        gap: '3.5px',
        ...style,
      }}
      onMouseEnter={(event) => {
        if (isClickable) {
          event.currentTarget.style.backgroundColor = hoverBgColor;
          event.currentTarget.style.textDecoration = 'underline';
        }
      }}
      onMouseLeave={(event) => {
        if (isClickable) {
          event.currentTarget.style.backgroundColor = bgColor;
          event.currentTarget.style.textDecoration = 'none';
        }
      }}>
      {yoy.text}
      {isClickable && <PopupIcon size={9.5} />}
    </span>
  );
};

/**
 * Returns true when `value` is a non-empty string other than
 * `NOT_AVAILABLE_LABEL`.
 */
export function hasDisplayValue(value: string | undefined): value is string {
  return Boolean(value && value !== NOT_AVAILABLE_LABEL);
}

/** Props for `HeroImpactCard`. */
export interface HeroImpactCardProps {
  readonly title: string;
  readonly isLoading?: boolean;
  readonly metrics: HeroScopeMetrics;
  /**
   * Card layout variant: `'country'` renders the compact/expandable National
   * GHG card; `'fir'` renders the bottom `'More metrics'` toggle button.
   */
  readonly variant?: 'country' | 'fir';
  readonly onYoYClick?: (event: React.MouseEvent<HTMLElement>) => void;
  readonly onAbsoluteRankClick?: (event: React.MouseEvent<HTMLElement>) => void;
  readonly onPerCapitaRankClick?: (
    event: React.MouseEvent<HTMLElement>,
  ) => void;
  readonly onNationalGhgRankClick?: (
    event: React.MouseEvent<HTMLElement>,
  ) => void;
  readonly heroIcon?: React.ReactNode;
  readonly optionalMetrics?: React.ReactNode;
}

interface CollapsedNationalGhgTileProps {
  readonly nationalGhgComparison: string;
  readonly isLoading: boolean;
  readonly onExpand: () => void;
}

const CollapsedNationalGhgTile: React.FC<CollapsedNationalGhgTileProps> = ({
  nationalGhgComparison,
  isLoading,
  onExpand,
}) => (
  <Box
    style={{
      flex: '1 1 auto',
      display: 'flex',
      justifyContent: 'center',
      alignItems: 'center',
      minWidth: 'fit-content',
    }}>
    <Box
      onClick={onExpand}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '6px',
        padding: '4px 8px',
        backgroundColor: '#f8f9fa',
        borderRadius: '6px',
        border: '1px solid #ebebeb',
        cursor: 'pointer',
        transition: 'all 0.15s ease',
        flexShrink: 0,
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.backgroundColor = '#f1f3f4';
        e.currentTarget.style.borderColor = '#dadce0';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.backgroundColor = '#f8f9fa';
        e.currentTarget.style.borderColor = '#ebebeb';
      }}
      title="Expand more metrics"
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          onExpand();
        }
      }}>
      <Box style={{display: 'flex', flexDirection: 'column'}}>
        <Box
          display="flex"
          alignItems="center"
          style={{gap: '4px', marginBottom: '1px'}}>
          <FactoryIcon />
          <span
            style={{
              fontSize: '11px',
              color: '#5f6368',
              fontWeight: 600,
            }}>
            vs. National GHG
          </span>
        </Box>
        <Box display="flex" alignItems="baseline" style={{gap: '3px'}}>
          <MetricValue
            value={nationalGhgComparison}
            isLoading={isLoading}
            size="14px"
          />
          {!isLoading && (
            <span style={{fontSize: '10.5px', color: '#80868b'}}>
              of national GHG
            </span>
          )}
        </Box>
      </Box>
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="currentColor"
        style={{
          color: '#5f6368',
          marginLeft: '2px',
          flexShrink: 0,
        }}>
        <path d="M7 10l5 5 5-5z" />
      </svg>
    </Box>
  </Box>
);

interface FirMoreMetricsSectionProps {
  readonly isExpanded: boolean;
  readonly onToggle: () => void;
  readonly optionalMetrics: React.ReactNode;
}

const FirMoreMetricsSection: React.FC<FirMoreMetricsSectionProps> = ({
  isExpanded,
  onToggle,
  optionalMetrics,
}) => {
  const toggleLabel = isExpanded ? 'Hide more metrics' : 'More metrics';
  return (
    <>
      <Box
        style={{
          marginTop: '2px',
          marginBottom: isExpanded ? '8px' : '0px',
        }}>
        <button
          onClick={(e) => {
            e.stopPropagation();
            onToggle();
          }}
          title={toggleLabel}
          aria-label={toggleLabel}
          style={{
            background: 'transparent',
            border: 'none',
            borderRadius: '4px',
            cursor: 'pointer',
            padding: '2px 4px',
            color: '#5f6368',
            display: 'inline-flex',
            alignItems: 'center',
            gap: '4px',
            fontSize: '11px',
            fontWeight: 500,
            transition: 'background-color 0.15s ease',
          }}
          onMouseEnter={(e) =>
            (e.currentTarget.style.backgroundColor = '#e8eaed')
          }
          onMouseLeave={(e) =>
            (e.currentTarget.style.backgroundColor = 'transparent')
          }>
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="currentColor"
            style={{
              transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)',
              transition: 'transform 0.2s ease',
            }}>
            <path d="M7 10l5 5 5-5z" />
          </svg>
          <span>{toggleLabel}</span>
        </button>
      </Box>
      {isExpanded && (
        <Box
          display="flex"
          flexWrap="wrap"
          style={{
            gap: '20px',
            alignItems: 'flex-start',
            marginTop: '4px',
          }}>
          {optionalMetrics}
        </Box>
      )}
    </>
  );
};

interface ExpandedNationalGhgCellProps {
  readonly metrics: HeroScopeMetrics;
  readonly isLoading: boolean;
  readonly onCollapse: () => void;
  readonly onNationalGhgRankClick?: (
    event: React.MouseEvent<HTMLElement>,
  ) => void;
}

const ExpandedNationalGhgCell: React.FC<ExpandedNationalGhgCellProps> = ({
  metrics,
  isLoading,
  onCollapse,
  onNationalGhgRankClick,
}) => {
  const {
    nationalGhgComparison,
    nationalGhgRank,
    nationalGhgRankNum,
    nationalGhgTotal,
    totalCount,
  } = metrics;
  return (
    <Box
      onClick={onCollapse}
      style={{
        minWidth: '130px',
        cursor: 'pointer',
        borderRadius: '6px',
        padding: '4px 6px',
        margin: '-4px -6px',
        transition: 'background-color 0.15s ease',
      }}
      onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = '#f1f3f4')}
      onMouseLeave={(e) =>
        (e.currentTarget.style.backgroundColor = 'transparent')
      }
      title="Click to collapse"
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          onCollapse();
        }
      }}>
      <Box
        display="flex"
        alignItems="center"
        style={{gap: '5px', marginBottom: '2px'}}>
        <FactoryIcon />
        <span
          style={{
            fontSize: '11.5px',
            color: '#5f6368',
            fontWeight: 600,
          }}>
          vs. National GHG
        </span>
      </Box>
      <Box display="flex" alignItems="baseline" style={{gap: '4px'}}>
        <MetricValue
          value={nationalGhgComparison ?? NOT_AVAILABLE_LABEL}
          isLoading={isLoading}
          size="14.5px"
        />
        {!isLoading && (
          <span style={{fontSize: '11px', color: '#80868b'}}>
            of national GHG
          </span>
        )}
      </Box>
      {!isLoading &&
        hasDisplayValue(nationalGhgRank) &&
        onNationalGhgRankClick && (
          <RankClickableBadge
            text={nationalGhgRank}
            rankNum={nationalGhgRankNum}
            totalCount={totalCount}
            onClick={onNationalGhgRankClick}
          />
        )}
      {!isLoading && nationalGhgTotal && (
        <span
          style={{
            fontSize: '11px',
            color: '#5f6368',
            display: 'block',
            marginTop: '2px',
          }}>
          ({nationalGhgTotal} national GHG)
        </span>
      )}
    </Box>
  );
};

interface ExpandedPerCapitaCellProps {
  readonly metrics: HeroScopeMetrics;
  readonly isLoading: boolean;
  readonly onPerCapitaRankClick?: (
    event: React.MouseEvent<HTMLElement>,
  ) => void;
}

const ExpandedPerCapitaCell: React.FC<ExpandedPerCapitaCellProps> = ({
  metrics,
  isLoading,
  onPerCapitaRankClick,
}) => {
  const {
    perCapitaValue,
    perCapitaRank,
    perCapitaRankNum,
    perCapitaVsWorldAvg,
    populationFormatted,
    totalCount,
  } = metrics;
  if (perCapitaValue === undefined) {
    return null;
  }
  const subtitle = perCapitaVsWorldAvg || populationFormatted;
  return (
    <Box style={{minWidth: '130px'}}>
      <Box
        display="flex"
        alignItems="center"
        style={{gap: '5px', marginBottom: '2px'}}>
        <PersonIcon />
        <span
          style={{
            fontSize: '11.5px',
            color: '#5f6368',
            fontWeight: 600,
          }}>
          Per capita
        </span>
      </Box>
      <Box display="flex" alignItems="baseline" style={{gap: '4px'}}>
        <MetricValue
          value={perCapitaValue}
          isLoading={isLoading}
          size="14.5px"
        />
        {!isLoading && (
          <span style={{fontSize: '11px', color: '#80868b'}}>/ person</span>
        )}
      </Box>
      {!isLoading && hasDisplayValue(perCapitaRank) && onPerCapitaRankClick && (
        <RankClickableBadge
          text={perCapitaRank}
          rankNum={perCapitaRankNum}
          totalCount={totalCount}
          onClick={onPerCapitaRankClick}
        />
      )}
      {!isLoading && subtitle ? (
        <span
          style={{
            fontSize: '11px',
            color: '#5f6368',
            display: 'block',
            marginTop: '2px',
          }}>
          ({subtitle})
        </span>
      ) : null}
    </Box>
  );
};

interface ExpandedCountryMetricsCardProps {
  readonly metrics: HeroScopeMetrics;
  readonly isLoading: boolean;
  readonly onCollapse: () => void;
  readonly onPerCapitaRankClick?: (
    event: React.MouseEvent<HTMLElement>,
  ) => void;
  readonly onNationalGhgRankClick?: (
    event: React.MouseEvent<HTMLElement>,
  ) => void;
  readonly optionalMetrics?: React.ReactNode;
}

const ExpandedCountryMetricsCard: React.FC<ExpandedCountryMetricsCardProps> = ({
  metrics,
  isLoading,
  onCollapse,
  onPerCapitaRankClick,
  onNationalGhgRankClick,
  optionalMetrics,
}) => (
  <Box
    style={{
      position: 'relative',
      padding: '10px 14px',
      backgroundColor: '#f8f9fa',
      borderRadius: '8px',
      border: '1px solid #dadce0',
      transition: 'all 0.15s ease',
      marginTop: '4px',
      marginBottom: '0px',
    }}>
    <button
      onClick={(e) => {
        e.stopPropagation();
        onCollapse();
      }}
      title="Collapse metrics"
      aria-label="Collapse metrics"
      style={{
        position: 'absolute',
        top: '6px',
        right: '6px',
        background: 'transparent',
        border: 'none',
        borderRadius: '4px',
        cursor: 'pointer',
        padding: '2px 4px',
        color: '#5f6368',
        display: 'flex',
        alignItems: 'center',
        gap: '2px',
        fontSize: '11px',
        fontWeight: 500,
        transition: 'background-color 0.15s ease',
      }}
      onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = '#e8eaed')}
      onMouseLeave={(e) =>
        (e.currentTarget.style.backgroundColor = 'transparent')
      }>
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="currentColor"
        style={{
          transform: 'rotate(180deg)',
          transition: 'transform 0.2s ease',
        }}>
        <path d="M7 10l5 5 5-5z" />
      </svg>
    </button>

    <Box
      style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
        gap: '12px',
        paddingRight: '22px',
      }}>
      <ExpandedNationalGhgCell
        metrics={metrics}
        isLoading={isLoading}
        onCollapse={onCollapse}
        onNationalGhgRankClick={onNationalGhgRankClick}
      />
      <ExpandedPerCapitaCell
        metrics={metrics}
        isLoading={isLoading}
        onPerCapitaRankClick={onPerCapitaRankClick}
      />
      {optionalMetrics}
    </Box>
  </Box>
);

/**
 * Expandable hero card displaying primary CO2e warming impact, optional
 * national GHG share and per-capita metrics, and secondary scope metrics.
 */
export const HeroImpactCard: React.FC<HeroImpactCardProps> = ({
  title,
  isLoading = false,
  metrics,
  variant = 'country',
  onYoYClick,
  onAbsoluteRankClick,
  onPerCapitaRankClick,
  onNationalGhgRankClick,
  heroIcon,
  optionalMetrics,
}) => {
  const [isExpanded, setIsExpanded] = React.useState<boolean>(false);
  const {
    absoluteValue,
    absoluteYoY,
    absoluteRank,
    absoluteRankNum,
    nationalGhgComparison,
    totalCount,
  } = metrics;
  const isCountryVariant = variant === 'country';
  const hasAbsoluteRank =
    !isLoading && hasDisplayValue(absoluteRank) && Boolean(onAbsoluteRankClick);

  return (
    <Box
      style={{
        width: '100%',
        maxWidth: '560px',
        minWidth: '280px',
        display: 'flex',
        flexDirection: 'column',
      }}>
      <span
        style={{
          fontSize: '12px',
          fontWeight: 600,
          color: '#5f6368',
          display: 'block',
          marginBottom: '4px',
        }}>
        {title}
      </span>

      <Box
        display="flex"
        alignItems="center"
        justifyContent="space-between"
        flexWrap="wrap"
        style={{gap: '10px', marginBottom: isExpanded ? '6px' : '4px'}}>
        <Box
          display="flex"
          alignItems="center"
          flexWrap="wrap"
          style={{gap: '8px'}}>
          <MetricValue
            value={absoluteValue}
            isLoading={isLoading}
            icon={heroIcon}
            size="20px"
          />
          {(absoluteYoY || hasAbsoluteRank) && (
            <Box
              display="flex"
              flexDirection="column"
              alignItems="flex-start"
              style={{gap: '2px'}}>
              {absoluteYoY && (
                <YoYBadge yoy={absoluteYoY} onClick={onYoYClick} />
              )}
              {!isLoading &&
                hasDisplayValue(absoluteRank) &&
                onAbsoluteRankClick && (
                  <RankClickableBadge
                    text={absoluteRank}
                    rankNum={absoluteRankNum}
                    totalCount={totalCount}
                    onClick={onAbsoluteRankClick}
                    style={{marginTop: 0, marginBottom: 0}}
                  />
                )}
            </Box>
          )}
        </Box>

        {isCountryVariant && !isExpanded && (
          <CollapsedNationalGhgTile
            nationalGhgComparison={nationalGhgComparison ?? NOT_AVAILABLE_LABEL}
            isLoading={isLoading}
            onExpand={() => {
              setIsExpanded(true);
            }}
          />
        )}
      </Box>

      {/* For individual FIR cards, render "More metrics" toggle */}
      {!isCountryVariant && optionalMetrics && (
        <FirMoreMetricsSection
          isExpanded={isExpanded}
          onToggle={() => {
            setIsExpanded((prev) => !prev);
          }}
          optionalMetrics={optionalMetrics}
        />
      )}

      {isCountryVariant && isExpanded && (
        <ExpandedCountryMetricsCard
          metrics={metrics}
          isLoading={isLoading}
          onCollapse={() => {
            setIsExpanded(false);
          }}
          onPerCapitaRankClick={onPerCapitaRankClick}
          onNationalGhgRankClick={onNationalGhgRankClick}
          optionalMetrics={optionalMetrics}
        />
      )}
    </Box>
  );
};

/** Minimal FIR record shape required by `computeFirIndex`. */
export declare interface FirIdentifiable {
  readonly firId?: string;
}

/**
 * Resolves the active FIR array index from `selectedFirId` (case-insensitive)
 * or falls back to `selectedFirIdx` clamped to `[0, records.length - 1]`.
 * @visibleForTesting
 */
export function computeFirIndex(
  records: readonly FirIdentifiable[] | undefined | null,
  selectedFirIdx: number,
  selectedFirId?: string | null,
): number {
  if (!records || records.length === 0) {
    return 0;
  }
  const safeSelectedIdx =
    selectedFirIdx >= 0 && selectedFirIdx < records.length ? selectedFirIdx : 0;

  if (selectedFirId) {
    const selId = String(selectedFirId || '')
      .trim()
      .toUpperCase();
    const idxById = records.findIndex(
      (r) =>
        String(r?.firId || '')
          .trim()
          .toUpperCase() === selId,
    );
    if (idxById !== -1) {
      return idxById;
    }
  }

  return safeSelectedIdx;
}

/**
 * Clears `isLoading` once initial backend fetches have settled, preventing
 * infinite loading spinners or regression to the loading state on unrelated
 * cache notifications when a country record does not exist.
 * @visibleForTesting
 */
export function resolveSettledLoadingState(
  raw: CountryData,
  fetchSettled: boolean,
): CountryData {
  if (!fetchSettled || !raw.isLoading) return raw;
  return {
    ...raw,
    isLoading: false,
  };
}
