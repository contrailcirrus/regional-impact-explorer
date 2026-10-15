"""Tests for derive_main: CLI validation, budgets, and end-to-end parity."""

from collections.abc import Mapping, Sequence
import dataclasses
import gzip
import json
import os
from typing import Any
from unittest import mock

from absl import flags
from absl.testing import absltest
from absl.testing import flagsaver
from absl.testing import parameterized
from apache_beam.options.pipeline_options import PipelineOptions
import numpy as np

from common import artifact_io
from common import constants
from data_extraction import derive
from data_extraction import derive_main
from data_extraction import extract_pipeline_main
from data_extraction import grid_publish
from data_extraction import grid_rasterizer
from data_extraction import intermediate_io
from data_extraction import test_fixtures

FLAGS = flags.FLAGS


def setUpModule() -> None:
  FLAGS.mark_as_parsed()


def _assert_summary_row_fields_equal(
    test_case: absltest.TestCase,
    actual_row: Mapping[str, Any],
    expected_row: Mapping[str, Any],
    sort_key_desc: str,
) -> None:
  """Asserts that all fields in two matching summary rows are equal."""
  test_case.assertEqual(set(actual_row.keys()), set(expected_row.keys()))
  for k in actual_row:
    actual_val = actual_row[k]
    expected_val = expected_row[k]
    if actual_val is None or expected_val is None:
      test_case.assertIs(
          actual_val,
          expected_val,
          f"Mismatch at field {k}: {actual_val} vs {expected_val}",
      )
    elif isinstance(actual_val, (float, np.floating)) or isinstance(
        expected_val, (float, np.floating)
    ):
      np.testing.assert_allclose(
          float(actual_val),
          float(expected_val),
          rtol=1e-5,
          atol=1e-8,
          err_msg=f"Float mismatch at field {k} in row {sort_key_desc}",
      )
    else:
      test_case.assertEqual(
          actual_val,
          expected_val,
          f"Value mismatch at field {k} in row {sort_key_desc}",
      )


def _assert_summary_rows_equal(
    test_case: absltest.TestCase,
    actual_rows: Sequence[Mapping[str, Any]],
    expected_rows: Sequence[Mapping[str, Any]],
    key_fields: Sequence[str],
) -> None:
  """Asserts that two collections of summary row dicts match closely."""
  test_case.assertLen(actual_rows, len(expected_rows))
  sort_key = lambda item_row: tuple(
      str(item_row.get(k, "")) for k in key_fields
  )
  actual_sorted = sorted(actual_rows, key=sort_key)
  expected_sorted = sorted(expected_rows, key=sort_key)
  for actual_row, expected_row in zip(
      actual_sorted, expected_sorted, strict=True
  ):
    _assert_summary_row_fields_equal(
        test_case, actual_row, expected_row, str(sort_key(actual_row))
    )


def _write_empty_geometry_geojson(test_dir: str) -> str:
  path = os.path.join(test_dir, "empty_geom.geojson")
  with open(path, "w", encoding="utf-8") as f:
    json.dump(
        {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"name": "NoPolygon", "iso_a2": "NP"},
                "geometry": None,
            }],
        },
        f,
    )
  return path


def _populate_empty_run_intermediate(
    intermediate_dir: str, run_id: str
) -> None:
  run_root = intermediate_io.dataset_root(
      intermediate_dir, 2025, "example_dataset"
  )
  intermediate_io.write_manifest(
      intermediate_io.RunManifest(
          run_id=run_id,
          failed_shard_count=0,
          grid_config=grid_rasterizer.GridConfig(
              min_lon=-180.0,
              max_lon=180.0,
              min_lat=-85.0,
              max_lat=85.0,
              width=40,
              height=20,
              step_size_meters=10000.0,
          ),
      ),
      run_root,
  )
  for key in grid_publish.all_export_keys():
    prefix = grid_publish.artifact_prefix(*key)
    intermediate_io.write_grid_slice(
        np.zeros((20, 40), dtype=np.float32),
        root=run_root,
        run_id=run_id,
        prefix=prefix,
    )


def _assert_geojson_files_match(
    test_case: absltest.TestCase,
    orig_dir: str,
    rederived_dir: str,
    filename: str,
) -> None:
  with (
      open(os.path.join(orig_dir, filename), "r", encoding="utf-8") as f_orig,
      open(
          os.path.join(rederived_dir, filename), "r", encoding="utf-8"
      ) as f_rederived,
  ):
    test_case.assertEqual(json.load(f_orig), json.load(f_rederived))


def _assert_all_grid_slices_match(
    orig_dir: str,
    rederived_dir: str,
) -> None:
  for key in grid_publish.all_export_keys():
    prefix = grid_publish.artifact_prefix(*key)
    bin_name = f"{prefix}.bin.gz"
    with (
        open(os.path.join(orig_dir, bin_name), "rb") as f_orig,
        open(os.path.join(rederived_dir, bin_name), "rb") as f_rederived,
    ):
      orig_decomp = gzip.decompress(f_orig.read())
      rederived_decomp = gzip.decompress(f_rederived.read())
    np.testing.assert_allclose(
        np.frombuffer(rederived_decomp, dtype=np.float16),
        np.frombuffer(orig_decomp, dtype=np.float16),
        err_msg=f"Grid slice {prefix} differed between extract and derive",
    )


def _assert_summary_tables_match(
    test_case: absltest.TestCase,
    orig_dir: str,
    rederived_dir: str,
) -> None:
  summary_key_fields = {
      constants.COUNTRY_EXPORT_FILENAME: ("iso_a2",),
      constants.COUNTRY_TEMPORAL_EXPORT_FILENAME: (
          "iso_a2",
          "month",
          "local_hour",
      ),
      constants.FIR_EXPORT_FILENAME: ("fir_id",),
      constants.FIR_TEMPORAL_EXPORT_FILENAME: (
          "fir_id",
          "month",
          "local_hour",
      ),
  }
  for json_filename, key_cols in summary_key_fields.items():
    with (
        open(
            os.path.join(orig_dir, json_filename), "r", encoding="utf-8"
        ) as f_orig,
        open(
            os.path.join(rederived_dir, json_filename), "r", encoding="utf-8"
        ) as f_rederived,
    ):
      orig_rows = json.load(f_orig)
      rederived_rows = json.load(f_rederived)
    test_case.assertNotEmpty(orig_rows)
    _assert_summary_rows_equal(
        test_case, rederived_rows, orig_rows, key_fields=key_cols
    )


class DeriveMainTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.addCleanup(flagsaver.restore_flag_values, flagsaver.save_flag_values())
    self.test_dir = self.create_tempdir().full_path
    self.airports_csv_path = test_fixtures.write_test_airports_csv(
        self.test_dir
    )
    self.country_geojson_path = test_fixtures.write_test_countries_geojson(
        self.test_dir
    )
    self.firs_geojson_path = test_fixtures.write_test_firs_geojson(
        self.test_dir
    )
    self.population_path, self.ghg_path = (
        test_fixtures.write_test_baseline_data(self.test_dir)
    )
    self.parquet_path = test_fixtures.write_test_parquet_segments(self.test_dir)

  def _configure_flags(
      self,
      output_dir: str,
      intermediate_dir: str,
      run_id: str,
      publish_run_id: str = "",
  ) -> None:
    """Sets standard flags used by both extract and derive."""
    FLAGS.input_year = "2025"
    FLAGS.dataset = "example_dataset"
    FLAGS.segments_path = self.parquet_path
    FLAGS.output_prefix = output_dir
    FLAGS.intermediate_prefix = intermediate_dir
    FLAGS.run_id = run_id
    FLAGS.publish_run_id = publish_run_id
    FLAGS.airports_path = self.airports_csv_path
    FLAGS.firs_path = self.firs_geojson_path
    FLAGS.country_geojson_path = self.country_geojson_path
    FLAGS.population_path = self.population_path
    FLAGS.ghg_emissions_path = self.ghg_path
    FLAGS.grid_width = 40
    FLAGS.grid_height = 20
    FLAGS.min_lon = -180.0
    FLAGS.max_lon = 180.0
    FLAGS.min_lat = -85.0
    FLAGS.max_lat = 85.0
    FLAGS.step_size_meters = 10000.0

  def _seed_intermediate_run(
      self,
      intermediate_dir: str,
      run_id: str,
      grid_config: grid_rasterizer.GridConfig | None,
      *,
      failed_shard_count: int = 0,
  ) -> None:
    """Writes a manifest, empty aggregates, and zero grid slices."""
    run_root = intermediate_io.dataset_root(
        intermediate_dir, 2025, "example_dataset"
    )
    intermediate_io.write_manifest(
        intermediate_io.RunManifest(
            run_id=run_id,
            failed_shard_count=failed_shard_count,
            grid_config=grid_config,
        ),
        run_root,
    )
    if grid_config is None:
      return
    intermediate_io.write_country_agg(
        [(
            ("Belgium", "BE", 1, 12),
            (1, 1, 100.0, 100.0, 100.0, 100.0, 10.0, 5.0),
        )],
        run_root,
        run_id,
    )
    intermediate_io.write_fir_agg([], run_root, run_id)
    zeros = np.zeros((grid_config.height, grid_config.width), dtype=np.float32)
    for key in grid_publish.all_export_keys():
      intermediate_io.write_grid_slice(
          zeros,
          root=run_root,
          run_id=run_id,
          prefix=grid_publish.artifact_prefix(*key),
      )

  def test_grid_config_from_manifest_rebuilds_config(self):
    output_dir = self.create_tempdir(name="out").full_path
    intermediate_dir = self.create_tempdir(name="inter").full_path
    self._configure_flags(output_dir, intermediate_dir, run_id="test-run")
    self._seed_intermediate_run(
        intermediate_dir,
        "test-run",
        grid_rasterizer.GridConfig(
            min_lon=-180.0,
            max_lon=180.0,
            min_lat=-85.0,
            max_lat=85.0,
            width=80,
            height=40,
            step_size_meters=5000.0,
        ),
    )

    derive_main.run_derivation()

    dataset_dir = artifact_io.resolve_dataset_dir(
        output_dir, 2025, "example_dataset"
    )
    self.assertIsNotNone(dataset_dir)
    meta_path = os.path.join(dataset_dir, constants.SHARED_GRID_META_FILENAME)
    with open(meta_path, encoding="utf-8") as f:
      meta = json.load(f)
    self.assertEqual(
        (meta["min_lon"], meta["max_lon"], meta["min_lat"], meta["max_lat"]),
        (-180.0, 180.0, -85.0, 85.0),
    )
    self.assertEqual((meta["width"], meta["height"]), (80, 40))

  def test_grid_config_from_manifest_raises_when_missing(self):
    output_dir = self.create_tempdir(name="out").full_path
    intermediate_dir = self.create_tempdir(name="inter").full_path
    self._configure_flags(output_dir, intermediate_dir, run_id="none-run")
    self._seed_intermediate_run(intermediate_dir, "none-run", grid_config=None)
    with self.assertRaisesRegex(
        ValueError, "has no grid_config in its manifest"
    ):
      derive_main.run_derivation()

  def test_run_manifest_from_dict_raises_on_empty_grid_config(self):
    raw = {
        "run_id": "empty-run",
        "failed_shard_count": 0,
        "schema_version": intermediate_io.SCHEMA_VERSION,
        "grid_config": {},
    }
    with self.assertRaisesRegex(ValueError, "must be non-empty"):
      intermediate_io.RunManifest.from_dict(raw)

  def test_grid_config_from_manifest_raises_on_missing_fields(self):
    raw = {
        "run_id": "missing-step-run",
        "failed_shard_count": 0,
        "schema_version": intermediate_io.SCHEMA_VERSION,
        "grid_config": {
            "min_lon": -180.0,
            "max_lon": 180.0,
            "min_lat": -85.0,
            "max_lat": 85.0,
            "width": 80,
            "height": 40,
        },
    }
    with self.assertRaisesRegex(ValueError, "missing fields.*step_size_meters"):
      intermediate_io.RunManifest.from_dict(raw)

  @parameterized.named_parameters(
      dict(
          testcase_name="clean_run",
          run_id="clean-run",
          failed_shard_count=0,
          max_failed=0,
      ),
      dict(
          testcase_name="within_budget",
          run_id="lossy-run",
          failed_shard_count=3,
          max_failed=3,
      ),
      dict(
          testcase_name="disabled_check",
          run_id="lossy-run",
          failed_shard_count=100,
          max_failed=-1,
      ),
  )
  def test_check_failed_shard_budget_accepts(
      self, run_id: str, failed_shard_count: int, max_failed: int
  ):
    manifest = intermediate_io.RunManifest(
        run_id=run_id, failed_shard_count=failed_shard_count
    )
    derive_main.check_failed_shard_budget(manifest, max_failed=max_failed)

  @parameterized.named_parameters(
      dict(
          testcase_name="zero_budget",
          failed_shard_count=1,
          max_failed=0,
          expected_regex=(
              r"lossy-run lost 1 input shards.*--max_failed_shards=0"
          ),
      ),
      dict(
          testcase_name="three_budget",
          failed_shard_count=4,
          max_failed=3,
          expected_regex=(
              r"lossy-run lost 4 input shards.*--max_failed_shards=3"
          ),
      ),
  )
  def test_check_failed_shard_budget_rejects_exceeded_budget(
      self, failed_shard_count: int, max_failed: int, expected_regex: str
  ):
    manifest = intermediate_io.RunManifest(
        run_id="lossy-run", failed_shard_count=failed_shard_count
    )
    with self.assertRaisesRegex(ValueError, expected_regex):
      derive_main.check_failed_shard_budget(manifest, max_failed=max_failed)

  @parameterized.named_parameters(
      dict(
          testcase_name="empty_run_id",
          flag_overrides={"run_id": ""},
          expected_regex="--run_id is required",
      ),
      dict(
          testcase_name="invalid_run_id",
          flag_overrides={"run_id": "../escape"},
          expected_regex="--run_id=.* is invalid",
      ),
      dict(
          testcase_name="invalid_publish_run_id",
          flag_overrides={"publish_run_id": "/tmp/x"},
          expected_regex="--publish_run_id=.* is invalid",
      ),
      dict(
          testcase_name="empty_intermediate_prefix",
          flag_overrides={"intermediate_prefix": ""},
          expected_regex="--intermediate_prefix is required",
      ),
      dict(
          testcase_name="empty_dataset",
          flag_overrides={"dataset": ""},
          expected_regex="--dataset is required",
      ),
      dict(
          testcase_name="unsafe_dataset",
          flag_overrides={"dataset": "../escape"},
          expected_regex="is invalid",
      ),
      dict(
          testcase_name="empty_input_year",
          flag_overrides={"input_year": ""},
          expected_regex="--input_year is required",
      ),
      dict(
          testcase_name="malformed_input_year",
          flag_overrides={"input_year": "../2031"},
          expected_regex="--input_year=.* is invalid",
      ),
      dict(
          testcase_name="missing_country_baseline_sources",
          flag_overrides={
              "population_path": "",
              "ghg_emissions_path": "",
              "country_stats_path": "",
          },
          expected_regex="No country baseline source configured",
      ),
  )
  def test_run_derivation_missing_or_invalid_flags_fail_before_io(
      self, flag_overrides: Mapping[str, str], expected_regex: str
  ):
    output_dir = self.create_tempdir(name="out").full_path
    intermediate_dir = self.create_tempdir(name="inter").full_path
    self._configure_flags(output_dir, intermediate_dir, run_id="run-1")
    loaders = test_fixtures.forbid_reference_data_reads(self)

    with flagsaver.flagsaver(**flag_overrides):
      with self.assertRaisesRegex(ValueError, expected_regex):
        derive_main.run_derivation()
    test_fixtures.assert_loaders_not_called(loaders)
    self.assertEmpty(os.listdir(output_dir))

  def test_run_derivation_rejects_equal_prefixes_before_any_io(self):
    output_dir = self.create_tempdir().full_path
    self._configure_flags(output_dir, output_dir, run_id="some-run")
    FLAGS.output_prefix = output_dir + "/"
    loaders = test_fixtures.forbid_reference_data_reads(self)
    with self.assertRaisesRegex(ValueError, "must name different roots"):
      derive_main.run_derivation()
    test_fixtures.assert_loaders_not_called(loaders)
    self.assertEmpty(os.listdir(output_dir))

  def test_run_derivation_missing_manifest_raises(self):
    output_dir = self.create_tempdir(name="out").full_path
    intermediate_dir = self.create_tempdir(name="inter").full_path
    self._configure_flags(output_dir, intermediate_dir, run_id="run-missing")

    with self.assertRaisesRegex(ValueError, "has no manifest under"):
      derive_main.run_derivation()

  def test_run_derivation_refuses_to_overwrite_existing_run(self):
    output_dir = self.create_tempdir(name="out").full_path
    intermediate_dir = os.path.join(self.test_dir, "missing_inter")
    self._configure_flags(
        output_dir,
        intermediate_dir,
        run_id="run-1",
        publish_run_id="already-exists",
    )
    existing_dir = artifact_io.run_output_dir(
        output_dir,
        year=2025,
        dataset="example_dataset",
        run_id="already-exists",
    )
    os.makedirs(existing_dir, exist_ok=True)
    artifact_io.write_run_pointer(
        output_dir,
        year=2025,
        dataset="example_dataset",
        run_id="already-exists",
    )

    with self.assertRaisesRegex(ValueError, "'already-exists' already exists"):
      derive_main.run_derivation()
    self.assertEmpty(os.listdir(existing_dir))
    self.assertEqual(
        artifact_io.read_run_pointer(output_dir, 2025, "example_dataset"),
        "already-exists",
    )

  def test_derive_main_enforces_failure_budget_on_lossy_intermediate(self):
    output_dir = self.create_tempdir(name="out").full_path
    intermediate_dir = self.create_tempdir(name="inter").full_path
    run_id = "2026-01-02T03-04-05Z-lossy"
    self._configure_flags(output_dir, intermediate_dir, run_id=run_id)
    self._seed_intermediate_run(
        intermediate_dir,
        run_id,
        grid_rasterizer.GridConfig(
            min_lon=-180.0,
            max_lon=180.0,
            min_lat=-85.0,
            max_lat=85.0,
            width=40,
            height=20,
            step_size_meters=10000.0,
        ),
        failed_shard_count=1,
    )

    FLAGS.max_failed_shards = 0
    with self.assertRaisesRegex(
        ValueError, "lost 1 input shards.*--max_failed_shards=0"
    ):
      derive_main.run_derivation()
    self.assertIsNone(
        artifact_io.read_run_pointer(output_dir, 2025, "example_dataset")
    )

    FLAGS.max_failed_shards = 1
    FLAGS.publish_run_id = "2026-02-03T04-05-06Z-rederive"
    derive_main.run_derivation()
    self.assertEqual(
        artifact_io.read_run_pointer(output_dir, 2025, "example_dataset"),
        "2026-02-03T04-05-06Z-rederive",
    )

  def test_run_derivation_empty_spatial_index_raises(self):
    output_dir = self.create_tempdir(name="out").full_path
    intermediate_dir = self.create_tempdir(name="inter").full_path
    empty_geojson = _write_empty_geometry_geojson(self.test_dir)

    self._configure_flags(output_dir, intermediate_dir, run_id="run-empty")
    FLAGS.country_geojson_path = empty_geojson
    _populate_empty_run_intermediate(intermediate_dir, "run-empty")

    with self.assertRaisesRegex(
        RuntimeError, "Country SpatialIndex initialized with 0 polygons"
    ):
      derive_main.run_derivation()

  def _run_extract_and_derive_pipelines(self) -> tuple[str, str]:
    output_dir = os.path.join(self.test_dir, "direct_extract_output")
    intermediate_dir = os.path.join(self.test_dir, "intermediate")
    rederived_dir = os.path.join(self.test_dir, "rederived_output")

    extract_run_id = "2026-01-02T03-04-05Z-extract"
    rederived_run_id = "2026-02-03T04-05-06Z-rederive"

    self._configure_flags(output_dir, intermediate_dir, run_id=extract_run_id)
    extract_pipeline_main.run_pipeline(
        custom_options=PipelineOptions(["--runner=DirectRunner"])
    )

    orig_dataset_dir = artifact_io.resolve_dataset_dir(
        output_dir, 2025, "example_dataset"
    )
    self.assertIsNotNone(orig_dataset_dir)
    self.assertEqual(
        artifact_io.read_run_pointer(output_dir, 2025, "example_dataset"),
        extract_run_id,
    )

    self._configure_flags(
        rederived_dir,
        intermediate_dir,
        run_id=extract_run_id,
        publish_run_id=rederived_run_id,
    )
    derive_main.run_derivation()

    rederived_dataset_dir = artifact_io.resolve_dataset_dir(
        rederived_dir, 2025, "example_dataset"
    )
    self.assertIsNotNone(rederived_dataset_dir)
    self.assertEqual(
        artifact_io.read_run_pointer(rederived_dir, 2025, "example_dataset"),
        rederived_run_id,
    )
    return orig_dataset_dir, rederived_dataset_dir

  def test_end_to_end_parity_artifact_listings(self):
    orig_dir, rederived_dir = self._run_extract_and_derive_pipelines()
    orig_files = sorted(os.listdir(orig_dir))
    rederived_files = sorted(os.listdir(rederived_dir))
    self.assertEqual(orig_files, rederived_files)

    expected_total_count = (
        len(grid_publish.all_export_keys())
        + 1  # grid_meta.json
        + len(derive._DATASET_ARTIFACTS)
        + 2  # countries.geojson, firs.geojson
    )
    self.assertLen(orig_files, expected_total_count)

  def test_end_to_end_parity_geojson_boundaries(self):
    orig_dir, rederived_dir = self._run_extract_and_derive_pipelines()
    _assert_geojson_files_match(
        self, orig_dir, rederived_dir, constants.COUNTRIES_GEOJSON_FILENAME
    )
    _assert_geojson_files_match(
        self, orig_dir, rederived_dir, constants.FIRS_GEOJSON_FILENAME
    )

  def test_end_to_end_parity_grid_metadata_and_slices(self):
    orig_dir, rederived_dir = self._run_extract_and_derive_pipelines()
    orig_value_max = test_fixtures.read_value_max(orig_dir)
    rederived_value_max = test_fixtures.read_value_max(rederived_dir)
    self.assertEqual(orig_value_max, rederived_value_max)
    self.assertGreater(orig_value_max["density"], 0.0)
    self.assertGreater(orig_value_max["impact"], 0.0)
    _assert_all_grid_slices_match(orig_dir, rederived_dir)

  def test_end_to_end_parity_summary_tables(self):
    orig_dir, rederived_dir = self._run_extract_and_derive_pipelines()
    _assert_summary_tables_match(self, orig_dir, rederived_dir)

  def test_run_derivation_missing_grid_slices_raises(self):
    output_dir = os.path.join(self.test_dir, "missing_grid_output")
    intermediate_dir = os.path.join(self.test_dir, "intermediate_missing_grid")
    run_id = "2026-01-02T03-04-05Z-missinggrid"

    self._configure_flags(output_dir, intermediate_dir, run_id=run_id)
    extract_pipeline_main.run_pipeline(
        custom_options=PipelineOptions(["--runner=DirectRunner"])
    )

    # Delete one intermediate grid slice
    run_root = intermediate_io.dataset_root(
        intermediate_dir, 2025, "example_dataset"
    )
    missing_slice = os.path.join(
        intermediate_io.run_dir(run_root, run_id),
        intermediate_io._GRID_SLICES,
        "total_global_impact_grid.npz",
    )
    os.remove(missing_slice)

    rederived_dir = os.path.join(self.test_dir, "rederived_missing_grid")
    FLAGS.output_prefix = rederived_dir
    FLAGS.publish_run_id = "2026-02-03T04-05-06Z-rederive"

    with self.assertRaisesRegex(ValueError, "is missing 1 of 74 grid slices"):
      derive_main.run_derivation()

  @parameterized.named_parameters(
      dict(
          testcase_name="empty_dataset",
          overrides={"dataset": ""},
          expected_regex="dataset is required",
      ),
      dict(
          testcase_name="invalid_dataset",
          overrides={"dataset": "a/b"},
          expected_regex="is invalid",
      ),
      dict(
          testcase_name="invalid_year",
          overrides={"year": -1},
          expected_regex="is invalid",
      ),
      dict(
          testcase_name="blank_intermediate_prefix",
          overrides={"intermediate_prefix": "  "},
          expected_regex="intermediate_prefix is required",
      ),
      dict(
          testcase_name="blank_run_id",
          overrides={"run_id": "  "},
          expected_regex="run_id is required",
      ),
      dict(
          testcase_name="blank_output_prefix",
          overrides={"output_prefix": "  "},
          expected_regex="output_prefix is required",
      ),
      dict(
          testcase_name="blank_country_geojson_path",
          overrides={"country_geojson_path": "  "},
          expected_regex="country_geojson_path is required",
      ),
      dict(
          testcase_name="blank_firs_path",
          overrides={"firs_path": "  "},
          expected_regex="firs_path is required",
      ),
      dict(
          testcase_name="missing_country_baseline_source",
          overrides={"population_path": ""},
          expected_regex="No country baseline source configured",
      ),
      dict(
          testcase_name="invalid_run_id",
          overrides={"run_id": ".."},
          expected_regex="--run_id=.* is invalid",
      ),
      dict(
          testcase_name="invalid_publish_run_id",
          overrides={"publish_run_id": "/escape"},
          expected_regex="--publish_run_id=.* is invalid",
      ),
  )
  def test_derive_config_validation_rules(
      self, overrides: Mapping[str, Any], expected_regex: str
  ):
    base_config = derive_main.DeriveConfig(
        dataset="example_dataset",
        year=2025,
        intermediate_prefix="/inter",
        run_id="test-run",
        output_prefix="/out",
        country_geojson_path="/countries.geojson",
        firs_path="/firs.geojson",
        population_path="/pop.csv",
    )

    with self.assertRaisesRegex(ValueError, expected_regex):
      dataclasses.replace(base_config, **overrides)

  def test_derive_config_strips_whitespace_from_string_fields(self):
    config = derive_main.DeriveConfig(
        dataset="  example_dataset  ",
        year=2025,
        intermediate_prefix="  /inter  ",
        run_id="  test-run  ",
        publish_run_id="  test-pub  ",
        output_prefix="  /out  ",
        country_geojson_path="  /countries.geojson  ",
        firs_path="  /firs.geojson  ",
        population_path="  /pop.csv  ",
    )
    self.assertEqual(
        (
            config.dataset,
            config.intermediate_prefix,
            config.run_id,
            config.publish_run_id,
            config.output_prefix,
        ),
        ("example_dataset", "/inter", "test-run", "test-pub", "/out"),
    )

  def test_derive_config_from_flags(self):
    output_dir = os.path.join(self.test_dir, "cfg_from_flags_out")
    intermediate_dir = os.path.join(self.test_dir, "cfg_from_flags_inter")
    self._configure_flags(
        output_dir,
        intermediate_dir,
        run_id="run-flag-1",
        publish_run_id="pub-flag-1",
    )
    config = derive_main.DeriveConfig.from_flags()
    self.assertEqual((config.dataset, config.year), ("example_dataset", 2025))
    self.assertEqual(config.intermediate_prefix, intermediate_dir)
    self.assertEqual(config.run_id, "run-flag-1")
    self.assertEqual(config.publish_run_id, "pub-flag-1")
    self.assertEqual(config.output_prefix, output_dir)

  def test_run_derivation_with_explicit_config(self):
    output_dir = os.path.join(self.test_dir, "extract_for_explicit_derive")
    intermediate_dir = os.path.join(self.test_dir, "inter_for_explicit_derive")
    rederived_dir = os.path.join(self.test_dir, "explicit_derive_out")
    extract_run_id = "2026-01-02T03-04-05Z-explicit-extract"
    rederived_run_id = "2026-02-03T04-05-06Z-explicit-rederive"

    self._configure_flags(output_dir, intermediate_dir, run_id=extract_run_id)
    extract_pipeline_main.run_pipeline(
        custom_options=PipelineOptions(["--runner=DirectRunner"])
    )

    # Save and restore clean default flags so derivation relies on config only.
    flagsaver.restore_flag_values(flagsaver.save_flag_values())

    config = derive_main.DeriveConfig(
        dataset="example_dataset",
        year=2025,
        intermediate_prefix=intermediate_dir,
        run_id=extract_run_id,
        publish_run_id=rederived_run_id,
        output_prefix=rederived_dir,
        country_geojson_path=self.country_geojson_path,
        firs_path=self.firs_geojson_path,
        population_path=self.population_path,
        ghg_emissions_path=self.ghg_path,
    )
    derive_main.run_derivation(config=config)
    self.assertEqual(
        artifact_io.read_run_pointer(rederived_dir, 2025, "example_dataset"),
        rederived_run_id,
    )

  def test_main_entry_point(self):
    with mock.patch.object(
        derive_main, "run_derivation", autospec=True, spec_set=True
    ) as mock_run:
      derive_main.main([])
      mock_run.assert_called_once()

  def test_set_up_module_marks_unparsed_flags_for_standalone_runners(self):
    self.addCleanup(FLAGS.mark_as_parsed)
    FLAGS.unparse_flags()
    self.assertFalse(FLAGS.is_parsed())
    setUpModule()
    self._configure_flags(
        os.path.join(self.test_dir, "out"),
        os.path.join(self.test_dir, "inter"),
        run_id="standalone-run",
    )
    self.assertEqual(
        derive_main.DeriveConfig.from_flags().run_id, "standalone-run"
    )


if __name__ == "__main__":
  absltest.main()
