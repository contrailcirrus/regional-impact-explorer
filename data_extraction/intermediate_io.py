"""The contract between the `reduce` and `derive` halves of the pipeline.

`reduce` reads flight-segment Parquet shards and produces country and FIR
aggregate rows plus a set of grid slices. `derive` turns those into published
artifacts in seconds without requiring a full rescan of flight segment Parquet
inputs.

This module is the single place that defines the intermediate format, so it is
the single place to test it and the single place to version it. Nothing else
should know the file layout.

Layout:

    {root}/{year}/{dataset}/runs/{run_id}/
      run.json                        provenance, schema version and shard
                                      loss; written after the aggregates
      provenance.json                 the reduce run's published provenance
                                      (reference inputs, code version);
                                      written before the pipeline starts
      country_agg.jsonl               one row per (name, iso, month, hour)
      fir_agg.jsonl                   one row per (firname, icao, iso, type,
                                      month, hour)
      grid_slices/{artifact_prefix}.npz   unencoded float32 slice (sparse or
                                          dense)

Deliberately absent: FIR and country areas. They are properties of the
geometry files, not of the flight data, so `derive` joins them from
`--firs_path` and `--country_geojson_path` at derivation time and publishes
`countries.geojson` and `firs.geojson` directly into the run directory.
"""

from collections.abc import Iterable, Iterator, Mapping
import dataclasses
import datetime
import io
import json
import os
import re
from typing import Any

import numpy as np

from common import constants
from common import fs
from data_extraction import grid_rasterizer

# Bump when the on-disk shape changes in a way `derive` must notice.
SCHEMA_VERSION = 2

_RUN_MANIFEST = "run.json"
_PROVENANCE = "provenance.json"
_COUNTRY_AGG = "country_agg.jsonl"
_FIR_AGG = "fir_agg.jsonl"
_GRID_SLICES = "grid_slices"
_KEY_GRID_CONFIG = "grid_config"
_NPZ_KEY_LAYOUT = "layout"
_NPZ_KEY_SHAPE = "shape"
_NPZ_KEY_ROWS = "rows"
_NPZ_KEY_COLS = "cols"
_NPZ_KEY_VALUES = "values"

# Below this fraction of non-zero cells a slice is cheaper to store sparsely.
# Above it the coordinate arrays cost more than the values they index.
_SPARSE_FILL_THRESHOLD = 0.25

# Mirror `artifact_io.validate_run_id`, which every run id must pass.
_MAX_RUN_ID_LENGTH = 128
_RUN_ID_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]", re.ASCII)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def dataset_root(intermediate_root: str, year: int, dataset: str) -> str:
  """Returns the intermediate directory holding a dataset's runs."""
  return os.path.join(intermediate_root, str(year), dataset)


def make_run_id(tag: str = "unknown") -> str:
  """Builds a sortable, attributable run identifier.

  A timestamp rather than a content hash, so runs sort chronologically and
  retention is obvious at a glance. `tag` makes a run attributable to the code
  that produced it; the caller supplies whatever version handle it has (a
  commit, a version tag, a build label).

  The tag may come from the environment, so characters a run id may not
  contain are replaced with '_' and it is truncated to keep the id within
  `artifact_io.validate_run_id`'s length limit.

  Args:
    tag: Build or version identifier to append after the UTC timestamp.

  Returns:
    A validated run identifier string `<UTC-timestamp>-<safe_tag>`.
  """
  stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
      "%Y-%m-%dT%H-%M-%SZ"
  )
  safe_tag = _RUN_ID_UNSAFE_CHARS.sub("_", tag)
  safe_tag = safe_tag[: _MAX_RUN_ID_LENGTH - len(stamp) - 1] or "unknown"
  return f"{stamp}-{safe_tag}"


def run_dir(root: str, run_id: str) -> str:
  """Returns the intermediate directory path for a specific `run_id`."""
  return os.path.join(root, constants.RUNS_DIR, run_id)


def manifest_path(root: str, run_id: str) -> str:
  """Returns the path to the `run.json` manifest for `run_id`."""
  return os.path.join(run_dir(root, run_id), _RUN_MANIFEST)


def country_agg_path(root: str, run_id: str) -> str:
  """Returns the path to the `country_agg.jsonl` file for `run_id`."""
  return os.path.join(run_dir(root, run_id), _COUNTRY_AGG)


def fir_agg_path(root: str, run_id: str) -> str:
  """Returns the path to the `fir_agg.jsonl` file for `run_id`."""
  return os.path.join(run_dir(root, run_id), _FIR_AGG)


def grid_slice_path(root: str, run_id: str, prefix: str) -> str:
  """Path of one raw grid slice, keyed by the artifact it derives.

  Keyed by published prefix (`total_global_impact_grid`, ...) rather than by
  `(type, month, hour)`. The pipeline only ever publishes a fixed set of
  buckets, and they are already materialised as combined grids, so this needs
  no extra combine. The cost is that a *new* bucketing (per-weekday, say)
  still requires a reduce re-run.

  Args:
    root: Dataset intermediate root directory.
    run_id: Run identifier.
    prefix: Published grid artifact prefix.

  Returns:
    Path to the `.npz` slice file under `grid_slices/`.
  """
  return os.path.join(run_dir(root, run_id), _GRID_SLICES, f"{prefix}.npz")


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RunManifest:
  """Provenance for one `reduce` run.

  `grid_config` lives here rather than in `derive` because rasterisation
  happens inside the per-segment DoFn. Re-rasterising at a different
  resolution is therefore a `reduce` parameter and costs a full scan; every
  other published choice does not.

  The manifest is written only after the failed-shard budget check passed and
  after both aggregate files, so its presence marks the aggregates as
  complete. `failed_shard_count` records how many input shards that budget
  let through, so a later `derive` can tell a lossless run from a lossy one.
  """

  run_id: str
  failed_shard_count: int
  input_glob: str = ""
  input_file_count: int = 0
  grid_config: grid_rasterizer.GridConfig | None = None
  started_at: str = ""
  finished_at: str = ""
  schema_version: int = SCHEMA_VERSION

  def to_dict(self) -> dict[str, Any]:
    """Serializes the manifest to a JSON-compatible dictionary."""
    result = dataclasses.asdict(self)
    if self.grid_config is not None:
      result[_KEY_GRID_CONFIG] = self.grid_config.to_dict()
    return result

  @classmethod
  def from_dict(cls, raw: Mapping[str, Any]) -> "RunManifest":
    """Parses a manifest written by this schema version.

    Args:
      raw: The decoded `run.json` contents.

    Returns:
      The parsed manifest. Unknown fields are ignored.

    Raises:
      ValueError: If the schema version differs from `SCHEMA_VERSION` or a
        required field is missing.
    """
    version = int(raw.get(constants.KEY_SCHEMA_VERSION, 0))
    if version != SCHEMA_VERSION:
      raise ValueError(
          f"intermediate schema version {version} is not readable by this "
          f"build, which expects {SCHEMA_VERSION}"
      )
    known = {f.name for f in dataclasses.fields(cls)}
    missing = [
        f.name
        for f in dataclasses.fields(cls)
        if f.default is dataclasses.MISSING and f.name not in raw
    ]
    if missing:
      raise ValueError(f"intermediate manifest is missing fields {missing}")
    kwargs = {k: item_value for k, item_value in raw.items() if k in known}
    grid_cfg_val = kwargs.get(_KEY_GRID_CONFIG)
    if grid_cfg_val is not None:
      if isinstance(grid_cfg_val, grid_rasterizer.GridConfig):
        kwargs[_KEY_GRID_CONFIG] = grid_cfg_val
      elif isinstance(grid_cfg_val, Mapping):
        kwargs[_KEY_GRID_CONFIG] = grid_rasterizer.GridConfig.from_dict(
            grid_cfg_val
        )
      else:
        raise ValueError(
            "grid_config in manifest must be a dict or GridConfig, got"
            f" {type(grid_cfg_val).__name__}"
        )
    return cls(**kwargs)


def write_manifest(manifest: RunManifest, root: str) -> str:
  """Atomically writes `manifest` to `run.json` and returns its path."""
  path = manifest_path(root, manifest.run_id)
  fs.ensure_parent_dir(path)
  with fs.atomic_open_for_write(path, "w") as f:
    json.dump(manifest.to_dict(), f, indent=2)
  return path


def manifest_exists(root: str, run_id: str) -> bool:
  """Returns whether `run.json` exists for `run_id` under `root`."""
  return fs.exists(manifest_path(root, run_id))


def read_manifest(root: str, run_id: str) -> RunManifest:
  """Reads and parses `run.json` for `run_id` under `root`."""
  with fs.open_file(manifest_path(root, run_id), "r") as f:
    return RunManifest.from_dict(json.load(f))


def provenance_path(root: str, run_id: str) -> str:
  """Returns the path to the reduce run's `provenance.json` for `run_id`."""
  return os.path.join(run_dir(root, run_id), _PROVENANCE)


def write_provenance(
    provenance: Mapping[str, Any], root: str, run_id: str
) -> str:
  """Atomically writes the reduce run's provenance and returns its path.

  Kept beside the aggregates so a later `derive` can carry it into the run it
  publishes, even when that run's directory differs from the reduce run's.

  Args:
    provenance: The document `provenance.build_provenance` returned.
    root: Intermediate dataset root.
    run_id: Intermediate run id.

  Returns:
    The written path.
  """
  path = provenance_path(root, run_id)
  fs.ensure_parent_dir(path)
  with fs.atomic_open_for_write(path, "w") as f:
    json.dump(dict(provenance), f, indent=2)
  return path


def read_provenance(root: str, run_id: str) -> dict[str, Any] | None:
  """Returns the reduce run's provenance, or None for runs that predate it."""
  path = provenance_path(root, run_id)
  if not fs.exists(path):
    return None
  with fs.open_file(path, "r") as f:
    return json.load(f)


# ---------------------------------------------------------------------------
# Aggregate rows
#
# Encoded with named fields rather than positional arrays: a self-describing
# row avoids positional indexing errors and is worth the handful of extra bytes.
# ---------------------------------------------------------------------------


def encode_country_row(
    row: constants.CountryRow,
) -> constants.EncodedCountryRow:
  """Encodes a `CountryRow` tuple into a named-field dictionary."""
  (name, iso_a2, month, local_hour), (
      segment_count,
      contrail_segment_count,
      prop_sum_ef_mj,
      local_flight_impact,
      total_flight_impact,
      outgoing_flight_impact,
      total_flight_distance_km,
      contrail_flight_distance_km,
  ) = row
  return {
      "name": name,
      "iso_a2": iso_a2,
      "month": int(month),
      "local_hour": int(local_hour),
      "segment_count": int(segment_count),
      "contrail_segment_count": int(contrail_segment_count),
      "prop_sum_ef_mj": float(prop_sum_ef_mj),
      "local_flight_impact": float(local_flight_impact),
      "total_flight_impact": float(total_flight_impact),
      "outgoing_flight_impact": float(outgoing_flight_impact),
      "total_flight_distance_km": float(total_flight_distance_km),
      "contrail_flight_distance_km": float(contrail_flight_distance_km),
  }


def decode_country_row(
    raw: constants.EncodedCountryRow | Mapping[str, Any],
) -> constants.CountryRow:
  """Decodes a serialized country record dictionary into a CountryRow tuple."""
  key = (
      str(raw["name"]),
      str(raw["iso_a2"]),
      int(raw["month"]),
      int(raw["local_hour"]),
  )
  values = (
      int(raw["segment_count"]),
      int(raw["contrail_segment_count"]),
      float(raw["prop_sum_ef_mj"]),
      float(raw["local_flight_impact"]),
      float(raw["total_flight_impact"]),
      float(raw["outgoing_flight_impact"]),
      float(raw["total_flight_distance_km"]),
      float(raw["contrail_flight_distance_km"]),
  )
  return key, values


def encode_fir_row(row: constants.FirRow) -> constants.EncodedFirRow:
  """Encodes a `FirRow` tuple into a named-field dictionary."""
  (firname, fir_icao, fir_iso2, fir_type, month, local_hour), (
      segment_count,
      contrail_segment_count,
      prop_sum_ef_mj,
      total_flight_distance_km,
      contrail_flight_distance_km,
  ) = row
  return {
      "firname": firname,
      "fir_icao": fir_icao,
      "fir_to_country_iso2s": fir_iso2,
      "type": fir_type,
      "month": int(month),
      "local_hour": int(local_hour),
      "segment_count": int(segment_count),
      "contrail_segment_count": int(contrail_segment_count),
      "prop_sum_ef_mj": float(prop_sum_ef_mj),
      "total_flight_distance_km": float(total_flight_distance_km),
      "contrail_flight_distance_km": float(contrail_flight_distance_km),
  }


def decode_fir_row(
    raw: constants.EncodedFirRow | Mapping[str, Any],
) -> constants.FirRow:
  """Decodes a serialized FIR record dictionary into a FirRow tuple."""
  key = (
      str(raw["firname"]),
      str(raw["fir_icao"]),
      str(raw["fir_to_country_iso2s"]),
      str(raw["type"]),
      int(raw["month"]),
      int(raw["local_hour"]),
  )
  values = (
      int(raw["segment_count"]),
      int(raw["contrail_segment_count"]),
      float(raw["prop_sum_ef_mj"]),
      float(raw["total_flight_distance_km"]),
      float(raw["contrail_flight_distance_km"]),
  )
  return key, values


def write_country_agg(
    rows: Iterable[constants.CountryRow], root: str, run_id: str
) -> str:
  """Writes country rows to `country_agg.jsonl` and returns its path."""
  return _write_jsonl(
      (encode_country_row(row) for row in rows), country_agg_path(root, run_id)
  )


def read_country_agg(root: str, run_id: str) -> list[constants.CountryRow]:
  """Reads and decodes combined country rows from `country_agg.jsonl`."""
  return [
      decode_country_row(raw)
      for raw in _read_jsonl(country_agg_path(root, run_id))
  ]


def write_fir_agg(
    rows: Iterable[constants.FirRow], root: str, run_id: str
) -> str:
  """Writes combined FIR rows to `fir_agg.jsonl` and returns its path."""
  return _write_jsonl(
      (encode_fir_row(row) for row in rows), fir_agg_path(root, run_id)
  )


def read_fir_agg(root: str, run_id: str) -> list[constants.FirRow]:
  """Reads and decodes combined FIR rows from `fir_agg.jsonl`."""
  return [
      decode_fir_row(raw) for raw in _read_jsonl(fir_agg_path(root, run_id))
  ]


# ---------------------------------------------------------------------------
# Grid slices
# ---------------------------------------------------------------------------


def write_grid_slice(
    grid: np.ndarray,
    *,
    root: str,
    run_id: str,
    prefix: str,
) -> str:
  """Stores one 2D slice, sparse or dense depending on how full it is.

  Hardcoding sparse would be a trap: the observed ~580k non-zero cells per
  total grid make it a large win today, but a denser dataset would make the
  coordinate arrays cost more than the grid they describe.

  Args:
    grid: 2D float32 grid array to store.
    root: Dataset intermediate root directory.
    run_id: Run identifier.
    prefix: Published grid artifact prefix.

  Returns:
    Path to the written `.npz` slice file.
  """
  path = grid_slice_path(root, run_id, prefix)
  fs.ensure_parent_dir(path)

  nz_rows, nz_cols = np.nonzero(grid)
  fill = (nz_rows.size / grid.size) if grid.size else 0.0

  buf = io.BytesIO()
  if fill <= _SPARSE_FILL_THRESHOLD:
    np.savez_compressed(
        buf,
        layout=np.array(constants.GRID_ACC_SPARSE),
        shape=np.asarray(grid.shape, dtype=np.int64),
        rows=nz_rows.astype(np.int32),
        cols=nz_cols.astype(np.int32),
        values=grid[nz_rows, nz_cols].astype(np.float32),
    )
  else:
    np.savez_compressed(
        buf,
        layout=np.array(constants.GRID_ACC_DENSE),
        shape=np.asarray(grid.shape, dtype=np.int64),
        values=grid.astype(np.float32),
    )

  with fs.atomic_open_for_write(path, "wb") as f:
    f.write(buf.getvalue())
  return path


def grid_slice_exists(root: str, run_id: str, prefix: str) -> bool:
  """Returns whether the intermediate `.npz` slice exists for `prefix`."""
  return fs.exists(grid_slice_path(root, run_id, prefix))


def read_grid_slice(root: str, run_id: str, prefix: str) -> np.ndarray:
  """Reads one slice back as a dense 2D float32 array."""
  path = grid_slice_path(root, run_id, prefix)
  with fs.open_file(path, "rb") as f:
    payload = np.load(io.BytesIO(f.read()), allow_pickle=False)

  shape = tuple(int(item_value) for item_value in payload[_NPZ_KEY_SHAPE])
  layout = str(payload[_NPZ_KEY_LAYOUT])
  if layout == constants.GRID_ACC_DENSE:
    return payload[_NPZ_KEY_VALUES].astype(np.float32).reshape(shape)

  grid = np.zeros(shape, dtype=np.float32)
  grid[payload[_NPZ_KEY_ROWS], payload[_NPZ_KEY_COLS]] = payload[
      _NPZ_KEY_VALUES
  ]
  return grid


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _write_jsonl(records: Iterable[Mapping[str, Any]], path: str) -> str:
  """Atomically writes `records` as newline-delimited JSON to `path`."""
  fs.ensure_parent_dir(path)
  with fs.atomic_open_for_write(path, "w") as f:
    for record in records:
      f.write(json.dumps(record, sort_keys=True))
      f.write("\n")
  return path


def _read_jsonl(path: str) -> Iterator[dict[str, Any]]:
  """Yields decoded JSON objects from non-empty lines of `path`."""
  with fs.open_file(path, "r") as f:
    for line in f:
      line = line.strip()
      if line:
        yield json.loads(line)
