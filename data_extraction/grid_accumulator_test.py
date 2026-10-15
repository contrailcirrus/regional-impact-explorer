"""Unit tests for grid_accumulator."""

from typing import Any
from absl import flags
from absl.testing import absltest
from absl.testing import parameterized
import numpy as np
from data_extraction import grid_accumulator


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


class GridAccumulatorTest(parameterized.TestCase):

  def test_as_grid_acc_returns_none_and_accumulator_instances_unchanged(self):
    self.assertIsNone(grid_accumulator.as_grid_acc(None))

    box = grid_accumulator.DenseBoundingBox(
        0, 10, 0, 10, np.zeros((10, 10), dtype=np.float32)
    )
    self.assertIs(grid_accumulator.as_grid_acc(box), box)

    sp = grid_accumulator.SparseAccumulator(
        "sparse",
        np.array([1], dtype=np.int16),
        np.array([2], dtype=np.int16),
        np.array([3.0], dtype=np.float32),
        1,
    )
    self.assertIs(grid_accumulator.as_grid_acc(sp), sp)

  def test_as_grid_acc_converts_sparse_tuple_slice(self):
    tuple_slice: Any = (
        "sparse",
        np.array([1], dtype=np.int16),
        np.array([2], dtype=np.int16),
        np.array([3.0], dtype=np.float32),
    )
    converted = grid_accumulator.as_grid_acc(tuple_slice)
    self.assertIsInstance(converted, grid_accumulator.SparseAccumulator)
    self.assertEqual(converted.last_compact_len, 1)

  def test_as_grid_acc_rejects_unsupported_payload(self):
    bad_payload: Any = "unsupported_payload"
    with self.assertRaisesRegex(TypeError, "Unsupported grid payload"):
      grid_accumulator.as_grid_acc(bad_payload)

  @parameterized.named_parameters(
      ("zero_size", 0, 0, 0, 0, (0, 0), True),
      ("inverted_rows", 10, 5, 0, 5, (0, 5), True),
      ("inverted_cols", 0, 5, 10, 5, (5, 0), True),
      ("non_empty", 0, 2, 0, 2, (2, 2), False),
  )
  def test_is_empty_box(
      self, r_min, r_max, c_min, c_max, shape, expected_empty
  ):
    box = grid_accumulator.DenseBoundingBox(
        r_min, r_max, c_min, c_max, np.ones(shape, dtype=np.float32)
    )
    self.assertEqual(grid_accumulator.is_empty_box(box), expected_empty)

  def test_merge_grid_boxes_expands_bounding_box(self):
    box_a = grid_accumulator.DenseBoundingBox(
        0, 2, 0, 2, np.ones((2, 2), dtype=np.float32)
    )
    box_b = grid_accumulator.DenseBoundingBox(
        1, 3, 1, 3, np.ones((2, 2), dtype=np.float32) * 2
    )

    merged = grid_accumulator.merge_grid_boxes(box_a, box_b)
    self.assertEqual(merged.r_min, 0)
    self.assertEqual(merged.r_max, 3)
    self.assertEqual(merged.c_min, 0)
    self.assertEqual(merged.c_max, 3)

  def test_merge_grid_boxes_sums_overlapping_cells(self):
    box_a = grid_accumulator.DenseBoundingBox(
        0, 2, 0, 2, np.ones((2, 2), dtype=np.float32)
    )
    box_b = grid_accumulator.DenseBoundingBox(
        1, 3, 1, 3, np.ones((2, 2), dtype=np.float32) * 2
    )

    merged = grid_accumulator.merge_grid_boxes(box_a, box_b)
    # Overlap cell at (1, 1): 1.0 (from a) + 2.0 (from b) = 3.0
    self.assertEqual(merged.sub_grid[1, 1], 3.0)
    self.assertEqual(merged.sub_grid[0, 0], 1.0)
    self.assertEqual(merged.sub_grid[2, 2], 2.0)

  def test_sparse_to_dense_box_computes_tight_bounds(self):
    rows = np.array([2, 4], dtype=np.int16)
    cols = np.array([3, 5], dtype=np.int16)
    values = np.array([10.0, 20.0], dtype=np.float32)

    box = grid_accumulator.sparse_to_dense_box(rows, cols, values)
    self.assertEqual(box.r_min, 2)
    self.assertEqual(box.r_max, 5)
    self.assertEqual(box.c_min, 3)
    self.assertEqual(box.c_max, 6)

  def test_sparse_to_dense_box_populates_sub_grid(self):
    rows = np.array([2, 4], dtype=np.int16)
    cols = np.array([3, 5], dtype=np.int16)
    values = np.array([10.0, 20.0], dtype=np.float32)

    box = grid_accumulator.sparse_to_dense_box(rows, cols, values)
    self.assertEqual(box.sub_grid[0, 0], 10.0)  # (2 - 2, 3 - 3)
    self.assertEqual(box.sub_grid[2, 2], 20.0)  # (4 - 2, 5 - 3)
    self.assertEqual(box.sub_grid[1, 1], 0.0)

  def test_sparse_to_dense_box_empty(self):
    box = grid_accumulator.sparse_to_dense_box(
        np.empty(0, dtype=np.int16),
        np.empty(0, dtype=np.int16),
        np.empty(0, dtype=np.float32),
    )
    self.assertTrue(grid_accumulator.is_empty_box(box))

  def test_merge_sparse_into_box(self):
    box = grid_accumulator.DenseBoundingBox(
        0, 3, 0, 3, np.ones((3, 3), dtype=np.float32)
    )
    # Point within bounds:
    merged = grid_accumulator.merge_sparse_into_box(
        box,
        nonzero_rows=np.array([1], dtype=np.int16),
        nonzero_cols=np.array([1], dtype=np.int16),
        nonzero_values=np.array([5.0], dtype=np.float32),
    )
    self.assertEqual(merged.sub_grid[1, 1], 6.0)

    # Point out of bounds:
    merged2 = grid_accumulator.merge_sparse_into_box(
        merged,
        nonzero_rows=np.array([4], dtype=np.int16),
        nonzero_cols=np.array([4], dtype=np.int16),
        nonzero_values=np.array([7.0], dtype=np.float32),
    )
    self.assertEqual(merged2.r_max, 5)
    self.assertEqual(merged2.c_max, 5)
    self.assertEqual(merged2.sub_grid[4, 4], 7.0)

  def test_compact_and_maybe_promote_sparse_merges_duplicates(self):
    rows = np.array([1, 1, 2], dtype=np.int16)
    cols = np.array([2, 2, 3], dtype=np.int16)
    values = np.array([1.5, 2.5, 4.0], dtype=np.float32)

    result = grid_accumulator.compact_and_maybe_promote_sparse(
        rows=rows, cols=cols, values=values
    )
    self.assertIsInstance(result, grid_accumulator.SparseAccumulator)
    self.assertLen(result.values, 2)
    np.testing.assert_array_equal(result.rows, [1, 2])
    np.testing.assert_array_equal(result.cols, [2, 3])
    np.testing.assert_allclose(result.values, [4.0, 4.0])

  def test_compact_and_maybe_promote_sparse_promotes_when_exceeding_max_points(
      self,
  ):
    rows = np.array([1, 1, 2], dtype=np.int16)
    cols = np.array([2, 2, 3], dtype=np.int16)
    values = np.array([1.5, 2.5, 4.0], dtype=np.float32)

    dense_res = grid_accumulator.compact_and_maybe_promote_sparse(
        rows=rows, cols=cols, values=values, max_sparse_points=1
    )
    self.assertIsInstance(dense_res, grid_accumulator.DenseBoundingBox)

  def test_merge_sparse_accumulators_promotes_when_exceeding_max_points(self):
    sparse_a = grid_accumulator.SparseAccumulator(
        "sparse",
        np.array([1, 2], dtype=np.int16),
        np.array([1, 2], dtype=np.int16),
        np.array([1.5, 2.5], dtype=np.float32),
        2,
    )
    sparse_b = grid_accumulator.SparseAccumulator(
        "sparse",
        np.array([1, 3], dtype=np.int16),
        np.array([1, 4], dtype=np.int16),
        np.array([3.5, 4.0], dtype=np.float32),
        2,
    )
    merged = grid_accumulator.merge_sparse_accumulators(
        sparse_a, sparse_b, max_uncompacted_delta=2, max_sparse_points=2
    )
    self.assertIsInstance(merged, grid_accumulator.DenseBoundingBox)
    self.assertEqual(
        (merged.r_min, merged.r_max, merged.c_min, merged.c_max), (1, 4, 1, 5)
    )
    self.assertEqual(
        (float(merged.sub_grid[0, 0]), float(merged.sub_grid[2, 3])),
        (5.0, 4.0),
    )

  def test_merge_sparse_accumulators_stays_sparse_within_max_points(self):
    sparse_a = grid_accumulator.SparseAccumulator(
        "sparse",
        np.array([1, 2], dtype=np.int16),
        np.array([1, 2], dtype=np.int16),
        np.array([1.5, 2.5], dtype=np.float32),
        2,
    )
    sparse_b = grid_accumulator.SparseAccumulator(
        "sparse",
        np.array([1, 2], dtype=np.int16),
        np.array([1, 2], dtype=np.int16),
        np.array([3.5, 4.5], dtype=np.float32),
        2,
    )
    merged = grid_accumulator.merge_sparse_accumulators(
        sparse_a, sparse_b, max_uncompacted_delta=2, max_sparse_points=2
    )
    self.assertIsInstance(merged, grid_accumulator.SparseAccumulator)
    self.assertLen(merged.values, 2)
    np.testing.assert_allclose(merged.values, [5.0, 7.0])

  def test_adopt_sparse_accumulator_promotes_when_exceeding_max_points(self):
    sparse = grid_accumulator.SparseAccumulator(
        "sparse",
        np.array([0, 1, 2], dtype=np.int16),
        np.array([0, 1, 2], dtype=np.int16),
        np.array([1.0, 2.0, 3.0], dtype=np.float32),
        3,
    )
    adopted = grid_accumulator.adopt_sparse_accumulator(
        sparse, max_sparse_points=2
    )
    self.assertIsInstance(adopted, grid_accumulator.DenseBoundingBox)
    self.assertEqual(float(adopted.sub_grid[2, 2]), 3.0)

  def test_merge_sparse_accumulators_promotes_immediately_above_last_compact(
      self,
  ):
    sparse_a = grid_accumulator.SparseAccumulator(
        "sparse",
        np.array([0, 1, 2], dtype=np.int16),
        np.array([0, 1, 2], dtype=np.int16),
        np.array([1.0, 2.0, 3.0], dtype=np.float32),
        3,
    )
    sparse_b = grid_accumulator.SparseAccumulator(
        "sparse",
        np.array([0], dtype=np.int16),
        np.array([0], dtype=np.int16),
        np.array([4.0], dtype=np.float32),
        1,
    )
    merged = grid_accumulator.merge_sparse_accumulators(
        sparse_a, sparse_b, max_uncompacted_delta=100, max_sparse_points=2
    )
    self.assertIsInstance(merged, grid_accumulator.DenseBoundingBox)
    self.assertEqual(float(merged.sub_grid[0, 0]), 5.0)

  def test_to_compact_grid_accumulator_preserves_empty_sparse(self):
    sp_empty = grid_accumulator.SparseAccumulator(
        "sparse",
        np.empty(0, dtype=np.int16),
        np.empty(0, dtype=np.int16),
        np.empty(0, dtype=np.float32),
        0,
    )
    compact_empty = grid_accumulator.to_compact_grid_accumulator(sp_empty)
    self.assertIsInstance(compact_empty, grid_accumulator.SparseAccumulator)
    self.assertEmpty(compact_empty.values)

  def test_to_compact_grid_accumulator_preserves_high_density_box(self):
    dense_box = grid_accumulator.DenseBoundingBox(
        0, 2, 0, 2, np.ones((2, 2), dtype=np.float32)
    )
    res_dense = grid_accumulator.to_compact_grid_accumulator(dense_box)
    self.assertIsInstance(res_dense, grid_accumulator.DenseBoundingBox)

  def test_to_compact_grid_accumulator_demotes_low_density_box_to_sparse(self):
    sparse_in_dense = np.zeros((10, 10), dtype=np.float32)
    sparse_in_dense[1, 1] = 5.0
    res_sparse = grid_accumulator.to_compact_grid_accumulator(
        grid_accumulator.DenseBoundingBox(0, 10, 0, 10, sparse_in_dense)
    )
    self.assertIsInstance(res_sparse, grid_accumulator.SparseAccumulator)


if __name__ == "__main__":
  absltest.main()
