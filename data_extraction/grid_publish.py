"""Pure Python and NumPy grid publication, LOD generation, and metadata."""

from collections.abc import Iterable, Mapping, Sequence
import gzip
import math
import os
from typing import Any, NamedTuple

import numpy as np

from common import artifact_io
from common import constants
from data_extraction import grid_rasterizer

GRID_TYPES = (constants.GRID_KIND_DENSITY, constants.GRID_KIND_IMPACT)

# Only grids at or above this resolution get downsampled LOD pyramids; below
# it the coarser levels would be uselessly small.
_MIN_LOD_WIDTH = 3600
_MIN_LOD_HEIGHT = 1800


def all_export_keys() -> list[tuple[str, str, int]]:
  """Every `(grid_type, bucket_type, bucket_index)` the pipeline must emit.

  Returned explicitly so the graph can seed empty slices for buckets that no
  flight touched, which is what guarantees a complete set of artifacts rather
  than a set whose size depends on the input.

  Returns:
    List of (grid_type, bucket_type, bucket_index) tuples.
  """
  keys: list[tuple[str, str, int]] = []
  for grid_type in GRID_TYPES:
    keys.append((grid_type, constants.GRID_FAMILY_TOTAL, 0))
    for month in range(1, 13):
      keys.append((grid_type, constants.GRID_FAMILY_MONTHLY, month))
    for hour in range(24):
      keys.append((grid_type, constants.GRID_FAMILY_HOURLY, hour))
  return keys


def artifact_prefix(grid_type: str, bucket_type: str, bucket_idx: int) -> str:
  """Filename prefix for one exported grid, without extension or LOD suffix.

  Args:
    grid_type: Grid category, either 'density' or 'impact'.
    bucket_type: Temporal family ('total', 'monthly', or 'hourly').
    bucket_idx: Bucket index (0 for total, 1-12 for month, 0-23 for hour).

  Returns:
    The grid filename prefix string.

  Raises:
    ValueError: If `grid_type` or `bucket_type` is not recognized, or
      `bucket_idx` is out of range for `bucket_type`.
  """
  if grid_type not in GRID_TYPES:
    raise ValueError(f"unknown grid type: {grid_type!r}")
  if bucket_type == constants.GRID_FAMILY_TOTAL:
    valid_indices, template = range(1), "total_global_{grid_type}_grid"
  elif bucket_type == constants.GRID_FAMILY_MONTHLY:
    valid_indices = range(1, 13)
    template = "monthly_global_{grid_type}_grid_{bucket_idx:02d}"
  elif bucket_type == constants.GRID_FAMILY_HOURLY:
    valid_indices = range(24)
    template = "hourly_global_{grid_type}_grid_{bucket_idx:02d}"
  else:
    raise ValueError(f"unknown bucket type: {bucket_type!r}")
  if bucket_idx not in valid_indices:
    raise ValueError(
        f"bucket index {bucket_idx!r} out of range for {bucket_type!r} grids"
    )
  return template.format(grid_type=grid_type, bucket_idx=bucket_idx)


# grid_meta.json key holding, per grid type, the largest reference-scaled
# absolute cell value across all of that type's buckets. The UI uses it as the
# top of the colour scale and has no fallback, so every published run must
# carry it.
VALUE_MAX_META_KEY = "value_max_ref_scaled"


class GridPublishResult(NamedTuple):
  """What publishing one grid reports back for the shared metadata.

  Attributes:
    key: `(grid_type, bucket_type, bucket_index)` identifying the exported grid.
    prefix: Filename prefix used for the exported artifacts.
    value_max_ref_scaled: Reference-scaled maximum absolute value in the grid.
  """

  key: tuple[str, str, int]
  prefix: str
  value_max_ref_scaled: float


def reduce_value_max(results: Iterable[GridPublishResult]) -> dict[str, float]:
  """Reduces per-grid maxima to one colour-scale ceiling per grid type.

  One scale per grid type (shared by total, monthly and hourly buckets) keeps
  colours comparable when the user switches between them.

  Args:
    results: One result per published grid.

  Returns:
    `{grid_type: max}` for every entry of GRID_TYPES.

  Raises:
    ValueError: unless `results` covers every export key exactly once with a
      finite, non-negative maximum. Publishing a partial scale would silently
      mis-colour the grids that are present.
  """
  results = list(results)
  seen = [item.key for item in results]
  expected = all_export_keys()
  if sorted(seen) != sorted(expected):
    missing = sorted(set(expected) - set(seen))
    extra = sorted(set(seen) - set(expected))
    raise ValueError(
        f"expected exactly one result per {len(expected)} export keys, got "
        f"{len(seen)} (missing: {missing[:5]}, unexpected: {extra[:5]})"
    )
  value_max: dict[str, float] = {}
  for item in results:
    max_value = float(item.value_max_ref_scaled)
    if not math.isfinite(max_value) or max_value < 0.0:
      raise ValueError(f"grid {item.key} has invalid maximum {max_value!r}")
    grid_type = item.key[0]
    value_max[grid_type] = max(value_max.get(grid_type, 0.0), max_value)
  return value_max


def lod_levels(
    grid_config: grid_rasterizer.GridConfig,
) -> Sequence[tuple[int, int]]:
  """Returns the LOD levels `(lod_idx, downsample_factor)` for a grid.

  Args:
    grid_config: GridConfig defining dimensions and coordinates.

  Returns:
    A sequence of `(lod_idx, factor)` tuples if the grid resolution meets or
    exceeds _MIN_LOD_WIDTH x _MIN_LOD_HEIGHT; otherwise an empty sequence.

  Raises:
    ValueError: If grid dimensions qualify for LOD generation but width or
      height is not divisible by 4.
  """
  if (
      grid_config.width >= _MIN_LOD_WIDTH
      and grid_config.height >= _MIN_LOD_HEIGHT
  ):
    if grid_config.width % 4 != 0 or grid_config.height % 4 != 0:
      raise ValueError(
          f"Grid dimensions ({grid_config.width}x{grid_config.height}) for LOD"
          " generation must be divisible by 4, but width % 4 ="
          f" {grid_config.width % 4} and height % 4 ="
          f" {grid_config.height % 4}."
      )
    return ((1, 2), (0, 4))
  return ()


def _validate_value_max(value_max: Mapping[str, float]) -> None:
  """Checks that `value_max` is a complete, valid colour-scale table.

  Args:
    value_max: `{grid_type: reference-scaled absolute max}`.

  Raises:
    ValueError: if `value_max` does not have exactly the GRID_TYPES keys or a
      value is not finite and non-negative.
  """
  if set(value_max) != set(GRID_TYPES):
    raise ValueError(
        f"value_max must have exactly the keys {sorted(GRID_TYPES)}, got "
        f"{sorted(value_max)}"
    )
  for grid_type, max_value in value_max.items():
    if not math.isfinite(max_value) or max_value < 0.0:
      raise ValueError(f"invalid {grid_type} maximum {max_value!r}")


def _cell_steps(grid_config: grid_rasterizer.GridConfig) -> tuple[float, float]:
  """Returns the full-resolution `(lon_step, lat_step)` cell size in degrees.

  A zero-width or zero-height grid yields a step of 0.0 on that axis.

  Args:
    grid_config: Full-resolution grid geometry.

  Returns:
    Tuple of (lon_step, lat_step) cell dimensions in degrees.
  """
  lon_step = (
      (grid_config.max_lon - grid_config.min_lon) / grid_config.width
      if grid_config.width > 0
      else 0.0
  )
  lat_step = (
      (grid_config.max_lat - grid_config.min_lat) / grid_config.height
      if grid_config.height > 0
      else 0.0
  )
  return lon_step, lat_step


def _lod_geometry(
    grid_config: grid_rasterizer.GridConfig,
    *,
    lon_step: float,
    lat_step: float,
) -> dict[str, dict[str, Any]]:
  """Builds the per-LOD dimensions and cell sizes, keyed by LOD index string.

  LOD 2 is the full-resolution grid; coarser levels come from `lod_levels`.

  Args:
    grid_config: Full-resolution grid geometry.
    lon_step: Full-resolution longitude cell size in degrees.
    lat_step: Full-resolution latitude cell size in degrees.

  Returns:
    `{str(lod_idx): {"width", "height", "lon_step", "lat_step", "lod"}}`.

  Raises:
    ValueError: If grid dimensions qualify for LOD generation but are not
      divisible by 4.
  """
  lods: dict[str, dict[str, Any]] = {
      "2": {
          constants.KEY_WIDTH: grid_config.width,
          constants.KEY_HEIGHT: grid_config.height,
          constants.KEY_LON_STEP: lon_step,
          constants.KEY_LAT_STEP: lat_step,
          constants.KEY_LOD: 2,
      }
  }
  for lod_idx, factor in lod_levels(grid_config):
    lods[str(lod_idx)] = {
        constants.KEY_WIDTH: grid_config.width // factor,
        constants.KEY_HEIGHT: grid_config.height // factor,
        constants.KEY_LON_STEP: lon_step * factor,
        constants.KEY_LAT_STEP: lat_step * factor,
        constants.KEY_LOD: lod_idx,
    }
  return lods


def build_shared_grid_meta(
    grid_config: grid_rasterizer.GridConfig,
    value_max: Mapping[str, float],
) -> dict[str, Any]:
  """Builds the per-run grid metadata dictionary covering all LODs.

  Args:
    grid_config: Full-resolution grid geometry.
    value_max: `{grid_type: reference-scaled absolute max}` for every entry of
      GRID_TYPES, as returned by `reduce_value_max`.

  Returns:
    The metadata dictionary written to grid_meta.json.

  Raises:
    ValueError: if `value_max` does not have exactly the GRID_TYPES keys, a
      value is not finite and non-negative, or grid dimensions qualify for LOD
      generation but are not divisible by 4.
  """
  _validate_value_max(value_max)
  lon_step, lat_step = _cell_steps(grid_config)
  bbox = {
      constants.KEY_MIN_LON: grid_config.min_lon,
      constants.KEY_MAX_LON: grid_config.max_lon,
      constants.KEY_MIN_LAT: grid_config.min_lat,
      constants.KEY_MAX_LAT: grid_config.max_lat,
  }
  lods = _lod_geometry(grid_config, lon_step=lon_step, lat_step=lat_step)

  return {
      constants.KEY_GRID_NAME: "global_grid",
      constants.KEY_WIDTH: grid_config.width,
      constants.KEY_HEIGHT: grid_config.height,
      constants.KEY_MIN_LON: grid_config.min_lon,
      constants.KEY_MAX_LON: grid_config.max_lon,
      constants.KEY_MIN_LAT: grid_config.min_lat,
      constants.KEY_MAX_LAT: grid_config.max_lat,
      constants.KEY_LON_STEP: lon_step,
      constants.KEY_LAT_STEP: lat_step,
      constants.KEY_BBOX: bbox,
      constants.KEY_DTYPE: "float16",
      constants.KEY_ENCODING: constants.ENCODING_SIGNED_LOG10_REF_SCALED,
      "compression": "gzip",
      constants.KEY_LOD: 2,
      constants.KEY_ORIGIN: constants.ORIGIN_LOWER_LEFT,
      constants.KEY_LODS: lods,
      # Impact grids are summed energy forcing in MJ per cell. These are the
      # exact multipliers this run used for every `co2e_{gwp}_tonnes` column,
      # published next to the grids so the UI can label the impact key in
      # t CO2e without keeping its own (potentially stale) copy. Keys match
      # the server's SUPPORTED_GWPS.
      "gwp_tonnes_co2e_per_mj": dict(constants.GWP_TONNES_PER_MJ),
      # Top of the colour scale per grid type, in the reference-scaled units
      # the grids are log-encoded in (see
      # `grid_rasterizer.ref_scaled_abs_max`). Valid for every bucket and LOD.
      VALUE_MAX_META_KEY: {k: float(value_max[k]) for k in GRID_TYPES},
  }


def write_shared_grid_meta(
    grid_config: grid_rasterizer.GridConfig,
    output_dir: str,
    value_max: Mapping[str, float],
) -> None:
  """Writes the per-run `grid_meta.json` file.

  Args:
    grid_config: GridConfig defining dimensions and coordinates.
    output_dir: Directory path where `grid_meta.json` will be written.
    value_max: Mapping from grid type to reference-scaled maximum value.

  Raises:
    ValueError: If `value_max` does not have exactly the `GRID_TYPES` keys, a
      value is not finite and non-negative, or grid dimensions qualify for LOD
      generation but are not divisible by 4.
  """
  artifact_io.write_json_output(
      build_shared_grid_meta(grid_config, value_max),
      os.path.join(output_dir, constants.SHARED_GRID_META_FILENAME),
  )


def _write_level(
    grid_array: np.ndarray,
    *,
    width: int,
    height: int,
    prefix: str,
    output_dir: str,
) -> None:
  """Encodes, gzip-compresses, and writes a single grid LOD level binary."""
  encoded = grid_rasterizer.encode_grid_to_float16_log10(
      grid_array, width, height
  )
  compressed = gzip.compress(encoded.tobytes(), compresslevel=6)
  artifact_io.write_binary_output(
      compressed,
      os.path.join(output_dir, f"{prefix}{constants.GRID_BIN_GZ_SUFFIX}"),
  )


def publish_grid_artifacts(
    key: tuple[str, str, int],
    grid_arr: np.ndarray,
    *,
    grid_config: grid_rasterizer.GridConfig,
    output_dir: str,
) -> GridPublishResult:
  """Encodes one finalized grid and writes its LOD pyramid.

  A plain function rather than a method, because this is the whole of the
  grid derivation step: given a raw combined grid it is a pure function of
  `grid_config`. The standalone derive binary calls it directly, with no Beam
  involved.

  It does not write grid_meta.json: the colour-scale maxima in there depend on
  every grid of the run, so the caller reduces the returned results with
  `reduce_value_max` and calls `write_shared_grid_meta` once at the end.

  Args:
    key: (grid_type, bucket_type, bucket_idx) tuple.
    grid_arr: 2D numpy array of grid values.
    grid_config: GridConfig defining dimensions and coordinates.
    output_dir: Directory path where artifacts will be written.

  Returns:
    The key, the artifact filename prefix, and the grid's reference-scaled
    absolute maximum.

  Raises:
    ValueError: If `bucket_type` is not one of `'total'`, `'monthly'`, or
      `'hourly'`, if grid dimensions qualify for LOD generation but are not
      divisible by 4, or if the grid is empty or contains a non-finite value.
  """
  grid_type, bucket_type, bucket_idx = key
  prefix = artifact_prefix(grid_type, bucket_type, bucket_idx)
  value_max = grid_rasterizer.ref_scaled_abs_max(
      grid_arr, grid_config.width, grid_config.height
  )

  _write_level(
      grid_arr,
      width=grid_config.width,
      height=grid_config.height,
      prefix=prefix,
      output_dir=output_dir,
  )

  for lod_idx, factor in lod_levels(grid_config):
    lod_arr = grid_rasterizer.downsample_grid_sum(grid_arr, factor)
    _write_level(
        lod_arr,
        width=grid_config.width // factor,
        height=grid_config.height // factor,
        prefix=f"{prefix}_lod{lod_idx}",
        output_dir=output_dir,
    )

  return GridPublishResult(
      key=(str(grid_type), str(bucket_type), int(bucket_idx)),
      prefix=prefix,
      value_max_ref_scaled=value_max,
  )
