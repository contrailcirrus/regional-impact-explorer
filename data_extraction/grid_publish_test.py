"""Unit tests for grid_publish."""

import gzip
import json
import os

from absl import flags
from absl.testing import absltest
from absl.testing import parameterized
import numpy as np

from common import constants
from data_extraction import grid_publish
from data_extraction import grid_rasterizer


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


class GridPublishTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.test_dir = self.create_tempdir().full_path

  def test_all_export_keys(self):
    keys = grid_publish.all_export_keys()
    # 2 metrics * (1 total + 12 monthly + 24 hourly) = 74 keys
    self.assertLen(keys, 74)
    self.assertIn(("density", "total", 0), keys)
    self.assertIn(("density", "monthly", 1), keys)
    self.assertIn(("density", "hourly", 23), keys)
    self.assertIn(("impact", "total", 0), keys)

  @parameterized.named_parameters(
      ("total_density", "density", "total", 0, "total_global_density_grid"),
      (
          "monthly_impact",
          "impact",
          "monthly",
          3,
          "monthly_global_impact_grid_03",
      ),
      (
          "hourly_density",
          "density",
          "hourly",
          14,
          "hourly_global_density_grid_14",
      ),
  )
  def test_artifact_prefix(self, metric, bucket_type, bucket_val, expected):
    self.assertEqual(
        grid_publish.artifact_prefix(metric, bucket_type, bucket_val),
        expected,
    )

  def test_artifact_prefix_rejects_unknown_bucket(self):
    with self.assertRaisesRegex(ValueError, "unknown bucket type"):
      grid_publish.artifact_prefix("density", "invalid_bucket", 0)

  @parameterized.parameters(
      ("impakt", "total", 0, "unknown grid type"),
      ("impact", "monthly", 0, "out of range for 'monthly'"),
      ("impact", "monthly", 13, "out of range for 'monthly'"),
      ("density", "hourly", 24, "out of range for 'hourly'"),
      ("density", "total", 1, "out of range for 'total'"),
  )
  def test_artifact_prefix_rejects_unknown_grid_type_and_bad_index(
      self, grid_type, bucket_type, bucket_idx, expected_regex
  ):
    with self.assertRaisesRegex(ValueError, expected_regex):
      grid_publish.artifact_prefix(grid_type, bucket_type, bucket_idx)

  def _cfg(self, width=40, height=20):
    return grid_rasterizer.GridConfig(
        min_lon=-180.0,
        max_lon=180.0,
        min_lat=-85.0,
        max_lat=85.0,
        width=width,
        height=height,
        step_size_meters=10000.0,
    )

  def _all_results(self, value_by_type):
    return [
        grid_publish.GridPublishResult(
            key=k,
            prefix=grid_publish.artifact_prefix(*k),
            value_max_ref_scaled=value_by_type[k[0]],
        )
        for k in grid_publish.all_export_keys()
    ]

  def test_publish_grid_artifacts_basic(self):
    cfg = self._cfg()
    grid = np.ones((20, 40), dtype=np.float32)
    grid[3, 5] = -7.0
    key = ("density", "total", 0)

    result = grid_publish.publish_grid_artifacts(
        key, grid, grid_config=cfg, output_dir=self.test_dir
    )
    self.assertEqual(result.key, key)
    self.assertEqual(result.prefix, "total_global_density_grid")
    # |min| wins, and the value is reference-scaled: 7 * 40 * 20 / 1200 / 600.
    self.assertAlmostEqual(
        result.value_max_ref_scaled, 7.0 * 40 * 20 / (1200.0 * 600.0)
    )

    bin_gz_path = os.path.join(
        self.test_dir, "total_global_density_grid.bin.gz"
    )
    with open(bin_gz_path, "rb") as f:
      compressed_bytes = f.read()
    self.assertEqual(compressed_bytes[:2], b"\x1f\x8b")
    self.assertLen(gzip.decompress(compressed_bytes), 40 * 20 * 2)

  def test_publish_grid_artifacts_omits_uncompressed_and_metadata_files(self):
    cfg = self._cfg()
    grid = np.ones((20, 40), dtype=np.float32)
    grid_publish.publish_grid_artifacts(
        ("density", "total", 0),
        grid,
        grid_config=cfg,
        output_dir=self.test_dir,
    )
    bin_gz_path = os.path.join(
        self.test_dir, "total_global_density_grid.bin.gz"
    )
    raw_bin_path = os.path.join(self.test_dir, "total_global_density_grid.bin")
    per_prefix_meta = os.path.join(
        self.test_dir, "total_global_density_grid_meta.json"
    )
    self.assertTrue(os.path.exists(bin_gz_path))
    self.assertFalse(os.path.exists(raw_bin_path))
    self.assertFalse(os.path.exists(per_prefix_meta))
    # The shared metadata needs every grid's maximum, so publishing a single
    # grid must not write it.
    self.assertFalse(
        os.path.exists(os.path.join(self.test_dir, "grid_meta.json"))
    )

  def test_published_max_matches_encoded_max(self):
    # The published maximum is exactly what the UI's log scale must cover:
    # log10(max + 1) equals the largest encoded |value|.
    cfg = self._cfg()
    rng = np.random.default_rng(0)
    grid = rng.normal(scale=1e9, size=(20, 40)).astype(np.float64)
    result = grid_publish.publish_grid_artifacts(
        ("impact", "total", 0), grid, grid_config=cfg, output_dir=self.test_dir
    )
    encoded = grid_rasterizer.encode_grid_to_float16_log10(grid, 40, 20)
    self.assertAlmostEqual(
        float(np.max(np.abs(encoded.astype(np.float64)))),
        np.log10(result.value_max_ref_scaled + 1.0),
        delta=0.01,  # float16 resolution at ~6
    )

  def test_publish_grid_artifacts_rejects_non_finite(self):
    grid = np.ones((20, 40), dtype=np.float32)
    grid[0, 0] = np.nan
    with self.assertRaisesRegex(ValueError, "non-finite"):
      grid_publish.publish_grid_artifacts(
          ("density", "total", 0),
          grid,
          grid_config=self._cfg(),
          output_dir=self.test_dir,
      )

  def test_reduce_value_max_takes_max_per_type(self):
    results = self._all_results({"density": 1.0, "impact": 2.0})
    # Make one hourly density grid the largest.
    index = next(
        i
        for i, item in enumerate(results)
        if item.key == ("density", "hourly", 7)
    )
    results[index] = results[index]._replace(value_max_ref_scaled=9.0)
    self.assertEqual(
        grid_publish.reduce_value_max(results),
        {"density": 9.0, "impact": 2.0},
    )

  def test_reduce_value_max_requires_every_key_once(self):
    results = self._all_results({"density": 1.0, "impact": 2.0})
    with self.assertRaisesRegex(
        ValueError, r"expected exactly one result per 74 export keys"
    ):
      grid_publish.reduce_value_max(results[:-1])
    with self.assertRaisesRegex(
        ValueError, r"expected exactly one result per 74 export keys"
    ):
      grid_publish.reduce_value_max(results + results[:1])

  @parameterized.named_parameters(
      ("nan", float("nan")),
      ("inf", float("inf")),
      ("negative", -1.0),
  )
  def test_reduce_value_max_rejects_invalid_values(self, bad_value):
    with self.assertRaisesRegex(ValueError, r"has invalid maximum"):
      grid_publish.reduce_value_max(
          self._all_results({"density": 1.0, "impact": bad_value})
      )

  def test_write_shared_grid_meta_format_and_dimensions(self):
    grid_publish.write_shared_grid_meta(
        self._cfg(), self.test_dir, {"density": 12.5, "impact": 3e9}
    )
    with open(
        os.path.join(self.test_dir, constants.SHARED_GRID_META_FILENAME),
        "r",
        encoding="utf-8",
    ) as f:
      meta = json.load(f)
    self.assertEqual(meta["grid_name"], "global_grid")
    self.assertEqual(
        (meta["dtype"], meta["encoding"], meta["compression"]),
        ("float16", "signed_log10_ref_scaled", "gzip"),
    )
    self.assertEqual((meta["width"], meta["height"]), (40, 20))
    self.assertIn("lods", meta)
    self.assertEqual(meta["lods"]["2"]["width"], 40)

  def test_write_shared_grid_meta_value_max_and_gwp_factors(self):
    grid_publish.write_shared_grid_meta(
        self._cfg(), self.test_dir, {"density": 12.5, "impact": 3e9}
    )
    with open(
        os.path.join(self.test_dir, constants.SHARED_GRID_META_FILENAME),
        "r",
        encoding="utf-8",
    ) as f:
      meta = json.load(f)
    self.assertEqual(
        meta[grid_publish.VALUE_MAX_META_KEY], {"density": 12.5, "impact": 3e9}
    )
    # The UI converts the MJ impact key to t CO2e with exactly these factors.
    self.assertEqual(
        meta["gwp_tonnes_co2e_per_mj"],
        {
            "gwp20": constants.GWP20_TONNES_PER_MJ,
            "gwp50": constants.GWP50_TONNES_PER_MJ,
            "gwp100": constants.GWP100_TONNES_PER_MJ,
        },
    )

  @parameterized.named_parameters(
      ("empty_dict", {}),
      ("missing_impact", {"density": 1.0}),
      ("extra_key", {"density": 1.0, "impact": 1.0, "other": 1.0}),
      ("nan_impact", {"density": 1.0, "impact": float("nan")}),
      ("negative_density", {"density": -1.0, "impact": 1.0}),
  )
  def test_build_shared_grid_meta_has_no_default_value_max(self, value_max):
    cfg = self._cfg()
    with self.assertRaisesRegex(
        ValueError,
        r"value_max must have exactly the keys|invalid .* maximum",
    ):
      grid_publish.build_shared_grid_meta(cfg, value_max)

  def test_shared_grid_meta_gwp_factors_are_a_copy(self):
    meta = grid_publish.build_shared_grid_meta(
        self._cfg(), {"density": 1.0, "impact": 1.0}
    )
    meta["gwp_tonnes_co2e_per_mj"]["gwp50"] = 0.0
    # Mutating one metadata dict must never alter the module constants.
    self.assertEqual(
        constants.GWP_TONNES_PER_MJ["gwp50"], constants.GWP50_TONNES_PER_MJ
    )

  def test_publish_grid_artifacts_with_lod_pyramid(self):
    # High resolution grid (>= 3600x1800) triggers LOD 1 and LOD 0 export
    cfg = self._cfg(width=3600, height=1800)
    grid = np.ones((1800, 3600), dtype=np.float32)
    key = ("impact", "total", 0)

    grid_publish.publish_grid_artifacts(
        key, grid, grid_config=cfg, output_dir=self.test_dir
    )

    base_bin = os.path.join(self.test_dir, "total_global_impact_grid.bin.gz")
    lod1_bin = os.path.join(
        self.test_dir, "total_global_impact_grid_lod1.bin.gz"
    )
    lod0_bin = os.path.join(
        self.test_dir, "total_global_impact_grid_lod0.bin.gz"
    )
    self.assertTrue(os.path.exists(base_bin))
    self.assertTrue(os.path.exists(lod1_bin))
    self.assertTrue(os.path.exists(lod0_bin))

  def test_build_shared_grid_meta_includes_lod_pyramid_widths(self):
    cfg = self._cfg(width=3600, height=1800)
    meta = grid_publish.build_shared_grid_meta(
        cfg, {"density": 1.0, "impact": 1.0}
    )
    self.assertEqual(meta["lods"]["2"]["width"], 3600)
    self.assertEqual(meta["lods"]["1"]["width"], 1800)
    self.assertEqual(meta["lods"]["0"]["width"], 900)

  def test_lod_levels_returns_expected_pyramid_for_valid_configs(self):
    cfg_valid = self._cfg(width=3600, height=1800)
    self.assertEqual(grid_publish.lod_levels(cfg_valid), ((1, 2), (0, 4)))

    cfg_small = self._cfg(width=800, height=400)
    self.assertEmpty(grid_publish.lod_levels(cfg_small))

  @parameterized.named_parameters(
      ("width_not_divisible_by_4", 3602, 1800),
      ("height_not_divisible_by_4", 3600, 1802),
      ("both_not_divisible_by_4", 3602, 1802),
      ("odd_width", 3601, 1800),
  )
  def test_lod_levels_validates_divisibility(self, width, height):
    bad_cfg = self._cfg(width=width, height=height)
    with self.assertRaisesRegex(
        ValueError, "for LOD generation must be divisible by 4"
    ):
      grid_publish.lod_levels(bad_cfg)

  def test_publish_grid_artifacts_rejects_non_divisible_lod_dimensions(self):
    bad_cfg = self._cfg(width=3602, height=1802)
    grid = np.ones((1802, 3602), dtype=np.float32)
    with self.assertRaisesRegex(
        ValueError, "for LOD generation must be divisible by 4"
    ):
      grid_publish.publish_grid_artifacts(
          ("impact", "total", 0),
          grid,
          grid_config=bad_cfg,
          output_dir=self.test_dir,
      )

  def test_build_shared_grid_meta_rejects_non_divisible_lod_dimensions(self):
    bad_cfg = self._cfg(width=3602, height=1802)
    with self.assertRaisesRegex(
        ValueError, "for LOD generation must be divisible by 4"
    ):
      grid_publish.build_shared_grid_meta(
          bad_cfg, {"density": 1.0, "impact": 1.0}
      )


if __name__ == "__main__":
  absltest.main()
