"""Vectorized flight segment extraction and spatial processing."""

from collections.abc import Iterable, Mapping, MutableMapping, MutableSequence, Sequence
import dataclasses
import enum
import threading
from typing import Any, NamedTuple
from absl import logging
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as _pc
import pyarrow.parquet as pq
import shapely
import shapely.geometry
from common import constants
from common import fs
from data_extraction import geo_utils
from data_extraction import grid_rasterizer
from data_extraction import metadata_loader

pc: Any = _pc


class ShardOutcome(enum.Enum):
  """Why a shard produced the records it did.

  A shard that yields no records is not necessarily healthy, and the pipeline
  used to be unable to tell the difference: an unreadable chunk and a chunk
  covering empty airspace both showed up as "nothing emitted", so a run could
  lose arbitrary amounts of data and still exit 0. Every no-output path now
  carries a reason.

  `OK`, `EMPTY_INPUT` and `NO_VALID_SEGMENTS` are expected outcomes.
  `MALFORMED_INPUT`, `READ_FAILED`, `MISSING_COLUMNS` and `EXCEPTION` are
  failures and count against the failed-shard budget.
  """

  OK = "ok"
  # The shard descriptor itself was not a path or a 4-element slice tuple.
  MALFORMED_INPUT = "malformed_input"
  # The file could not be read, could not be read at the requested scope, or
  # lacks a coordinate column (checked by `read_parquet_table`).
  READ_FAILED = "read_failed"
  # The read succeeded and returned zero rows.
  EMPTY_INPUT = "empty_input"
  # The table has coordinates but lacks the energy forcing or timestamp
  # column, or its time or timezone column has an unsupported type.
  MISSING_COLUMNS = "missing_columns"
  # Rows were present but every one of them failed validation.
  NO_VALID_SEGMENTS = "no_valid_segments"
  # Raised out of processing (e.g. an unsupported coordinate or energy-forcing
  # column type raising `pyarrow.ArrowException`); set by the caller, never
  # returned by `process_shard`.
  EXCEPTION = "exception"


@dataclasses.dataclass(frozen=True)
class ProcessWorkerContext:
  """Context containing spatial indexes and lookup tables for a worker."""

  airports_map: dict[str, str]
  country_index: geo_utils.SpatialIndex
  country_names_by_iso: dict[str, str]
  fir_index: geo_utils.SpatialIndex


@dataclasses.dataclass(frozen=True)
class ExtractedFlightData:
  """Columnar arrays of validated flight segment data."""

  lon_start: np.ndarray
  lat_start: np.ndarray
  lon_end: np.ndarray
  lat_end: np.ndarray
  ef: np.ndarray
  months: np.ndarray
  hours: np.ndarray
  dep_countries: np.ndarray
  arr_countries: np.ndarray
  invalid_coords_count: int = 0
  invalid_times_count: int = 0
  zero_len_count: int = 0
  outside_year_count: int = 0

  @property
  def count(self) -> int:
    """Number of validated flight segments in the batch."""
    return len(self.lon_start)


@dataclasses.dataclass(frozen=True)
class ShardProcessingResult:
  """Structured outputs from processing a single parquet shard.

  A result is always produced, even when nothing was emitted; `outcome` says
  why. Callers must inspect it rather than treating "no records" as success.
  """

  grid_slices: list[
      tuple[
          tuple[str, int, int],
          tuple[str, np.ndarray, np.ndarray, np.ndarray],
      ]
  ]
  country_records: list[constants.CountryRow]
  fir_records: list[constants.FirRow]
  invalid_coords_count: int = 0
  invalid_times_count: int = 0
  zero_len_count: int = 0
  outside_year_count: int = 0
  outcome: ShardOutcome = ShardOutcome.OK

  @property
  def is_failure(self) -> bool:
    """True when the shard did not yield records because something broke."""
    return self.outcome in (
        ShardOutcome.MALFORMED_INPUT,
        ShardOutcome.READ_FAILED,
        ShardOutcome.MISSING_COLUMNS,
        ShardOutcome.EXCEPTION,
    )


def empty_shard_result(
    outcome: ShardOutcome,
    *,
    invalid_coords_count: int = 0,
    invalid_times_count: int = 0,
    zero_len_count: int = 0,
    outside_year_count: int = 0,
) -> ShardProcessingResult:
  """Builds a record-free result that still reports why it is empty."""
  return ShardProcessingResult(
      grid_slices=[],
      country_records=[],
      fir_records=[],
      invalid_coords_count=invalid_coords_count,
      invalid_times_count=invalid_times_count,
      zero_len_count=zero_len_count,
      outside_year_count=outside_year_count,
      outcome=outcome,
  )


def load_process_worker_context(
    airports_path: str,
    firs_path: str,
    country_geojson_path: str | None = None,
) -> ProcessWorkerContext:
  """Loads airports map and builds spatial indexes for countries and FIRs.

  Args:
    airports_path: Path to airports CSV metadata.
    firs_path: Path to FIR GeoJSON file.
    country_geojson_path: Optional path to country GeoJSON boundaries.

  Returns:
    An initialized ProcessWorkerContext instance.

  Raises:
    ValueError: If FIR feature validation fails.
    RuntimeError: If country boundaries, FIR boundaries, or airport mappings are
      empty.
  """
  logging.info(
      "Initializing process-wide spatial indexes and airport metadata..."
  )
  airports_map = metadata_loader.load_airports_mapping(airports_path)
  boundaries = metadata_loader.load_boundaries(
      firs_path=firs_path, country_geojson_path=country_geojson_path
  )

  if not airports_map:
    raise RuntimeError(
        "Airports mapping initialized with 0 entries; refusing to run "
        "with empty airport mapping."
    )
  logging.info(
      "Worker context initialized. Countries: %d, FIRs: %d, Airports: %d",
      boundaries.country_index.count,
      boundaries.fir_index.count,
      len(airports_map),
  )
  return ProcessWorkerContext(
      airports_map=airports_map,
      country_index=boundaries.country_index,
      country_names_by_iso=boundaries.country_names,
      fir_index=boundaries.fir_index,
  )


_worker_context_lock = threading.Lock()
_worker_context_cache: dict[tuple[Any, ...], ProcessWorkerContext] = {}


def acquire_process_worker_context(
    airports_path: str,
    firs_path: str,
    country_geojson_path: str | None = None,
) -> ProcessWorkerContext:
  """Acquires a process-wide cached `ProcessWorkerContext` across threads."""
  key = (
      airports_path,
      firs_path,
      country_geojson_path,
  )
  with _worker_context_lock:
    cached = _worker_context_cache.get(key)
    if cached is None:
      cached = load_process_worker_context(
          airports_path=airports_path,
          firs_path=firs_path,
          country_geojson_path=country_geojson_path,
      )
      _worker_context_cache[key] = cached
    return cached


def parse_shard_input(
    shard_input: Any,
) -> tuple[str, int | None, int, int | None] | None:
  """Parses shard input into (path, row_group_idx, row_offset, num_rows).

  Args:
    shard_input: String path or tuple of (path, row_group_idx, row_offset,
      num_rows).

  Returns:
    A 4-element tuple or None if input format is unrecognized.
  """
  if isinstance(shard_input, str):
    return (shard_input, None, 0, None)
  elif isinstance(shard_input, (tuple, list)):
    if len(shard_input) != 4:
      logging.error(
          "Expected 4-element tuple for shard input, got length %d",
          len(shard_input),
      )
      return None
    parquet_path, row_group_idx, row_offset, num_rows = shard_input
    return (parquet_path, row_group_idx, row_offset, num_rows)
  logging.error("Unknown shard input format: %s", type(shard_input))
  return None


# _SEGMENT_COLUMN_CANDIDATES describes the logical columns the pipeline needs
# and the source column names, in priority order, that may carry them. The
# energy forcing ("ef") column must be non-effective (no ERF/RF ratio applied);
# ERF/RF is applied later, only in the CO2e conversion
# (`constants.GWP_TONNES_PER_MJ`).
_SEGMENT_COLUMN_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lon_s", ("lon_start", "longitude_start", "start_lon")),
    ("lat_s", ("lat_start", "latitude_start", "start_lat")),
    ("lon_e", ("lon_end", "longitude_end", "end_lon")),
    ("lat_e", ("lat_end", "latitude_end", "end_lat")),
    ("ef", ("sum_ef_mj", "ef_mj", "contrail_ef_mj", "energy_forcing_mj")),
    ("time", ("time_start", "timestamp_start", "time", "flight_time")),
    ("tz", ("time_start_tz", "tz", "timezone_offset")),
    (
        "dep",
        (
            "departure_airport_icao",
            "departure_airport",
            "dep_icao",
            "origin_airport",
        ),
    ),
    (
        "arr",
        (
            "arrival_airport_icao",
            "arrival_airport",
            "arr_icao",
            "destination_airport",
        ),
    ),
)

# Without these four there is no segment geometry and nothing can be
# attributed, so a file missing any of them is unreadable rather than sparse.
_REQUIRED_COLUMN_KEYS: tuple[str, ...] = ("lon_s", "lat_s", "lon_e", "lat_e")


def _resolve_segment_columns(
    column_names: Sequence[str],
) -> tuple[dict[str, str], list[str]]:
  """Maps logical column keys onto the names actually present in a schema.

  Args:
    column_names: Column names as they appear in the source file.

  Returns:
    Tuple of (logical key -> real column name, list of columns to project).
  """
  available = {col_name.lower(): col_name for col_name in column_names}
  resolved: dict[str, str] = {}
  cols_to_load: list[str] = []
  for key, candidates in _SEGMENT_COLUMN_CANDIDATES:
    for candidate in candidates:
      if (real_name := available.get(candidate)) is not None:
        resolved[key] = real_name
        cols_to_load.append(real_name)
        break
  return resolved, cols_to_load


_PARQUET_READ_BATCH_SIZE = 65536


def _validate_parquet_read_request(
    pq_file: pq.ParquetFile,
    parquet_path: str,
    *,
    row_group_idx: int | None,
    row_offset: int,
    num_rows: int | None,
) -> list[str]:
  """Validates schema columns and slice bounds for a Parquet read request.

  Args:
    pq_file: Opened `ParquetFile` instance.
    parquet_path: Source path for diagnostic logging.
    row_group_idx: Optional target row group index.
    row_offset: Starting row offset.
    num_rows: Optional row count limit.

  Returns:
    Projected column list to load, or empty list if validation fails.
  """
  resolved, cols_to_load = _resolve_segment_columns(list(pq_file.schema.names))
  if not all(k in resolved for k in _REQUIRED_COLUMN_KEYS):
    logging.error(
        "Parquet file %s is missing required coordinate columns; has %s",
        parquet_path,
        sorted(pq_file.schema.names),
    )
    return []

  if row_offset < 0 or (num_rows is not None and num_rows < 0):
    logging.error(
        "Invalid row_offset %d or num_rows %s for %s",
        row_offset,
        num_rows,
        parquet_path,
    )
    return []

  if row_group_idx is not None and (
      row_group_idx < 0 or row_group_idx >= pq_file.metadata.num_row_groups
  ):
    logging.error(
        "Row group %d is out of range for %s (%d row groups)",
        row_group_idx,
        parquet_path,
        pq_file.metadata.num_row_groups,
    )
    return []

  return cols_to_load


def _slice_record_batches(
    batches: Iterable[pa.RecordBatch],
    *,
    row_offset: int,
    num_rows: int | None,
) -> list[pa.RecordBatch]:
  """Keeps the rows in [row_offset, row_offset + num_rows) of a batch stream.

  Iteration stops as soon as the requested range has been covered.

  Args:
    batches: Record batches in row order.
    row_offset: First row to keep, relative to the start of `batches`.
    num_rows: Number of rows to keep, or None to keep all remaining rows.

  Returns:
    The overlapping batches, sliced to the requested range.
  """
  sliced = []
  current_row = 0
  target_start = row_offset
  target_end = (row_offset + num_rows) if num_rows is not None else None

  for batch in batches:
    batch_len = batch.num_rows
    batch_start = current_row
    batch_end = current_row + batch_len
    current_row = batch_end

    if batch_end <= target_start:
      continue
    if target_end is not None and batch_start >= target_end:
      break

    slice_start = max(0, target_start - batch_start)
    slice_end = (
        min(batch_len, target_end - batch_start)
        if target_end is not None
        else batch_len
    )
    slice_len = slice_end - slice_start
    if slice_len > 0:
      if slice_start == 0 and slice_len == batch_len:
        sliced.append(batch)
      else:
        sliced.append(batch.slice(slice_start, slice_len))

    if target_end is not None and current_row >= target_end:
      break
  return sliced


def _empty_projected_table(
    pq_file: pq.ParquetFile, cols_to_load: Sequence[str]
) -> pa.Table:
  """Returns an empty table with the schema of the projected columns."""
  schema = (
      pa.schema(
          [pq_file.schema_arrow.field(col_name) for col_name in cols_to_load]
      )
      if cols_to_load
      else pq_file.schema_arrow
  )
  return pa.Table.from_batches([], schema=schema)


def _read_parquet_row_slice(
    pq_file: pq.ParquetFile,
    *,
    cols_to_load: Sequence[str],
    row_group_idx: int | None,
    row_offset: int,
    num_rows: int | None,
) -> pa.Table:
  """Reads a contiguous row slice from `pq_file` using `iter_batches`."""
  target_row_groups = [row_group_idx] if row_group_idx is not None else None
  batch_size = (
      min(num_rows, _PARQUET_READ_BATCH_SIZE)
      if num_rows is not None and num_rows > 0
      else _PARQUET_READ_BATCH_SIZE
  )
  batches = _slice_record_batches(
      pq_file.iter_batches(
          batch_size=batch_size,
          row_groups=target_row_groups,
          columns=list(cols_to_load),
      ),
      row_offset=row_offset,
      num_rows=num_rows,
  )
  if batches:
    return pa.Table.from_batches(batches)
  return _empty_projected_table(pq_file, cols_to_load)


def _read_projected_table(
    pq_file: pq.ParquetFile,
    *,
    cols_to_load: list[str],
    row_group_idx: int | None,
    row_offset: int,
    num_rows: int | None,
) -> pa.Table:
  """Reads the requested scope of `pq_file`, restricted to `cols_to_load`.

  Args:
    pq_file: Opened `ParquetFile` instance.
    cols_to_load: Columns to project.
    row_group_idx: Optional target row group index.
    row_offset: Starting row offset.
    num_rows: Optional row count limit.

  Returns:
    The whole file or row group when no slice is requested, otherwise the
    requested row slice.
  """
  if row_offset == 0 and num_rows is None:
    if row_group_idx is not None:
      return pq_file.read_row_group(row_group_idx, columns=cols_to_load)
    return pq_file.read(columns=cols_to_load)
  return _read_parquet_row_slice(
      pq_file,
      cols_to_load=cols_to_load,
      row_group_idx=row_group_idx,
      row_offset=row_offset,
      num_rows=num_rows,
  )


def _has_requested_row_count(
    table: pa.Table,
    parquet_path: str,
    *,
    row_group_idx: int | None,
    row_offset: int,
    num_rows: int | None,
) -> bool:
  """Checks that a sliced read returned exactly `num_rows` rows.

  Args:
    table: Table that was read.
    parquet_path: Source path for diagnostic logging.
    row_group_idx: Requested row group index, for logging.
    row_offset: Requested row offset, for logging.
    num_rows: Requested row count, or None if unbounded.

  Returns:
    True if no row count was requested or it matches; otherwise logs an error
    and returns False.
  """
  if num_rows is not None and table.num_rows != num_rows:
    logging.error(
        "Read %d rows from %s (row group %s, offset %d) but %d were"
        " requested; dropping the chunk rather than emitting a partial"
        " slice",
        table.num_rows,
        parquet_path,
        row_group_idx,
        row_offset,
        num_rows,
    )
    return False
  return True


def _read_parquet_file(
    pq_file: pq.ParquetFile,
    parquet_path: str,
    *,
    row_group_idx: int | None,
    row_offset: int,
    num_rows: int | None,
) -> pa.Table | None:
  """Validates and serves a read request against an opened Parquet file.

  Args:
    pq_file: Opened `ParquetFile` instance.
    parquet_path: Source path for diagnostic logging.
    row_group_idx: Optional target row group index.
    row_offset: Starting row offset.
    num_rows: Optional row count limit.

  Returns:
    The projected table, or None if the request is invalid or the read did
    not return exactly `num_rows` rows.
  """
  cols_to_load = _validate_parquet_read_request(
      pq_file,
      parquet_path,
      row_group_idx=row_group_idx,
      row_offset=row_offset,
      num_rows=num_rows,
  )
  if not cols_to_load:
    return None

  table = _read_projected_table(
      pq_file,
      cols_to_load=cols_to_load,
      row_group_idx=row_group_idx,
      row_offset=row_offset,
      num_rows=num_rows,
  )
  if not _has_requested_row_count(
      table,
      parquet_path,
      row_group_idx=row_group_idx,
      row_offset=row_offset,
      num_rows=num_rows,
  ):
    return None
  return table


def read_parquet_table(
    parquet_path: str,
    *,
    row_group_idx: int | None = None,
    row_offset: int = 0,
    num_rows: int | None = None,
) -> pa.Table | None:
  """Reads a parquet file or slice into a PyArrow Table with column projection.

  `row_offset` is relative to `row_group_idx` when one is given, and relative
  to the file otherwise. A slice shorter than `num_rows` is treated as a
  failure,
  since it means the offsets and the file disagree.

  Args:
    parquet_path: Path to the Parquet file (any registered filesystem).
    row_group_idx: Optional index of the specific row group to read.
    row_offset: Row offset within the target row group, or within the file when
      `row_group_idx` is None.
    num_rows: Optional row count limit to slice.

  Returns:
    PyArrow Table, or None if the request could not be served exactly.
  """
  try:
    with fs.open_file(parquet_path, "rb") as f:
      return _read_parquet_file(
          pq.ParquetFile(f),
          parquet_path,
          row_group_idx=row_group_idx,
          row_offset=row_offset,
          num_rows=num_rows,
      )
  except (
      pa.ArrowException,
      OSError,
      fs.Error,
      ValueError,
  ) as e:
    logging.error(
        "Failed to read parquet shard %s: %s", parquet_path, e, exc_info=True
    )
    return None


def _normalize_airport_code(k: Any) -> str | None:
  """Normalizes an airport code to uppercase, or returns `None` if blank."""
  if k is None:
    return None
  if isinstance(k, str):
    cleaned_code = k.strip().upper()
    return cleaned_code if cleaned_code else None
  if isinstance(k, bytes):
    cleaned_code = k.decode("utf-8", errors="ignore").strip().upper()
    return cleaned_code if cleaned_code else None
  if pd.isna(k):
    return None
  cleaned_code = str(k).strip().upper()
  return cleaned_code if cleaned_code else None


def _dictionary_indices(dict_arr: Any, *, null_index: int) -> np.ndarray:
  """Returns a dictionary array's indices as numpy, nulls as `null_index`."""
  if dict_arr.null_count > 0:
    filled: Any = pc.fill_null(
        pc.cast(dict_arr.indices, pa.int32()), null_index
    )
    return filled.to_numpy(zero_copy_only=False)
  return dict_arr.indices.to_numpy(zero_copy_only=False)


def _map_airports_via_dictionary(
    col: Any,
    *,
    valid_indices: np.ndarray | None,
    num_valid: int,
    airports_map: Mapping[str, str],
) -> np.ndarray:
  """Maps airport codes to ISO-2 codes, looking up each distinct code once.

  Args:
    col: Arrow airport ICAO column for all rows.
    valid_indices: Rows to keep, or None to keep every row.
    num_valid: Number of rows kept.
    airports_map: Mapping from airport ICAO codes to ISO-2 country codes.

  Returns:
    Object array of ISO-2 codes (None when unknown) for the kept rows.

  Raises:
    pa.ArrowException, TypeError, ValueError: If Arrow cannot take or
      dictionary-encode the column.
  """
  if valid_indices is not None:
    col = col.take(valid_indices)
    if col.null_count == num_valid:
      return np.full(num_valid, None, dtype=object)
  combined: Any = col.combine_chunks()
  dict_arr: Any = (
      combined
      if pa.types.is_dictionary(combined.type)
      else combined.dictionary_encode()
  )
  dictionary_values = dict_arr.dictionary.to_pylist()
  num_keys = len(dictionary_values)
  lookup = np.empty(num_keys + 1, dtype=object)
  for index, raw_val in enumerate(dictionary_values):
    normalized_code = _normalize_airport_code(raw_val)
    lookup[index] = (
        airports_map.get(normalized_code) if normalized_code else None
    )
  lookup[num_keys] = None
  return lookup[_dictionary_indices(dict_arr, null_index=num_keys)]


_CACHE_MISS = object()


def _map_airports_row_by_row(
    raw_vals: np.ndarray,
    *,
    num_valid: int,
    airports_map: Mapping[str, str],
) -> np.ndarray:
  """Maps airport codes to ISO-2 codes one row at a time, caching lookups.

  Args:
    raw_vals: Raw airport code per kept row.
    num_valid: Number of kept rows.
    airports_map: Mapping from airport ICAO codes to ISO-2 country codes.

  Returns:
    Object array of ISO-2 codes (None when unknown).
  """
  cache: dict[Any, str | None] = {None: None, "": None}
  out = np.empty(num_valid, dtype=object)
  for index, raw_val in enumerate(raw_vals):
    try:
      cached_iso = cache.get(raw_val, _CACHE_MISS)
      if cached_iso is not _CACHE_MISS:
        out[index] = cached_iso
        continue
    except TypeError:
      pass
    normalized_code = _normalize_airport_code(raw_val)
    iso_val = airports_map.get(normalized_code) if normalized_code else None
    try:
      cache[raw_val] = iso_val
    except TypeError:
      pass
    out[index] = iso_val
  return out


def _extract_airport_countries(
    table: pa.Table,
    *,
    col_name: str | None,
    valid_indices: np.ndarray | None,
    num_valid: int,
    airports_map: Mapping[str, str],
) -> np.ndarray:
  """Maps an Arrow airport ICAO column to ISO-2 country codes for valid rows."""
  if num_valid == 0:
    return np.empty(0, dtype=object)
  if not col_name or col_name not in table.column_names:
    return np.full(num_valid, None, dtype=object)

  col: Any = table[col_name]
  if pa.types.is_null(col.type) or col.null_count == len(col):
    return np.full(num_valid, None, dtype=object)

  try:
    return _map_airports_via_dictionary(
        col,
        valid_indices=valid_indices,
        num_valid=num_valid,
        airports_map=airports_map,
    )
  except (pa.ArrowException, TypeError, ValueError):
    raw_vals = table[col_name].to_numpy(zero_copy_only=False)
    if valid_indices is not None:
      raw_vals = raw_vals[valid_indices]
    return _map_airports_row_by_row(
        raw_vals, num_valid=num_valid, airports_map=airports_map
    )


@dataclasses.dataclass(frozen=True)
class _CalendarParts:
  """Per-row UTC calendar fields; zero where the timestamp is null."""

  # Calendar year.
  years: np.ndarray
  # Month of year in [1, 12].
  month_of_year: np.ndarray
  # Hour of day in [0, 23].
  hours: np.ndarray
  # True where the timestamp parsed to a non-null value.
  is_valid: np.ndarray


def _calendar_parts_from_pandas(time_column: Any) -> _CalendarParts:
  """Parses a time column with pandas, coercing unparsable values to null."""
  time_series = pd.to_datetime(
      time_column.to_pandas(), errors="coerce", utc=True
  )
  valid_ts = time_series.notna().to_numpy()
  return _CalendarParts(
      years=np.where(valid_ts, time_series.dt.year.to_numpy(dtype=np.int32), 0),
      month_of_year=np.where(
          valid_ts, time_series.dt.month.to_numpy(dtype=np.int32), 0
      ),
      hours=np.where(valid_ts, time_series.dt.hour.to_numpy(dtype=np.int32), 0),
      is_valid=valid_ts,
  )


def _calendar_parts_from_arrow(time_column: Any) -> _CalendarParts:
  """Reads calendar fields from an Arrow timestamp or date column."""
  time_column_us = pc.cast(time_column, pa.timestamp("us"), safe=False)
  is_valid_ts = pc.is_valid(time_column_us).to_numpy(zero_copy_only=False)
  raw_years = pc.year(time_column_us).to_numpy(zero_copy_only=False)
  raw_months = pc.month(time_column_us).to_numpy(zero_copy_only=False)
  raw_hours = pc.hour(time_column_us).to_numpy(zero_copy_only=False)
  return _CalendarParts(
      years=np.where(is_valid_ts, raw_years, 0).astype(np.int32),
      month_of_year=np.where(is_valid_ts, raw_months, 0).astype(np.int32),
      hours=np.where(is_valid_ts, raw_hours, 0).astype(np.int32),
      is_valid=is_valid_ts,
  )


def _valid_calendar_mask(parts: _CalendarParts) -> np.ndarray:
  """Returns True where the timestamp is present and within 1900-2100."""
  return (
      parts.is_valid
      & (parts.month_of_year >= 1)
      & (parts.month_of_year <= 12)
      & (parts.years >= 1900)
      & (parts.years <= 2100)
  )


def _is_temporal_type(arrow_type: pa.DataType) -> bool:
  """Returns whether `arrow_type` is an Arrow timestamp or date type."""
  return (
      pa.types.is_timestamp(arrow_type)
      or pa.types.is_date(arrow_type)
      or pa.types.is_date32(arrow_type)
      or pa.types.is_date64(arrow_type)
  )


def _extract_timestamp_arrays(
    table: pa.Table,
    time_col: str,
    parquet_path: str = "",
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
  """Extracts timestamp arrays, or `None` if `time_col` is unsupported."""
  time_column = table[time_col]
  is_string_type = pa.types.is_string(
      time_column.type
  ) or pa.types.is_large_string(time_column.type)
  if not (is_string_type or _is_temporal_type(time_column.type)):
    logging.warning(
        "Shard %s has unsupported type %s for time column %s; raw numeric"
        " epoch values without an explicit time unit are not supported."
        " Timestamps must be timestamp, date, or ISO-8601 string.",
        parquet_path,
        time_column.type,
        time_col,
    )
    return None

  try:
    if is_string_type:
      parts = _calendar_parts_from_pandas(time_column)
    else:
      parts = _calendar_parts_from_arrow(time_column)
  except (pa.ArrowException, TypeError, ValueError) as e:
    logging.warning(
        "Arrow timestamp parsing failed (%s), falling back to pandas",
        e,
        exc_info=True,
    )
    parts = _calendar_parts_from_pandas(time_column)

  valid_time_mask = _valid_calendar_mask(parts)
  months = parts.years * 100 + parts.month_of_year
  return parts.years, months, parts.hours, valid_time_mask


def _is_string_type(arrow_type: pa.DataType) -> bool:
  """Returns whether `arrow_type` is an Arrow UTF-8 string type."""
  return (
      pa.types.is_string(arrow_type)
      or pa.types.is_large_string(arrow_type)
      or pa.types.is_string_view(arrow_type)
  )


def _parse_timezone_offset_string(raw_entry: Any) -> int | None:
  """Parses a numeric timezone offset string into whole hours, or None."""
  if not isinstance(raw_entry, str) or "_" in raw_entry:
    return None
  try:
    offset_hours = float(raw_entry)
  except ValueError:
    return None
  if (
      not np.isfinite(offset_hours)
      or offset_hours < -14.0
      or offset_hours > 14.0
  ):
    return None
  return int(np.round(offset_hours))


def _build_timezone_dictionary_lookups(
    dictionary_entries: Sequence[Any],
) -> tuple[np.ndarray, np.ndarray]:
  """Builds offset and validity lookup tables for dictionary-encoded strings."""
  entry_count = len(dictionary_entries)
  offset_lookup = np.zeros(entry_count + 1, dtype=np.int32)
  valid_lookup = np.zeros(entry_count + 1, dtype=bool)
  for entry_index, raw_entry in enumerate(dictionary_entries):
    if raw_entry is None:
      valid_lookup[entry_index] = True
      continue
    parsed_hours = _parse_timezone_offset_string(raw_entry)
    if parsed_hours is not None:
      offset_lookup[entry_index] = parsed_hours
      valid_lookup[entry_index] = True
  valid_lookup[entry_count] = True
  return offset_lookup, valid_lookup


def _parse_string_timezone_offsets(
    tz_raw: Any,
) -> tuple[np.ndarray, np.ndarray] | None:
  """Parses a string or dictionary-string timezone column via dictionary lookup.

  Args:
    tz_raw: Arrow string or dictionary-encoded string timezone offset column.

  Returns:
    Tuple of (int32 whole-hour offsets, boolean validity mask), or None if
    dictionary encoding fails and numeric fallback is needed.
  """
  try:
    combined_chunks: Any = tz_raw.combine_chunks()
    dictionary_array: Any = (
        combined_chunks
        if pa.types.is_dictionary(combined_chunks.type)
        else combined_chunks.dictionary_encode()
    )
    dictionary_entries = dictionary_array.dictionary.to_pylist()
    offset_lookup, valid_lookup = _build_timezone_dictionary_lookups(
        dictionary_entries
    )
    if dictionary_array.null_count > 0:
      filled_indices: Any = pc.fill_null(
          pc.cast(dictionary_array.indices, pa.int32()),
          len(dictionary_entries),
      )
      row_indices = filled_indices.to_numpy(zero_copy_only=False)
    else:
      row_indices = dictionary_array.indices.to_numpy(zero_copy_only=False)
    return offset_lookup[row_indices], valid_lookup[row_indices]
  except (pa.ArrowException, TypeError, ValueError):
    return None


def _read_timezone_hours(
    tz_raw: Any,
    *,
    tz_col: str,
    parquet_path: str,
) -> tuple[np.ndarray, np.ndarray] | None:
  """Reads a numeric timezone offset column as float hours.

  Args:
    tz_raw: Arrow timezone offset column.
    tz_col: Column name for diagnostic logging.
    parquet_path: Source path for diagnostic logging.

  Returns:
    Tuple of (null mask, float64 offsets), or None if the column cannot be
    read as numeric.
  """
  try:
    tz_float_arr = pc.cast(tz_raw, pa.float64(), safe=False)
    tz_is_null = pc.is_null(tz_raw, nan_is_null=False).to_numpy(
        zero_copy_only=False
    )
    tz_floats = tz_float_arr.to_numpy(zero_copy_only=False)
  except (pa.ArrowException, TypeError, ValueError):
    try:
      tz_numeric_series = pd.to_numeric(tz_raw.to_pandas(), errors="coerce")
      tz_is_null = pc.is_null(tz_raw, nan_is_null=False).to_numpy(
          zero_copy_only=False
      )
      tz_floats = tz_numeric_series.to_numpy(dtype=np.float64)
    except (pa.ArrowException, TypeError, ValueError, OSError) as e:
      logging.warning(
          "Shard %s: failed to read timezone offset column %s as numeric (%s)",
          parquet_path,
          tz_col,
          e,
      )
      return None
  return tz_is_null, tz_floats


def _clean_and_round_timezone_offsets(
    tz_floats: np.ndarray, tz_is_null: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
  """Validates timezone offsets in [-14, 14] and rounds to int32."""
  valid_tz = tz_is_null | (
      np.isfinite(tz_floats) & (tz_floats >= -14.0) & (tz_floats <= 14.0)
  )
  clean_floats = np.where(valid_tz & (~tz_is_null), tz_floats, 0.0)
  tz_offsets = np.round(clean_floats).astype(np.int32)
  return tz_offsets, valid_tz


def _is_null_column(raw_col: pa.Array, num_rows: int) -> bool:
  """Returns True if the column is null-typed or entirely null."""
  return bool(pa.types.is_null(raw_col.type) or raw_col.null_count == num_rows)


def _is_supported_timezone_type(
    tz_raw: pa.Array, tz_col: str, parquet_path: str
) -> bool:
  """Validates that tz_raw is numeric or numeric-string hours in [-14, 14]."""
  value_type = (
      tz_raw.type.value_type
      if pa.types.is_dictionary(tz_raw.type)
      else tz_raw.type
  )
  if (
      pa.types.is_integer(value_type)
      or pa.types.is_floating(value_type)
      or _is_string_type(value_type)
  ):
    return True
  logging.warning(
      "Shard %s has unsupported timezone offset column type %s for %s;"
      " timezone offset must be numeric or numeric-string hours in [-14, 14].",
      parquet_path,
      tz_raw.type,
      tz_col,
  )
  return False


def _extract_timezone_offsets(
    table: pa.Table,
    *,
    tz_col: str | None,
    num_rows_total: int,
    parquet_path: str = "",
) -> tuple[np.ndarray, np.ndarray] | None:
  """Extracts timezone offsets and masks, or returns `None` if unsupported."""
  if not tz_col or tz_col not in table.column_names:
    return np.zeros(num_rows_total, dtype=np.int32), np.ones(
        num_rows_total, dtype=bool
    )

  tz_raw = table[tz_col]
  if _is_null_column(tz_raw, num_rows_total):
    return np.zeros(num_rows_total, dtype=np.int32), np.ones(
        num_rows_total, dtype=bool
    )
  if not _is_supported_timezone_type(tz_raw, tz_col, parquet_path):
    return None

  value_type = (
      tz_raw.type.value_type
      if pa.types.is_dictionary(tz_raw.type)
      else tz_raw.type
  )
  if _is_string_type(value_type):
    parsed_string_offsets = _parse_string_timezone_offsets(tz_raw)
    if parsed_string_offsets is not None:
      return parsed_string_offsets

  tz_hours = _read_timezone_hours(
      tz_raw, tz_col=tz_col, parquet_path=parquet_path
  )
  if tz_hours is None:
    return None
  tz_is_null, tz_floats = tz_hours
  return _clean_and_round_timezone_offsets(tz_floats, tz_is_null)


def _extract_energy_forcing(table: pa.Table, ef_col: str) -> np.ndarray:
  """Extracts and sanitizes energy forcing values from an Arrow table."""
  try:
    ef_arrow = pc.fill_null(
        pc.cast(table[ef_col], pa.float64(), safe=False), 0.0
    )
    ef_vals = ef_arrow.to_numpy(zero_copy_only=False).astype(
        np.float64, copy=False
    )
  except (pa.ArrowException, TypeError, ValueError):
    ef_vals = (
        pd.to_numeric(table[ef_col].to_pandas(), errors="coerce")
        .fillna(0.0)
        .to_numpy(dtype=np.float64)
    )
  if not bool(np.all(np.isfinite(ef_vals))):
    ef_vals = np.where(np.isfinite(ef_vals), ef_vals, 0.0)
  return ef_vals


@dataclasses.dataclass(frozen=True)
class _CoordinateArrays:
  """Raw segment endpoint coordinates and their validity masks."""

  # Segment start longitude as read (float64, may be NaN or out of range).
  lon_start: np.ndarray
  # Segment start latitude as read.
  lat_start: np.ndarray
  # Segment end longitude as read.
  lon_end: np.ndarray
  # Segment end latitude as read.
  lat_end: np.ndarray
  # True where all four coordinates are finite and within bounds.
  valid_mask: np.ndarray
  # True for valid segments whose endpoints coincide.
  zero_len_mask: np.ndarray


def _read_float64_columns(
    table: pa.Table, column_names: Sequence[str]
) -> list[np.ndarray]:
  """Reads columns as float64, falling back to pandas for all of them.

  Args:
    table: Input PyArrow Table.
    column_names: Columns to read.

  Returns:
    One float64 array per column. If Arrow fails to cast any column, every
    column is re-read with `pd.to_numeric(errors="coerce")`.
  """
  try:
    return [
        pc.cast(table[col_name], pa.float64(), safe=False)
        .to_numpy(zero_copy_only=False)
        .astype(np.float64, copy=False)
        for col_name in column_names
    ]
  except (pa.ArrowException, TypeError, ValueError):
    return [
        pd.to_numeric(table[col_name].to_pandas(), errors="coerce").to_numpy(
            dtype=np.float64
        )
        for col_name in column_names
    ]


def _valid_coordinate_mask(
    *,
    lon_s_vals: np.ndarray,
    lat_s_vals: np.ndarray,
    lon_e_vals: np.ndarray,
    lat_e_vals: np.ndarray,
) -> np.ndarray:
  """Returns True where all four coordinates are non-NaN and within bounds."""
  coord_bounds_mask = (
      (lon_s_vals >= -180.0)
      & (lon_s_vals <= 180.0)
      & (lat_s_vals >= -90.0)
      & (lat_s_vals <= 90.0)
      & (lon_e_vals >= -180.0)
      & (lon_e_vals <= 180.0)
      & (lat_e_vals >= -90.0)
      & (lat_e_vals <= 90.0)
  )
  coord_nan_mask = (
      np.isnan(lon_s_vals)
      | np.isnan(lat_s_vals)
      | np.isnan(lon_e_vals)
      | np.isnan(lat_e_vals)
  )
  return (~coord_nan_mask) & coord_bounds_mask


def _zero_length_mask(
    coords: Sequence[np.ndarray],
    valid_coord_mask: np.ndarray,
) -> np.ndarray:
  """Flags valid segments whose endpoints are the same point on the globe.

  Args:
    coords: (lon_start, lat_start, lon_end, lat_end) arrays.
    valid_coord_mask: True where the coordinates are valid.

  Returns:
    True for valid segments with identical endpoints, endpoints on opposite
    sides of the antimeridian at the same latitude, or both endpoints at the
    same pole.
  """
  clean_lon_s, clean_lat_s, clean_lon_e, clean_lat_e = (
      np.where(valid_coord_mask, values, 0.0) for values in coords
  )
  identical_coords = (clean_lon_s == clean_lon_e) & (clean_lat_s == clean_lat_e)
  antimeridian_coincident = (
      np.abs(clean_lon_s - clean_lon_e) >= 360.0 - 1e-6
  ) & (np.abs(clean_lat_s - clean_lat_e) < 1e-6)
  polar_coincident = (
      (clean_lat_s >= 90.0 - 1e-6) & (clean_lat_e >= 90.0 - 1e-6)
  ) | ((clean_lat_s <= -90.0 + 1e-6) & (clean_lat_e <= -90.0 + 1e-6))
  return (
      identical_coords | antimeridian_coincident | polar_coincident
  ) & valid_coord_mask


def _extract_coordinate_arrays(
    table: pa.Table,
    *,
    lon_s_col: str,
    lat_s_col: str,
    lon_e_col: str,
    lat_e_col: str,
) -> _CoordinateArrays:
  """Extracts coordinates and determines bounds, NaN, and zero-length masks."""
  coords = _read_float64_columns(
      table, (lon_s_col, lat_s_col, lon_e_col, lat_e_col)
  )
  lon_s_vals, lat_s_vals, lon_e_vals, lat_e_vals = coords
  valid_coord_mask = _valid_coordinate_mask(
      lon_s_vals=lon_s_vals,
      lat_s_vals=lat_s_vals,
      lon_e_vals=lon_e_vals,
      lat_e_vals=lat_e_vals,
  )
  return _CoordinateArrays(
      lon_start=lon_s_vals,
      lat_start=lat_s_vals,
      lon_end=lon_e_vals,
      lat_end=lat_e_vals,
      valid_mask=valid_coord_mask,
      zero_len_mask=_zero_length_mask(coords, valid_coord_mask),
  )


@dataclasses.dataclass(frozen=True)
class _ResolvedFlightColumns:
  """Resolved column names from a flight segment Arrow table.

  Each field holds the actual (case-preserved) column name in the table.
  """

  # Segment start longitude, degrees in [-180, 180].
  lon_start: str
  # Segment start latitude, degrees in [-90, 90].
  lat_start: str
  # Segment end longitude, degrees in [-180, 180].
  lon_end: str
  # Segment end latitude, degrees in [-90, 90].
  lat_end: str
  # Contrail energy forcing per segment in MJ (numeric; null/NaN read as 0).
  ef: str
  # Segment start time in UTC: Arrow timestamp/date or ISO 8601 string.
  time: str
  # Optional local UTC offset in numeric hours in [-14, 14]; None means UTC.
  tz: str | None
  # Optional departure airport ICAO code (e.g. "KJFK").
  departure_airport: str | None
  # Optional arrival airport ICAO code (e.g. "EGLL").
  arrival_airport: str | None


def _has_required_flight_columns(
    resolved: Mapping[str, str],
    table: pa.Table,
    parquet_path: str,
) -> bool:
  """Checks for coordinate, energy forcing and timestamp columns.

  Args:
    resolved: Logical column key to real column name, from
      `_resolve_segment_columns`.
    table: Input PyArrow Table.
    parquet_path: Source path for diagnostic logging.

  Returns:
    True if all required columns are present; otherwise logs a warning naming
    the first missing group and returns False.
  """
  if not all(key in resolved for key in _REQUIRED_COLUMN_KEYS):
    logging.warning(
        "Shard %s missing coordinate columns. Found: %s",
        parquet_path,
        list(table.column_names),
    )
    return False
  if "ef" not in resolved:
    logging.warning(
        "Shard %s missing energy forcing column. Found: %s",
        parquet_path,
        list(table.column_names),
    )
    return False
  time_col = resolved.get("time")
  if time_col is None or time_col not in table.column_names:
    logging.warning(
        "Shard %s missing timestamp column. Found: %s",
        parquet_path,
        list(table.column_names),
    )
    return False
  return True


def _resolve_flight_table_columns(
    table: pa.Table,
    parquet_path: str,
) -> _ResolvedFlightColumns | None:
  """Resolves required and optional column names from a segment Arrow table.

  Args:
    table: Input PyArrow Table.
    parquet_path: Source path for diagnostic logging.

  Returns:
    Resolved column names, or `None` if any required coordinate, energy forcing,
    or timestamp column is missing.
  """
  resolved, _ = _resolve_segment_columns(table.column_names)
  if not _has_required_flight_columns(resolved, table, parquet_path):
    return None

  return _ResolvedFlightColumns(
      lon_start=resolved["lon_s"],
      lat_start=resolved["lat_s"],
      lon_end=resolved["lon_e"],
      lat_end=resolved["lat_e"],
      ef=resolved["ef"],
      time=resolved["time"],
      tz=resolved.get("tz"),
      departure_airport=resolved.get("dep"),
      arrival_airport=resolved.get("arr"),
  )


@dataclasses.dataclass(frozen=True)
class _TimeArrays:
  """Per-row UTC year/month, local hour, and time validity."""

  # UTC calendar year.
  years: np.ndarray
  # UTC month encoded as `year * 100 + month`.
  months: np.ndarray
  # Local wall-clock hour in [0, 23].
  hours: np.ndarray
  # True where both the timestamp and the timezone offset are valid.
  valid_mask: np.ndarray


def _extract_time_arrays(
    table: pa.Table,
    resolved_cols: _ResolvedFlightColumns,
    parquet_path: str,
) -> _TimeArrays | None:
  """Extracts UTC calendar fields and local hours for every row.

  Args:
    table: Input PyArrow Table.
    resolved_cols: Resolved column names.
    parquet_path: Source path for diagnostic logging.

  Returns:
    Time arrays, or None if the timestamp or timezone offset column has an
    unsupported type.
  """
  ts_res = _extract_timestamp_arrays(table, resolved_cols.time, parquet_path)
  if ts_res is None:
    return None
  years, months, base_hours, valid_time_mask = ts_res

  tz_res = _extract_timezone_offsets(
      table,
      tz_col=resolved_cols.tz,
      num_rows_total=table.num_rows,
      parquet_path=parquet_path,
  )
  if tz_res is None:
    return None
  tz_offsets, valid_tz = tz_res

  # Local solar / wall-clock hour in [0, 23] for diurnal breakdown.
  # Note: `years` and `months` remain UTC to align with global annual/monthly
  # datasets and to prevent boundary drops when Parquet files are partitioned
  # by UTC calendar year.
  return _TimeArrays(
      years=years,
      months=months,
      hours=((base_hours + tz_offsets) % 24).astype(np.int32),
      valid_mask=valid_time_mask & valid_tz,
  )


@dataclasses.dataclass(frozen=True)
class _RowValidation:
  """Per-reason drop counts and the year-filtered time mask."""

  # Time validity with rows outside the expected year cleared.
  valid_time_mask: np.ndarray
  invalid_coords_count: int
  invalid_times_count: int
  outside_year_count: int
  zero_len_count: int

  @property
  def all_valid(self) -> bool:
    """True when no row is dropped for any reason."""
    return (
        self.invalid_coords_count == 0
        and self.invalid_times_count == 0
        and self.outside_year_count == 0
        and self.zero_len_count == 0
    )


def _validate_rows(
    coords: _CoordinateArrays,
    times: _TimeArrays,
    *,
    expected_year: int | None,
) -> _RowValidation:
  """Counts rows dropped for bad coordinates, times, year, or zero length.

  Args:
    coords: Coordinate arrays and masks.
    times: Time arrays and validity mask.
    expected_year: Optional target calendar year.

  Returns:
    Drop counts and the time mask with out-of-year rows cleared.
  """
  valid_coord_mask = coords.valid_mask
  valid_time_mask = times.valid_mask
  invalid_coords_count = int(np.count_nonzero(~valid_coord_mask))
  invalid_times_count = int(np.count_nonzero(~valid_time_mask))
  if expected_year is not None:
    outside_year_mask = valid_time_mask & (times.years != int(expected_year))
    outside_year_count = int(np.count_nonzero(outside_year_mask))
    valid_time_mask = valid_time_mask & (~outside_year_mask)
  else:
    outside_year_count = 0
  zero_len_count = int(
      np.count_nonzero(
          valid_coord_mask & valid_time_mask & coords.zero_len_mask
      )
  )
  return _RowValidation(
      valid_time_mask=valid_time_mask,
      invalid_coords_count=invalid_coords_count,
      invalid_times_count=invalid_times_count,
      outside_year_count=outside_year_count,
      zero_len_count=zero_len_count,
  )


def _valid_row_indices(
    validation: _RowValidation,
    *,
    coords: _CoordinateArrays,
    parquet_path: str,
    num_rows_total: int,
) -> np.ndarray | None:
  """Returns the indices of rows that pass validation.

  Args:
    validation: Drop counts and year-filtered time mask.
    coords: Coordinate validity and zero-length masks.
    parquet_path: Source path for diagnostic logging.
    num_rows_total: Number of rows in the table, for logging.

  Returns:
    None when every row is valid (no filtering needed); otherwise the indices
    of the valid rows, after logging the drop counts.
  """
  if validation.all_valid:
    return None
  logging.info(
      "Shard %s filtered %d invalid coordinates, %d invalid timestamps, "
      "%d outside-year segments, %d zero-length segments out of %d total"
      " rows.",
      parquet_path,
      validation.invalid_coords_count,
      validation.invalid_times_count,
      validation.outside_year_count,
      validation.zero_len_count,
      num_rows_total,
  )
  valid_mask = (
      coords.valid_mask & validation.valid_time_mask & (~coords.zero_len_mask)
  )
  return np.nonzero(valid_mask)[0]


def _take_rows(
    values: np.ndarray, valid_indices: np.ndarray | None
) -> np.ndarray:
  """Returns `values` unchanged if `valid_indices` is None, else the subset."""
  return values if valid_indices is None else values[valid_indices]


def _extract_route_countries(
    table: pa.Table,
    *,
    resolved_cols: _ResolvedFlightColumns,
    valid_indices: np.ndarray | None,
    airports_map: Mapping[str, str],
) -> tuple[np.ndarray, np.ndarray]:
  """Maps departure and arrival airports of the kept rows to ISO-2 codes.

  Args:
    table: Input PyArrow Table.
    resolved_cols: Resolved column names.
    valid_indices: Rows to keep, or None to keep every row.
    airports_map: Mapping from airport ICAO codes to ISO-2 country codes.

  Returns:
    Tuple of (departure countries, arrival countries) object arrays.
  """
  num_valid = table.num_rows if valid_indices is None else len(valid_indices)
  dep_countries = _extract_airport_countries(
      table,
      col_name=resolved_cols.departure_airport,
      valid_indices=valid_indices,
      num_valid=num_valid,
      airports_map=airports_map,
  )
  arr_countries = _extract_airport_countries(
      table,
      col_name=resolved_cols.arrival_airport,
      valid_indices=valid_indices,
      num_valid=num_valid,
      airports_map=airports_map,
  )
  return dep_countries, arr_countries


def _build_filtered_flight_data(
    *,
    table: pa.Table,
    resolved_cols: _ResolvedFlightColumns,
    airports_map: Mapping[str, str],
    parquet_path: str,
    expected_year: int | None,
    coords: _CoordinateArrays,
    times: _TimeArrays,
    ef_vals: np.ndarray,
) -> ExtractedFlightData:
  """Filters invalid/out-of-year rows and constructs `ExtractedFlightData`."""
  validation = _validate_rows(coords, times, expected_year=expected_year)
  valid_indices = _valid_row_indices(
      validation,
      coords=coords,
      parquet_path=parquet_path,
      num_rows_total=table.num_rows,
  )
  dep_countries, arr_countries = _extract_route_countries(
      table,
      resolved_cols=resolved_cols,
      valid_indices=valid_indices,
      airports_map=airports_map,
  )

  return ExtractedFlightData(
      lon_start=_take_rows(coords.lon_start, valid_indices),
      lat_start=_take_rows(coords.lat_start, valid_indices),
      lon_end=_take_rows(coords.lon_end, valid_indices),
      lat_end=_take_rows(coords.lat_end, valid_indices),
      ef=_take_rows(ef_vals, valid_indices),
      months=_take_rows(times.months, valid_indices),
      hours=_take_rows(times.hours, valid_indices),
      dep_countries=dep_countries,
      arr_countries=arr_countries,
      invalid_coords_count=validation.invalid_coords_count,
      invalid_times_count=validation.invalid_times_count,
      zero_len_count=validation.zero_len_count,
      outside_year_count=validation.outside_year_count,
  )


def extract_flight_arrays(
    table: pa.Table,
    *,
    airports_map: Mapping[str, str],
    parquet_path: str = "",
    expected_year: int | None = None,
) -> ExtractedFlightData | None:
  """Extracts and validates columnar arrays from a flight segment Arrow table.

  Args:
    table: PyArrow Table containing segment columns.
    airports_map: Mapping from airport ICAO codes to ISO-2 country codes.
    parquet_path: Optional source path used for diagnostic logging.
    expected_year: Optional target calendar year; rows outside it are filtered.

  Returns:
    Extracted columnar data, or None if the coordinate, energy forcing or
    timestamp columns are missing, or the timestamp or timezone offset column
    has an unsupported type. A missing timezone column is treated as UTC. A
    table whose rows all fail validation yields a zero-length result rather
    than None, so the drop counts remain observable.
  """
  resolved_cols = _resolve_flight_table_columns(table, parquet_path)
  if resolved_cols is None:
    return None

  times = _extract_time_arrays(table, resolved_cols, parquet_path)
  if times is None:
    return None

  ef_vals = _extract_energy_forcing(table, resolved_cols.ef)
  coords = _extract_coordinate_arrays(
      table,
      lon_s_col=resolved_cols.lon_start,
      lat_s_col=resolved_cols.lat_start,
      lon_e_col=resolved_cols.lon_end,
      lat_e_col=resolved_cols.lat_end,
  )

  return _build_filtered_flight_data(
      table=table,
      resolved_cols=resolved_cols,
      airports_map=airports_map,
      parquet_path=parquet_path,
      expected_year=expected_year,
      coords=coords,
      times=times,
      ef_vals=ef_vals,
  )


def rasterize_flight_grid_slices(
    flight_data: ExtractedFlightData,
    grid_config: grid_rasterizer.GridConfig,
) -> list[
    tuple[
        tuple[str, int, int],
        tuple[str, np.ndarray, np.ndarray, np.ndarray],
    ]
]:
  """Rasterizes flight segments into sparse grid slices.

  Args:
    flight_data: Validated columnar arrays of segment positions and attributes.
    grid_config: Configuration for grid dimensions and bounds.

  Returns:
    List of (key, slice_payload) tuples where slice_payload is
    ("sparse", nz_rows, nz_cols, nz_vals).
  """
  rasterized_grids = grid_rasterizer.rasterize_segments_arrays(
      xs_all=flight_data.lon_start,
      ys_all=flight_data.lat_start,
      xe_all=flight_data.lon_end,
      ye_all=flight_data.lat_end,
      ef_all=flight_data.ef,
      months_all=flight_data.months,
      hours_all=flight_data.hours,
      config=grid_config,
  )
  slices = []
  for key, (nz_rows, nz_cols, nz_vals) in rasterized_grids.items():
    if nz_rows.size == 0:
      continue
    slice_payload = (
        constants.GRID_ACC_SPARSE,
        nz_rows,
        nz_cols,
        nz_vals,
    )
    slices.append((key, slice_payload))
  return slices


def _resolve_iso_id(
    raw_item: Any,
    *,
    iso_to_id: MutableMapping[str, int],
    id_to_iso: MutableSequence[str],
    raw_cache: MutableMapping[Any, int],
) -> int:
  """Returns the integer ID for one raw country value, registering new ISOs.

  Args:
    raw_item: Raw country value (ISO string, None, NaN, or other scalar).
    iso_to_id: ISO code to ID mapping; extended with unseen codes.
    id_to_iso: ID to ISO code list; extended with unseen codes.
    raw_cache: Raw value to ID cache; updated for non-missing values.

  Returns:
    The ISO ID, or 0 for missing or blank values.
  """
  if raw_item is None or (isinstance(raw_item, float) and np.isnan(raw_item)):
    return 0
  if not isinstance(raw_item, str):
    if pd.isna(raw_item):
      return 0
    raw_item = str(raw_item)
  cleaned_code = raw_item.strip().upper()
  if not cleaned_code:
    raw_cache[raw_item] = 0
    return 0
  iso_id = iso_to_id.get(cleaned_code)
  if iso_id is None:
    iso_id = len(id_to_iso)
    iso_to_id[cleaned_code] = iso_id
    id_to_iso.append(cleaned_code)
  raw_cache[raw_item] = iso_id
  return iso_id


def _cached_iso_id(
    raw_item: Any,
    *,
    iso_to_id: MutableMapping[str, int],
    id_to_iso: MutableSequence[str],
    raw_cache: MutableMapping[Any, int],
) -> int:
  """Returns the cached ID for `raw_item`, resolving it on a cache miss."""
  if (cached_id := raw_cache.get(raw_item)) is not None:
    return cached_id
  return _resolve_iso_id(
      raw_item, iso_to_id=iso_to_id, id_to_iso=id_to_iso, raw_cache=raw_cache
  )


def _encode_iso_column_via_dictionary(
    raw_countries: np.ndarray | Sequence[Any],
    *,
    iso_to_id: MutableMapping[str, int],
    id_to_iso: MutableSequence[str],
    raw_cache: MutableMapping[Any, int],
) -> np.ndarray:
  """Encodes country values by resolving each distinct value only once.

  Args:
    raw_countries: Raw country values per segment.
    iso_to_id: ISO code to ID mapping; extended with unseen codes.
    id_to_iso: ID to ISO code list; extended with unseen codes.
    raw_cache: Raw value to ID cache.

  Returns:
    Int32 ISO IDs per segment (0 for missing).

  Raises:
    pa.ArrowException, TypeError, ValueError: If the values cannot be
      dictionary-encoded by Arrow.
  """
  arrow_array: Any = pa.array(raw_countries, from_pandas=True)
  if pa.types.is_null(arrow_array.type) or arrow_array.null_count == len(
      arrow_array
  ):
    return np.zeros(len(raw_countries), dtype=np.int32)
  dict_arr: Any = arrow_array.dictionary_encode()
  dictionary_values = dict_arr.dictionary.to_pylist()
  num_keys = len(dictionary_values)
  lookup = np.zeros(num_keys + 1, dtype=np.int32)
  for index, raw_item in enumerate(dictionary_values):
    lookup[index] = _cached_iso_id(
        raw_item, iso_to_id=iso_to_id, id_to_iso=id_to_iso, raw_cache=raw_cache
    )
  return lookup[_dictionary_indices(dict_arr, null_index=num_keys)]


def _encode_iso_column(
    raw_countries: np.ndarray | Sequence[Any],
    *,
    iso_to_id: MutableMapping[str, int],
    id_to_iso: MutableSequence[str],
    raw_cache: MutableMapping[Any, int],
) -> np.ndarray:
  """Encodes country ISO strings into compact integer IDs (0 for missing)."""
  if len(raw_countries) >= 64:
    try:
      return _encode_iso_column_via_dictionary(
          raw_countries,
          iso_to_id=iso_to_id,
          id_to_iso=id_to_iso,
          raw_cache=raw_cache,
      )
    except (pa.ArrowException, TypeError, ValueError):
      pass

  return np.asarray(
      [
          _cached_iso_id(
              country_value,
              iso_to_id=iso_to_id,
              id_to_iso=id_to_iso,
              raw_cache=raw_cache,
          )
          for country_value in raw_countries
      ],
      dtype=np.int32,
  )


def _intern_iso(
    country_iso: str,
    *,
    iso_to_id: MutableMapping[str, int],
    id_to_iso: MutableSequence[str],
) -> int:
  """Returns the integer ID of `country_iso`, assigning a new one if unseen.

  Args:
    country_iso: Normalized ISO code; empty maps to the reserved ID 0.
    iso_to_id: ISO code to ID mapping, extended in place.
    id_to_iso: ID to ISO code list, extended in place.

  Returns:
    The ID of `country_iso`.
  """
  if not country_iso:
    return 0
  iso_id = iso_to_id.get(country_iso)
  if iso_id is None:
    iso_id = len(id_to_iso)
    iso_to_id[country_iso] = iso_id
    id_to_iso.append(country_iso)
  return iso_id


def _build_country_entity_table(
    country_metadata: Sequence[Mapping[str, Any]],
    *,
    country_names_by_iso: Mapping[str, str],
    iso_to_id: MutableMapping[str, int],
    id_to_iso: MutableSequence[str],
) -> tuple[list[tuple[str, str]], np.ndarray, np.ndarray]:
  """Maps country polygons to deduplicated (c_name, c_iso) entity IDs."""
  c_entities: list[tuple[str, str]] = []
  country_entity_isos: list[int] = []
  c_entity_map: dict[tuple[str, str], int] = {}
  c_poly_to_entity = np.empty(len(country_metadata), dtype=np.int32)

  for p_idx, meta in enumerate(country_metadata):
    country_iso = str(meta["id"]).strip().upper()
    raw_name = str(meta[constants.KEY_NAME])
    c_name = (
        raw_name
        if country_iso.startswith("-")
        else country_names_by_iso.get(country_iso, raw_name)
    )
    entity_pair = (c_name, country_iso)
    e_idx = c_entity_map.get(entity_pair)
    if e_idx is None:
      e_idx = len(c_entities)
      c_entity_map[entity_pair] = e_idx
      c_entities.append(entity_pair)
      country_entity_isos.append(
          _intern_iso(country_iso, iso_to_id=iso_to_id, id_to_iso=id_to_iso)
      )
    c_poly_to_entity[p_idx] = e_idx

  return (
      c_entities,
      np.asarray(country_entity_isos, dtype=np.int32),
      c_poly_to_entity,
  )


def _build_fir_entity_table(
    fir_metadata: Sequence[Mapping[str, Any]],
) -> tuple[list[tuple[str, str, str, str]], np.ndarray]:
  """Maps FIR polygons to deduplicated (firname, icao, iso2, type) IDs."""
  f_entities: list[tuple[str, str, str, str]] = []
  f_entity_map: dict[tuple[str, str, str, str], int] = {}
  f_poly_to_entity = np.empty(len(fir_metadata), dtype=np.int32)

  for p_idx, meta in enumerate(fir_metadata):
    properties = meta.get(constants.GEOJSON_PROPERTIES, {})
    fir_icao = (
        str(properties.get(constants.KEY_FIR_ICAO) or meta["id"])
        .strip()
        .upper()
    )
    firname = constants.clean_fir_name(
        str(properties.get(constants.KEY_FIRNAME) or meta[constants.KEY_NAME]),
        fir_icao,
    )
    fir_iso2 = str(
        properties.get(constants.KEY_FIR_TO_COUNTRY_ISO2S) or ""
    ).strip()
    fir_type = (
        str(properties.get(constants.KEY_TYPE) or constants.FIR_TYPE_DOMESTIC)
        .strip()
        .lower()
    )
    fir_entity = (firname, fir_icao, fir_iso2, fir_type)
    e_idx = f_entity_map.get(fir_entity)
    if e_idx is None:
      e_idx = len(f_entities)
      f_entity_map[fir_entity] = e_idx
      f_entities.append(fir_entity)
    f_poly_to_entity[p_idx] = e_idx

  return f_entities, f_poly_to_entity


class _PairProportions(NamedTuple):
  """Intersection proportions keyed by unique (segment, entity) pair."""

  # Segment index of each pair, sorted by (segment, entity).
  seg_idx: np.ndarray
  # Entity index of each pair.
  entity_idx: np.ndarray
  # Summed proportion of each pair, at most 1.0.
  props: np.ndarray


def _sum_proportions_per_pair(
    *,
    hit_seg_idx: np.ndarray,
    hit_entity_idx: np.ndarray,
    hit_props: np.ndarray,
) -> _PairProportions:
  """Sorts hits by (segment, entity) and sums proportions of duplicate pairs.

  Args:
    hit_seg_idx: Segment index of each intersection hit.
    hit_entity_idx: Entity index of each intersection hit.
    hit_props: Weighted intersection proportion of each hit.

  Returns:
    One entry per unique (segment, entity) pair with its proportion clamped to
    at most 1.0.
  """
  pair_key = (hit_seg_idx.astype(np.int64) << 32) | hit_entity_idx.astype(
      np.int64
  )
  if len(pair_key) > 1 and bool(np.any(pair_key[1:] < pair_key[:-1])):
    order = np.argsort(pair_key, kind="stable")
    pair_key = pair_key[order]
    hit_seg_idx = hit_seg_idx[order]
    hit_entity_idx = hit_entity_idx[order]
    hit_props = hit_props[order]

  if len(pair_key) > 1:
    dup_mask = pair_key[1:] == pair_key[:-1]
    if bool(np.any(dup_mask)):
      unique_starts = np.flatnonzero(np.concatenate(([True], ~dup_mask)))
      return _PairProportions(
          seg_idx=hit_seg_idx[unique_starts],
          entity_idx=hit_entity_idx[unique_starts],
          props=np.minimum(1.0, np.add.reduceat(hit_props, unique_starts)),
      )
  return _PairProportions(
      seg_idx=hit_seg_idx,
      entity_idx=hit_entity_idx,
      props=np.minimum(1.0, hit_props),
  )


def _dedup_and_clamp_intersections(
    *,
    sub_idx: np.ndarray,
    poly_idx: np.ndarray,
    proportions: np.ndarray,
    valid_geom_indices: np.ndarray,
    all_seg_idx: np.ndarray,
    all_weights: np.ndarray,
    poly_to_entity: np.ndarray,
) -> _PairProportions:
  """Sums proportional intersections per (segment, entity) and clamps to 1.0.

  When a segment intersects multiple entities such that the sum of proportions
  across entities exceeds 1.0 (for example, lying along a shared boundary
  between two adjacent polygons or crossing slightly overlapping boundaries),
  the proportions are normalized so that their sum does not exceed 1.0.

  Args:
    sub_idx: Indices of query linestrings in `valid_geom_indices`.
    poly_idx: Indices of polygons intersected in the spatial index.
    proportions: Array of intersection length proportions relative to query
      pieces.
    valid_geom_indices: Mapping from query linestring index to split-piece index
      in `all_seg_idx` and `all_weights`.
    all_seg_idx: Full segment indices.
    all_weights: Sub-piece weighting (e.g. for antimeridian-split pieces).
    poly_to_entity: Mapping from spatial index polygon ID to entity index.

  Returns:
    `_PairProportions` of `(seg_idx, entity_idx, props)`.
  """
  if sub_idx.size == 0:
    return _PairProportions(
        seg_idx=np.empty(0, dtype=all_seg_idx.dtype),
        entity_idx=np.empty(0, dtype=poly_to_entity.dtype),
        props=np.empty(0, dtype=proportions.dtype),
    )

  piece_idx = valid_geom_indices[sub_idx]
  pairs = _sum_proportions_per_pair(
      hit_seg_idx=all_seg_idx[piece_idx],
      hit_entity_idx=poly_to_entity[poly_idx],
      hit_props=proportions * all_weights[piece_idx],
  )
  seg_idx_u = pairs.seg_idx
  total_props = pairs.props

  # Normalize across entities if total proportion for a segment exceeds 1.0
  # (e.g. when a segment lies on a shared boundary between two entities).
  if len(seg_idx_u) > 1:
    seg_totals = np.bincount(seg_idx_u, weights=total_props)
    norm_factor = np.maximum(1.0, seg_totals)[seg_idx_u]
    if bool(np.any(norm_factor > 1.0)):
      total_props = total_props / norm_factor

  return _PairProportions(
      seg_idx=seg_idx_u, entity_idx=pairs.entity_idx, props=total_props
  )


def _compute_unique_month_hours(
    flight_data: ExtractedFlightData,
    num_valid: int,
) -> tuple[np.ndarray, list[tuple[int, int]], int]:
  """Computes unique (month, hour) pairs and per-segment indices."""
  first_m = int(flight_data.months[0])
  first_h = int(flight_data.hours[0])
  if bool(
      np.all(flight_data.months == first_m)
      and np.all(flight_data.hours == first_h)
  ):
    unique_mh_pairs = [(first_m, first_h)]
    seg_mh_idx = np.zeros(num_valid, dtype=np.int64)
  else:
    mh_keys = geo_utils.pack_month_hour_keys(
        flight_data.months, flight_data.hours
    )
    u_mh_keys, seg_mh_idx = np.unique(mh_keys, return_inverse=True)
    seg_mh_idx = seg_mh_idx.astype(np.int64)
    unique_mh_pairs = geo_utils.unpack_month_hour_keys(u_mh_keys)
  num_mh = len(unique_mh_pairs)
  return seg_mh_idx, unique_mh_pairs, num_mh


@dataclasses.dataclass(slots=True)
class CountryShardAccumulator:
  """Mutable per-shard accumulator for country-level segment metrics."""

  total_segment_count: int = 0
  contrail_segment_count: int = 0
  prop_sum_ef_mj: float = 0.0
  local_flight_impact: float = 0.0
  total_flight_impact: float = 0.0
  outgoing_flight_impact: float = 0.0
  total_flight_distance_km: float = 0.0
  contrail_flight_distance_km: float = 0.0

  def to_metrics_tuple(self) -> constants.CountryMetricsTuple:
    """Converts accumulated metrics to a canonical CountryMetricsTuple."""
    return (
        self.total_segment_count,
        self.contrail_segment_count,
        self.prop_sum_ef_mj,
        self.local_flight_impact,
        self.total_flight_impact,
        self.outgoing_flight_impact,
        self.total_flight_distance_km,
        self.contrail_flight_distance_km,
    )


@dataclasses.dataclass(slots=True)
class FirShardAccumulator:
  """Mutable per-shard accumulator for FIR-level segment metrics."""

  total_segment_count: int = 0
  contrail_segment_count: int = 0
  prop_sum_ef_mj: float = 0.0
  total_flight_distance_km: float = 0.0
  contrail_flight_distance_km: float = 0.0

  def to_metrics_tuple(self) -> constants.FirMetricsTuple:
    """Converts accumulated metrics to a canonical FirMetricsTuple."""
    return (
        self.total_segment_count,
        self.contrail_segment_count,
        self.prop_sum_ef_mj,
        self.total_flight_distance_km,
        self.contrail_flight_distance_km,
    )


def _attribute_outgoing_flights(
    *,
    dep_iso_ids: np.ndarray,
    has_contrail: np.ndarray,
    ef_clean: np.ndarray,
    num_mh: int,
    seg_mh_idx: np.ndarray,
    id_to_iso: Sequence[str],
    country_names_by_iso: Mapping[str, str],
    unique_mh_pairs: Sequence[tuple[int, int]],
    country_accum: MutableMapping[
        constants.CountryKey, CountryShardAccumulator
    ],
) -> None:
  """Attributes outgoing flight impacts to departure countries."""
  out_mask = (dep_iso_ids > 0) & has_contrail
  if not bool(np.any(out_mask)):
    return
  out_idx = np.flatnonzero(out_mask)
  out_group_id = (
      dep_iso_ids[out_idx].astype(np.int64) * num_mh + seg_mh_idx[out_idx]
  )
  u_out_groups, out_inv = np.unique(out_group_id, return_inverse=True)
  out_ef_sums = np.bincount(out_inv, weights=ef_clean[out_idx])
  for g_idx, grp_val in enumerate(u_out_groups):
    grp = int(grp_val)
    iso_id = grp // num_mh
    mh_idx = grp % num_mh
    d_iso = id_to_iso[iso_id]
    dep_name = country_names_by_iso.get(d_iso, d_iso)
    m_val, h_val = unique_mh_pairs[mh_idx]
    key = (dep_name, d_iso, m_val, h_val)
    country_accum[key] = CountryShardAccumulator(
        outgoing_flight_impact=float(out_ef_sums[g_idx])
    )


@dataclasses.dataclass(frozen=True)
class _SegmentPieces:
  """Segment pieces after splitting antimeridian crossings.

  Non-crossing segments come first, followed by the first and then the second
  piece of every crossing segment.
  """

  # Piece start longitude, degrees in [-180, 180].
  lon_start: np.ndarray
  # Piece start latitude, degrees.
  lat_start: np.ndarray
  # Piece end longitude, degrees in [-180, 180].
  lon_end: np.ndarray
  # Piece end latitude, degrees.
  lat_end: np.ndarray
  # Index of the originating segment in the flight data for each piece.
  seg_idx: np.ndarray
  # Fraction of the originating segment covered by each piece.
  weights: np.ndarray


def _split_crossing_segments(
    flight_data: ExtractedFlightData,
    *,
    crosses: np.ndarray,
) -> _SegmentPieces:
  """Splits every crossing segment into two pieces at the antimeridian.

  Args:
    flight_data: Validated segment arrays.
    crosses: True for segments that cross the antimeridian.

  Returns:
    Pieces for the non-crossing segments followed by both halves of every
    crossing segment, weighted by the fraction of the segment they cover.
  """
  xs_c = flight_data.lon_start[crosses]
  ys_c = flight_data.lat_start[crosses]
  xe_c = flight_data.lon_end[crosses]
  ye_c = flight_data.lat_end[crosses]

  eastbound, fraction = geo_utils.antimeridian_crossing_fraction(xs_c, xe_c)
  ymid = ys_c + fraction * (ye_c - ys_c)

  x_split1 = np.where(eastbound, 180.0, -180.0)
  x_split2 = np.where(eastbound, -180.0, 180.0)

  nc_mask = ~crosses
  nc_indices = np.nonzero(nc_mask)[0]
  c_indices = np.nonzero(crosses)[0]

  w_nc = np.ones(len(nc_indices), dtype=np.float64)
  return _SegmentPieces(
      lon_start=np.concatenate(
          [flight_data.lon_start[nc_mask], xs_c, x_split2]
      ),
      lat_start=np.concatenate([flight_data.lat_start[nc_mask], ys_c, ymid]),
      lon_end=np.concatenate([flight_data.lon_end[nc_mask], x_split1, xe_c]),
      lat_end=np.concatenate([flight_data.lat_end[nc_mask], ymid, ye_c]),
      seg_idx=np.concatenate([nc_indices, c_indices, c_indices]),
      weights=np.concatenate([w_nc, fraction, 1.0 - fraction]),
  )


def _split_antimeridian_segments(
    flight_data: ExtractedFlightData,
    num_valid: int,
) -> _SegmentPieces:
  """Detects and splits segments crossing the antimeridian."""
  crosses = np.abs(flight_data.lon_end - flight_data.lon_start) > 180.0

  if np.any(crosses):
    return _split_crossing_segments(flight_data, crosses=crosses)
  return _SegmentPieces(
      lon_start=flight_data.lon_start,
      lat_start=flight_data.lat_start,
      lon_end=flight_data.lon_end,
      lat_end=flight_data.lat_end,
      seg_idx=np.arange(num_valid, dtype=np.int64),
      weights=np.ones(num_valid, dtype=np.float64),
  )


@dataclasses.dataclass(frozen=True)
class _PreparedSegmentAttributionInputs:
  """Sanitized segment scalars, month-hour groups, and encoded routes."""

  # Per-segment great-circle length in km; non-finite or negative set to 0.
  seg_km_clean: np.ndarray
  # Per-segment energy forcing in MJ; non-finite values set to 0.
  ef_clean: np.ndarray
  # Per-segment bool mask: True where `ef_clean` is non-zero.
  has_contrail: np.ndarray
  # Per-segment index into `unique_mh_pairs`.
  seg_mh_idx: np.ndarray
  # Distinct (UTC month, local hour) pairs present in the shard.
  unique_mh_pairs: list[tuple[int, int]]
  # Number of distinct month-hour pairs, i.e. `len(unique_mh_pairs)`.
  num_mh: int
  # ISO2 code to dense integer id; "" (unknown) maps to 0.
  iso_to_id: dict[str, int]
  # Inverse of `iso_to_id`: dense integer id to ISO2 code.
  id_to_iso: list[str]
  # Per-segment departure-country id (0 if unknown).
  dep_iso_ids: np.ndarray
  # Per-segment arrival-country id (0 if unknown).
  arr_iso_ids: np.ndarray
  # Per-segment bool mask: both departure and arrival countries are known.
  has_valid_route: np.ndarray


@dataclasses.dataclass(frozen=True)
class _SegmentGeometry:
  """Projected linestrings for the non-degenerate segment pieces."""

  # Shapely linestrings in the index projection, one per valid piece.
  lines_array: np.ndarray
  # Projected length of each linestring in `lines_array`.
  valid_lengths: np.ndarray
  # Index into the split pieces for each linestring in `lines_array`.
  valid_geom_indices: np.ndarray
  # Originating segment index for every split piece.
  all_seg_idx: np.ndarray
  # Fraction of the originating segment covered by every split piece.
  all_weights: np.ndarray


@dataclasses.dataclass(frozen=True)
class _GroupedIntersectionSums:
  """Intersection metrics summed per (entity, month-hour) group."""

  # Per-hit energy forcing in MJ scaled by the intersected proportion.
  prop_ef: np.ndarray
  # Sorted unique group ids, `entity_idx * num_mh + month_hour_idx`.
  groups: np.ndarray
  # Per-hit index into `groups`.
  inverse: np.ndarray
  # Number of intersecting segments per group.
  segment_counts: np.ndarray
  # Number of intersecting segments with a contrail per group.
  contrail_counts: np.ndarray
  # Proportioned energy forcing in MJ per group.
  sum_ef_mj: np.ndarray
  # Proportioned flight distance in km per group.
  sum_km: np.ndarray
  # Proportioned contrail-forming flight distance in km per group.
  sum_contrail_km: np.ndarray


def _group_intersection_sums(
    pairs: _PairProportions,
    inputs: _PreparedSegmentAttributionInputs,
) -> _GroupedIntersectionSums:
  """Proportions per-segment metrics and sums them per entity and month-hour.

  Args:
    pairs: Deduplicated (segment, entity) hits with their proportions.
    inputs: Sanitized per-segment metrics and month-hour groups.

  Returns:
    Per-group sums together with the grouping needed for further sums.
  """
  seg_idx = pairs.seg_idx
  prop_ef = inputs.ef_clean[seg_idx] * pairs.props
  prop_km = inputs.seg_km_clean[seg_idx] * pairs.props
  contrail_mask = inputs.has_contrail[seg_idx]
  contrail_km = np.where(contrail_mask, prop_km, 0.0)

  group_id = (
      pairs.entity_idx.astype(np.int64) * inputs.num_mh
      + inputs.seg_mh_idx[seg_idx]
  )
  groups, inverse = np.unique(group_id, return_inverse=True)
  return _GroupedIntersectionSums(
      prop_ef=prop_ef,
      groups=groups,
      inverse=inverse,
      segment_counts=np.bincount(inverse),
      contrail_counts=np.bincount(
          inverse[contrail_mask], minlength=len(groups)
      ),
      sum_ef_mj=np.bincount(inverse, weights=prop_ef),
      sum_km=np.bincount(inverse, weights=prop_km),
      sum_contrail_km=np.bincount(inverse, weights=contrail_km),
  )


def _country_route_impact_sums(
    *,
    seg_idx: np.ndarray,
    hit_iso_ids: np.ndarray,
    sums: _GroupedIntersectionSums,
    inputs: _PreparedSegmentAttributionInputs,
) -> tuple[np.ndarray, np.ndarray]:
  """Sums local and total flight impact per country group.

  Args:
    seg_idx: Segment index of each deduplicated (segment, country) hit.
    hit_iso_ids: Encoded ISO id of the country of each hit.
    sums: Grouping and proportioned energy forcing of the hits.
    inputs: Encoded departure/arrival countries per segment.

  Returns:
    Tuple of (local impact, total impact) per group. Both only count segments
    with a known route; local impact further requires the country to be the
    departure or arrival country.
  """
  valid_route = inputs.has_valid_route[seg_idx]
  is_local = (inputs.dep_iso_ids[seg_idx] == hit_iso_ids) | (
      inputs.arr_iso_ids[seg_idx] == hit_iso_ids
  )
  local_imp = np.where(valid_route & is_local, sums.prop_ef, 0.0)
  total_imp = np.where(valid_route, sums.prop_ef, 0.0)
  return (
      np.bincount(sums.inverse, weights=local_imp),
      np.bincount(sums.inverse, weights=total_imp),
  )


def _merge_country_intersection_metrics(
    existing: CountryShardAccumulator,
    delta: CountryShardAccumulator,
) -> None:
  """Adds `delta`'s intersection metrics (all but outgoing) into `existing`."""
  existing.total_segment_count += delta.total_segment_count
  existing.contrail_segment_count += delta.contrail_segment_count
  existing.prop_sum_ef_mj += delta.prop_sum_ef_mj
  existing.local_flight_impact += delta.local_flight_impact
  existing.total_flight_impact += delta.total_flight_impact
  existing.total_flight_distance_km += delta.total_flight_distance_km
  existing.contrail_flight_distance_km += delta.contrail_flight_distance_km


def _accumulate_country_groups(
    *,
    pairs: _PairProportions,
    entities: Sequence[tuple[str, str]],
    entity_iso_ids: np.ndarray,
    inputs: _PreparedSegmentAttributionInputs,
    country_accum: MutableMapping[
        constants.CountryKey, CountryShardAccumulator
    ],
) -> None:
  """Sums country hits per (country, month-hour) into `country_accum`.

  Args:
    pairs: Deduplicated (segment, country entity) hits.
    entities: (name, ISO2) for each country entity index.
    entity_iso_ids: Encoded ISO id for each country entity index.
    inputs: Sanitized per-segment metrics, routes and month-hour groups.
    country_accum: Accumulator to create or update entries in.
  """
  sums = _group_intersection_sums(pairs, inputs)
  sum_local, sum_total = _country_route_impact_sums(
      seg_idx=pairs.seg_idx,
      hit_iso_ids=entity_iso_ids[pairs.entity_idx],
      sums=sums,
      inputs=inputs,
  )
  num_mh = inputs.num_mh
  for g_idx, grp_val in enumerate(sums.groups):
    grp = int(grp_val)
    e_idx = grp // num_mh
    mh_idx = grp % num_mh
    c_name, country_iso = entities[e_idx]
    m_val, h_val = inputs.unique_mh_pairs[mh_idx]
    c_key = (c_name, country_iso, m_val, h_val)
    delta = CountryShardAccumulator(
        total_segment_count=int(sums.segment_counts[g_idx]),
        contrail_segment_count=int(sums.contrail_counts[g_idx]),
        prop_sum_ef_mj=float(sums.sum_ef_mj[g_idx]),
        local_flight_impact=float(sum_local[g_idx]),
        total_flight_impact=float(sum_total[g_idx]),
        outgoing_flight_impact=0.0,
        total_flight_distance_km=float(sums.sum_km[g_idx]),
        contrail_flight_distance_km=float(sums.sum_contrail_km[g_idx]),
    )
    existing = country_accum.get(c_key)
    if existing is None:
      country_accum[c_key] = delta
    else:
      _merge_country_intersection_metrics(existing, delta)


def _attribute_country_intersections(
    *,
    country_index: geo_utils.SpatialIndex,
    geometry: _SegmentGeometry,
    inputs: _PreparedSegmentAttributionInputs,
    country_names_by_iso: Mapping[str, str],
    country_accum: MutableMapping[
        constants.CountryKey, CountryShardAccumulator
    ],
) -> None:
  """Queries country spatial index and aggregates intersection metrics."""
  c_sub_idx, c_poly_idx, c_props = country_index.query_intersections_batch(
      geometry.lines_array, geometry.valid_lengths
  )
  if c_sub_idx.size == 0:
    return

  c_entities, c_entity_iso_ids, c_poly_to_entity = _build_country_entity_table(
      country_index.metadata,
      country_names_by_iso=country_names_by_iso,
      iso_to_id=inputs.iso_to_id,
      id_to_iso=inputs.id_to_iso,
  )
  pairs = _dedup_and_clamp_intersections(
      sub_idx=c_sub_idx,
      poly_idx=c_poly_idx,
      proportions=c_props,
      valid_geom_indices=geometry.valid_geom_indices,
      all_seg_idx=geometry.all_seg_idx,
      all_weights=geometry.all_weights,
      poly_to_entity=c_poly_to_entity,
  )
  _accumulate_country_groups(
      pairs=pairs,
      entities=c_entities,
      entity_iso_ids=c_entity_iso_ids,
      inputs=inputs,
      country_accum=country_accum,
  )


def _attribute_fir_intersections(
    *,
    fir_index: geo_utils.SpatialIndex,
    geometry: _SegmentGeometry,
    inputs: _PreparedSegmentAttributionInputs,
    fir_accum: MutableMapping[constants.FirKey, FirShardAccumulator],
) -> None:
  """Queries FIR spatial index and aggregates intersection metrics."""
  f_sub_idx, f_poly_idx, f_props = fir_index.query_intersections_batch(
      geometry.lines_array, geometry.valid_lengths
  )
  if f_sub_idx.size == 0:
    return

  f_entities, f_poly_to_entity = _build_fir_entity_table(fir_index.metadata)
  pairs = _dedup_and_clamp_intersections(
      sub_idx=f_sub_idx,
      poly_idx=f_poly_idx,
      proportions=f_props,
      valid_geom_indices=geometry.valid_geom_indices,
      all_seg_idx=geometry.all_seg_idx,
      all_weights=geometry.all_weights,
      poly_to_entity=f_poly_to_entity,
  )
  sums = _group_intersection_sums(pairs, inputs)

  num_mh = inputs.num_mh
  for g_idx, grp_val in enumerate(sums.groups):
    grp = int(grp_val)
    e_idx = grp // num_mh
    mh_idx = grp % num_mh
    firname, fir_icao, fir_iso2, fir_type = f_entities[e_idx]
    m_val, h_val = inputs.unique_mh_pairs[mh_idx]
    f_key = (firname, fir_icao, fir_iso2, fir_type, m_val, h_val)
    fir_accum[f_key] = FirShardAccumulator(
        total_segment_count=int(sums.segment_counts[g_idx]),
        contrail_segment_count=int(sums.contrail_counts[g_idx]),
        prop_sum_ef_mj=float(sums.sum_ef_mj[g_idx]),
        total_flight_distance_km=float(sums.sum_km[g_idx]),
        contrail_flight_distance_km=float(sums.sum_contrail_km[g_idx]),
    )


def _clean_segment_lengths_km(flight_data: ExtractedFlightData) -> np.ndarray:
  """Returns great-circle segment lengths in km, with invalid lengths as 0."""
  seg_lengths_km = (
      geo_utils.haversine_meters(
          flight_data.lon_start,
          flight_data.lat_start,
          flight_data.lon_end,
          flight_data.lat_end,
      )
      / 1000.0
  )
  return np.where(
      np.isfinite(seg_lengths_km) & (seg_lengths_km >= 0.0),
      seg_lengths_km,
      0.0,
  )


def _prepare_segment_attribution_inputs(
    flight_data: ExtractedFlightData,
) -> _PreparedSegmentAttributionInputs:
  """Computes segment lengths, month-hour indices, and encoded route ISOs."""
  num_valid = flight_data.count
  seg_km_clean = _clean_segment_lengths_km(flight_data)
  ef_clean = np.where(np.isfinite(flight_data.ef), flight_data.ef, 0.0)
  has_contrail = ef_clean != 0.0

  seg_mh_idx, unique_mh_pairs, num_mh = _compute_unique_month_hours(
      flight_data, num_valid
  )

  iso_to_id: dict[str, int] = {"": 0}
  id_to_iso: list[str] = [""]
  raw_iso_cache: dict[Any, int] = {None: 0, "": 0}

  dep_iso_ids = _encode_iso_column(
      flight_data.dep_countries,
      iso_to_id=iso_to_id,
      id_to_iso=id_to_iso,
      raw_cache=raw_iso_cache,
  )
  arr_iso_ids = _encode_iso_column(
      flight_data.arr_countries,
      iso_to_id=iso_to_id,
      id_to_iso=id_to_iso,
      raw_cache=raw_iso_cache,
  )
  has_valid_route = (dep_iso_ids > 0) & (arr_iso_ids > 0)

  return _PreparedSegmentAttributionInputs(
      seg_km_clean=seg_km_clean,
      ef_clean=ef_clean,
      has_contrail=has_contrail,
      seg_mh_idx=seg_mh_idx,
      unique_mh_pairs=unique_mh_pairs,
      num_mh=num_mh,
      iso_to_id=iso_to_id,
      id_to_iso=id_to_iso,
      dep_iso_ids=dep_iso_ids,
      arr_iso_ids=arr_iso_ids,
      has_valid_route=has_valid_route,
  )


def _project_and_build_segment_linestrings(
    flight_data: ExtractedFlightData,
    transformer: Any,
) -> _SegmentGeometry:
  """Splits antimeridian segments, projects endpoints, and builds lines."""
  pieces = _split_antimeridian_segments(flight_data, flight_data.count)
  xs_proj_s, ys_proj_s = transformer.transform(
      pieces.lon_start, pieces.lat_start
  )
  xs_proj_e, ys_proj_e = transformer.transform(pieces.lon_end, pieces.lat_end)

  dx = xs_proj_e - xs_proj_s
  dy = ys_proj_e - ys_proj_s
  line_lengths = np.hypot(dx, dy)
  valid_geom_mask = np.isfinite(line_lengths) & (line_lengths > 0.0)
  valid_geom_indices = np.nonzero(valid_geom_mask)[0]

  if valid_geom_indices.size == 0:
    return _SegmentGeometry(
        lines_array=np.empty(0, dtype=object),
        valid_lengths=np.empty(0, dtype=np.float64),
        valid_geom_indices=valid_geom_indices,
        all_seg_idx=pieces.seg_idx,
        all_weights=pieces.weights,
    )

  coords = np.empty((len(valid_geom_indices), 2, 2), dtype=np.float64)
  coords[:, 0, 0] = xs_proj_s[valid_geom_indices]
  coords[:, 0, 1] = ys_proj_s[valid_geom_indices]
  coords[:, 1, 0] = xs_proj_e[valid_geom_indices]
  coords[:, 1, 1] = ys_proj_e[valid_geom_indices]
  return _SegmentGeometry(
      lines_array=shapely.linestrings(coords),
      valid_lengths=line_lengths[valid_geom_indices],
      valid_geom_indices=valid_geom_indices,
      all_seg_idx=pieces.seg_idx,
      all_weights=pieces.weights,
  )


def _attribute_intersections(
    flight_data: ExtractedFlightData,
    *,
    inputs: _PreparedSegmentAttributionInputs,
    country_index: geo_utils.SpatialIndex,
    country_names_by_iso: Mapping[str, str],
    fir_index: geo_utils.SpatialIndex,
    country_accum: MutableMapping[
        constants.CountryKey, CountryShardAccumulator
    ],
    fir_accum: MutableMapping[constants.FirKey, FirShardAccumulator],
) -> None:
  """Attributes segments to the countries and FIRs their paths intersect.

  Args:
    flight_data: Non-empty validated segment arrays.
    inputs: Sanitized per-segment metrics, routes and month-hour groups.
    country_index: Spatial index for country boundaries; its transformer also
      projects the segments for the FIR query.
    country_names_by_iso: Mapping of ISO-2 country codes to English names.
    fir_index: Spatial index for Flight Information Regions.
    country_accum: Country accumulator to create or update entries in.
    fir_accum: FIR accumulator to add entries to.
  """
  geometry = _project_and_build_segment_linestrings(
      flight_data, country_index.transformer
  )
  if geometry.valid_geom_indices.size > 0:
    _attribute_country_intersections(
        country_index=country_index,
        geometry=geometry,
        inputs=inputs,
        country_names_by_iso=country_names_by_iso,
        country_accum=country_accum,
    )
    _attribute_fir_intersections(
        fir_index=fir_index,
        geometry=geometry,
        inputs=inputs,
        fir_accum=fir_accum,
    )


def attribute_spatial_segments(
    flight_data: ExtractedFlightData,
    *,
    country_index: geo_utils.SpatialIndex,
    country_names_by_iso: Mapping[str, str],
    fir_index: geo_utils.SpatialIndex,
) -> tuple[
    dict[constants.CountryKey, CountryShardAccumulator],
    dict[constants.FirKey, FirShardAccumulator],
]:
  """Attributes flight segments spatially to countries and FIRs.

  Args:
    flight_data: Validated columnar arrays of segment positions and attributes.
    country_index: Spatial index for country boundaries.
    country_names_by_iso: Mapping of ISO-2 country codes to English names.
    fir_index: Spatial index for Flight Information Regions.

  Returns:
    Tuple of (country_accum, fir_accum) dictionaries with aggregated metrics.
  """
  country_accum: dict[constants.CountryKey, CountryShardAccumulator] = {}
  fir_accum: dict[constants.FirKey, FirShardAccumulator] = {}
  if flight_data.count == 0:
    return country_accum, fir_accum

  inputs = _prepare_segment_attribution_inputs(flight_data)
  _attribute_outgoing_flights(
      dep_iso_ids=inputs.dep_iso_ids,
      has_contrail=inputs.has_contrail,
      ef_clean=inputs.ef_clean,
      num_mh=inputs.num_mh,
      seg_mh_idx=inputs.seg_mh_idx,
      id_to_iso=inputs.id_to_iso,
      country_names_by_iso=country_names_by_iso,
      unique_mh_pairs=inputs.unique_mh_pairs,
      country_accum=country_accum,
  )

  _attribute_intersections(
      flight_data,
      inputs=inputs,
      country_index=country_index,
      country_names_by_iso=country_names_by_iso,
      fir_index=fir_index,
      country_accum=country_accum,
      fir_accum=fir_accum,
  )
  return country_accum, fir_accum


def format_country_records(
    country_accum: Mapping[constants.CountryKey, CountryShardAccumulator],
) -> list[constants.CountryRow]:
  """Converts accumulated country metrics into canonical CountryRow tuples.

  Values are `(total_segment_count, contrail_segment_count, prop_sum_ef_mj,
  local_flight_impact, total_flight_impact, outgoing_flight_impact,
  total_flight_distance_km, contrail_flight_distance_km)`.

  Args:
    country_accum: Mapping of CountryKey to accumulated metric values.

  Returns:
    List of CountryRow tuples.
  """
  return [
      (key, metrics.to_metrics_tuple())
      for key, metrics in country_accum.items()
  ]


def format_fir_records(
    fir_accum: Mapping[constants.FirKey, FirShardAccumulator],
) -> list[constants.FirRow]:
  """Converts accumulated FIR metrics into canonical FirRow tuples.

  Values are `(total_segment_count, contrail_segment_count, prop_sum_ef_mj,
  total_flight_distance_km, contrail_flight_distance_km)`.

  Args:
    fir_accum: Mapping of FirKey to accumulated metric values.

  Returns:
    List of FirRow tuples.
  """
  return [
      (key, metrics.to_metrics_tuple()) for key, metrics in fir_accum.items()
  ]


def _build_shard_result(
    flight_data: ExtractedFlightData,
    context: ProcessWorkerContext,
    grid_config: grid_rasterizer.GridConfig,
) -> ShardProcessingResult:
  """Rasterizes and attributes validated segments into a shard result.

  Args:
    flight_data: Non-empty validated segment arrays for the shard.
    context: Initialized ProcessWorkerContext.
    grid_config: Grid configuration for rasterization.

  Returns:
    A ShardProcessingResult with grid slices, country and FIR records, and the
    shard's drop counts.
  """
  grid_slices = rasterize_flight_grid_slices(flight_data, grid_config)

  country_accum, fir_accum = attribute_spatial_segments(
      flight_data=flight_data,
      country_index=context.country_index,
      country_names_by_iso=context.country_names_by_iso,
      fir_index=context.fir_index,
  )

  country_records = format_country_records(country_accum)
  fir_records = format_fir_records(fir_accum)

  return ShardProcessingResult(
      grid_slices=grid_slices,
      country_records=country_records,
      fir_records=fir_records,
      invalid_coords_count=flight_data.invalid_coords_count,
      invalid_times_count=flight_data.invalid_times_count,
      zero_len_count=flight_data.zero_len_count,
      outside_year_count=flight_data.outside_year_count,
  )


def process_shard(
    shard_input: Any,
    *,
    context: ProcessWorkerContext,
    grid_config: grid_rasterizer.GridConfig,
    expected_year: int | None = None,
) -> ShardProcessingResult:
  """Executes end-to-end shard processing pipeline for a single shard.

  Args:
    shard_input: Shard path or 4-element slice tuple.
    context: Initialized ProcessWorkerContext.
    grid_config: Grid configuration for rasterization.
    expected_year: Optional target calendar year for filtering segments.

  Returns:
    A ShardProcessingResult. It is always returned, even when no records
    were produced; inspect `outcome` (and `is_failure`) to find out why a
    shard emitted nothing. Returning None here previously made an
    unreadable shard indistinguishable from an empty one.
  """
  parsed = parse_shard_input(shard_input)
  if parsed is None:
    logging.error("Unrecognized shard descriptor: %r", shard_input)
    return empty_shard_result(ShardOutcome.MALFORMED_INPUT)
  parquet_path, row_group_idx, row_offset, num_rows = parsed

  table = read_parquet_table(
      parquet_path,
      row_group_idx=row_group_idx,
      row_offset=row_offset,
      num_rows=num_rows,
  )
  if table is None:
    logging.error("Shard %s could not be read.", parquet_path)
    return empty_shard_result(ShardOutcome.READ_FAILED)
  if table.num_rows == 0:
    return empty_shard_result(ShardOutcome.EMPTY_INPUT)

  flight_data = extract_flight_arrays(
      table,
      airports_map=context.airports_map,
      parquet_path=parquet_path,
      expected_year=expected_year,
  )
  if flight_data is None:
    return empty_shard_result(ShardOutcome.MISSING_COLUMNS)
  if flight_data.count == 0:
    return empty_shard_result(
        ShardOutcome.NO_VALID_SEGMENTS,
        invalid_coords_count=flight_data.invalid_coords_count,
        invalid_times_count=flight_data.invalid_times_count,
        zero_len_count=flight_data.zero_len_count,
        outside_year_count=flight_data.outside_year_count,
    )

  return _build_shard_result(flight_data, context, grid_config)
