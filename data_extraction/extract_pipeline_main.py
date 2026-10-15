"""Apache Beam pipeline runner for regional contrail data extraction."""

from collections.abc import Callable, Sequence
import dataclasses
import datetime
import os
from typing import Any

from absl import app
from absl import flags
from absl import logging
import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions

from common import artifact_io
from common import fs
from data_extraction import beam_stages
from data_extraction import common_flags
from data_extraction import grid_publish
from data_extraction import grid_rasterizer
from data_extraction import intermediate_io
from data_extraction import metadata_loader

# Shared with the derive binary; see common_flags for input_year, dataset,
# output_prefix, intermediate_prefix, run_id, publish_run_id,
# max_failed_shards, expected_fir_count, and the geometry/baseline paths.

# Reduce-specific: these describe the parquet scan and the rasterisation, and
# have no meaning to a derive pass.
_SEGMENTS_PATH = flags.DEFINE_string(
    "segments_path",
    "",
    "Glob pattern, directory, or single file of flight segment Parquet files."
    " The energy forcing column must be non-effective (no ERF/RF applied);"
    " the pipeline applies ERF/RF = 0.42 in the CO2e conversion.",
)
_AIRPORTS_PATH = flags.DEFINE_string(
    "airports_path",
    "",
    "Path to ourairports CSV.",
)

# Configurable Grid Geometry and Resolution
_GRID_WIDTH = flags.DEFINE_integer(
    "grid_width", None, "Output grid width in pixels."
)
_GRID_HEIGHT = flags.DEFINE_integer(
    "grid_height", None, "Output grid height in pixels."
)
_MIN_LON = flags.DEFINE_float("min_lon", None, "Grid minimum longitude.")
_MAX_LON = flags.DEFINE_float("max_lon", None, "Grid maximum longitude.")
_MIN_LAT = flags.DEFINE_float("min_lat", None, "Grid minimum latitude.")
_MAX_LAT = flags.DEFINE_float("max_lat", None, "Grid maximum latitude.")
_STEP_SIZE_METERS = flags.DEFINE_float(
    "step_size_meters", None, "Segment interpolation step in meters."
)
_MAX_ROWS_PER_CHUNK = flags.DEFINE_integer(
    "max_rows_per_chunk",
    1000000,
    "Maximum number of flight segments per sub-shard chunk (0 to disable"
    " splitting).",
)


def resolve_parquet_files(path_or_pattern: str) -> list[str]:
  """Resolves file paths from directory, file, or glob pattern.

  Filesystem errors propagate, so an unreadable input location is not reported
  as one that matches no files.

  Args:
    path_or_pattern: Directory, single file, or glob pattern.

  Returns:
    The matching file paths; empty if `path_or_pattern` is blank or matches
    nothing.
  """
  cleaned = path_or_pattern.strip()
  if not cleaned:
    return []

  if any(character in cleaned for character in ("*", "?", "[")):
    return fs.glob(cleaned)

  if fs.exists(cleaned):
    if fs.isdir(cleaned):
      return [
          os.path.join(cleaned, filename)
          for filename in fs.listdir(cleaned)
          if filename.endswith(".pq") or filename.endswith(".parquet")
      ]
    return [cleaned]

  return fs.glob(cleaned)


_GRID_FLAGS = (
    ("grid_width", _GRID_WIDTH),
    ("grid_height", _GRID_HEIGHT),
    ("min_lon", _MIN_LON),
    ("max_lon", _MAX_LON),
    ("min_lat", _MIN_LAT),
    ("max_lat", _MAX_LAT),
    ("step_size_meters", _STEP_SIZE_METERS),
)


def _grid_config_from_flags() -> grid_rasterizer.GridConfig:
  """Builds the output grid geometry from the required grid flags.

  There are no defaults: resolution and extent determine every published
  grid, so a run must state them.

  Returns:
    A validated `GridConfig` built from the CLI flag values.

  Raises:
    ValueError: If any grid flag is unset, or if grid geometry is invalid.
  """
  values: dict[str, Any] = {}
  missing: list[str] = []
  for name, holder in _GRID_FLAGS:
    if holder.value is None:
      missing.append(name)
    else:
      values[name] = holder.value
  if missing:
    raise ValueError(
        "Grid geometry flags are required: "
        + ", ".join(f"--{name}" for name in missing)
    )
  return grid_rasterizer.GridConfig(
      min_lon=values["min_lon"],
      max_lon=values["max_lon"],
      min_lat=values["min_lat"],
      max_lat=values["max_lat"],
      width=values["grid_width"],
      height=values["grid_height"],
      step_size_meters=values["step_size_meters"],
  )


@dataclasses.dataclass(frozen=True)
class ExtractConfig:
  """Configuration for a contrail extraction pipeline run."""

  dataset: str
  year: int
  output_prefix: str
  segments_path: str
  airports_path: str
  country_geojson_path: str
  firs_path: str
  grid_config: grid_rasterizer.GridConfig
  intermediate_prefix: str = ""
  run_id: str = ""
  publish_run_id: str = ""
  max_failed_shards: int = 0
  population_path: str = ""
  ghg_emissions_path: str = ""
  country_stats_path: str = ""
  expected_fir_count: int = 0
  max_rows_per_chunk: int = 1_000_000

  def __post_init__(self) -> None:
    """Validates required input paths, run identifiers, and chunk parameters."""
    for field in dataclasses.fields(self):
      value = getattr(self, field.name)
      if isinstance(value, str):
        object.__setattr__(self, field.name, value.strip())
    common_flags.validate_dataset(self.dataset)
    common_flags.validate_input_year(self.year)
    if not self.output_prefix:
      raise ValueError("--output_prefix is required.")
    common_flags.require_distinct_prefixes(
        intermediate_root=self.intermediate_prefix,
        output_root=self.output_prefix,
    )
    for flag_name, value in (
        ("segments_path", self.segments_path),
        ("airports_path", self.airports_path),
        ("country_geojson_path", self.country_geojson_path),
        ("firs_path", self.firs_path),
    ):
      if not value:
        raise ValueError(f"--{flag_name} is required.")
    common_flags.validate_baseline_source(
        population_path=self.population_path,
        ghg_emissions_path=self.ghg_emissions_path,
        country_stats_path=self.country_stats_path,
    )
    grid_publish.lod_levels(self.grid_config)
    if self.run_id:
      artifact_io.validate_run_id(self.run_id, label="--run_id")
    if self.publish_run_id:
      artifact_io.validate_run_id(self.publish_run_id, label="--publish_run_id")
    if self.max_rows_per_chunk < 0:
      raise ValueError("max_rows_per_chunk must be non-negative.")

  @classmethod
  def from_flags(cls) -> "ExtractConfig":
    """Constructs ExtractConfig from absl flags."""
    return cls(
        dataset=common_flags.require_dataset(),
        year=common_flags.require_input_year(),
        output_prefix=common_flags.OUTPUT_PREFIX.value or "",
        segments_path=_SEGMENTS_PATH.value or "",
        airports_path=_AIRPORTS_PATH.value or "",
        country_geojson_path=common_flags.COUNTRY_GEOJSON_PATH.value or "",
        firs_path=common_flags.FIRS_PATH.value or "",
        grid_config=_grid_config_from_flags(),
        intermediate_prefix=common_flags.INTERMEDIATE_PREFIX.value or "",
        run_id=common_flags.RUN_ID.value or "",
        publish_run_id=common_flags.PUBLISH_RUN_ID.value or "",
        max_failed_shards=common_flags.MAX_FAILED_SHARDS.value,
        population_path=common_flags.POPULATION_PATH.value or "",
        ghg_emissions_path=common_flags.GHG_EMISSIONS_PATH.value or "",
        country_stats_path=common_flags.COUNTRY_STATS_PATH.value or "",
        expected_fir_count=common_flags.EXPECTED_FIR_COUNT.value,
        max_rows_per_chunk=_MAX_ROWS_PER_CHUNK.value,
    )


@dataclasses.dataclass(frozen=True)
class _PipelineRunContext:
  """Resolved driver-side state and reference data for one pipeline run."""

  # Validated pipeline configuration the run was started with.
  config: ExtractConfig
  # Input parquet segment file paths matched by `config.segments_path`.
  parquet_files: list[str]
  # Intermediate run id, e.g. "2026-09-30T08-38-01Z-g1a2b3c4".
  run_id: str
  # Published run directory name; defaults to `run_id`.
  publish_run_id: str
  # Stripped `--intermediate_prefix`; empty disables intermediate output.
  intermediate_root: str
  # `<intermediate_root>/<year>/<dataset>`, or empty if intermediates are off.
  intermediate_run_root: str
  # New, not-yet-existing published run directory for this run's artifacts.
  dataset_dir: str
  # Driver start time as a UTC ISO 8601 string, e.g.
  # "2026-09-30T08:38:01.123456+00:00".
  started_at: str
  # Loaded and indexed country and FIR boundary geometries.
  boundaries: metadata_loader.Boundaries
  # Per-country baseline stats (population, ghg_mtco2e, ...) keyed by ISO2.
  baseline_stats: dict[str, dict[str, Any]]


def _resolve_input_parquet_files(segments_path: str) -> list[str]:
  """Lists the segment parquet files and rejects an empty match.

  Args:
    segments_path: Glob pattern, directory, or single parquet file path.

  Returns:
    The matched parquet file paths.

  Raises:
    ValueError: If `segments_path` matches no parquet files.
  """
  # Listing the segment files is cheap next to the reference-data load below,
  # so an empty match is a configuration error caught here too.
  parquet_files = resolve_parquet_files(segments_path)
  logging.info("Discovered %d parquet files to process.", len(parquet_files))
  if not parquet_files:
    # Publishing from zero inputs would replace the live dataset with zeros.
    raise ValueError(
        f"--segments_path={segments_path!r} matched no parquet files."
    )
  return parquet_files


def _resolve_run_ids(config: ExtractConfig) -> tuple[str, str]:
  """Resolves the intermediate run id and the published run id.

  Args:
    config: Validated pipeline configuration.

  Returns:
    The intermediate run id and the published run id, in that order.

  Raises:
    ValueError: If either resolved run id is invalid.
  """
  pipeline_version = fs.get_version_tag()
  run_id = artifact_io.validate_run_id(
      config.run_id.strip() or intermediate_io.make_run_id(pipeline_version),
      label="--run_id",
  )
  # Defaults to the same id, which is the point: the published directory name
  # is then the lookup key for the aggregates it was derived from.
  publish_run_id = artifact_io.validate_run_id(
      config.publish_run_id.strip() or run_id, label="--publish_run_id"
  )
  return run_id, publish_run_id


def _load_and_publish_reference_data(
    config: ExtractConfig, dataset_dir: str
) -> tuple[metadata_loader.Boundaries, dict[str, dict[str, Any]]]:
  """Loads boundaries and baseline stats, and publishes the boundaries.

  Args:
    config: Validated pipeline configuration.
    dataset_dir: New published run directory for this run's artifacts.

  Returns:
    The loaded boundaries and the per-country baseline stats, in that order.

  Raises:
    ValueError: If FIR feature validation fails.
    RuntimeError: If boundary geometry indices or baseline reference files are
      missing or empty.
  """
  # Boundary inputs are loaded and checked first: they are small, and a bad
  # FIR or country file would otherwise only surface after the full Beam run
  # as silently dropped or misattributed rows. Areas come from geometry alone,
  # so they are resolved here and joined during finalization rather than
  # carried through the per-segment reduce.
  boundaries = metadata_loader.load_boundaries(
      firs_path=config.firs_path,
      country_geojson_path=config.country_geojson_path,
      expected_fir_count=config.expected_fir_count,
  )
  baseline_stats = metadata_loader.load_country_baseline_stats(
      population_path=config.population_path,
      ghg_emissions_path=config.ghg_emissions_path,
      country_stats_path=config.country_stats_path,
      input_year=config.year,
  )
  # Publish the boundaries this run attributes against. Written from the
  # driver before the pipeline starts: the run directory stays invisible
  # until the pointer flips, so this needs no publish signal of its own.
  artifact_io.write_boundary_geojson(
      dataset_dir, boundaries.country_features, boundaries.fir_features
  )
  return boundaries, baseline_stats


def _prepare_pipeline_run(config: ExtractConfig) -> _PipelineRunContext:
  """Resolves input files, run directories, and reference data on the driver.

  Args:
    config: Validated pipeline configuration.

  Returns:
    Resolved driver-side context for constructing the Beam DAG.

  Raises:
    ValueError: If `config.segments_path` matches no parquet files, run IDs are
      invalid, the target output run directory already exists, or FIR feature
      validation fails.
    RuntimeError: If boundary geometry indices or baseline reference files are
      missing or empty.
  """
  parquet_files = _resolve_input_parquet_files(config.segments_path)

  # Resolved once here, not inside the DoFn: a timestamp evaluated per worker
  # would scatter one run across several directories.
  intermediate_root = config.intermediate_prefix.strip()
  run_id, publish_run_id = _resolve_run_ids(config)
  # One run directory shared by the grid slices and the aggregates, so a
  # derive pass reads a single consistent snapshot.
  intermediate_run_root = ""
  if intermediate_root:
    intermediate_run_root = intermediate_io.dataset_root(
        intermediate_root, config.year, config.dataset
    )
  # Artifacts land in an immutable per-run directory; `latest.json` is
  # flipped at the very end, once both the grid branch and the summary
  # branch have finished. Readers therefore never observe a partial set.
  # Checked here, on the driver and before any input is read: a reused id
  # would overwrite that run in place, and a check inside a retried pipeline
  # step would fail on its own partial output.
  dataset_dir = artifact_io.require_new_run_dir(
      config.output_prefix,
      year=config.year,
      dataset=config.dataset,
      run_id=publish_run_id,
  )
  started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

  logging.info(
      "Starting Contrail Extraction Pipeline for Year %s Dataset %s",
      config.year,
      config.dataset,
  )
  logging.info("Segment pattern: %s", config.segments_path)
  logging.info("Target Output Directory: %s", config.output_prefix)

  boundaries, baseline_stats = _load_and_publish_reference_data(
      config, dataset_dir
  )

  return _PipelineRunContext(
      config=config,
      parquet_files=parquet_files,
      run_id=run_id,
      publish_run_id=publish_run_id,
      intermediate_root=intermediate_root,
      intermediate_run_root=intermediate_run_root,
      dataset_dir=dataset_dir,
      started_at=started_at,
      boundaries=boundaries,
      baseline_stats=baseline_stats,
  )


def _resolve_beam_runner_and_options(
    *,
    pipeline_args: Sequence[str] | None,
    custom_options: PipelineOptions | None,
    runner_factory: Callable[[], Any] | None,
) -> tuple[Any, PipelineOptions]:
  """Resolves the Beam runner instance and PipelineOptions for execution."""
  if custom_options is not None:
    return None, custom_options
  options = PipelineOptions(pipeline_args)
  runner_obj = runner_factory() if runner_factory is not None else None
  return runner_obj, options


def _ingest_and_process_shards(
    pipeline: beam.Pipeline,
    run_context: _PipelineRunContext,
) -> Any:
  """Creates file shards, splits chunks, and runs per-shard extraction."""
  config = run_context.config
  raw_files = (
      pipeline
      | "CreateFileShards" >> beam.Create(run_context.parquet_files)
      | "ReshuffleFiles" >> beam.Reshuffle()
  )
  shards_split = (
      raw_files
      | "SplitParquetShards"
      >> beam.ParDo(
          beam_stages.SplitParquetShardDoFn(
              max_rows_per_chunk=config.max_rows_per_chunk
          )
      )
      | "ReshuffleChunks" >> beam.Reshuffle()
  )
  return shards_split | "ProcessParquetShards" >> beam.ParDo(
      beam_stages.ProcessParquetShardDoFn(
          airports_path=config.airports_path,
          firs_path=config.firs_path,
          country_geojson_path=config.country_geojson_path,
          grid_config=config.grid_config,
          input_year=config.year,
      )
  ).with_outputs(
      beam_stages.ProcessParquetShardDoFn.OUTPUT_TAG_COUNTRY,
      beam_stages.ProcessParquetShardDoFn.OUTPUT_TAG_FIR,
      beam_stages.ProcessParquetShardDoFn.OUTPUT_TAG_GRID,
      beam_stages.ProcessParquetShardDoFn.OUTPUT_TAG_FAILED,
  )


def _gate_failed_shards(shards_processed: Any, *, max_failed: int) -> Any:
  """Logs failed shards and reduces them to a single threshold verdict.

  Counted globally so the verdict is a single element, which the summary
  export (and with it the intermediate manifest) and the pointer flip both
  wait on as a side input.

  Args:
    shards_processed: Tagged outputs of the per-shard extraction step.
    max_failed: Maximum number of failed shards tolerated by the run.

  Returns:
    A single-element PCollection holding the failed-shard count.
  """
  return (
      shards_processed[beam_stages.ProcessParquetShardDoFn.OUTPUT_TAG_FAILED]
      | "LogFailedShards" >> beam.Map(beam_stages.log_failed_shard)
      | "CountFailedShards" >> beam.combiners.Count.Globally()
      | "EnforceFailedShardThreshold"
      >> beam.Map(beam_stages.check_failed_shard_count, max_failed=max_failed)
  )


def _group_region_impacts(shards_processed: Any) -> tuple[Any, Any]:
  """Sums the per-shard country and FIR impact tuples per region key.

  Args:
    shards_processed: Tagged outputs of the per-shard extraction step.

  Returns:
    The grouped country impacts and the grouped FIR impacts, in that order.
  """
  # 1. Country Metrics Branch
  country_grouped = shards_processed[
      beam_stages.ProcessParquetShardDoFn.OUTPUT_TAG_COUNTRY
  ] | "GroupCountryImpacts" >> beam.CombinePerKey(
      beam_stages.SumTupleCombineFn()
  )
  # 2. FIR Metrics Branch
  fir_grouped = shards_processed[
      beam_stages.ProcessParquetShardDoFn.OUTPUT_TAG_FIR
  ] | "GroupFIRImpacts" >> beam.CombinePerKey(beam_stages.SumTupleCombineFn())
  return country_grouped, fir_grouped


def _export_grid_slices(
    shards_processed: Any, run_context: _PipelineRunContext
) -> Any:
  """Combines grid slices into the export keys and writes them in parallel.

  Args:
    shards_processed: Tagged outputs of the per-shard extraction step.
    run_context: Resolved driver-side state for this run.

  Returns:
    The PCollection of per-grid write results.
  """
  config = run_context.config
  # 3. Spatial Grids Branch (two-stage combine into the 74 export keys +
  # parallel write; see `beam_stages.combine_grid_slices_to_export_keys`).
  return beam_stages.combine_grid_slices_to_export_keys(
      shards_processed[beam_stages.ProcessParquetShardDoFn.OUTPUT_TAG_GRID],
      grid_config=config.grid_config,
      expected_year=config.year,
  ) | "ExportGridSlicesParallel" >> beam.ParDo(
      beam_stages.WriteGridArtifactsDoFn(
          config.grid_config,
          output_dir=run_context.dataset_dir,
          intermediate_root=run_context.intermediate_run_root,
          run_id=run_context.run_id,
      )
  )


def _build_export_artifacts_dofn(
    run_context: _PipelineRunContext,
) -> beam_stages.ExportArtifactsDoFn:
  """Builds the DoFn that finalizes and writes the summary artifacts."""
  config = run_context.config
  return beam_stages.ExportArtifactsDoFn(
      year=config.year,
      dataset=config.dataset,
      intermediate_root=run_context.intermediate_root,
      intermediate_run_root=run_context.intermediate_run_root,
      run_id=run_context.run_id,
      segments_pattern=config.segments_path,
      input_file_count=len(run_context.parquet_files),
      grid_config=config.grid_config,
      started_at=run_context.started_at,
      country_areas=run_context.boundaries.country_areas,
      fir_areas=run_context.boundaries.fir_areas,
      baseline_stats=run_context.baseline_stats,
      dataset_dir=run_context.dataset_dir,
      country_names=run_context.boundaries.country_names,
  )


def _export_summary_artifacts(
    pipeline: beam.Pipeline,
    run_context: _PipelineRunContext,
    *,
    country_grouped: Any,
    fir_grouped: Any,
    failed_shard_verdict: Any,
) -> Any:
  """Finalizes and exports the country and FIR summary artifacts.

  Args:
    pipeline: The Beam pipeline being constructed.
    run_context: Resolved driver-side state for this run.
    country_grouped: Grouped per-country impact tuples.
    fir_grouped: Grouped per-FIR impact tuples.
    failed_shard_verdict: Single-element failed-shard count PCollection.

  Returns:
    The PCollection signalling that the summary export has finished.
  """
  # Sink: Finalize and Export Country & FIR Summary Artifacts
  export_dofn = _build_export_artifacts_dofn(run_context)
  # The verdict is a side input here too, not only on the pointer flip: the
  # intermediate manifest written by this step is what lets derive_main
  # republish the run, so it must not exist for a run that failed the gate.
  return (
      pipeline
      | "TriggerExport" >> beam.Create([1])
      | "ExportAllArtifacts"
      >> beam.ParDo(
          export_dofn,
          country_items=beam.pvalue.AsList(country_grouped),
          fir_items=beam.pvalue.AsList(fir_grouped),
          failed_shard_count=beam.pvalue.AsSingleton(failed_shard_verdict),
      )
  )


def _publish_run_pointer(
    run_context: _PipelineRunContext,
    *,
    grid_exported: Any,
    summary_done: Any,
    failed_shard_verdict: Any,
) -> None:
  """Flips `latest.json` once both the grid and summary branches finish.

  Args:
    run_context: Resolved driver-side state for this run.
    grid_exported: Per-grid write results from the grid branch.
    summary_done: Completion signal from the summary export branch.
    failed_shard_verdict: Single-element failed-shard count PCollection.
  """
  config = run_context.config
  # The pointer flip must observe *both* publish branches. Joining them
  # through Flatten -> ToList forces Beam to schedule the flip after the last
  # grid slice and the last summary artifact have been written; anything less
  # would let a reader follow the pointer into a half-populated run. The grid
  # branch is represented by the grid_meta.json write, which itself waits on
  # (and verifies) every grid, because its colour-scale maxima need them all.
  grid_meta_done = (
      grid_exported
      | "AwaitAllGrids" >> beam.combiners.ToList()
      | "WriteSharedGridMeta"
      >> beam.Map(
          beam_stages.WriteSharedGridMetaFn(
              config.grid_config, run_context.dataset_dir
          )
      )
  )
  publish_signals = (
      (grid_meta_done, summary_done)
      | "FlattenPublishSignals" >> beam.Flatten()
      | "AwaitAllArtifacts" >> beam.combiners.ToList()
  )
  flip_fn = beam_stages.FlipRunPointerFn(
      output_root=config.output_prefix,
      year=config.year,
      dataset=config.dataset,
      publish_run_id=run_context.publish_run_id,
  )
  _ = publish_signals | "PublishRunPointer" >> beam.Map(
      flip_fn, beam.pvalue.AsSingleton(failed_shard_verdict)
  )


def _wire_export_and_publish_branches(
    pipeline: beam.Pipeline,
    shards_processed: Any,
    run_context: _PipelineRunContext,
) -> None:
  """Wires aggregation, artifact export, and atomic run-pointer flip stages."""
  # 0. Failed-shard gate.
  failed_shard_verdict = _gate_failed_shards(
      shards_processed, max_failed=run_context.config.max_failed_shards
  )
  country_grouped, fir_grouped = _group_region_impacts(shards_processed)
  grid_exported = _export_grid_slices(shards_processed, run_context)
  summary_done = _export_summary_artifacts(
      pipeline,
      run_context,
      country_grouped=country_grouped,
      fir_grouped=fir_grouped,
      failed_shard_verdict=failed_shard_verdict,
  )
  _publish_run_pointer(
      run_context,
      grid_exported=grid_exported,
      summary_done=summary_done,
      failed_shard_verdict=failed_shard_verdict,
  )


def run_pipeline(
    config: ExtractConfig | None = None,
    *,
    pipeline_args: Sequence[str] | None = None,
    custom_options: PipelineOptions | None = None,
    runner_factory: Callable[[], Any] | None = None,
) -> None:
  """Builds and executes the Beam extraction pipeline.

  Args:
    config: Optional explicit pipeline configuration. If omitted, built from
      command-line flags.
    pipeline_args: Optional command line arguments passed to PipelineOptions.
    custom_options: Optional explicit PipelineOptions instance.
    runner_factory: Optional factory callable returning a Beam runner instance.

  Raises:
    ValueError: If required configuration flags or segment inputs are invalid.
    RuntimeError: If boundary geometry indices are empty.
  """
  if config is None:
    config = ExtractConfig.from_flags()
  run_context = _prepare_pipeline_run(config)
  runner_obj, options = _resolve_beam_runner_and_options(
      pipeline_args=pipeline_args,
      custom_options=custom_options,
      runner_factory=runner_factory,
  )
  with beam.Pipeline(runner=runner_obj, options=options) as pipeline:
    shards_processed = _ingest_and_process_shards(pipeline, run_context)
    _wire_export_and_publish_branches(pipeline, shards_processed, run_context)


def main(argv: Sequence[str]) -> None:
  """Runs the Beam extraction pipeline entry point."""
  pipeline_args = list(argv[1:]) if len(argv) > 1 else []
  run_pipeline(pipeline_args=pipeline_args)


if __name__ == "__main__":
  # Required flags are validated in run_pipeline() rather than with
  # flags.mark_flags_as_required(): some runners start worker processes from
  # this same entry point without forwarding these flags, and a parse-time
  # validator would make those workers fail at startup.
  app.run(main)
