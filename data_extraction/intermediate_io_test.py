"""Unit tests for intermediate_io module."""

import os
from unittest import mock

from absl import flags
from absl.testing import absltest
import numpy as np

from common import fs
from data_extraction import grid_rasterizer
from data_extraction import intermediate_io


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


class IntermediateFormatTest(absltest.TestCase):

  def setUp(self):
    super().setUp()
    self.root = self.create_tempdir().full_path
    self.run_id = "2026-01-02T03-04-05Z-deadbee"

  def test_run_id_is_sortable_and_attributable(self):
    run_id = intermediate_io.make_run_id("abc1234")
    self.assertTrue(run_id.endswith("-abc1234"))
    self.assertRegex(run_id, r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-abc1234$")

  def test_run_id_replaces_unsafe_tag_characters(self):
    run_id = intermediate_io.make_run_id("rev/123+dirty café")
    self.assertTrue(run_id.endswith("-rev_123_dirty_caf_"), run_id)
    self.assertRegex(run_id, r"^[A-Za-z0-9_.-]+$")

  def test_run_id_truncates_long_tags_to_128_characters(self):
    run_id = intermediate_io.make_run_id("x" * 300)
    self.assertLen(run_id, 128)
    self.assertTrue(run_id.endswith("-" + "x" * 107), run_id)

  def test_run_id_falls_back_for_an_empty_tag(self):
    self.assertTrue(intermediate_io.make_run_id("").endswith("-unknown"))

  def test_dataset_root(self):
    self.assertEqual(
        intermediate_io.dataset_root("/intermediate", 2025, "example_dataset"),
        "/intermediate/2025/example_dataset",
    )

  def test_run_dir(self):
    self.assertEqual(
        intermediate_io.run_dir(
            "/intermediate/2025/example_dataset", "run-123"
        ),
        "/intermediate/2025/example_dataset/runs/run-123",
    )

  def test_country_row_round_trip(self):
    row = (
        ("Belgium", "BE", 202503, 9),
        (12, 4, 5000.0, 1.5, 2.5, 3.5, 1450.25, 480.75),
    )
    decoded = intermediate_io.decode_country_row(
        intermediate_io.encode_country_row(row)
    )
    self.assertEqual(decoded, row)

  def test_decode_rejects_rows_without_distance_or_hour_fields(self):
    country = dict(
        intermediate_io.encode_country_row((
            ("Belgium", "BE", 202503, 9),
            (12, 4, 5000.0, 1.5, 2.5, 3.5, 1450.25, 480.75),
        ))
    )
    del country["total_flight_distance_km"]
    with self.assertRaisesRegex(KeyError, "total_flight_distance_km"):
      intermediate_io.decode_country_row(country)
    fir = dict(
        intermediate_io.encode_fir_row((
            ("FIR NADI EAST", "NFFF", "FJ", "oceanic", 202507, 3),
            (1, 1, 2.0, 10.0, 5.0),
        ))
    )
    del fir["local_hour"]
    with self.assertRaisesRegex(KeyError, "local_hour"):
      intermediate_io.decode_fir_row(fir)

  def test_fir_row_round_trip(self):
    row = (
        ("FIR MAGADAN EAST", "UHMM", "RU", "oceanic", 202507, 17),
        (9, 3, 1234.5, 890.0, 310.0),
    )
    decoded = intermediate_io.decode_fir_row(
        intermediate_io.encode_fir_row(row)
    )
    self.assertEqual(decoded, row)

  def test_country_agg_file_round_trip(self):
    rows = [
        (
            ("Belgium", "BE", 202503, 9),
            (12, 4, 5000.0, 1.5, 2.5, 3.5, 1200.0, 400.0),
        ),
        (
            ("Germany", "DE", 202503, 10),
            (7, 2, 900.0, 0.5, 0.25, 0.25, 700.0, 200.0),
        ),
    ]
    intermediate_io.write_country_agg(rows, self.root, self.run_id)
    self.assertEqual(
        intermediate_io.read_country_agg(self.root, self.run_id), rows
    )

  def test_fir_agg_file_round_trip(self):
    rows = [
        (
            ("FIR BRUSSELS", "EBBU", "BE", "continental", 202503, 9),
            (12, 4, 5.0, 650.0, 210.0),
        ),
        (
            ("FIR LANGEN", "EDGG", "DE", "continental", 202503, 9),
            (3, 1, 2.0, 180.0, 60.0),
        ),
    ]
    intermediate_io.write_fir_agg(rows, self.root, self.run_id)
    self.assertEqual(intermediate_io.read_fir_agg(self.root, self.run_id), rows)

  def test_manifest_round_trip(self):
    grid_cfg = grid_rasterizer.GridConfig(
        min_lon=-180.0,
        max_lon=180.0,
        min_lat=-90.0,
        max_lat=90.0,
        width=1200,
        height=600,
        step_size_meters=10000.0,
    )
    manifest = intermediate_io.RunManifest(
        run_id=self.run_id,
        failed_shard_count=3,
        input_glob="/data/fake/*.parquet",
        input_file_count=1024,
        grid_config=grid_cfg,
        started_at="2026-01-02T03:04:05Z",
        finished_at="2026-01-02T09:55:00Z",
    )
    self.assertFalse(intermediate_io.manifest_exists(self.root, self.run_id))
    intermediate_io.write_manifest(manifest, self.root)
    self.assertTrue(intermediate_io.manifest_exists(self.root, self.run_id))
    read_back = intermediate_io.read_manifest(self.root, self.run_id)
    self.assertEqual(read_back, manifest)
    self.assertEqual(read_back.failed_shard_count, 3)

  def test_manifest_from_dict_decodes_grid_config_dict(self):
    raw = {
        "run_id": self.run_id,
        "failed_shard_count": 0,
        "schema_version": intermediate_io.SCHEMA_VERSION,
        "grid_config": {
            "min_lon": -180.0,
            "max_lon": 180.0,
            "min_lat": -90.0,
            "max_lat": 90.0,
            "width": 1200,
            "height": 600,
            "step_size_meters": 10000.0,
        },
    }
    manifest = intermediate_io.RunManifest.from_dict(raw)
    self.assertIsInstance(manifest.grid_config, grid_rasterizer.GridConfig)
    self.assertEqual(manifest.grid_config.width, 1200)
    self.assertEqual(manifest.grid_config.height, 600)

  def test_manifest_rejects_foreign_schema_version(self):
    raw = intermediate_io.RunManifest(
        run_id=self.run_id, failed_shard_count=0
    ).to_dict()
    raw["schema_version"] = intermediate_io.SCHEMA_VERSION + 1
    with self.assertRaisesRegex(
        ValueError, r"intermediate schema version \d+ is not readable"
    ):
      intermediate_io.RunManifest.from_dict(raw)

  def test_manifest_rejects_version_without_shard_loss(self):
    # Version 1 manifests did not record shard loss, so a lossy run cannot be
    # told apart from a clean one; they must not be republished.
    raw = {"run_id": self.run_id, "schema_version": 1}
    with self.assertRaisesRegex(ValueError, "schema version 1 is not readable"):
      intermediate_io.RunManifest.from_dict(raw)

  def test_manifest_rejects_missing_failed_shard_count(self):
    raw = intermediate_io.RunManifest(
        run_id=self.run_id, failed_shard_count=0
    ).to_dict()
    del raw["failed_shard_count"]
    with self.assertRaisesRegex(
        ValueError, "missing fields.*failed_shard_count"
    ):
      intermediate_io.RunManifest.from_dict(raw)

  def test_manifest_ignores_unknown_fields(self):
    raw = intermediate_io.RunManifest(
        run_id=self.run_id, failed_shard_count=0
    ).to_dict()
    raw["some_future_annotation"] = "ignored"
    self.assertEqual(
        intermediate_io.RunManifest.from_dict(raw).run_id, self.run_id
    )

  def test_grid_slice_round_trip_sparse_branch(self):
    grid = np.zeros((40, 80), dtype=np.float32)
    grid[3, 7] = 12.5
    grid[20, 60] = -4.25
    path = intermediate_io.write_grid_slice(
        grid,
        root=self.root,
        run_id=self.run_id,
        prefix="monthly_global_impact_grid_03",
    )
    self.assertTrue(
        path.endswith("grid_slices/monthly_global_impact_grid_03.npz")
    )
    read_back = intermediate_io.read_grid_slice(
        self.root, self.run_id, "monthly_global_impact_grid_03"
    )
    np.testing.assert_array_equal(read_back, grid)

  def test_grid_slice_round_trip_dense_branch(self):
    grid = np.ones((20, 20), dtype=np.float32) * 3.5
    intermediate_io.write_grid_slice(
        grid,
        root=self.root,
        run_id=self.run_id,
        prefix="total_global_density_grid",
    )
    read_back = intermediate_io.read_grid_slice(
        self.root, self.run_id, "total_global_density_grid"
    )
    np.testing.assert_array_equal(read_back, grid)

  def test_grid_slice_preserves_shape_of_empty_grid(self):
    grid = np.zeros((5, 9), dtype=np.float32)
    intermediate_io.write_grid_slice(
        grid,
        root=self.root,
        run_id=self.run_id,
        prefix="hourly_global_impact_grid_00",
    )
    read_back = intermediate_io.read_grid_slice(
        self.root, self.run_id, "hourly_global_impact_grid_00"
    )
    self.assertEqual(read_back.shape, (5, 9))
    self.assertEqual(float(read_back.sum()), 0.0)

  def test_grid_slice_exists_reports_missing_slices(self):
    self.assertFalse(
        intermediate_io.grid_slice_exists(
            self.root, self.run_id, "total_global_impact_grid"
        )
    )
    intermediate_io.write_grid_slice(
        np.zeros((4, 4), dtype=np.float32),
        root=self.root,
        run_id=self.run_id,
        prefix="total_global_impact_grid",
    )
    self.assertTrue(
        intermediate_io.grid_slice_exists(
            self.root, self.run_id, "total_global_impact_grid"
        )
    )

  def _make_cancelling_open(self, orig_open):
    """Returns an open_file wrapper that aborts on write."""

    class _AbortOnWriteFile:
      """Simulates a backup worker killed after opening its temp file."""

      def __init__(self, inner):
        self._inner = inner

      def __enter__(self):
        self._inner.__enter__()
        return self

      def __exit__(self, exc_type, exc_val, exc_tb):
        return self._inner.__exit__(exc_type, exc_val, exc_tb)

      def write(self, data):
        del data
        raise RuntimeError("Speculative backup worker cancelled before flush")

    def _cancelling_open(path: str, mode: str = "r"):
      handle = orig_open(path, mode)
      if "w" in mode:
        return _AbortOnWriteFile(handle)
      return handle

    return _cancelling_open

  def test_interrupted_grid_slice_write_preserves_existing_slice(self):
    prefix = "monthly_global_density_grid_05"
    primary_grid = np.zeros((16, 32), dtype=np.float32)
    primary_grid[5, 11] = 42.5
    slice_path = intermediate_io.write_grid_slice(
        primary_grid, root=self.root, run_id=self.run_id, prefix=prefix
    )

    with mock.patch.object(
        fs,
        "open_file",
        side_effect=self._make_cancelling_open(fs.open_file),
        autospec=True,
        spec_set=True,
    ):
      with self.assertRaisesRegex(RuntimeError, "cancelled before flush"):
        intermediate_io.write_grid_slice(
            np.ones((16, 32), dtype=np.float32),
            root=self.root,
            run_id=self.run_id,
            prefix=prefix,
        )

    self.assertGreater(os.path.getsize(slice_path), 0)
    read_back = intermediate_io.read_grid_slice(self.root, self.run_id, prefix)
    np.testing.assert_array_equal(read_back, primary_grid)
    self.assertEqual(os.listdir(os.path.dirname(slice_path)), [f"{prefix}.npz"])


if __name__ == "__main__":
  absltest.main()
