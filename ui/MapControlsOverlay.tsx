/**
 * @fileoverview Map overlay controls, playback timeline scrubber, boundary
 * legend, status/loading banners, and vertical colorbar legend for the
 * Regional Contrails Dashboard.
 * @jsx React.createElement
 * @jsxFrag React.Fragment
 */

import {Box, CircularProgress, Typography} from 'material-ui';
import * as React from 'react';
import {
  getCountryFirBoundaryInfo,
  getCountryNameFromIso2,
} from './CountryMetadata';
import {
  GridAvailability,
  HOUR_CODES,
  MAPLIBRE_NETWORK_ERROR_TEXT,
} from './dashboard_geo_utils';
import {
  DENSITY_COLORBAR_GRADIENT,
  densityLegendTickValues,
  formatDensityLegendTick,
  formatImpactLegendTick,
  GwpTonnesPerMj,
  IMPACT_COLORBAR_GRADIENT,
  impactLegendTickValues,
  impactLegendTitle,
  toGwpHorizonKey,
} from './geo_raster_utils';
import {
  extractIcaoFromFirId,
  MONTH_FULL_NAMES,
  MONTH_SHORT_NAMES,
} from './metric_formatters';
import {FilterState} from './RegionalFilterDrawer';

/** Active raster metric overlay mode on the map. */
export type OverlayMode = 'impact' | 'density';

/** Grid playback modes offered by the timeline controls. */
export type PlaybackMode = 'all' | 'monthly' | 'hourly';

/** Initialization status of the MapLibre GL JS library and map instance. */
export type MapLibreStatus = 'loading' | 'ready' | 'error';

/** Loading state of the currently selected heatmap raster grid. */
export type GridStatus = 'loading' | 'ready' | 'no_data';

/** Progress counter while preloading a monthly or hourly grid sequence. */
export interface GridLoadProgress {
  readonly loaded: number;
  readonly total: number;
}

/** Pixel dimensions of the active raster grid. */
export interface GridDimensions {
  readonly width: number;
  readonly height: number;
}

/**
 * Returns `prev` if `availability` still has grids for it, or else the first
 * available fallback mode (`'all'` if nothing else is available).
 */
export function reconcilePlaybackModeWithAvailability(
  prev: PlaybackMode,
  availability: GridAvailability,
): PlaybackMode {
  if (prev === 'monthly' && !availability.hasMonthly) {
    return availability.hasTotal
      ? 'all'
      : availability.hasHourly
        ? 'hourly'
        : 'all';
  }
  if (prev === 'hourly' && !availability.hasHourly) {
    return availability.hasTotal
      ? 'all'
      : availability.hasMonthly
        ? 'monthly'
        : 'all';
  }
  if (prev === 'all' && !availability.hasTotal) {
    return availability.hasMonthly
      ? 'monthly'
      : availability.hasHourly
        ? 'hourly'
        : 'all';
  }
  return prev;
}

/** Props for the `MapModeToggleBar` toolbar above the map. */
export interface MapModeToggleBarProps {
  readonly overlayMode: OverlayMode;
  readonly onToggleOverlayMode: () => void;
  readonly playbackMode: PlaybackMode;
  readonly gridAvailability: GridAvailability;
  readonly onSelectPlaybackMode: (mode: PlaybackMode) => void;
}

const OVERLAY_SWITCH_BUTTON_STYLE: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: '8px',
  height: '32px',
  padding: '4px 10px',
  borderRadius: '8px',
  border: '1px solid #dadce0',
  backgroundColor: '#ffffff',
  cursor: 'pointer',
  boxShadow: '0 1px 2px rgba(0,0,0,0.06)',
  outline: 'none',
  userSelect: 'none',
};

const TEMPORAL_GROUP_STYLE: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  height: '32px',
  padding: '2px',
  borderRadius: '8px',
  border: '1px solid #dadce0',
  backgroundColor: '#ffffff',
  boxShadow: '0 1px 2px rgba(0,0,0,0.06)',
  boxSizing: 'border-box',
  gap: '2px',
  userSelect: 'none',
};

const TEMPORAL_MODE_OPTIONS = [
  {key: 'all' as const, label: 'Yearly data'},
  {key: 'monthly' as const, label: 'Monthly'},
  {key: 'hourly' as const, label: 'Hourly'},
] as const;

function getOverlaySwitchLabelStyle(
  isActive: boolean,
  activeColor: string,
): React.CSSProperties {
  return {
    fontSize: '12px',
    fontWeight: isActive ? 700 : 500,
    color: isActive ? activeColor : '#70757a',
    transition: 'color 0.15s ease',
  };
}

function getOverlaySwitchTrackStyle(
  overlayMode: OverlayMode,
): React.CSSProperties {
  const isImpact = overlayMode === 'impact';
  return {
    position: 'relative',
    boxSizing: 'border-box',
    width: '32px',
    height: '18px',
    borderRadius: '9px',
    backgroundColor: isImpact ? '#fce8e6' : '#e8f0fe',
    border: isImpact ? '1px solid #f28b82' : '1px solid #8ab4f8',
    transition: 'background-color 0.2s ease, border-color 0.2s ease',
    flexShrink: 0,
  };
}

function getOverlaySwitchThumbStyle(
  overlayMode: OverlayMode,
): React.CSSProperties {
  const isImpact = overlayMode === 'impact';
  return {
    position: 'absolute',
    boxSizing: 'border-box',
    top: '50%',
    transform: 'translateY(-50%)',
    left: isImpact ? '2px' : '16px',
    width: '12px',
    height: '12px',
    borderRadius: '50%',
    backgroundColor: isImpact ? '#d9381e' : '#1a73e8',
    boxShadow: '0 0.5px 1.5px rgba(0,0,0,0.18)',
    transition:
      'left 0.2s cubic-bezier(0.4, 0, 0.2, 1), background-color 0.2s ease',
  };
}

interface OverlayModeSwitchProps {
  readonly overlayMode: OverlayMode;
  readonly onToggleOverlayMode: () => void;
}

const OverlayModeSwitch: React.FC<OverlayModeSwitchProps> = (props) => {
  const {overlayMode, onToggleOverlayMode} = props;
  return (
    <button
      type="button"
      role="switch"
      aria-checked={overlayMode === 'density'}
      onClick={onToggleOverlayMode}
      style={OVERLAY_SWITCH_BUTTON_STYLE}
      title="Toggle between Contrail Impact and Flight Density">
      <span
        style={getOverlaySwitchLabelStyle(overlayMode === 'impact', '#d9381e')}>
        🔥 Contrail Impact
      </span>
      <div style={getOverlaySwitchTrackStyle(overlayMode)}>
        <div style={getOverlaySwitchThumbStyle(overlayMode)} />
      </div>
      <span
        style={getOverlaySwitchLabelStyle(
          overlayMode === 'density',
          '#1a73e8',
        )}>
        ✈️ Flight Density
      </span>
    </button>
  );
};

function getTemporalButtonStyle(
  isSelected: boolean,
  isDisabled: boolean,
): React.CSSProperties {
  return {
    height: '26px',
    padding: '0 10px',
    borderRadius: '6px',
    border: 'none',
    backgroundColor: isSelected ? '#e8f0fe' : 'transparent',
    color: isSelected ? '#1a73e8' : '#5f6368',
    fontSize: '12px',
    fontWeight: isSelected ? 600 : 500,
    cursor: isDisabled ? 'not-allowed' : 'pointer',
    opacity: isDisabled ? 0.45 : 1,
    outline: 'none',
    userSelect: 'none',
    transition: 'background-color 0.15s ease, color 0.15s ease',
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    whiteSpace: 'nowrap',
  };
}

interface TemporalModeOptionButtonProps {
  readonly modeKey: PlaybackMode;
  readonly label: string;
  readonly isSelected: boolean;
  readonly isDisabled: boolean;
  readonly onSelectPlaybackMode: (mode: PlaybackMode) => void;
}

const TemporalModeOptionButton: React.FC<TemporalModeOptionButtonProps> = (
  props,
) => {
  const {modeKey, label, isSelected, isDisabled, onSelectPlaybackMode} = props;
  const titleText = isDisabled
    ? `Temporal data not available for ${label}`
    : `Select ${label} view`;
  return (
    <button
      disabled={isDisabled}
      onClick={() => {
        onSelectPlaybackMode(modeKey);
      }}
      style={getTemporalButtonStyle(isSelected, isDisabled)}
      onMouseEnter={(e) => {
        if (!isSelected && !isDisabled) {
          e.currentTarget.style.backgroundColor = '#f1f3f4';
          e.currentTarget.style.color = '#202124';
        }
      }}
      onMouseLeave={(e) => {
        if (!isSelected && !isDisabled) {
          e.currentTarget.style.backgroundColor = 'transparent';
          e.currentTarget.style.color = '#5f6368';
        }
      }}
      title={titleText}>
      {label}
    </button>
  );
};

/**
 * Returns whether the given temporal playback option is disabled based on
 * `availability` (`'monthly'` and `'hourly'` require their respective grids).
 */
function isTemporalOptionDisabled(
  key: PlaybackMode,
  availability: GridAvailability,
): boolean {
  return (
    (key === 'monthly' && !availability.hasMonthly) ||
    (key === 'hourly' && !availability.hasHourly)
  );
}

const MapModeToggleBarInner: React.FC<MapModeToggleBarProps> = (props) => (
  <Box
    display="flex"
    alignItems="center"
    justifyContent="space-between"
    flexWrap="wrap"
    style={{marginBottom: '10px', gap: '8px', flex: '0 0 auto'}}>
    <Box
      display="flex"
      alignItems="center"
      flexWrap="wrap"
      style={{gap: '8px'}}>
      <OverlayModeSwitch
        overlayMode={props.overlayMode}
        onToggleOverlayMode={props.onToggleOverlayMode}
      />
      <div
        role="group"
        aria-label="Temporal view mode"
        style={TEMPORAL_GROUP_STYLE}>
        {TEMPORAL_MODE_OPTIONS.map(({key, label}) => (
          <TemporalModeOptionButton
            key={key}
            modeKey={key}
            label={label}
            isSelected={props.playbackMode === key}
            isDisabled={isTemporalOptionDisabled(key, props.gridAvailability)}
            onSelectPlaybackMode={props.onSelectPlaybackMode}
          />
        ))}
      </div>
    </Box>
  </Box>
);

/**
 * Toolbar rendered above the MapLibre viewport with the Contrail Impact vs.
 * Flight Density switch and the Yearly / Monthly / Hourly temporal mode
 * buttons.
 */
export const MapModeToggleBar = React.memo(MapModeToggleBarInner);

const TIMELINE_CARD_STYLE: React.CSSProperties = {
  position: 'absolute',
  top: '10px',
  left: '12px',
  right: '56px',
  zIndex: 10,
  backgroundColor: 'rgba(255, 255, 255, 0.95)',
  backdropFilter: 'blur(8px)',
  border: '1px solid rgba(0, 0, 0, 0.12)',
  borderRadius: '10px',
  padding: '8px 12px',
  boxShadow: '0 2px 10px rgba(0, 0, 0, 0.12)',
  display: 'flex',
  flexDirection: 'column',
  gap: '6px',
  userSelect: 'none',
};

const TIMELINE_TITLE_STYLE: React.CSSProperties = {
  fontSize: '12.5px',
  fontWeight: 600,
  color: '#202124',
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
};

const BUFFERING_BADGE_STYLE: React.CSSProperties = {
  fontSize: '10px',
  fontWeight: 600,
  color: '#1a73e8',
  backgroundColor: '#e8f0fe',
  padding: '1px 6px',
  borderRadius: '4px',
  whiteSpace: 'nowrap',
};

function getPlayPauseButtonStyle(isPlaying: boolean): React.CSSProperties {
  return {
    background: isPlaying ? '#f1f3f4' : '#e8f0fe',
    border: isPlaying ? '1px solid #dadce0' : '1px solid #1a73e8',
    borderRadius: '5px',
    padding: '2px 8px',
    fontSize: '11px',
    fontWeight: 600,
    color: isPlaying ? '#3c4043' : '#1a73e8',
    cursor: 'pointer',
    display: 'inline-flex',
    alignItems: 'center',
    gap: '4px',
    transition: 'all 0.15s ease',
  };
}

function getTimelineStepStyle(
  isActive: boolean,
  isHourly: boolean,
): React.CSSProperties {
  return {
    padding: isHourly ? '3px 0' : '4px 0',
    borderRadius: isHourly ? '3px' : '4px',
    border: 'none',
    backgroundColor: isActive ? '#1a73e8' : '#f1f3f4',
    color: isActive ? '#ffffff' : isHourly ? '#5f6368' : '#3c4043',
    fontSize: isHourly ? '9px' : '11px',
    fontWeight: isActive ? 700 : 500,
    cursor: 'pointer',
    textAlign: 'center',
    transition: isHourly ? 'all 0.12s ease' : 'all 0.15s ease',
    boxShadow: isActive ? '0 1px 3px rgba(26,115,232,0.35)' : 'none',
    ...(isHourly ? {lineHeight: '1.2', letterSpacing: '-0.3px'} : {}),
  };
}

interface TimelineStepButtonProps {
  readonly label: string;
  readonly title: string;
  readonly isActive: boolean;
  readonly isHourly: boolean;
  readonly onClick: () => void;
}

const TimelineStepButton: React.FC<TimelineStepButtonProps> = (props) => {
  const {label, title, isActive, isHourly, onClick} = props;
  return (
    <button
      onClick={onClick}
      title={title}
      style={getTimelineStepStyle(isActive, isHourly)}
      onMouseEnter={(e) => {
        if (!isActive) {
          e.currentTarget.style.backgroundColor = '#e8f0fe';
          e.currentTarget.style.color = '#1a73e8';
        }
      }}
      onMouseLeave={(e) => {
        if (!isActive) {
          e.currentTarget.style.backgroundColor = '#f1f3f4';
          e.currentTarget.style.color = isHourly ? '#5f6368' : '#3c4043';
        }
      }}>
      {label}
    </button>
  );
};

interface TimelineHeaderRowProps {
  readonly titleText: string;
  readonly isBufferingSequence: boolean;
  readonly isPlaying: boolean;
  readonly onTogglePlaying: () => void;
}

const TimelineHeaderRow: React.FC<TimelineHeaderRowProps> = (props) => {
  const {titleText, isBufferingSequence, isPlaying, onTogglePlaying} = props;
  const playTitle = isPlaying
    ? 'Pause timeline playback'
    : 'Resume timeline playback';
  return (
    <Box
      display="flex"
      alignItems="center"
      justifyContent="space-between"
      style={{gap: '8px'}}>
      <Box display="flex" alignItems="center" style={{gap: '6px', minWidth: 0}}>
        <Typography style={TIMELINE_TITLE_STYLE}>{titleText}</Typography>
        {isBufferingSequence && (
          <span style={BUFFERING_BADGE_STYLE}>Buffering...</span>
        )}
      </Box>
      <Box
        display="flex"
        alignItems="center"
        style={{gap: '6px', flexShrink: 0}}>
        {!isBufferingSequence && (
          <button
            onClick={onTogglePlaying}
            style={getPlayPauseButtonStyle(isPlaying)}
            title={playTitle}>
            <span>{isPlaying ? '⏸' : '▶'}</span>
            <span>{isPlaying ? 'Pause' : 'Play'}</span>
          </button>
        )}
      </Box>
    </Box>
  );
};

interface TimelineTrackGridProps {
  readonly isMonthly: boolean;
  readonly currentMonthIdx: number;
  readonly currentHourIdx: number;
  readonly year: number;
  readonly onSelectMonthIdx: (idx: number) => void;
  readonly onSelectHourIdx: (idx: number) => void;
}

function getTimelineGridStyle(isMonthly: boolean): React.CSSProperties {
  return {
    display: 'grid',
    gridTemplateColumns: isMonthly ? 'repeat(12, 1fr)' : 'repeat(24, 1fr)',
    gap: isMonthly ? '3px' : '2px',
    width: '100%',
  };
}

const TimelineTrackGrid: React.FC<TimelineTrackGridProps> = (props) => {
  const {isMonthly, currentMonthIdx, currentHourIdx, year} = props;
  return (
    <Box style={getTimelineGridStyle(isMonthly)}>
      {isMonthly
        ? MONTH_SHORT_NAMES.map((shortName, idx) => (
            <TimelineStepButton
              key={shortName}
              label={shortName}
              title={
                `Jump to ${MONTH_FULL_NAMES[idx]} ${year} ` +
                '(pauses playback)'
              }
              isActive={idx === currentMonthIdx}
              isHourly={false}
              onClick={() => {
                props.onSelectMonthIdx(idx);
              }}
            />
          ))
        : HOUR_CODES.map((hCode, idx) => (
            <TimelineStepButton
              key={hCode}
              label={hCode}
              title={`Jump to Local Hour ${hCode}:00 (pauses playback)`}
              isActive={idx === currentHourIdx}
              isHourly={true}
              onClick={() => {
                props.onSelectHourIdx(idx);
              }}
            />
          ))}
    </Box>
  );
};

interface TimelineScrubberCardProps {
  readonly playbackMode: PlaybackMode;
  readonly currentMonthIdx: number;
  readonly currentHourIdx: number;
  readonly year: number;
  readonly isPlaying: boolean;
  readonly isBufferingSequence: boolean;
  readonly onTogglePlaying: () => void;
  readonly onSelectMonthIdx: (idx: number) => void;
  readonly onSelectHourIdx: (idx: number) => void;
}

const TimelineScrubberCard: React.FC<TimelineScrubberCardProps> = (props) => {
  const isMonthly = props.playbackMode === 'monthly';
  const titleText = isMonthly
    ? `${MONTH_FULL_NAMES[props.currentMonthIdx]} ${props.year}`
    : `Local Hour ${HOUR_CODES[props.currentHourIdx]}:00`;
  return (
    <Box style={TIMELINE_CARD_STYLE}>
      <TimelineHeaderRow
        titleText={titleText}
        isBufferingSequence={props.isBufferingSequence}
        isPlaying={props.isPlaying}
        onTogglePlaying={props.onTogglePlaying}
      />
      <TimelineTrackGrid
        isMonthly={isMonthly}
        currentMonthIdx={props.currentMonthIdx}
        currentHourIdx={props.currentHourIdx}
        year={props.year}
        onSelectMonthIdx={props.onSelectMonthIdx}
        onSelectHourIdx={props.onSelectHourIdx}
      />
    </Box>
  );
};

const BOUNDARY_KEY_CARD_STYLE: React.CSSProperties = {
  position: 'absolute',
  bottom: '16px',
  left: '16px',
  backgroundColor: 'rgba(255, 255, 255, 0.93)',
  backdropFilter: 'blur(6px)',
  border: '1px solid #dadce0',
  borderRadius: '8px',
  padding: '8px 12px',
  boxShadow: '0 2px 8px rgba(0,0,0,0.12)',
  zIndex: 10,
  display: 'flex',
  flexDirection: 'column',
  gap: '6px',
  fontSize: '11px',
  color: '#3c4043',
};

const COUNTRY_SWATCH_STYLE: React.CSSProperties = {
  width: '16px',
  height: '3px',
  backgroundColor: '#1a73e8',
  display: 'inline-block',
  borderRadius: '2px',
};

const DOMESTIC_FIR_SWATCH_STYLE: React.CSSProperties = {
  width: '16px',
  height: '2px',
  borderTop: '2.5px dashed #00bcd4',
  display: 'inline-block',
};

const INTERNATIONAL_FIR_SWATCH_STYLE: React.CSSProperties = {
  width: '16px',
  height: '2px',
  borderTop: '2.5px dashed #ab47bc',
  display: 'inline-block',
};

const SELECTED_FIR_SWATCH_BASE_STYLE: React.CSSProperties = {
  width: '16px',
  height: '10px',
  border: '1px solid rgba(128, 128, 128, 0.6)',
  display: 'inline-block',
  borderRadius: '2px',
};

const SELECTED_DOMESTIC_FIR_SWATCH_STYLE: React.CSSProperties = {
  ...SELECTED_FIR_SWATCH_BASE_STYLE,
  backgroundColor: 'rgba(0, 188, 212, 0.45)',
};

const SELECTED_INTERNATIONAL_FIR_SWATCH_STYLE: React.CSSProperties = {
  ...SELECTED_FIR_SWATCH_BASE_STYLE,
  backgroundColor: 'rgba(171, 71, 188, 0.38)',
};

/**
 * Returns whether `selectedFirId` matches any international FIR identifier in
 * `firIntFirIds` (either by full `FIR_ID` or by 4-letter ICAO prefix).
 *
 * @visibleForTesting
 */
export function isSelectedFirInternational(
  selectedFirId: string | null | undefined,
  firIntFirIds: readonly string[] | null | undefined,
): boolean {
  const cleanSelectedFirId = selectedFirId?.trim().toUpperCase() ?? '';
  if (!cleanSelectedFirId || !firIntFirIds || firIntFirIds.length === 0) {
    return false;
  }
  const selectedIcao = extractIcaoFromFirId(cleanSelectedFirId);
  return firIntFirIds.some((id) => {
    const cleanIntId = id.trim().toUpperCase();
    return (
      cleanIntId === cleanSelectedFirId ||
      (selectedIcao !== '' && extractIcaoFromFirId(cleanIntId) === selectedIcao)
    );
  });
}

interface BoundaryLegendRowProps {
  readonly swatchStyle: React.CSSProperties;
  readonly label: string;
}

const BoundaryLegendRow: React.FC<BoundaryLegendRowProps> = ({
  swatchStyle,
  label,
}) => (
  <Box display="flex" alignItems="center" style={{gap: '8px'}}>
    <span style={swatchStyle} />
    <span style={{fontWeight: 600}}>{label}</span>
  </Box>
);

interface BoundaryKeyLegendProps {
  readonly country: string;
  readonly year: number;
  readonly selectedFirId: string | null;
}

const BoundaryKeyLegend: React.FC<BoundaryKeyLegendProps> = ({
  country,
  year,
  selectedFirId,
}) => {
  const boundaryInfo = getCountryFirBoundaryInfo(country, year);
  const hasDom = Boolean(boundaryInfo?.hasDomesticFir);
  const hasInt = Boolean(boundaryInfo?.hasInternationalFir);
  const firLabel = selectedFirId
    ? extractIcaoFromFirId(selectedFirId) || selectedFirId
    : '';
  const countryLabel = getCountryNameFromIso2(country, year) || country;
  const isSelectedInternational = isSelectedFirInternational(
    selectedFirId,
    boundaryInfo?.firIntFirId,
  );
  return (
    <Box style={BOUNDARY_KEY_CARD_STYLE}>
      <BoundaryLegendRow
        swatchStyle={COUNTRY_SWATCH_STYLE}
        label={`Country border (${countryLabel})`}
      />
      {hasDom && (
        <BoundaryLegendRow
          swatchStyle={DOMESTIC_FIR_SWATCH_STYLE}
          label="Domestic FIR"
        />
      )}
      {hasInt && (
        <BoundaryLegendRow
          swatchStyle={INTERNATIONAL_FIR_SWATCH_STYLE}
          label="International FIR"
        />
      )}
      {selectedFirId && (
        <BoundaryLegendRow
          swatchStyle={
            isSelectedInternational
              ? SELECTED_INTERNATIONAL_FIR_SWATCH_STYLE
              : SELECTED_DOMESTIC_FIR_SWATCH_STYLE
          }
          label={`Selected FIR highlight (${firLabel})`}
        />
      )}
    </Box>
  );
};

const MAPLIBRE_INIT_BANNER_STYLE: React.CSSProperties = {
  position: 'absolute',
  top: '50%',
  left: '50%',
  transform: 'translate(-50%, -50%)',
  backgroundColor: 'rgba(255,255,255,0.92)',
  padding: '12px 24px',
  borderRadius: '8px',
  boxShadow: '0 1px 4px rgba(0,0,0,0.2)',
  fontWeight: 500,
  fontSize: '14px',
  color: '#3c4043',
};

const NO_DATA_BANNER_STYLE: React.CSSProperties = {
  position: 'absolute',
  top: '50%',
  left: '50%',
  transform: 'translate(-50%, -50%)',
  backgroundColor: 'rgba(255, 255, 255, 0.94)',
  border: '1px solid #dadce0',
  borderRadius: '8px',
  padding: '16px 28px',
  boxShadow: '0 4px 12px rgba(0,0,0,0.12)',
  textAlign: 'center',
  zIndex: 10,
};

const MAPLIBRE_ERROR_BANNER_STYLE: React.CSSProperties = {
  position: 'absolute',
  top: '50%',
  left: '50%',
  transform: 'translate(-50%, -50%)',
  backgroundColor: '#fce8e6',
  border: '1px solid #ea4335',
  padding: '16px 24px',
  borderRadius: '8px',
  textAlign: 'center',
  maxWidth: '420px',
};

const PROGRESS_TRACK_STYLE: React.CSSProperties = {
  width: '100%',
  height: '6px',
  backgroundColor: '#e8eaed',
  borderRadius: '3px',
  overflow: 'hidden',
};

const PROGRESS_TEXT_STYLE: React.CSSProperties = {
  fontSize: '11px',
  color: '#5f6368',
  textAlign: 'right',
  marginTop: '4px',
  fontWeight: 500,
};

function getGridLoadingCardStyle(total: number): React.CSSProperties {
  return {
    position: 'absolute',
    top: '50%',
    left: '50%',
    transform: 'translate(-50%, -50%)',
    backgroundColor: 'rgba(255, 255, 255, 0.96)',
    border: '1px solid #dadce0',
    borderRadius: '16px',
    padding: '14px 24px',
    boxShadow: '0 4px 16px rgba(0,0,0,0.16)',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    gap: '10px',
    minWidth: total > 1 ? '220px' : 'auto',
    zIndex: 10,
  };
}

const GridLoadingOverlay: React.FC<{
  readonly gridLoadProgress: GridLoadProgress;
}> = ({gridLoadProgress}) => {
  const {loaded, total} = gridLoadProgress;
  const pct = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
  return (
    <Box style={getGridLoadingCardStyle(total)}>
      <Box display="flex" alignItems="center" style={{gap: '12px'}}>
        <CircularProgress size={22} style={{color: '#1a73e8'}} />
        <Typography
          style={{fontSize: '14px', fontWeight: 500, color: '#3c4043'}}>
          Loading heatmap grid...
        </Typography>
      </Box>
      {total > 1 && (
        <Box style={{width: '100%'}}>
          <Box style={PROGRESS_TRACK_STYLE}>
            <Box
              style={{
                width: `${pct}%`,
                height: '100%',
                backgroundColor: '#1a73e8',
                borderRadius: '3px',
                transition: 'width 0.18s ease-out',
              }}
            />
          </Box>
          <Typography style={PROGRESS_TEXT_STYLE}>
            {loaded} / {total} grids loaded
          </Typography>
        </Box>
      )}
    </Box>
  );
};

const NO_DATA_TITLE_STYLE: React.CSSProperties = {
  fontWeight: 600,
  fontSize: '16px',
  color: '#202124',
  marginBottom: '4px',
};

const ERROR_TITLE_STYLE: React.CSSProperties = {
  fontWeight: 600,
  color: '#c5221f',
  fontSize: '15px',
  marginBottom: '8px',
};

interface MapStatusBannersProps {
  readonly mapLibreStatus: MapLibreStatus;
  readonly gridStatus: GridStatus;
  readonly isBufferingSequence: boolean;
  readonly gridLoadProgress: GridLoadProgress;
  readonly overlayMode: OverlayMode;
  readonly year: number;
}

const MapStatusBanners: React.FC<MapStatusBannersProps> = (props) => {
  const {mapLibreStatus, gridStatus, isBufferingSequence, gridLoadProgress} =
    props;
  return (
    <>
      {mapLibreStatus === 'loading' && (
        <Box style={MAPLIBRE_INIT_BANNER_STYLE}>
          Initializing MapLibre GL JS WebGL Tiles...
        </Box>
      )}
      {(gridStatus === 'loading' || isBufferingSequence) &&
        mapLibreStatus === 'ready' && (
          <GridLoadingOverlay gridLoadProgress={gridLoadProgress} />
        )}
      {gridStatus === 'no_data' && mapLibreStatus === 'ready' && (
        <Box style={NO_DATA_BANNER_STYLE}>
          <Typography style={NO_DATA_TITLE_STYLE}>No Data</Typography>
          <Typography style={{fontSize: '13px', color: '#5f6368'}}>
            No {props.overlayMode === 'density' ? 'density' : 'impact'} grid
            data available for {props.year}.
          </Typography>
        </Box>
      )}
      {mapLibreStatus === 'error' && (
        <Box style={MAPLIBRE_ERROR_BANNER_STYLE}>
          <Typography style={ERROR_TITLE_STYLE}>
            MapLibre GL JS Library Unavailable
          </Typography>
          <Typography
            style={{fontSize: '13px', color: '#3c4043', marginBottom: '12px'}}>
            {MAPLIBRE_NETWORK_ERROR_TEXT}
          </Typography>
        </Box>
      )}
    </>
  );
};

const COLORBAR_CARD_STYLE: React.CSSProperties = {
  position: 'absolute',
  bottom: '16px',
  right: '16px',
  backgroundColor: 'rgba(255, 255, 255, 0.92)',
  backdropFilter: 'blur(6px)',
  border: '1px solid #dadce0',
  borderRadius: '8px',
  padding: '10px 12px',
  boxShadow: '0 2px 10px rgba(0,0,0,0.15)',
  zIndex: 10,
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  minWidth: '110px',
};

const COLORBAR_TICK_COLUMN_STYLE: React.CSSProperties = {
  fontSize: '9.5px',
  fontWeight: 500,
  color: '#3c4043',
  fontFamily: 'Roboto, monospace',
};

const COLORBAR_MIDLINE_STYLE: React.CSSProperties = {
  position: 'absolute',
  top: '50%',
  left: 0,
  right: 0,
  height: '1px',
  backgroundColor: '#bdc1c6',
  transform: 'translateY(-50%)',
};

function getColorbarTitleStyle(isImpact: boolean): React.CSSProperties {
  return {
    fontSize: '10px',
    fontWeight: 600,
    color: '#3c4043',
    marginBottom: '8px',
    textAlign: 'center',
    textTransform: isImpact ? 'none' : 'uppercase',
    letterSpacing: '0.4px',
  };
}

function getColorbarGradientBoxStyle(isImpact: boolean): React.CSSProperties {
  return {
    position: 'relative',
    width: '14px',
    borderRadius: '3px',
    background: isImpact ? IMPACT_COLORBAR_GRADIENT : DENSITY_COLORBAR_GRADIENT,
    border: '1px solid #bdc1c6',
  };
}

interface ColorbarAxisTicksProps {
  readonly overlayMode: OverlayMode;
  readonly gridValueMax: number;
  readonly activeResolutionScale: number;
  readonly tonnesPerMj: number | undefined;
}

const ColorbarAxisTicks: React.FC<ColorbarAxisTicksProps> = (props) => {
  const {overlayMode, gridValueMax, activeResolutionScale, tonnesPerMj} = props;
  const isImpact = overlayMode === 'impact';
  const ticks = isImpact
    ? impactLegendTickValues(gridValueMax)
    : densityLegendTickValues(gridValueMax);
  return (
    <Box
      display="flex"
      flexDirection="column"
      justifyContent="space-between"
      style={COLORBAR_TICK_COLUMN_STYLE}>
      {ticks.map((refVal, idx) => {
        if (refVal === 0) {
          return isImpact ? (
            <span key={refVal} style={{color: '#202124', fontWeight: 700}}>
              0
            </span>
          ) : (
            <span key={refVal} style={{color: '#80868b'}}>
              0 flights
            </span>
          );
        }
        const label = isImpact
          ? formatImpactLegendTick(refVal, activeResolutionScale, tonnesPerMj)
          : formatDensityLegendTick(refVal, activeResolutionScale, {
              isTop: idx === 0,
            });
        return <span key={refVal}>{label}</span>;
      })}
    </Box>
  );
};

interface DensityColorbarLegendProps {
  readonly overlayMode: OverlayMode;
  readonly gwp: NonNullable<FilterState['gwp']>;
  readonly gridValueMax: number | undefined;
  readonly gridGwpFactors: GwpTonnesPerMj | undefined;
  readonly activeGridDimensions: GridDimensions;
}

const DensityColorbarLegend: React.FC<DensityColorbarLegendProps> = (props) => {
  const {overlayMode, gwp, gridValueMax, gridGwpFactors, activeGridDimensions} =
    props;
  const isImpact = overlayMode === 'impact';
  const tonnesPerMj = gridGwpFactors?.[toGwpHorizonKey(gwp)];
  const activeResolutionScale =
    (activeGridDimensions.width * activeGridDimensions.height) / (1200 * 600) ||
    1;
  return (
    <Box style={COLORBAR_CARD_STYLE}>
      <Typography style={getColorbarTitleStyle(isImpact)}>
        {isImpact ? impactLegendTitle(tonnesPerMj, gwp) : 'Flight Density'}
      </Typography>
      <Box
        display="flex"
        alignItems="stretch"
        style={{height: '220px', gap: '8px'}}>
        <Box style={getColorbarGradientBoxStyle(isImpact)}>
          {isImpact && <Box style={COLORBAR_MIDLINE_STYLE} />}
        </Box>
        {gridValueMax !== undefined && (
          <ColorbarAxisTicks
            overlayMode={overlayMode}
            gridValueMax={gridValueMax}
            activeResolutionScale={activeResolutionScale}
            tonnesPerMj={tonnesPerMj}
          />
        )}
      </Box>
    </Box>
  );
};

/** Props for the `MapControlsOverlay` floating overlays inside the map. */
export interface MapControlsOverlayProps {
  readonly playbackMode: PlaybackMode;
  readonly currentMonthIdx: number;
  readonly currentHourIdx: number;
  readonly isPlaying: boolean;
  readonly isBufferingSequence: boolean;
  readonly onTogglePlaying: () => void;
  readonly onSelectMonthIdx: (idx: number) => void;
  readonly onSelectHourIdx: (idx: number) => void;
  readonly mapLibreStatus: MapLibreStatus;
  readonly gridStatus: GridStatus;
  readonly gridLoadProgress: GridLoadProgress;
  readonly overlayMode: OverlayMode;
  readonly country: string;
  readonly year: number;
  readonly gwp?: FilterState['gwp'];
  readonly selectedFirId: string | null;
  readonly gridValueMax: number | undefined;
  readonly gridGwpFactors: GwpTonnesPerMj | undefined;
  readonly activeGridDimensions: GridDimensions;
}

const MapControlsOverlayInner: React.FC<MapControlsOverlayProps> = (props) => {
  const isMapReady = props.mapLibreStatus === 'ready';
  return (
    <>
      {props.playbackMode !== 'all' && <TimelineScrubberCard {...props} />}
      {isMapReady && Boolean(props.country) && (
        <BoundaryKeyLegend
          country={props.country}
          year={props.year}
          selectedFirId={props.selectedFirId}
        />
      )}
      <MapStatusBanners {...props} />
      {isMapReady && props.gridStatus === 'ready' && (
        <DensityColorbarLegend
          overlayMode={props.overlayMode}
          gwp={props.gwp ?? 'GWP50'}
          gridValueMax={props.gridValueMax}
          gridGwpFactors={props.gridGwpFactors}
          activeGridDimensions={props.activeGridDimensions}
        />
      )}
    </>
  );
};

/**
 * Renders the floating map overlays: the interactive monthly/hourly playback
 * scrubber, the country/FIR boundary key, loading/no-data/error banners, and
 * the vertical colorbar legend.
 */
export const MapControlsOverlay = React.memo(MapControlsOverlayInner);
