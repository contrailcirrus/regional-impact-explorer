"""Integration tests for extract_pipeline_main and pipeline graph execution."""

from collections.abc import Sequence
import dataclasses
import gzip
import json
import os
import shutil
from typing import Any
from unittest import mock

from absl import flags
from absl.testing import absltest
from absl.testing import flagsaver
from absl.testing import parameterized
import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.testing import util as beam_testing_util
from apache_beam.testing.test_pipeline import TestPipeline
import numpy as np

from common import artifact_io
from common import constants
from common import fs
from data_extraction import beam_stages
from data_extraction import common_flags
from data_extraction import derive_main
from data_extraction import extract_pipeline_main
from data_extraction import grid_publish
from data_extraction import grid_rasterizer
from data_extraction import intermediate_io
from data_extraction import provenance
from data_extraction import test_fixtures

FLAGS = flags.FLAGS


def setUpModule() -> None:
  FLAGS.mark_as_parsed()


def _touch_files(paths: Sequence[str]) -> None:
  """Touches files on disk with dummy content."""
  for p in paths:
    with open(p, "w", encoding="utf-8") as fp:
      fp.write("content")


class ExtractPipelineIntegrationTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    # Tests set FLAGS directly; restore them so values never leak between
    # tests regardless of execution order.
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

  def test_flip_run_pointer_fn_success(self):
    flip_fn = beam_stages.FlipRunPointerFn(
        output_root=self.test_dir,
        year=2025,
        dataset="example_dataset",
        publish_run_id="run-123",
        expected_signal_count=2,
    )
    result = flip_fn(["signal_1", "signal_2"])
    self.assertTrue(os.path.exists(result))
    self.assertEqual(
        artifact_io.read_run_pointer(self.test_dir, 2025, "example_dataset"),
        "run-123",
    )

  def test_flip_run_pointer_fn_raises_on_signal_mismatch(self):
    flip_fn = beam_stages.FlipRunPointerFn(
        output_root=self.test_dir,
        year=2025,
        dataset="example_dataset",
        publish_run_id="run-123",
        expected_signal_count=3,
    )
    with self.assertRaisesRegex(
        RuntimeError, r"expected 3 publish signals, saw 1"
    ):
      flip_fn(["signal_1"])

  def _make_export_dofn(self, dataset_dir: str):
    return beam_stages.ExportArtifactsDoFn(
        year=2025,
        dataset="example_dataset",
        intermediate_root="",
        intermediate_run_root="",
        run_id="test-run",
        segments_pattern="",
        input_file_count=0,
        grid_config=grid_rasterizer.GridConfig(
            min_lon=-180.0,
            max_lon=180.0,
            min_lat=-85.0,
            max_lat=85.0,
            width=800,
            height=400,
            step_size_meters=10000.0,
        ),
        started_at="2026-01-01T00:00:00Z",
        country_areas={},
        fir_areas={},
        baseline_stats={},
        dataset_dir=dataset_dir,
    )

  def test_export_artifacts_dofn_process(self):
    output_dir = os.path.join(self.test_dir, "export_dofn_out")
    dataset_dir = os.path.join(output_dir, "2025", "example_dataset")
    dofn = self._make_export_dofn(dataset_dir)
    results = list(
        dofn.process(
            None,
            country_items=[(
                ("Belgium", "BE", 202501, 12),
                (1, 1, 10.0, 5.0, 10.0, 5.0, 0.0, 0.0),
            )],
            fir_items=[],
            failed_shard_count=0,
        )
    )
    self.assertEqual(results, ["DONE"])

  def test_export_artifacts_dofn_raises_on_empty_aggregates(self):
    output_dir = os.path.join(self.test_dir, "export_dofn_empty")
    dataset_dir = os.path.join(output_dir, "2025", "example_dataset")
    dofn = self._make_export_dofn(dataset_dir)
    with self.assertRaisesRegex(ValueError, "No country or FIR aggregates"):
      list(
          dofn.process(
              None, country_items=[], fir_items=[], failed_shard_count=0
          )
      )

  def test_export_artifacts_dofn_records_failed_shard_count(self):
    intermediate_root = self.create_tempdir().full_path
    dataset_dir = self.create_tempdir().full_path
    dofn = beam_stages.ExportArtifactsDoFn(
        year=2025,
        dataset="example_dataset",
        intermediate_root=intermediate_root,
        intermediate_run_root=intermediate_root,
        run_id="test-run",
        segments_pattern="/fake/*.parquet",
        input_file_count=2,
        grid_config=grid_rasterizer.GridConfig(
            min_lon=-180.0,
            max_lon=180.0,
            min_lat=-85.0,
            max_lat=85.0,
            width=40,
            height=20,
            step_size_meters=10000.0,
        ),
        started_at="2026-01-01T00:00:00Z",
        country_areas={},
        fir_areas={},
        baseline_stats={},
        dataset_dir=dataset_dir,
    )
    list(
        dofn.process(
            None,
            country_items=[(
                ("Belgium", "BE", 202501, 12),
                (1, 1, 10.0, 5.0, 10.0, 5.0, 0.0, 0.0),
            )],
            fir_items=[],
            failed_shard_count=2,
        )
    )
    manifest = intermediate_io.read_manifest(intermediate_root, "test-run")
    self.assertEqual(manifest.failed_shard_count, 2)
    self.assertEqual(manifest.input_file_count, 2)

  def test_require_path_returns_stripped_value(self):
    FLAGS.output_prefix = "  /some/valid/path  "
    self.assertEqual(
        common_flags.require_path("output_prefix"), "/some/valid/path"
    )

  @parameterized.named_parameters(
      dict(testcase_name="empty", raw_value=""),
      dict(testcase_name="whitespace", raw_value="   "),
  )
  def test_require_path_raises_on_empty_or_blank(self, raw_value):
    FLAGS.output_prefix = raw_value
    with self.assertRaisesRegex(ValueError, "--output_prefix is required"):
      common_flags.require_path("output_prefix")

  @parameterized.named_parameters(
      dict(testcase_name="empty", pattern=""),
      dict(testcase_name="whitespace", pattern="   "),
  )
  def test_resolve_parquet_files_empty(self, pattern):
    self.assertEmpty(extract_pipeline_main.resolve_parquet_files(pattern))

  def test_resolve_parquet_files_single_file(self):
    test_file = os.path.join(self.test_dir, "test.parquet")
    _touch_files([test_file])
    self.assertEqual(
        extract_pipeline_main.resolve_parquet_files(test_file), [test_file]
    )

  def test_resolve_parquet_files_propagates_filesystem_errors(self):
    test_subdir = os.path.join(self.test_dir, "unreadable_dir")
    os.makedirs(test_subdir, exist_ok=True)
    with mock.patch.object(
        fs,
        "listdir",
        side_effect=PermissionError("denied"),
        autospec=True,
        spec_set=True,
    ):
      with self.assertRaisesRegex(PermissionError, "denied"):
        extract_pipeline_main.resolve_parquet_files(test_subdir)

  def test_resolve_parquet_files_directory(self):
    test_subdir = os.path.join(self.test_dir, "pq_dir")
    os.makedirs(test_subdir, exist_ok=True)
    f1 = os.path.join(test_subdir, "part-1.pq")
    part_2_path = os.path.join(test_subdir, "part-2.parquet")
    f3 = os.path.join(test_subdir, "other.txt")
    _touch_files([f1, part_2_path, f3])
    resolved = extract_pipeline_main.resolve_parquet_files(test_subdir)
    self.assertCountEqual(resolved, [f1, part_2_path])

  def test_resolve_parquet_files_glob(self):
    test_subdir = os.path.join(self.test_dir, "glob_dir")
    os.makedirs(test_subdir, exist_ok=True)
    f1 = os.path.join(test_subdir, "part-1.pq")
    part_2_path = os.path.join(test_subdir, "part-2.parquet")
    f3 = os.path.join(test_subdir, "sub.txt")
    _touch_files([f1, part_2_path, f3])
    resolved = extract_pipeline_main.resolve_parquet_files(
        os.path.join(test_subdir, "*.p*")
    )
    self.assertCountEqual(resolved, [f1, part_2_path])

  def test_extract_pipeline_graph_reshuffle_files(self):
    test_pipeline = TestPipeline()
    p_files = (
        test_pipeline
        | "CreateFileShards" >> beam.Create(["/fake/path.pq"])
    )
    reshuffled = p_files | "ReshuffleFiles" >> beam.Reshuffle()
    self.assertIsNotNone(reshuffled)
    self.assertIn("ReshuffleFiles", reshuffled.producer.full_label)

  def _run_extract_pipeline_to_dataset_dir(self, output_dir: str) -> str:
    self._configure_run(self.parquet_path, output_dir)
    options = PipelineOptions(["--runner=DirectRunner"])
    extract_pipeline_main.run_pipeline(custom_options=options)
    dataset_dir = artifact_io.resolve_dataset_dir(
        output_dir, 2025, "example_dataset"
    )
    self.assertIsNotNone(dataset_dir)
    return dataset_dir

  def _get_default_extract_dataset_dir(self) -> str:
    output_dir = os.path.join(self.test_dir, "output")
    return self._run_extract_pipeline_to_dataset_dir(output_dir)

  def test_end_to_end_pipeline_country_and_fir_exports(self):
    dataset_dir = self._get_default_extract_dataset_dir()
    country_fir_json = os.path.join(
        dataset_dir, "country_fir_density_df_export_per_country.json"
    )
    with open(country_fir_json, "r", encoding="utf-8") as f:
      c_data = json.load(f)
    c_be = next(
        (row_item for row_item in c_data if row_item["iso_a2"] == "BE"), None
    )
    self.assertIsNotNone(c_be)
    self.assertEmpty(
        {"prop_sum_ef_mj", "total_segment_count", "country"} & c_be.keys()
    )
    self.assertContainsSubset(
        ["co2e_gwp50_tonnes", "mean_rf_mw_per_sq_m", "segment_count"],
        c_be.keys(),
    )
    self.assertEqual(
        (
            c_be["iso_a3"],
            c_be["population"],
            c_be["ghg_mtco2e"],
            all("iso_a3" in row for row in c_data),
        ),
        ("BEL", 1000000.0, 10.0, True),
    )

    fir_json_path = os.path.join(
        dataset_dir, "fir_density_df_export_per_country.json"
    )
    with open(fir_json_path, "r", encoding="utf-8") as f:
      f_data = json.load(f)
    self.assertNotEmpty(f_data)

  def test_end_to_end_pipeline_grid_artifacts(self):
    dataset_dir = self._get_default_extract_dataset_dir()
    total_impact_bin = os.path.join(
        dataset_dir, "total_global_impact_grid.bin.gz"
    )
    shared_grid_meta = os.path.join(dataset_dir, "grid_meta.json")
    self.assertTrue(os.path.exists(total_impact_bin))
    with open(total_impact_bin, "rb") as f:
      self.assertLen(gzip.decompress(f.read()), 40 * 20 * 2)
    with open(shared_grid_meta, "r", encoding="utf-8") as f:
      value_max = json.load(f)[grid_publish.VALUE_MAX_META_KEY]
    self.assertCountEqual(value_max, ["density", "impact"])
    self.assertGreater(value_max["density"], 0.0)
    self.assertGreater(value_max["impact"], 0.0)

  def test_end_to_end_pipeline_published_boundaries(self):
    dataset_dir = self._get_default_extract_dataset_dir()
    with open(
        os.path.join(dataset_dir, constants.COUNTRIES_GEOJSON_FILENAME),
        "r",
        encoding="utf-8",
    ) as f:
      published_countries = json.load(f)
    with open(self.country_geojson_path, "r", encoding="utf-8") as f:
      self.assertEqual(
          published_countries["features"], json.load(f)["features"]
      )
    with open(
        os.path.join(dataset_dir, constants.FIRS_GEOJSON_FILENAME),
        "r",
        encoding="utf-8",
    ) as f:
      published_firs = json.load(f)
    self.assertNotEmpty(published_firs["features"])
    self.assertTrue(
        all(
            "fir_id" in feat["properties"] and "fir_icao" in feat["properties"]
            for feat in published_firs["features"]
        )
    )

  def _run_extract_pipeline_with_intermediate(
      self, output_dir: str, intermediate_dir: str, run_id: str
  ) -> None:
    self._configure_run(self.parquet_path, output_dir)
    FLAGS.intermediate_prefix = intermediate_dir
    FLAGS.run_id = run_id
    extract_pipeline_main.run_pipeline(
        custom_options=PipelineOptions(["--runner=DirectRunner"])
    )

  def test_end_to_end_pipeline_writes_intermediate_artifacts(self):
    output_dir = os.path.join(self.test_dir, "output_intermediate")
    intermediate_dir = os.path.join(self.test_dir, "intermediate")
    run_id = "2026-01-02T03-04-05Z-test"
    self._run_extract_pipeline_with_intermediate(
        output_dir, intermediate_dir, run_id
    )

    run_root = os.path.join(intermediate_dir, "2025", "example_dataset")
    manifest = intermediate_io.read_manifest(run_root, run_id)
    self.assertEqual(
        (manifest.run_id, manifest.schema_version),
        (run_id, intermediate_io.SCHEMA_VERSION),
    )

    country_rows = intermediate_io.read_country_agg(run_root, run_id)
    self.assertIn("BE", {key[1] for key, _ in country_rows})

    fir_rows = intermediate_io.read_fir_agg(run_root, run_id)
    self.assertIn("EBBU", {key[1] for key, _ in fir_rows})
    self.assertEqual(
        artifact_io.read_run_pointer(output_dir, 2025, "example_dataset"),
        run_id,
    )
    reduce_provenance = intermediate_io.read_provenance(run_root, run_id)
    self.assertEqual(
        (reduce_provenance["stage"], reduce_provenance["run_id"]),
        ("reduce", run_id),
    )

  def test_end_to_end_pipeline_publishes_provenance(self):
    dataset_dir = self._get_default_extract_dataset_dir()
    with open(
        os.path.join(dataset_dir, provenance.PROVENANCE_FILENAME),
        "r",
        encoding="utf-8",
    ) as f:
      published = json.load(f)

    self.assertEqual(
        (published["stage"], published["dataset"], published["year"]),
        ("reduce", "example_dataset", 2025),
    )
    self.assertEqual(
        published["segments"],
        {
            "path": self.parquet_path,
            "file_count": 1,
            "files": [self.parquet_path],
        },
    )
    sources = {
        "airports": self.airports_csv_path,
        "countries": self.country_geojson_path,
        "firs": self.firs_geojson_path,
        "population": self.population_path,
        "ghg_emissions": self.ghg_path,
    }
    self.assertCountEqual(published["reference_inputs"], sources)
    for name, source in sources.items():
      record = published["reference_inputs"][name]
      with open(source, "rb") as f:
        original = f.read()
      with open(os.path.join(dataset_dir, record["copy"]), "rb") as f:
        self.assertEqual(f.read(), original, name)
      self.assertEqual(record["source"], source)

  def test_workers_read_the_published_reference_copies(self):
    # Deleting the originals once they are copied proves the workers load
    # the copies: provenance.json must describe exactly what the run used.
    copy_reference_inputs = provenance.copy_reference_inputs

    def copy_then_delete_worker_inputs(run_dir, sources):
      copied = copy_reference_inputs(run_dir, sources)
      for name in ("airports", "firs", "countries"):
        os.remove(sources[name])
      return copied

    output_dir = os.path.join(self.test_dir, "output_copies")
    with mock.patch.object(
        provenance,
        "copy_reference_inputs",
        side_effect=copy_then_delete_worker_inputs,
    ):
      dataset_dir = self._run_extract_pipeline_to_dataset_dir(output_dir)

    self.assertTrue(
        os.path.exists(
            os.path.join(dataset_dir, constants.COUNTRY_EXPORT_FILENAME)
        )
    )

  def test_missing_airports_path_fails_before_writing_the_run(self):
    output_dir = os.path.join(self.test_dir, "output_missing_airports")
    self._configure_run(self.parquet_path, output_dir)
    FLAGS.airports_path = os.path.join(self.test_dir, "no_such_airports.csv")

    with self.assertRaisesRegex(ValueError, "airports="):
      extract_pipeline_main.run_pipeline(
          custom_options=PipelineOptions(["--runner=DirectRunner"])
      )
    self.assertFalse(os.path.exists(output_dir))


  def test_firs_path_under_another_years_directory_is_accepted(self):
    # Boundary files are independent of --input_year: an ICAO 2015 FIR file
    # must be usable for a 2025 run in both reduce and derive.
    year_dir = self.create_tempdir(name="2015")
    firs_2015 = os.path.join(year_dir.full_path, "firs.geojson")
    shutil.copy(self.firs_geojson_path, firs_2015)
    output_dir = self.create_tempdir(name="output").full_path
    run_id = "2026-01-02T03-04-05Z-firs2015"

    self._configure_run(self.parquet_path, output_dir)
    FLAGS.firs_path = firs_2015
    FLAGS.intermediate_prefix = self.create_tempdir(name="inter").full_path
    FLAGS.run_id = run_id
    extract_pipeline_main.run_pipeline(
        custom_options=PipelineOptions(["--runner=DirectRunner"])
    )
    self.assertEqual(
        artifact_io.read_run_pointer(output_dir, 2025, "example_dataset"),
        run_id,
    )

    rederived_dir = self.create_tempdir(name="rederived").full_path
    rederived_run_id = "2026-02-03T04-05-06Z-firs2015"
    FLAGS.publish_run_id = rederived_run_id
    FLAGS.output_prefix = rederived_dir
    derive_main.run_derivation()
    self.assertEqual(
        artifact_io.read_run_pointer(rederived_dir, 2025, "example_dataset"),
        rederived_run_id,
    )

  def test_failed_shard_threshold_allows_a_clean_run(self):
    self.assertIsNone(beam_stages.enforce_failed_shard_threshold(0, 0))

  def test_failed_shard_threshold_fails_the_pipeline_by_default(self):
    # One unreadable shard must stop the run, not shrink the output.
    with self.assertRaisesRegex(
        RuntimeError,
        r"1 shards failed to process, which exceeds the allowed maximum of 0",
    ):
      beam_stages.enforce_failed_shard_threshold(1, 0)

  def test_failed_shard_threshold_allows_failures_within_budget(self):
    self.assertIsNone(beam_stages.enforce_failed_shard_threshold(3, 3))

  def test_failed_shard_threshold_rejects_failures_exceeding_budget(self):
    with self.assertRaisesRegex(
        RuntimeError,
        r"4 shards failed to process, which exceeds the allowed maximum of 3",
    ):
      beam_stages.enforce_failed_shard_threshold(4, 3)

  def test_failed_shard_threshold_can_be_disabled(self):
    self.assertIsNone(beam_stages.enforce_failed_shard_threshold(100, -1))

  def test_dataset_flag_has_no_default(self):
    self.assertEqual(FLAGS["dataset"].default, "")

  @parameterized.parameters(
      "output_prefix",
      "segments_path",
      "airports_path",
      "firs_path",
      "country_geojson_path",
      "population_path",
      "ghg_emissions_path",
      "country_stats_path",
  )
  def test_path_flags_default_to_empty_string(self, flag_name):
    self.assertEqual(FLAGS[flag_name].default, "")

  @parameterized.parameters("", "   ")
  def test_require_dataset_rejects_empty_values(self, value):
    with flagsaver.flagsaver(dataset=value):
      with self.assertRaisesRegex(ValueError, "--dataset is required"):
        common_flags.require_dataset()

  def test_require_dataset_error_names_the_server_env_var(self):
    with flagsaver.flagsaver(dataset=""):
      with self.assertRaisesRegex(ValueError, "CONTRAILS_DATASET"):
        common_flags.require_dataset()

  @parameterized.parameters("../escape", "a/b", "a b", "a.b", "x" * 65)
  def test_require_dataset_rejects_unsafe_values(self, value):
    with flagsaver.flagsaver(dataset=value):
      with self.assertRaisesRegex(ValueError, "is invalid"):
        common_flags.require_dataset()

  # The server only accepts ASCII; str.isalnum() is true for all of these.
  @parameterized.parameters("café", "données", "数据", "２０２５")
  def test_require_dataset_rejects_non_ascii_letters_and_digits(self, value):
    with flagsaver.flagsaver(dataset=value):
      with self.assertRaisesRegex(ValueError, "is invalid"):
        common_flags.require_dataset()

  @parameterized.parameters(
      ("  my-data_1 ", "my-data_1"),
      ("x" * 64, "x" * 64),
  )
  def test_require_dataset_strips_and_accepts_safe_values(
      self, value, expected
  ):
    with flagsaver.flagsaver(dataset=value):
      self.assertEqual(common_flags.require_dataset(), expected)

  def test_run_pipeline_fails_before_any_io_without_dataset(self):
    output_dir = os.path.join(self.test_dir, "output_no_dataset")
    self._configure_run(self.parquet_path, output_dir)
    with flagsaver.flagsaver(dataset=""):
      with self.assertRaisesRegex(ValueError, "--dataset is required"):
        extract_pipeline_main.run_pipeline(
            custom_options=PipelineOptions(["--runner=DirectRunner"])
        )
    self.assertFalse(os.path.exists(output_dir))

  def test_input_year_flag_has_no_default(self):
    self.assertEqual(FLAGS["input_year"].default, "")
    self.assertFalse(hasattr(constants, "DEFAULT_YEAR"))

  @parameterized.parameters("", "   ")
  def test_require_input_year_rejects_empty_values(self, value):
    with flagsaver.flagsaver(input_year=value):
      with self.assertRaisesRegex(ValueError, "--input_year is required"):
        common_flags.require_input_year()

  @parameterized.parameters(
      "203", "20311", "20x1", "-203", "../x", "20 31", "２０３１"
  )
  def test_require_input_year_rejects_malformed_values(self, value):
    with flagsaver.flagsaver(input_year=value):
      with self.assertRaisesRegex(ValueError, "is invalid"):
        common_flags.require_input_year()

  @parameterized.parameters(("2031", 2031), (" 1999 ", 1999), ("0042", 42))
  def test_require_input_year_strips_and_returns_an_int(self, value, expected):
    with flagsaver.flagsaver(input_year=value):
      self.assertEqual(common_flags.require_input_year(), expected)
      self.assertIsInstance(common_flags.require_input_year(), int)

  def test_run_pipeline_fails_before_any_io_without_input_year(self):
    output_dir = os.path.join(self.test_dir, "output_no_year")
    self._configure_run(self.parquet_path, output_dir)
    with flagsaver.flagsaver(input_year=""):
      with self.assertRaisesRegex(ValueError, "--input_year is required"):
        extract_pipeline_main.run_pipeline(
            custom_options=PipelineOptions(["--runner=DirectRunner"])
        )
    self.assertFalse(os.path.exists(output_dir))

  @parameterized.parameters(
      "abc1234", "unversioned", "rev/123+dirty", "v1.2", "x" * 300
  )
  def test_auto_generated_run_ids_are_valid(self, tag):
    run_id = intermediate_io.make_run_id(tag)
    self.assertEqual(artifact_io.validate_run_id(run_id), run_id)

  @parameterized.product(
      flag_name=("run_id", "publish_run_id"),
      value=("..", "/tmp/x", "a/b", "２０２５"),
  )
  def test_run_pipeline_rejects_unsafe_run_ids_before_any_io(
      self, flag_name, value
  ):
    output_dir = self.create_tempdir().full_path
    self._configure_run(self.parquet_path, output_dir)
    with flagsaver.flagsaver(**{flag_name: value}):
      with self.assertRaisesRegex(ValueError, f"--{flag_name}=.* is invalid"):
        extract_pipeline_main.run_pipeline(
            custom_options=PipelineOptions(["--runner=DirectRunner"])
        )
    self.assertEmpty(os.listdir(output_dir))

  def test_run_pipeline_refuses_to_overwrite_an_existing_run(self):
    output_dir = self.create_tempdir().full_path
    self._configure_run(self.parquet_path, output_dir)
    live_dir = artifact_io.run_output_dir(
        output_dir, year=2025, dataset="example_dataset", run_id="live-run"
    )
    os.makedirs(live_dir)
    sentinel = os.path.join(live_dir, "sentinel.json")
    with open(sentinel, "w", encoding="utf-8") as f:
      f.write("{}")
    artifact_io.write_run_pointer(
        output_dir, year=2025, dataset="example_dataset", run_id="live-run"
    )
    with flagsaver.flagsaver(run_id="live-run"):
      with self.assertRaisesRegex(ValueError, "'live-run' already exists"):
        extract_pipeline_main.run_pipeline(
            custom_options=PipelineOptions(["--runner=DirectRunner"])
        )
    self.assertEqual(os.listdir(live_dir), ["sentinel.json"])
    self.assertEqual(
        artifact_io.read_run_pointer(output_dir, 2025, "example_dataset"),
        "live-run",
    )

  @parameterized.parameters(
      "population_path", "ghg_emissions_path", "country_stats_path"
  )
  def test_require_baseline_source_accepts_any_single_source(self, flag_name):
    flag_values = {
        "population_path": "",
        "ghg_emissions_path": "",
        "country_stats_path": "",
    }
    flag_values[flag_name] = "/data/ref/baseline"
    with flagsaver.flagsaver(**flag_values):
      common_flags.require_baseline_source()

  @parameterized.parameters("", "   ")
  def test_require_baseline_source_rejects_unset_or_blank_flags(self, value):
    with flagsaver.flagsaver(
        population_path=value,
        ghg_emissions_path=value,
        country_stats_path=value,
    ):
      with self.assertRaisesRegex(
          ValueError, "No country baseline source configured"
      ):
        common_flags.require_baseline_source()

  def test_run_pipeline_rejects_missing_baseline_before_reading_inputs(self):
    output_dir = os.path.join(self.test_dir, "output_no_baseline")
    self._configure_run(self.parquet_path, output_dir)
    loaders = test_fixtures.forbid_reference_data_reads(self)
    with flagsaver.flagsaver(population_path="", ghg_emissions_path=""):
      with self.assertRaisesRegex(
          ValueError, "No country baseline source configured"
      ):
        extract_pipeline_main.run_pipeline(
            custom_options=PipelineOptions(["--runner=DirectRunner"])
        )
    test_fixtures.assert_loaders_not_called(loaders)
    self.assertFalse(os.path.exists(output_dir))

  @parameterized.named_parameters(
      ("glob", "*.parquet"),
      ("directory", ""),
  )
  def test_run_pipeline_rejects_empty_segments_before_reading_inputs(
      self, suffix
  ):
    empty_dir = self.create_tempdir().full_path
    segments_path = os.path.join(empty_dir, suffix) if suffix else empty_dir
    output_dir = os.path.join(self.test_dir, "output_no_segments")
    self._configure_run(segments_path, output_dir)
    loaders = test_fixtures.forbid_reference_data_reads(self)
    with self.assertRaisesRegex(ValueError, "matched no parquet files"):
      extract_pipeline_main.run_pipeline(
          custom_options=PipelineOptions(["--runner=DirectRunner"])
      )
    test_fixtures.assert_loaders_not_called(loaders)
    self.assertFalse(os.path.exists(output_dir))

  @parameterized.parameters(
      ("/data", "/data"),
      ("/data/", "/data"),
      ("/data//", "/data/./"),
      (" /data ", "/data"),
      ("/var/x/data", "/var/x/other/../data"),
  )
  def test_require_distinct_prefixes_rejects_equal_roots(
      self, intermediate_root, output_root
  ):
    with self.assertRaisesRegex(ValueError, "must name different roots"):
      common_flags.require_distinct_prefixes(
          intermediate_root=intermediate_root, output_root=output_root
      )

  @parameterized.parameters(
      ("", "/data"),
      ("   ", "/data"),
      ("/data/intermediate", "/data"),
      ("/data", "/data/published"),
      ("/data-a", "/data-b"),
  )
  def test_require_distinct_prefixes_accepts_distinct_or_disabled_roots(
      self, intermediate_root, output_root
  ):
    common_flags.require_distinct_prefixes(
        intermediate_root=intermediate_root, output_root=output_root
    )

  def test_run_pipeline_rejects_equal_prefixes_before_any_io(self):
    output_dir = self.create_tempdir().full_path
    self._configure_run(self.parquet_path, output_dir)
    with flagsaver.flagsaver(intermediate_prefix=output_dir + "/"):
      with self.assertRaisesRegex(ValueError, "must name different roots"):
        extract_pipeline_main.run_pipeline(
            custom_options=PipelineOptions(["--runner=DirectRunner"])
        )
    self.assertEmpty(os.listdir(output_dir))

  def _make_mixed_input_dir(self) -> str:
    """Builds an input directory with one good and one unreadable shard."""
    input_dir = os.path.join(self.test_dir, "mixed_input")
    os.makedirs(input_dir, exist_ok=True)
    shutil.copy(self.parquet_path, os.path.join(input_dir, "good.parquet"))
    with open(
        os.path.join(input_dir, "corrupt.parquet"), "wb"
    ) as f:
      f.write(b"PAR1 this is not a parquet file")
    return input_dir

  def _configure_run(self, segments_path: str, output_dir: str) -> None:
    FLAGS.input_year = "2025"
    FLAGS.dataset = "example_dataset"
    FLAGS.segments_path = segments_path
    FLAGS.output_prefix = output_dir
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

  def test_end_to_end_pipeline_fails_when_a_shard_cannot_be_read(self):
    # An unreadable shard must stop the run, not shrink the published data:
    # before the failed-shard check, a corrupt file was swallowed and the run
    # exited successfully with a dataset quietly missing half its input.
    output_dir = os.path.join(self.test_dir, "output_corrupt")
    intermediate_dir = os.path.join(self.test_dir, "intermediate_corrupt")
    run_id = "2026-01-02T03-04-05Z-corrupt"
    self._configure_run(self._make_mixed_input_dir(), output_dir)
    FLAGS.intermediate_prefix = intermediate_dir
    FLAGS.run_id = run_id

    with self.assertRaisesRegex(Exception, "shards failed to process"):
      extract_pipeline_main.run_pipeline(
          custom_options=PipelineOptions(["--runner=DirectRunner"])
      )

    # The pointer flip is gated on the threshold check, so nothing was
    # published for readers to follow.
    self.assertIsNone(
        artifact_io.read_run_pointer(output_dir, 2025, "example_dataset")
    )
    # Nor was the manifest written, so derive cannot revive the run.
    run_root = intermediate_io.dataset_root(
        intermediate_dir, 2025, "example_dataset"
    )
    self.assertFalse(intermediate_io.manifest_exists(run_root, run_id))
    FLAGS.output_prefix = os.path.join(self.test_dir, "rederived_corrupt")
    with self.assertRaisesRegex(ValueError, "has no manifest"):
      derive_main.run_derivation()
    self.assertIsNone(
        artifact_io.read_run_pointer(
            FLAGS.output_prefix, 2025, "example_dataset"
        )
    )

  def test_end_to_end_pipeline_tolerates_failures_within_the_budget(self):
    output_dir = os.path.join(self.test_dir, "output_budgeted")
    intermediate_dir = os.path.join(self.test_dir, "intermediate_budgeted")
    run_id = "2026-01-02T03-04-05Z-budgeted"
    self._configure_run(self._make_mixed_input_dir(), output_dir)
    FLAGS.intermediate_prefix = intermediate_dir
    FLAGS.run_id = run_id
    FLAGS.max_failed_shards = 1
    extract_pipeline_main.run_pipeline(
        custom_options=PipelineOptions(["--runner=DirectRunner"])
    )

    self.assertIsNotNone(
        artifact_io.read_run_pointer(output_dir, 2025, "example_dataset")
    )
    run_root = intermediate_io.dataset_root(
        intermediate_dir, 2025, "example_dataset"
    )
    self.assertEqual(
        intermediate_io.read_manifest(run_root, run_id).failed_shard_count, 1
    )

  def test_end_to_end_pipeline_refuses_to_publish_when_no_row_is_in_year(self):
    # A year mismatch drops every row; that must not publish an empty run.
    output_dir = os.path.join(self.test_dir, "output_wrong_year")
    self._configure_run(self.parquet_path, output_dir)
    FLAGS.input_year = "2024"  # The fixture rows are all from 2025.

    with self.assertRaisesRegex(Exception, "No country or FIR aggregates"):
      extract_pipeline_main.run_pipeline(
          custom_options=PipelineOptions(["--runner=DirectRunner"])
      )

    self.assertIsNone(
        artifact_io.read_run_pointer(output_dir, 2024, "example_dataset")
    )

  def test_grid_config_from_flags_validates_lod_divisibility(self):
    self._configure_run(self.parquet_path, os.path.join(self.test_dir, "out"))
    FLAGS.grid_width = 3602
    FLAGS.grid_height = 1802
    with self.assertRaisesRegex(
        ValueError, "for LOD generation must be divisible by 4"
    ):
      extract_pipeline_main.ExtractConfig.from_flags()

  def _make_base_extract_config(self) -> extract_pipeline_main.ExtractConfig:
    return extract_pipeline_main.ExtractConfig(
        dataset="example_dataset",
        year=2025,
        output_prefix="/out",
        segments_path="/segments/*.pq",
        airports_path="/airports.csv",
        country_geojson_path="/countries.geojson",
        firs_path="/firs.geojson",
        grid_config=grid_rasterizer.GridConfig(
            min_lon=-180.0,
            max_lon=180.0,
            min_lat=-85.0,
            max_lat=85.0,
            width=40,
            height=20,
            step_size_meters=10000.0,
        ),
        population_path="/pop.csv",
    )

  @parameterized.named_parameters(
      ("empty_dataset", "dataset", "", "dataset is required"),
      ("invalid_dataset", "dataset", "a/b", "is invalid"),
      ("negative_year", "year", -1, "is invalid"),
      (
          "blank_output_prefix",
          "output_prefix",
          "  ",
          "output_prefix is required",
      ),
      (
          "blank_segments_path",
          "segments_path",
          "  ",
          "segments_path is required",
      ),
      (
          "blank_airports_path",
          "airports_path",
          "  ",
          "airports_path is required",
      ),
      (
          "blank_country_geojson_path",
          "country_geojson_path",
          "  ",
          "country_geojson_path is required",
      ),
      ("blank_firs_path", "firs_path", "  ", "firs_path is required"),
      (
          "missing_baseline_source",
          "population_path",
          "",
          "No country baseline source configured",
      ),
      ("invalid_run_id", "run_id", "..", "--run_id=.* is invalid"),
      (
          "invalid_publish_run_id",
          "publish_run_id",
          "/escape",
          "--publish_run_id=.* is invalid",
      ),
      (
          "negative_max_rows_per_chunk",
          "max_rows_per_chunk",
          -1,
          "max_rows_per_chunk must be non-negative",
      ),
      (
          "non_divisible_lod_grid",
          "grid_config",
          grid_rasterizer.GridConfig(
              min_lon=-180.0,
              max_lon=180.0,
              min_lat=-85.0,
              max_lat=85.0,
              width=3602,
              height=1802,
              step_size_meters=10000.0,
          ),
          "for LOD generation must be divisible by 4",
      ),
  )
  def test_extract_config_rejects_invalid_fields(
      self, field_name, field_value, expected_error
  ):
    base_cfg = self._make_base_extract_config()
    with self.assertRaisesRegex(ValueError, expected_error):
      dataclasses.replace(base_cfg, **{field_name: field_value})

  def test_extract_config_allows_zero_max_rows_per_chunk(self):
    base_cfg = self._make_base_extract_config()
    zero_chunk_cfg = dataclasses.replace(base_cfg, max_rows_per_chunk=0)
    self.assertEqual(zero_chunk_cfg.max_rows_per_chunk, 0)

  def test_extract_config_strips_whitespace_from_string_fields(self):
    base_cfg = self._make_base_extract_config()
    padded_cfg = dataclasses.replace(
        base_cfg,
        dataset="  example_dataset  ",
        output_prefix="  /out  ",
        run_id="  test-run  ",
        publish_run_id="  test-pub  ",
    )
    self.assertEqual(
        (
            padded_cfg.dataset,
            padded_cfg.output_prefix,
            padded_cfg.run_id,
            padded_cfg.publish_run_id,
        ),
        ("example_dataset", "/out", "test-run", "test-pub"),
    )

  def test_extract_config_from_flags(self):
    output_dir = os.path.join(self.test_dir, "cfg_from_flags")
    self._configure_run(self.parquet_path, output_dir)
    FLAGS.run_id = "test-run-id"
    FLAGS.publish_run_id = "test-pub-id"
    cfg = extract_pipeline_main.ExtractConfig.from_flags()
    self.assertEqual((cfg.dataset, cfg.year), ("example_dataset", 2025))
    self.assertEqual(cfg.output_prefix, output_dir)
    self.assertEqual(cfg.run_id, "test-run-id")
    self.assertEqual(cfg.publish_run_id, "test-pub-id")
    self.assertEqual(cfg.grid_config.width, 40)

  def test_set_up_module_marks_unparsed_flags_for_standalone_runners(self):
    self.addCleanup(FLAGS.mark_as_parsed)
    FLAGS.unparse_flags()
    self.assertFalse(FLAGS.is_parsed())
    setUpModule()
    self._configure_run(self.parquet_path, os.path.join(self.test_dir, "out"))
    self.assertEqual(
        extract_pipeline_main.ExtractConfig.from_flags().year, 2025
    )

  def test_run_pipeline_with_explicit_config(self):
    output_dir = os.path.join(self.test_dir, "output_explicit_config")
    config = extract_pipeline_main.ExtractConfig(
        dataset="example_dataset",
        year=2025,
        output_prefix=output_dir,
        segments_path=self.parquet_path,
        airports_path=self.airports_csv_path,
        country_geojson_path=self.country_geojson_path,
        firs_path=self.firs_geojson_path,
        grid_config=grid_rasterizer.GridConfig(
            min_lon=-180.0,
            max_lon=180.0,
            min_lat=-85.0,
            max_lat=85.0,
            width=40,
            height=20,
            step_size_meters=10000.0,
        ),
        population_path=self.population_path,
        ghg_emissions_path=self.ghg_path,
    )
    options = PipelineOptions(["--runner=DirectRunner"])
    extract_pipeline_main.run_pipeline(config=config, custom_options=options)
    self.assertIsNotNone(
        artifact_io.read_run_pointer(output_dir, 2025, "example_dataset")
    )


def _generate_dense_heavy_elements(
    rng: np.random.Generator,
    cfg: grid_rasterizer.GridConfig,
    slice_fn: Any,
    count: int = 120,
) -> list[Any]:
  elements = []
  for _ in range(count):
    num_cells = int(rng.integers(1, 400))
    elements.append((
        (
            ("density", "impact")[int(rng.integers(2))],
            202500 + int(rng.integers(1, 13)),
            int(rng.integers(0, 3)),
        ),
        slice_fn(
            rng.integers(0, cfg.height, num_cells),
            rng.integers(0, cfg.width, num_cells),
            rng.random(num_cells),
        ),
    ))
  return elements


class CombineGridSlicesToExportKeysTest(absltest.TestCase):
  """Runs the two-stage grid combine subgraph on the DirectRunner."""

  _CFG = grid_rasterizer.GridConfig(
      min_lon=-180.0,
      max_lon=180.0,
      min_lat=-85.0,
      max_lat=85.0,
      width=40,
      height=20,
      step_size_meters=10000.0,
  )

  @staticmethod
  def _slice(rows, cols, values):
    return (
        "sparse",
        np.asarray(rows, dtype=np.int16),
        np.asarray(cols, dtype=np.int16),
        np.asarray(values, dtype=np.float32),
    )

  def _run(self, elements, expected_year=2025):
    height, width = self._CFG.height, self._CFG.width
    # numpy reference: every in-year slice added to its total, monthly and
    # hourly grid. Keys that no slice touches must still exist as zeros.
    expected = {
        k: np.zeros((height, width), dtype=np.float32)
        for k in grid_publish.all_export_keys()
    }
    for (gt, month_val, hour), (_, row_item, cols_arr, cell_values) in elements:
      if month_val >= 100 and month_val // 100 != expected_year:
        continue
      month = month_val % 100 if month_val >= 100 else month_val
      if not (1 <= month <= 12 and 0 <= hour <= 23):
        continue
      for key in ((gt, "total", 0), (gt, "monthly", month),
                  (gt, "hourly", hour)):
        np.add.at(expected[key], (row_item, cols_arr), cell_values)

    def check(actual):
      actual = list(actual)
      keys = [k for k, _ in actual]
      # Exactly one output per export key: no duplicates, none missing.
      if sorted(keys) != sorted(expected):
        raise AssertionError(f"unexpected export keys: {sorted(keys)}")
      for k, grid in actual:
        if grid.shape != (height, width):
          raise AssertionError(f"{k}: shape {grid.shape}")
        np.testing.assert_allclose(
            grid, expected[k], rtol=1e-5, atol=1e-5, err_msg=str(k)
        )

    with TestPipeline(
        options=PipelineOptions(["--runner=DirectRunner"])
    ) as pipeline:
      out = beam_stages.combine_grid_slices_to_export_keys(
          pipeline | beam.Create(elements),
          grid_config=self._CFG,
          expected_year=expected_year,
      )
      beam_testing_util.assert_that(out, check)

  def test_emits_all_74_keys_matching_numpy_reference(self):
    elements = [
        # Same (month, hour) cell under both key forms: must sum together.
        (("density", 202503, 14), self._slice([1, 2], [3, 4], [1.0, 2.0])),
        (("density", 3, 14), self._slice([1], [3], [0.5])),
        (("density", 202503, 15), self._slice([19], [39], [4.0])),
        (("impact", 202512, 0), self._slice([0, 0], [0, 0], [1.0, -0.25])),
    ]
    self._run(elements)

  def test_drops_out_of_year_and_invalid_slices(self):
    elements = [
        (("density", 202501, 1), self._slice([5], [5], [1.0])),
        # Out of year: must not leak into total/monthly/hourly.
        (("density", 202401, 1), self._slice([5], [5], [100.0])),
        # Invalid hour: dropped by the normalizer.
        (("density", 202501, 24), self._slice([6], [6], [100.0])),
    ]
    self._run(elements)

  def test_empty_input_still_emits_all_keys_as_zeros(self):
    self._run([])

  def test_randomized_dense_heavy_input(self):
    elements = _generate_dense_heavy_elements(
        np.random.default_rng(7), self._CFG, self._slice
    )
    self._run(elements)


if __name__ == "__main__":
  absltest.main()
