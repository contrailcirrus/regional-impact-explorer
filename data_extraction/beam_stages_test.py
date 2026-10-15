"""Unit tests for the Beam DoFns and CombineFns in `beam_stages`."""

from collections.abc import Mapping, Sequence
import json
import os
from typing import Any
from unittest import mock

from absl import flags
from absl.testing import absltest
from absl.testing import parameterized
from apache_beam.metrics import execution as metrics_execution
from apache_beam.runners.worker import statesampler
from apache_beam.utils import counters
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from data_extraction import beam_stages
from data_extraction import grid_accumulator
from data_extraction import grid_publish
from data_extraction import grid_rasterizer
from data_extraction import shard_processor
from data_extraction import test_fixtures

_TEST_GRID_CONFIG = grid_rasterizer.GridConfig(
    min_lon=-180.0,
    max_lon=180.0,
    min_lat=-85.0,
    max_lat=85.0,
    width=800,
    height=400,
    step_size_meters=10000.0,
)


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


def _make_sparse_batch_accs(count: int = 5) -> list[Any]:
  accs = []
  for i in range(count):
    rows_arr = np.array([i, i + 1], dtype=np.int16)
    cols_arr = np.array([i * 2, i * 2 + 1], dtype=np.int16)
    values_arr = np.array([10.0, 20.0], dtype=np.float32)
    accs.append(("sparse", rows_arr, cols_arr, values_arr))
  return accs


def _assert_sparse_batch_output_cells(
    test_case: absltest.TestCase, out: np.ndarray, count: int = 5
) -> None:
  for i in range(count):
    test_case.assertEqual(float(out[i, i * 2]), 10.0)
    test_case.assertEqual(float(out[i + 1, i * 2 + 1]), 20.0)


def _write_native_arrow_parquet(path: str) -> None:
  schema = pa.schema([
      ("start_lon", pa.float64()),
      ("start_lat", pa.float64()),
      ("end_lon", pa.float64()),
      ("end_lat", pa.float64()),
      ("contrail_ef_mj", pa.float64()),
      ("time_start", pa.timestamp("us")),
      ("time_start_tz", pa.int32()),
      ("dep_icao", pa.string()),
      ("arr_icao", pa.string()),
  ])
  timestamps = [
      pd.Timestamp("2025-03-10 08:30:00"),
      pd.Timestamp("2025-07-20 15:45:00"),
      pd.Timestamp("2025-11-05 22:15:00"),
      pd.Timestamp("2025-01-01 00:00:00"),
  ]
  table = pa.table(
      {
          "start_lon": [3.0, 4.0, 8.0, float("nan")],
          "start_lat": [50.0, 50.5, 51.0, 50.0],
          "end_lon": [5.0, 6.0, 10.0, 5.0],
          "end_lat": [51.0, 51.5, 52.0, 51.0],
          "contrail_ef_mj": [5000.0, 7500.0, None, 1000.0],
          "time_start": timestamps,
          "time_start_tz": [1, 2, 0, 0],
          "dep_icao": ["EBBR", "EDDF", None, "EBBR"],
          "arr_icao": ["EDDF", "EBBR", "EBBR", "EDDF"],
      },
      schema=schema,
  )
  pq.write_table(table, path)


def _make_large_base_sparse_accumulator(
    n_base: int = 1_200_000,
) -> tuple[grid_accumulator.SparseAccumulator, np.ndarray, np.ndarray]:
  base_r = (np.arange(n_base, dtype=np.int64) // 7200).astype(np.int16)
  base_c = (np.arange(n_base, dtype=np.int64) % 7200).astype(np.int16)
  base_v = np.ones(n_base, dtype=np.float32)
  accumulator = grid_accumulator.SparseAccumulator(
      "sparse", base_r, base_c, base_v, n_base
  )
  return accumulator, base_r, base_c


def _generate_sparse_shards_and_ground_truth(
    rng: np.random.Generator,
    height: int = 60,
    width: int = 80,
    num_shards: int = 16,
) -> tuple[list[Any], np.ndarray]:
  sparse_shards = [(
      "sparse",
      np.empty(0, dtype=np.int16),
      np.empty(0, dtype=np.int16),
      np.empty(0, dtype=np.float32),
  )]
  for i in range(num_shards):
    n_pts = int(rng.integers(25, 90))
    if i % 2 == 0:
      rows = rng.integers(10, 35, size=n_pts, dtype=np.int16)
      cols = rng.integers(15, 50, size=n_pts, dtype=np.int16)
    else:
      rows = rng.integers(0, height, size=n_pts, dtype=np.int16)
      cols = rng.integers(0, width, size=n_pts, dtype=np.int16)
    values = rng.uniform(-500.0, 1500.0, size=n_pts).astype(np.float32)
    sparse_shards.append(("sparse", rows, cols, values))

  expected_f64 = np.zeros((height, width), dtype=np.float64)
  for _, rows_arr, cols_arr, vals_arr in sparse_shards:
    if vals_arr.size > 0:
      np.add.at(expected_f64, (rows_arr, cols_arr), vals_arr.astype(np.float64))
  return sparse_shards, expected_f64.astype(np.float32)


def _run_pure_sparse_combine(
    shards: Sequence[Any], height: int, width: int, delta: int
) -> tuple[Any, np.ndarray]:
  fn = beam_stages.SumDenseGridCombineFn(
      height=height,
      width=width,
      max_sparse_points=None,
      max_uncompacted_delta=delta,
  )
  acc = fn.create_accumulator()
  for shard in shards:
    acc = fn.add_input(acc, shard)
  return acc, fn.extract_output(acc)


def _run_default_pipeline_combine(
    shards: Sequence[Any], height: int, width: int
) -> tuple[Any, np.ndarray]:
  fn = beam_stages.SumDenseGridCombineFn(
      height=height,
      width=width,
      max_sparse_points=120,
      max_uncompacted_delta=30,
  )
  bundles = [shards[0:4], shards[4:9], shards[9:13], shards[13:]]
  map_accs = []
  for bundle in bundles:
    b_acc = fn.create_accumulator()
    for shard in bundle:
      b_acc = fn.add_input(b_acc, shard)
    map_accs.append(fn.compact(b_acc))
  merged = fn.merge_accumulators(map_accs)
  return merged, fn.extract_output(merged)


def _run_pure_dense_combine(
    shards: Sequence[Any], height: int, width: int
) -> tuple[Any, np.ndarray]:
  fn = beam_stages.SumDenseGridCombineFn(height=height, width=width)
  acc = fn.create_accumulator()
  for _, r_arr, c_arr, v_arr in shards:
    box = grid_accumulator.sparse_to_dense_box(r_arr, c_arr, v_arr)
    acc = fn.add_input(acc, box)
  return acc, fn.extract_output(acc)


class BeamStagesTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
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

  def test_sum_tuple_combine_fn_sums_country_and_fir_records(self):
    fn = beam_stages.SumTupleCombineFn()
    self.assertIsNone(fn.extract_output(fn.create_accumulator()))

    acc1 = fn.add_input(
        fn.create_accumulator(), (1, 1, 100.0, 50.0, 100.0, 50.0, 120.5, 120.5)
    )
    acc2 = fn.add_input(
        fn.create_accumulator(),
        (2, 1, 200.0, 100.0, 200.0, 100.0, 300.25, 150.25),
    )
    out = fn.extract_output(fn.merge_accumulators([acc1, None, acc2]))
    self.assertEqual(out, (3, 2, 300.0, 150.0, 300.0, 150.0, 420.75, 270.75))
    self.assertIsNotNone(out)
    self.assertIsInstance(out[0], int)

    fir_accumulator = fn.add_input(
        fn.create_accumulator(), (1, 1, 50.0, 80.0, 80.0)
    )
    fir_accumulator = fn.add_input(fir_accumulator, (1, 0, 75.0, 60.0, 0.0))
    self.assertEqual(
        fn.extract_output(fir_accumulator), (2, 1, 125.0, 140.0, 80.0)
    )

  def test_sum_tuple_combine_fn_rejects_mismatched_tuple_length(self):
    fn = beam_stages.SumTupleCombineFn()
    acc_f = fn.add_input(fn.create_accumulator(), (1, 1, 50.0, 80.0, 80.0))
    with self.assertRaisesRegex(ValueError, r"zip\(\)"):
      fn.add_input(acc_f, (1, 1, 50.0))

  def test_sum_dense_grid_combine_fn_merges_full_extent_boxes(self):
    grid_fn = beam_stages.SumDenseGridCombineFn()
    g1 = grid_accumulator.DenseBoundingBox(
        0, 4, 0, 4, np.ones((4, 4), np.float32)
    )
    g2 = grid_accumulator.DenseBoundingBox(
        0, 4, 0, 4, np.ones((4, 4), np.float32) * 2.0
    )
    out_g = grid_fn.extract_output(grid_fn.merge_accumulators([g1, g2]))
    np.testing.assert_allclose(
        out_g, np.ones((4, 4), dtype=np.float32) * 3.0
    )

  def test_dense_grid_combiner_with_sub_boxes(self):
    grid_fn = beam_stages.SumDenseGridCombineFn(height=10, width=10)
    b1 = grid_accumulator.DenseBoundingBox(
        1, 4, 2, 5, np.ones((3, 3), dtype=np.float32)
    )
    b2 = grid_accumulator.DenseBoundingBox(
        2, 6, 3, 7, np.ones((4, 4), dtype=np.float32) * 2.0
    )
    accumulator = grid_fn.add_input(None, b1)
    accumulator = grid_fn.add_input(accumulator, b2)
    out = grid_fn.extract_output(accumulator)
    self.assertEqual(out.shape, (10, 10))
    self.assertEqual(float(out[2, 3]), 3.0)
    self.assertEqual(float(out[1, 2]), 1.0)
    self.assertEqual(float(out[5, 6]), 2.0)
    self.assertEqual(float(out[0, 0]), 0.0)

  def test_dense_grid_combiner_with_sparse_slice(self):
    grid_fn = beam_stages.SumDenseGridCombineFn(height=10, width=10)
    sp1 = (
        "sparse",
        np.array([1, 2, 3], dtype=np.int16),
        np.array([2, 3, 4], dtype=np.int16),
        np.array([5.0, 10.0, 15.0], dtype=np.float32),
    )
    accumulator = grid_fn.add_input(None, sp1)
    out1 = grid_fn.extract_output(accumulator)
    self.assertEqual(float(out1[1, 2]), 5.0)
    self.assertEqual(float(out1[2, 3]), 10.0)
    self.assertEqual(float(out1[3, 4]), 15.0)
    self.assertEqual(float(out1[0, 0]), 0.0)

  def test_dense_grid_combiner_with_hybrid_sparse_and_dense_slices(self):
    grid_fn = beam_stages.SumDenseGridCombineFn(height=10, width=10)
    sp1 = (
        "sparse",
        np.array([1, 2, 3], dtype=np.int16),
        np.array([2, 3, 4], dtype=np.int16),
        np.array([5.0, 10.0, 15.0], dtype=np.float32),
    )
    accumulator = grid_fn.add_input(None, sp1)
    dense_box = grid_accumulator.DenseBoundingBox(
        2, 5, 3, 6, np.ones((3, 3), dtype=np.float32) * 2.0
    )
    accumulator = grid_fn.add_input(accumulator, dense_box)
    sp2 = (
        "sparse",
        np.array([2], dtype=np.int16),
        np.array([3], dtype=np.int16),
        np.array([3.0], dtype=np.float32),
    )
    accumulator = grid_fn.add_input(accumulator, sp2)
    out = grid_fn.extract_output(accumulator)
    self.assertEqual(float(out[2, 3]), 15.0)
    self.assertEqual(float(out[1, 2]), 5.0)
    self.assertEqual(float(out[4, 5]), 2.0)

  def test_split_parquet_shard_dofn_small_file(self):
    small_file = os.path.join(self.test_dir, "small.pq")
    table_small = pa.table({
        "lon_start": np.linspace(0, 10, 50),
        "lat_start": np.linspace(40, 50, 50),
        "lon_end": np.linspace(1, 11, 50),
        "lat_end": np.linspace(41, 51, 50),
        "sum_ef_mj": np.ones(50),
    })
    pq.write_table(table_small, small_file)

    splitter = beam_stages.SplitParquetShardDoFn(max_rows_per_chunk=100)
    chunks_small = list(splitter.process(small_file))
    self.assertLen(chunks_small, 1)
    self.assertEqual(chunks_small[0][0], small_file)
    self.assertEqual(chunks_small[0][3], 50)

  def test_split_parquet_shard_dofn_large_file(self):
    large_file = os.path.join(self.test_dir, "large.pq")
    table_large = pa.table({
        "lon_start": np.linspace(0, 10, 250),
        "lat_start": np.linspace(40, 50, 250),
        "lon_end": np.linspace(1, 11, 250),
        "lat_end": np.linspace(41, 51, 250),
        "sum_ef_mj": np.ones(250),
    })
    pq.write_table(table_large, large_file)

    splitter = beam_stages.SplitParquetShardDoFn(max_rows_per_chunk=100)
    chunks_large = list(splitter.process(large_file))
    self.assertLen(chunks_large, 3)
    self.assertEqual(
        [cols_or_chunk[3] for cols_or_chunk in chunks_large], [100, 100, 50]
    )
    self.assertEqual(
        [cols_or_chunk[2] for cols_or_chunk in chunks_large], [0, 100, 200]
    )

  def test_split_parquet_shard_dofn_multi_row_group_real_file(self):
    path = os.path.join(self.test_dir, "multi_rg_real.parquet")
    schema = pa.schema([("sum_ef_mj", pa.float64())])
    with pq.ParquetWriter(path, schema) as writer:
      writer.write_table(pa.table({"sum_ef_mj": np.ones(40)}, schema=schema))
      writer.write_table(pa.table({"sum_ef_mj": np.ones(120)}, schema=schema))
      writer.write_table(pa.table({"sum_ef_mj": np.ones(30)}, schema=schema))

    splitter = beam_stages.SplitParquetShardDoFn(max_rows_per_chunk=50)
    chunks = list(splitter.process(path))
    expected = [
        (path, 0, 0, 40),
        (path, 1, 0, 50),
        (path, 1, 50, 50),
        (path, 1, 100, 20),
        (path, 2, 0, 30),
    ]
    self.assertEqual(chunks, expected)

  def test_split_parquet_shard_dofn_metadata_error_emits_only_fallback(self):
    path = os.path.join(self.test_dir, "multi_rg.pq")
    pq.write_table(pa.table({"sum_ef_mj": np.ones(300)}), path)
    # The second row group's metadata is unreadable, after the first row
    # group was already split into chunks.
    metadata = mock.create_autospec(pq.FileMetaData, instance=True)
    metadata.num_row_groups = 3
    metadata.num_rows = 300
    first_row_group = mock.create_autospec(pq.RowGroupMetaData, instance=True)
    first_row_group.num_rows = 100
    metadata.row_group.side_effect = [
        first_row_group,
        OSError("truncated footer"),
    ]

    splitter = beam_stages.SplitParquetShardDoFn(max_rows_per_chunk=50)
    with mock.patch.object(
        pq, "ParquetFile", autospec=True, spec_set=True
    ) as parquet_file:
      parquet_file.return_value.metadata = metadata
      chunks = list(splitter.process(path))

    # Only the whole-file fallback; no partial chunks that would double count.
    self.assertEqual(chunks, [(path, None, 0, None)])

  def test_merge_sparse_accumulators_small_no_compaction(self):
    rows_1 = np.array([1, 2, 3], dtype=np.int16)
    cols_1 = np.array([10, 20, 30], dtype=np.int16)
    values_1 = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    sp1 = grid_accumulator.SparseAccumulator(
        "sparse", rows_1, cols_1, values_1, len(values_1)
    )
    rows_2 = np.array([1, 4], dtype=np.int16)
    cols_2 = np.array([10, 40], dtype=np.int16)
    values_2 = np.array([5.0, 6.0], dtype=np.float32)
    sp2 = grid_accumulator.SparseAccumulator(
        "sparse", rows_2, cols_2, values_2, len(values_2)
    )
    merged = grid_accumulator.merge_sparse_accumulators(sp1, sp2)
    self.assertEqual(merged[0], "sparse")
    self.assertLen(merged[3], 5)
    np.testing.assert_array_equal(
        merged[1], np.array([1, 2, 3, 1, 4], dtype=np.int16)
    )

  def test_merge_sparse_accumulators_large_triggers_compaction(self):
    r_big1 = np.full(150000, 5, dtype=np.int16)
    c_big1 = np.full(150000, 5, dtype=np.int16)
    v_big1 = np.ones(150000, dtype=np.float32)
    sp_big1 = grid_accumulator.SparseAccumulator(
        "sparse", r_big1, c_big1, v_big1, 100000
    )
    r_big2 = np.full(100000, 5, dtype=np.int16)
    c_big2 = np.full(100000, 5, dtype=np.int16)
    v_big2 = np.full(100000, 2.0, dtype=np.float32)
    sp_big2 = grid_accumulator.SparseAccumulator(
        "sparse", r_big2, c_big2, v_big2, len(v_big2)
    )
    merged_compact = grid_accumulator.merge_sparse_accumulators(
        sp_big1, sp_big2
    )
    self.assertEqual(merged_compact[0], "sparse")
    self.assertLen(merged_compact[3], 1)
    self.assertEqual(merged_compact[1][0], 5)
    self.assertEqual(merged_compact[2][0], 5)
    self.assertAlmostEqual(
        float(merged_compact[3][0]), 150000.0 * 1.0 + 100000.0 * 2.0
    )

  def test_sum_dense_grid_combine_fn_sparse_batch_merge(self):
    grid_fn = beam_stages.SumDenseGridCombineFn(height=20, width=20)
    accs = _make_sparse_batch_accs(5)
    merged = grid_fn.merge_accumulators(accs)
    self.assertIsNotNone(merged)
    self.assertEqual(merged[0], "sparse")
    self.assertLen(merged[3], 10)

    out = grid_fn.extract_output(merged)
    self.assertEqual(out.shape, (20, 20))
    _assert_sparse_batch_output_cells(self, out, 5)

  def test_sum_dense_grid_combine_fn_dense_box_merge(self):
    grid_fn = beam_stages.SumDenseGridCombineFn(height=20, width=20)
    boxes = [
        grid_accumulator.DenseBoundingBox(
            0, 5, 0, 5, np.ones((5, 5), dtype=np.float32) * k
        )
        for k in (1.0, 2.0, 3.0)
    ]
    merged_dense = grid_fn.merge_accumulators(boxes)
    self.assertIsInstance(merged_dense, grid_accumulator.DenseBoundingBox)
    np.testing.assert_array_equal(
        merged_dense.sub_grid, np.ones((5, 5), dtype=np.float32) * 6.0
    )

  @parameterized.named_parameters(
      dict(testcase_name="dict_payload", bad_payload={"unexpected": "dict"}),
      dict(
          testcase_name="unknown_tag_tuple",
          bad_payload=("unknown_tag", 1, 2, 3),
      ),
      dict(testcase_name="short_tuple", bad_payload=(1, 2, 3)),
      dict(testcase_name="empty_tuple", bad_payload=()),
      dict(
          testcase_name="1d_array",
          bad_payload=np.ones((5,), dtype=np.float32),
      ),
  )
  def test_sum_dense_grid_combine_fn_rejects_unrecognized_payload(
      self, bad_payload: Any
  ):
    grid_fn = beam_stages.SumDenseGridCombineFn(height=10, width=10)
    valid_sparse: Any = (
        "sparse",
        np.array([1], dtype=np.int16),
        np.array([2], dtype=np.int16),
        np.array([5.0], dtype=np.float32),
    )
    with self.assertRaisesRegex(TypeError, "Unsupported grid payload"):
      grid_fn.add_input(None, bad_payload)
    with self.assertRaisesRegex(TypeError, "Unsupported grid payload"):
      grid_fn.add_input(valid_sparse, bad_payload)
    with self.assertRaisesRegex(TypeError, "Unsupported grid payload"):
      grid_fn.merge_accumulators([valid_sparse, bad_payload])
    with self.assertRaisesRegex(TypeError, "Unsupported grid payload"):
      grid_fn.extract_output(bad_payload)

  def test_sum_dense_grid_combine_fn_rejects_unrecognized_accumulator(self):
    grid_fn = beam_stages.SumDenseGridCombineFn(height=10, width=10)
    valid_sparse: Any = (
        "sparse",
        np.array([1], dtype=np.int16),
        np.array([2], dtype=np.int16),
        np.array([5.0], dtype=np.float32),
    )
    corrupt_acc: Any = {"corrupt": "acc"}
    with self.assertRaisesRegex(TypeError, "Unsupported grid payload"):
      grid_fn.add_input(corrupt_acc, valid_sparse)

  def test_process_parquet_shard_dofn_pyarrow_native_and_compute(self):
    pq_native_path = os.path.join(self.test_dir, "native_arrow.pq")
    _write_native_arrow_parquet(pq_native_path)

    dofn = beam_stages.ProcessParquetShardDoFn(
        airports_path=self.airports_csv_path,
        firs_path=self.firs_geojson_path,
        grid_config=_TEST_GRID_CONFIG,
        country_geojson_path=self.country_geojson_path,
    )
    dofn.setup()
    outputs = list(dofn.process(pq_native_path))

    country_outputs = [
        output_item.value
        for output_item in outputs
        if output_item.tag
        == beam_stages.ProcessParquetShardDoFn.OUTPUT_TAG_COUNTRY
    ]
    fir_outputs = [
        output_item.value
        for output_item in outputs
        if output_item.tag == beam_stages.ProcessParquetShardDoFn.OUTPUT_TAG_FIR
    ]
    grid_outputs = [
        output_item.value
        for output_item in outputs
        if output_item.tag
        == beam_stages.ProcessParquetShardDoFn.OUTPUT_TAG_GRID
    ]

    self.assertNotEmpty(country_outputs)
    self.assertNotEmpty(fir_outputs)
    self.assertNotEmpty(grid_outputs)

  def test_process_parquet_shard_dofn_with_injected_context(self):
    pq_native_path = os.path.join(self.test_dir, "injected_test.parquet")
    table = pa.Table.from_arrays(
        [
            pa.array([3.0]),
            pa.array([50.0]),
            pa.array([5.0]),
            pa.array([51.0]),
            pa.array([1000.0]),
        ],
        names=["lon_start", "lat_start", "lon_end", "lat_end", "sum_ef_mj"],
    )
    pq.write_table(table, pq_native_path)

    worker_ctx = shard_processor.load_process_worker_context(
        airports_path=self.airports_csv_path,
        firs_path=self.firs_geojson_path,
        country_geojson_path=self.country_geojson_path,
    )
    dofn = beam_stages.ProcessParquetShardDoFn(
        airports_path="",
        firs_path="",
        grid_config=_TEST_GRID_CONFIG,
        worker_context=worker_ctx,
    )
    # setup() should be a no-op when worker_context is pre-injected
    dofn.setup()
    outputs = list(dofn.process(pq_native_path))
    self.assertNotEmpty(outputs)

  def _run_dofn(self, shard_input: object) -> tuple[list[Any], dict[str, int]]:
    """Runs the shard DoFn and captures both its outputs and its counters.

    Beam counters are written through the state sampler of the current
    thread, so a bare MetricsContainer is not enough: the container has to
    be installed as the scoped state of a running sampler.

    Args:
      shard_input: Parquet file path or `ShardWorkItem` passed to
        `ProcessParquetShardDoFn.process`.

    Returns:
      Tuple of (outputs, counters keyed by metric name).
    """
    dofn = beam_stages.ProcessParquetShardDoFn(
        airports_path=self.airports_csv_path,
        firs_path=self.firs_geojson_path,
        grid_config=_TEST_GRID_CONFIG,
        country_geojson_path=self.country_geojson_path,
    )
    dofn.setup()
    container = metrics_execution.MetricsContainer("test_step")
    sampler = statesampler.StateSampler("", counters.CounterFactory())
    statesampler.set_current_tracker(sampler)
    self.addCleanup(statesampler.set_current_tracker, None)
    sampler.start()
    with sampler.scoped_state(
        "test_step", "test_state", metrics_container=container
    ):
      outputs = list(dofn.process(shard_input))
    sampler.stop()
    metric_counters = {
        key.metric.name: value
        for key, value in container.get_cumulative().counters.items()
    }
    return outputs, metric_counters

  def _failed_outputs(self, outputs):
    return [
        output_item.value
        for output_item in outputs
        if output_item.tag
        == beam_stages.ProcessParquetShardDoFn.OUTPUT_TAG_FAILED
    ]

  def test_process_parquet_shard_dofn_reports_an_unreadable_shard(self):
    # A shard that cannot be read must not be indistinguishable from success.
    # The old behaviour returned nothing at all, so a job could lose an
    # arbitrary number of chunks and still exit 0.
    outputs, counters = self._run_dofn("nonexistent_file.parquet")

    failed = self._failed_outputs(outputs)
    self.assertLen(failed, 1)
    self.assertEqual(
        failed[0][:2],
        (
            "nonexistent_file.parquet",
            shard_processor.ShardOutcome.READ_FAILED.value,
        ),
    )
    self.assertEqual(counters.get("shards_read_failed"), 1)
    self.assertEqual(counters.get("shards_failed"), 1)
    self.assertIsNone(counters.get("shards_succeeded"))

  def test_process_parquet_shard_dofn_reports_a_malformed_shard_input(self):
    outputs, counters = self._run_dofn(("only", "two"))

    failed = self._failed_outputs(outputs)
    self.assertLen(failed, 1)
    self.assertEqual(
        failed[0][1], shard_processor.ShardOutcome.MALFORMED_INPUT.value
    )
    self.assertEqual(counters.get("shards_malformed_input"), 1)
    self.assertEqual(counters.get("shards_failed"), 1)

  def test_process_parquet_shard_dofn_reports_an_exception_as_a_failure(self):
    path = os.path.join(self.test_dir, "boom.parquet")
    pq.write_table(
        pa.table({
            "lon_start": [3.0],
            "lat_start": [50.0],
            "lon_end": [5.0],
            "lat_end": [51.0],
        }),
        path,
    )

    with mock.patch.object(
        shard_processor,
        "process_shard",
        side_effect=ValueError("boom"),
        autospec=True,
        spec_set=True,
    ):
      outputs, counters = self._run_dofn(path)

    failed = self._failed_outputs(outputs)
    self.assertLen(failed, 1)
    self.assertEqual(
        failed[0][1], shard_processor.ShardOutcome.EXCEPTION.value
    )
    self.assertEqual(counters.get("shards_failed"), 1)
    # A failure must not leave partially emitted records behind.
    self.assertLen(outputs, 1)

  def test_process_parquet_shard_dofn_does_not_flag_an_empty_shard(self):
    # Zero rows is not a failure and must not trip the failure threshold.
    path = os.path.join(self.test_dir, "empty.parquet")
    pq.write_table(
        pa.table(
            {
                "lon_start": [],
                "lat_start": [],
                "lon_end": [],
                "lat_end": [],
            },
            schema=pa.schema([
                ("lon_start", pa.float64()),
                ("lat_start", pa.float64()),
                ("lon_end", pa.float64()),
                ("lat_end", pa.float64()),
            ]),
        ),
        path,
    )

    outputs, counters = self._run_dofn(path)

    self.assertEmpty(self._failed_outputs(outputs))
    self.assertEqual(counters.get("shards_empty"), 1)
    self.assertIsNone(counters.get("shards_failed"))

  def test_process_parquet_shard_dofn_counts_a_fully_invalid_shard(self):
    # All rows dropped is a data-quality signal, not a read failure.
    path = os.path.join(self.test_dir, "all_invalid.parquet")
    pq.write_table(
        pa.table({
            "lon_start": [float("nan"), float("nan")],
            "lat_start": [50.0, 50.0],
            "lon_end": [5.0, 5.0],
            "lat_end": [51.0, 51.0],
            "time_start": ["2025-06-15T10:00:00", "2025-06-15T10:00:00"],
            "sum_ef_mj": [1.0, 1.0],
        }),
        path,
    )

    outputs, counters = self._run_dofn(path)

    self.assertEmpty(self._failed_outputs(outputs))
    self.assertEqual(counters.get("shards_no_valid_segments"), 1)
    self.assertIsNone(counters.get("shards_failed"))
    # The drop counters must survive a shard with no survivors.
    self.assertEqual(counters.get("invalid_coordinates_dropped"), 2)

  def test_process_parquet_shard_dofn_counts_a_successful_shard(self):
    path = os.path.join(self.test_dir, "good.parquet")
    pq.write_table(
        pa.table({
            "lon_start": [3.0],
            "lat_start": [50.0],
            "lon_end": [5.0],
            "lat_end": [51.0],
            "sum_ef_mj": [1000.0],
            "time_start": ["2025-06-15T10:00:00"],
        }),
        path,
    )

    outputs, counters = self._run_dofn(path)

    self.assertEmpty(self._failed_outputs(outputs))
    self.assertEqual(counters.get("shards_succeeded"), 1)
    self.assertNotEmpty(outputs)

  @parameterized.named_parameters(
      dict(testcase_name="empty_left", empty_left=True),
      dict(testcase_name="empty_right", empty_left=False),
  )
  def test_merge_grid_boxes_with_single_empty_operand(self, empty_left: bool):
    empty_box = grid_accumulator.DenseBoundingBox(
        0, 0, 0, 0, np.zeros((0, 0), dtype=np.float32)
    )
    regional_box = grid_accumulator.DenseBoundingBox(
        2500,
        2700,
        5000,
        5200,
        np.ones((200, 200), dtype=np.float32),
    )
    merged = (
        grid_accumulator.merge_grid_boxes(empty_box, regional_box)
        if empty_left
        else grid_accumulator.merge_grid_boxes(regional_box, empty_box)
    )
    self.assertEqual(merged[:4], (2500, 2700, 5000, 5200))
    self.assertEqual(merged[4].shape, (200, 200))

  def test_merge_grid_boxes_both_empty(self):
    empty_box = grid_accumulator.DenseBoundingBox(
        0, 0, 0, 0, np.zeros((0, 0), dtype=np.float32)
    )
    merged_empty = grid_accumulator.merge_grid_boxes(empty_box, empty_box)
    self.assertEqual(merged_empty[:4], (0, 0, 0, 0))
    self.assertEqual(merged_empty[4].size, 0)

  def test_merge_sparse_into_box_with_empty_box(self):
    empty_box = grid_accumulator.DenseBoundingBox(
        0, 0, 0, 0, np.zeros((0, 0), dtype=np.float32)
    )
    nonzero_rows = np.array([2500, 2600], dtype=np.int16)
    nonzero_cols = np.array([5000, 5100], dtype=np.int16)
    nonzero_values = np.array([1.5, 2.5], dtype=np.float32)

    merged = grid_accumulator.merge_sparse_into_box(
        empty_box,
        nonzero_rows=nonzero_rows,
        nonzero_cols=nonzero_cols,
        nonzero_values=nonzero_values,
    )
    self.assertEqual(merged[:4], (2500, 2601, 5000, 5101))
    self.assertEqual(merged[4].shape, (101, 101))
    self.assertEqual(float(merged[4][0, 0]), 1.5)
    self.assertEqual(float(merged[4][100, 100]), 2.5)

    # Empty points into box returns box unmodified
    empty_pts = np.empty(0, dtype=np.int16)
    empty_vals = np.empty(0, dtype=np.float32)
    unmodified = grid_accumulator.merge_sparse_into_box(
        merged,
        nonzero_rows=empty_pts,
        nonzero_cols=empty_pts,
        nonzero_values=empty_vals,
    )
    self.assertEqual(unmodified[:4], (2500, 2601, 5000, 5101))

  def test_dense_grid_combiner_empty_sparse_seed_does_not_expand_box(self):
    empty_sparse = (
        "sparse",
        np.empty(0, dtype=np.int16),
        np.empty(0, dtype=np.int16),
        np.empty(0, dtype=np.float32),
    )
    grid_fn = beam_stages.SumDenseGridCombineFn(height=3600, width=7200)
    accumulator = grid_fn.add_input(None, empty_sparse)

    regional_box = grid_accumulator.DenseBoundingBox(
        2500,
        2700,
        5000,
        5200,
        np.ones((200, 200), dtype=np.float32) * 4.0,
    )
    accumulator = grid_fn.add_input(accumulator, regional_box)
    self.assertIsInstance(accumulator, grid_accumulator.DenseBoundingBox)
    self.assertEqual(
        (
            accumulator.r_min,
            accumulator.r_max,
            accumulator.c_min,
            accumulator.c_max,
        ),
        (2500, 2700, 5000, 5200),
    )
    self.assertEqual(accumulator.sub_grid.shape, (200, 200))

    out = grid_fn.extract_output(accumulator)
    self.assertEqual(out.shape, (3600, 7200))
    self.assertEqual((float(out[2550, 5050]), float(out[0, 0])), (4.0, 0.0))

  def test_dense_grid_combiner_merge_accumulators_with_empty_operands(self):
    empty_sparse: Any = (
        "sparse",
        np.empty(0, dtype=np.int16),
        np.empty(0, dtype=np.int16),
        np.empty(0, dtype=np.float32),
    )
    empty_dense = grid_accumulator.DenseBoundingBox(
        0, 0, 0, 0, np.zeros((0, 0), dtype=np.float32)
    )
    regional_dense = grid_accumulator.DenseBoundingBox(
        2500,
        2700,
        5000,
        5200,
        np.ones((200, 200), dtype=np.float32) * 7.0,
    )
    grid_fn = beam_stages.SumDenseGridCombineFn(height=3600, width=7200)
    merged = grid_fn.merge_accumulators(
        [empty_sparse, empty_dense, regional_dense]
    )
    self.assertIsInstance(merged, grid_accumulator.DenseBoundingBox)
    self.assertEqual(
        (merged.r_min, merged.r_max, merged.c_min, merged.c_max),
        (2500, 2700, 5000, 5200),
    )
    self.assertEqual(merged.sub_grid.shape, (200, 200))

    out = grid_fn.extract_output(merged)
    self.assertEqual(out.shape, (3600, 7200))
    self.assertEqual((float(out[2600, 5100]), float(out[0, 0])), (7.0, 0.0))

  def test_dense_grid_combiner_all_empty_accumulators_returns_zeros(self):
    grid_fn = beam_stages.SumDenseGridCombineFn(height=100, width=200)
    empty_sparse: Any = (
        "sparse",
        np.empty(0, dtype=np.int16),
        np.empty(0, dtype=np.int16),
        np.empty(0, dtype=np.float32),
    )
    empty_dense = grid_accumulator.DenseBoundingBox(
        0, 0, 0, 0, np.zeros((0, 0), dtype=np.float32)
    )
    merged = grid_fn.merge_accumulators([empty_sparse, empty_dense, None])
    out = grid_fn.extract_output(merged)
    self.assertEqual(out.shape, (100, 200))
    np.testing.assert_array_equal(
        out, np.zeros((100, 200), dtype=np.float32)
    )

  def test_merge_grid_boxes_never_mutates_input_when_acc_contains_input(self):
    # Ownership contract: box_a is the accumulator and MAY be mutated in place;
    # box_b is an input element shared across three export keys and MUST NOT be.
    grid_a = np.ones((10, 10), dtype=np.float32)
    grid_b = np.ones((5, 5), dtype=np.float32) * 2.0
    box_a = grid_accumulator.DenseBoundingBox(0, 10, 0, 10, grid_a)
    box_b = grid_accumulator.DenseBoundingBox(2, 7, 2, 7, grid_b)

    merged = grid_accumulator.merge_grid_boxes(box_a, box_b)
    self.assertIs(merged[4], grid_a)
    np.testing.assert_array_equal(
        grid_b, np.ones((5, 5), dtype=np.float32) * 2.0
    )
    self.assertEqual(float(merged[4][3, 3]), 3.0)

  def test_merge_grid_boxes_never_mutates_input_when_input_contains_acc(self):
    # box_b contains box_a: the union is the input's extent, so a new buffer is
    # allocated rather than writing into the input.
    grid_a2 = np.ones((5, 5), dtype=np.float32) * 2.0
    grid_b2 = np.ones((10, 10), dtype=np.float32)
    merged_rev = grid_accumulator.merge_grid_boxes(
        grid_accumulator.DenseBoundingBox(2, 7, 2, 7, grid_a2),
        grid_accumulator.DenseBoundingBox(0, 10, 0, 10, grid_b2),
    )
    self.assertIsNot(merged_rev[4], grid_b2)
    np.testing.assert_array_equal(grid_b2, np.ones((10, 10), dtype=np.float32))
    self.assertEqual(float(merged_rev[4][3, 3]), 3.0)

  def test_merge_grid_boxes_copies_when_adopting_an_empty_accumulator(self):
    grid_b = np.ones((5, 5), dtype=np.float32) * 2.0
    empty = grid_accumulator.DenseBoundingBox(
        0, 0, 0, 0, np.zeros((0, 0), dtype=np.float32)
    )

    merged = grid_accumulator.merge_grid_boxes(
        empty, grid_accumulator.DenseBoundingBox(2, 7, 2, 7, grid_b)
    )
    self.assertIsNot(merged[4], grid_b)
    merged[4][0, 0] = 99.0
    np.testing.assert_array_equal(
        grid_b, np.ones((5, 5), dtype=np.float32) * 2.0
    )

  def test_merge_sparse_into_box_never_mutates_the_input_points(self):
    grid_2d = np.ones((10, 10), dtype=np.float32)
    box = grid_accumulator.DenseBoundingBox(0, 10, 0, 10, grid_2d)
    nonzero_rows = np.array([2, 3], dtype=np.int16)
    nonzero_cols = np.array([2, 3], dtype=np.int16)
    nonzero_values = np.array([5.0, 5.0], dtype=np.float32)

    merged = grid_accumulator.merge_sparse_into_box(
        box,
        nonzero_rows=nonzero_rows,
        nonzero_cols=nonzero_cols,
        nonzero_values=nonzero_values,
    )
    # The accumulator buffer is reused; only the input points are read-only.
    self.assertIs(merged[4], grid_2d)
    np.testing.assert_array_equal(
        nonzero_values, np.array([5.0, 5.0], dtype=np.float32)
    )
    self.assertEqual(float(merged[4][2, 2]), 6.0)

  def test_sum_dense_grid_combine_fn_add_input_never_mutates_the_input(self):
    grid_fn = beam_stages.SumDenseGridCombineFn()
    acc_grid = np.ones((5, 5), dtype=np.float32)
    val_grid = np.ones((5, 5), dtype=np.float32) * 2.0
    accumulator = grid_accumulator.DenseBoundingBox(0, 5, 0, 5, acc_grid)
    rhs_value = grid_accumulator.DenseBoundingBox(0, 5, 0, 5, val_grid)
    result = grid_fn.add_input(accumulator, rhs_value)
    # The accumulator is accumulated into directly, per the Beam CombineFn
    # contract; the input element is untouched.
    self.assertIsInstance(result, grid_accumulator.DenseBoundingBox)
    self.assertIs(result.sub_grid, acc_grid)
    np.testing.assert_array_equal(
        val_grid, np.ones((5, 5), dtype=np.float32) * 2.0
    )
    np.testing.assert_array_equal(
        result.sub_grid, np.ones((5, 5), dtype=np.float32) * 3.0
    )

  def test_one_payload_fanned_out_to_three_keys_stays_independent(self):
    # beam_stages.FanoutGridExportKeysDoFn yields the SAME payload object to the
    # total, monthly and hourly keys. Because add_input now accumulates in
    # place, adopting that payload without copying would make three
    # accumulators share one buffer and corrupt two unrelated buckets.
    rows = np.array([1, 2], dtype=np.int16)
    cols = np.array([1, 2], dtype=np.int16)
    values = np.array([3.0, 4.0], dtype=np.float32)
    shared_payload = grid_accumulator.SparseAccumulator(
        "sparse", rows, cols, values, len(values)
    )

    grid_fn = beam_stages.SumDenseGridCombineFn(height=4, width=4)
    empty_seed = grid_accumulator.SparseAccumulator(
        "sparse",
        np.empty(0, dtype=np.int16),
        np.empty(0, dtype=np.int16),
        np.empty(0, dtype=np.float32),
        0,
    )

    # Three keys each seeded empty, then each fed the same payload object.
    accs = [grid_fn.add_input(None, empty_seed) for _ in range(3)]
    accs = [
        grid_fn.add_input(acc_item_a, shared_payload) for acc_item_a in accs
    ]

    # Only the first key receives a second contribution.
    accs[0] = grid_fn.add_input(accs[0], shared_payload)

    out = [grid_fn.extract_output(acc_item_a) for acc_item_a in accs]
    self.assertEqual(float(out[0][1, 1]), 6.0)
    self.assertEqual(float(out[1][1, 1]), 3.0)
    self.assertEqual(float(out[2][1, 1]), 3.0)

    # The shared payload itself is unchanged.
    np.testing.assert_array_equal(
        shared_payload.values, np.array([3.0, 4.0], dtype=np.float32)
    )

  def test_dense_accumulators_fanned_out_to_three_keys_stay_independent(self):
    shared_grid = np.ones((4, 4), dtype=np.float32)
    shared_payload = grid_accumulator.DenseBoundingBox(0, 4, 0, 4, shared_grid)

    grid_fn = beam_stages.SumDenseGridCombineFn(height=4, width=4)
    accs = [grid_fn.add_input(None, shared_payload) for _ in range(3)]
    accs[0] = grid_fn.add_input(accs[0], shared_payload)

    out = [grid_fn.extract_output(acc_item_a) for acc_item_a in accs]
    self.assertEqual(float(out[0][0, 0]), 2.0)
    self.assertEqual(float(out[1][0, 0]), 1.0)
    self.assertEqual(float(out[2][0, 0]), 1.0)
    np.testing.assert_array_equal(
        shared_grid, np.ones((4, 4), dtype=np.float32)
    )

  def test_combine_fn_typed_sparse_accumulator(self):
    grid_fn = beam_stages.SumDenseGridCombineFn(height=10, width=10)
    sp_val = (
        "sparse",
        np.array([1, 2], dtype=np.int16),
        np.array([3, 4], dtype=np.int16),
        np.array([5.0, 6.0], dtype=np.float32),
    )
    g_acc = grid_fn.add_input(None, sp_val)
    self.assertIsInstance(g_acc, grid_accumulator.SparseAccumulator)
    self.assertEqual(g_acc.tag, "sparse")
    self.assertEqual(g_acc.last_compact_len, 2)
    np.testing.assert_array_equal(g_acc.rows, np.array([1, 2], dtype=np.int16))

  def test_combine_fn_typed_dense_accumulator(self):
    grid_fn = beam_stages.SumDenseGridCombineFn(height=10, width=10)
    dense_val = grid_accumulator.DenseBoundingBox(
        1, 4, 2, 5, np.ones((3, 3), dtype=np.float32)
    )
    g_dense_acc = grid_fn.add_input(None, dense_val)
    self.assertIsInstance(g_dense_acc, grid_accumulator.DenseBoundingBox)
    self.assertEqual(
        (
            g_dense_acc.r_min,
            g_dense_acc.r_max,
            g_dense_acc.c_min,
            g_dense_acc.c_max,
        ),
        (1, 4, 2, 5),
    )
    self.assertEqual(g_dense_acc.sub_grid.shape, (3, 3))

  def test_merge_sparse_accumulators_defers_compaction_below_threshold(self):
    n_base = 1_200_000
    accumulator, base_r, base_c = _make_large_base_sparse_accumulator(n_base)
    delta_small = 50_000
    small_input = grid_accumulator.SparseAccumulator(
        "sparse",
        base_r[:delta_small],
        base_c[:delta_small],
        np.ones(delta_small, dtype=np.float32),
        delta_small,
    )
    acc_uncompacted = grid_accumulator.merge_sparse_accumulators(
        accumulator, small_input, max_uncompacted_delta=500_000
    )
    self.assertIsInstance(acc_uncompacted, grid_accumulator.SparseAccumulator)
    self.assertLen(acc_uncompacted.values, n_base + delta_small)
    self.assertEqual(acc_uncompacted.last_compact_len, n_base)

  def test_merge_sparse_accumulators_triggers_compaction_at_threshold(self):
    n_base = 1_200_000
    accumulator, base_r, base_c = _make_large_base_sparse_accumulator(n_base)
    delta_small = 50_000
    small_input = grid_accumulator.SparseAccumulator(
        "sparse",
        base_r[:delta_small],
        base_c[:delta_small],
        np.ones(delta_small, dtype=np.float32),
        delta_small,
    )
    acc_uncompacted = grid_accumulator.merge_sparse_accumulators(
        accumulator, small_input, max_uncompacted_delta=500_000
    )
    self.assertIsInstance(acc_uncompacted, grid_accumulator.SparseAccumulator)
    delta_rest = 450_000
    rest_input = grid_accumulator.SparseAccumulator(
        "sparse",
        base_r[:delta_rest],
        base_c[:delta_rest],
        np.ones(delta_rest, dtype=np.float32),
        delta_rest,
    )
    acc_compacted = grid_accumulator.merge_sparse_accumulators(
        acc_uncompacted, rest_input, max_uncompacted_delta=500_000
    )
    self.assertIsInstance(acc_compacted, grid_accumulator.SparseAccumulator)
    self.assertLen(acc_compacted.values, n_base)
    self.assertEqual(acc_compacted.last_compact_len, n_base)

  def _make_compacted_sparse_map_accumulator(
      self,
      *,
      max_sparse_points: int = 70,
  ) -> tuple[beam_stages.SumDenseGridCombineFn, Any]:
    grid_fn = beam_stages.SumDenseGridCombineFn(
        height=50,
        width=50,
        max_sparse_points=max_sparse_points,
        max_uncompacted_delta=25,
    )
    dup_r = np.tile(np.arange(10, dtype=np.int16), 8)
    dup_c = np.tile(np.arange(10, dtype=np.int16), 8)
    dup_v = np.ones(80, dtype=np.float32)
    accumulator = grid_fn.add_input(None, ("sparse", dup_r, dup_c, dup_v))
    index = np.arange(10, 65, dtype=np.int16)
    new_r = index // 50
    new_c = index % 50
    new_v = np.full(len(index), 2.0, dtype=np.float32)
    accumulator = grid_fn.add_input(
        accumulator, ("sparse", new_r, new_c, new_v)
    )
    return grid_fn, accumulator

  def test_sparse_accumulator_stays_sparse_in_map_within_max_points(self):
    _, accumulator = self._make_compacted_sparse_map_accumulator(
        max_sparse_points=70
    )
    self.assertIsInstance(accumulator, grid_accumulator.SparseAccumulator)
    self.assertLen(accumulator.values, 64)
    self.assertEqual(accumulator.last_compact_len, 64)

  def test_sparse_accumulator_stays_sparse_in_single_merge_compact(self):
    grid_fn, accumulator = self._make_compacted_sparse_map_accumulator(
        max_sparse_points=70
    )
    accumulator = grid_fn.add_input(
        accumulator,
        (
            "sparse",
            np.zeros(5, dtype=np.int16),
            np.zeros(5, dtype=np.int16),
            np.ones(5, dtype=np.float32),
        ),
    )
    self.assertIsInstance(accumulator, grid_accumulator.SparseAccumulator)
    self.assertLen(accumulator.values, 69)
    compacted_single = grid_fn.merge_accumulators([accumulator])
    self.assertIsInstance(compacted_single, grid_accumulator.SparseAccumulator)
    self.assertLen(compacted_single.values, 64)
    self.assertEqual(compacted_single.last_compact_len, 64)

  def test_sparse_accumulator_promotes_to_dense_in_map_above_max_points(self):
    grid_fn, accumulator = self._make_compacted_sparse_map_accumulator(
        max_sparse_points=60
    )
    self.assertIsInstance(accumulator, grid_accumulator.DenseBoundingBox)
    out = grid_fn.extract_output(accumulator)
    self.assertEqual(out.shape, (50, 50))
    self.assertEqual((float(out[0, 0]), float(out[0, 10])), (8.0, 2.0))

  def test_sparse_accumulator_promotes_to_dense_in_compact_above_max_points(
      self,
  ):
    grid_fn = beam_stages.SumDenseGridCombineFn(
        height=50, width=50, max_sparse_points=60, max_uncompacted_delta=25
    )
    first_idx = np.arange(50, dtype=np.int16)
    accumulator = grid_fn.add_input(
        None,
        ("sparse", first_idx // 50, first_idx % 50, np.ones(50, np.float32)),
    )
    second_idx = np.arange(50, 70, dtype=np.int16)
    accumulator = grid_fn.add_input(
        accumulator,
        ("sparse", second_idx // 50, second_idx % 50, np.ones(20, np.float32)),
    )
    self.assertIsInstance(accumulator, grid_accumulator.SparseAccumulator)
    self.assertLen(accumulator.values, 70)
    promoted = grid_fn.merge_accumulators([accumulator])
    self.assertIsInstance(promoted, grid_accumulator.DenseBoundingBox)
    out = grid_fn.extract_output(promoted)
    self.assertEqual((float(out[0, 0]), float(out[1, 19])), (1.0, 1.0))

  def test_compact_demotes_low_density_dense_box_before_shuffle(self):
    grid_fn, accumulator = self._make_compacted_sparse_map_accumulator(
        max_sparse_points=60
    )
    self.assertIsInstance(accumulator, grid_accumulator.DenseBoundingBox)
    shuffled = grid_fn.merge_accumulators([accumulator])
    self.assertIsInstance(shuffled, grid_accumulator.SparseAccumulator)
    self.assertLen(shuffled.values, 64)
    out = grid_fn.extract_output(shuffled)
    self.assertEqual((float(out[0, 0]), float(out[0, 10])), (8.0, 2.0))

  def test_extract_output_full_grid_box_returns_owned_float32_copy(self):
    grid_fn = beam_stages.SumDenseGridCombineFn(height=2, width=3)
    raw_sub_grid = np.ones((2, 3), dtype=np.float64)
    box = grid_accumulator.DenseBoundingBox(0, 2, 0, 3, raw_sub_grid)
    out = grid_fn.extract_output(box)
    self.assertEqual(out.dtype, np.float32)
    out[0, 0] = 99.0
    self.assertEqual(float(raw_sub_grid[0, 0]), 1.0)

  def test_extract_output_clips_partially_negative_and_overflowing_box(self):
    grid_fn = beam_stages.SumDenseGridCombineFn(height=4, width=5)
    sub_grid = np.arange(35, dtype=np.float32).reshape(5, 7)
    box = grid_accumulator.DenseBoundingBox(-2, 3, -1, 6, sub_grid)
    out = grid_fn.extract_output(box)
    self.assertEqual(out.shape, (4, 5))
    np.testing.assert_array_equal(out[0:3, 0:5], sub_grid[2:5, 1:6])
    np.testing.assert_array_equal(out[3, :], np.zeros(5, dtype=np.float32))

  @parameterized.named_parameters(
      dict(testcase_name="all_negative_rows", bounds=(-5, -1, 0, 3)),
      dict(testcase_name="all_negative_cols", bounds=(0, 3, -4, 0)),
      dict(testcase_name="past_grid_height", bounds=(4, 8, 0, 3)),
      dict(testcase_name="past_grid_width", bounds=(0, 3, 5, 9)),
  )
  def test_extract_output_completely_out_of_bounds_box_returns_zeros(
      self, bounds: tuple[int, int, int, int]
  ):
    grid_fn = beam_stages.SumDenseGridCombineFn(height=4, width=5)
    r_min, r_max, c_min, c_max = bounds
    box = grid_accumulator.DenseBoundingBox(
        r_min,
        r_max,
        c_min,
        c_max,
        np.ones((r_max - r_min, c_max - c_min), dtype=np.float32),
    )
    out = grid_fn.extract_output(box)
    self.assertEqual(out.shape, (4, 5))
    np.testing.assert_array_equal(out, np.zeros((4, 5), dtype=np.float32))

  def test_sparse_accumulator_promotes_to_dense_in_multi_accumulator_merge(
      self,
  ):
    grid_fn = beam_stages.SumDenseGridCombineFn(
        height=50, width=50, max_sparse_points=60, max_uncompacted_delta=25
    )
    index = np.arange(64, dtype=np.int16)
    sp_a = grid_accumulator.SparseAccumulator(
        "sparse",
        index // 50,
        index % 50,
        np.ones(64, dtype=np.float32),
        64,
    )
    sp_b = grid_accumulator.SparseAccumulator(
        "sparse",
        np.array([0], dtype=np.int16),
        np.array([0], dtype=np.int16),
        np.array([3.5], dtype=np.float32),
        1,
    )
    merged = grid_fn.merge_accumulators([sp_a, sp_b])
    self.assertIsInstance(merged, grid_accumulator.DenseBoundingBox)
    out = grid_fn.extract_output(merged)
    self.assertEqual(out.shape, (50, 50))
    self.assertAlmostEqual(float(out[0, 0]), 4.5, places=5)

  def test_grid_accumulation_pure_sparse_uncompacted_equivalence(self):
    shards, expected = _generate_sparse_shards_and_ground_truth(
        np.random.default_rng(42)
    )
    acc, out = _run_pure_sparse_combine(shards, 60, 80, delta=10_000_000)
    self.assertIsInstance(acc, grid_accumulator.SparseAccumulator)
    self.assertEqual(out.shape, (60, 80))
    np.testing.assert_allclose(out, expected, rtol=1e-5, atol=1e-3)

  def test_grid_accumulation_pure_sparse_compacted_equivalence(self):
    shards, expected = _generate_sparse_shards_and_ground_truth(
        np.random.default_rng(42)
    )
    acc, out = _run_pure_sparse_combine(shards, 60, 80, delta=30)
    self.assertIsInstance(acc, grid_accumulator.SparseAccumulator)
    self.assertEqual(out.shape, (60, 80))
    np.testing.assert_allclose(out, expected, rtol=1e-5, atol=1e-3)

  def test_grid_accumulation_default_pipeline_equivalence(self):
    shards, expected = _generate_sparse_shards_and_ground_truth(
        np.random.default_rng(42)
    )
    merged, out = _run_default_pipeline_combine(shards, 60, 80)
    self.assertIsInstance(merged, grid_accumulator.DenseBoundingBox)
    self.assertEqual(out.shape, (60, 80))
    np.testing.assert_allclose(out, expected, rtol=1e-5, atol=1e-3)

  def test_grid_accumulation_pure_dense_box_equivalence(self):
    shards, expected = _generate_sparse_shards_and_ground_truth(
        np.random.default_rng(42)
    )
    acc, out = _run_pure_dense_combine(shards, 60, 80)
    self.assertIsInstance(acc, grid_accumulator.DenseBoundingBox)
    self.assertEqual(out.shape, (60, 80))
    np.testing.assert_allclose(out, expected, rtol=1e-5, atol=1e-3)


def _sparse(rows, cols, values) -> grid_accumulator.SparseAccumulator:
  return grid_accumulator.SparseAccumulator(
      "sparse",
      np.asarray(rows, dtype=np.int16),
      np.asarray(cols, dtype=np.int16),
      np.asarray(values, dtype=np.float32),
      len(values),
  )


def _as_sparse(payload_input: Any) -> grid_accumulator.SparseAccumulator:
  """Asserts `payload_input` is a SparseAccumulator and narrows its type."""
  if not isinstance(payload_input, grid_accumulator.SparseAccumulator):
    raise AssertionError(
        f"expected SparseAccumulator, got {type(payload_input)!r}"
    )
  return payload_input


def _as_box(payload_input: Any) -> grid_accumulator.DenseBoundingBox:
  """Asserts `payload_input` is a DenseBoundingBox and narrows its type."""
  if not isinstance(payload_input, grid_accumulator.DenseBoundingBox):
    raise AssertionError(
        f"expected DenseBoundingBox, got {type(payload_input)!r}"
    )
  return payload_input


def _densify(accumulator, height: int, width: int) -> np.ndarray:
  """Materializes any accumulator encoding into a full grid for comparison."""
  return beam_stages.SumDenseGridCombineFn(
      height=height, width=width
  ).extract_output(accumulator)


def _simulate_combine_per_key(
    fn: Any,
    elements,
    rng: np.random.Generator,
    num_bundles: int = 3,
):
  """Mimics Beam's lifted CombinePerKey: per-bundle compaction, then merge.

  Elements are randomly split across `num_bundles` map-side bundles so both
  the single-accumulator `compact` path and the multi-accumulator merge path
  (including dense promotion) are exercised.

  Args:
    fn: The combine fn under test.
    elements: `(key, value)` pairs to combine.
    rng: Random generator that assigns each value to a bundle.
    num_bundles: Number of map-side bundles per key.

  Returns:
    Mapping of key to the combine fn's extracted output.
  """
  by_key = {}
  for k, values_item in elements:
    by_key.setdefault(k, []).append(values_item)
  out = {}
  for k, values in by_key.items():
    bundles = [fn.create_accumulator() for _ in range(num_bundles)]
    for values_item in values:
      acc_item_b = int(rng.integers(num_bundles))
      bundles[acc_item_b] = fn.add_input(bundles[acc_item_b], values_item)
    partials = [fn.compact(acc_item_b) for acc_item_b in bundles]
    out[k] = fn.extract_output(fn.merge_accumulators(partials))
  return out


def _simulate_stage2_accumulation(
    fn: beam_stages.SumDenseGridCombineFn, payload: Any
) -> np.ndarray:
  accumulator = fn.add_input(fn.create_accumulator(), payload)
  accumulator = fn.add_input(accumulator, payload)
  accumulator = fn.add_input(accumulator, _sparse([0], [0], [10.0]))
  return fn.extract_output(accumulator)


def _generate_two_stage_elements(
    rng: np.random.Generator, height: int, width: int, count: int = 300
) -> list[Any]:
  elements = []
  for _ in range(count):
    gt = ("density", "impact")[int(rng.integers(2))]
    month = int(rng.integers(1, 13))
    hour = int(rng.integers(24))
    month_key = 202500 + month if rng.random() < 0.5 else month
    num_items = int(rng.integers(0, 25))
    payload = (
        "sparse",
        rng.integers(0, height, num_items).astype(np.int16),
        rng.integers(0, width, num_items).astype(np.int16),
        rng.normal(size=num_items).astype(np.float32),
    )
    elements.append(((gt, month_key, hour), payload))
  return elements


def _run_two_stage_pipeline_combine(
    elements: Sequence[Any],
    seeds: Sequence[Any],
    height: int,
    width: int,
    rng: np.random.Generator,
) -> tuple[dict[Any, Any], list[Any], dict[Any, Any], dict[Any, Any]]:
  normalize = beam_stages.NormalizeGridMonthHourKeyDoFn(expected_year=2025)
  norm_in = []
  for e in elements:
    norm_in.extend(normalize.process(e))
  fanout = beam_stages.FanoutGridExportKeysDoFn()
  legacy_in = []
  for ne in norm_in:
    legacy_in.extend(fanout.process(ne))
  legacy = _simulate_combine_per_key(
      beam_stages.SumDenseGridCombineFn(height=height, width=width),
      legacy_in + list(seeds),
      rng,
  )
  stage1 = _simulate_combine_per_key(
      beam_stages.SumGridSlicesToAccumulatorCombineFn(max_sparse_points=8),
      norm_in,
      rng,
  )
  stage2_in = []
  for kv in stage1.items():
    stage2_in.extend(fanout.process(kv))
  two_stage = _simulate_combine_per_key(
      beam_stages.SumDenseGridCombineFn(height=height, width=width),
      stage2_in + list(seeds),
      rng,
  )
  return stage1, stage2_in, two_stage, legacy


def _assert_two_stage_grids_match_legacy(
    test_case: absltest.TestCase,
    two_stage: Mapping[Any, np.ndarray],
    legacy: Mapping[Any, np.ndarray],
    height: int,
    width: int,
) -> None:
  for k in legacy:
    test_case.assertEqual(two_stage[k].shape, (height, width), msg=str(k))
    np.testing.assert_allclose(
        two_stage[k], legacy[k], rtol=1e-5, atol=1e-4, err_msg=str(k)
    )


class GridTwoStageCombineTest(parameterized.TestCase):
  """Covers the Stage-1 `(month, hour)` -> Stage-2 export-key grid combine."""

  _H = 20
  _W = 40

  def test_to_compact_none_returns_none(self):
    self.assertIsNone(grid_accumulator.to_compact_grid_accumulator(None))

  @parameterized.named_parameters(
      dict(
          testcase_name="empty_sparse",
          empty_acc=_sparse([], [], []),
      ),
      dict(
          testcase_name="empty_dense_box",
          empty_acc=grid_accumulator.DenseBoundingBox(
              0, 0, 0, 0, np.zeros((0, 0), dtype=np.float32)
          ),
      ),
  )
  def test_to_compact_empty_accumulator_returns_empty_sparse(self, empty_acc):
    out = grid_accumulator.to_compact_grid_accumulator(empty_acc)
    out = _as_sparse(out)
    self.assertEmpty(out.values)

  def test_to_compact_sparse_removes_duplicates(self):
    accumulator = grid_accumulator.SparseAccumulator(
        "sparse",
        np.array([1, 1, 2], dtype=np.int16),
        np.array([3, 3, 4], dtype=np.int16),
        np.array([1.0, 2.0, 5.0], dtype=np.float32),
        0,  # Deliberately stale `last_compact_len`.
    )
    out = grid_accumulator.to_compact_grid_accumulator(accumulator)
    out = _as_sparse(out)
    self.assertLen(out.values, 2)
    self.assertEqual(out.last_compact_len, 2)
    np.testing.assert_allclose(
        _densify(out, self._H, self._W), _densify(accumulator, self._H, self._W)
    )

  def test_to_compact_plain_sparse_tuple(self):
    accumulator = (
        "sparse",
        np.array([0, 0], dtype=np.int16),
        np.array([1, 1], dtype=np.int16),
        np.array([1.5, 2.5], dtype=np.float32),
    )
    out = grid_accumulator.to_compact_grid_accumulator(accumulator)
    out = _as_sparse(out)
    self.assertLen(out.values, 1)
    self.assertAlmostEqual(float(out.values[0]), 4.0)

  def test_to_compact_sparse_friendly_box_becomes_sparse_with_global_indices(
      self,
  ):
    # A 10x10 box at offset (5, 7) with only 3 non-zero cells: sparse is
    # 3*8=24 bytes vs 100*4=400 bytes dense, so it must convert.
    sub = np.zeros((10, 10), dtype=np.float32)
    sub[0, 0] = 1.0
    sub[4, 9] = 2.0
    sub[9, 3] = -3.0  # Negative values must survive (not just > 0).
    box = grid_accumulator.DenseBoundingBox(5, 15, 7, 17, sub)
    out = grid_accumulator.to_compact_grid_accumulator(box)
    out = _as_sparse(out)
    self.assertEqual(out.rows.dtype, np.int16)
    self.assertEqual(out.values.dtype, np.float32)
    self.assertEqual(out.last_compact_len, 3)
    self.assertCountEqual(
        zip(out.rows.tolist(), out.cols.tolist(), strict=True),
        [(5, 7), (9, 16), (14, 10)],
    )
    np.testing.assert_allclose(
        _densify(out, self._H, self._W), _densify(box, self._H, self._W)
    )

  def test_to_compact_dense_friendly_box_stays_dense(self):
    sub = np.ones((4, 4), dtype=np.float64)  # float64 must be cast.
    sub[0, 0] = 0.0
    box = grid_accumulator.DenseBoundingBox(2, 6, 3, 7, sub)
    with mock.patch.object(
        np,
        "nonzero",
        side_effect=AssertionError("nonzero called"),
        autospec=True,
        spec_set=True,
    ):
      out = grid_accumulator.to_compact_grid_accumulator(box)
    out = _as_box(out)
    self.assertEqual(out.sub_grid.dtype, np.float32)
    self.assertEqual((out.r_min, out.r_max, out.c_min, out.c_max), (2, 6, 3, 7))

  def test_to_compact_all_zero_box_becomes_empty_sparse_without_nonzero(self):
    sub = np.zeros((4, 4), dtype=np.float32)
    box = grid_accumulator.DenseBoundingBox(2, 6, 3, 7, sub)
    with mock.patch.object(
        np,
        "nonzero",
        side_effect=AssertionError("nonzero called"),
        autospec=True,
        spec_set=True,
    ):
      out = grid_accumulator.to_compact_grid_accumulator(box)
    out = _as_sparse(out)
    self.assertEmpty(out.values)

  def test_to_compact_break_even_prefers_dense(self):
    # Exactly half populated: 8*nnz == 4*size, ties go to dense.
    sub = np.zeros((2, 2), dtype=np.float32)
    sub[0, 0] = 1.0
    sub[1, 1] = 1.0
    out = grid_accumulator.to_compact_grid_accumulator(
        grid_accumulator.DenseBoundingBox(0, 2, 0, 2, sub)
    )
    self.assertIsInstance(out, grid_accumulator.DenseBoundingBox)

  @parameterized.named_parameters(
      dict(testcase_name="string_payload", bad_payload="not an accumulator"),
      dict(
          testcase_name="2d_ndarray",
          bad_payload=np.zeros((5, 5), dtype=np.float32),
      ),
      dict(
          testcase_name="legacy_dense_tuple",
          bad_payload=(
              "dense",
              1,
              4,
              2,
              5,
              np.zeros((3, 3), dtype=np.float32),
          ),
      ),
  )
  def test_to_compact_rejects_unknown_type(self, bad_payload: Any):
    with self.assertRaisesRegex(TypeError, "Unsupported grid payload"):
      grid_accumulator.to_compact_grid_accumulator(bad_payload)

  def test_stage1_extract_output_is_compact_not_full_grid(self):
    fn = beam_stages.SumGridSlicesToAccumulatorCombineFn()
    self.assertIsNone(fn.extract_output(fn.create_accumulator()))
    accumulator = fn.create_accumulator()
    accumulator = fn.add_input(accumulator, _sparse([1, 2], [3, 4], [1.0, 2.0]))
    accumulator = fn.add_input(accumulator, _sparse([1], [3], [0.5]))
    out = fn.extract_output(accumulator)
    out = _as_sparse(out)
    self.assertLen(out.values, 2)  # Compacted: (1, 3) appears once.
    # The Stage-2 combiner still materializes a full grid from it.
    legacy = beam_stages.SumDenseGridCombineFn(height=self._H, width=self._W)
    self.assertEqual(
        legacy.extract_output(accumulator).shape, (self._H, self._W)
    )

  def test_stage1_converts_promoted_dense_box_back_to_sparse(self):
    # A tiny `max_sparse_points` forces merge_accumulators to promote into a
    # DenseBoundingBox. Stage-1 output must not ship that box when a sparse
    # encoding is smaller - this is the ~169 GB shuffle regression guard.
    fn = beam_stages.SumGridSlicesToAccumulatorCombineFn(max_sparse_points=2)
    acc_item_a = fn.add_input(None, _sparse([0, 0], [0, 1], [1.0, 1.0]))
    acc_item_b = fn.add_input(None, _sparse([19], [39], [3.0]))
    merged = fn.merge_accumulators([acc_item_a, acc_item_b])
    merged = _as_box(merged)
    self.assertEqual(merged.sub_grid.size, 20 * 40)
    out = fn.extract_output(merged)
    out = _as_sparse(out)
    self.assertLen(out.values, 3)
    np.testing.assert_allclose(
        _densify(out, self._H, self._W), _densify(merged, self._H, self._W)
    )

  @parameterized.named_parameters(
      dict(testcase_name="sparse_payload", use_dense=False),
      dict(testcase_name="dense_payload", use_dense=True),
  )
  def test_stage2_does_not_mutate_shared_stage1_payload(self, use_dense: bool):
    # The fanout hands the SAME Stage-1 output to total/monthly/hourly. Each
    # Stage-2 accumulator must leave it untouched, for both encodings.
    payload = (
        grid_accumulator.DenseBoundingBox(
            0, 3, 0, 3, np.ones((3, 3), dtype=np.float32)
        )
        if use_dense
        else _sparse([1, 2], [1, 2], [1.0, 2.0])
    )
    snapshot = _densify(payload, self._H, self._W).copy()
    fn = beam_stages.SumDenseGridCombineFn(height=self._H, width=self._W)
    out1 = _simulate_stage2_accumulation(fn, payload)
    out2 = _simulate_stage2_accumulation(fn, payload)
    out3 = _simulate_stage2_accumulation(fn, payload)
    np.testing.assert_array_equal(_densify(payload, self._H, self._W), snapshot)
    expected = 2 * snapshot + _densify(
        _sparse([0], [0], [10.0]), self._H, self._W
    )
    np.testing.assert_allclose(out1, expected)
    np.testing.assert_allclose(out2, expected)
    np.testing.assert_allclose(out3, expected)

  def test_two_stage_matches_fanout_first_single_stage(self):
    rng = np.random.default_rng(1234)
    elements = _generate_two_stage_elements(rng, self._H, self._W, count=300)
    seeds = [(k, _sparse([], [], [])) for k in grid_publish.all_export_keys()]
    stage1, stage2_in, two_stage, legacy = _run_two_stage_pipeline_combine(
        elements, seeds, self._H, self._W, rng
    )
    self.assertLessEqual(len(stage1), 2 * 12 * 24)
    self.assertLen(stage2_in, 3 * len(stage1))
    self.assertCountEqual(two_stage.keys(), grid_publish.all_export_keys())
    self.assertCountEqual(legacy.keys(), two_stage.keys())
    _assert_two_stage_grids_match_legacy(
        self, two_stage, legacy, self._H, self._W
    )


class GridExportDoFnTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.test_dir = self.create_tempdir().full_path

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

  def test_fanout_grid_export_keys_dofn(self):
    payload = object()
    dofn = beam_stages.FanoutGridExportKeysDoFn()
    outputs = list(dofn.process((("density", 3, 14), payload)))
    self.assertEqual(
        outputs,
        [
            (("density", "total", 0), payload),
            (("density", "monthly", 3), payload),
            (("density", "hourly", 14), payload),
        ],
    )
    self.assertIs(outputs[0][1], payload)
    self.assertIs(outputs[1][1], payload)
    self.assertIs(outputs[2][1], payload)

  def test_normalize_grid_month_hour_key_dofn(self):
    dofn = beam_stages.NormalizeGridMonthHourKeyDoFn(expected_year=2025)
    payload = object()
    # YYYYMM is reduced to MM, and the payload is forwarded by identity (no
    # copy) - exactly one output per input.
    outputs = list(dofn.process((("density", 202503, "14"), payload)))
    self.assertLen(outputs, 1)
    self.assertEqual(outputs[0][0], ("density", 3, 14))
    self.assertIs(outputs[0][1], payload)
    # An already-normalized month maps to the same Stage-1 key as its YYYYMM
    # form; this is what lets both forms combine together.
    outputs_mm = list(dofn.process((("density", 3, 14), payload)))
    self.assertEqual(outputs_mm[0][0], ("density", 3, 14))

  @parameterized.named_parameters(
      ("wrong_year", 202412, 3),
      ("month_above_12", 202513, 3),
      ("month_zero", 202500, 3),
      ("hour_above_23", 202501, 24),
      ("hour_negative", 202501, -1),
  )
  def test_normalize_grid_month_hour_key_dofn_drops_bad_slices(
      self, year_month, hour
  ):
    dofn = beam_stages.NormalizeGridMonthHourKeyDoFn(expected_year=2025)
    self.assertEmpty(list(dofn.process((("density", year_month, hour), "x"))))

  def test_normalize_grid_month_hour_key_dofn_accepts_any_year_unconstrained(
      self,
  ):
    no_year = beam_stages.NormalizeGridMonthHourKeyDoFn()
    self.assertEqual(
        list(no_year.process((("density", 202412, 3), "x")))[0][0],
        ("density", 12, 3),
    )

  def test_write_shared_grid_meta_fn(self):
    fn = beam_stages.WriteSharedGridMetaFn(self._cfg(), self.test_dir)
    self.assertEqual(
        fn(self._all_results({"density": 4.0, "impact": 5.0})),
        beam_stages.WriteSharedGridMetaFn.SIGNAL,
    )
    with open(
        os.path.join(self.test_dir, "grid_meta.json"), "r", encoding="utf-8"
    ) as f:
      self.assertEqual(
          json.load(f)["value_max_ref_scaled"],
          {"density": 4.0, "impact": 5.0},
      )
    with self.assertRaisesRegex(
        ValueError, r"expected exactly one result per 74 export keys"
    ):
      fn([])

  def test_write_grid_artifacts_dofn_process(self):
    cfg = self._cfg(width=40, height=20)
    grid = np.ones((20, 40), dtype=np.float32)
    output_dir = os.path.join(self.test_dir, "output")
    inter_dir = os.path.join(self.test_dir, "inter")
    dofn = beam_stages.WriteGridArtifactsDoFn(
        grid_config=cfg,
        output_dir=output_dir,
        intermediate_root=inter_dir,
        run_id="run-1",
    )
    results = list(dofn.process((("impact", "total", 0), grid)))
    self.assertLen(results, 1)
    first_result = results[0]
    self.assertEqual(first_result.key, ("impact", "total", 0))
    self.assertEqual(first_result.prefix, "total_global_impact_grid")
    self.assertTrue(
        os.path.exists(
            os.path.join(output_dir, "total_global_impact_grid.bin.gz")
        )
    )
    self.assertTrue(
        os.path.exists(
            os.path.join(
                inter_dir,
                "runs",
                "run-1",
                "grid_slices",
                "total_global_impact_grid.npz",
            )
        )
    )

  def test_write_grid_artifacts_dofn_rejects_invalid_key(self):
    cfg = self._cfg(width=40, height=20)
    grid = np.ones((20, 40), dtype=np.float32)
    output_dir = os.path.join(self.test_dir, "output_invalid")
    dofn = beam_stages.WriteGridArtifactsDoFn(
        grid_config=cfg,
        output_dir=output_dir,
    )
    with self.assertRaisesRegex(ValueError, "unknown bucket type"):
      list(dofn.process((("impact", "invalid_bucket", 0), grid)))
    self.assertFalse(os.path.exists(output_dir))


if __name__ == "__main__":
  absltest.main()
