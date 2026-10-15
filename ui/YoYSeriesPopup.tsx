/**
 * @fileoverview Multi-year contrail metric time-series modal dialog and SVG
 * axis scale, coordinate mapping, and path construction helpers.
 * @jsx React.createElement
 * @jsxFrag React.Fragment
 */

import {Box, CircularProgress, Typography} from 'material-ui';
import * as React from 'react';
import {
  fetchTimeSeriesFromBackend,
  TimeSeriesData,
  TimeSeriesPoint,
} from './CountryMetadata';
import {
  UNIT_MILLIWATTS_PER_SQ_M,
  UNIT_RATIO_OF_NATIONAL_GHG,
  UNIT_RATIO_OF_WORLD_AVG_GHG_PER_CAPITA,
  unitDisplayLabel,
} from './metric_formatters';
import {ModalShell} from './metrics_panel_widgets';

/** Props for the `YoYSeriesPopup` component. */
export interface YoYSeriesPopupProps {
  readonly isOpen: boolean;
  readonly onClose: () => void;
  readonly metricKey: string;
  readonly metricTitle: string;
  readonly categoryTitle?: string;
  readonly selectedCountry?: string;
  readonly selectedFirId?: string | null;
  readonly year: number;
  readonly gwp?: string;
  readonly anchorRect?: DOMRect | null;
}

/**
 * Computed vertical axis bounds for a multi-year time series chart.
 */
interface SeriesScale {
  readonly minVal: number;
  readonly yUpper: number;
}

/**
 * Returns whether a series point carries a finite numeric value. Missing
 * (`null`), `NaN`, and `±Infinity` values are all treated as "no data" so the
 * axis scale, the SVG path, and the header badge agree on which points exist.
 * @visibleForTesting
 */
export function hasFiniteValue(
  point: TimeSeriesPoint,
): point is TimeSeriesPoint & {readonly value: number} {
  return point.value != null && Number.isFinite(point.value);
}

/**
 * Computes the vertical axis lower bound (`minVal`, anchored at `<= 0` so
 * non-negative series always include the zero baseline) and upper bound
 * (`yUpper`, adding 15% headroom above the range, or `[0, 1]` when all points
 * are `0` or `[0, 115]` when `points` contains no finite values).
 *
 * @visibleForTesting
 */
export function computeSeriesScale(
  points: readonly TimeSeriesPoint[],
): SeriesScale {
  let maxVal = 0;
  let minVal = 0;
  let hasAny = false;
  for (const p of points) {
    if (!hasFiniteValue(p)) continue;
    hasAny = true;
    if (p.value > maxVal) maxVal = p.value;
    if (p.value < minVal) minVal = p.value;
  }
  if (!hasAny) {
    maxVal = 100;
  }
  const rawRange = maxVal - minVal;
  const valRange = rawRange > 0 ? rawRange : 1;
  // 15% headroom
  const yUpper = maxVal === 0 && minVal === 0 ? 1 : maxVal + valRange * 0.15;
  return {minVal, yUpper};
}

/** Layout inputs for mapping a point index to an SVG X coordinate. */
export interface SeriesXOptions {
  readonly idx: number;
  readonly total: number;
  readonly padLeft: number;
  readonly chartW: number;
}

/**
 * Computes the horizontal SVG coordinate for point `idx` out of `total`.
 * @visibleForTesting
 */
export function getSeriesX({
  idx,
  total,
  padLeft,
  chartW,
}: SeriesXOptions): number {
  if (total <= 1) return padLeft + chartW / 2;
  return padLeft + (idx / (total - 1)) * chartW;
}

/**
 * Layout and scale inputs for mapping a metric value to an SVG Y coordinate.
 */
export interface SeriesYOptions {
  readonly val: number | null;
  readonly minVal: number;
  readonly yUpper: number;
  readonly padTop: number;
  readonly chartH: number;
}

/**
 * Computes the vertical SVG coordinate for `val` within `[minVal, yUpper]`.
 * @visibleForTesting
 */
export function getSeriesY({
  val,
  minVal,
  yUpper,
  padTop,
  chartH,
}: SeriesYOptions): number {
  if (val == null || !Number.isFinite(val)) return padTop + chartH;
  const denom = yUpper - minVal;
  const norm = denom > 0 ? (val - minVal) / denom : 0;
  return padTop + chartH - norm * chartH;
}

/**
 * Computes the percentage change from `points[idx - 1]` to `points[idx]`, or
 * returns `null` when the years are non-consecutive or either value is invalid.
 * @visibleForTesting
 */
export function deriveYoYPercent(
  points: readonly TimeSeriesPoint[],
  idx: number,
): number | null {
  if (idx <= 0 || idx >= points.length) return null;
  const prevYear = points[idx - 1].year;
  const curYear = points[idx].year;
  if (
    Number.isFinite(prevYear) &&
    Number.isFinite(curYear) &&
    curYear - prevYear !== 1
  ) {
    return null;
  }
  const prev = points[idx - 1].value;
  const cur = points[idx].value;
  if (
    prev == null ||
    cur == null ||
    prev === 0 ||
    !Number.isFinite(prev) ||
    !Number.isFinite(cur)
  )
    return null;
  return ((cur - prev) / Math.abs(prev)) * 100;
}

/** Screen-space point and validity flag for building SVG series paths. */
export interface SeriesLinePoint {
  readonly x: number;
  readonly y: number;
  readonly hasVal: boolean;
}

/**
 * Builds SVG line (`pathD`) and area (`areaD`) path strings that break cleanly
 * across missing (`hasVal === false`) years.
 * @visibleForTesting
 */
export function buildSeriesSvgPaths(
  linePoints: readonly SeriesLinePoint[],
  baselineY: number,
): {readonly pathD: string; readonly areaD: string} {
  const pathParts: string[] = [];
  const areaRuns: SeriesLinePoint[][] = [];
  let currentRun: SeriesLinePoint[] = [];

  for (let i = 0; i < linePoints.length; i++) {
    const pt = linePoints[i];
    if (pt.hasVal) {
      const cmd = i === 0 || !linePoints[i - 1].hasVal ? 'M' : 'L';
      pathParts.push(`${cmd} ${pt.x.toFixed(1)} ${pt.y.toFixed(1)}`);
      currentRun.push(pt);
    } else if (currentRun.length > 0) {
      areaRuns.push(currentRun);
      currentRun = [];
    }
  }
  if (currentRun.length > 0) {
    areaRuns.push(currentRun);
  }

  const areaParts: string[] = [];
  for (const run of areaRuns) {
    if (run.length < 2) continue;
    const runLine = run
      .map(
        (pt, idx) =>
          `${idx === 0 ? 'M' : 'L'} ${pt.x.toFixed(1)} ${pt.y.toFixed(1)}`,
      )
      .join(' ');
    areaParts.push(
      `${runLine} ` +
        `L ${run[run.length - 1].x.toFixed(1)} ${baselineY.toFixed(1)} ` +
        `L ${run[0].x.toFixed(1)} ${baselineY.toFixed(1)} Z`,
    );
  }

  return {
    pathD: pathParts.join(' '),
    areaD: areaParts.join(' '),
  };
}

/**
 * Fetches `/series/{metric}` for the selected country or FIR when opened,
 * positions the anchored modal dialog, and renders the SVG trend chart with
 * interactive year hover readouts.
 */
export const YoYSeriesPopup: React.FC<YoYSeriesPopupProps> = ({
  isOpen,
  onClose,
  metricKey,
  metricTitle,
  categoryTitle = 'Above country',
  selectedCountry,
  selectedFirId,
  year,
  gwp = 'GWP50',
  anchorRect,
}) => {
  const [data, setData] = React.useState<TimeSeriesData | null>(null);
  const [loading, setLoading] = React.useState<boolean>(true);
  const [hoveredYear, setHoveredYear] = React.useState<number | null>(null);

  // Fetch multi-year data series on open or prop changes
  React.useEffect(() => {
    if (!isOpen) {
      setData(null);
      setHoveredYear(null);
      return;
    }

    const abortController = new AbortController();
    setData(null);
    setHoveredYear(null);
    setLoading(true);

    void fetchTimeSeriesFromBackend(metricKey, {
      iso: selectedCountry,
      firId: selectedFirId,
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
          console.warn('Failed to fetch time series from backend:', err);
        }
      });

    return () => {
      abortController.abort();
    };
  }, [isOpen, metricKey, selectedCountry, selectedFirId, gwp]);

  if (!isOpen) return null;

  // Popup placement relative to anchorRect
  const popupWidth = 470;
  const popupHeight = 280;
  let left = 20;
  let top = 20;

  if (typeof window !== 'undefined') {
    if (anchorRect) {
      left = anchorRect.left;
      if (left + popupWidth > window.innerWidth - 20) {
        left = Math.max(16, window.innerWidth - popupWidth - 20);
      }
      top = Math.max(
        20,
        Math.min(window.innerHeight - popupHeight - 20, anchorRect.top - 50),
      );
    } else {
      left = Math.max(16, (window.innerWidth - popupWidth) / 2);
      top = Math.max(20, (window.innerHeight - popupHeight) / 2);
    }
  }

  const points = data?.points ?? [];
  const {minVal, yUpper} = computeSeriesScale(points);

  // SVG dimensions
  const svgW = 420;
  const svgH = 175;
  const padLeft = 60;
  const padRight = 30;
  const padTop = 25;
  const padBottom = 30;
  const chartW = svgW - padLeft - padRight;
  const chartH = svgH - padTop - padBottom;

  const getX = (idx: number, total: number) =>
    getSeriesX({idx, total, padLeft, chartW});
  const getY = (val: number | null) =>
    getSeriesY({val, minVal, yUpper, padTop, chartH});

  // Build SVG Path
  const linePoints = points.map((p, i) => ({
    x: getX(i, points.length),
    y: getY(p.value),
    hasVal: hasFiniteValue(p),
    p,
  }));

  const validLinePoints = linePoints.filter((lp) => lp.hasVal);
  const baselineY = getY(Math.max(minVal, Math.min(0, yUpper)));
  const {pathD, areaD} = buildSeriesSvgPaths(linePoints, baselineY);

  // Y-axis grid ticks (3 ticks: 0, mid, max)
  const yTicks = [
    {val: minVal, y: padTop + chartH},
    {val: minVal + (yUpper - minVal) * 0.5, y: padTop + chartH * 0.5},
    {val: yUpper, y: padTop},
  ];

  const formatTick = (val: number) => {
    // Dispatch on the machine unit identifier rather than formatted display
    // labels.
    if (
      data?.unit === UNIT_RATIO_OF_NATIONAL_GHG ||
      data?.unit === UNIT_RATIO_OF_WORLD_AVG_GHG_PER_CAPITA
    ) {
      // The value is a ratio; the axis is labelled '%'.
      return `${(val * 100).toFixed(1)}`;
    }
    if (data?.unit === UNIT_MILLIWATTS_PER_SQ_M) {
      return `${val.toFixed(1)}`;
    }
    const abs = Math.abs(val);
    if (abs >= 1_000_000_000) return `${(val / 1_000_000_000).toFixed(1)}B`;
    if (abs >= 1_000_000) return `${(val / 1_000_000).toFixed(1)}M`;
    if (abs >= 1_000) return `${(val / 1_000).toFixed(0)}K`;
    if (abs > 0 && abs < 10) return Number(val.toFixed(2)).toString();
    return `${val.toFixed(0)}`;
  };

  const activeYearKey = hoveredYear ?? year;
  const activePointIdx = points.findIndex((p) => p.year === activeYearKey);
  const activePoint = activePointIdx >= 0 ? points[activePointIdx] : null;
  const activeYoY =
    activePointIdx >= 0 ? deriveYoYPercent(points, activePointIdx) : null;

  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      top={top}
      left={left}
      width={popupWidth}
      height={popupHeight}
      headerContent={
        <>
          <Box
            display="flex"
            alignItems="center"
            style={{gap: '6px', marginBottom: '4px'}}>
            <span
              style={{
                fontSize: '11px',
                fontWeight: 600,
                textTransform: 'uppercase',
                color: '#1a73e8',
                backgroundColor: '#e8f0fe',
                padding: '2px 6px',
                borderRadius: '4px',
                letterSpacing: '0.4px',
              }}>
              {categoryTitle}
            </span>
            {activePoint != null && hasFiniteValue(activePoint) && (
              <span
                style={{
                  fontSize: '11px',
                  fontWeight: 600,
                  color: '#3c4043',
                  backgroundColor: '#f1f3f4',
                  padding: '2px 6px',
                  borderRadius: '4px',
                }}>
                {activePoint.year}: {formatTick(activePoint.value)}
                {activeYoY != null
                  ? ` (${activeYoY >= 0 ? '+' : ''}` +
                    `${activeYoY.toFixed(1)}% YoY)`
                  : ''}
              </span>
            )}
          </Box>
          <Typography
            variant="h6"
            style={{
              fontSize: '15px',
              fontWeight: 600,
              color: '#202124',
              lineHeight: 1.3,
            }}>
            {data?.metricTitle ?? metricTitle}
          </Typography>
        </>
      }>
      {/* Content Body */}
      <Box
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          padding: '12px 16px',
          backgroundColor: '#ffffff',
          position: 'relative',
        }}>
        {loading ? (
          <Box
            display="flex"
            flexDirection="column"
            alignItems="center"
            justifyContent="center"
            style={{height: '180px', gap: '12px'}}>
            <CircularProgress size={32} style={{color: '#1a73e8'}} />
            <Typography style={{fontSize: '13px', color: '#5f6368'}}>
              Loading multi-year time-series...
            </Typography>
          </Box>
        ) : points.length === 0 || validLinePoints.length === 0 ? (
          <Box
            display="flex"
            alignItems="center"
            justifyContent="center"
            style={{height: '180px'}}>
            <Typography style={{fontSize: '13px', color: '#80868b'}}>
              No historical time-series data available.
            </Typography>
          </Box>
        ) : (
          <>
            {/* SVG Line Graph */}
            <Box style={{width: '100%', position: 'relative'}}>
              <svg
                viewBox={`0 0 ${svgW} ${svgH}`}
                style={{width: '100%', height: '175px', overflow: 'visible'}}>
                <defs>
                  <linearGradient id="yoyAreaGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#1a73e8" stopOpacity="0.22" />
                    <stop
                      offset="100%"
                      stopColor="#1a73e8"
                      stopOpacity="0.01"
                    />
                  </linearGradient>
                </defs>

                {/* Horizontal Grid lines */}
                {yTicks.map((t, idx) => (
                  <g key={`grid_${idx}`}>
                    <line
                      x1={padLeft}
                      y1={t.y}
                      x2={padLeft + chartW}
                      y2={t.y}
                      stroke="#e0e0e0"
                      strokeDasharray="3 3"
                      strokeWidth="1"
                    />
                    <text
                      x={padLeft - 8}
                      y={t.y + 4}
                      textAnchor="end"
                      fontSize="10"
                      fill="#80868b"
                      fontFamily="inherit">
                      {formatTick(t.val)}
                    </text>
                  </g>
                ))}

                {/* Y Axis Unit label */}
                {data?.unit && (
                  <text
                    x={padLeft}
                    y={padTop - 10}
                    textAnchor="start"
                    fontSize="10"
                    fontWeight="500"
                    fill="#5f6368"
                    fontFamily="inherit">
                    {unitDisplayLabel(data.unit)}
                  </text>
                )}

                {/* Area fill */}
                {areaD && <path d={areaD} fill="url(#yoyAreaGrad)" />}

                {/* Line stroke */}
                {pathD && (
                  <path
                    d={pathD}
                    fill="none"
                    stroke="#1a73e8"
                    strokeWidth="2.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                )}

                {/* Data Points & X Axis labels */}
                {linePoints.map((pt) => {
                  const isCurrentYear = pt.p.year === year;
                  const isHovered = hoveredYear === pt.p.year;
                  return (
                    <g
                      key={`pt_${pt.p.year}`}
                      style={{cursor: 'pointer'}}
                      onMouseEnter={() => {
                        setHoveredYear(pt.p.year);
                      }}
                      onMouseLeave={() => {
                        setHoveredYear(null);
                      }}>
                      {/* Vertical dotted guide on hover/active */}
                      {(isHovered || (isCurrentYear && !hoveredYear)) && (
                        <line
                          x1={pt.x}
                          y1={padTop}
                          x2={pt.x}
                          y2={padTop + chartH}
                          stroke="#1a73e8"
                          strokeDasharray="2 2"
                          strokeWidth="1.2"
                          opacity="0.6"
                        />
                      )}

                      {/* X-axis tick label */}
                      <text
                        x={pt.x}
                        y={padTop + chartH + 18}
                        textAnchor="middle"
                        fontSize={isCurrentYear ? '12' : '11'}
                        fontWeight={isCurrentYear ? '700' : '500'}
                        fill={isCurrentYear ? '#1a73e8' : '#5f6368'}
                        fontFamily="inherit">
                        {pt.p.year}
                      </text>

                      {/* Point Marker */}
                      {pt.hasVal && (
                        <>
                          {/* Outer halo for active/hovered year */}
                          {(isCurrentYear || isHovered) && (
                            <circle
                              cx={pt.x}
                              cy={pt.y}
                              r={isHovered ? 8 : 7}
                              fill="none"
                              stroke="#1a73e8"
                              strokeWidth="2"
                              opacity="0.35"
                            />
                          )}
                          <circle
                            cx={pt.x}
                            cy={pt.y}
                            r={isCurrentYear || isHovered ? 5.5 : 4.5}
                            fill={
                              isCurrentYear || isHovered ? '#1a73e8' : '#ffffff'
                            }
                            stroke="#1a73e8"
                            strokeWidth="2.5"
                          />
                        </>
                      )}
                    </g>
                  );
                })}
              </svg>
            </Box>
          </>
        )}
      </Box>
    </ModalShell>
  );
};
