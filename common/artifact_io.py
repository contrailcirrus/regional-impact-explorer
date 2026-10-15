"""Reads and writes published artifacts and the `latest.json` run pointer.

Serves as the single choke point for artifact writes, publishing into run
directories named by `run_id` with atomic `latest.json` pointer flips, and
provides the pointer resolution the server uses to find the active run.
"""

from collections.abc import Mapping, Sequence
import json
import math
import os
import re
from typing import Any

from absl import logging

from common import constants
from common import fs


def _sanitize_json_floats(obj: Any) -> Any:
  """Recursively replaces non-finite floats (NaN, ±Inf) with 0.0."""
  if isinstance(obj, float):
    return obj if math.isfinite(obj) else 0.0
  if isinstance(obj, dict):
    return {
        k: _sanitize_json_floats(item_value) for k, item_value in obj.items()
    }
  if isinstance(obj, (list, tuple)):
    return [_sanitize_json_floats(item_value) for item_value in obj]
  return obj


def write_json_output(
    data: Any, target_path: str, compact: bool = False
) -> None:
  """Writes a JSON object to the configured output filesystem.

  Non-finite floats (NaN, +/-Inf) are written as `0.0`, because strict JSON has
  no representation for them; pass `None` where a value is unknown.

  Args:
    data: JSON-serializable object.
    target_path: Destination file path.
    compact: Omit indentation and separator whitespace. Used for large GeoJSON
      artifacts, where indentation would roughly double the size.
  """
  fs.ensure_parent_dir(target_path)
  dump_kwargs: dict[str, Any] = (
      {"separators": (",", ":")} if compact else {"indent": 2}
  )
  try:
    serialized = json.dumps(data, allow_nan=False, **dump_kwargs)
  except ValueError:
    serialized = json.dumps(
        _sanitize_json_floats(data), allow_nan=False, **dump_kwargs
    )
  with fs.atomic_open_for_write(target_path, "w") as f:
    f.write(serialized)
  logging.info("Successfully wrote JSON artifact to: %s", target_path)


def write_binary_output(data: bytes, target_path: str) -> None:
  """Writes a raw binary buffer to the configured output filesystem."""
  fs.ensure_parent_dir(target_path)
  with fs.atomic_open_for_write(target_path, "wb") as f:
    f.write(data)
  logging.info("Successfully wrote binary buffer to: %s", target_path)


# ---------------------------------------------------------------------------
# Run-prefixed publishing
#
# A dataset directory holds immutable per-run subdirectories plus one mutable
# pointer:
#
#     {root}/{year}/{dataset}/runs/{run_id}/...   never rewritten
#     {root}/{year}/{dataset}/latest.json         flipped once, at the end
#
# A reader either sees the old run or the new one, never a half-written mix,
# even though publishing ~150 artifacts takes minutes.
#
# This only holds if the pointer is flipped *after* every artifact has landed,
# and if caching respects the asymmetry: `runs/**` is immutable and may be
# cached forever (keyed on the resolved `runs/{run_id}/` directory rather than
# `(year, dataset)` alone so endpoints cannot mix artifacts across runs). A
# reader that memoizes `latest.json` (as the server does, per process) keeps
# serving the run it first resolved until it re-reads the pointer.
# ---------------------------------------------------------------------------

MAX_RUN_ID_LENGTH = 128
# Same style as the dataset allowlist, plus '.' so version tags like `v1.2`
# survive. ASCII only, so no non-ASCII digit or letter can slip through.
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]+", re.ASCII)


def validate_run_id(run_id: str, *, label: str = "run id") -> str:
  """Returns `run_id` if it is safe to use as a single path component.

  A run id names a directory under `runs/`. Anything that is not one plain
  path component could write outside it: `..` lands next to `latest.json`,
  and an absolute id replaces the whole output root in `os.path.join`.

  Args:
    run_id: The candidate run id.
    label: How to name the value in the error message, e.g. `--run_id`.

  Returns:
    `run_id`, unchanged.

  Raises:
    ValueError: If `run_id` is not a string of 1 to `MAX_RUN_ID_LENGTH` ASCII
      letters, digits, '.', '-' or '_', or is `.` or `..`.
  """
  if (
      not isinstance(run_id, str)
      or len(run_id) > MAX_RUN_ID_LENGTH
      or not _RUN_ID_PATTERN.fullmatch(run_id)
      or run_id in (".", "..")
  ):
    raise ValueError(
        f"{label}={run_id!r} is invalid: use 1 to {MAX_RUN_ID_LENGTH} ASCII"
        " letters, digits, '.', '-' or '_', other than '.' and '..'."
    )
  return run_id


def year_dir_name(year: int) -> str:
  """Returns the directory name for `year`: always exactly four digits.

  The only place a year becomes a path component, so every writer and reader
  agrees on the spelling (the server lists only four-digit directories).

  Args:
    year: Four-digit calendar year in [0, 9999].

  Raises:
    ValueError: If `year` is not an int in [0, 9999].
  """
  if (
      isinstance(year, bool)
      or not isinstance(year, int)
      or not 0 <= year <= 9999
  ):
    raise ValueError(f"year={year!r} is not a four-digit year.")
  return f"{year:04d}"


def dataset_root(output_root: str, year: int, dataset: str) -> str:
  """Directory holding a dataset's runs and its pointer."""
  return os.path.join(output_root, year_dir_name(year), dataset)


def run_output_dir(
    output_root: str, *, year: int, dataset: str, run_id: str
) -> str:
  """Returns the directory of one published run.

  Args:
    output_root: Root of the published layout.
    year: Dataset year.
    dataset: Dataset name.
    run_id: Run id of the published run.

  Returns:
    The run directory.

  Raises:
    ValueError: If `run_id` is not a valid run id (see `validate_run_id`).
  """
  validate_run_id(run_id)
  return os.path.join(
      dataset_root(output_root, year, dataset), constants.RUNS_DIR, run_id
  )


def require_new_run_dir(
    output_root: str, *, year: int, dataset: str, run_id: str
) -> str:
  """Returns the directory of a run that has not been published to yet.

  Runs are immutable once written. Publishing into an existing run directory
  would overwrite it in place, and if that run is the one `latest.json` names,
  a failure midway would leave readers on a torn mix of old and new artifacts.
  Call this once, before writing anything: a check inside a retried pipeline
  step would trip over its own partial output.

  Args:
    output_root: Root of the published layout.
    year: Dataset year.
    dataset: Dataset name.
    run_id: Run id to publish under.

  Returns:
    The run directory, which does not exist yet.

  Raises:
    ValueError: If `run_id` is invalid or its run directory already exists.
  """
  run_dir = run_output_dir(
      output_root, year=year, dataset=dataset, run_id=run_id
  )
  if fs.exists(run_dir):
    raise ValueError(
        f"run {run_id!r} already exists at {run_dir}. Published runs are"
        " never overwritten: pick a new publish run id."
    )
  return run_dir


def pointer_path(output_root: str, year: int, dataset: str) -> str:
  """Returns the `latest.json` pointer file path for `(year, dataset)`."""
  return os.path.join(
      dataset_root(output_root, year, dataset), constants.POINTER_FILENAME
  )


def write_run_pointer(
    output_root: str,
    *,
    year: int,
    dataset: str,
    run_id: str,
    published_at: str = "",
) -> str:
  """Flips a dataset to a new run. Call only once all artifacts have landed."""
  validate_run_id(run_id)
  path = pointer_path(output_root, year, dataset)
  write_json_output(
      {
          constants.KEY_SCHEMA_VERSION: constants.POINTER_SCHEMA_VERSION,
          constants.KEY_RUN_ID: run_id,
          constants.KEY_PUBLISHED_AT: published_at,
      },
      path,
  )
  return path


def read_run_pointer(output_root: str, year: int, dataset: str) -> str | None:
  """Returns the current run id, or None if this dataset has no valid pointer.

  A dataset without a pointer has not finished publishing (or was never
  published), so it is not an error: readers treat it as absent. The same
  goes for a pointer that cannot be read or parsed, or that was written with
  another schema version.

  Args:
    output_root: Root directory containing published datasets.
    year: Calendar year of the target dataset.
    dataset: Logical name of the dataset.

  Returns:
    The run id string, or None if unpublished or invalid.

  Raises:
    ValueError: If the pointer parses but is malformed: not a JSON object, a
      non-integer schema version, or a missing or invalid run id. Such a
      pointer was not written by `write_run_pointer`, and following it could
      lead outside the dataset directory.
  """
  path = pointer_path(output_root, year, dataset)
  try:
    if not fs.exists(path):
      return None
    with fs.open_file(path, "r") as f:
      payload = json.load(f)
  except (OSError, fs.Error, json.JSONDecodeError, ValueError) as e:
    logging.warning(
        "Unreadable run pointer at %s: %s; treating dataset as unpublished",
        path,
        e,
        exc_info=True,
    )
    return None

  if not isinstance(payload, dict):
    raise ValueError(
        f"Run pointer at {path} is not a JSON object: {type(payload).__name__}."
    )
  version = payload.get(constants.KEY_SCHEMA_VERSION, 0)
  if isinstance(version, bool) or not isinstance(version, int):
    raise ValueError(
        f"Run pointer at {path} has a non-integer schema_version {version!r}."
    )
  if version != constants.POINTER_SCHEMA_VERSION:
    logging.warning(
        "Run pointer at %s has schema version %d, expected %d; ignoring it",
        path,
        version,
        constants.POINTER_SCHEMA_VERSION,
    )
    return None
  run_id = payload.get(constants.KEY_RUN_ID)
  if not isinstance(run_id, str):
    raise ValueError(f"Run pointer at {path} has no string run_id: {run_id!r}.")
  return validate_run_id(run_id, label=f"run_id in run pointer {path}")


def resolve_dataset_dir(
    output_root: str, year: int, dataset: str
) -> str | None:
  """Returns the directory of the run the pointer names.

  Args:
    output_root: Root of the published layout.
    year: Dataset year.
    dataset: Dataset name.

  Returns:
    The run directory, or None if the dataset has no valid pointer.

  Raises:
    ValueError: If the pointer is malformed (see `read_run_pointer`).
  """
  run_id = read_run_pointer(output_root, year, dataset)
  if run_id:
    return run_output_dir(
        output_root, year=year, dataset=dataset, run_id=run_id
    )
  return None


# ---------------------------------------------------------------------------
# Boundary geometry
#
# The country and FIR boundaries a run was derived with are published into the
# same directory as its metrics, and the server serves them from there. For a
# reduce run these are also the boundaries the segments were attributed
# against. `derive_main` publishes the boundary files it is given, which may
# differ from the ones the stored intermediate was attributed against.
# ---------------------------------------------------------------------------


def write_boundary_geojson(
    dataset_dir: str,
    country_features: Sequence[Mapping[str, Any]],
    fir_features: Sequence[Mapping[str, Any]],
) -> list[str]:
  """Publishes the boundary features a run used into its dataset directory.

  Args:
    dataset_dir: Run directory the other artifacts go to.
    country_features: Country features as returned by
      `metadata_loader.load_country_geojson`, i.e. with normalized lowercase
      `name`, `iso_a2`, and `iso_a3` properties.
    fir_features: FIR features as returned by
      `metadata_loader.load_firs_geojson`, i.e. with normalized `fir_id`,
      `fir_icao`, `firname`, `fir_to_country_iso2s` and `type` properties.

  Returns:
    The written file paths.
  """
  written: list[str] = []
  for filename, features in (
      (constants.COUNTRIES_GEOJSON_FILENAME, country_features),
      (constants.FIRS_GEOJSON_FILENAME, fir_features),
  ):
    path = os.path.join(dataset_dir, filename)
    write_json_output(
        {
            constants.KEY_TYPE: constants.GEOJSON_FEATURE_COLLECTION,
            constants.GEOJSON_FEATURES: list(features),
        },
        path,
        compact=True,
    )
    written.append(path)
  return written
