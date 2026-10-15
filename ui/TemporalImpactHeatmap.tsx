/**
 * @fileoverview Interactive 24x12 hour-by-month heatmap panel visualizing
 * contrail warming impact, airspace flight distance, and the contrail avoidance
 * opportunity index.
 * @jsx React.createElement
 * @jsxFrag React.Fragment
 */

import * as d3 from 'd3';
import {Box, CircularProgress, Typography} from 'material-ui';
import * as React from 'react';
import {
  getFirRecordsForCountry,
  getIso2FromCountry,
  requireYear,
  subscribeToCountryCache,
} from './CountryMetadata';
import {
  formatTonnesCo2e,
  MONTH_SHORT_NAMES,
  parseNonNegativeInteger,
} from './metric_formatters';
import {
  buildSigmoidTrackGradient,
  CELL_STROKE_COLOR,
  CELL_STROKE_WIDTH,
  clampSigmoidMidpoint,
  CONGESTION_DIAGRAM_LABELS,
  CONGESTION_SIGMOID_BAND,
  DEFAULT_CONGESTION_MIDPOINT,
  DEFAULT_IMPACT_PERCENTILE_MIDPOINT,
  formatBadgeFlightText,
  getCongestionColor,
  getImpactColor,
  getOpportunityScoreColor,
  HOVER_STROKE_COLOR,
  HOVER_STROKE_WIDTH,
  IMPACT_SIGMOID_BAND,
  OR_RD_PALETTE,
  RD_YL_GN_PALETTE,
  SigmoidAxisDirection,
  THRESHOLD_OVERLAY_CLASS,
  THRESHOLD_STROKE_COLOR,
  THRESHOLD_STROKE_WIDTH,
} from './opportunity_scale';
import {FilterState} from './RegionalFilterDrawer';
import {
  buildHeatmapCells,
  buildTemporalApiUrl,
  buildTemporalCsvContent,
  buildTemporalCsvFilename,
  buildThresholdOutlinePath,
  cacheTemporalResponse,
  CellHighlightRule,
  computeHasTemporalData,
  computeHighlightBoundaryEdges,
  computeOpportunityData,
  formatHourOfDay,
  formatMidpointPercent,
  formatTemporalNoDataText,
  getDaysInMonth,
  getHeatmapCellClassName,
  getOpportunityFootnoteText,
  getTemporalCacheEntry,
  HEATMAP_GRID_HEIGHT,
  HEATMAP_GRID_WIDTH,
  HEATMAP_MARGIN,
  HEATMAP_TOTAL_WIDTH,
  HeatmapCell,
  HoveredCellInfo,
  normalizeTemporalData,
  resolveDisplayTitle,
  resolveHeatmapCellFill,
  resolveHourLabels,
  resolveTargetFir,
  resolveTemporalCsvFilenameOptions,
  resolveTemporalEntityLabel,
  resolveTemporalImpactMatrix,
  TargetFir,
  TemporalDataResponse,
} from './temporal_heatmap_model';

/** Props for the `TemporalImpactHeatmap` component. */
export interface TemporalImpactHeatmapProps {
  readonly filters: FilterState;
  readonly selectedFirId?: string | null;
  readonly embedded?: boolean;
  readonly hideTitleFirName?: boolean;
}

/** Browser `Window` augmented with an optional global `d3` namespace. */
interface D3Window extends Window {
  readonly d3?: typeof d3;
}

const SLIDER_INSET_LEFT = `${
  (HEATMAP_MARGIN.left / HEATMAP_TOTAL_WIDTH) * 100
}%`;
const SLIDER_TRACK_WIDTH = `${
  (HEATMAP_GRID_WIDTH / HEATMAP_TOTAL_WIDTH) * 100
}%`;

/** Props for `SigmoidMidpointSlider`. */
interface SigmoidMidpointSliderProps {
  readonly label: string;
  readonly ariaLabel: string;
  readonly midpoint: number;
  readonly direction: SigmoidAxisDirection;
  /**
   * Transition half-width of this axis's sigmoid; see `IMPACT_SIGMOID_BAND`.
   */
  readonly band: number;
  readonly onChange: (next: number) => void;
}

/**
 * Midpoint control for one of the two input sigmoids.
 *
 * The track is painted with the opportunity colour scale evaluated *through*
 * the current sigmoid, so the red-to-green transition slides with the midpoint
 * and shows the transfer function being edited. The two axes have different
 * transition widths, so the impact track's ramp is visibly wider than the
 * congestion track's. The native range input is held
 * at zero opacity over the track and a plain div is drawn as the thumb: the
 * dashboard has no stylesheet (all styling is inline style objects), and the
 * browser-default thumb can only be restyled through `::-webkit-slider-thumb`
 * and friends.
 */
const SigmoidMidpointSlider = React.memo(
  ({
    label,
    ariaLabel,
    midpoint,
    direction,
    band,
    onChange,
  }: SigmoidMidpointSliderProps) => {
    const clamped = clampSigmoidMidpoint(midpoint);
    const gradient = React.useMemo(
      () => buildSigmoidTrackGradient(clamped, direction, band),
      [clamped, direction, band],
    );

    return (
      <Box
        style={{
          marginLeft: SLIDER_INSET_LEFT,
          width: SLIDER_TRACK_WIDTH,
          marginTop: '2px',
        }}>
        <Box
          display="flex"
          justifyContent="space-between"
          alignItems="baseline">
          <Typography
            style={{fontSize: '11px', fontWeight: 500, color: '#5f6368'}}>
            {label}
          </Typography>
          <Typography
            style={{
              fontSize: '11.5px',
              fontWeight: 600,
              color: '#202124',
              fontFamily: 'monospace',
            }}>
            {formatMidpointPercent(clamped)}
          </Typography>
        </Box>
        <div style={{position: 'relative', height: '20px'}}>
          <div
            style={{
              position: 'absolute',
              top: '6px',
              left: 0,
              right: 0,
              height: '8px',
              borderRadius: '4px',
              border: '1px solid #dadce0',
              boxSizing: 'border-box',
              background: gradient,
            }}
          />
          <div
            style={{
              position: 'absolute',
              top: '1px',
              // The native input insets its (hidden) thumb by half a thumb
              // width at each end, so the value-to-x mapping is over
              // `100% - 12px`, not over the full track. Mirror that or the
              // drawn thumb overhangs the track at 0% and 100% and drifts
              // from the pointer.
              left: `calc(6px + ${clamped} * (100% - 12px))`,
              marginLeft: '-6px',
              width: '12px',
              height: '18px',
              borderRadius: '3px',
              backgroundColor: '#ffffff',
              border: '2px solid #202124',
              boxSizing: 'border-box',
              pointerEvents: 'none',
            }}
          />
          <input
            type="range"
            aria-label={ariaLabel}
            min={0}
            max={1}
            step={0.01}
            value={clamped}
            onChange={(event) => {
              onChange(clampSigmoidMidpoint(Number(event.target.value)));
            }}
            style={{
              position: 'absolute',
              top: 0,
              left: 0,
              width: '100%',
              height: '20px',
              margin: 0,
              opacity: 0,
              cursor: 'pointer',
            }}
          />
        </div>
      </Box>
    );
  },
);

/** Resolves the D3 library from module import or global window fallback. */
function getD3Lib(): typeof d3 | undefined {
  return (
    d3 || (typeof window !== 'undefined' ? (window as D3Window).d3 : undefined)
  );
}

/** Props for rendering a single declarative SVG heatmap column. */
interface HeatmapSvgGridProps {
  readonly temporalData: TemporalDataResponse | null;
  readonly matrix: ReadonlyArray<readonly number[]> | undefined;
  readonly activeImpactMatrix?: ReadonlyArray<readonly number[]>;
  readonly daysInMonths: readonly number[];
  readonly monthLabels: readonly string[];
  readonly hourLabels: readonly string[];
  readonly title: string;
  readonly unitLabel: string;
  readonly gradientId: string;
  readonly colorPalette: readonly string[];
  readonly mode: 'opportunity' | 'impact' | 'count';
  readonly scoreMatrix?: ReadonlyArray<readonly number[]>;
  readonly congestionMatrix?: ReadonlyArray<readonly number[]>;
  readonly normImpactMatrix?: ReadonlyArray<readonly number[]>;
  readonly rawCongestionMatrix?: ReadonlyArray<readonly number[]>;
  readonly impactPercentileMatrix?: ReadonlyArray<readonly number[]>;
  /** When set, cells satisfying the rule get a thick outline. */
  readonly highlightRule?: CellHighlightRule;
  readonly hoveredCoord: {
    readonly hourIdx: number;
    readonly monthIdx: number;
  } | null;
  readonly onCellHover: (cell: HeatmapCell) => void;
  readonly onCellLeave: () => void;
}

/** Builds the cell color scale function for the active heatmap mode. */
function buildHeatmapColorScale(
  d3Lib: typeof d3,
  mode: 'opportunity' | 'impact' | 'count',
  maxVal: number,
  colorPalette: readonly string[],
): (val: number) => string {
  if (mode === 'opportunity') {
    const rdYlGnInterpolator = d3Lib.interpolateRdYlGn;
    if (rdYlGnInterpolator) {
      const seq = d3Lib.scaleSequential(rdYlGnInterpolator).domain([0, 1.0]);
      return (val: number) => seq(val);
    }
    const quantize = d3Lib
      .scaleQuantize<string>()
      .domain([0, 1.0])
      .range(colorPalette);
    return (val: number) => quantize(val);
  }
  const interpolator = d3Lib.interpolateOrRd || d3Lib.interpolateOranges;
  if (interpolator) {
    const seq = d3Lib.scaleSequential(interpolator).domain([0, maxVal]);
    return (val: number) => seq(val);
  }
  const quantize = d3Lib
    .scaleQuantize<string>()
    .domain([0, maxVal])
    .range(colorPalette);
  return (val: number) => quantize(val);
}

/** Selects the D3 tick number formatter for a heatmap colorbar legend. */
function resolveLegendTickFormatter(
  d3Lib: typeof d3,
  mode: 'opportunity' | 'impact' | 'count',
  maxVal: number,
): (val: number) => string {
  if (mode === 'opportunity') return d3Lib.format('.1f');
  if (maxVal >= 10000) return d3Lib.format('.1s');
  if (maxVal < 1) return d3Lib.format('.2f');
  if (maxVal < 10) return d3Lib.format('.1f');
  return d3Lib.format('.0f');
}

interface HeatmapCellRectsProps {
  readonly cells: readonly HeatmapCell[];
  readonly mode: 'opportunity' | 'impact' | 'count';
  readonly colorScale: (val: number) => string;
  readonly xScale: (monthStr: string) => number | undefined;
  readonly yScale: (hourStr: string) => number | undefined;
  readonly cellWidth: number;
  readonly cellHeight: number;
  readonly onCellHover: (cell: HeatmapCell) => void;
  readonly onCellLeave: () => void;
}

/** Renders the 288 interactive `<rect>` cells of a single heatmap grid. */
const HeatmapCellRects: React.FC<HeatmapCellRectsProps> = ({
  cells,
  mode,
  colorScale,
  xScale,
  yScale,
  cellWidth,
  cellHeight,
  onCellHover,
  onCellLeave,
}) => (
  <>
    {cells.map((cell) => {
      const fill = resolveHeatmapCellFill(cell, mode, colorScale);
      return (
        <rect
          key={`cell-h-${cell.hourIdx}-m-${cell.monthIdx}`}
          className={getHeatmapCellClassName(cell.hourIdx, cell.monthIdx)}
          data-cell-coord={`cell-h-${cell.hourIdx}-m-${cell.monthIdx}`}
          x={xScale(cell.monthStr) ?? 0}
          y={yScale(cell.hourStr) ?? 0}
          width={cellWidth}
          height={cellHeight}
          fill={fill}
          stroke={CELL_STROKE_COLOR}
          strokeWidth={CELL_STROKE_WIDTH}
          style={{cursor: 'pointer'}}
          onMouseEnter={() => {
            onCellHover(cell);
          }}
          onMouseLeave={onCellLeave}
        />
      );
    })}
  </>
);

interface HeatmapXAxisAndTitleProps {
  readonly monthLabels: readonly string[];
  readonly xScale: (monthStr: string) => number | undefined;
  readonly cellWidth: number;
  readonly title: string;
}

/** Renders the month X-axis ticks/label and grid title. */
const HeatmapXAxisAndTitle = React.memo(
  ({monthLabels, xScale, cellWidth, title}: HeatmapXAxisAndTitleProps) => (
    <>
      <g transform={`translate(0, ${HEATMAP_GRID_HEIGHT})`}>
        {monthLabels.map((monthStr) => {
          const tickX = (xScale(monthStr) ?? 0) + cellWidth / 2;
          return (
            <g key={monthStr} transform={`translate(${tickX}, 0)`}>
              <line y2={3} stroke="currentColor" />
              <text
                transform="rotate(90)"
                x={9}
                y={0}
                dy=".35em"
                style={{
                  textAnchor: 'start',
                  fontSize: '13.5px',
                  fontWeight: 500,
                  fill: '#5f6368',
                }}>
                {monthStr}
              </text>
            </g>
          );
        })}
      </g>
      <text
        x={HEATMAP_GRID_WIDTH / 2}
        y={HEATMAP_GRID_HEIGHT + 58}
        style={{
          textAnchor: 'middle',
          fontSize: '14px',
          fontWeight: 600,
          fill: '#5f6368',
        }}>
        Month
      </text>
      <text
        x={HEATMAP_GRID_WIDTH / 2}
        y={-16}
        style={{
          textAnchor: 'middle',
          fontSize: '18px',
          fontWeight: 600,
          fill: '#202124',
        }}>
        {title}
      </text>
    </>
  ),
);

interface HeatmapYAxisProps {
  readonly hourLabels: readonly string[];
  readonly yScale: (hourStr: string) => number | undefined;
  readonly cellHeight: number;
}

/** Renders the 24-hour Y-axis ticks and vertical axis label. */
const HeatmapYAxis = React.memo(
  ({hourLabels, yScale, cellHeight}: HeatmapYAxisProps) => (
    <>
      <g>
        {hourLabels.map((hourStr) => {
          const tickY = (yScale(hourStr) ?? 0) + cellHeight / 2;
          const hourNum = parseNonNegativeInteger(hourStr);
          const tickLabel =
            hourNum !== null && hourNum % 2 === 0 ? hourStr : '';
          return (
            <g key={hourStr} transform={`translate(0, ${tickY})`}>
              <line x2={-3} stroke="currentColor" />
              {tickLabel !== '' && (
                <text
                  x={-6}
                  dy="0.32em"
                  style={{
                    textAnchor: 'end',
                    fontSize: '13.5px',
                    fontFamily: 'monospace',
                    fill: '#5f6368',
                  }}>
                  {tickLabel}
                </text>
              )}
            </g>
          );
        })}
      </g>
      <text
        transform="rotate(-90)"
        y={-42}
        x={-HEATMAP_GRID_HEIGHT / 2}
        style={{
          textAnchor: 'middle',
          fontSize: '14px',
          fontWeight: 600,
          fill: '#5f6368',
        }}>
        Local Hour of Day
      </text>
    </>
  ),
);

interface HeatmapColorbarLegendProps {
  readonly gradientId: string;
  readonly unitLabel: string;
  readonly legendTicks: readonly number[];
  readonly legendScale: (tickVal: number) => number;
  readonly formatFn: (tickVal: number) => string;
}

/** Renders the vertical SVG colorbar `<rect>`, tick marks, and unit title. */
const HeatmapColorbarLegend = React.memo(
  ({
    gradientId,
    unitLabel,
    legendTicks,
    legendScale,
    formatFn,
  }: HeatmapColorbarLegendProps) => {
    const legendX = HEATMAP_GRID_WIDTH + 12;
    const legendWidth = 10;
    const legendHeight = HEATMAP_GRID_HEIGHT;
    return (
      <>
        <rect
          x={legendX}
          y={0}
          width={legendWidth}
          height={legendHeight}
          rx={2}
          style={{
            fill: `url(#${gradientId})`,
            stroke: '#dadce0',
            strokeWidth: '1px',
          }}
        />
        <g transform={`translate(${legendX + legendWidth}, 0)`}>
          {legendTicks.map((tickVal) => (
            <g
              key={tickVal}
              transform={`translate(0, ${legendScale(tickVal)})`}>
              <line x2={6} stroke="currentColor" />
              <text
                x={9}
                dy="0.32em"
                style={{
                  textAnchor: 'start',
                  fontSize: '10.5px',
                  fontFamily: 'monospace',
                  fill: '#5f6368',
                }}>
                {formatFn(tickVal)}
              </text>
            </g>
          ))}
        </g>
        <text
          transform="rotate(-90)"
          y={legendX + legendWidth + 46}
          x={-legendHeight / 2}
          style={{
            textAnchor: 'middle',
            fontSize: '14px',
            fontWeight: 600,
            fill: '#5f6368',
          }}>
          {unitLabel}
        </text>
      </>
    );
  },
);

interface HeatmapGridScaleData {
  readonly xScale: (monthStr: string) => number | undefined;
  readonly yScale: (hourStr: string) => number | undefined;
  readonly cellWidth: number;
  readonly cellHeight: number;
}

/** Computes band scales and cell dimensions for the 24x12 heatmap grid. */
function buildHeatmapBandScales(
  d3Lib: typeof d3 | undefined,
  monthLabels: readonly string[],
  hourLabels: readonly string[],
): HeatmapGridScaleData | null {
  if (!d3Lib) return null;
  const xBand = d3Lib
    .scaleBand()
    .domain(monthLabels)
    .range([0, HEATMAP_GRID_WIDTH])
    .padding(0.01);
  const yBand = d3Lib
    .scaleBand()
    .domain(hourLabels)
    .range([0, HEATMAP_GRID_HEIGHT])
    .padding(0.01);
  return {
    xScale: (monthStr: string) => xBand(monthStr),
    yScale: (hourStr: string) => yBand(hourStr),
    cellWidth: xBand.bandwidth(),
    cellHeight: yBand.bandwidth(),
  };
}

interface HeatmapComputedGridModel {
  readonly cells: readonly HeatmapCell[];
  readonly colorScale: (val: number) => string;
  readonly maxVal: number;
}

/** Computes normalized cells and color scale for a grid. */
function buildHeatmapComputedGridModel(
  d3Lib: typeof d3 | undefined,
  props: HeatmapSvgGridProps,
  resolvedImpactMatrix: ReadonlyArray<readonly number[]> | undefined,
): HeatmapComputedGridModel | null {
  const {
    temporalData,
    matrix,
    daysInMonths,
    monthLabels,
    hourLabels,
    colorPalette,
    mode,
    scoreMatrix,
    congestionMatrix,
    normImpactMatrix,
    rawCongestionMatrix,
    impactPercentileMatrix,
  } = props;
  if (!d3Lib || !temporalData || !matrix || !resolvedImpactMatrix) return null;
  const {cells, maxVal} = buildHeatmapCells({
    matrix,
    activeImpactMatrix: resolvedImpactMatrix,
    temporalData,
    daysInMonths,
    hourLabels,
    monthLabels,
    mode,
    scoreMatrix,
    congestionMatrix,
    normImpactMatrix,
    rawCongestionMatrix,
    impactPercentileMatrix,
  });
  return {
    cells,
    colorScale: buildHeatmapColorScale(d3Lib, mode, maxVal, colorPalette),
    maxVal,
  };
}

/** Renders the threshold-outline `<path>` and hovered-cell `<rect>` overlay. */
const HeatmapGridOverlays: React.FC<{
  readonly outlinePath: string;
  readonly activeHoveredCell: HeatmapCell | undefined;
  readonly scales: HeatmapGridScaleData;
}> = ({outlinePath, activeHoveredCell, scales}) => (
  <>
    {outlinePath !== '' && (
      <g className={THRESHOLD_OVERLAY_CLASS} style={{pointerEvents: 'none'}}>
        <path
          d={outlinePath}
          fill="none"
          stroke={THRESHOLD_STROKE_COLOR}
          strokeWidth={THRESHOLD_STROKE_WIDTH}
          strokeLinecap="square"
        />
      </g>
    )}
    {activeHoveredCell && (
      <rect
        x={scales.xScale(activeHoveredCell.monthStr) ?? 0}
        y={scales.yScale(activeHoveredCell.hourStr) ?? 0}
        width={scales.cellWidth}
        height={scales.cellHeight}
        fill="none"
        stroke={HOVER_STROKE_COLOR}
        strokeWidth={HOVER_STROKE_WIDTH}
        style={{pointerEvents: 'none'}}
      />
    )}
  </>
);

/**
 * Declarative SVG heatmap column that uses D3 strictly for scale, color, and
 * tick math while delegating all DOM construction and updates to React JSX.
 */
const HeatmapSvgGrid = React.memo((props: HeatmapSvgGridProps) => {
  const {
    temporalData,
    matrix,
    activeImpactMatrix,
    daysInMonths,
    monthLabels,
    hourLabels,
    title,
    unitLabel,
    gradientId,
    colorPalette,
    mode,
    scoreMatrix,
    congestionMatrix,
    normImpactMatrix,
    rawCongestionMatrix,
    impactPercentileMatrix,
    highlightRule,
    hoveredCoord,
    onCellHover,
    onCellLeave,
  } = props;
  const d3Lib = getD3Lib();
  const resolvedImpactMatrix = activeImpactMatrix ?? matrix;
  const scales = React.useMemo(
    () => buildHeatmapBandScales(d3Lib, monthLabels, hourLabels),
    [d3Lib, monthLabels, hourLabels],
  );
  const gridModel = React.useMemo(
    () => buildHeatmapComputedGridModel(d3Lib, props, resolvedImpactMatrix),
    [
      d3Lib,
      temporalData,
      matrix,
      resolvedImpactMatrix,
      daysInMonths,
      hourLabels,
      monthLabels,
      mode,
      scoreMatrix,
      congestionMatrix,
      normImpactMatrix,
      rawCongestionMatrix,
      impactPercentileMatrix,
      colorPalette,
    ],
  );
  const legendProps = React.useMemo(() => {
    if (!d3Lib || !gridModel) return null;
    const legendDomainMax = mode === 'opportunity' ? 1.0 : gridModel.maxVal;
    const linearLegend = d3Lib
      .scaleLinear()
      .domain([0, legendDomainMax])
      .range([HEATMAP_GRID_HEIGHT, 0]);
    return {
      legendScale: (tickVal: number) => linearLegend(tickVal),
      formatFn: resolveLegendTickFormatter(d3Lib, mode, gridModel.maxVal),
      legendTicks: linearLegend.ticks(5),
    };
  }, [d3Lib, mode, gridModel?.maxVal]);
  const outlinePath = React.useMemo(() => {
    if (!gridModel || !scales) return '';
    const edges = computeHighlightBoundaryEdges(gridModel.cells, highlightRule);
    return edges.length > 0
      ? buildThresholdOutlinePath(
          edges,
          scales.xScale,
          scales.yScale,
          scales.cellWidth,
          scales.cellHeight,
        )
      : '';
  }, [gridModel, scales, highlightRule]);
  const activeHoveredCell = React.useMemo(
    () =>
      hoveredCoord &&
      gridModel &&
      hoveredCoord.hourIdx >= 0 &&
      hoveredCoord.hourIdx < 24 &&
      hoveredCoord.monthIdx >= 0 &&
      hoveredCoord.monthIdx < 12
        ? gridModel.cells[hoveredCoord.hourIdx * 12 + hoveredCoord.monthIdx]
        : undefined,
    [gridModel, hoveredCoord],
  );
  const cellRectsNode = React.useMemo(
    () =>
      scales && gridModel ? (
        <HeatmapCellRects
          cells={gridModel.cells}
          mode={mode}
          colorScale={gridModel.colorScale}
          xScale={scales.xScale}
          yScale={scales.yScale}
          cellWidth={scales.cellWidth}
          cellHeight={scales.cellHeight}
          onCellHover={onCellHover}
          onCellLeave={onCellLeave}
        />
      ) : null,
    [gridModel, mode, scales, onCellHover, onCellLeave],
  );

  if (!scales || !gridModel || !legendProps) {
    return <svg style={{width: '100%', height: 'auto', display: 'block'}} />;
  }
  const totalHeight =
    HEATMAP_GRID_HEIGHT + HEATMAP_MARGIN.top + HEATMAP_MARGIN.bottom;
  return (
    <svg
      viewBox={`0 0 ${HEATMAP_TOTAL_WIDTH} ${totalHeight}`}
      style={{width: '100%', height: 'auto', display: 'block'}}>
      <defs>
        <linearGradient id={gradientId} x1="0%" y1="100%" x2="0%" y2="0%">
          {colorPalette.map((col, idx) => (
            <stop
              key={col}
              offset={`${(idx / (colorPalette.length - 1)) * 100}%`}
              stopColor={col}
            />
          ))}
        </linearGradient>
      </defs>
      <g
        className="heatmap-grid-group"
        transform={`translate(${HEATMAP_MARGIN.left}, ${HEATMAP_MARGIN.top})`}>
        {cellRectsNode}
        <HeatmapGridOverlays
          outlinePath={outlinePath}
          activeHoveredCell={activeHoveredCell}
          scales={scales}
        />
        <HeatmapXAxisAndTitle
          monthLabels={monthLabels}
          xScale={scales.xScale}
          cellWidth={scales.cellWidth}
          title={title}
        />
        <HeatmapYAxis
          hourLabels={hourLabels}
          yScale={scales.yScale}
          cellHeight={scales.cellHeight}
        />
        <HeatmapColorbarLegend
          gradientId={gradientId}
          unitLabel={unitLabel}
          legendTicks={legendProps.legendTicks}
          legendScale={legendProps.legendScale}
          formatFn={legendProps.formatFn}
        />
      </g>
    </svg>
  );
});

const TemporalImpactHeatmapInner: React.FC<TemporalImpactHeatmapProps> = ({
  filters,
  selectedFirId,
  embedded = false,
  hideTitleFirName = false,
}) => {
  const [temporalData, setTemporalData] =
    React.useState<TemporalDataResponse | null>(null);
  const [loading, setLoading] = React.useState<boolean>(true);
  const [hoveredCoord, setHoveredCoord] = React.useState<{
    readonly hourIdx: number;
    readonly monthIdx: number;
  } | null>(null);

  // Sigmoid inflection points, driven by the sliders under the contrail impact
  // and airspace traffic grids. Purely client-side: changing them re-derives
  // the opportunity index from the already-fetched matrices with no refetch.
  const [congestionMidpoint, setCongestionMidpoint] = React.useState<number>(
    DEFAULT_CONGESTION_MIDPOINT,
  );
  const [impactMidpoint, setImpactMidpoint] = React.useState<number>(
    DEFAULT_IMPACT_PERCENTILE_MIDPOINT,
  );

  const hoverClearRafRef = React.useRef<number | null>(null);

  const activeGwp = filters.gwp || 'GWP50';

  const [cacheVersion, setCacheVersion] = React.useState(0);
  React.useEffect(() => {
    return subscribeToCountryCache(() => {
      setCacheVersion((v) => v + 1);
    });
  }, []);

  // Determine the target FIR: matching selectedFirId, or defaulting to #1 FIR
  // for the selected country
  const targetFir = React.useMemo<TargetFir | null>(() => {
    const firRecords = getFirRecordsForCountry(
      filters.country,
      filters.year,
      activeGwp,
    );
    return resolveTargetFir(selectedFirId, firRecords);
  }, [filters.country, filters.year, activeGwp, selectedFirId, cacheVersion]);

  // Fetch real temporal data from backend API
  React.useEffect(() => {
    // `/temporal` serves one country or FIR. Until either is known (the
    // country list is still loading), keep the loading state.
    if (!filters.country && !targetFir) return;
    const countryIso = filters.country
      ? getIso2FromCountry(filters.country, filters.year)
      : '';
    if (!countryIso && !targetFir?.firId) {
      setHoveredCoord(null);
      setTemporalData(null);
      setLoading(false);
      return;
    }
    const url = buildTemporalApiUrl({
      year: filters.year,
      country: countryIso || filters.country,
      targetFir,
    });

    const cached = getTemporalCacheEntry(url);
    if (cached) {
      setHoveredCoord(null);
      setTemporalData(cached);
      setLoading(false);
      return;
    }

    const abortController = new AbortController();
    setHoveredCoord(null);
    setTemporalData(null);
    setLoading(true);

    let usedFallbackUrl: string | null = null;
    const fetchFallback = (): Promise<unknown> => {
      if (!targetFir || !countryIso) return Promise.resolve(null);
      const fallbackUrl = buildTemporalApiUrl({
        year: filters.year,
        country: countryIso || filters.country,
        targetFir: null,
      });
      usedFallbackUrl = fallbackUrl;
      const fallbackCached = getTemporalCacheEntry(fallbackUrl);
      if (fallbackCached) {
        return Promise.resolve(fallbackCached);
      }
      return fetch(fallbackUrl, {signal: abortController.signal}).then(
        (fbRes) => (fbRes.ok ? fbRes.json() : null),
      );
    };

    void fetch(url, {signal: abortController.signal})
      .then((res) => {
        if (!res.ok) {
          return fetchFallback();
        }
        return res.json();
      })
      .catch((err: unknown) => {
        if (err instanceof Error && err.name === 'AbortError') {
          throw err;
        }
        return fetchFallback();
      })
      .then((raw: unknown) => {
        if (abortController.signal.aborted) {
          return;
        }
        const normalized = normalizeTemporalData(raw);
        if (normalized) {
          cacheTemporalResponse(url, usedFallbackUrl, normalized);
          setTemporalData(normalized);
        } else {
          setTemporalData(null);
        }
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (
          abortController.signal.aborted ||
          (err instanceof Error && err.name === 'AbortError')
        ) {
          return;
        }
        console.warn('Error fetching temporal metrics:', err);
        setTemporalData(null);
        setLoading(false);
      });

    return () => {
      abortController.abort();
    };
  }, [filters.year, filters.country, targetFir]);

  const monthLabels = React.useMemo(() => {
    if (temporalData?.months && temporalData.months.length === 12) {
      return temporalData.months.map((mStr) => {
        const mNum = parseNonNegativeInteger(mStr.slice(-2));
        const monthName =
          mNum === null ? undefined : MONTH_SHORT_NAMES[mNum - 1];
        return monthName ?? mStr;
      });
    }
    return MONTH_SHORT_NAMES;
  }, [temporalData]);

  const hourLabels = React.useMemo(
    () => resolveHourLabels(temporalData),
    [temporalData],
  );

  // Compute Contrail Avoidance Opportunity Index matrix: (1 - Congestion) *
  // Normalized Impact. Because GWP20/50/100 matrices are positive scalar
  // multiples of energy forcing (EF), the impact percentile ranking - and
  // therefore the index - is invariant to GWP choice.
  const baselineOpportunityData = React.useMemo(
    () => computeOpportunityData(temporalData, 'GWP50'),
    [temporalData],
  );
  const opportunityData = React.useMemo(
    () =>
      congestionMidpoint === DEFAULT_CONGESTION_MIDPOINT &&
      impactMidpoint === DEFAULT_IMPACT_PERCENTILE_MIDPOINT
        ? baselineOpportunityData
        : computeOpportunityData(temporalData, 'GWP50', {
            congestionMidpoint,
            impactMidpoint,
            rawCongestionMatrix: baselineOpportunityData.rawCongestionMatrix,
            impactPercentileMatrix:
              baselineOpportunityData.impactPercentileMatrix,
          }),
    [temporalData, congestionMidpoint, impactMidpoint, baselineOpportunityData],
  );

  const activeImpactMatrix = React.useMemo(
    () =>
      temporalData
        ? resolveTemporalImpactMatrix(temporalData, activeGwp)
        : undefined,
    [temporalData, activeGwp],
  );

  const daysInMonths = React.useMemo(() => {
    if (!temporalData) return [];
    const yearNum = requireYear(temporalData.year, 'TemporalImpactHeatmap');
    return Array.from({length: 12}, (_, m) => getDaysInMonth(yearNum, m));
  }, [temporalData]);

  const hoveredCell = React.useMemo<HoveredCellInfo | null>(() => {
    if (
      !hoveredCoord ||
      !temporalData ||
      hoveredCoord.hourIdx < 0 ||
      hoveredCoord.hourIdx >= 24 ||
      hoveredCoord.monthIdx < 0 ||
      hoveredCoord.monthIdx >= 12
    ) {
      return null;
    }
    const {hourIdx, monthIdx} = hoveredCoord;
    const days = daysInMonths[monthIdx] || 30;
    const rawCo2e = activeImpactMatrix?.[hourIdx]?.[monthIdx] || 0;
    const safeCo2e = Number.isFinite(rawCo2e) ? rawCo2e : 0;
    const rawDist =
      temporalData.totalFlightDistanceKm[hourIdx]?.[monthIdx] || 0;
    const safeDist = Number.isFinite(rawDist) ? rawDist : 0;
    const hourStr = hourLabels[hourIdx] || String(hourIdx).padStart(2, '0');
    const monthStr = monthLabels[monthIdx] || `M${monthIdx + 1}`;
    return {
      month: monthStr,
      hour: formatHourOfDay(hourStr),
      hourIdx,
      monthIdx,
      dailyCo2e: safeCo2e / days,
      dailyFlightDistanceKm: safeDist / days,
      score: opportunityData.matrix[hourIdx]?.[monthIdx] ?? 0,
      congestion: opportunityData.congestionMatrix[hourIdx]?.[monthIdx] ?? 0,
      rawCongestion: opportunityData.rawCongestionMatrix[hourIdx]?.[monthIdx],
      normImpact: opportunityData.normImpactMatrix[hourIdx]?.[monthIdx] ?? 0,
    };
  }, [
    hoveredCoord,
    temporalData,
    activeImpactMatrix,
    opportunityData,
    daysInMonths,
    monthLabels,
    hourLabels,
  ]);

  React.useEffect(() => {
    return () => {
      if (
        hoverClearRafRef.current != null &&
        typeof cancelAnimationFrame !== 'undefined'
      ) {
        cancelAnimationFrame(hoverClearRafRef.current);
        hoverClearRafRef.current = null;
      }
    };
  }, []);

  const handleCellHover = React.useCallback((d: HeatmapCell) => {
    if (
      hoverClearRafRef.current != null &&
      typeof cancelAnimationFrame !== 'undefined'
    ) {
      cancelAnimationFrame(hoverClearRafRef.current);
      hoverClearRafRef.current = null;
    }
    setHoveredCoord((prev) =>
      prev && prev.hourIdx === d.hourIdx && prev.monthIdx === d.monthIdx
        ? prev
        : {hourIdx: d.hourIdx, monthIdx: d.monthIdx},
    );
  }, []);

  const handleCellLeave = React.useCallback(() => {
    const clearHover = () => {
      setHoveredCoord(null);
    };
    if (typeof requestAnimationFrame !== 'undefined') {
      if (hoverClearRafRef.current != null) {
        cancelAnimationFrame(hoverClearRafRef.current);
      }
      hoverClearRafRef.current = requestAnimationFrame(clearHover);
    } else {
      clearHover();
    }
  }, []);

  const impactHighlightRule = React.useMemo<CellHighlightRule>(
    () => ({
      source: 'impactPercentile',
      comparison: 'above',
      threshold: impactMidpoint,
    }),
    [impactMidpoint],
  );
  const countHighlightRule = React.useMemo<CellHighlightRule>(
    () => ({
      source: 'rawCongestion',
      comparison: 'below',
      threshold: congestionMidpoint,
    }),
    [congestionMidpoint],
  );

  const displayTitle = React.useMemo(
    () =>
      resolveDisplayTitle({
        temporalDataName: temporalData?.name,
        isFirData: temporalData?.isFir,
        targetFir,
        country: filters.country,
      }),
    [temporalData?.name, temporalData?.isFir, targetFir, filters.country],
  );
  const firName = React.useMemo(
    () =>
      resolveTemporalEntityLabel({
        temporalData,
        targetFir,
        selectedFirId,
        country: filters.country,
      }),
    [temporalData, targetFir, selectedFirId, filters.country],
  );

  const hasData = React.useMemo(
    () => computeHasTemporalData(temporalData, activeGwp),
    [temporalData, activeGwp],
  );

  const opportunityFootnoteText = React.useMemo(
    () => getOpportunityFootnoteText(congestionMidpoint, impactMidpoint),
    [congestionMidpoint, impactMidpoint],
  );

  const handleExportCsv = React.useCallback(() => {
    if (!temporalData) return;
    const csvContent = buildTemporalCsvContent({
      temporalData,
      gwp: activeGwp,
      opportunityMatrix: opportunityData.matrix,
    });
    const filename = buildTemporalCsvFilename(
      resolveTemporalCsvFilenameOptions({
        temporalData,
        targetFir,
        selectedFirId,
        fallbackYear: filters.year,
      }),
    );
    if (
      typeof Blob === 'undefined' ||
      typeof URL === 'undefined' ||
      typeof document === 'undefined'
    ) {
      return;
    }
    const blob = new Blob([csvContent], {type: 'text/csv;charset=utf-8;'});
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = filename;
    link.style.display = 'none';
    document.body?.appendChild(link);
    link.click();
    document.body?.removeChild(link);
    URL.revokeObjectURL(objectUrl);
  }, [
    temporalData,
    activeGwp,
    opportunityData.matrix,
    targetFir,
    selectedFirId,
    filters.year,
  ]);

  return (
    <Box
      style={{
        backgroundColor: embedded ? 'transparent' : '#ffffff',
        borderRadius: embedded ? '0' : '12px',
        boxShadow: embedded ? 'none' : '0 1px 3px rgba(0,0,0,0.1)',
        padding: embedded ? '0' : '20px 24px 24px 24px',
        width: '100%',
        boxSizing: 'border-box',
        marginTop: '0px',
        position: 'relative',
      }}>
      {/* Header */}
      <Box
        display="flex"
        justifyContent="space-between"
        alignItems="flex-start"
        flexWrap="wrap"
        style={{marginBottom: '16px', gap: '12px'}}>
        <Box
          display="flex"
          flexDirection="column"
          style={{flex: '1 1 auto', minWidth: '280px'}}>
          <Typography
            style={{
              fontSize: '18px',
              fontWeight: 600,
              color: '#202124',
              letterSpacing: '-0.2px',
            }}>
            {hideTitleFirName
              ? 'Contrail avoidance opportunity'
              : `${displayTitle}: Contrail avoidance opportunity`}
          </Typography>
          <Typography
            style={{fontSize: '13px', color: '#5f6368', marginTop: '3px'}}>
            {'Explore the contrail avoidance opportunities by month and ' +
              `hour for ${firName}. A high opportunity score is assigned ` +
              'for windows above the contrail impact percentile midpoint ' +
              'and below the airspace traffic midpoint set by the sliders ' +
              'below. The score indicates when FIR control resources could ' +
              'be available for high impact contrail avoidance.'}
          </Typography>
        </Box>

        {/* Hover Info Badge */}
        <Box
          style={{
            minHeight: '32px',
            height: 'auto',
            padding: '4px 14px',
            backgroundColor: hoveredCell ? '#f1f3f4' : 'transparent',
            borderRadius: '6px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'flex-end',
            flexWrap: 'wrap',
            gap: '4px 8px',
          }}>
          {hoveredCell ? (
            <span
              style={{fontSize: '12.5px', fontWeight: 500, color: '#202124'}}>
              <strong style={{color: '#5f6368', fontWeight: 400}}>
                {hoveredCell.month} @ {hoveredCell.hour}:
              </strong>{' '}
              <span
                style={{
                  color: getOpportunityScoreColor(hoveredCell.score ?? 0),
                  fontWeight: 600,
                }}>
                Opportunity: {(hoveredCell.score ?? 0).toFixed(2)}
              </span>{' '}
              <span
                style={{color: '#5f6368', fontSize: '11.5px', fontWeight: 400}}>
                (
                <span
                  title="Airspace Traffic Ratio (lower is better for avoidance)"
                  style={{
                    color: getCongestionColor(hoveredCell.congestion ?? 0),
                    fontWeight: 600,
                  }}>
                  Traffic: {(hoveredCell.congestion ?? 0).toFixed(2)}
                </span>
                {', '}
                <span
                  title={
                    'Normalized Contrail Warming Impact ' +
                    '(higher is better for avoidance)'
                  }
                  style={{
                    color: getImpactColor(hoveredCell.normImpact ?? 0),
                    fontWeight: 600,
                  }}>
                  Impact: {(hoveredCell.normImpact ?? 0).toFixed(2)}
                </span>
                )
              </span>
              {' | '}
              {formatTonnesCo2e(
                hoveredCell.dailyCo2e,
                `CO₂e (${activeGwp})/day`,
              )}{' '}
              |{' '}
              {formatBadgeFlightText(
                hoveredCell.dailyFlightDistanceKm,
                hoveredCell.rawCongestion,
              )}
            </span>
          ) : (
            <span
              style={{fontSize: '12px', color: '#80868b', fontStyle: 'italic'}}>
              Hover over any cell to cross-inspect opportunity, impact & traffic
            </span>
          )}
        </Box>
      </Box>

      {/* Loading Overlay */}
      {loading && (
        <Box
          style={{
            position: 'absolute',
            top: '70px',
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: 'rgba(255, 255, 255, 0.85)',
            zIndex: 10,
            display: 'flex',
            justifyContent: 'center',
            alignItems: 'center',
            borderRadius: '0 0 12px 12px',
          }}>
          <CircularProgress
            size={28}
            style={{color: '#1a73e8', marginRight: '12px'}}
          />
          <Typography
            style={{fontSize: '14px', color: '#5f6368', fontWeight: 500}}>
            Loading temporal dataset for {filters.country}...
          </Typography>
        </Box>
      )}

      {/* No Data Overlay for years without exported temporal dataset */}
      {!loading && !hasData && (
        <Box
          style={{
            position: 'absolute',
            top: '70px',
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: 'rgba(255, 255, 255, 0.94)',
            zIndex: 10,
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'center',
            alignItems: 'center',
            borderRadius: '0 0 12px 12px',
          }}>
          <Typography
            style={{
              fontSize: '15px',
              fontWeight: 600,
              color: '#3c4043',
              marginBottom: '4px',
            }}>
            No Temporal Data Available for {filters.year}
          </Typography>
          <Typography style={{fontSize: '13px', color: '#5f6368'}}>
            {formatTemporalNoDataText(filters.year)}
          </Typography>
        </Box>
      )}

      {/* 3-column SVG heatmaps, left to right: Contrail Impact, Airspace
         Traffic, Contrail Avoidance Opportunity */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          width: '100%',
          gap: '12px',
          minHeight: '340px',
          overflowX: 'auto',
          paddingBottom: '6px',
        }}>
        <Box style={{flex: '1 1 300px', minWidth: '270px', maxWidth: '380px'}}>
          <HeatmapSvgGrid
            temporalData={temporalData}
            matrix={activeImpactMatrix}
            daysInMonths={daysInMonths}
            monthLabels={monthLabels}
            hourLabels={hourLabels}
            title="Contrail impact"
            unitLabel={`Impact (tCO₂e ${activeGwp} / day)`}
            gradientId="gradient-impact-orrd"
            colorPalette={OR_RD_PALETTE}
            mode="impact"
            impactPercentileMatrix={opportunityData.impactPercentileMatrix}
            highlightRule={impactHighlightRule}
            hoveredCoord={hoveredCoord}
            onCellHover={handleCellHover}
            onCellLeave={handleCellLeave}
          />
          <SigmoidMidpointSlider
            label="Impact percentile"
            ariaLabel="Contrail impact percentile sigmoid midpoint"
            midpoint={impactMidpoint}
            direction="higher-is-better"
            band={IMPACT_SIGMOID_BAND}
            onChange={setImpactMidpoint}
          />
        </Box>
        <Box style={{flex: '1 1 300px', minWidth: '270px', maxWidth: '380px'}}>
          <HeatmapSvgGrid
            temporalData={temporalData}
            matrix={temporalData?.totalFlightDistanceKm}
            activeImpactMatrix={activeImpactMatrix}
            daysInMonths={daysInMonths}
            monthLabels={monthLabels}
            hourLabels={hourLabels}
            title={CONGESTION_DIAGRAM_LABELS.title}
            unitLabel={CONGESTION_DIAGRAM_LABELS.unitLabel}
            gradientId="gradient-count-orrd"
            colorPalette={OR_RD_PALETTE}
            mode="count"
            rawCongestionMatrix={opportunityData.rawCongestionMatrix}
            highlightRule={countHighlightRule}
            hoveredCoord={hoveredCoord}
            onCellHover={handleCellHover}
            onCellLeave={handleCellLeave}
          />
          <SigmoidMidpointSlider
            label="Traffic"
            ariaLabel="Airspace traffic sigmoid midpoint"
            midpoint={congestionMidpoint}
            direction="lower-is-better"
            band={CONGESTION_SIGMOID_BAND}
            onChange={setCongestionMidpoint}
          />
        </Box>
        <Box style={{flex: '1 1 300px', minWidth: '270px', maxWidth: '380px'}}>
          <HeatmapSvgGrid
            temporalData={temporalData}
            matrix={activeImpactMatrix}
            daysInMonths={daysInMonths}
            monthLabels={monthLabels}
            hourLabels={hourLabels}
            title="Contrail avoidance opportunity"
            unitLabel="Avoidance Opportunity"
            gradientId="gradient-opportunity-rdylgn"
            colorPalette={RD_YL_GN_PALETTE}
            mode="opportunity"
            scoreMatrix={opportunityData.matrix}
            congestionMatrix={opportunityData.congestionMatrix}
            normImpactMatrix={opportunityData.normImpactMatrix}
            rawCongestionMatrix={opportunityData.rawCongestionMatrix}
            impactPercentileMatrix={opportunityData.impactPercentileMatrix}
            hoveredCoord={hoveredCoord}
            onCellHover={handleCellHover}
            onCellLeave={handleCellLeave}
          />
          <Box
            style={{
              marginLeft: SLIDER_INSET_LEFT,
              width: SLIDER_TRACK_WIDTH,
              marginTop: '2px',
              minHeight: '36px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}>
            <button
              type="button"
              onClick={handleExportCsv}
              disabled={loading || !hasData || !temporalData}
              title={
                'Export contrail impact, airspace traffic, and ' +
                'opportunity index as CSV'
              }
              aria-label="Export CSV"
              style={{
                padding: '5px 14px',
                borderRadius: '6px',
                border: '1px solid #dadce0',
                backgroundColor:
                  loading || !hasData || !temporalData ? '#f1f3f4' : '#ffffff',
                color:
                  loading || !hasData || !temporalData ? '#9aa0a6' : '#3c4043',
                fontSize: '12px',
                fontWeight: 500,
                cursor:
                  loading || !hasData || !temporalData
                    ? 'not-allowed'
                    : 'pointer',
                display: 'inline-flex',
                alignItems: 'center',
                gap: '6px',
                transition:
                  'background-color 0.15s ease, border-color 0.15s ease',
              }}
              onMouseEnter={(e) => {
                if (!loading && hasData && temporalData) {
                  e.currentTarget.style.backgroundColor = '#f8f9fa';
                  e.currentTarget.style.borderColor = '#bdc1c6';
                }
              }}
              onMouseLeave={(e) => {
                if (!loading && hasData && temporalData) {
                  e.currentTarget.style.backgroundColor = '#ffffff';
                  e.currentTarget.style.borderColor = '#dadce0';
                }
              }}>
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="currentColor"
                aria-hidden="true">
                <path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z" />
              </svg>
              <span>Export CSV</span>
            </button>
          </Box>
        </Box>
      </div>

      {/* Informational Footnotes */}
      <Box
        style={{
          marginTop: '12px',
          display: 'flex',
          flexDirection: 'column',
          gap: '3px',
        }}>
        <Typography
          style={{fontSize: '11.5px', color: '#5f6368', fontStyle: 'italic'}}>
          {opportunityFootnoteText}
        </Typography>
      </Box>
    </Box>
  );
};

/**
 * Renders side-by-side 2D grid heatmaps, left to right:
 * 1. Contrail impact (OrRd colormap)
 * 2. Airspace traffic (total flight distance, OrRd colormap)
 * 3. Contrail Avoidance Opportunity Index (RdYlGn colormap: green = high, red =
 *    low) showing Local Hour of Day (00:00 to 23:00) vs Month (Jan to Dec) with
 *    cross-matrix hover synchronization.
 */
export const TemporalImpactHeatmap = React.memo(TemporalImpactHeatmapInner);
