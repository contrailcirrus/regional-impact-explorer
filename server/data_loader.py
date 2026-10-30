"""Data loading, run-pointer path resolution, and thread-safe caching.

Handles ingestion and indexing of per-country, per-FIR, grid, and temporal
contrails metrics published under one configurable data root.

This module is deliberately free of web-framework imports so that offline
tooling can reuse the loaders. Invalid caller input raises
`DataValidationError`; callers that serve HTTP map it onto a status code. See
the exception handlers in server.py.
"""

import collections
from collections import abc
from concurrent import futures
import functools
import json
import os
import re
import threading
import types
from typing import Any, NamedTuple, TypeAlias, TypedDict, cast

from absl import logging

from common import artifact_io
from common import constants
from common import fs
from common import gcs_backend
from server import json_utils
from server import metrics

_PACKAGE_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_LOCAL_DATA_DIR: str = os.path.join(_PACKAGE_DIR, "data")
DATA_DIR_ENV_VAR = "CONTRAILS_DATA_DIR"

# Root of the published datasets: `{root}/{year}/{dataset}/...`, see
# `artifact_io.resolve_dataset_dir`. The available years are the `{year}`
# directories that hold a per-country export for the configured dataset.
DATA_DIR: str = os.environ.get(DATA_DIR_ENV_VAR, _DEFAULT_LOCAL_DATA_DIR)
_YEAR_DIR_PATTERN = re.compile(r"^[0-9]{4}$")
_GRID_PREFIX_DESCRIPTION = "grid prefix"
_MAX_GRID_CACHE_ENTRIES: int = 128
_MAX_TEMPORAL_CACHE_ENTRIES: int = 256

# Name of the environment variable holding the dataset to serve: the
# `{dataset}` directory under `{root}/{year}/`, i.e. the pipeline's `--dataset`.
DATASET_ENV_VAR = "CONTRAILS_DATASET"
_MAX_DATASET_LENGTH = 64

# Set once at startup by `configure_dataset` / `ensure_dataset_configured`.
# There is deliberately no default: serving a dataset nobody asked for would
# look like "no data" rather than like a configuration error.
_active_dataset: str | None = None


class DataValidationError(Exception):
  """Raised when caller-supplied identifiers fail validation."""


class DatasetConfigError(Exception):
  """Raised when the served dataset is missing or invalid.

  Deliberately not a `RuntimeError` or `DataValidationError`: it is an
  operator mistake, and must not be swallowed by the handlers for bad input
  or missing files.
  """


class ArtifactUnavailableError(Exception):
  """Raised when a required artifact is missing, unreadable, or malformed.

  The caller's request was valid, but the data behind it cannot be served, so
  HTTP callers map this onto 503. Deliberately not a `RuntimeError`, so that
  handlers for it cannot swallow unrelated runtime failures.
  """


def _is_valid_dataset_token(dataset: str) -> bool:
  """Reports whether `dataset` is a non-empty, path-safe ASCII token."""
  return (
      bool(dataset)
      and dataset.isascii()
      and len(dataset) <= _MAX_DATASET_LENGTH
      and all(char.isalnum() or char in ("-", "_") for char in dataset)
  )


def configure_dataset(dataset: str) -> str:
  """Sets the dataset the server reads and returns the cleaned name.

  Args:
    dataset: The `{dataset}` directory name under `{DATA_DIR}/{year}/`.

  Returns:
    The stripped dataset name.

  Raises:
    DatasetConfigError: If `dataset` is empty or not a safe path token. The
      previously configured dataset, if any, is kept.
  """
  global _active_dataset
  dataset_clean = (dataset or "").strip()
  if not _is_valid_dataset_token(dataset_clean):
    raise DatasetConfigError(
        f"Invalid dataset name {dataset!r}: use at most"
        f" {_MAX_DATASET_LENGTH} letters, digits, '-' or '_'."
    )
  _active_dataset = dataset_clean
  return dataset_clean


def configure_dataset_from_env(
    environ: abc.Mapping[str, str] | None = None,
) -> str:
  """Configures the dataset from `CONTRAILS_DATASET`.

  Args:
    environ: The environment to read; defaults to `os.environ`.

  Returns:
    The configured dataset name.

  Raises:
    DatasetConfigError: If the variable is unset, blank or invalid.
  """
  env = os.environ if environ is None else environ
  value = env.get(DATASET_ENV_VAR, "").strip()
  if not value:
    raise DatasetConfigError(
        f"{DATASET_ENV_VAR} is not set. Set it to the name of the dataset to"
        " serve: the directory under"
        f" {os.path.join(DATA_DIR, '<year>', '<dataset>')}, i.e. the"
        " --dataset value the pipeline published with."
    )
  try:
    return configure_dataset(value)
  except DatasetConfigError as e:
    raise DatasetConfigError(f"{DATASET_ENV_VAR}: {e}") from e


def ensure_dataset_configured() -> str:
  """Returns the configured dataset, reading `CONTRAILS_DATASET` if unset.

  An explicit `configure_dataset` call (e.g. from a launcher) takes
  precedence over the environment.

  Returns:
    Configured dataset identifier string.

  Raises:
    DatasetConfigError: If no dataset is configured and the environment
      variable is unset, blank or invalid.
  """
  if _active_dataset is not None:
    return _active_dataset
  return configure_dataset_from_env()


def configure_storage() -> None:
  """Reads a `gs://` data root with the GCS client rather than Apache Beam.

  Registers `gcs_backend.GcsBackend` with `fs` when `DATA_DIR` is a `gs://`
  URI; a local data root keeps the standard library. Call once at startup,
  before the first request.
  """
  if DATA_DIR[:5].lower() == "gs://":
    fs.register_filesystem_backend(gcs_backend.GcsBackend())
    logging.info("Reading %s with the GCS client", DATA_DIR)


def active_dataset() -> str:
  """Returns the configured dataset.

  Returns:
    Configured dataset identifier string.

  Raises:
    DatasetConfigError: If no dataset has been configured.
  """
  if _active_dataset is None:
    raise DatasetConfigError(
        f"No dataset is configured. Set {DATASET_ENV_VAR} before starting the"
        " server, or call data_loader.configure_dataset()."
    )
  return _active_dataset


TemporalCacheKey: TypeAlias = tuple[
    str | None, int, str, str | None, str | None
]
_TEMPORAL_CACHE_LOCK = threading.Lock()
_TEMPORAL_MATRIX_CACHE: collections.OrderedDict[TemporalCacheKey, Any] = (
    collections.OrderedDict()
)


def get_cached_temporal_matrix(key: TemporalCacheKey) -> Any | None:
  """Returns the cached temporal matrix response for `key`, or `None`."""
  with _TEMPORAL_CACHE_LOCK:
    value = _TEMPORAL_MATRIX_CACHE.get(key)
    if value is not None:
      _TEMPORAL_MATRIX_CACHE.move_to_end(key)
    return value


def cache_temporal_matrix(key: TemporalCacheKey, response: Any) -> None:
  """Stores a temporal matrix response under `key` (bounded to 256 entries)."""
  with _TEMPORAL_CACHE_LOCK:
    _TEMPORAL_MATRIX_CACHE[key] = response
    _TEMPORAL_MATRIX_CACHE.move_to_end(key)
    while len(_TEMPORAL_MATRIX_CACHE) > _MAX_TEMPORAL_CACHE_ENTRIES:
      _TEMPORAL_MATRIX_CACHE.popitem(last=False)


def clean_dataset(dataset: str | None = None) -> str:
  """Validates a dataset identifier; `None` means the configured dataset.

  Resolved at call time rather than as a default argument, so the dataset
  configured at startup applies to every loader.

  Args:
    dataset: An explicit dataset identifier, or None.

  Returns:
    The stripped identifier.

  Raises:
    DataValidationError: If an explicit identifier is not a safe path token.
    DatasetConfigError: If `dataset` is None and no dataset is configured.
  """
  dataset_clean = (active_dataset() if dataset is None else dataset).strip()
  if not _is_valid_dataset_token(dataset_clean):
    raise DataValidationError("Invalid dataset format.")
  return dataset_clean


def _parse_year_token(year: object) -> int | None:
  """Parses a 4-digit calendar year token, or returns None if malformed."""
  if isinstance(year, int) and not isinstance(year, bool):
    return year if 1000 <= year <= 9999 else None
  if isinstance(year, str):
    year_clean = year.strip()
    if _YEAR_DIR_PATTERN.match(year_clean):
      return int(year_clean)
  return None


def require_supported_year(year: int | str, dataset: str | None = None) -> int:
  """Parses `year` and rejects years for which the dataset has no data."""
  dataset_clean = clean_dataset(dataset)
  available = get_available_years(dataset_clean)
  year_int = _parse_year_token(year)
  if year_int is None or year_int not in available:
    if year_int is not None:
      resolve_dataset_dir(year_int, dataset_clean)
    supported = ", ".join(str(y) for y in available) or "none"
    raise DataValidationError(
        f"Year '{year}' is not available for dataset '{dataset_clean}'."
        " Available"
        f" years are {supported}."
    )
  return year_int


def require_year_format(year: int | str) -> int:
  """Parses a four-digit calendar year token, or raises DataValidationError."""
  year_int = _parse_year_token(year)
  if year_int is None:
    raise DataValidationError(f"Invalid year format: {year!r}.")
  return year_int


def require_path_safe_token(value: str, description: str) -> None:
  """Rejects identifiers that could escape the artifact directory."""
  if (
      not value
      or not value.isascii()
      or not all(char.isalnum() or char in ("-", "_") for char in value)
  ):
    raise DataValidationError(f"Invalid {description} format.")


def validate_fir_id(fir_id: str | None = None) -> str | None:
  """Normalizes `fir_id` and checks its length, or returns `None` if blank.

  Only the length is validated; any other value is a plain lookup key that
  matches no FIR unless it is a canonical `fir_id`.

  Args:
    fir_id: Raw FIR identifier from the request.

  Returns:
    The stripped, upper-cased identifier, or `None` if blank.

  Raises:
    DataValidationError: If the identifier is longer than 128 characters.
  """
  fir_id_clean = fir_id.strip().upper() if fir_id else None
  if fir_id_clean and len(fir_id_clean) > 128:
    raise DataValidationError("Invalid FIR ID length.")
  return fir_id_clean or None


def resolve_iso_code(iso: str) -> str:
  """Validates and canonicalizes an ISO country identifier."""
  iso_clean = iso.strip()
  if (
      not iso_clean
      or not iso_clean.isascii()
      or len(iso_clean) > 64
      or not all(char.isalnum() or char in ("-", "_") for char in iso_clean)
  ):
    raise DataValidationError("Invalid ISO identifier format.")
  return iso_clean.upper()


def safe_exists(path: str, *, strict: bool = False) -> bool:
  """Reports whether a path exists.

  Args:
    path: File or directory path.
    strict: If False, an error from the existence check (permissions, a
      transient storage failure) is reported as "absent", which suits probing a
      list of candidate locations. If True, the error raises instead; use this
      wherever the answer is cached, so a transient failure is not memoized as a
      missing artifact.

  Returns:
    Whether `path` exists.

  Raises:
    ArtifactUnavailableError: If `strict` and the existence check fails.
  """
  try:
    return fs.exists(path)
  except (fs.Error, OSError, RuntimeError) as e:
    if strict:
      raise ArtifactUnavailableError(
          f"Could not check whether {os.path.basename(path)!r} exists:"
          f" {type(e).__name__}"
      ) from e
    logging.debug("Filesystem check failed for %s: %s", path, e)
  return False


def load_json(path: str | None, description: str) -> Any | None:
  """Reads and parses the JSON file at `path`.

  Args:
    path: File path, or None for an unpublished dataset.
    description: Human-readable artifact description for logs and errors.

  Returns:
    The parsed JSON value, or None if `path` is None or the file is absent.

  Raises:
    ArtifactUnavailableError: If the file exists but cannot be read or parsed.
      Callers must not treat a corrupt or temporarily unreadable artifact as
      missing, and the error keeps the failure out of `lru_cache`d callers.
  """
  if path is None or not safe_exists(path, strict=True):
    return None
  try:
    with fs.open_file(path, "r") as json_file:
      data = json.load(json_file)
  except (
      fs.Error,
      OSError,
      json.JSONDecodeError,
      UnicodeDecodeError,
  ) as e:
    logging.warning("Could not load %s from %s: %s", description, path, e)
    raise ArtifactUnavailableError(
        f"Could not read {description}: {type(e).__name__}"
    ) from e
  logging.info("Successfully loaded %s from %s", description, path)
  return data


@functools.lru_cache(maxsize=128)
def _resolve_dataset_dir_cached(year_int: int, dataset: str) -> str | None:
  """Resolves and caches the published run directory for a validated year."""
  try:
    resolved = artifact_io.resolve_dataset_dir(DATA_DIR, year_int, dataset)
  except ValueError as e:
    logging.exception(
        "Malformed run pointer for %s/%s under %s: %s",
        year_int,
        dataset,
        DATA_DIR,
        e,
    )
    raise ArtifactUnavailableError(
        f"Malformed run pointer for year {year_int} dataset {dataset}: {e}"
    ) from e
  logging.info(
      "Resolved dataset dir for %s/%s to %s", year_int, dataset, resolved
  )
  return resolved


def resolve_dataset_dir(year: int | str, dataset: str) -> str | None:
  """Resolves the published run directory of a dataset, None if unpublished.

  The result is memoized for the life of the process, so a `latest.json` flip
  is only picked up after `clear_all_caches` or a restart.

  Args:
    year: Four-digit calendar year.
    dataset: Validated dataset identifier.

  Returns:
    The resolved `runs/<run_id>` directory path, or `None` if unpublished.

  Raises:
    DataValidationError: If `year` is not a four-digit calendar year.
    ArtifactUnavailableError: If `latest.json` is malformed or invalid.
  """
  year_int = require_year_format(year)
  return _resolve_dataset_dir_cached(year_int, dataset)


def safe_rank_metric(value: Any) -> float:
  """Converts a metric value to a finite float, defaulting to 0.0."""
  coerced = metrics.coerce_float(value)
  return coerced if coerced is not None else 0.0


def _artifact_path(year: int, dataset: str, filename: str) -> str | None:
  """Path of `filename` in the published run, or None if unpublished."""
  run_dir = resolve_dataset_dir(year, dataset)
  return os.path.join(run_dir, filename) if run_dir else None


def _artifact_exists(year: int, dataset: str, filename: str) -> bool:
  """Reports whether `filename` exists in the published run for `year`."""
  try:
    path = _artifact_path(year, dataset, filename)
  except ArtifactUnavailableError:
    return False
  return path is not None and safe_exists(path, strict=True)


def _list_year_dirs() -> list[int]:
  """Returns the four-digit directory years directly under the data root.

  Returns:
    The years in ascending order; empty if the data root does not exist.

  Raises:
    ArtifactUnavailableError: If the data root exists but cannot be listed.
      Raising (not returning an empty list) keeps a transient failure from
      being memoized as "no years" by `_get_available_years_cached`.
  """
  try:
    entries = fs.listdir(DATA_DIR)
  except (OSError, fs.Error) as e:
    if not safe_exists(DATA_DIR, strict=True):
      logging.warning("Data root %s does not exist", DATA_DIR)
      return []
    logging.warning("Cannot list data root %s: %s", DATA_DIR, e)
    raise ArtifactUnavailableError(
        f"Cannot list the data root: {type(e).__name__}"
    ) from e
  names = {str(entry).rstrip("/") for entry in entries}
  return sorted(int(name) for name in names if _YEAR_DIR_PATTERN.match(name))


@functools.lru_cache(maxsize=16)
def _get_available_years_cached(dataset_clean: str) -> tuple[int, ...]:
  """Returns the sorted years whose directory holds `dataset_clean` data.

  Discovered from the data root on first use and cached until
  `clear_all_caches`; a newly published year needs a server restart.

  Args:
    dataset_clean: A validated dataset identifier.

  Returns:
    Tuple of four-digit calendar years sorted in ascending order.
  """
  available: list[int] = []
  for year in _list_year_dirs():
    if _artifact_exists(year, dataset_clean, constants.COUNTRY_EXPORT_FILENAME):
      available.append(year)
  if not available:
    logging.warning(
        "No year under %s has data for dataset %s", DATA_DIR, dataset_clean
    )
  return tuple(available)


def get_available_years(dataset: str | None = None) -> list[int]:
  """Returns the sorted list of years available for the dataset.

  Discovered from the data root on first use and memoized until
  `clear_all_caches` or a process restart.

  Args:
    dataset: Dataset identifier; defaults to the configured dataset.

  Returns:
    Four-digit calendar years sorted in ascending order.
  """
  dataset_clean = clean_dataset(dataset)
  return list(_get_available_years_cached(dataset_clean))


def _freeze_json_value(value: Any) -> Any:
  """Recursively wraps dicts in `MappingProxyType` and lists in `tuple`."""
  if isinstance(value, dict):
    return types.MappingProxyType(
        {k: _freeze_json_value(v) for k, v in value.items()}
    )
  if isinstance(value, list):
    return tuple(_freeze_json_value(item) for item in value)
  return value


def _thaw_json_value(value: Any) -> Any:
  """Recursively unwraps `MappingProxyType` to `dict` and `tuple` to `list`."""
  if isinstance(value, types.MappingProxyType):
    return {k: _thaw_json_value(v) for k, v in value.items()}
  if isinstance(value, tuple):
    return [_thaw_json_value(item) for item in value]
  return value


def _normalize_tabular_row(raw_row: dict[str, Any]) -> abc.Mapping[str, Any]:
  """Sanitizes non-finite floats in `raw_row` and freezes it."""
  return _freeze_json_value(json_utils.sanitize_non_finite_floats(raw_row))


@functools.lru_cache(maxsize=64)
def _load_tabular_json(
    run_dir: str | None,
    *,
    year: int,
    dataset_clean: str,
    filename: str,
    required: bool,
    label: str,
) -> tuple[abc.Mapping[str, Any], ...]:
  """Loads and normalizes a tabular JSON artifact from `run_dir`.

  Args:
    run_dir: Resolved run directory path, or `None` if unpublished.
    year: Validated four-digit calendar year.
    dataset_clean: Validated dataset identifier.
    filename: Artifact filename inside `run_dir`.
    required: Whether a missing or unreadable file raises an error.
    label: Human-readable artifact description for logs and error messages.

  Returns:
    Tuple of frozen, float-sanitized row mappings.

  Raises:
    ArtifactUnavailableError: If `required` is True and the file cannot be
      loaded, or if the parsed JSON is not a list of objects.
  """
  path = os.path.join(run_dir, filename) if run_dir else None
  data = load_json(
      path,
      f"{label} for year {year} dataset {dataset_clean}",
  )
  if data is None:
    if required:
      raise ArtifactUnavailableError(
          f"Failed to load {label} for year {year} dataset {dataset_clean}."
      )
    return ()
  if not isinstance(data, list) or not all(
      isinstance(row, dict) for row in data
  ):
    message = (
        f"Malformed {label} for year {year} dataset {dataset_clean}:"
        " expected a JSON list of objects."
    )
    logging.error(message)
    raise ArtifactUnavailableError(message)
  return tuple(_normalize_tabular_row(row) for row in data)


class CountryYearData(NamedTuple):
  """Loaded per-country rows and ISO-2 lookup index for one year."""

  rows: abc.Sequence[abc.Mapping[str, Any]]
  iso_index: abc.Mapping[str, abc.Mapping[str, Any]]


@functools.lru_cache(maxsize=64)
def _get_country_data_cached(
    run_dir: str | None, year: int, dataset_clean: str
) -> CountryYearData:
  """Loads and indexes per-country JSON rows for a resolved `run_dir`."""
  data = _load_tabular_json(
      run_dir,
      year=year,
      dataset_clean=dataset_clean,
      filename=constants.COUNTRY_EXPORT_FILENAME,
      required=True,
      label="per-country JSON data",
  )
  iso_index: dict[str, abc.Mapping[str, Any]] = {}
  for row in data:
    iso = row.get(constants.KEY_ISO_A2)
    if iso is not None:
      iso_index.setdefault(str(iso).strip().upper(), row)
  return CountryYearData(rows=data, iso_index=types.MappingProxyType(iso_index))


def get_country_data_for_year(
    year: int | str, dataset: str | None = None
) -> CountryYearData:
  """Loads per-country rows for a year and dataset.

  Args:
    year: Dataset year to load.
    dataset: Dataset name; defaults to the configured dataset.

  Returns:
    `CountryYearData(rows, iso_index)`: all rows, and the first row per
    upper-cased `iso_a2`.
  """
  dataset_clean = clean_dataset(dataset)
  year_int = require_supported_year(year, dataset_clean)
  run_dir = resolve_dataset_dir(year_int, dataset_clean)
  return _get_country_data_cached(run_dir, year_int, dataset_clean)


def get_fir_data_for_year(
    year: int | str, dataset: str | None = None
) -> abc.Sequence[abc.Mapping[str, Any]]:
  """Loads the per-FIR JSON rows for a specific year and dataset."""
  dataset_clean = clean_dataset(dataset)
  year_int = require_supported_year(year, dataset_clean)
  run_dir = resolve_dataset_dir(year_int, dataset_clean)
  return _load_tabular_json(
      run_dir,
      year=year_int,
      dataset_clean=dataset_clean,
      filename=constants.FIR_EXPORT_FILENAME,
      required=True,
      label="FIR JSON data",
  )


def get_temporal_raw_data(
    year: int | str, dataset: str | None = None
) -> abc.Sequence[abc.Mapping[str, Any]]:
  """Loads raw per-country temporal records for a year and dataset."""
  dataset_clean = clean_dataset(dataset)
  year_int = require_supported_year(year, dataset_clean)
  run_dir = resolve_dataset_dir(year_int, dataset_clean)
  return _load_tabular_json(
      run_dir,
      year=year_int,
      dataset_clean=dataset_clean,
      filename=constants.COUNTRY_TEMPORAL_EXPORT_FILENAME,
      required=False,
      label="temporal records",
  )


def get_fir_temporal_raw_data(
    year: int | str, dataset: str | None = None
) -> abc.Sequence[abc.Mapping[str, Any]]:
  """Loads raw per-FIR temporal records for a year and dataset."""
  dataset_clean = clean_dataset(dataset)
  year_int = require_supported_year(year, dataset_clean)
  run_dir = resolve_dataset_dir(year_int, dataset_clean)
  return _load_tabular_json(
      run_dir,
      year=year_int,
      dataset_clean=dataset_clean,
      filename=constants.FIR_TEMPORAL_EXPORT_FILENAME,
      required=False,
      label="FIR temporal records",
  )


@functools.lru_cache(maxsize=64)
def _get_fir_index(
    run_dir: str | None, year: int, dataset_clean: str
) -> abc.Mapping[str, abc.Mapping[str, Any]]:
  """Builds an indexed lookup map for FIR rows by canonical fir_id."""
  fir_rows = _load_tabular_json(
      run_dir,
      year=year,
      dataset_clean=dataset_clean,
      filename=constants.FIR_EXPORT_FILENAME,
      required=True,
      label="FIR JSON data",
  )
  fir_id_map: dict[str, abc.Mapping[str, Any]] = {}
  for row in fir_rows:
    row_id = str(row.get(constants.KEY_FIR_ID) or "").strip().upper()
    if row_id:
      fir_id_map.setdefault(row_id, row)
  return types.MappingProxyType(fir_id_map)


def get_fir_row_by_identifier(
    year: int | str,
    dataset: str | None = None,
    fir_id: str | None = None,
) -> dict[str, Any] | None:
  """Looks up a FIR row by canonical `fir_id`, or returns `None` if absent."""
  fir_id_clean = validate_fir_id(fir_id)
  if not fir_id_clean:
    return None
  dataset_clean = clean_dataset(dataset)
  year_int = require_supported_year(year, dataset_clean)
  run_dir = resolve_dataset_dir(year_int, dataset_clean)
  fir_id_map = _get_fir_index(run_dir, year_int, dataset_clean)
  row = fir_id_map.get(fir_id_clean)
  return dict(row) if row is not None else None


def _normalize_grid_lod(lod: int | None) -> int | None:
  """Returns `lod` when it is `0` or `1`, or `None` for full resolution."""
  return lod if lod in (0, 1) else None


@functools.lru_cache(maxsize=32)
def _load_shared_grid_meta_cached(
    run_dir: str | None, year: int
) -> abc.Mapping[str, Any] | None:
  """Loads and caches `grid_meta.json` for `run_dir`, or `None` if absent."""
  path = (
      os.path.join(run_dir, constants.SHARED_GRID_META_FILENAME)
      if run_dir
      else None
  )
  shared_meta = load_json(
      path,
      f"shared grid metadata ({year})",
  )
  return (
      _freeze_json_value(shared_meta) if isinstance(shared_meta, dict) else None
  )


class GridBinaryArtifact(NamedTuple):
  """Resolved grid binary path and whether it is a downsampled LOD artifact."""

  path: str
  is_lod_artifact: bool


@functools.lru_cache(maxsize=_MAX_GRID_CACHE_ENTRIES * 2)
def _resolve_grid_binary_artifact(
    run_dir: str | None,
    prefix_clean: str,
    effective_lod: int | None = None,
) -> GridBinaryArtifact | None:
  """Resolves and caches `GridBinaryArtifact` for a prefix/LOD, or `None`."""
  if not run_dir:
    return None
  candidates = [(f"{prefix_clean}{constants.GRID_BIN_GZ_SUFFIX}", False)]
  if effective_lod in (0, 1):
    candidates.insert(
        0,
        (
            f"{prefix_clean}_lod{effective_lod}{constants.GRID_BIN_GZ_SUFFIX}",
            True,
        ),
    )
  for filename, is_lod in candidates:
    path = os.path.join(run_dir, filename)
    if safe_exists(path, strict=True):
      return GridBinaryArtifact(path=path, is_lod_artifact=is_lod)
  return None


@functools.lru_cache(maxsize=_MAX_GRID_CACHE_ENTRIES)
def _get_grid_meta_cached(
    run_dir: str | None,
    *,
    year: int,
    prefix_clean: str,
    effective_lod: int | None = None,
) -> abc.Mapping[str, Any] | None:
  """Computes and caches resolved grid metadata, or `None` if unavailable."""
  shared_meta = _load_shared_grid_meta_cached(run_dir, year)
  if shared_meta is None:
    return None

  resolved_bin = _resolve_grid_binary_artifact(
      run_dir, prefix_clean, effective_lod
  )
  if resolved_bin is None:
    return None

  resolved = dict(shared_meta)
  resolved.pop(constants.KEY_LODS, None)
  lods_map = shared_meta.get(constants.KEY_LODS)
  lod_override = (
      lods_map.get(str(effective_lod))
      if isinstance(lods_map, abc.Mapping) and effective_lod is not None
      else None
  )
  if resolved_bin.is_lod_artifact and isinstance(lod_override, abc.Mapping):
    resolved.update(lod_override)
    effective_name = f"{prefix_clean}_lod{effective_lod}"
  else:
    effective_name = prefix_clean
  resolved[constants.KEY_GRID_NAME] = effective_name
  return _freeze_json_value(resolved)


def _resolve_supported_grid_run(
    year: int | str,
    dataset: str | None = None,
    *,
    prefix: str | None = None,
) -> tuple[int, str | None]:
  """Validates grid year, dataset, and optional prefix and resolves run_dir."""
  dataset_clean = clean_dataset(dataset)
  year_int = require_year_format(year)
  require_supported_year(year_int, dataset_clean)
  if prefix is not None:
    require_path_safe_token(prefix, _GRID_PREFIX_DESCRIPTION)
  return year_int, resolve_dataset_dir(year_int, dataset_clean)


def get_grid_meta(
    year: int | str,
    *,
    prefix: str,
    dataset: str | None = None,
    lod: int | None = None,
) -> dict[str, Any] | None:
  """Returns the spatial metadata header for `prefix`, or None if absent.

  Args:
    year: Dataset year (e.g. 2024).
    prefix: Grid artifact prefix, for example "total_global_impact_grid".
    dataset: Optional dataset identifier. Defaults to the active dataset.
    lod: Optional level of detail: 0 or 1 select the downsampled artifacts;
      any other value selects full resolution.

  Returns:
    A mutable copy of the spatial metadata dictionary, or None if the dataset
    directory or grid artifact is absent.
  """
  prefix_clean = prefix.strip()
  year_int, run_dir = _resolve_supported_grid_run(
      year, dataset, prefix=prefix_clean
  )
  effective_lod = _normalize_grid_lod(lod)
  cached = _get_grid_meta_cached(
      run_dir,
      year=year_int,
      prefix_clean=prefix_clean,
      effective_lod=effective_lod,
  )
  return _thaw_json_value(cached) if cached is not None else None


@functools.lru_cache(maxsize=_MAX_GRID_CACHE_ENTRIES)
def _get_grid_meta_json_cached(
    run_dir: str | None,
    *,
    year: int,
    prefix_clean: str,
    effective_lod: int | None = None,
) -> str | None:
  """Computes and caches the compact JSON header, or `None` if unavailable."""
  meta = _get_grid_meta_cached(
      run_dir,
      year=year,
      prefix_clean=prefix_clean,
      effective_lod=effective_lod,
  )
  if meta is None:
    return None
  return json.dumps(_thaw_json_value(meta), separators=(",", ":"))


def get_grid_meta_json(
    year: int | str,
    *,
    prefix: str,
    dataset: str | None = None,
    lod: int | None = None,
) -> str | None:
  """Returns the compact JSON metadata header for `prefix`, or `None`."""
  prefix_clean = prefix.strip()
  year_int, run_dir = _resolve_supported_grid_run(
      year, dataset, prefix=prefix_clean
  )
  effective_lod = _normalize_grid_lod(lod)
  return _get_grid_meta_json_cached(
      run_dir,
      year=year_int,
      prefix_clean=prefix_clean,
      effective_lod=effective_lod,
  )


@functools.lru_cache(maxsize=_MAX_GRID_CACHE_ENTRIES)
def _read_gzip_grid_file(path: str) -> bytes:
  """Reads the gzip-compressed grid binary at `path` (served as-is)."""
  with fs.open_file(path, "rb") as binary_file:
    return binary_file.read()


def get_grid_binary_payload(
    year: int | str,
    *,
    prefix: str,
    dataset: str | None = None,
    lod: int | None = None,
) -> bytes | None:
  """Returns the gzipped grid binary (`.bin.gz`).

  Args:
    year: Four-digit calendar year.
    prefix: Grid artifact filename prefix.
    dataset: Dataset identifier; defaults to the configured dataset.
    lod: Level of detail; defaults to the finest published level.

  Returns:
    The gzip-compressed payload, or `None` if the grid is not published.

  Raises:
    ArtifactUnavailableError: If the grid file exists but cannot be read.
  """
  prefix_clean = prefix.strip()
  _, run_dir = _resolve_supported_grid_run(
      year, dataset, prefix=prefix_clean
  )
  effective_lod = _normalize_grid_lod(lod)

  resolved_bin = _resolve_grid_binary_artifact(
      run_dir, prefix_clean, effective_lod
  )
  if resolved_bin is None:
    return None
  try:
    return _read_gzip_grid_file(resolved_bin.path)
  except (fs.Error, OSError) as e:
    logging.warning(
        "Error reading grid binary from %s: %s", resolved_bin.path, e
    )
    raise ArtifactUnavailableError(
        f"Could not read grid {prefix_clean!r} for year {year}:"
        f" {type(e).__name__}"
    ) from e


class GridAvailabilityDict(TypedDict):
  """Availability flags and counts for total, monthly, and hourly grids."""

  year: int
  has_total: bool
  has_monthly: bool
  has_hourly: bool
  months_count: int
  hours_count: int


@functools.lru_cache(maxsize=128)
def _get_grid_availability_cached(
    run_dir: str | None, year: int
) -> abc.Mapping[str, Any]:
  """Computes and caches grid availability for a resolved `run_dir`."""

  def has_grid(prefix: str) -> bool:
    """Returns True if the grid binary gz file exists for `prefix`.

    Args:
      prefix: Grid artifact filename prefix.

    Returns:
      True if the compressed binary grid file exists in `run_dir`.
    """
    return bool(
        run_dir
        and safe_exists(
            os.path.join(run_dir, f"{prefix}{constants.GRID_BIN_GZ_SUFFIX}")
        )
    )

  has_total = has_grid(constants.TOTAL_GLOBAL_IMPACT_GRID_PREFIX) or has_grid(
      constants.TOTAL_GLOBAL_DENSITY_GRID_PREFIX
  )
  has_monthly = has_grid(
      f"{constants.MONTHLY_GLOBAL_IMPACT_GRID_PREFIX}_01"
  ) or has_grid(f"{constants.MONTHLY_GLOBAL_DENSITY_GRID_PREFIX}_01")
  has_hourly = has_grid(
      f"{constants.HOURLY_GLOBAL_IMPACT_GRID_PREFIX}_00"
  ) or has_grid(f"{constants.HOURLY_GLOBAL_DENSITY_GRID_PREFIX}_00")
  return types.MappingProxyType({
      "year": year,
      "has_total": has_total,
      "has_monthly": has_monthly,
      "has_hourly": has_hourly,
      "months_count": 12 if has_monthly else 0,
      "hours_count": 24 if has_hourly else 0,
  })


def get_grid_availability(
    year: int | str,
    dataset: str | None = None,
) -> GridAvailabilityDict:
  """Reports which total, monthly, and hourly grids exist for a year."""
  year_int, run_dir = _resolve_supported_grid_run(year, dataset)
  return cast(
      GridAvailabilityDict,
      dict(_get_grid_availability_cached(run_dir, year_int)),
  )


_CACHED_FUNCTIONS = (
    _resolve_dataset_dir_cached,
    _get_available_years_cached,
    _load_tabular_json,
    _get_country_data_cached,
    _get_fir_index,
    _load_shared_grid_meta_cached,
    _resolve_grid_binary_artifact,
    _get_grid_meta_cached,
    _get_grid_meta_json_cached,
    _read_gzip_grid_file,
    _get_grid_availability_cached,
)
_cache_clear_callbacks: list[abc.Callable[[], None]] = []


def register_cache_clear_callback(callback: abc.Callable[[], None]) -> None:
  """Registers a callback invoked whenever `clear_all_caches` runs."""
  if callback not in _cache_clear_callbacks:
    _cache_clear_callbacks.append(callback)


def clear_all_caches() -> None:
  """Empties every in-memory cache held by this module and callbacks."""
  with _TEMPORAL_CACHE_LOCK:
    _TEMPORAL_MATRIX_CACHE.clear()
  for fn in _CACHED_FUNCTIONS:
    fn.cache_clear()
  for callback in _cache_clear_callbacks:
    callback()


_YEAR_PRELOAD_LOADERS: tuple[
    tuple[str, collections.abc.Callable[[int, str], object]], ...
] = (
    ("per-country", get_country_data_for_year),
    ("FIR", get_fir_data_for_year),
    (
        "FIR index",
        lambda year_arg, ds: _get_fir_index(
            resolve_dataset_dir(year_arg, ds), year_arg, ds
        ),
    ),
    ("temporal", get_temporal_raw_data),
    ("FIR temporal", get_fir_temporal_raw_data),
    ("grid availability", get_grid_availability),
    (
        "total impact grid metadata",
        lambda year_arg, ds: get_grid_meta_json(
            year_arg,
            prefix=constants.TOTAL_GLOBAL_IMPACT_GRID_PREFIX,
            dataset=ds,
            lod=2,
        ),
    ),
    (
        "total impact grid binary",
        lambda year_arg, ds: get_grid_binary_payload(
            year_arg,
            prefix=constants.TOTAL_GLOBAL_IMPACT_GRID_PREFIX,
            dataset=ds,
            lod=2,
        ),
    ),
)


def _preload_year_data(target: tuple[int, str]) -> None:
  """Warms per-country, FIR, temporal, and initial grid caches for a year."""
  year, dataset = target
  for description, loader in _YEAR_PRELOAD_LOADERS:
    try:
      loader(year, dataset)
      logging.info(
          "Preloaded %s data for year %s dataset %s", description, year, dataset
      )
    except (
        DataValidationError,
        ArtifactUnavailableError,
        OSError,
        fs.Error,
    ) as e:
      logging.exception(
          "Could not preload %s data for year %s dataset %s: %s",
          description,
          year,
          dataset,
          e,
      )


def preload_all_data() -> None:
  """Warms every available year's country, FIR, temporal, and grid caches."""
  dataset = active_dataset()
  targets = [(year, dataset) for year in get_available_years(dataset)]
  if not targets:
    return
  max_workers = min(4, len(targets))
  with futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
    list(executor.map(_preload_year_data, targets))
