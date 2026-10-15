"""Beam transforms, DoFns, and CombineFns for contrail processing."""

from collections.abc import Iterable, Iterator, Mapping, Sequence
import datetime
import types
from typing import Any, Final

from absl import logging
import apache_beam as beam
from apache_beam.pvalue import TaggedOutput
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from common import artifact_io
from common import constants
from common import fs
from data_extraction import derive
from data_extraction import grid_accumulator
from data_extraction import grid_publish
from data_extraction import grid_rasterizer
from data_extraction import intermediate_io
from data_extraction import shard_processor


class SplitParquetShardDoFn(beam.DoFn):
  """Splits Parquet files into bounded row groups or row slices."""

  def __init__(self, max_rows_per_chunk: int):
    """Initializes the splitter.

    Args:
      max_rows_per_chunk: Maximum number of rows per emitted chunk. Values <= 0
        disable splitting, so each file is emitted whole.
    """
    super().__init__()
    self._max_rows_per_chunk = max_rows_per_chunk

  def process(
      self, parquet_path: str
  ) -> Iterable[tuple[str, int | None, int, int | None]]:
    """Inspects Parquet metadata and yields sub-shard chunk descriptors.

    Args:
      parquet_path: Path to the input Parquet file.

    Yields:
      Tuples of `(path, row_group_idx, row_offset, num_rows)`, where
      `row_group_idx` is `None` when the file is emitted as a single whole-file
      chunk or on metadata-inspection fallback, and `num_rows` is `total_rows`
      for a planned whole-file chunk and `None` only on metadata fallback.
    """
    # Plan all chunks before yielding anything: if the metadata read fails
    # halfway, emitting the chunks seen so far plus the whole-file fallback
    # would count those rows twice.
    try:
      chunks = self._plan_chunks(parquet_path)
    except (
        OSError,
        fs.Error,
        pa.ArrowException,
        ValueError,
    ) as e:
      logging.warning(
          "Failed to inspect Parquet metadata for %s: %s. Emitting whole file.",
          parquet_path,
          e,
          exc_info=True,
      )
      chunks = [(parquet_path, None, 0, None)]
    yield from chunks

  def _plan_chunks(
      self, parquet_path: str
  ) -> list[tuple[str, int | None, int, int | None]]:
    """Returns `(path, row_group_idx, row_offset, num_rows)` chunks.

    `row_group_idx` is `None` when the whole file fits in a single chunk, and
    the returned list is empty when the file has 0 rows or 0 row groups.

    Args:
      parquet_path: Path to the Parquet file to plan chunks for.

    Returns:
      List of (path, row_group_idx, row_offset, num_rows) tuple chunks.
    """
    with fs.open_file(parquet_path, "rb") as f:
      meta = pq.ParquetFile(f).metadata
      num_row_groups = meta.num_row_groups
      total_rows = meta.num_rows

      if num_row_groups == 0 or total_rows == 0:
        return []

      # If file is already small enough or splitting disabled, emit it as a
      # single unit.
      if (
          self._max_rows_per_chunk <= 0
          or total_rows <= self._max_rows_per_chunk
      ):
        return [(parquet_path, None, 0, total_rows)]

      if num_row_groups == 1:
        # Single monolithic row group with > max_rows_per_chunk
        return _row_slices(
            parquet_path,
            row_group_index=0,
            num_rows=total_rows,
            max_rows_per_chunk=self._max_rows_per_chunk,
        )

      # If file has multiple row groups, inspect each row group
      chunks = []
      for row_group_index in range(num_row_groups):
        row_group_rows = meta.row_group(row_group_index).num_rows
        if row_group_rows <= self._max_rows_per_chunk:
          chunks.append((parquet_path, row_group_index, 0, row_group_rows))
        else:
          chunks.extend(
              _row_slices(
                  parquet_path,
                  row_group_index=row_group_index,
                  num_rows=row_group_rows,
                  max_rows_per_chunk=self._max_rows_per_chunk,
              )
          )
      return chunks


def _row_slices(
    parquet_path: str,
    *,
    row_group_index: int,
    num_rows: int,
    max_rows_per_chunk: int,
) -> list[tuple[str, int | None, int, int | None]]:
  """Splits one row group into `(path, row_group, offset, length)` slices.

  Args:
    parquet_path: Path to the input Parquet file.
    row_group_index: Index of the row group to split.
    num_rows: Number of rows in the row group.
    max_rows_per_chunk: Maximum number of rows per slice; must be positive.

  Returns:
    Consecutive slices of at most `max_rows_per_chunk` rows covering the group.
  """
  return [
      (
          parquet_path,
          row_group_index,
          offset,
          min(max_rows_per_chunk, num_rows - offset),
      )
      for offset in range(0, num_rows, max_rows_per_chunk)
  ]


class ProcessParquetShardDoFn(beam.DoFn):
  """Reads a Parquet file shard and performs spatial attribution and binning.

  Attributes:
    invalid_coords_counter: Beam counter of segments dropped for invalid
      coordinates (`invalid_coordinates_dropped`).
    invalid_times_counter: Beam counter of segments dropped for invalid
      timestamps (`invalid_timestamps_dropped`).
    zero_len_counter: Beam counter of zero-length segments dropped
      (`zero_length_segments_dropped`).
    outside_year_counter: Beam counter of segments outside `--input_year`
      (`segments_outside_input_year`).
    failed_shards_counter: Beam counter of shards that could not be processed,
      summed across all failure outcomes (`shards_failed`).
  """

  OUTPUT_TAG_COUNTRY = "country_records"
  OUTPUT_TAG_FIR = "fir_records"
  OUTPUT_TAG_GRID = "grid_slices"
  # Dead letters: (shard_input, outcome value) for every shard that could not
  # be processed. Without this a lost shard just shrinks the output.
  OUTPUT_TAG_FAILED = "failed_shards"

  # Counter names keyed by the outcome they describe. Every shard increments
  # exactly one of these, so `sum(_OUTCOME_COUNTER_NAMES.values())` over a run
  # must equal the number of shards the splitter emitted. This is a class
  # attribute, so pickling a DoFn instance references it through the class
  # rather than serializing the (unpicklable) `MappingProxyType`.
  _OUTCOME_COUNTER_NAMES: Final[Mapping[shard_processor.ShardOutcome, str]] = (
      types.MappingProxyType({
          shard_processor.ShardOutcome.OK: "shards_succeeded",
          shard_processor.ShardOutcome.MALFORMED_INPUT: (
              "shards_malformed_input"
          ),
          shard_processor.ShardOutcome.READ_FAILED: "shards_read_failed",
          shard_processor.ShardOutcome.EMPTY_INPUT: "shards_empty",
          shard_processor.ShardOutcome.MISSING_COLUMNS: (
              "shards_missing_columns"
          ),
          shard_processor.ShardOutcome.NO_VALID_SEGMENTS: (
              "shards_no_valid_segments"
          ),
          shard_processor.ShardOutcome.EXCEPTION: "shards_exception",
      })
  )

  def __init__(
      self,
      *,
      airports_path: str,
      firs_path: str,
      grid_config: grid_rasterizer.GridConfig,
      country_geojson_path: str | None = None,
      worker_context: shard_processor.ProcessWorkerContext | None = None,
      input_year: int | None = None,
  ):
    """Initializes the shard processor DoFn and Beam counters."""
    super().__init__()
    self._airports_path = airports_path
    self._firs_path = firs_path
    self._country_geojson_path = country_geojson_path
    self._grid_config = grid_config
    self._worker_context = worker_context
    self._input_year = int(input_year) if input_year is not None else None
    self.invalid_coords_counter = beam.metrics.Metrics.counter(
        "data_extraction", "invalid_coordinates_dropped"
    )
    self.invalid_times_counter = beam.metrics.Metrics.counter(
        "data_extraction", "invalid_timestamps_dropped"
    )
    self.zero_len_counter = beam.metrics.Metrics.counter(
        "data_extraction", "zero_length_segments_dropped"
    )
    self.outside_year_counter = beam.metrics.Metrics.counter(
        "data_extraction", "segments_outside_input_year"
    )
    self._outcome_counters = {
        outcome: beam.metrics.Metrics.counter("data_extraction", name)
        for outcome, name in self._OUTCOME_COUNTER_NAMES.items()
    }
    # Rolled up so an operator does not have to add four counters together to
    # answer "did this run lose data?".
    self.failed_shards_counter = beam.metrics.Metrics.counter(
        "data_extraction", "shards_failed"
    )

  def setup(self) -> None:
    """Initializes spatial indexes once per worker process."""
    if self._worker_context is not None:
      return

    self._worker_context = shard_processor.acquire_process_worker_context(
        airports_path=self._airports_path,
        firs_path=self._firs_path,
        country_geojson_path=self._country_geojson_path,
    )

  def process(self, shard_input: Any) -> Iterable[TaggedOutput]:
    """Processes a single parquet shard or sub-shard chunk."""
    # Only the computation is guarded. Wrapping the yield loops too would let
    # an error raised by a downstream consumer be reported as a shard failure,
    # and would silently truncate a partially emitted shard.
    result = self._compute_shard_result(shard_input)
    self._record_shard_counters(result)

    if result.is_failure:
      self.failed_shards_counter.inc()
      yield TaggedOutput(
          self.OUTPUT_TAG_FAILED, (shard_input, result.outcome.value)
      )
      return

    for item in result.grid_slices:
      yield TaggedOutput(self.OUTPUT_TAG_GRID, item)
    for item in result.country_records:
      yield TaggedOutput(self.OUTPUT_TAG_COUNTRY, item)
    for item in result.fir_records:
      yield TaggedOutput(self.OUTPUT_TAG_FIR, item)

  def _compute_shard_result(
      self, shard_input: Any
  ) -> shard_processor.ShardProcessingResult:
    """Runs shard processing, mapping recoverable errors to `EXCEPTION`.

    Args:
      shard_input: The shard or sub-shard chunk descriptor to process.

    Returns:
      The processing result, or an empty `EXCEPTION` result if processing
      raised a recoverable I/O, Arrow, type, or value error.

    Raises:
      RuntimeError: If `setup()` fails to initialize `_worker_context`.
    """
    if self._worker_context is None:
      self.setup()
    if self._worker_context is None:
      raise RuntimeError("Worker context was not initialized by setup().")

    try:
      return shard_processor.process_shard(
          shard_input=shard_input,
          context=self._worker_context,
          grid_config=self._grid_config,
          expected_year=self._input_year,
      )
    except (
        OSError,
        fs.Error,
        pa.ArrowException,
        TypeError,
        ValueError,
    ) as e:
      logging.error(
          "Error during segment attribution/rasterization for shard %s: %s",
          shard_input,
          e,
          exc_info=True,
      )
      return shard_processor.empty_shard_result(
          shard_processor.ShardOutcome.EXCEPTION
      )

  def _record_shard_counters(
      self, result: shard_processor.ShardProcessingResult
  ) -> None:
    """Increments the outcome counter and the dropped-segment counters."""
    self._outcome_counters[result.outcome].inc()

    if result.invalid_coords_count:
      self.invalid_coords_counter.inc(result.invalid_coords_count)
    if result.invalid_times_count:
      self.invalid_times_counter.inc(result.invalid_times_count)
    if result.zero_len_count:
      self.zero_len_counter.inc(result.zero_len_count)
    if result.outside_year_count:
      self.outside_year_counter.inc(result.outside_year_count)


class SumTupleCombineFn(beam.CombineFn):
  """Element-wise sum of fixed-length numeric records.

  Combines both record kinds `shard_processor.process_shard` emits: 8 values
  per country key and 5 per FIR key (see `shard_processor.format_*_records`
  for the field order). Every field is a plain sum, and integer counts stay
  integers because `int + int` is an `int`.
  """

  def create_accumulator(self) -> tuple[int | float, ...] | None:
    """Returns `None` as the empty initial accumulator."""
    return None

  def add_input(
      self,
      accumulator: tuple[int | float, ...] | None,
      value: Sequence[int | float],
  ) -> tuple[int | float, ...]:
    """Adds a numeric tuple element-wise into `accumulator`."""
    if accumulator is None:
      return tuple(value)
    return tuple(
        left + right for left, right in zip(accumulator, value, strict=True)
    )

  def merge_accumulators(
      self, accumulators: Iterable[tuple[int | float, ...] | None]
  ) -> tuple[int | float, ...] | None:
    """Sums non-`None` accumulators element-wise, or returns `None` if empty."""
    merged = None
    for accumulator in accumulators:
      if accumulator is not None:
        merged = self.add_input(merged, accumulator)
    return merged

  def extract_output(
      self, accumulator: tuple[int | float, ...] | None
  ) -> tuple[int | float, ...] | None:
    """Returns the summed tuple, or `None` if no values were accumulated."""
    return accumulator


def _copy_dense_box(
    value: grid_accumulator.DenseBoundingBox,
) -> grid_accumulator.DenseBoundingBox:
  """Returns an owned `float32` copy of a dense bounding-box payload."""
  return grid_accumulator.DenseBoundingBox(
      value.r_min,
      value.r_max,
      value.c_min,
      value.c_max,
      value.sub_grid.astype(np.float32),
  )


def _materialize_box_to_grid(
    box: grid_accumulator.DenseBoundingBox,
    *,
    grid_height: int,
    grid_width: int,
) -> np.ndarray:
  """Clips and copies `box` into a `(grid_height, grid_width)` float32 grid."""
  if grid_accumulator.is_empty_box(box):
    return np.zeros((grid_height, grid_width), dtype=np.float32)
  r_min, r_max, c_min, c_max, sub_grid = box
  if sub_grid.shape == (grid_height, grid_width) and r_min == 0 and c_min == 0:
    return sub_grid.astype(np.float32, copy=True)
  full_grid = np.zeros((grid_height, grid_width), dtype=np.float32)
  r_start = max(0, r_min)
  r_end = min(grid_height, r_max)
  c_start = max(0, c_min)
  c_end = min(grid_width, c_max)
  if r_start < r_end and c_start < c_end:
    full_grid[r_start:r_end, c_start:c_end] = sub_grid[
        r_start - r_min : r_end - r_min,
        c_start - c_min : c_end - c_min,
    ]
  return full_grid


class SumDenseGridCombineFn(beam.CombineFn):
  """Sums sparse and dense grid slices element-wise for spatial grids.

  In `combine_grid_slices_to_export_keys` this is the Stage-2 combiner (keyed
  by the 74 export keys) and outputs the final `(height, width)` float32 grid.
  Stage 1 uses `SumGridSlicesToAccumulatorCombineFn`, which delegates to this
  class but emits a compact accumulator instead. Stage 2 can consume those
  directly because `add_input` accepts every `GridPayload`.
  """

  def __init__(
      self,
      *,
      height: int | None = None,
      width: int | None = None,
      max_sparse_points: int | None = (
          grid_accumulator.MAX_SPARSE_ACCUMULATOR_POINTS
      ),
      max_uncompacted_delta: int = (
          grid_accumulator.MAX_UNCOMPACTED_SPARSE_DELTA
      ),
  ):
    """Initializes the dense grid combiner with target dimensions and limits."""
    super().__init__()
    self._height = height
    self._width = width
    self._max_sparse_points = max_sparse_points
    self._max_uncompacted_delta = max_uncompacted_delta

  def create_accumulator(self) -> grid_accumulator.GridAccumulator:
    """Returns `None` as the empty initial grid accumulator."""
    return None

  def compact(
      self, accumulator: grid_accumulator.GridAccumulator
  ) -> grid_accumulator.GridAccumulator:
    """Compacts uncompacted sparse additions and demotes low-density boxes."""
    accumulator = grid_accumulator.as_grid_acc(accumulator)
    if isinstance(accumulator, grid_accumulator.SparseAccumulator):
      if len(accumulator.values) <= accumulator.last_compact_len:
        return accumulator
      accumulator = grid_accumulator.compact_and_maybe_promote_sparse(
          rows=accumulator.rows,
          cols=accumulator.cols,
          values=accumulator.values,
          max_sparse_points=self._max_sparse_points,
      )
    if isinstance(accumulator, grid_accumulator.DenseBoundingBox):
      return grid_accumulator.to_compact_grid_accumulator(accumulator)
    return accumulator

  def add_input(
      self,
      accumulator: grid_accumulator.GridAccumulator,
      value: grid_accumulator.GridPayload | None,
  ) -> grid_accumulator.GridAccumulator:
    """Merges one sparse or dense grid payload into `accumulator`."""
    accumulator = grid_accumulator.as_grid_acc(accumulator)
    value = grid_accumulator.as_grid_acc(value)
    if value is None:
      return accumulator

    if accumulator is None:
      # Adopt a copy: the input is shared with other export keys.
      if isinstance(value, grid_accumulator.SparseAccumulator):
        return grid_accumulator.adopt_sparse_accumulator(
            value, max_sparse_points=self._max_sparse_points
        )
      return _copy_dense_box(value)

    if isinstance(accumulator, grid_accumulator.SparseAccumulator):
      if isinstance(value, grid_accumulator.SparseAccumulator):
        return grid_accumulator.merge_sparse_accumulators(
            accumulator,
            value,
            max_uncompacted_delta=self._max_uncompacted_delta,
            max_sparse_points=self._max_sparse_points,
        )
      if accumulator.values.size == 0:
        return _copy_dense_box(value)
      accumulator = grid_accumulator.sparse_to_dense_box(
          accumulator.rows, accumulator.cols, accumulator.values
      )

    if isinstance(value, grid_accumulator.SparseAccumulator):
      return grid_accumulator.merge_sparse_into_box(
          accumulator,
          nonzero_rows=value.rows,
          nonzero_cols=value.cols,
          nonzero_values=value.values,
      )
    return grid_accumulator.merge_grid_boxes(accumulator, value)

  def _partition_accumulators(
      self, accumulators: Iterable[grid_accumulator.GridAccumulator]
  ) -> tuple[
      list[grid_accumulator.SparseAccumulator],
      list[grid_accumulator.DenseBoundingBox],
  ]:
    """Partitions raw accumulators into sparse and dense accumulators."""
    sparse: list[grid_accumulator.SparseAccumulator] = []
    dense: list[grid_accumulator.DenseBoundingBox] = []
    for raw_accumulator in accumulators:
      accumulator = grid_accumulator.as_grid_acc(raw_accumulator)
      if isinstance(accumulator, grid_accumulator.SparseAccumulator):
        if accumulator.values.size > 0:
          sparse.append(accumulator)
      elif isinstance(
          accumulator, grid_accumulator.DenseBoundingBox
      ) and not grid_accumulator.is_empty_box(accumulator):
        dense.append(accumulator)
    return sparse, dense

  def merge_accumulators(
      self, accumulators: Iterable[grid_accumulator.GridAccumulator]
  ) -> grid_accumulator.GridAccumulator:
    """Combines sparse and dense accumulators, or returns `None` if empty."""
    sparse_accumulators, dense_accumulators = self._partition_accumulators(
        accumulators
    )
    if not sparse_accumulators and not dense_accumulators:
      return None

    # Single accumulator (`len(sparse_accumulators) + len(dense_accumulators) ==
    # 1`), which Beam's map-side `CombineValues/Partial` calls when finalizing a
    # map bundle before writing to shuffle: compact any uncompacted delta and
    # demote `<50%`-occupancy dense boxes so minimal bytes cross shuffle.
    if len(sparse_accumulators) + len(dense_accumulators) == 1:
      single_acc = (
          sparse_accumulators[0]
          if sparse_accumulators
          else dense_accumulators[0]
      )
      return self.compact(single_acc)

    # Multi-accumulator reduction (`len(sparse_accumulators) > 1` or
    # `dense_accumulators` non-empty): if any dense accumulator is present or
    # total sparse points exceed `self._max_sparse_points`, scatter directly
    # into a single union `DenseBoundingBox` via `np.add.at`
    # (`merge_to_dense_box`), skipping `np.concatenate` and `np.unique` sorting.
    total_count = sum(
        len(accumulator.values) for accumulator in sparse_accumulators
    )
    if dense_accumulators or (
        self._max_sparse_points is not None
        and total_count > self._max_sparse_points
    ):
      return grid_accumulator.merge_to_dense_box(
          sparse_accumulators, dense_accumulators
      )

    concat_rows = np.concatenate(
        [accumulator.rows for accumulator in sparse_accumulators]
    )
    concat_cols = np.concatenate(
        [accumulator.cols for accumulator in sparse_accumulators]
    )
    concat_values = np.concatenate(
        [accumulator.values for accumulator in sparse_accumulators]
    )
    return grid_accumulator.compact_and_maybe_promote_sparse(
        rows=concat_rows,
        cols=concat_cols,
        values=concat_values,
        max_sparse_points=self._max_sparse_points,
    )

  def extract_output(
      self, accumulator: grid_accumulator.GridAccumulator
  ) -> np.ndarray:
    """Materializes the final 2D `float32` grid from `accumulator`."""
    accumulator = grid_accumulator.as_grid_acc(accumulator)
    if accumulator is None:
      grid_height = self._height if self._height is not None else 1
      grid_width = self._width if self._width is not None else 1
      return np.zeros((grid_height, grid_width), dtype=np.float32)

    if isinstance(accumulator, grid_accumulator.SparseAccumulator):
      box = grid_accumulator.sparse_to_dense_box(
          accumulator.rows, accumulator.cols, accumulator.values
      )
    else:
      box = accumulator

    grid_height = (
        self._height if self._height is not None else max(1, box.r_max)
    )
    grid_width = self._width if self._width is not None else max(1, box.c_max)
    return _materialize_box_to_grid(
        box, grid_height=grid_height, grid_width=grid_width
    )


class SumGridSlicesToAccumulatorCombineFn(beam.CombineFn):
  """Stage-1 grid combiner: sums like `SumDenseGridCombineFn`, outputs compact.

  Used for the `(grid_type, month, hour)` combine in
  `combine_grid_slices_to_export_keys`. Its output is fanned out 3x and
  re-shuffled into the Stage-2 export-key combine, so it must NEVER materialize
  a full `(height, width)` grid (97.9 MB at 7200x3400). `extract_output`
  therefore returns `to_compact_grid_accumulator(acc)`, which Stage-2
  `add_input` accepts.

  All accumulation is delegated to a wrapped `SumDenseGridCombineFn`, so
  compaction thresholds, dense promotion, and the read-only input ownership
  contract are identical in both stages. Composition (rather than subclassing)
  keeps `SumDenseGridCombineFn.extract_output` typed as `np.ndarray`.
  """

  def __init__(
      self,
      max_sparse_points: int | None = (
          grid_accumulator.MAX_SPARSE_ACCUMULATOR_POINTS
      ),
      max_uncompacted_delta: int = (
          grid_accumulator.MAX_UNCOMPACTED_SPARSE_DELTA
      ),
  ):
    """Initializes the Stage-1 combiner with sparse compaction limits."""
    super().__init__()
    self._inner = SumDenseGridCombineFn(
        max_sparse_points=max_sparse_points,
        max_uncompacted_delta=max_uncompacted_delta,
    )

  def create_accumulator(self) -> grid_accumulator.GridAccumulator:
    """Returns `None` as the empty initial Stage-1 accumulator."""
    return self._inner.create_accumulator()

  def add_input(
      self,
      accumulator: grid_accumulator.GridAccumulator,
      value: grid_accumulator.GridPayload | None,
  ) -> grid_accumulator.GridAccumulator:
    """Merges one grid slice payload into `accumulator`."""
    return self._inner.add_input(accumulator, value)

  def merge_accumulators(
      self, accumulators: Iterable[grid_accumulator.GridAccumulator]
  ) -> grid_accumulator.GridAccumulator:
    """Merges Stage-1 grid accumulators, or returns `None` if all empty."""
    return self._inner.merge_accumulators(accumulators)

  def compact(
      self, accumulator: grid_accumulator.GridAccumulator
  ) -> grid_accumulator.GridAccumulator:
    """Compacts uncompacted sparse additions before Stage-1 shuffle."""
    return self._inner.compact(accumulator)

  def extract_output(
      self, accumulator: grid_accumulator.GridAccumulator
  ) -> grid_accumulator.GridAccumulator:
    """Returns a compact `GridAccumulator` suitable for 3x export fanout."""
    return grid_accumulator.to_compact_grid_accumulator(accumulator)


class NormalizeGridMonthHourKeyDoFn(beam.DoFn):
  """Rekeys `(grid_type, YYYYMM|MM, hour)` to `(grid_type, MM, hour)`.

  This MUST run before the Stage-1 `(month, hour)` combine: without it,
  slices keyed `202503` and `3` would land in different Stage-1 groups and
  the year filter would only apply after the expensive combine. The payload
  is forwarded untouched (no copy) - one output per input, so there is no
  aliasing concern here.

  Attributes:
    outside_year_slices_counter: Beam counter of slices dropped because their
      `YYYYMM` key falls outside the expected input year.
    invalid_bucket_slices_counter: Beam counter of slices dropped because their
      month or hour bucket is out of range.
  """

  def __init__(self, expected_year: int | None = None):
    """Initializes the normalizer with an optional expected calendar year."""
    super().__init__()
    self._expected_year = (
        int(expected_year) if expected_year is not None else None
    )
    self.outside_year_slices_counter = beam.metrics.Metrics.counter(
        "data_extraction", "grid_slices_outside_input_year"
    )
    self.invalid_bucket_slices_counter = beam.metrics.Metrics.counter(
        "data_extraction", "grid_slices_invalid_bucket"
    )

  def _normalize(
      self, grid_type: str, month_val: Any, hour_val: Any
  ) -> tuple[int, int] | None:
    """Returns `(month_idx 1..12, hour_idx 0..23)`, or None if dropped."""
    month_number = int(month_val)
    if (
        self._expected_year is not None
        and month_number >= 100
        and (month_number // 100) != self._expected_year
    ):
      self.outside_year_slices_counter.inc()
      logging.warning(
          "Dropping grid slice (%s, %s, %s) outside expected year %d",
          grid_type,
          month_val,
          hour_val,
          self._expected_year,
      )
      return None
    month_idx = month_number % 100 if month_number >= 100 else month_number
    hour_idx = int(hour_val)
    if not (1 <= month_idx <= 12 and 0 <= hour_idx <= 23):
      self.invalid_bucket_slices_counter.inc()
      logging.warning(
          "Dropping grid slice (%s, %s, %s) with invalid month/hour bucket",
          grid_type,
          month_val,
          hour_val,
      )
      return None
    return month_idx, hour_idx

  def process(self, element) -> Iterator[tuple[tuple[str, int, int], Any]]:
    """Validates and normalizes the `(grid_type, month, hour)` key."""
    (grid_type, month_val, hour_val), slice_payload = element
    normalized = self._normalize(grid_type, month_val, hour_val)
    if normalized is None:
      return
    month_idx, hour_idx = normalized
    yield ((grid_type, month_idx, hour_idx), slice_payload)


class FanoutGridExportKeysDoFn(beam.DoFn):
  """Maps each normalized `(grid_type, month, hour)` slice onto 3 export keys.

  The SAME payload object is yielded for all three keys. Downstream combiners
  must treat inputs as read-only (SumDenseGridCombineFn copies on adopt).
  In the two-stage graph this runs on the at-most-576 Stage-1 outputs, not on
  the raw per-shard slices, so the 3x duplication costs ~1.7k records instead
  of tripling the whole per-shard grid shuffle.
  """

  def process(self, element) -> Iterator[tuple[tuple[str, str, int], Any]]:
    """Yields total, monthly, and hourly export keys for a normalized slice."""
    (grid_type, month_idx, hour_idx), slice_payload = element
    yield ((grid_type, constants.GRID_FAMILY_TOTAL, 0), slice_payload)
    yield ((grid_type, constants.GRID_FAMILY_MONTHLY, month_idx), slice_payload)
    yield ((grid_type, constants.GRID_FAMILY_HOURLY, hour_idx), slice_payload)


class WriteGridArtifactsDoFn(beam.DoFn):
  """Publishes one finalized grid, optionally tee-ing it to the intermediate.

  The combined grid arriving here is already exactly one published bucket, so
  persisting it costs a write and no extra shuffle. That is what makes grid
  re-derivation cheap without touching the combine topology of
  `GroupGridSlices`, which is the most OOM-prone stage in the pipeline.
  """

  def __init__(
      self,
      grid_config: grid_rasterizer.GridConfig,
      *,
      output_dir: str,
      intermediate_root: str = "",
      run_id: str = "",
  ):
    """Initializes the grid artifact writer.

    Args:
      grid_config: GridConfig defining dimensions and coordinates.
      output_dir: Directory path where published grid artifacts are written.
      intermediate_root: Optional root path for intermediate grid slices.
      run_id: Optional run identifier for intermediate tracking.
    """
    self._grid_config = grid_config
    self._output_dir = output_dir
    self._intermediate_root = intermediate_root
    self._run_id = run_id

  def process(self, element) -> Iterator[grid_publish.GridPublishResult]:
    """Writes one grid bucket's artifacts and yields its publish result.

    Args:
      element: Tuple of (export_key, grid_array) for one bucket.

    Yields:
      GridPublishResult describing the published grid and its maximum value.

    Raises:
      ValueError: If `export_key` names an unknown grid bucket. Every key comes
        from the pipeline's own bucketing, so an unknown one is a bug; skipping
        it would only fail later, less clearly, when the shared grid metadata
        finds a bucket missing.
    """
    key, grid_array = element
    prefix = grid_publish.artifact_prefix(*key)

    if self._intermediate_root:
      intermediate_io.write_grid_slice(
          grid_array,
          root=self._intermediate_root,
          run_id=self._run_id,
          prefix=prefix,
      )

    yield grid_publish.publish_grid_artifacts(
        key,
        grid_array,
        grid_config=self._grid_config,
        output_dir=self._output_dir,
    )


class WriteSharedGridMetaFn:
  """Writes grid_meta.json from every grid's publish result.

  Used as a `beam.Map` over the `ToList` of `WriteGridArtifactsDoFn` outputs,
  so it runs exactly once, after the last grid has been written. Raises (and
  so blocks the run pointer flip) unless all export keys are present.
  """

  SIGNAL = "grid_meta"

  def __init__(self, grid_config: grid_rasterizer.GridConfig, output_dir: str):
    """Initializes the shared `grid_meta.json` writer.

    Args:
      grid_config: GridConfig defining dimensions and coordinates.
      output_dir: Directory path where `grid_meta.json` will be written.
    """
    self._grid_config = grid_config
    self._output_dir = output_dir

  def __call__(
      self, results: Sequence[grid_publish.GridPublishResult]
  ) -> str:
    """Writes `grid_meta.json` from `results` maxima and returns `SIGNAL`.

    Args:
      results: Sequence of GridPublishResult items from all exported grids.

    Returns:
      The `SIGNAL` string constant indicating completion.
    """
    value_max = grid_publish.reduce_value_max(results)
    grid_publish.write_shared_grid_meta(
        self._grid_config, self._output_dir, value_max
    )
    logging.info(
        "Wrote %s with maxima %s",
        constants.SHARED_GRID_META_FILENAME,
        value_max,
    )
    return self.SIGNAL


def log_failed_shard(failed_shard: tuple[Any, str]) -> tuple[Any, str]:
  """Records a shard that could not be processed, and passes it through.

  Args:
    failed_shard: Tuple of (shard_descriptor, failure_outcome_reason).

  Returns:
    The unchanged failed shard tuple for downstream tracking.
  """
  shard_input, outcome = failed_shard
  logging.error("Shard %r failed to process: %s", shard_input, outcome)
  return failed_shard


def enforce_failed_shard_threshold(failed_count: int, max_failed: int) -> None:
  """Fails the run when too many shards could not be processed.

  A shard that cannot be read does not produce an error on its own: it just
  contributes nothing, so the run finishes green with a quietly smaller
  dataset. This turns that into a hard failure.

  Args:
    failed_count: Number of shards the ParDo reported as failures.
    max_failed: Budget of tolerated failures; negative disables the check.

  Raises:
    RuntimeError: If more shards failed than the budget allows.
  """
  if max_failed < 0:
    logging.warning(
        "Failed-shard threshold is disabled; %d shards were not processed.",
        failed_count,
    )
    return
  if failed_count > max_failed:
    raise RuntimeError(
        "%d shards failed to process, which exceeds the allowed maximum of"
        " %d. The published dataset would be incomplete. See the"
        " shards_failed counter and the logged shard descriptors."
        % (failed_count, max_failed)
    )
  if failed_count:
    logging.warning(
        "%d shards failed to process, within the allowed maximum of %d.",
        failed_count,
        max_failed,
    )


def check_failed_shard_count(failed_count: int, max_failed: int) -> int:
  """Beam wrapper: enforces the budget and passes the count downstream.

  Args:
    failed_count: Total number of failed shards observed.
    max_failed: Budget of tolerated failures; negative disables the check.

  Returns:
    The passed-through failed shard count.

  Raises:
    RuntimeError: If failed_count exceeds max_failed.
  """
  enforce_failed_shard_threshold(failed_count, max_failed)
  return failed_count


class ExportArtifactsDoFn(beam.DoFn):
  """Gathers aggregated country/FIR records and writes output JSON files."""

  def __init__(
      self,
      *,
      year: int,
      dataset: str,
      intermediate_root: str,
      intermediate_run_root: str,
      run_id: str,
      segments_pattern: str,
      input_file_count: int,
      grid_config: grid_rasterizer.GridConfig,
      started_at: str,
      country_areas: Mapping[str, float],
      fir_areas: Mapping[tuple[str, str], float] | None,
      baseline_stats: Mapping[str, Any],
      dataset_dir: str,
      country_names: Mapping[str, str] | None = None,
  ):
    """Initializes the summary artifact exporter.

    Args:
      year: Four-digit calendar year integer.
      dataset: Dataset identifier string.
      intermediate_root: Root path for intermediate artifacts, or empty string.
      intermediate_run_root: Run-specific intermediate root directory.
      run_id: Unique pipeline run identifier string.
      segments_pattern: File glob pattern used for input segments.
      input_file_count: Number of input Parquet files matched.
      grid_config: GridConfig defining dimensions and coordinates.
      started_at: ISO UTC timestamp when the pipeline run started.
      country_areas: Mapping from ISO2 to country area in square kilometres.
      fir_areas: Optional mapping from (name, ID) to FIR area in sq km.
      baseline_stats: Mapping of country baseline population and GHG metrics.
      dataset_dir: Directory path where published JSON summaries will land.
      country_names: Optional mapping from ISO2 code to country display name.
    """
    super().__init__()
    self._year = year
    self._dataset = dataset
    self._intermediate_root = intermediate_root
    self._intermediate_run_root = intermediate_run_root
    self._run_id = run_id
    self._segments_pattern = segments_pattern
    self._input_file_count = input_file_count
    self._grid_config = grid_config
    self._started_at = started_at
    self._country_areas = country_areas
    self._fir_areas = fir_areas
    self._baseline_stats = baseline_stats
    self._dataset_dir = dataset_dir
    self._country_names = country_names

  def process(
      self,
      unused_element: Any,
      country_items: Sequence[constants.CountryRow],
      fir_items: Sequence[constants.FirRow],
      failed_shard_count: int,
  ) -> Iterator[str]:
    """Persists the intermediate, if enabled, and publishes the summaries.

    Args:
      unused_element: The single trigger element.
      country_items: Every combined country row.
      fir_items: Every combined FIR row.
      failed_shard_count: Output of the failed-shard budget check. Taking it as
        a side input means this runs only once that check has passed, so an
        intermediate manifest is never written for a run that failed it.

    Yields:
      A single completion signal for the pointer flip.
    """
    logging.info(
        "Finalizing data export for Year %s Dataset %s...",
        self._year,
        self._dataset,
    )

    # 0. Optional: persist the reduce output before deriving anything from
    # it. Written first so a crash in derivation still leaves the
    # expensive half of the run recoverable.
    if self._intermediate_root:
      self._write_intermediate(country_items, fir_items, failed_shard_count)

    written = derive.derive_and_publish(
        country_items,
        fir_items,
        country_areas=self._country_areas,
        fir_areas=self._fir_areas,
        baseline_stats=self._baseline_stats,
        year=self._year,
        dataset_dir=self._dataset_dir,
        country_names=self._country_names,
    )

    logging.info(
        "Published %d summary artifacts for year %s dataset %s.",
        len(written),
        self._year,
        self._dataset,
    )
    yield "DONE"

  def _write_intermediate(
      self,
      country_items: Sequence[constants.CountryRow],
      fir_items: Sequence[constants.FirRow],
      failed_shard_count: int,
  ) -> None:
    """Writes the country/FIR aggregates, then the run manifest.

    The manifest goes last: its presence is what tells derive the aggregates
    are complete.

    Args:
      country_items: Every combined country row.
      fir_items: Every combined FIR row.
      failed_shard_count: Number of failed shards, recorded in the manifest.
    """
    run_root = self._intermediate_run_root
    intermediate_io.write_country_agg(country_items, run_root, self._run_id)
    intermediate_io.write_fir_agg(fir_items, run_root, self._run_id)
    intermediate_io.write_manifest(
        intermediate_io.RunManifest(
            run_id=self._run_id,
            failed_shard_count=failed_shard_count,
            input_glob=self._segments_pattern,
            input_file_count=self._input_file_count,
            grid_config=self._grid_config,
            started_at=self._started_at,
            finished_at=datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
        ),
        run_root,
    )
    logging.info(
        "Wrote intermediate aggregates to %s",
        intermediate_io.run_dir(run_root, self._run_id),
    )


class FlipRunPointerFn:
  """Verifies all publish signals and writes the atomic run pointer."""

  def __init__(
      self,
      output_root: str,
      *,
      year: int,
      dataset: str,
      publish_run_id: str,
      expected_signal_count: int | None = None,
  ):
    """Initializes the atomic run pointer flip callable.

    Args:
      output_root: Root directory where published runs and pointers live.
      year: Four-digit calendar year integer.
      dataset: Dataset identifier string.
      publish_run_id: Run identifier to point `latest.json` to.
      expected_signal_count: Optional count of completion signals required
        before flipping the pointer (defaults to 2).
    """
    self._output_root = output_root
    self._year = year
    self._dataset = dataset
    self._publish_run_id = publish_run_id
    # One signal from the grid_meta.json write (which already verified all
    # export keys) and one from the summary export.
    self._expected_signal_count = (
        expected_signal_count if expected_signal_count is not None else 2
    )

  def __call__(
      self,
      signals: Sequence[str],
      unused_failed_shard_count: int | None = None,
  ) -> str:
    """Verifies signal count, writes `latest.json`, and returns its path.

    Args:
      signals: Sequence of upstream completion signal tokens.
      unused_failed_shard_count: Side-input dependency ensuring the failed-shard
        budget check completed before writing the pointer.

    Returns:
      The filesystem path to the newly written `latest.json` pointer file.

    Raises:
      RuntimeError: If the number of observed signals does not match
        `expected_signal_count`.
    """
    # `unused_failed_shard_count` is a side input purely for ordering: it
    # forces the failed-shard budget check to run to completion before the
    # pointer is flipped, so a run that lost more shards than
    # `--max_failed_shards` allows is never published.
    if len(signals) != self._expected_signal_count:
      raise RuntimeError(
          "Refusing to flip %s: expected %d publish signals, saw %d. "
          "Some artifacts did not land."
          % (
              artifact_io.pointer_path(
                  self._output_root, self._year, self._dataset
              ),
              self._expected_signal_count,
              len(signals),
          )
      )
    path = artifact_io.write_run_pointer(
        self._output_root,
        year=self._year,
        dataset=self._dataset,
        run_id=self._publish_run_id,
        published_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    logging.info("Flipped %s to run %s", path, self._publish_run_id)
    return path


def _empty_sparse_slice() -> tuple[str, np.ndarray, np.ndarray, np.ndarray]:
  """Returns an empty 4-element sparse grid slice tuple for seeding keys."""
  return (
      constants.GRID_ACC_SPARSE,
      np.empty(0, dtype=np.int16),
      np.empty(0, dtype=np.int16),
      np.empty(0, dtype=np.float32),
  )


def combine_grid_slices_to_export_keys(
    grid_slices: beam.PCollection,
    grid_config: grid_rasterizer.GridConfig,
    expected_year: int | None,
) -> beam.PCollection:
  """Sums per-shard grid slices into one full grid per export key.

  Input elements are `((grid_type, YYYYMM|MM, hour), sparse_payload)` as
  emitted by `ProcessParquetShardDoFn.OUTPUT_TAG_GRID`. Output elements are
  `((grid_type, bucket_type, bucket_idx), np.ndarray[height, width])` for
  every key in `grid_publish.all_export_keys()` (exactly 74), including keys no
  flight touched, which come out as all-zero grids.

  Args:
    grid_slices: PCollection of `((grid_type, month, hour), payload)`.
    grid_config: Grid geometry; only height and width are used.
    expected_year: If set, slices whose `YYYYMM` month is from another year are
      dropped (and counted) before Stage 1. If `None`, all months are kept.

  Returns:
    PCollection of `(export_key, np.ndarray)` with exactly 74 elements.
  """
  # Normalization MUST precede Stage 1 so `202503` and `3` share one key, and
  # so out-of-year slices never reach the expensive combine.
  month_hour_accumulators = (
      grid_slices
      | "NormalizeGridMonthHourKeys"
      >> beam.ParDo(
          NormalizeGridMonthHourKeyDoFn(expected_year=expected_year)
      )
      | "CombineGridSlicesByMonthHour"
      >> beam.CombinePerKey(
          # extract_output returns a compact accumulator, not a full grid.
          SumGridSlicesToAccumulatorCombineFn()
      )
  )

  fanned_grid_slices = (
      month_hour_accumulators
      | "FanoutGridExportKeys" >> beam.ParDo(FanoutGridExportKeysDoFn())
  )

  # Seed every export key so the artifact set is complete regardless of
  # which buckets the input happened to touch. Seeds are added AFTER Stage 1:
  # they are keyed by export key, not by (month, hour).
  seed_grid_slices = grid_slices.pipeline | "SeedAllGridKeys" >> beam.Create(
      [(k, _empty_sparse_slice()) for k in grid_publish.all_export_keys()]
  )

  return (
      (fanned_grid_slices, seed_grid_slices)
      | "FlattenGridSlices" >> beam.Flatten()
      | "GroupGridSlices"
      >> beam.CombinePerKey(
          # Stage 2 consumes Stage-1 accumulators directly (`add_input`
          # accepts every `GridPayload`) and treats them as read-only, which is
          # required because the fanout shares one payload across 3 keys.
          SumDenseGridCombineFn(
              height=grid_config.height, width=grid_config.width
          )
      )
  )
