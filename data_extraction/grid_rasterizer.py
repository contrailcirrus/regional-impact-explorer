"""Fast geospatial rasterization and dense grid binning utilities."""

from collections.abc import Mapping, MutableMapping, Sequence
import dataclasses
from typing import Any, NamedTuple
import numpy as np
from common import constants
from data_extraction import geo_utils

# Sparse grid cells store their row and column as int16, so larger grids
# would silently wrap around to wrong cells.
MAX_GRID_DIMENSION = int(np.iinfo(np.int16).max)


def _validate_coordinate_range(
    name_min: str,
    val_min: float,
    name_max: str,
    val_max: float,
    *,
    limit: float,
) -> None:
  """Validates coordinate interval is in [-limit, limit] and non-empty."""
  if not -limit <= val_min <= limit:
    raise ValueError(
        f"{name_min}={val_min} is out of range: must be in [-{limit}, {limit}]."
    )
  if not -limit <= val_max <= limit:
    raise ValueError(
        f"{name_max}={val_max} is out of range: must be in [-{limit}, {limit}]."
    )
  if val_min >= val_max:
    raise ValueError(
        f"{name_min} ({val_min}) must be strictly less than {name_max}"
        f" ({val_max})."
    )


@dataclasses.dataclass(frozen=True)
class GridConfig:
  """Configuration parameters for global/regional rasterization.

  Raises:
    ValueError: If extent coordinates are swapped or out of range, if
      step_size_meters is not positive, or if `width` or `height` is not in
      [1, MAX_GRID_DIMENSION].
  """

  min_lon: float
  max_lon: float
  min_lat: float
  max_lat: float
  width: int
  height: int
  step_size_meters: float

  def __post_init__(self) -> None:
    """Validates bounding box coordinates, grid dimensions, and step size."""
    _validate_coordinate_range(
        "min_lon", self.min_lon, "max_lon", self.max_lon, limit=180.0
    )
    _validate_coordinate_range(
        "min_lat", self.min_lat, "max_lat", self.max_lat, limit=90.0
    )
    if self.step_size_meters <= 0.0:
      raise ValueError(
          f"step_size_meters={self.step_size_meters} must be positive (> 0.0)."
      )
    for name, value in (
        (constants.KEY_WIDTH, self.width),
        (constants.KEY_HEIGHT, self.height),
    ):
      if not 1 <= value <= MAX_GRID_DIMENSION:
        raise ValueError(
            f"Grid {name}={value} is out of range: it must be between 1 and"
            f" {MAX_GRID_DIMENSION} cells, since sparse cells store their"
            " row and column as int16."
        )

  def to_dict(self) -> dict[str, Any]:
    """Serializes the configuration to a dictionary."""
    return {
        constants.KEY_MIN_LON: self.min_lon,
        constants.KEY_MAX_LON: self.max_lon,
        constants.KEY_MIN_LAT: self.min_lat,
        constants.KEY_MAX_LAT: self.max_lat,
        constants.KEY_WIDTH: self.width,
        constants.KEY_HEIGHT: self.height,
        constants.KEY_STEP_SIZE_METERS: self.step_size_meters,
    }

  @classmethod
  def from_dict(cls, data: Mapping[str, Any]) -> "GridConfig":
    """Strictly deserializes a GridConfig from a dictionary.

    Args:
      data: Dictionary containing grid configuration fields.

    Returns:
      A validated GridConfig instance.

    Raises:
      ValueError: If data is not a non-empty dictionary, is missing any
        required field, or fails numeric coercion or GridConfig validation.
    """
    if not isinstance(data, Mapping) or not data:
      raise ValueError("GridConfig dictionary must be non-empty.")
    required = (
        constants.KEY_MIN_LON,
        constants.KEY_MAX_LON,
        constants.KEY_MIN_LAT,
        constants.KEY_MAX_LAT,
        constants.KEY_WIDTH,
        constants.KEY_HEIGHT,
        constants.KEY_STEP_SIZE_METERS,
    )
    missing = [k for k in required if k not in data]
    if missing:
      raise ValueError(f"GridConfig dictionary is missing fields: {missing}")
    return cls(
        min_lon=float(data[constants.KEY_MIN_LON]),
        max_lon=float(data[constants.KEY_MAX_LON]),
        min_lat=float(data[constants.KEY_MIN_LAT]),
        max_lat=float(data[constants.KEY_MAX_LAT]),
        width=int(data[constants.KEY_WIDTH]),
        height=int(data[constants.KEY_HEIGHT]),
        step_size_meters=float(data[constants.KEY_STEP_SIZE_METERS]),
    )


class _SegmentArrays(NamedTuple):
  """Parallel per-segment arrays consumed by the rasterizer.

  Attributes:
    xs: Start longitudes.
    ys: Start latitudes.
    xe: End longitudes.
    ye: End latitudes.
    ef: Energy forcing values in MJ.
    weights: Density weight of each segment.
    months: Month values (e.g. 202501).
    hours: Local hour values (0..23).
  """

  xs: np.ndarray
  ys: np.ndarray
  xe: np.ndarray
  ye: np.ndarray
  ef: np.ndarray
  weights: np.ndarray
  months: np.ndarray
  hours: np.ndarray

  def select(self, index: np.ndarray | slice) -> "_SegmentArrays":
    """Returns the segments picked by a mask, permutation or slice."""
    return self._make(array[index] for array in self)


class _PixelGrid(NamedTuple):
  """Grid configuration plus its derived cell sizes and flat index dtype."""

  config: GridConfig
  dx: float
  dy: float
  index_dtype: Any

  @classmethod
  def from_config(cls, config: GridConfig) -> "_PixelGrid":
    """Derives cell sizes in degrees and the narrowest flat index dtype."""
    return cls(
        config=config,
        dx=(config.max_lon - config.min_lon) / config.width,
        dy=(config.max_lat - config.min_lat) / config.height,
        index_dtype=(
            np.int32 if config.width * config.height < (1 << 31) else np.int64
        ),
    )


def _split_crossing_segments(
    crossing: _SegmentArrays,
) -> tuple[_SegmentArrays, _SegmentArrays]:
  """Cuts antimeridian-crossing segments into the parts on either side.

  Energy forcing and density weight are shared in proportion to the fraction
  of the segment's longitude span that lies on each side.

  Args:
    crossing: Segments that all satisfy `|xe - xs| > 180.0`.

  Returns:
    The parts ending at the antimeridian and the parts starting from it.
  """
  xs_c, ys_c, xe_c, ye_c = crossing.xs, crossing.ys, crossing.xe, crossing.ye
  eastbound, before_frac_f64 = geo_utils.antimeridian_crossing_fraction(
      xs_c, xe_c
  )
  before_fraction = before_frac_f64.astype(np.float32)
  after_fraction = np.float32(1.0) - before_fraction
  ymid = (ys_c + before_fraction * (ye_c - ys_c)).astype(np.float32)

  x_split1 = np.where(eastbound, 180.0, -180.0).astype(np.float32)
  x_split2 = np.where(eastbound, -180.0, 180.0).astype(np.float32)

  before = crossing._replace(
      xe=x_split1,
      ye=ymid,
      ef=crossing.ef * before_fraction,
      weights=crossing.weights * before_fraction,
  )
  after = crossing._replace(
      xs=x_split2,
      ys=ymid,
      ef=crossing.ef * after_fraction,
      weights=crossing.weights * after_fraction,
  )
  return before, after


def _split_antimeridian_grid_segments(
    segments: _SegmentArrays,
) -> _SegmentArrays:
  """Splits segments crossing the antimeridian (|xe - xs| > 180.0).

  Args:
    segments: Segments to check.

  Returns:
    The non-crossing segments followed by the western/eastern halves of each
    crossing segment, or `segments` itself if none cross.
  """
  crosses = np.abs(segments.xe - segments.xs) > 180.0
  if not np.any(crosses):
    return segments
  kept = segments.select(~crosses)
  before, after = _split_crossing_segments(segments.select(crosses))
  return _SegmentArrays._make(
      np.concatenate(parts) for parts in zip(kept, before, after, strict=True)
  )


class _MonthHourGroup(NamedTuple):
  """Contiguous slice [start_idx, end_idx) for a single (month, hour) bucket."""

  month: int
  hour: int
  start_idx: int
  end_idx: int


class _RasterizedChunk(NamedTuple):
  """Binned pixel indices and weights for a sub-chunk of flight segments."""

  pixel_indices: np.ndarray
  density_weights: np.ndarray
  impact_weights: np.ndarray


def _month_hour_groups_from_sorted(
    *,
    months_sorted: np.ndarray,
    hours_sorted: np.ndarray,
    sorted_keys: np.ndarray,
) -> list[_MonthHourGroup]:
  """Finds the contiguous (month, hour) runs in key-sorted segment arrays.

  Args:
    months_sorted: Month of each segment, in key order.
    hours_sorted: Hour of each segment, in key order.
    sorted_keys: Combined (month, hour) sort keys, ascending.

  Returns:
    One group per distinct key, in key order.
  """
  num_segments = len(sorted_keys)
  change_idx = np.flatnonzero(sorted_keys[1:] != sorted_keys[:-1]) + 1
  group_starts = np.concatenate(([0], change_idx))
  group_ends = np.concatenate((change_idx, [num_segments]))
  return [
      _MonthHourGroup(
          month=int(months_sorted[g_start]),
          hour=int(hours_sorted[g_start]),
          start_idx=int(g_start),
          end_idx=int(g_end),
      )
      for g_start, g_end in zip(group_starts, group_ends, strict=True)
  ]


def _group_segments_by_month_hour(
    segments: _SegmentArrays,
) -> tuple[_SegmentArrays, list[_MonthHourGroup]]:
  """Sorts segment arrays by (month, hour) and identifies group boundaries."""
  num_segments = len(segments.xs)
  months_all, hours_all = segments.months, segments.hours
  first_m = int(months_all[0])
  first_h = int(hours_all[0])
  if bool(np.all(months_all == first_m) and np.all(hours_all == first_h)):
    return segments, [
        _MonthHourGroup(
            month=first_m, hour=first_h, start_idx=0, end_idx=num_segments
        )
    ]

  mh_keys = geo_utils.pack_month_hour_keys(months_all, hours_all)
  order = np.argsort(mh_keys, kind="stable")
  sorted_segments = segments.select(order)
  mh_groups = _month_hour_groups_from_sorted(
      months_sorted=sorted_segments.months,
      hours_sorted=sorted_segments.hours,
      sorted_keys=mh_keys[order],
  )
  return sorted_segments, mh_groups


def _count_waypoints_per_segment(
    segments: _SegmentArrays, grid: _PixelGrid
) -> np.ndarray:
  """Returns the number of waypoints to sample along each segment.

  A segment gets two waypoints per pixel spanned or one per
  `step_size_meters` travelled, whichever is more, and at least one.

  Args:
    segments: Segments to sample.
    grid: Target pixel grid.

  Returns:
    int32 waypoint counts, one per segment.
  """
  xs, ys, xe, ye = segments.xs, segments.ys, segments.xe, segments.ye
  lengths = geo_utils.haversine_meters(xs, ys, xe, ye)
  pixel_span = np.maximum(np.abs(xe - xs) / grid.dx, np.abs(ye - ys) / grid.dy)
  pixel_steps = np.ceil(2.0 * pixel_span).astype(np.int32)
  meter_steps = np.ceil(lengths / grid.config.step_size_meters).astype(np.int32)
  return np.clip(np.maximum(pixel_steps, meter_steps), a_min=1, a_max=None)


def _interpolate_waypoints(
    segments: _SegmentArrays,
    *,
    num_points: np.ndarray,
    inv_num_points: np.ndarray,
    total_pts: int,
) -> tuple[np.ndarray, np.ndarray]:
  """Places waypoints at the midpoints of equal sub-intervals of each segment.

  Args:
    segments: Segments to sample.
    num_points: Number of waypoints per segment.
    inv_num_points: float32 reciprocal of `num_points`.
    total_pts: Sum of `num_points`.

  Returns:
    Waypoint longitudes and latitudes, grouped by segment.
  """
  boundaries = np.cumsum(num_points, dtype=np.int64)
  steps = np.ones(total_pts, dtype=np.int32)
  steps[0] = 0
  steps[boundaries[:-1]] = 1 - num_points[:-1]
  grouped_idx = np.cumsum(steps, out=steps)

  interp_fraction = (
      grouped_idx.astype(np.float32) + np.float32(0.5)
  ) * np.repeat(inv_num_points, num_points)

  lons = np.repeat(segments.xe - segments.xs, num_points)
  lons *= interp_fraction
  lons += np.repeat(segments.xs, num_points)

  lats = np.repeat(segments.ye - segments.ys, num_points)
  lats *= interp_fraction
  lats += np.repeat(segments.ys, num_points)
  return lons, lats


def _waypoint_bbox_mask(
    lons: np.ndarray, lats: np.ndarray, config: GridConfig
) -> np.ndarray:
  """Returns which waypoints fall inside the grid's bounding box."""
  return (
      (lons >= config.min_lon)
      & (lons <= config.max_lon)
      & (lats >= config.min_lat)
      & (lats <= config.max_lat)
  )


def _flat_pixel_indices(
    lons: np.ndarray, lats: np.ndarray, grid: _PixelGrid
) -> np.ndarray:
  """Maps waypoints to row-major flat pixel indices, clamped to the grid."""
  config = grid.config
  cols = np.clip(
      np.floor((lons - config.min_lon) / grid.dx).astype(grid.index_dtype),
      0,
      config.width - 1,
  )
  rows = np.clip(
      np.floor((lats - config.min_lat) / grid.dy).astype(grid.index_dtype),
      0,
      config.height - 1,
  )
  return rows * grid.index_dtype(config.width) + cols


def _sum_by_pixel(chunk: _RasterizedChunk) -> _RasterizedChunk:
  """Sums density and impact weights per unique pixel index (float64)."""
  unique_idx, inverse = np.unique(chunk.pixel_indices, return_inverse=True)
  return _RasterizedChunk(
      pixel_indices=unique_idx,
      density_weights=np.bincount(inverse, weights=chunk.density_weights),
      impact_weights=np.bincount(inverse, weights=chunk.impact_weights),
  )


def _rasterize_chunk_waypoints(
    segments: _SegmentArrays,
    *,
    grid: _PixelGrid,
    multi_chunk: bool,
) -> _RasterizedChunk | None:
  """Bins waypoints for a segment sub-chunk, or returns `None` if empty.

  Args:
    segments: Segments of the sub-chunk.
    grid: Target pixel grid.
    multi_chunk: Whether the bucket spans several sub-chunks, in which case
      weights are pre-summed per pixel to bound memory.

  Returns:
    Per-waypoint (or per-pixel if `multi_chunk`) indices and weights, or
    `None` if no waypoint falls inside the grid.
  """
  num_points = _count_waypoints_per_segment(segments, grid)
  total_pts = int(np.sum(num_points))
  if total_pts <= 0:
    return None

  inv_num_points = np.float32(1.0) / num_points.astype(np.float32)
  lons, lats = _interpolate_waypoints(
      segments,
      num_points=num_points,
      inv_num_points=inv_num_points,
      total_pts=total_pts,
  )

  bbox_mask = _waypoint_bbox_mask(lons, lats, grid.config)
  all_in_bbox = bool(np.all(bbox_mask))
  if not all_in_bbox:
    if not np.any(bbox_mask):
      return None
    lons = lons[bbox_mask]
    lats = lats[bbox_mask]

  density_weights = np.repeat(segments.weights * inv_num_points, num_points)
  impact_weights = np.repeat(segments.ef * inv_num_points, num_points)
  if not all_in_bbox:
    density_weights = density_weights[bbox_mask]
    impact_weights = impact_weights[bbox_mask]

  chunk = _RasterizedChunk(
      pixel_indices=_flat_pixel_indices(lons, lats, grid),
      density_weights=density_weights,
      impact_weights=impact_weights,
  )
  return _sum_by_pixel(chunk) if multi_chunk else chunk


def _merge_chunks(
    chunks: Sequence[_RasterizedChunk], *, multi_chunk: bool
) -> _RasterizedChunk | None:
  """Sums the weights of all chunks per unique pixel as float32.

  Args:
    chunks: Non-empty sequence of rasterized chunks of one bucket.
    multi_chunk: Whether each chunk is already summed per pixel.

  Returns:
    The merged per-pixel weights, or `None` if the chunks hold no waypoints.
  """
  if len(chunks) == 1 and multi_chunk:
    merged = chunks[0]
  else:
    if len(chunks) == 1:
      combined = chunks[0]
    else:
      combined = _RasterizedChunk._make(
          np.concatenate(parts) for parts in zip(*chunks, strict=True)
      )
    if combined.pixel_indices.size == 0:
      return None
    merged = _sum_by_pixel(combined)
  return merged._replace(
      density_weights=merged.density_weights.astype(np.float32),
      impact_weights=merged.impact_weights.astype(np.float32),
  )


def _store_nonzero_grids(
    merged: _RasterizedChunk,
    *,
    config: GridConfig,
    m_val: int,
    h_val: int,
    result_grids: MutableMapping[
        tuple[str, int, int], tuple[np.ndarray, np.ndarray, np.ndarray]
    ],
) -> None:
  """Stores sparse (rows, cols, values) density and impact grids if non-zero.

  Args:
    merged: Per-pixel float32 weights of the bucket.
    config: Grid configuration.
    m_val: Month of the bucket.
    h_val: Hour of the bucket.
    result_grids: Output mapping, updated in place.
  """
  u_idx = merged.pixel_indices
  nz_rows = (u_idx // config.width).astype(np.int16)
  nz_cols = (u_idx % config.width).astype(np.int16)

  for g_type, u_vals in (
      (constants.GRID_KIND_DENSITY, merged.density_weights),
      (constants.GRID_KIND_IMPACT, merged.impact_weights),
  ):
    key = (g_type, m_val, h_val)
    nz_mask = np.abs(u_vals) > 0
    if not np.any(nz_mask):
      continue
    result_grids[key] = (
        nz_rows[nz_mask],
        nz_cols[nz_mask],
        u_vals[nz_mask],
    )


def _populate_result_grids(
    chunks: Sequence[_RasterizedChunk],
    *,
    multi_chunk: bool,
    config: GridConfig,
    m_val: int,
    h_val: int,
    result_grids: MutableMapping[
        tuple[str, int, int], tuple[np.ndarray, np.ndarray, np.ndarray]
    ],
) -> None:
  """Aggregates rasterized chunks and populates sparse result grids."""
  merged = _merge_chunks(chunks, multi_chunk=multi_chunk)
  if merged is None:
    return
  _store_nonzero_grids(
      merged,
      config=config,
      m_val=m_val,
      h_val=h_val,
      result_grids=result_grids,
  )


def _as_segment_arrays(
    *,
    xs_all: np.ndarray,
    ys_all: np.ndarray,
    xe_all: np.ndarray,
    ye_all: np.ndarray,
    ef_all: np.ndarray,
    months_all: np.ndarray,
    hours_all: np.ndarray,
) -> _SegmentArrays:
  """Casts segment inputs, zeroing non-finite EF and setting unit weights."""
  xs_all = np.asarray(xs_all, dtype=np.float32)
  ys_all = np.asarray(ys_all, dtype=np.float32)
  xe_all = np.asarray(xe_all, dtype=np.float32)
  ye_all = np.asarray(ye_all, dtype=np.float32)
  ef_all = np.asarray(ef_all, dtype=np.float32)
  if not bool(np.all(np.isfinite(ef_all))):
    ef_all = np.where(np.isfinite(ef_all), ef_all, np.float32(0.0))
  return _SegmentArrays(
      xs=xs_all,
      ys=ys_all,
      xe=xe_all,
      ye=ye_all,
      ef=ef_all,
      months=np.asarray(months_all, dtype=np.int32),
      hours=np.asarray(hours_all, dtype=np.int32),
      weights=np.ones(len(xs_all), dtype=np.float32),
  )


def _prepare_and_group_grid_segments(
    *,
    xs_all: np.ndarray,
    ys_all: np.ndarray,
    xe_all: np.ndarray,
    ye_all: np.ndarray,
    ef_all: np.ndarray,
    months_all: np.ndarray,
    hours_all: np.ndarray,
) -> tuple[_SegmentArrays, list[_MonthHourGroup]]:
  """Casts segment arrays, splits at antimeridian, and groups by bucket."""
  segments = _as_segment_arrays(
      xs_all=xs_all,
      ys_all=ys_all,
      xe_all=xe_all,
      ye_all=ye_all,
      ef_all=ef_all,
      months_all=months_all,
      hours_all=hours_all,
  )
  return _group_segments_by_month_hour(
      _split_antimeridian_grid_segments(segments)
  )


def _rasterize_month_hour_group(
    *,
    segments: _SegmentArrays,
    group: _MonthHourGroup,
    grid: _PixelGrid,
    sub_chunk_size: int,
    result_grids: MutableMapping[
        tuple[str, int, int], tuple[np.ndarray, np.ndarray, np.ndarray]
    ],
) -> None:
  """Rasterizes one (month, hour) slice of segments in bounded sub-chunks."""
  multi_chunk = (group.end_idx - group.start_idx) > sub_chunk_size
  chunks: list[_RasterizedChunk] = []

  for start_idx in range(group.start_idx, group.end_idx, sub_chunk_size):
    end_idx = min(start_idx + sub_chunk_size, group.end_idx)
    chunk_res = _rasterize_chunk_waypoints(
        segments.select(slice(start_idx, end_idx)),
        grid=grid,
        multi_chunk=multi_chunk,
    )
    if chunk_res is not None:
      chunks.append(chunk_res)

  if not chunks:
    return

  _populate_result_grids(
      chunks=chunks,
      multi_chunk=multi_chunk,
      config=grid.config,
      m_val=group.month,
      h_val=group.hour,
      result_grids=result_grids,
  )


def rasterize_segments_arrays(
    *,
    xs_all: np.ndarray,
    ys_all: np.ndarray,
    xe_all: np.ndarray,
    ye_all: np.ndarray,
    ef_all: np.ndarray,
    months_all: np.ndarray,
    hours_all: np.ndarray,
    config: GridConfig,
    sub_chunk_size: int = 25000,
) -> dict[tuple[str, int, int], tuple[np.ndarray, np.ndarray, np.ndarray]]:
  """Interpolates waypoints along segments and bins them into sparse grids.

  Args:
    xs_all: Start longitudes.
    ys_all: Start latitudes.
    xe_all: End longitudes.
    ye_all: End latitudes.
    ef_all: Energy forcing values in MJ.
    months_all: Month values (e.g. 202501).
    hours_all: Local hour values (0..23).
    config: Rasterization grid dimensions and spatial extent.
    sub_chunk_size: Internal batch size to cap memory allocations.

  Returns:
    Dictionary mapping (grid_type, month, hour) -> (nz_rows, nz_cols, nz_vals)
    sparse tuple. grid_type is 'density' or 'impact'.
  """
  if xs_all.size == 0:
    return {}

  segments, mh_groups = _prepare_and_group_grid_segments(
      xs_all=xs_all,
      ys_all=ys_all,
      xe_all=xe_all,
      ye_all=ye_all,
      ef_all=ef_all,
      months_all=months_all,
      hours_all=hours_all,
  )
  grid = _PixelGrid.from_config(config)

  result_grids: dict[
      tuple[str, int, int], tuple[np.ndarray, np.ndarray, np.ndarray]
  ] = {}
  for group in mh_groups:
    _rasterize_month_hour_group(
        segments=segments,
        group=group,
        grid=grid,
        sub_chunk_size=sub_chunk_size,
        result_grids=result_grids,
    )

  return result_grids


def downsample_grid_sum(grid_2d: np.ndarray, factor: int) -> np.ndarray:
  """Downsamples a 2D sum grid by summing factor x factor blocks.

  Args:
    grid_2d: 2D numpy array to downsample.
    factor: Integer block size for downsampling.

  Returns:
    Downsampled 2D numpy array.

  Raises:
    ValueError: If grid dimensions are not divisible by factor.
  """
  if factor <= 1:
    return grid_2d
  height, width = grid_2d.shape
  if height % factor != 0 or width % factor != 0:
    raise ValueError(
        f"Grid shape ({height}, {width}) must be divisible by downsample factor"
        f" {factor} to avoid edge truncation (h % factor = {height % factor},"
        f" w % factor = {width % factor})."
    )
  new_h, new_w = height // factor, width // factor
  return grid_2d.reshape(new_h, factor, new_w, factor).sum(axis=(1, 3))


REF_PIXELS = 1200.0 * 600.0


def encode_grid_to_float16_log10(
    grid_2d: np.ndarray, width: int, height: int
) -> np.ndarray:
  """Converts raw 2D grid to reference-scaled signed log10 in float16."""
  density_scale_factor = np.float32(float(width * height) / REF_PIXELS)
  buf = np.multiply(grid_2d, density_scale_factor, dtype=np.float32)
  neg = buf < 0.0
  np.abs(buf, out=buf)
  np.add(buf, np.float32(1.0), out=buf)
  np.log10(buf, out=buf)
  np.negative(buf, out=buf, where=neg)
  return buf.astype(np.float16)


def ref_scaled_abs_max(grid_2d: np.ndarray, width: int, height: int) -> float:
  """Returns the largest reference-scaled `|value|` of a 2D grid.

  Reference scaling multiplies each cell by `width * height / REF_PIXELS`, so a
  coarser LOD (cells summed over `f x f` blocks, scale divided by `f ** 2`)
  holds block averages of the full-resolution values. The full-resolution
  maximum therefore bounds every LOD of the same grid, and is what the UI
  needs as the top of its colour scale.

  Args:
    grid_2d: Raw (unencoded) 2D grid.
    width: Grid width in cells.
    height: Grid height in cells.

  Returns:
    The reference-scaled absolute maximum; 0.0 for an all-zero grid.

  Raises:
    ValueError: if the grid is empty or contains a non-finite value.
  """
  if grid_2d.size == 0:
    raise ValueError("cannot take the maximum of an empty grid")
  abs_max = float(np.max(np.abs(grid_2d)))
  if not np.isfinite(abs_max):
    raise ValueError(f"grid contains a non-finite value (max |v| = {abs_max})")
  return abs_max * float(width * height) / REF_PIXELS
