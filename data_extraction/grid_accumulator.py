"""Accumulator structures and array operations for spatial grid aggregation."""

from collections.abc import Sequence
from typing import NamedTuple, TypeAlias
import numpy as np
from common import constants
from data_extraction import grid_rasterizer


class DenseBoundingBox(NamedTuple):
  """A tight rectangular sub-grid slice bounded by row/col intervals.

  Attributes:
    r_min: First grid row covered by `sub_grid` (inclusive).
    r_max: Grid row just past the last row covered by `sub_grid` (exclusive).
    c_min: First grid column covered by `sub_grid` (inclusive).
    c_max: Grid column just past the last column covered by `sub_grid`
      (exclusive).
    sub_grid: Float array of shape `(r_max - r_min, c_max - c_min)` holding the
      accumulated values for the bounded region.
  """

  r_min: int
  r_max: int
  c_min: int
  c_max: int
  sub_grid: np.ndarray


class SparseAccumulator(NamedTuple):
  """Sparse non-zero coordinates and values accumulator.

  Attributes:
    tag: Encoding tag; always `"sparse"`.
    rows: Grid row index of each point.
    cols: Grid column index of each point, aligned with `rows`.
    values: Accumulated value of each point, aligned with `rows`. Points may
      repeat a `(row, col)` cell until the accumulator is compacted.
    last_compact_len: Number of points right after the most recent compaction,
      used to decide when the next compaction is due.
  """

  tag: str  # Always "sparse"
  rows: np.ndarray
  cols: np.ndarray
  values: np.ndarray
  last_compact_len: int


# The per-shard grid slice emitted by
# `shard_processor.rasterize_flight_grid_slices`, and the empty seed
# `beam_stages.combine_grid_slices_to_export_keys` adds for every
# export key: `("sparse", rows, cols, vals)`.
SparseSlice: TypeAlias = tuple[str, np.ndarray, np.ndarray, np.ndarray]
GridAccumulator: TypeAlias = DenseBoundingBox | SparseAccumulator | None
# Everything the grid combiners accept as input: raw slices in Stage 1, plus
# the Stage-1 outputs Stage 2 consumes.
GridPayload: TypeAlias = DenseBoundingBox | SparseAccumulator | SparseSlice


def as_grid_acc(value: GridPayload | None) -> GridAccumulator:
  """Normalizes a grid payload to a `SparseAccumulator` or `DenseBoundingBox`.

  Arrays are wrapped, not copied: when `value` is an input element the result
  is shared with other keys and must be treated as read-only.

  Args:
    value: A `SparseSlice`, an accumulator, or `None`.

  Returns:
    A `SparseAccumulator` or `DenseBoundingBox`, or `None` when `value` is
    `None`.

  Raises:
    TypeError: If `value` is not one of the supported encodings.
  """
  if value is None or isinstance(value, (SparseAccumulator, DenseBoundingBox)):
    return value
  if isinstance(value, tuple) and len(value) == 4:
    tag, rows, cols, values = value
    if tag == constants.GRID_ACC_SPARSE:
      return SparseAccumulator(
          constants.GRID_ACC_SPARSE, rows, cols, values, len(values)
      )
  raise TypeError(f"Unsupported grid payload: {type(value)!r}")


def is_empty_box(box: DenseBoundingBox) -> bool:
  """Returns whether `box` has zero area or an empty `sub_grid`."""
  return (
      box.sub_grid.size == 0
      or box.r_max <= box.r_min
      or box.c_max <= box.c_min
  )


def merge_grid_boxes(
    box_a: DenseBoundingBox, box_b: DenseBoundingBox
) -> DenseBoundingBox:
  """Merges two sub-bounding box slices into their minimal bounding union.

  Ownership contract, which the whole grid combiner depends on:

    * `box_a` is the ACCUMULATOR. Beam's `CombineFn` contract explicitly permits
      mutating an accumulator in place, and doing so is the only way to keep
      this stage tractable: for the `("impact", "total", 0)` key the accumulator
      converges on the full 7200x3600 float32 grid, so copying it per element
      would cost a 103 MB copy plus a 103 MB allocation for every one of the
      hundreds of thousands of slices in a bundle.
    * `box_b` is an INPUT ELEMENT and must never be mutated.
      `beam_stages.FanoutGridExportKeysDoFn` yields the *same* payload object to
      the total, monthly and hourly keys, so three independent accumulators can
      reference one buffer. Writing into it would corrupt two unrelated buckets.

  `merge_accumulators` passes two accumulators, so mutating `box_a` is safe
  there too.

  Args:
    box_a: The accumulator box. May be mutated in place.
    box_b: The input box. Treated as read-only.

  Returns:
    A DenseBoundingBox covering the union of both extents.
  """
  r_min_a, r_max_a, c_min_a, c_max_a, grid_a = box_a
  r_min_b, r_max_b, c_min_b, c_max_b, grid_b = box_b

  if is_empty_box(box_a):
    # Adopting the input as the accumulator: copy, because `box_b` is shared.
    return DenseBoundingBox(r_min_b, r_max_b, c_min_b, c_max_b, grid_b.copy())
  if is_empty_box(box_b):
    return box_a

  r_min = min(r_min_a, r_min_b)
  r_max = max(r_max_a, r_max_b)
  c_min = min(c_min_a, c_min_b)
  c_max = max(c_max_a, c_max_b)

  if (r_min, r_max, c_min, c_max) == (r_min_a, r_max_a, c_min_a, c_max_a):
    # Hot path once the accumulator extent has converged: accumulate in place.
    grid_a[
        r_min_b - r_min : r_max_b - r_min, c_min_b - c_min : c_max_b - c_min
    ] += grid_b
    return DenseBoundingBox(r_min, r_max, c_min, c_max, grid_a)
  elif (r_min, r_max, c_min, c_max) == (r_min_b, r_max_b, c_min_b, c_max_b):
    # The union is the input's extent, and the input is read-only, so a new
    # buffer is required.
    grid_out = grid_b.copy()
    grid_out[
        r_min_a - r_min : r_max_a - r_min, c_min_a - c_min : c_max_a - c_min
    ] += grid_a
    return DenseBoundingBox(r_min, r_max, c_min, c_max, grid_out)
  else:
    merged = np.zeros((r_max - r_min, c_max - c_min), dtype=np.float32)
    merged[
        r_min_a - r_min : r_max_a - r_min, c_min_a - c_min : c_max_a - c_min
    ] = grid_a
    merged[
        r_min_b - r_min : r_max_b - r_min, c_min_b - c_min : c_max_b - c_min
    ] += grid_b
    return DenseBoundingBox(r_min, r_max, c_min, c_max, merged)


def sparse_to_dense_box(
    nonzero_rows: np.ndarray,
    nonzero_cols: np.ndarray,
    nonzero_values: np.ndarray,
) -> DenseBoundingBox:
  """Converts sparse coordinates to a tight dense bounding box."""
  if nonzero_values.size == 0:
    return DenseBoundingBox(0, 0, 0, 0, np.zeros((0, 0), dtype=np.float32))
  r_min = int(nonzero_rows.min())
  r_max = int(nonzero_rows.max()) + 1
  c_min = int(nonzero_cols.min())
  c_max = int(nonzero_cols.max()) + 1
  sub_grid = np.zeros((r_max - r_min, c_max - c_min), dtype=np.float32)
  np.add.at(
      sub_grid, (nonzero_rows - r_min, nonzero_cols - c_min), nonzero_values
  )
  return DenseBoundingBox(r_min, r_max, c_min, c_max, sub_grid)


def merge_sparse_into_box(
    box: DenseBoundingBox,
    *,
    nonzero_rows: np.ndarray,
    nonzero_cols: np.ndarray,
    nonzero_values: np.ndarray,
) -> DenseBoundingBox:
  """Merges sparse points directly into a dense bounding box.

  Ownership contract: `box` is the ACCUMULATOR and is mutated in place (see
  `merge_grid_boxes` for why). `nonzero_rows` / `nonzero_cols` /
  `nonzero_values` belong to the input element, are shared with up to two other
  keys, and are read-only.

  Args:
    box: The accumulator box. May be mutated in place.
    nonzero_rows: Row indices of the input points. Read-only.
    nonzero_cols: Column indices of the input points. Read-only.
    nonzero_values: Values of the input points. Read-only.

  Returns:
    A DenseBoundingBox covering the union of the box and the points.
  """
  if nonzero_values.size == 0:
    return box
  if is_empty_box(box):
    return sparse_to_dense_box(nonzero_rows, nonzero_cols, nonzero_values)
  r_min_b, r_max_b, c_min_b, c_max_b, grid_b = box

  pts_r_min = int(nonzero_rows.min())
  pts_r_max = int(nonzero_rows.max()) + 1
  pts_c_min = int(nonzero_cols.min())
  pts_c_max = int(nonzero_cols.max()) + 1

  if (
      pts_r_min >= r_min_b
      and pts_r_max <= r_max_b
      and pts_c_min >= c_min_b
      and pts_c_max <= c_max_b
  ):
    # Hot path: the points already fit inside the accumulator extent, so scatter
    # straight into the accumulator's own buffer.
    np.add.at(
        grid_b, (nonzero_rows - r_min_b, nonzero_cols - c_min_b), nonzero_values
    )
    return box

  r_min = min(r_min_b, pts_r_min)
  r_max = max(r_max_b, pts_r_max)
  c_min = min(c_min_b, pts_c_min)
  c_max = max(c_max_b, pts_c_max)

  expanded = np.zeros((r_max - r_min, c_max - c_min), dtype=np.float32)
  expanded[
      r_min_b - r_min : r_max_b - r_min, c_min_b - c_min : c_max_b - c_min
  ] = grid_b
  np.add.at(
      expanded, (nonzero_rows - r_min, nonzero_cols - c_min), nonzero_values
  )
  return DenseBoundingBox(r_min, r_max, c_min, c_max, expanded)


# Minimum total sparse coordinates before geometric 1.5x compaction triggers.
MIN_SPARSE_COMPACT_POINTS = 200_000

# Maximum uncompacted point growth (`total_count - last_compact_len`) allowed
# before forcing a compaction pass. Importantly, this caps the *uncompacted
# delta* (~4 MB of appended coordinate buffers) rather than `total_count`.
MAX_UNCOMPACTED_SPARSE_DELTA = 500_000

# Maximum unique sparse points before promoting a compacted
# `SparseAccumulator` to a `DenseBoundingBox` or scattering directly into a
# `DenseBoundingBox` in `merge_accumulators`.
MAX_SPARSE_ACCUMULATOR_POINTS = 1_500_000


def compact_and_maybe_promote_sparse(
    *,
    rows: np.ndarray,
    cols: np.ndarray,
    values: np.ndarray,
    max_sparse_points: int | None = None,
) -> SparseAccumulator | DenseBoundingBox:
  """Compacts duplicate `(row, col)` cells and promotes to dense if needed."""
  # Any stride above the largest column index keeps (row, col) pairs distinct.
  stride = grid_rasterizer.MAX_GRID_DIMENSION + 1
  flat = rows.astype(np.int64) * stride + cols.astype(np.int64)
  unique_flat, inverse_indices = np.unique(flat, return_inverse=True)
  unique_values = np.bincount(inverse_indices, weights=values).astype(
      np.float32
  )
  unique_rows = (unique_flat // stride).astype(np.int16)
  unique_cols = (unique_flat % stride).astype(np.int16)
  if max_sparse_points is not None and len(unique_values) > max_sparse_points:
    return sparse_to_dense_box(unique_rows, unique_cols, unique_values)
  return SparseAccumulator(
      constants.GRID_ACC_SPARSE,
      unique_rows,
      unique_cols,
      unique_values,
      len(unique_values),
  )


class _GridExtent(NamedTuple):
  """Row and column half-open bounds `[r_min, r_max) x [c_min, c_max)`."""

  r_min: int
  r_max: int
  c_min: int
  c_max: int


def _union_extent(
    sparse_accumulators: Sequence[SparseAccumulator],
    dense_accumulators: Sequence[DenseBoundingBox],
) -> _GridExtent | None:
  """Returns the bounding extent of all accumulators, or `None` if empty.

  Args:
    sparse_accumulators: Sparse accumulators; each must hold at least one point.
    dense_accumulators: Dense bounding boxes.

  Returns:
    The smallest `_GridExtent` covering every accumulator, or `None` when both
    sequences are empty.
  """
  extents = [
      _GridExtent(
          r_min=int(sparse.rows.min()),
          r_max=int(sparse.rows.max()) + 1,
          c_min=int(sparse.cols.min()),
          c_max=int(sparse.cols.max()) + 1,
      )
      for sparse in sparse_accumulators
  ]
  extents.extend(
      _GridExtent(
          r_min=box.r_min,
          r_max=box.r_max,
          c_min=box.c_min,
          c_max=box.c_max,
      )
      for box in dense_accumulators
  )
  if not extents:
    return None

  return _GridExtent(
      r_min=min(extent.r_min for extent in extents),
      r_max=max(extent.r_max for extent in extents),
      c_min=min(extent.c_min for extent in extents),
      c_max=max(extent.c_max for extent in extents),
  )


def merge_to_dense_box(
    sparse_accumulators: Sequence[SparseAccumulator],
    dense_accumulators: Sequence[DenseBoundingBox],
) -> DenseBoundingBox | None:
  """Merges sparse and dense accumulators into a box, or `None` if empty."""
  extent = _union_extent(sparse_accumulators, dense_accumulators)
  if extent is None:
    return None
  r_min, r_max, c_min, c_max = extent

  grid = np.zeros((r_max - r_min, c_max - c_min), dtype=np.float32)
  for dense_box in dense_accumulators:
    grid[
        dense_box.r_min - r_min : dense_box.r_max - r_min,
        dense_box.c_min - c_min : dense_box.c_max - c_min,
    ] += dense_box.sub_grid.astype(np.float32, copy=False)
  for sparse in sparse_accumulators:
    np.add.at(
        grid,
        (sparse.rows - r_min, sparse.cols - c_min),
        sparse.values.astype(np.float32, copy=False),
    )
  return DenseBoundingBox(r_min, r_max, c_min, c_max, grid)


def adopt_sparse_accumulator(
    sparse: SparseAccumulator,
    *,
    max_sparse_points: int | None = None,
) -> SparseAccumulator | DenseBoundingBox:
  """Returns an owned copy of `sparse`, compacting/promoting if above limit.

  Args:
    sparse: Input sparse accumulator whose buffers may be shared across keys.
    max_sparse_points: Maximum unique points before promoting the compacted
      result to a `DenseBoundingBox`; `None` disables promotion.

  Returns:
    An owned `SparseAccumulator` or `DenseBoundingBox`.
  """
  if max_sparse_points is not None and len(sparse.values) > max_sparse_points:
    return compact_and_maybe_promote_sparse(
        rows=sparse.rows,
        cols=sparse.cols,
        values=sparse.values,
        max_sparse_points=max_sparse_points,
    )
  return SparseAccumulator(
      constants.GRID_ACC_SPARSE,
      sparse.rows.copy(),
      sparse.cols.copy(),
      sparse.values.astype(np.float32),
      len(sparse.values),
  )


def merge_sparse_accumulators(
    sparse_a: SparseAccumulator,
    sparse_b: SparseAccumulator,
    *,
    max_uncompacted_delta: int = MAX_UNCOMPACTED_SPARSE_DELTA,
    max_sparse_points: int | None = None,
) -> SparseAccumulator | DenseBoundingBox:
  """Merges two sparse point sets, compacting when enough points accumulated.

  Ownership contract: `sparse_a` is the ACCUMULATOR, `sparse_b` wraps the INPUT
  element's arrays. When the accumulator is empty the input is adopted, and that
  adoption must copy.

  Args:
    sparse_a: The accumulator. Returned as-is when the input is empty.
    sparse_b: The input. Copied if adopted; otherwise read-only.
    max_uncompacted_delta: Maximum uncompacted coordinate growth (`total_count -
      last_compact_len`) before forcing a compaction pass.
    max_sparse_points: Maximum unique points before promoting the compacted
      result to a `DenseBoundingBox`; `None` disables promotion.

  Returns:
    A `SparseAccumulator` or `DenseBoundingBox` holding the union of both point
    sets.
  """
  if sparse_a.values.size == 0:
    return adopt_sparse_accumulator(
        sparse_b, max_sparse_points=max_sparse_points
    )
  if sparse_b.values.size == 0:
    return sparse_a

  concat_rows = np.concatenate([sparse_a.rows, sparse_b.rows])
  concat_cols = np.concatenate([sparse_a.cols, sparse_b.cols])
  concat_values = np.concatenate([sparse_a.values, sparse_b.values])
  total_count = len(concat_values)

  last_compact_len = sparse_a.last_compact_len
  if max_sparse_points is not None and last_compact_len > max_sparse_points:
    return sparse_to_dense_box(concat_rows, concat_cols, concat_values)

  should_compact = (
      total_count > MIN_SPARSE_COMPACT_POINTS
      and total_count >= int(1.5 * last_compact_len)
  ) or (total_count - last_compact_len >= max_uncompacted_delta)

  if should_compact:
    return compact_and_maybe_promote_sparse(
        rows=concat_rows,
        cols=concat_cols,
        values=concat_values,
        max_sparse_points=max_sparse_points,
    )

  return SparseAccumulator(
      constants.GRID_ACC_SPARSE,
      concat_rows,
      concat_cols,
      concat_values,
      last_compact_len,
  )


SPARSE_BYTES_PER_CELL = 8
DENSE_BYTES_PER_CELL = 4


def empty_sparse_accumulator() -> SparseAccumulator:
  """Returns an empty `SparseAccumulator` with zero-length typed arrays."""
  return SparseAccumulator(
      constants.GRID_ACC_SPARSE,
      np.empty(0, dtype=np.int16),
      np.empty(0, dtype=np.int16),
      np.empty(0, dtype=np.float32),
      0,
  )


def to_compact_grid_accumulator(
    accumulator: GridPayload | None,
) -> GridAccumulator:
  """Re-encodes a grid accumulator in its smallest shuffle-safe form.

  Used as the `extract_output` of the Stage-1 `(grid_type, month, hour)`
  combine.

  Args:
    accumulator: Any accumulator produced by `SumDenseGridCombineFn`, or `None`.

  Returns:
    A `SparseAccumulator` or `DenseBoundingBox`, or `None` when `accumulator`
    is `None`.

  Raises:
    TypeError: If `accumulator` is not a recognized accumulator encoding.
  """
  box = as_grid_acc(accumulator)
  if box is None:
    return None

  if isinstance(box, SparseAccumulator):
    if box.values.size == 0:
      return empty_sparse_accumulator()
    return compact_and_maybe_promote_sparse(
        rows=box.rows,
        cols=box.cols,
        values=np.asarray(box.values, dtype=np.float32),
    )

  if is_empty_box(box):
    return empty_sparse_accumulator()

  sub_grid = box.sub_grid
  nnz = int(np.count_nonzero(sub_grid))
  if nnz * SPARSE_BYTES_PER_CELL >= sub_grid.size * DENSE_BYTES_PER_CELL:
    # More than half the bounding box is populated: the dense box is smaller.
    return DenseBoundingBox(
        box.r_min,
        box.r_max,
        box.c_min,
        box.c_max,
        sub_grid.astype(np.float32, copy=False),
    )
  if nnz == 0:
    return empty_sparse_accumulator()

  nonzero_rows, nonzero_cols = np.nonzero(sub_grid)
  return SparseAccumulator(
      constants.GRID_ACC_SPARSE,
      (nonzero_rows + box.r_min).astype(np.int16),
      (nonzero_cols + box.c_min).astype(np.int16),
      sub_grid[nonzero_rows, nonzero_cols].astype(np.float32, copy=False),
      nnz,
  )
