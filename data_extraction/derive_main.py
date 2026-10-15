"""Re-derives published artifacts from a stored intermediate, without Beam.

The reduce half of the pipeline reads every segment Parquet file, which takes
hours for a global year. This binary reads only the aggregate rows (at most
288 per country or FIR and year) and 74 grid slices, and republishes every
artifact in minutes. Use it whenever a change
affects only derivation: metric definitions, GWP horizons, ranking, grid
encoding, LOD levels, area denominators.

Deliberately not a Beam pipeline. The work is a loop over 74 grids on one
machine; introducing a runner would add minutes of startup to a job that does
not need distribution, and would re-couple derivation to a distributed Beam
runner.

Usage:

  python -m data_extraction.derive_main \
    --intermediate_prefix=/path/to/intermediate \
    --input_year=<year> --dataset=my_dataset \
    --run_id=<run_id> \
    --output_prefix=/path/to/data \
    --firs_path=/path/to/firs.geojson \
    --country_geojson_path=/path/to/countries.geojson \
    --population_path=/path/to/population.csv

See README.md for every flag.
"""

from collections.abc import Sequence
import dataclasses
import datetime

from absl import app
from absl import logging

from common import artifact_io
from common import fs
from data_extraction import common_flags
from data_extraction import derive
from data_extraction import grid_publish
from data_extraction import grid_rasterizer
from data_extraction import intermediate_io
from data_extraction import metadata_loader


@dataclasses.dataclass(frozen=True)
class DeriveConfig:
  """Configuration for a contrail derivation run."""

  dataset: str
  year: int
  output_prefix: str
  intermediate_prefix: str
  run_id: str
  country_geojson_path: str
  firs_path: str
  publish_run_id: str = ""
  max_failed_shards: int = 0
  population_path: str = ""
  ghg_emissions_path: str = ""
  country_stats_path: str = ""
  expected_fir_count: int = 0

  def __post_init__(self) -> None:
    """Validates required paths, run identifiers, and baseline configuration."""
    for field in dataclasses.fields(self):
      value = getattr(self, field.name)
      if isinstance(value, str):
        object.__setattr__(self, field.name, value.strip())
    common_flags.validate_dataset(self.dataset)
    common_flags.validate_input_year(self.year)
    if not self.output_prefix:
      raise ValueError("--output_prefix is required.")
    if not self.intermediate_prefix:
      raise ValueError("--intermediate_prefix is required.")
    common_flags.require_distinct_prefixes(
        intermediate_root=self.intermediate_prefix,
        output_root=self.output_prefix,
    )
    if not self.run_id:
      raise ValueError("--run_id is required.")
    artifact_io.validate_run_id(self.run_id, label="--run_id")
    if self.publish_run_id:
      artifact_io.validate_run_id(self.publish_run_id, label="--publish_run_id")
    if not self.firs_path:
      raise ValueError("--firs_path is required.")
    if not self.country_geojson_path:
      raise ValueError("--country_geojson_path is required.")
    common_flags.validate_baseline_source(
        population_path=self.population_path,
        ghg_emissions_path=self.ghg_emissions_path,
        country_stats_path=self.country_stats_path,
    )

  @classmethod
  def from_flags(cls) -> "DeriveConfig":
    """Constructs DeriveConfig from absl flags."""
    return cls(
        dataset=common_flags.require_dataset(),
        year=common_flags.require_input_year(),
        output_prefix=common_flags.OUTPUT_PREFIX.value or "",
        intermediate_prefix=common_flags.INTERMEDIATE_PREFIX.value or "",
        run_id=common_flags.RUN_ID.value or "",
        country_geojson_path=common_flags.COUNTRY_GEOJSON_PATH.value or "",
        firs_path=common_flags.FIRS_PATH.value or "",
        publish_run_id=common_flags.PUBLISH_RUN_ID.value or "",
        max_failed_shards=common_flags.MAX_FAILED_SHARDS.value,
        population_path=common_flags.POPULATION_PATH.value or "",
        ghg_emissions_path=common_flags.GHG_EMISSIONS_PATH.value or "",
        country_stats_path=common_flags.COUNTRY_STATS_PATH.value or "",
        expected_fir_count=common_flags.EXPECTED_FIR_COUNT.value,
    )


def _grid_config_from_manifest(
    manifest: intermediate_io.RunManifest,
) -> grid_rasterizer.GridConfig:
  """Rebuilds the grid geometry the reduce stage actually rasterised at.

  Taken from the manifest rather than from flags: the slices on disk were
  produced at one specific resolution, and silently re-encoding them at
  another would produce artifacts that look right and are wrong.

  Args:
    manifest: The intermediate run manifest containing `grid_config`.

  Returns:
    The validated `GridConfig` reconstructed from the manifest.

  Raises:
    ValueError: If `grid_config` is missing from the manifest or fails LOD
      validation.
  """
  if isinstance(manifest.grid_config, grid_rasterizer.GridConfig):
    config = manifest.grid_config
  elif isinstance(manifest.grid_config, dict) and manifest.grid_config:
    config = grid_rasterizer.GridConfig.from_dict(manifest.grid_config)
  else:
    raise ValueError(
        f"run {manifest.run_id} has no grid_config in its manifest; its grid "
        "slices cannot be safely re-encoded"
    )
  grid_publish.lod_levels(config)
  return config


def check_failed_shard_budget(
    manifest: intermediate_io.RunManifest, max_failed: int
) -> None:
  """Refuses to republish a run that lost more shards than allowed.

  Reduce already enforced its own `--max_failed_shards` before writing the
  manifest, but that budget may have been looser than the one in force now.
  Applying the current budget again keeps derive from quietly republishing a
  dataset that is missing input.

  Args:
    manifest: The manifest of the intermediate run to republish.
    max_failed: Budget of tolerated failed shards; negative disables the check.

  Raises:
    ValueError: If the run recorded more failed shards than the budget.
  """
  count = manifest.failed_shard_count
  if max_failed < 0:
    if count:
      logging.warning(
          "Failed-shard check is disabled; run %s lost %d shards.",
          manifest.run_id,
          count,
      )
    return
  if count > max_failed:
    raise ValueError(
        f"run {manifest.run_id} lost {count} input shards during reduce, "
        f"which exceeds --max_failed_shards={max_failed}. Its aggregates are "
        "incomplete. Re-run reduce, or pass a larger --max_failed_shards to "
        "publish it anyway."
    )
  if count:
    logging.warning(
        "Run %s lost %d shards, within --max_failed_shards=%d.",
        manifest.run_id,
        count,
        max_failed,
    )


@dataclasses.dataclass(frozen=True)
class _PreparedDerivationInputs:
  """Validated paths, metadata, and export keys for a derivation run."""

  # Validated id of the new published run, e.g.
  # "2026-09-30T08-38-01Z-g1a2b3c4".
  publish_run_id: str
  # New, not-yet-existing published run directory to write artifacts into.
  dataset_dir: str
  # Intermediate `<intermediate_prefix>/<year>/<dataset>` root holding runs.
  run_root: str
  # Per-country baseline stats (iso_a3, name, population, ...) keyed by ISO2.
  baseline_stats: dict[str, dict[str, object]]
  # Loaded and indexed country and FIR boundary geometries.
  boundaries: metadata_loader.Boundaries
  # Grid resolution and encoding recorded in the intermediate run manifest.
  grid_config: grid_rasterizer.GridConfig
  # Every (grid_type, bucket_type, bucket_index) grid to publish, e.g.
  # ("impact", "monthly", 3).
  keys: list[tuple[str, str, int]]
  # Artifact filename prefix per key, e.g. "monthly_global_impact_grid_03".
  prefixes: list[str]


def _resolve_publish_run_id(config: DeriveConfig) -> str:
  """Returns the validated id of the run to publish into.

  Args:
    config: The derivation run configuration.

  Returns:
    `config.publish_run_id` if set, otherwise a freshly generated run id.

  Raises:
    ValueError: If the resulting run id is malformed.
  """
  # A fresh id by default. Republishing into an existing run, including the
  # one we are reading from or the one `latest.json` names, would mutate an
  # immutable directory, which is the one thing this layout exists to
  # prevent.
  return artifact_io.validate_run_id(
      config.publish_run_id.strip()
      or intermediate_io.make_run_id(fs.get_version_tag()),
      label="--publish_run_id",
  )


def _read_publishable_manifest(
    config: DeriveConfig, run_root: str
) -> intermediate_io.RunManifest:
  """Reads the intermediate run manifest and checks it may be republished.

  Args:
    config: The derivation run configuration.
    run_root: Intermediate dataset root holding the run named by
      `config.run_id`.

  Returns:
    The manifest of the intermediate run.

  Raises:
    ValueError: If the run has no manifest or lost more shards than
      `config.max_failed_shards` allows.
  """
  if not intermediate_io.manifest_exists(run_root, config.run_id):
    raise ValueError(
        f"run {config.run_id} has no manifest under {run_root}. The manifest"
        " is written only once reduce has passed its failed-shard check and"
        " stored both aggregate files, so this run is incomplete or failed"
        " and cannot be republished."
    )
  manifest = intermediate_io.read_manifest(run_root, config.run_id)
  check_failed_shard_budget(manifest, config.max_failed_shards)
  logging.info(
      "Deriving from run %s (%d input files).",
      manifest.run_id,
      manifest.input_file_count,
  )
  return manifest


def _check_grid_slices_present(
    *, run_root: str, run_id: str, prefixes: Sequence[str]
) -> None:
  """Raises unless every expected grid slice exists in the intermediate run.

  Args:
    run_root: Intermediate dataset root holding the run.
    run_id: Id of the intermediate run to check.
    prefixes: Artifact filename prefix of every grid slice to expect.

  Raises:
    ValueError: If any grid slice is missing.
  """
  # Check the whole slice set before publishing anything: a run missing some
  # grids would look complete to the server and is not.
  missing = [
      prefix
      for prefix in prefixes
      if not intermediate_io.grid_slice_exists(run_root, run_id, prefix)
  ]
  if missing:
    raise ValueError(
        f"run {run_id} is missing {len(missing)} of {len(prefixes)} grid "
        f"slices, first few: {missing[:5]}. Re-run reduce with "
        "--intermediate_prefix set."
    )


def _validate_grid_slices(
    manifest: intermediate_io.RunManifest, run_root: str, run_id: str
) -> tuple[grid_rasterizer.GridConfig, list[tuple[str, str, int]], list[str]]:
  """Validates that all expected grid slice artifacts exist in storage."""
  grid_config = _grid_config_from_manifest(manifest)
  keys = grid_publish.all_export_keys()
  prefixes = [grid_publish.artifact_prefix(*key) for key in keys]
  _check_grid_slices_present(
      run_root=run_root, run_id=run_id, prefixes=prefixes
  )
  return grid_config, keys, prefixes


def _prepare_derivation_inputs(
    config: DeriveConfig,
) -> _PreparedDerivationInputs:
  """Validates intermediate run manifest, slices, and reference metadata."""
  publish_run_id = _resolve_publish_run_id(config)
  dataset_dir = artifact_io.require_new_run_dir(
      config.output_prefix,
      year=config.year,
      dataset=config.dataset,
      run_id=publish_run_id,
  )

  run_root = intermediate_io.dataset_root(
      config.intermediate_prefix, config.year, config.dataset
  )
  manifest = _read_publishable_manifest(config, run_root)
  logging.info("Publishing into run %s", publish_run_id)

  # Cheap input checks first: a missing or malformed baseline should fail in
  # seconds, not after the geometry below has been loaded and indexed.
  baseline_stats = metadata_loader.load_country_baseline_stats(
      population_path=config.population_path,
      ghg_emissions_path=config.ghg_emissions_path,
      country_stats_path=config.country_stats_path,
      input_year=config.year,
  )

  # The server serves the map boundaries from whichever run `latest.json`
  # names, so every published run carries its own copy.
  boundaries = metadata_loader.load_boundaries(
      firs_path=config.firs_path,
      country_geojson_path=config.country_geojson_path,
      expected_fir_count=config.expected_fir_count,
  )
  grid_config, keys, prefixes = _validate_grid_slices(
      manifest, run_root, config.run_id
  )

  return _PreparedDerivationInputs(
      publish_run_id=publish_run_id,
      dataset_dir=dataset_dir,
      run_root=run_root,
      baseline_stats=baseline_stats,
      boundaries=boundaries,
      grid_config=grid_config,
      keys=keys,
      prefixes=prefixes,
  )


def _publish_derived_grids(
    *,
    run_root: str,
    run_id: str,
    keys: Sequence[tuple[str, str, int]],
    prefixes: Sequence[str],
    grid_config: grid_rasterizer.GridConfig,
    dataset_dir: str,
) -> None:
  """Reads intermediate grid slices and publishes encoded grids and metadata."""
  grid_results: list[grid_publish.GridPublishResult] = []
  for grid_number, (key, prefix) in enumerate(
      zip(keys, prefixes, strict=True), start=1
  ):
    grid_arr = intermediate_io.read_grid_slice(run_root, run_id, prefix)
    grid_results.append(
        grid_publish.publish_grid_artifacts(
            key, grid_arr, grid_config=grid_config, output_dir=dataset_dir
        )
    )
    logging.info("Published grid %d/%d: %s", grid_number, len(keys), prefix)
  value_max = grid_publish.reduce_value_max(grid_results)
  grid_publish.write_shared_grid_meta(grid_config, dataset_dir, value_max)
  logging.info("Wrote grid metadata with maxima %s", value_max)


def _publish_summary_artifacts(
    config: DeriveConfig, inputs: _PreparedDerivationInputs
) -> None:
  """Reads the intermediate aggregates and publishes the summary artifacts.

  Args:
    config: The derivation run configuration.
    inputs: Validated inputs from `_prepare_derivation_inputs`.
  """
  country_items = intermediate_io.read_country_agg(
      inputs.run_root, config.run_id
  )
  fir_items = intermediate_io.read_fir_agg(inputs.run_root, config.run_id)
  logging.info(
      "Read %d country rows and %d FIR rows from the intermediate.",
      len(country_items),
      len(fir_items),
  )
  written = derive.derive_and_publish(
      country_items,
      fir_items,
      country_areas=inputs.boundaries.country_areas,
      fir_areas=inputs.boundaries.fir_areas,
      baseline_stats=inputs.baseline_stats,
      year=config.year,
      dataset_dir=inputs.dataset_dir,
      country_names=inputs.boundaries.country_names,
  )
  logging.info("Published %d summary artifacts.", len(written))


def run_derivation(config: DeriveConfig | None = None) -> None:
  """Reads one intermediate run and republishes its artifacts."""
  if config is None:
    config = DeriveConfig.from_flags()

  inputs = _prepare_derivation_inputs(config)

  artifact_io.write_boundary_geojson(
      inputs.dataset_dir,
      inputs.boundaries.country_features,
      inputs.boundaries.fir_features,
  )

  _publish_summary_artifacts(config, inputs)

  _publish_derived_grids(
      run_root=inputs.run_root,
      run_id=config.run_id,
      keys=inputs.keys,
      prefixes=inputs.prefixes,
      grid_config=inputs.grid_config,
      dataset_dir=inputs.dataset_dir,
  )

  # Last, and only once everything above has returned. An exception anywhere
  # earlier leaves the new run orphaned and the pointer still naming the old
  # one, which is the failure mode we want: readers keep serving.
  path = artifact_io.write_run_pointer(
      config.output_prefix,
      year=config.year,
      dataset=config.dataset,
      run_id=inputs.publish_run_id,
      published_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
  )
  logging.info("Flipped %s to run %s", path, inputs.publish_run_id)


def main(argv: Sequence[str]) -> None:
  """Runs the standalone Stage-2 derivation entry point."""
  if len(argv) > 1:
    raise app.UsageError("Too many command-line arguments.")
  run_derivation()


if __name__ == "__main__":
  app.run(main)
