"""Unit tests for grid_rasterizer."""

from collections.abc import Mapping
import tracemalloc
from typing import Any

from absl import flags
from absl.testing import absltest
from absl.testing import parameterized
import numpy as np

from data_extraction import grid_rasterizer


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


def _sparse_to_dense(
    sparse_grids: Mapping[Any, tuple[np.ndarray, np.ndarray, np.ndarray]],
    cfg: grid_rasterizer.GridConfig,
) -> dict[Any, np.ndarray]:
  """Materializes sparse (rows, cols, vals) tuples into 2D float32 arrays."""
  dense: dict[Any, np.ndarray] = {}
  for key, (nz_rows, nz_cols, nz_vals) in sparse_grids.items():
    grid_2d = np.zeros((cfg.height, cfg.width), dtype=np.float32)
    grid_2d[nz_rows, nz_cols] = nz_vals
    dense[key] = grid_2d
  return dense


def _assert_grid_dicts_allclose(
    actual: Mapping[Any, np.ndarray],
    expected: Mapping[Any, np.ndarray],
    rtol: float = 1e-5,
    atol: float = 1e-5,
) -> None:
  """Asserts that all grids in two mappings match numerically."""
  for key in expected:
    np.testing.assert_allclose(actual[key], expected[key], rtol=rtol, atol=atol)


def _assert_bucket_conserved(
    grid: np.ndarray, expected_sum: float, name: str
) -> None:
  """Asserts that the sum of cells in a grid matches expected_sum."""
  np.testing.assert_allclose(
      float(np.sum(grid)), expected_sum, rtol=1e-5, err_msg=name
  )


def _generate_multi_bucket_segments(
    rng: np.random.Generator, n_segs: int
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
]:
  """Generates synthetic segment coordinate and metadata arrays."""
  xs = rng.uniform(-170.0, 170.0, n_segs).astype(np.float32)
  ys = rng.uniform(-80.0, 80.0, n_segs).astype(np.float32)
  xe = (xs + rng.uniform(-4.0, 4.0, n_segs)).astype(np.float32)
  ye = (ys + rng.uniform(-4.0, 4.0, n_segs)).astype(np.float32)
  ef = rng.uniform(-1000.0, 5000.0, n_segs).astype(np.float32)
  months = rng.choice([202501, 202502, 202512], size=n_segs).astype(np.int32)
  hours = rng.choice([0, 6, 12, 23], size=n_segs).astype(np.int32)
  return xs, ys, xe, ye, ef, months, hours


def _rasterize_isolated_buckets(
    xs: np.ndarray,
    ys: np.ndarray,
    xe: np.ndarray,
    ye: np.ndarray,
    ef: np.ndarray,
    months: np.ndarray,
    hours: np.ndarray,
    cfg: grid_rasterizer.GridConfig,
) -> dict[Any, np.ndarray]:
  """Rasterizes isolated buckets independently as a baseline."""
  expected_dense = {}
  for m_val in (202501, 202502, 202512):
    for h_val in (0, 6, 12, 23):
      mask = (months == m_val) & (hours == h_val)
      if not np.any(mask):
        continue
      isolated = grid_rasterizer.rasterize_segments_arrays(
          xs_all=xs[mask],
          ys_all=ys[mask],
          xe_all=xe[mask],
          ye_all=ye[mask],
          ef_all=ef[mask],
          months_all=months[mask],
          hours_all=hours[mask],
          config=cfg,
      )
      expected_dense.update(_sparse_to_dense(isolated, cfg))
  return expected_dense


def _assert_reconstructed_sparse_matches_dense(
    sparse_grids: Mapping[Any, Any],
    dense_grids: Mapping[Any, np.ndarray],
    height: int,
    width: int,
) -> None:
  """Asserts that sparse coordinate payloads match dense grid arrays."""
  for key, expected_grid in dense_grids.items():
    nz_rows, nz_cols, nz_vals = sparse_grids[key]
    reconstructed = np.zeros((height, width), dtype=np.float32)
    reconstructed[nz_rows, nz_cols] = nz_vals
    np.testing.assert_allclose(
        reconstructed, expected_grid, rtol=1e-6, atol=1e-6
    )


def _generate_bincount_test_segments(
    rng: np.random.Generator, n_segs: int
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
]:
  """Generates test segments for bincount equivalence testing."""
  xs = rng.uniform(-170.0, 170.0, n_segs)
  ys = rng.uniform(-80.0, 80.0, n_segs)
  xe = xs + rng.uniform(-3.0, 3.0, n_segs)
  ye = ys + rng.uniform(-3.0, 3.0, n_segs)
  ef = rng.uniform(0.0, 5000.0, n_segs)
  months = rng.choice([202501], size=n_segs)
  hours = rng.choice([10, 14], size=n_segs)
  return xs, ys, xe, ye, ef, months, hours


def _generate_sub_chunk_segments(
    rng: np.random.Generator, n_segs: int
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
]:
  """Generates test segments for sub-chunk invariance testing."""
  xs = rng.uniform(-15.0, 15.0, n_segs).astype(np.float32)
  ys = rng.uniform(-15.0, 15.0, n_segs).astype(np.float32)
  xe = (xs + rng.uniform(-5.0, 5.0, n_segs)).astype(np.float32)
  ye = (ys + rng.uniform(-5.0, 5.0, n_segs)).astype(np.float32)
  ef = rng.uniform(10.0, 500.0, n_segs).astype(np.float32)
  months = rng.choice([202501, 202502], size=n_segs).astype(np.int32)
  hours = rng.choice([3, 9], size=n_segs).astype(np.int32)
  return xs, ys, xe, ye, ef, months, hours


class GridRasterizerTest(parameterized.TestCase):

  def _make_bincount_cfg(self) -> grid_rasterizer.GridConfig:
    return grid_rasterizer.GridConfig(
        min_lon=-180.0,
        max_lon=180.0,
        min_lat=-90.0,
        max_lat=90.0,
        width=120,
        height=60,
        step_size_meters=10000.0,
    )

  def _make_sub_chunk_cfg(self) -> grid_rasterizer.GridConfig:
    return grid_rasterizer.GridConfig(
        min_lon=-20.0,
        max_lon=20.0,
        min_lat=-20.0,
        max_lat=20.0,
        width=40,
        height=40,
        step_size_meters=25000.0,
    )

  def _make_single_segment_cfg(self) -> grid_rasterizer.GridConfig:
    return grid_rasterizer.GridConfig(
        min_lon=-10.0,
        max_lon=20.0,
        min_lat=40.0,
        max_lat=60.0,
        width=30,
        height=20,
        step_size_meters=10000.0,
    )

  def test_grid_rasterizer_binning_sparse(self):
    cfg = self._make_single_segment_cfg()
    sparse_grids = grid_rasterizer.rasterize_segments_arrays(
        xs_all=np.array([3.0]),
        ys_all=np.array([50.0]),
        xe_all=np.array([5.0]),
        ye_all=np.array([51.0]),
        ef_all=np.array([1000.0]),
        months_all=np.array([202501]),
        hours_all=np.array([13]),
        config=cfg,
    )
    self.assertCountEqual(
        sparse_grids.keys(), [("density", 202501, 13), ("impact", 202501, 13)]
    )

    d_rows, d_cols, d_vals = sparse_grids[("density", 202501, 13)]
    self.assertEqual(d_rows.dtype, np.int16)
    self.assertEqual(d_cols.dtype, np.int16)
    self.assertEqual(d_vals.dtype, np.float32)

    dense_grids = _sparse_to_dense(sparse_grids, cfg)
    density_arr = dense_grids[("density", 202501, 13)]
    impact_arr = dense_grids[("impact", 202501, 13)]
    self.assertEqual(density_arr.shape, (20, 30))
    self.assertAlmostEqual(float(np.sum(density_arr)), 1.0, delta=0.01)
    self.assertAlmostEqual(float(np.sum(impact_arr)), 1000.0, delta=1.0)

  def test_rasterize_segments_arrays_empty_input(self):
    cfg = grid_rasterizer.GridConfig(
        min_lon=-10.0,
        max_lon=20.0,
        min_lat=40.0,
        max_lat=60.0,
        width=30,
        height=20,
        step_size_meters=10000.0,
    )
    empty = np.array([], dtype=np.float32)
    empty_i32 = np.array([], dtype=np.int32)
    self.assertEmpty(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=empty,
            ys_all=empty,
            xe_all=empty,
            ye_all=empty,
            ef_all=empty,
            months_all=empty_i32,
            hours_all=empty_i32,
            config=cfg,
        )
    )

  def test_grid_config_accepts_largest_int16_dimensions(self):
    max_dim = grid_rasterizer.MAX_GRID_DIMENSION
    self.assertEqual(max_dim, 32767)
    cfg = grid_rasterizer.GridConfig(
        min_lon=-180.0,
        max_lon=180.0,
        min_lat=-90.0,
        max_lat=90.0,
        width=max_dim,
        height=max_dim,
        step_size_meters=1000.0,
    )
    self.assertEqual((cfg.width, cfg.height), (max_dim, max_dim))

  @parameterized.named_parameters(
      ("width_too_large", grid_rasterizer.MAX_GRID_DIMENSION + 1, 10, "width"),
      (
          "height_too_large",
          10,
          grid_rasterizer.MAX_GRID_DIMENSION + 1,
          "height",
      ),
      ("width_zero", 0, 10, "width"),
      ("height_negative", 10, -1, "height"),
  )
  def test_grid_config_rejects_dimensions_outside_int16_range(
      self, width, height, name
  ):
    with self.assertRaisesRegex(ValueError, f"Grid {name}=.* out of range"):
      grid_rasterizer.GridConfig(
          min_lon=-180.0,
          max_lon=180.0,
          min_lat=-90.0,
          max_lat=90.0,
          width=width,
          height=height,
          step_size_meters=1000.0,
      )

  @parameterized.named_parameters(
      dict(
          testcase_name="swapped_lon",
          min_lon=20.0,
          max_lon=10.0,
          expected_regex="min_lon .* must be strictly less than max_lon",
      ),
      dict(
          testcase_name="equal_lon",
          min_lon=10.0,
          max_lon=10.0,
          expected_regex="min_lon .* must be strictly less than max_lon",
      ),
      dict(
          testcase_name="swapped_lat",
          min_lat=50.0,
          max_lat=40.0,
          expected_regex="min_lat .* must be strictly less than max_lat",
      ),
      dict(
          testcase_name="equal_lat",
          min_lat=50.0,
          max_lat=50.0,
          expected_regex="min_lat .* must be strictly less than max_lat",
      ),
      dict(
          testcase_name="min_lon_below_180",
          min_lon=-181.0,
          expected_regex="min_lon=.* out of range",
      ),
      dict(
          testcase_name="min_lon_above_180",
          min_lon=185.0,
          expected_regex="min_lon=.* out of range",
      ),
      dict(
          testcase_name="max_lon_below_180",
          max_lon=-185.0,
          expected_regex="max_lon=.* out of range",
      ),
      dict(
          testcase_name="max_lon_above_180",
          max_lon=185.0,
          expected_regex="max_lon=.* out of range",
      ),
      dict(
          testcase_name="min_lat_below_90",
          min_lat=-91.0,
          expected_regex="min_lat=.* out of range",
      ),
      dict(
          testcase_name="min_lat_above_90",
          min_lat=95.0,
          expected_regex="min_lat=.* out of range",
      ),
      dict(
          testcase_name="max_lat_below_90",
          max_lat=-95.0,
          expected_regex="max_lat=.* out of range",
      ),
      dict(
          testcase_name="max_lat_above_90",
          max_lat=95.0,
          expected_regex="max_lat=.* out of range",
      ),
      dict(
          testcase_name="zero_step_size",
          step_size_meters=0.0,
          expected_regex="step_size_meters=.* must be positive",
      ),
      dict(
          testcase_name="negative_step_size",
          step_size_meters=-10.0,
          expected_regex="step_size_meters=.* must be positive",
      ),
  )
  def test_grid_config_rejects_invalid_extents_and_step_size(
      self,
      expected_regex,
      min_lon=-180.0,
      max_lon=180.0,
      min_lat=-90.0,
      max_lat=90.0,
      step_size_meters=1000.0,
  ):
    with self.assertRaisesRegex(ValueError, expected_regex):
      grid_rasterizer.GridConfig(
          min_lon=min_lon,
          max_lon=max_lon,
          min_lat=min_lat,
          max_lat=max_lat,
          width=360,
          height=180,
          step_size_meters=step_size_meters,
      )

  def test_antimeridian_segment_rasterization(self):
    cfg = grid_rasterizer.GridConfig(
        min_lon=-180.0,
        max_lon=180.0,
        min_lat=-85.0,
        max_lat=85.0,
        width=360,
        height=170,
        step_size_meters=10000.0,
    )
    grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=np.array([179.0]),
            ys_all=np.array([10.0]),
            xe_all=np.array([-179.0]),
            ye_all=np.array([10.0]),
            ef_all=np.array([1000.0]),
            months_all=np.array([202501]),
            hours_all=np.array([12]),
            config=cfg,
        ),
        cfg,
    )
    density_grid = grids[("density", 202501, 12)]
    impact_grid = grids[("impact", 202501, 12)]

    col_zero = int((0.0 - (-180.0)) / (360.0 / 360))  # col 180
    self.assertEqual(
        float(np.sum(density_grid[:, col_zero - 50 : col_zero + 50])), 0.0
    )
    self.assertEqual(
        float(np.sum(impact_grid[:, col_zero - 50 : col_zero + 50])), 0.0
    )
    self.assertGreater(float(np.sum(density_grid[:, :5])), 0.0)
    self.assertGreater(float(np.sum(density_grid[:, -5:])), 0.0)
    self.assertAlmostEqual(float(np.sum(impact_grid)), 1000.0, places=3)

  def test_westbound_antimeridian_segment_rasterization(self):
    cfg = grid_rasterizer.GridConfig(
        min_lon=-180.0,
        max_lon=180.0,
        min_lat=-85.0,
        max_lat=85.0,
        width=360,
        height=170,
        step_size_meters=10000.0,
    )
    # Westbound crossing: from -179.0 (179W) to +179.0 (179E) at latitude 10.0
    grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=np.array([-179.0]),
            ys_all=np.array([10.0]),
            xe_all=np.array([179.0]),
            ye_all=np.array([10.0]),
            ef_all=np.array([1000.0]),
            months_all=np.array([202501]),
            hours_all=np.array([12]),
            config=cfg,
        ),
        cfg,
    )
    density_grid = grids[("density", 202501, 12)]
    impact_grid = grids[("impact", 202501, 12)]

    col_zero = int((0.0 - (-180.0)) / (360.0 / 360))  # col 180 (Prime Meridian)
    # Must not smear across the Prime Meridian
    self.assertEqual(
        float(np.sum(density_grid[:, col_zero - 50 : col_zero + 50])), 0.0
    )
    self.assertEqual(
        float(np.sum(impact_grid[:, col_zero - 50 : col_zero + 50])), 0.0
    )
    self.assertGreater(float(np.sum(density_grid[:, :5])), 0.0)
    self.assertGreater(float(np.sum(density_grid[:, -5:])), 0.0)
    self.assertAlmostEqual(float(np.sum(impact_grid)), 1000.0, places=3)

  @parameterized.named_parameters(
      ("eastbound", 179.0, -179.0),
      ("westbound", -179.0, 179.0),
  )
  def test_antimeridian_crossing_density_conservation(self, start_lon, end_lon):
    cfg = grid_rasterizer.GridConfig(
        min_lon=-180.0,
        max_lon=180.0,
        min_lat=-85.0,
        max_lat=85.0,
        width=360,
        height=170,
        step_size_meters=10000.0,
    )
    grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=np.array([start_lon]),
            ys_all=np.array([10.0]),
            xe_all=np.array([end_lon]),
            ye_all=np.array([10.0]),
            ef_all=np.array([1000.0]),
            months_all=np.array([202501]),
            hours_all=np.array([12]),
            config=cfg,
        ),
        cfg,
    )
    density_grid = grids[("density", 202501, 12)]
    # A single flight segment must contribute total density of ~1.0, not 2.0
    self.assertAlmostEqual(
        float(np.sum(density_grid)),
        1.0,
        delta=0.02,
    )

  def test_prime_meridian_crossing_segment_rasterization(self):
    cfg = grid_rasterizer.GridConfig(
        min_lon=-180.0,
        max_lon=180.0,
        min_lat=-85.0,
        max_lat=85.0,
        width=360,
        height=170,
        step_size_meters=10000.0,
    )
    # Crossing Prime Meridian: from -2.0W to +2.0E at latitude 50.0N
    grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=np.array([-2.0]),
            ys_all=np.array([50.0]),
            xe_all=np.array([2.0]),
            ye_all=np.array([50.0]),
            ef_all=np.array([500.0]),
            months_all=np.array([202501]),
            hours_all=np.array([12]),
            config=cfg,
        ),
        cfg,
    )
    density_grid = grids[("density", 202501, 12)]
    impact_grid = grids[("impact", 202501, 12)]
    col_zero = 180  # longitude 0.0 is column 180

    # Impact and density should be concentrated around col 180
    self.assertGreater(
        float(np.sum(impact_grid[:, col_zero - 4 : col_zero + 4])), 0.0
    )
    # Remote columns (e.g. far east/west) must be 0
    self.assertEqual(float(np.sum(impact_grid[:, :100])), 0.0)
    self.assertEqual(float(np.sum(impact_grid[:, 260:])), 0.0)
    self.assertAlmostEqual(float(np.sum(density_grid)), 1.0, delta=0.02)
    self.assertAlmostEqual(float(np.sum(impact_grid)), 500.0, places=3)

  def test_equator_crossing_segment_rasterization(self):
    cfg = grid_rasterizer.GridConfig(
        min_lon=-180.0,
        max_lon=180.0,
        min_lat=-85.0,
        max_lat=85.0,
        width=360,
        height=170,
        step_size_meters=10000.0,
    )
    # Crossing Equator: from latitude -3.0S to +3.0N at longitude 10.0E
    grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=np.array([10.0]),
            ys_all=np.array([-3.0]),
            xe_all=np.array([10.0]),
            ye_all=np.array([3.0]),
            ef_all=np.array([800.0]),
            months_all=np.array([202501]),
            hours_all=np.array([12]),
            config=cfg,
        ),
        cfg,
    )
    density_grid = grids[("density", 202501, 12)]
    impact_grid = grids[("impact", 202501, 12)]
    row_equator = 85  # latitude 0.0 is row 85 (-85 to +85)

    # Both Northern (>= row 85) and Southern (< row 85) rows must receive impact
    self.assertGreater(float(np.sum(impact_grid[:row_equator, :])), 0.0)
    self.assertGreater(float(np.sum(impact_grid[row_equator:, :])), 0.0)
    self.assertAlmostEqual(float(np.sum(density_grid)), 1.0, delta=0.02)
    self.assertAlmostEqual(float(np.sum(impact_grid)), 800.0, places=3)

  def _assert_hour_bucket_matches(
      self,
      grids: Mapping[Any, np.ndarray],
      ef: np.ndarray,
      hours: np.ndarray,
      hour: int,
  ) -> None:
    self.assertEqual(grids[("impact", 202501, hour)].shape, (60, 120))
    _assert_bucket_conserved(
        grids[("impact", 202501, hour)],
        float(np.sum(ef[hours == hour])),
        f"Impact mismatch in bucket {hour}",
    )
    _assert_bucket_conserved(
        grids[("density", 202501, hour)],
        float(np.sum(hours == hour)),
        f"Density mismatch in bucket {hour}",
    )

  def test_grid_rasterizer_bincount_numerical_equivalence(self):
    cfg = self._make_bincount_cfg()
    n_segs = 100
    xs, ys, xe, ye, ef, months, hours = _generate_bincount_test_segments(
        np.random.default_rng(42), n_segs
    )
    bincount_grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=xs,
            ys_all=ys,
            xe_all=xe,
            ye_all=ye,
            ef_all=ef,
            months_all=months,
            hours_all=hours,
            config=cfg,
        ),
        cfg,
    )
    self.assertNotEmpty(bincount_grids)

    self._assert_hour_bucket_matches(bincount_grids, ef, hours, 10)
    self._assert_hour_bucket_matches(bincount_grids, ef, hours, 14)
    total_impact = float(
        np.sum(bincount_grids[("impact", 202501, 10)])
        + np.sum(bincount_grids[("impact", 202501, 14)])
    )
    total_density = float(
        np.sum(bincount_grids[("density", 202501, 10)])
        + np.sum(bincount_grids[("density", 202501, 14)])
    )
    np.testing.assert_allclose(total_impact, float(np.sum(ef)), rtol=1e-5)
    np.testing.assert_allclose(total_density, float(n_segs), rtol=1e-5)

  def test_ref_scaled_abs_max(self):
    grid = np.array([[-5.0, 2.0], [10.0, -15.0]], dtype=np.float32)
    # width=2, height=2 -> max |v| is 15.0
    expected = 15.0 * 4.0 / grid_rasterizer.REF_PIXELS
    self.assertAlmostEqual(
        grid_rasterizer.ref_scaled_abs_max(grid, width=2, height=2), expected
    )

  def test_ref_scaled_abs_max_zero_grid(self):
    grid = np.zeros((4, 4), dtype=np.float32)
    self.assertEqual(
        grid_rasterizer.ref_scaled_abs_max(grid, width=4, height=4), 0.0
    )

  def test_ref_scaled_abs_max_empty_grid_raises(self):
    grid = np.empty((0, 0), dtype=np.float32)
    with self.assertRaisesRegex(
        ValueError, "cannot take the maximum of an empty grid"
    ):
      grid_rasterizer.ref_scaled_abs_max(grid, width=0, height=0)

  @parameterized.named_parameters(
      ("nan", float("nan")),
      ("pos_inf", float("inf")),
      ("neg_inf", float("-inf")),
  )
  def test_ref_scaled_abs_max_non_finite_raises(self, bad_val):
    grid = np.array([[1.0, bad_val]], dtype=np.float32)
    with self.assertRaisesRegex(ValueError, "contains a non-finite value"):
      grid_rasterizer.ref_scaled_abs_max(grid, width=2, height=1)

  def test_downsample_grid_sum(self):
    grid = np.ones((8, 8), dtype=np.float32)
    downsampled = grid_rasterizer.downsample_grid_sum(grid, factor=2)
    self.assertEqual(downsampled.shape, (4, 4))
    # 2x2 blocks of 1.0 sum to 4.0
    self.assertEqual(float(downsampled[0, 0]), 4.0)

  @parameterized.parameters(
      ((7, 8), 2),
      ((8, 7), 2),
      ((10, 10), 4),
      ((3602, 1802), 4),
      ((3601, 1802), 2),
  )
  def test_downsample_grid_sum_rejects_non_divisible_dimensions(
      self, shape, factor
  ):
    grid = np.ones(shape, dtype=np.float32)
    with self.assertRaisesRegex(
        ValueError, "must be divisible by downsample factor"
    ):
      grid_rasterizer.downsample_grid_sum(grid, factor=factor)

  def test_encode_grid_to_float16_log10(self):
    grid = np.ones((10, 10), dtype=np.float32) * 5.0
    grid[0, 1] = -3.0
    encoded = grid_rasterizer.encode_grid_to_float16_log10(grid, 10, 10)
    self.assertEqual(encoded.shape, (10, 10))
    self.assertEqual(encoded.dtype, np.float16)
    self.assertGreater(float(encoded[0, 0]), 0.0)
    self.assertLess(float(encoded[0, 1]), 0.0)
    scale = 100.0 / grid_rasterizer.REF_PIXELS
    expected = np.sign(grid) * np.log10(np.abs(grid) * scale + 1.0)
    np.testing.assert_allclose(encoded, expected, rtol=1e-2)

  def test_multi_month_hour_interleaved_matches_isolated_buckets(self):
    cfg = self._make_bincount_cfg()
    xs, ys, xe, ye, ef, months, hours = _generate_multi_bucket_segments(
        np.random.default_rng(123), 150
    )
    combined_sparse = grid_rasterizer.rasterize_segments_arrays(
        xs_all=xs,
        ys_all=ys,
        xe_all=xe,
        ye_all=ye,
        ef_all=ef,
        months_all=months,
        hours_all=hours,
        config=cfg,
    )
    expected_dense = _rasterize_isolated_buckets(
        xs, ys, xe, ye, ef, months, hours, cfg
    )
    self.assertEqual(combined_sparse.keys(), expected_dense.keys())
    _assert_reconstructed_sparse_matches_dense(
        combined_sparse, expected_dense, cfg.height, cfg.width
    )

  @parameterized.parameters(1, 2, 4, 7)
  def test_sub_chunk_size_invariance(self, chunk_size):
    cfg = self._make_sub_chunk_cfg()
    xs, ys, xe, ye, ef, months, hours = _generate_sub_chunk_segments(
        np.random.default_rng(7), 25
    )
    ref_grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=xs,
            ys_all=ys,
            xe_all=xe,
            ye_all=ye,
            ef_all=ef,
            months_all=months,
            hours_all=hours,
            config=cfg,
            sub_chunk_size=25000,
        ),
        cfg,
    )
    chunked_grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=xs,
            ys_all=ys,
            xe_all=xe,
            ye_all=ye,
            ef_all=ef,
            months_all=months,
            hours_all=hours,
            config=cfg,
            sub_chunk_size=chunk_size,
        ),
        cfg,
    )
    self.assertEqual(chunked_grids.keys(), ref_grids.keys())
    _assert_grid_dicts_allclose(chunked_grids, ref_grids)

  def test_sub_chunk_partial_and_empty_chunks(self):
    cfg = grid_rasterizer.GridConfig(
        min_lon=-20.0,
        max_lon=20.0,
        min_lat=-20.0,
        max_lat=20.0,
        width=40,
        height=40,
        step_size_meters=25000.0,
    )
    # Multi-chunk group where only 1 sub-chunk is inside the bounding box, plus
    # a second group where all sub-chunks are outside the bounding box.
    xs_mixed = np.array([100.0, 105.0, 0.0, 1.0, -100.0, -105.0])
    ys_mixed = np.array([0.0, 0.0, 0.0, 1.0, 0.0, 0.0])
    xe_mixed = np.array([101.0, 106.0, 2.0, 3.0, -99.0, -104.0])
    ye_mixed = np.array([0.0, 0.0, 0.0, 1.0, 0.0, 0.0])
    ef_mixed = np.array([100.0, 100.0, 250.0, 350.0, 100.0, 100.0])
    months_mixed = np.array([202501, 202501, 202501, 202501, 202502, 202502])
    hours_mixed = np.array([5, 5, 5, 5, 8, 8])

    mixed_grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=xs_mixed,
            ys_all=ys_mixed,
            xe_all=xe_mixed,
            ye_all=ye_mixed,
            ef_all=ef_mixed,
            months_all=months_mixed,
            hours_all=hours_mixed,
            config=cfg,
            sub_chunk_size=2,
        ),
        cfg,
    )
    self.assertIn(("density", 202501, 5), mixed_grids)
    self.assertIn(("impact", 202501, 5), mixed_grids)
    self.assertNotIn(("density", 202502, 8), mixed_grids)
    self.assertAlmostEqual(
        float(np.sum(mixed_grids[("density", 202501, 5)])), 2.0, places=4
    )
    self.assertAlmostEqual(
        float(np.sum(mixed_grids[("impact", 202501, 5)])), 600.0, places=3
    )

  def test_full_bbox_clipping_returns_empty(self):
    cfg = grid_rasterizer.GridConfig(
        min_lon=0.0,
        max_lon=10.0,
        min_lat=0.0,
        max_lat=10.0,
        width=10,
        height=10,
        step_size_meters=500000.0,
    )
    outside = grid_rasterizer.rasterize_segments_arrays(
        xs_all=np.array([20.0]),
        ys_all=np.array([20.0]),
        xe_all=np.array([25.0]),
        ye_all=np.array([25.0]),
        ef_all=np.array([100.0]),
        months_all=np.array([202501]),
        hours_all=np.array([1]),
        config=cfg,
    )
    self.assertEmpty(outside)

  def test_partial_bbox_clipping(self):
    cfg = grid_rasterizer.GridConfig(
        min_lon=0.0,
        max_lon=10.0,
        min_lat=0.0,
        max_lat=10.0,
        width=10,
        height=10,
        step_size_meters=500000.0,
    )
    # Segment from x=5.0 to x=14.0 (dx=1.0, step_size_meters large -> 18 points
    # sampled at midpoints from x=5.25 to x=13.75; 10 of 18 points <= 10.0,
    # giving the exact geometric fraction 10/18 = 5/9).
    partial = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=np.array([5.0]),
            ys_all=np.array([5.0]),
            xe_all=np.array([14.0]),
            ye_all=np.array([5.0]),
            ef_all=np.array([1000.0]),
            months_all=np.array([202501]),
            hours_all=np.array([1]),
            config=cfg,
        ),
        cfg,
    )
    self.assertAlmostEqual(
        float(np.sum(partial[("density", 202501, 1)])), 5.0 / 9.0, places=5
    )
    self.assertAlmostEqual(
        float(np.sum(partial[("impact", 202501, 1)])),
        1000.0 * 5.0 / 9.0,
        places=3,
    )

  def test_single_point_upper_boundary_zero_ef_segment(self):
    cfg = grid_rasterizer.GridConfig(
        min_lon=-10.0,
        max_lon=10.0,
        min_lat=-10.0,
        max_lat=10.0,
        width=20,
        height=20,
        step_size_meters=10000.0,
    )
    # Zero-length segment at upper-right boundary (10.0, 10.0) with zero EF.
    grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=np.array([10.0]),
            ys_all=np.array([10.0]),
            xe_all=np.array([10.0]),
            ye_all=np.array([10.0]),
            ef_all=np.array([0.0]),
            months_all=np.array([202501]),
            hours_all=np.array([4]),
            config=cfg,
        ),
        cfg,
    )
    # Hour 4 has non-zero density at (row=19, col=19) but zero impact.
    self.assertIn(("density", 202501, 4), grids)
    self.assertNotIn(("impact", 202501, 4), grids)
    self.assertEqual(float(grids[("density", 202501, 4)][19, 19]), 1.0)

  def test_cancelling_ef_segments_omit_impact_grid(self):
    cfg = grid_rasterizer.GridConfig(
        min_lon=-10.0,
        max_lon=10.0,
        min_lat=-10.0,
        max_lat=10.0,
        width=20,
        height=20,
        step_size_meters=10000.0,
    )
    # Two overlapping segments whose EFs (+200, -200) cancel out to 0.
    grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=np.array([0.0, 0.0]),
            ys_all=np.array([0.0, 0.0]),
            xe_all=np.array([0.0, 0.0]),
            ye_all=np.array([0.0, 0.0]),
            ef_all=np.array([200.0, -200.0]),
            months_all=np.array([202501, 202501]),
            hours_all=np.array([5, 5]),
            config=cfg,
        ),
        cfg,
    )
    self.assertIn(("density", 202501, 5), grids)
    self.assertNotIn(("impact", 202501, 5), grids)
    self.assertEqual(float(grids[("density", 202501, 5)][10, 10]), 2.0)

  def _rasterize_multi_bucket_antimeridian_crossings(
      self,
  ) -> Mapping[Any, np.ndarray]:
    cfg = grid_rasterizer.GridConfig(
        min_lon=-180.0,
        max_lon=180.0,
        min_lat=-85.0,
        max_lat=85.0,
        width=360,
        height=170,
        step_size_meters=10000.0,
    )
    return _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=np.array([179.0, 10.0, -179.0]),
            ys_all=np.array([10.0, 20.0, -15.0]),
            xe_all=np.array([-179.0, 12.0, 179.0]),
            ye_all=np.array([10.0, 20.0, -15.0]),
            ef_all=np.array([1000.0, 400.0, 800.0]),
            months_all=np.array([202501, 202502, 202501]),
            hours_all=np.array([12, 6, 18]),
            config=cfg,
        ),
        cfg,
    )

  def test_antimeridian_crossings_multi_bucket_density_conservation(self):
    grids = self._rasterize_multi_bucket_antimeridian_crossings()
    self.assertAlmostEqual(
        float(np.sum(grids[("density", 202501, 12)])), 1.0, delta=0.02
    )
    self.assertAlmostEqual(
        float(np.sum(grids[("density", 202502, 6)])), 1.0, delta=0.02
    )
    self.assertAlmostEqual(
        float(np.sum(grids[("density", 202501, 18)])), 1.0, delta=0.02
    )

  def test_antimeridian_crossings_multi_bucket_impact_conservation(self):
    grids = self._rasterize_multi_bucket_antimeridian_crossings()
    self.assertAlmostEqual(
        float(np.sum(grids[("impact", 202501, 12)])), 1000.0, places=3
    )
    self.assertAlmostEqual(
        float(np.sum(grids[("impact", 202502, 6)])), 400.0, places=3
    )
    self.assertAlmostEqual(
        float(np.sum(grids[("impact", 202501, 18)])), 800.0, places=3
    )

  def _make_test_parity_values(self) -> np.ndarray:
    values = np.array(
        [
            [0.0, -0.0, 1e-6, -1e-6, 0.25, -0.25],
            [1.0, -1.0, 42.5, -42.5, 1e5, -1e5],
            [1e8, -1e8, 1e12, -1e12, 1e14, -1e14],
        ],
        dtype=np.float32,
    )
    values.flags.writeable = False
    return values

  @parameterized.parameters((1200, 600), (3600, 1800), (7200, 3600))
  def test_encode_float16_log10_numerical_parity_and_non_mutation(
      self, width, height
  ):
    values = self._make_test_parity_values()
    vals_snapshot = values.copy()
    encoded = grid_rasterizer.encode_grid_to_float16_log10(
        values, width=width, height=height
    )
    self.assertEqual(encoded.dtype, np.float16)
    self.assertEqual(encoded.shape, values.shape)
    np.testing.assert_array_equal(values, vals_snapshot)

    scale_f64 = float(width * height) / grid_rasterizer.REF_PIXELS
    scaled_ref = values.astype(np.float64) * scale_f64
    encoded_ref = (
        np.sign(scaled_ref) * np.log10(np.abs(scaled_ref) + 1.0)
    ).astype(np.float16)
    np.testing.assert_allclose(
        encoded.astype(np.float32),
        encoded_ref.astype(np.float32),
        rtol=1e-3,
        atol=1e-3,
    )
    self.assertEqual((float(encoded[0, 0]), float(encoded[0, 1])), (0.0, 0.0))

  def test_encode_float16_log10_peak_memory_bound(self):
    # 1,000 x 1,000 = 1,000,000 cells:
    # float32 = 4 MB, float64 = 8 MB, float16 = 2 MB, bool = 1 MB.
    grid = np.linspace(-1e6, 1e6, 1_000_000, dtype=np.float32).reshape(
        1000, 1000
    )

    # Stopping is a no-op when not tracing, so the cleanup only matters if a
    # measured call raises.
    self.addCleanup(tracemalloc.stop)
    tracemalloc.start()
    tracemalloc.reset_peak()
    encoded = grid_rasterizer.encode_grid_to_float16_log10(grid, 7200, 3600)
    _, encode_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    self.assertEqual(encoded.dtype, np.float16)
    # In-place float32 + bool mask + float16 output uses ~7 MB (<< 24+ MB for
    # multi-buffer float64 temporaries).
    self.assertLess(encode_peak, 10 * 1024 * 1024)

  def test_rasterize_segments_arrays_sanitizes_non_finite_ef_preserving_pixel(
      self,
  ):
    cfg = grid_rasterizer.GridConfig(
        min_lon=-10.0,
        max_lon=20.0,
        min_lat=40.0,
        max_lat=60.0,
        width=30,
        height=20,
        step_size_meters=10000.0,
    )
    # Five co-located segments in the exact same grid cell:
    # NaN, +Inf, -Inf, a float64 value overflowing float32 (1e40), and valid
    # 750.0.
    grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=np.array([3.2, 3.2, 3.2, 3.2, 3.2], dtype=np.float64),
            ys_all=np.array([50.2, 50.2, 50.2, 50.2, 50.2], dtype=np.float64),
            xe_all=np.array([3.3, 3.3, 3.3, 3.3, 3.3], dtype=np.float64),
            ye_all=np.array([50.3, 50.3, 50.3, 50.3, 50.3], dtype=np.float64),
            ef_all=np.array(
                [float("nan"), float("inf"), float("-inf"), 1e40, 750.0],
                dtype=np.float64,
            ),
            months_all=np.array([202501] * 5, dtype=np.int32),
            hours_all=np.array([12] * 5, dtype=np.int32),
            config=cfg,
        ),
        cfg,
    )
    density_grid = grids[("density", 202501, 12)]
    impact_grid = grids[("impact", 202501, 12)]
    self.assertTrue(bool(np.all(np.isfinite(density_grid))))
    self.assertTrue(bool(np.all(np.isfinite(impact_grid))))
    self.assertAlmostEqual(float(np.sum(impact_grid)), 750.0, places=3)

  def test_midpoint_sampling_does_not_skip_cells(self):
    cfg = grid_rasterizer.GridConfig(
        min_lon=0.0,
        max_lon=10.0,
        min_lat=0.0,
        max_lat=10.0,
        width=10,
        height=10,
        step_size_meters=1000000.0,
    )
    # A segment spanning from x=0.9 to x=3.4 across cells 0, 1, 2, 3 (dx=1.0).
    # In older integer-truncation sampling, cell 1 was skipped completely.
    # Midpoint sampling ensures intermediate cell 1 receives non-zero weight.
    grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=np.array([0.9]),
            ys_all=np.array([5.0]),
            xe_all=np.array([3.4]),
            ye_all=np.array([5.0]),
            ef_all=np.array([1000.0]),
            months_all=np.array([202501]),
            hours_all=np.array([12]),
            config=cfg,
        ),
        cfg,
    )
    density_grid = grids[("density", 202501, 12)]
    impact_grid = grids[("impact", 202501, 12)]

    # Cell 1 (col 1, row 5) must receive density and impact.
    self.assertGreater(float(density_grid[5, 1]), 0.0)
    self.assertGreater(float(impact_grid[5, 1]), 0.0)
    self.assertAlmostEqual(float(np.sum(density_grid)), 1.0, places=5)
    self.assertAlmostEqual(float(np.sum(impact_grid)), 1000.0, places=3)

  def test_shared_vertex_not_double_weighted(self):
    cfg = grid_rasterizer.GridConfig(
        min_lon=0.0,
        max_lon=10.0,
        min_lat=0.0,
        max_lat=10.0,
        width=10,
        height=10,
        step_size_meters=1000000.0,
    )
    # Two contiguous flight segments: segment 1 from (0.0, 5.0) to (1.0, 5.0),
    # segment 2 from (1.0, 5.0) to (2.0, 5.0).
    # With dx=1.0, segment 1 is in col 0 [0.0, 1.0) and segment 2 is in col 1
    # [1.0, 2.0). Endpoint sampling previously evaluated the shared vertex
    # (1.0, 5.0) in both segments into col 1, giving col 1 3x the weight of
    # col 0. Midpoint sampling balances the density uniformly to 1.0 in each
    # cell.
    grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=np.array([0.0, 1.0]),
            ys_all=np.array([5.0, 5.0]),
            xe_all=np.array([1.0, 2.0]),
            ye_all=np.array([5.0, 5.0]),
            ef_all=np.array([100.0, 100.0]),
            months_all=np.array([202501, 202501]),
            hours_all=np.array([12, 12]),
            config=cfg,
        ),
        cfg,
    )
    density_grid = grids[("density", 202501, 12)]
    impact_grid = grids[("impact", 202501, 12)]

    np.testing.assert_allclose(density_grid[5, :2], [1.0, 1.0], atol=1e-5)
    np.testing.assert_allclose(impact_grid[5, :2], [100.0, 100.0], atol=1e-3)
    self.assertAlmostEqual(float(np.sum(density_grid)), 2.0, places=5)
    self.assertAlmostEqual(float(np.sum(impact_grid)), 200.0, places=3)

  def test_grid_config_to_dict_and_from_dict_round_trip(self):
    cfg = grid_rasterizer.GridConfig(
        min_lon=-10.0,
        max_lon=20.0,
        min_lat=40.0,
        max_lat=60.0,
        width=30,
        height=20,
        step_size_meters=10000.0,
    )
    serialized = cfg.to_dict()
    self.assertEqual(
        serialized,
        {
            "min_lon": -10.0,
            "max_lon": 20.0,
            "min_lat": 40.0,
            "max_lat": 60.0,
            "width": 30,
            "height": 20,
            "step_size_meters": 10000.0,
        },
    )
    restored = grid_rasterizer.GridConfig.from_dict(serialized)
    self.assertEqual(restored, cfg)

  def test_grid_config_from_dict_raises_on_empty_dict(self):
    with self.assertRaisesRegex(ValueError, "must be non-empty"):
      grid_rasterizer.GridConfig.from_dict({})

  @parameterized.parameters(
      "min_lon",
      "max_lon",
      "min_lat",
      "max_lat",
      "width",
      "height",
      "step_size_meters",
  )
  def test_grid_config_from_dict_raises_on_missing_field(self, missing_field):
    valid_config_data = {
        "min_lon": -10.0,
        "max_lon": 20.0,
        "min_lat": 40.0,
        "max_lat": 60.0,
        "width": 30,
        "height": 20,
        "step_size_meters": 10000.0,
    }
    del valid_config_data[missing_field]
    with self.assertRaisesRegex(ValueError, "missing fields"):
      grid_rasterizer.GridConfig.from_dict(valid_config_data)

  def test_grid_config_from_dict_raises_on_invalid_coords(self):
    valid_config_data = {
        "min_lon": 25.0,
        "max_lon": 20.0,
        "min_lat": 40.0,
        "max_lat": 60.0,
        "width": 30,
        "height": 20,
        "step_size_meters": 10000.0,
    }
    with self.assertRaisesRegex(ValueError, "strictly less than"):
      grid_rasterizer.GridConfig.from_dict(valid_config_data)

  def test_rasterize_antimeridian_crossing_conserves_density_and_ef_weights(
      self,
  ):
    cfg = grid_rasterizer.GridConfig(
        min_lon=-180.0,
        max_lon=180.0,
        min_lat=-85.0,
        max_lat=85.0,
        width=36,
        height=17,
        step_size_meters=500000.0,
    )
    grids = _sparse_to_dense(
        grid_rasterizer.rasterize_segments_arrays(
            xs_all=np.array([170.0, -175.0], dtype=np.float32),
            ys_all=np.array([10.0, -20.0], dtype=np.float32),
            xe_all=np.array([-170.0, 165.0], dtype=np.float32),
            ye_all=np.array([20.0, -10.0], dtype=np.float32),
            ef_all=np.array([400.0, 800.0], dtype=np.float32),
            months_all=np.array([202501, 202501], dtype=np.int32),
            hours_all=np.array([12, 12], dtype=np.int32),
            config=cfg,
        ),
        cfg,
    )
    density = grids[("density", 202501, 12)]
    impact = grids[("impact", 202501, 12)]
    self.assertAlmostEqual(float(np.sum(density)), 2.0, places=5)
    self.assertAlmostEqual(float(np.sum(impact)), 1200.0, places=3)
    self.assertAlmostEqual(float(np.sum(impact[:, :2])), 400.0, places=3)
    self.assertAlmostEqual(float(np.sum(impact[:, -2:])), 800.0, places=3)


if __name__ == "__main__":
  absltest.main()
