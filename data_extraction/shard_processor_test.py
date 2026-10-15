"""Unit tests for shard_processor module."""

from collections.abc import Iterator, Mapping, Sequence
import json
import os
from typing import Any
from unittest import mock

from absl import flags
from absl.testing import absltest
from absl.testing import parameterized
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from data_extraction import geo_utils
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


def _with_ef(table: pa.Table, value: float = 1.0) -> pa.Table:
  """Adds a constant `sum_ef_mj` column for tests that don't exercise EF."""
  return table.append_column(
      "sum_ef_mj", pa.array([value] * table.num_rows, type=pa.float64())
  )


class _CountingAirportsMap(Mapping[str, str]):
  """Mapping wrapper that records every looked-up airport code key."""

  def __init__(self, backing: Mapping[str, str]):
    self._backing = backing
    self.looked_up_keys: list[str] = []

  def __getitem__(self, key: str) -> str:
    self.looked_up_keys.append(key)
    return self._backing[key]

  def __iter__(self) -> Iterator[str]:
    return iter(self._backing)

  def __len__(self) -> int:
    return len(self._backing)


_TIME_ZONE_COLUMNS = (
    "lon_start",
    "lat_start",
    "lon_end",
    "lat_end",
    "time_start",
    "time_start_tz",
)
_AIRPORT_NORM_COLUMNS = (
    "lon_start",
    "lat_start",
    "lon_end",
    "lat_end",
    "departure_airport_icao",
    "arrival_airport_icao",
    "time_start",
)
_NONFINITE_TZ_OFFSETS = (
    2.0,
    330.0,
    -15.0,
    float("nan"),
    float("inf"),
    -float("inf"),
    14.0,
    -14.0,
)


def _write_test_countries_geojson(test_dir: str) -> str:
  path = os.path.join(test_dir, "countries.geojson")
  features = [
      test_fixtures.make_country_feature(
          "Belgium",
          "BE",
          [[2.0, 49.0], [6.0, 49.0], [6.0, 52.0], [2.0, 52.0], [2.0, 49.0]],
      ),
      test_fixtures.make_country_feature(
          "Germany",
          "DE",
          [[6.0, 47.0], [15.0, 47.0], [15.0, 55.0], [6.0, 55.0], [6.0, 47.0]],
      ),
  ]
  with open(path, "w", encoding="utf-8") as f:
    json.dump({"type": "FeatureCollection", "features": features}, f)
  return path


def _write_test_firs_geojson(test_dir: str) -> str:
  path = os.path.join(test_dir, "firs.geojson")
  features = [
      test_fixtures.make_fir_feature(
          "BRUSSELS FIR",
          "EBBU",
          "BE",
          "domestic",
          [[2.0, 49.0], [6.0, 49.0], [6.0, 52.0], [2.0, 52.0], [2.0, 49.0]],
      ),
      test_fixtures.make_fir_feature(
          "LANGEN FIR",
          "EDGG",
          "DE",
          "domestic",
          [[6.0, 47.0], [15.0, 47.0], [15.0, 55.0], [6.0, 55.0], [6.0, 47.0]],
      ),
  ]
  with open(path, "w", encoding="utf-8") as f:
    json.dump({"type": "FeatureCollection", "features": features}, f)
  return path


def _write_test_airports_csv(test_dir: str) -> str:
  path = os.path.join(test_dir, "airports.csv")
  with open(path, "w", encoding="utf-8") as f:
    f.write(
        "ident,iso_country,iata_code\nEBBR,BE,BRU\nEDDF,DE,FRA\nKJFK,US,JFK\n"
    )
  return path


def _make_multi_name_belgium_features() -> list[dict[str, Any]]:
  return [
      test_fixtures.make_country_feature(
          "Belgium",
          "BE",
          [[2.0, 50.0], [4.0, 50.0], [4.0, 52.0], [2.0, 52.0], [2.0, 50.0]],
      ),
      test_fixtures.make_country_feature(
          "Belgium-South",
          "BE",
          [[4.0, 49.0], [6.0, 49.0], [6.0, 51.0], [4.0, 51.0], [4.0, 49.0]],
      ),
  ]


def _make_sentinel_features() -> list[dict[str, Any]]:
  coords_kosovo = [
      [20.0, 42.0],
      [22.0, 42.0],
      [22.0, 44.0],
      [20.0, 44.0],
      [20.0, 42.0],
  ]
  coords_somaliland = [
      [44.0, 8.0],
      [48.0, 8.0],
      [48.0, 11.0],
      [44.0, 11.0],
      [44.0, 8.0],
  ]
  return [
      test_fixtures.make_country_feature("Kosovo", "-99", coords_kosovo),
      test_fixtures.make_country_feature(
          "Somaliland", "-99", coords_somaliland
      ),
  ]


def _make_multipolygon_country_features() -> list[dict[str, Any]]:
  be = [[2.0, 49.0], [6.0, 49.0], [6.0, 52.0], [2.0, 52.0], [2.0, 49.0]]
  de1 = [[6.0, 47.0], [8.0, 47.0], [8.0, 55.0], [6.0, 55.0], [6.0, 47.0]]
  de2 = [[8.0, 47.0], [10.0, 47.0], [10.0, 55.0], [8.0, 55.0], [8.0, 47.0]]
  return [
      test_fixtures.make_country_feature("Belgium", "BE", be),
      test_fixtures.make_country_feature("Belgium", "BE", be),
      test_fixtures.make_country_feature("Germany", "DE", de1),
      test_fixtures.make_country_feature("Germany", "DE", de2),
  ]


def _make_multipolygon_fir_features() -> list[dict[str, Any]]:
  be = [[2.0, 49.0], [6.0, 49.0], [6.0, 52.0], [2.0, 52.0], [2.0, 49.0]]
  return [
      test_fixtures.make_fir_feature(
          "BRUSSELS FIR", "EBBU", "BE", "domestic", be
      ),
      test_fixtures.make_fir_feature(
          "BRUSSELS FIR", "EBBU", "BE", "domestic", be
      ),
  ]


def _create_multipolygon_test_spatial_indices(
    test_dir: str,
) -> tuple[geo_utils.SpatialIndex, geo_utils.SpatialIndex]:
  c_feats = _make_multipolygon_country_features()
  f_feats = _make_multipolygon_fir_features()
  c_path = os.path.join(test_dir, "multi_countries.geojson")
  f_path = os.path.join(test_dir, "multi_firs.geojson")
  with open(c_path, "w", encoding="utf-8") as f:
    json.dump({"type": "FeatureCollection", "features": c_feats}, f)
  with open(f_path, "w", encoding="utf-8") as f:
    json.dump({"type": "FeatureCollection", "features": f_feats}, f)
  c_idx = geo_utils.SpatialIndex(c_feats, id_key="iso_a2", name_key="name")
  f_idx = geo_utils.SpatialIndex(f_feats, id_key="fir_icao", name_key="firname")
  return c_idx, f_idx


def _build_outgoing_comprehensive_flight_data() -> (
    shard_processor.ExtractedFlightData
):
  segments = [
      ("BE", "DE", 4.0, 50.0, 8.0, 50.0, 10000.0),
      ("BE", "DE", 8.0, 50.0, 10.0, 50.0, 7000.0),
      ("US", "DE", 8.0, 50.0, 10.0, 50.0, 3000.0),
      ("DE", "DE", 8.0, 50.0, 10.0, 51.0, 500.0),
  ]
  return shard_processor.ExtractedFlightData(
      lon_start=np.array([s[2] for s in segments], dtype=np.float64),
      lat_start=np.array([s[3] for s in segments], dtype=np.float64),
      lon_end=np.array([s[4] for s in segments], dtype=np.float64),
      lat_end=np.array([s[5] for s in segments], dtype=np.float64),
      ef=np.array([s[6] for s in segments], dtype=np.float64),
      months=np.array([202501] * len(segments), dtype=np.int32),
      hours=np.array([12] * len(segments), dtype=np.int32),
      dep_countries=np.array([s[0] for s in segments], dtype=object),
      arr_countries=np.array([s[1] for s in segments], dtype=object),
  )


def _merge_metrics_dict(
    dest: dict[Any, list[float]], src: Mapping[Any, Any]
) -> None:
  for k, entry in src.items():
    metrics = entry.to_metrics_tuple()
    existing = dest.get(k)
    if existing is None:
      dest[k] = list(metrics)
    else:
      dest[k] = [a + b for a, b in zip(existing, metrics, strict=True)]


def _make_interleaved_parity_flight_data() -> (
    shard_processor.ExtractedFlightData
):
  lons_s = [3.0, 4.0, 7.0, 3.5, 8.0, 2.5]
  lats_s = [50.0, 50.0, 50.0, 51.0, 49.0, 50.0]
  lons_e = [5.0, 8.0, 11.0, 4.5, 12.0, 9.5]
  lats_e = [50.0, 50.0, 51.0, 51.0, 49.0, 50.0]
  efs = [1000.0, 2500.0, 0.0, float("nan"), 800.0, 1600.0]
  months = [202501, 202506, 202501, 202506, 202512, 202501]
  hours = [8, 14, 8, 14, 23, 8]
  deps = [" be ", "DE", "US", None, "be", "FR"]
  arrs = ["de", "BE", "DE", "BE", "", "NL"]
  return shard_processor.ExtractedFlightData(
      lon_start=np.array(lons_s, dtype=np.float64),
      lat_start=np.array(lats_s, dtype=np.float64),
      lon_end=np.array(lons_e, dtype=np.float64),
      lat_end=np.array(lats_e, dtype=np.float64),
      ef=np.array(efs, dtype=np.float64),
      months=np.array(months, dtype=np.int32),
      hours=np.array(hours, dtype=np.int32),
      dep_countries=np.array(deps, dtype=object),
      arr_countries=np.array(arrs, dtype=object),
  )


def _make_border_crossing_flight_data() -> shard_processor.ExtractedFlightData:
  return shard_processor.ExtractedFlightData(
      lon_start=np.array([4.0], dtype=np.float64),
      lat_start=np.array([50.0], dtype=np.float64),
      lon_end=np.array([8.0], dtype=np.float64),
      lat_end=np.array([50.0], dtype=np.float64),
      ef=np.array([2000.0], dtype=np.float64),
      months=np.array([202506], dtype=np.int32),
      hours=np.array([11], dtype=np.int32),
      dep_countries=np.array(["US"], dtype=object),
      arr_countries=np.array(["DE"], dtype=object),
  )


def _compute_single_segment_expected_accumulators(
    flight_data: shard_processor.ExtractedFlightData,
    context: shard_processor.ProcessWorkerContext,
) -> tuple[dict[Any, list[float]], dict[Any, list[float]]]:
  expected_c: dict[Any, list[float]] = {}
  expected_f: dict[Any, list[float]] = {}
  for i in range(flight_data.count):
    single = shard_processor.ExtractedFlightData(
        lon_start=flight_data.lon_start[i : i + 1],
        lat_start=flight_data.lat_start[i : i + 1],
        lon_end=flight_data.lon_end[i : i + 1],
        lat_end=flight_data.lat_end[i : i + 1],
        ef=flight_data.ef[i : i + 1],
        months=flight_data.months[i : i + 1],
        hours=flight_data.hours[i : i + 1],
        dep_countries=flight_data.dep_countries[i : i + 1],
        arr_countries=flight_data.arr_countries[i : i + 1],
    )
    sc, sf = shard_processor.attribute_spatial_segments(
        flight_data=single,
        country_index=context.country_index,
        country_names_by_iso=context.country_names_by_iso,
        fir_index=context.fir_index,
    )
    _merge_metrics_dict(expected_c, sc)
    _merge_metrics_dict(expected_f, sf)
  return expected_c, expected_f


def _assert_spatial_accumulators_match_expected(
    batch_map: Mapping[Any, Any], expected_map: Mapping[Any, Sequence[float]]
) -> None:
  for k, exp in expected_map.items():
    np.testing.assert_allclose(batch_map[k].to_metrics_tuple(), exp, rtol=1e-12)


def _make_non_finite_ef_table() -> pa.Table:
  return pa.table(
      {
          "lon_start": [3.0, 3.0, 3.0, 3.0, 3.0],
          "lat_start": [50.0, 50.0, 50.0, 50.0, 50.0],
          "lon_end": [4.0, 4.0, 4.0, 4.0, 4.0],
          "lat_end": [51.0, 51.0, 51.0, 51.0, 51.0],
          "sum_ef_mj": [
              float("nan"),
              float("inf"),
              float("-inf"),
              None,
              600.0,
          ],
          "time_start": ["2025-06-15T10:00:00"] * 5,
          "dep_icao": ["EBBR"] * 5,
          "arr_icao": ["EDDF"] * 5,
      },
      schema=pa.schema([
          ("lon_start", pa.float64()),
          ("lat_start", pa.float64()),
          ("lon_end", pa.float64()),
          ("lat_end", pa.float64()),
          ("sum_ef_mj", pa.float64()),
          ("time_start", pa.string()),
          ("dep_icao", pa.string()),
          ("arr_icao", pa.string()),
      ]),
  )


def _assert_fir_metrics_finite(
    test_case: absltest.TestCase, f_accum: Mapping[Any, Any]
) -> None:
  for slice_values in f_accum.values():
    test_case.assertTrue(
        all(
            np.isfinite(entry_values)
            for entry_values in slice_values.to_metrics_tuple()
        )
    )


class ShardProcessorTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.test_dir = self.create_tempdir().full_path
    self.airports_csv_path = _write_test_airports_csv(self.test_dir)
    self.country_geojson_path = _write_test_countries_geojson(self.test_dir)
    self.firs_geojson_path = _write_test_firs_geojson(self.test_dir)
    self.context = shard_processor.load_process_worker_context(
        airports_path=self.airports_csv_path,
        firs_path=self.firs_geojson_path,
        country_geojson_path=self.country_geojson_path,
    )
    self.airports_map = self.context.airports_map

  def test_parse_shard_input_string_path(self):
    proc_result = shard_processor.parse_shard_input("test/path.parquet")
    self.assertEqual(proc_result, ("test/path.parquet", None, 0, None))

  @parameterized.named_parameters(
      (
          "tuple_input",
          ("test/path.parquet", 1, 100, 500),
          ("test/path.parquet", 1, 100, 500),
      ),
      (
          "list_input",
          ["test/path.parquet", None, 0, 100],
          ("test/path.parquet", None, 0, 100),
      ),
  )
  def test_parse_shard_input_tuple_and_list(self, shard_input, expected):
    self.assertEqual(shard_processor.parse_shard_input(shard_input), expected)

  @parameterized.named_parameters(
      ("wrong_tuple_length", ("only", "two")),
      ("non_sequence_type", 12345),
  )
  def test_parse_shard_input_invalid_format(self, invalid_input):
    self.assertIsNone(shard_processor.parse_shard_input(invalid_input))

  def test_acquire_process_worker_context_caching(self):
    ctx1 = shard_processor.acquire_process_worker_context(
        airports_path=self.airports_csv_path,
        firs_path=self.firs_geojson_path,
        country_geojson_path=self.country_geojson_path,
    )
    ctx2 = shard_processor.acquire_process_worker_context(
        airports_path=self.airports_csv_path,
        firs_path=self.firs_geojson_path,
        country_geojson_path=self.country_geojson_path,
    )
    self.assertIs(ctx1, ctx2)
    self.assertLen(ctx1.airports_map, 3)

  def test_acquire_process_worker_context_cache_miss_on_different_path(self):
    airports2_path = os.path.join(self.test_dir, "airports2.csv")
    with open(airports2_path, "w", encoding="utf-8") as f:
      f.write("ident,iso_country,iata_code\nEBBR,BE,BRU\n")

    ctx1 = shard_processor.acquire_process_worker_context(
        airports_path=self.airports_csv_path,
        firs_path=self.firs_geojson_path,
        country_geojson_path=self.country_geojson_path,
    )
    ctx2 = shard_processor.acquire_process_worker_context(
        airports_path=airports2_path,
        firs_path=self.firs_geojson_path,
        country_geojson_path=self.country_geojson_path,
    )
    self.assertIsNot(ctx1, ctx2)
    self.assertLen(ctx2.airports_map, 1)

  def test_read_parquet_table_with_projection(self):
    pq_path = os.path.join(self.test_dir, "projection_test.parquet")
    table = pa.Table.from_arrays(
        [
            pa.array([3.0, 4.0]),
            pa.array([50.0, 51.0]),
            pa.array([5.0, 6.0]),
            pa.array([51.0, 52.0]),
            pa.array([100.0, 200.0]),
            pa.array(["unwanted_val1", "unwanted_val2"]),
        ],
        names=[
            "start_lon",
            "start_lat",
            "end_lon",
            "end_lat",
            "contrail_ef_mj",
            "unwanted_column",
        ],
    )
    pq.write_table(table, pq_path)

    loaded = shard_processor.read_parquet_table(pq_path)
    self.assertIsNotNone(loaded)
    self.assertIn("start_lon", loaded.column_names)
    self.assertIn("contrail_ef_mj", loaded.column_names)
    self.assertNotIn("unwanted_column", loaded.column_names)
    self.assertEqual(loaded.num_rows, 2)

  def test_read_parquet_table_slicing(self):
    pq_path = os.path.join(self.test_dir, "slice_test.parquet")
    table = pa.Table.from_arrays(
        [
            pa.array([1.0, 2.0, 3.0, 4.0, 5.0]),
            pa.array([50.0, 50.0, 50.0, 50.0, 50.0]),
            pa.array([2.0, 3.0, 4.0, 5.0, 6.0]),
            pa.array([51.0, 51.0, 51.0, 51.0, 51.0]),
        ],
        names=["lon_start", "lat_start", "lon_end", "lat_end"],
    )
    pq.write_table(table, pq_path)

    sliced = shard_processor.read_parquet_table(
        pq_path, row_offset=1, num_rows=2
    )
    self.assertIsNotNone(sliced)
    self.assertEqual(sliced.num_rows, 2)
    self.assertEqual(sliced["lon_start"].to_pylist(), [2.0, 3.0])

  def test_read_parquet_table_missing_file(self):
    missing_path = os.path.join(self.test_dir, "nonexistent.parquet")
    self.assertIsNone(shard_processor.read_parquet_table(missing_path))

  def _write_segment_parquet(
      self,
      name: str,
      lon_start: list[float],
      lat_start: list[float],
      lon_end: list[float],
      lat_end: list[float],
  ) -> str:
    """Writes a minimal well-formed segment parquet file."""
    path = os.path.join(self.test_dir, name)
    table = pa.table(
        {
            "lon_start": lon_start,
            "lat_start": lat_start,
            "lon_end": lon_end,
            "lat_end": lat_end,
            "sum_ef_mj": [1000.0] * len(lon_start),
            "time_start": ["2025-06-15T10:00:00"] * len(lon_start),
        },
        schema=pa.schema([
            ("lon_start", pa.float64()),
            ("lat_start", pa.float64()),
            ("lon_end", pa.float64()),
            ("lat_end", pa.float64()),
            ("sum_ef_mj", pa.float64()),
            ("time_start", pa.string()),
        ]),
    )
    pq.write_table(table, path)
    return path

  def _process(self, shard_input: object):
    return shard_processor.process_shard(
        shard_input=shard_input,
        context=self.context,
        grid_config=_TEST_GRID_CONFIG,
    )

  def test_process_shard_reports_ok_for_usable_data(self):
    path = self._write_segment_parquet(
        "outcome_ok.parquet", [3.0], [50.0], [5.0], [51.0]
    )
    result = self._process(path)
    self.assertEqual(result.outcome, shard_processor.ShardOutcome.OK)
    self.assertNotEmpty(result.country_records)

  def test_process_shard_reports_malformed_input(self):
    result = self._process(("only", "two"))
    self.assertEqual(
        result.outcome, shard_processor.ShardOutcome.MALFORMED_INPUT
    )
    self.assertEmpty(result.country_records)

  def test_process_shard_reports_read_failure(self):
    # An unreadable shard must not look like an empty one.
    result = self._process(os.path.join(self.test_dir, "nope.parquet"))
    self.assertEqual(result.outcome, shard_processor.ShardOutcome.READ_FAILED)

  def test_process_shard_reports_read_failure_for_missing_coordinates(self):
    # Coordinates are checked at read time, unlike the EF and time columns.
    path = os.path.join(self.test_dir, "outcome_nocoords.parquet")
    pq.write_table(pa.table({"foo": [1.0], "bar": [2.0]}), path)
    result = self._process(path)
    self.assertEqual(result.outcome, shard_processor.ShardOutcome.READ_FAILED)

  def test_process_shard_reports_empty_input(self):
    # Zero rows is a legitimate outcome, not a failure.
    path = self._write_segment_parquet("outcome_empty.parquet", [], [], [], [])
    result = self._process(path)
    self.assertEqual(result.outcome, shard_processor.ShardOutcome.EMPTY_INPUT)

  def test_process_shard_reports_no_valid_segments_and_keeps_counters(self):
    # A shard where every row is invalid must still report its drops.
    path = self._write_segment_parquet(
        "outcome_allbad.parquet",
        [float("nan"), float("nan")],
        [50.0, 50.0],
        [5.0, 5.0],
        [51.0, 51.0],
    )
    result = self._process(path)
    self.assertEqual(
        result.outcome, shard_processor.ShardOutcome.NO_VALID_SEGMENTS
    )
    self.assertEqual(result.invalid_coords_count, 2)
    self.assertEmpty(result.country_records)

  def test_extract_flight_arrays_keeps_counters_when_nothing_survives(self):
    # The drop counts must not vanish with the rows they describe.
    table = pa.table({
        "lon_start": [float("nan"), 3.0],
        "lat_start": [50.0, 50.0],
        "lon_end": [5.0, 3.0],
        "lat_end": [51.0, 50.0],
        "time_start": ["2025-06-15T10:00:00", "2025-06-15T10:00:00"],
    })
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=self.airports_map, parquet_path="test"
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, 0)
    self.assertEqual(flight_data.invalid_coords_count, 1)
    self.assertEqual(flight_data.zero_len_count, 1)

  def test_extract_flight_arrays_returns_none_without_coordinates(self):
    table = pa.table({"foo": [1.0], "bar": [2.0]})
    self.assertIsNone(
        shard_processor.extract_flight_arrays(
            table, airports_map=self.airports_map, parquet_path="test"
        )
    )

  def test_extract_flight_arrays_returns_none_without_time_column(self):
    # Missing time column must return None, never stamp a fixed month.
    table = pa.table({
        "lon_start": [3.0],
        "lat_start": [50.0],
        "lon_end": [5.0],
        "lat_end": [51.0],
        "sum_ef_mj": [1000.0],
    })
    self.assertIsNone(
        shard_processor.extract_flight_arrays(
            table, airports_map=self.airports_map, parquet_path="test"
        )
    )

  def test_process_shard_reports_missing_columns_without_time_column(self):
    # Shard with coordinates but no time column must report MISSING_COLUMNS.
    path = os.path.join(self.test_dir, "outcome_notime.parquet")
    pq.write_table(
        pa.table({
            "lon_start": [3.0],
            "lat_start": [50.0],
            "lon_end": [5.0],
            "lat_end": [51.0],
            "sum_ef_mj": [1000.0],
        }),
        path,
    )
    result = self._process(path)
    self.assertEqual(
        result.outcome, shard_processor.ShardOutcome.MISSING_COLUMNS
    )
    self.assertTrue(result.is_failure)

  @parameterized.named_parameters(
      ("int64", pa.int64()),
      ("int32", pa.int32()),
      ("float64", pa.float64()),
  )
  def test_extract_flight_arrays_rejects_numeric_time_columns(self, type_):
    # Raw integer and float time columns must be rejected as unsupported.
    table = pa.Table.from_arrays(
        [
            pa.array([3.0]),
            pa.array([50.0]),
            pa.array([5.0]),
            pa.array([51.0]),
            pa.array([1749981600], type=type_),
        ],
        names=["lon_start", "lat_start", "lon_end", "lat_end", "time_start"],
    )
    self.assertIsNone(
        shard_processor.extract_flight_arrays(
            _with_ef(table), airports_map=self.airports_map, parquet_path="test"
        )
    )

  @parameterized.named_parameters(
      ("int64", pa.int64()),
      ("int32", pa.int32()),
      ("float64", pa.float64()),
  )
  def test_process_shard_reports_missing_columns_for_numeric_time_columns(
      self, type_
  ):
    # Numeric time columns must cause process_shard to report MISSING_COLUMNS.
    path = os.path.join(self.test_dir, "outcome_numtime.parquet")
    table = pa.Table.from_arrays(
        [
            pa.array([3.0]),
            pa.array([50.0]),
            pa.array([5.0]),
            pa.array([51.0]),
            pa.array([1000.0]),
            pa.array([1749981600], type=type_),
        ],
        names=[
            "lon_start",
            "lat_start",
            "lon_end",
            "lat_end",
            "sum_ef_mj",
            "time_start",
        ],
    )
    pq.write_table(table, path)
    result = self._process(path)
    self.assertEqual(
        result.outcome, shard_processor.ShardOutcome.MISSING_COLUMNS
    )
    self.assertTrue(result.is_failure)

  @parameterized.named_parameters(
      (
          "pa_timestamp_s",
          pa.array([1749981600], type=pa.timestamp("s")),
          10,
      ),
      (
          "pa_timestamp_ms",
          pa.array([1749981600 * 1000], type=pa.timestamp("ms")),
          10,
      ),
      (
          "pa_timestamp_us",
          pa.array([1749981600 * 1000000], type=pa.timestamp("us")),
          10,
      ),
      (
          "pa_timestamp_ns",
          pa.array([1749981600 * 1000000000], type=pa.timestamp("ns")),
          10,
      ),
      (
          "pa_string",
          pa.array(["2025-06-15T10:00:00"], type=pa.string()),
          10,
      ),
      (
          "pa_large_string",
          pa.array(["2025-06-15T10:00:00"], type=pa.large_string()),
          10,
      ),
      ("pa_date32", pa.array([1749981600 // 86400], type=pa.date32()), 0),
      (
          "pa_date64",
          pa.array([(1749981600 // 86400) * 86400000], type=pa.date64()),
          0,
      ),
  )
  def test_extract_flight_arrays_accepts_valid_timestamp_and_string_types(
      self, time_arr, expected_hour
  ):
    # Arrow timestamps across all units, dates, and strings must be accepted.
    table = pa.Table.from_arrays(
        [
            pa.array([3.0]),
            pa.array([50.0]),
            pa.array([5.0]),
            pa.array([51.0]),
            time_arr,
        ],
        names=[
            "lon_start",
            "lat_start",
            "lon_end",
            "lat_end",
            "time_start",
        ],
    )
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=self.airports_map
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, 1)
    self.assertEqual(flight_data.invalid_times_count, 0)
    self.assertEqual(flight_data.months[0], 202506)
    self.assertEqual(flight_data.hours[0], expected_hour)

  def test_extract_flight_arrays_returns_none_without_ef_column(self):
    # A shard without energy forcing must not be read as zero EF.
    table = pa.table({
        "lon_start": [3.0],
        "lat_start": [50.0],
        "lon_end": [5.0],
        "lat_end": [51.0],
        "time_start": ["2025-06-01T12:00:00"],
    })
    self.assertIsNone(
        shard_processor.extract_flight_arrays(
            table, airports_map=self.airports_map, parquet_path="test"
        )
    )

  def test_process_shard_reports_missing_columns_without_ef_column(self):
    path = os.path.join(self.test_dir, "outcome_noef.parquet")
    pq.write_table(
        pa.table({
            "lon_start": [3.0],
            "lat_start": [50.0],
            "lon_end": [5.0],
            "lat_end": [51.0],
            "time_start": ["2025-06-01T12:00:00"],
        }),
        path,
    )
    result = self._process(path)
    self.assertEqual(
        result.outcome, shard_processor.ShardOutcome.MISSING_COLUMNS
    )
    self.assertTrue(result.is_failure)

  def _write_row_grouped_parquet(
      self, name: str, group_sizes: list[int]
  ) -> str:
    """Writes a parquet file with one row group per entry in `group_sizes`.

    `lon_start` holds the absolute row index, so any chunk can be checked
    against the rows it was supposed to contain.

    Args:
      name: File name within the test directory.
      group_sizes: Row count for each row group, in order.

    Returns:
      Absolute path to the written file.
    """
    path = os.path.join(self.test_dir, name)
    total = sum(group_sizes)
    schema = pa.schema([
        ("lon_start", pa.float64()),
        ("lat_start", pa.float64()),
        ("lon_end", pa.float64()),
        ("lat_end", pa.float64()),
        ("unwanted_column", pa.string()),
    ])
    full = pa.table(
        {
            "lon_start": [float(i) for i in range(total)],
            "lat_start": [50.0] * total,
            "lon_end": [float(i) + 0.5 for i in range(total)],
            "lat_end": [51.0] * total,
            "unwanted_column": [f"row{i}" for i in range(total)],
        },
        schema=schema,
    )
    with pq.ParquetWriter(path, schema) as writer:
      offset = 0
      for size in group_sizes:
        writer.write_table(full.slice(offset, size))
        offset += size
    return path

  def test_read_parquet_table_reads_row_group(self):
    # Reading a specific row group returns the expected rows.
    path = self._write_row_grouped_parquet("rg_read.parquet", [10, 10, 10])
    table = shard_processor.read_parquet_table(path, row_group_idx=1)
    self.assertIsNotNone(table)
    self.assertEqual(
        table["lon_start"].to_pylist(), [float(i) for i in range(10, 20)]
    )
    self.assertIn("lat_start", table.column_names)
    self.assertNotIn("unwanted_column", table.column_names)

  @parameterized.named_parameters(
      ("rg1_chunk0", 1, 0, 10, 30, 40),
      ("rg1_chunk1", 1, 10, 10, 40, 50),
      ("rg1_chunk2", 1, 20, 10, 50, 60),
      ("rg0_subslice", 0, 15, 10, 15, 25),
  )
  def test_read_parquet_table_intra_row_group_chunk_offsets(
      self, row_group_idx, row_offset, num_rows, expected_start, expected_end
  ):
    path = self._write_row_grouped_parquet("rg_multi.parquet", [30, 30])
    chunk = shard_processor.read_parquet_table(
        path,
        row_group_idx=row_group_idx,
        row_offset=row_offset,
        num_rows=num_rows,
    )
    self.assertIsNotNone(chunk)
    self.assertEqual(
        chunk["lon_start"].to_pylist(),
        [float(i) for i in range(expected_start, expected_end)],
    )

  @parameterized.named_parameters(
      ("above_upper_bound", 5),
      ("negative_index", -1),
  )
  def test_read_parquet_table_rejects_out_of_range_row_group(
      self, row_group_idx
  ):
    path = self._write_row_grouped_parquet("rg_bounds.parquet", [10, 10])
    self.assertIsNone(
        shard_processor.read_parquet_table(
            path, row_group_idx=row_group_idx, row_offset=0, num_rows=10
        )
    )

  @parameterized.named_parameters(
      ("negative_row_offset", -5, 10),
      ("negative_num_rows", 0, -5),
  )
  def test_read_parquet_table_rejects_negative_offset_or_rows(
      self, row_offset, num_rows
  ):
    path = self._write_row_grouped_parquet("rg_neg.parquet", [10])
    self.assertIsNone(
        shard_processor.read_parquet_table(
            path, row_group_idx=0, row_offset=row_offset, num_rows=num_rows
        )
    )

  def test_read_parquet_table_rejects_short_reads(self):
    # Returning fewer rows than requested means the offsets are wrong.
    path = self._write_row_grouped_parquet("rg_short.parquet", [30])

    self.assertIsNone(
        shard_processor.read_parquet_table(
            path, row_group_idx=None, row_offset=25, num_rows=10
        ),
        "a short read was returned instead of a hard failure",
    )

  def test_extract_flight_arrays_all_synonyms(self):
    table = pa.Table.from_arrays(
        [
            pa.array([3.0]),
            pa.array([50.0]),
            pa.array([5.0]),
            pa.array([51.0]),
            pa.array([5000.0]),
            pa.array(["2025-06-15T10:00:00"]),
            pa.array(["EBBR"]),
            pa.array(["EDDF"]),
        ],
        names=[
            "start_lon",
            "start_lat",
            "end_lon",
            "end_lat",
            "energy_forcing_mj",
            "flight_time",
            "dep_icao",
            "arr_icao",
        ],
    )
    flight_data = shard_processor.extract_flight_arrays(
        table, airports_map=self.context.airports_map
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, 1)
    self.assertEqual(
        (flight_data.lon_start[0], flight_data.lat_start[0]), (3.0, 50.0)
    )
    self.assertAlmostEqual(flight_data.ef[0], 5000.0)
    self.assertEqual(
        (flight_data.dep_countries[0], flight_data.arr_countries[0]),
        ("BE", "DE"),
    )

  def test_extract_flight_arrays_timestamp_and_tz(self):
    table = pa.Table.from_arrays(
        [
            pa.array([3.0, 4.0]),
            pa.array([50.0, 50.0]),
            pa.array([5.0, 6.0]),
            pa.array([51.0, 51.0]),
            pa.array(["2025-06-15T08:30:00", "2025-12-01T23:00:00"]),
            pa.array([2, 2]),
        ],
        names=[
            "lon_start",
            "lat_start",
            "lon_end",
            "lat_end",
            "time_start",
            "time_start_tz",
        ],
    )
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=self.context.airports_map
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, 2)
    # 2025-06 -> 202506, hour 8 + tz 2 -> 10
    self.assertEqual(
        (flight_data.months[0], flight_data.hours[0]), (202506, 10)
    )
    # 2025-12 -> 202512, hour 23 + tz 2 -> (23 + 2) % 24 = 1
    self.assertEqual((flight_data.months[1], flight_data.hours[1]), (202512, 1))

  def test_extract_flight_arrays_timestamp_string_offsets_parsed_as_utc(self):
    coords = [3.0, 4.0, 5.0, 6.0]
    table = pa.Table.from_arrays(
        [
            pa.array(coords),
            pa.array([50.0] * 4),
            pa.array([c + 2.0 for c in coords]),
            pa.array([51.0] * 4),
            pa.array([
                "2025-06-15T10:00:00+02:00",  # UTC hour 8
                "2025-06-15T10:00:00Z",  # UTC hour 10
                "2025-06-15T10:00:00-04:00",  # UTC hour 14
                "2025-12-31T23:30:00-05:00",  # UTC 2026-01-01 04:30:00
            ]),
            pa.array([2.0, 0.0, -4.0, -5.0]),
        ],
        names=_TIME_ZONE_COLUMNS,
    )
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=self.context.airports_map
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, 4)
    self.assertEqual(list(flight_data.months), [202506, 202506, 202506, 202601])
    self.assertEqual(list(flight_data.hours), [10, 10, 10, 23])

  @parameterized.named_parameters(
      ("pa_string", pa.string()),
      ("pa_large_string", pa.large_string()),
  )
  def test_extract_flight_arrays_parses_numeric_string_timezone_offsets(
      self, string_type
  ):
    table = pa.Table.from_arrays(
        [
            pa.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]),
            pa.array([50.0] * 7),
            pa.array([2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]),
            pa.array([51.0] * 7),
            pa.array(["2025-06-15T10:00:00"] * 7),
            pa.array(
                ["+00", "+02", "-05", "+13", "-12", None, "+05:30"],
                type=string_type,
            ),
        ],
        names=[
            "lon_start",
            "lat_start",
            "lon_end",
            "lat_end",
            "time_start",
            "time_start_tz",
        ],
    )
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table),
        airports_map=self.context.airports_map,
        parquet_path="test",
    )
    self.assertIsNotNone(flight_data)
    # 6 valid rows (indices 0..5); 1 invalid row ("+05:30" at index 6)
    self.assertEqual(flight_data.count, 6)
    self.assertEqual(flight_data.invalid_times_count, 1)
    np.testing.assert_array_equal(
        flight_data.hours, np.array([10, 12, 5, 23, 22, 10], dtype=np.int32)
    )

  def test_extract_flight_arrays_parses_dictionary_string_timezone_offsets(
      self,
  ):
    dict_tz = pa.DictionaryArray.from_arrays(
        pa.array([0, 1, 2, None, 3, 4], type=pa.int8()),
        pa.array(["+00", "-08", "+09", "+02:00", "+15"]),
    )
    dict_table = pa.Table.from_arrays(
        [
            pa.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
            pa.array([50.0] * 6),
            pa.array([2.0, 3.0, 4.0, 5.0, 6.0, 7.0]),
            pa.array([51.0] * 6),
            pa.array(["2025-06-15T10:00:00"] * 6),
            dict_tz,
        ],
        names=[
            "lon_start",
            "lat_start",
            "lon_end",
            "lat_end",
            "time_start",
            "time_start_tz",
        ],
    )
    dict_data = shard_processor.extract_flight_arrays(
        _with_ef(dict_table),
        airports_map=self.context.airports_map,
        parquet_path="test",
    )
    self.assertIsNotNone(dict_data)
    self.assertEqual(dict_data.count, 4)
    self.assertEqual(dict_data.invalid_times_count, 2)
    np.testing.assert_array_equal(
        dict_data.hours, np.array([10, 2, 19, 10], dtype=np.int32)
    )

  def test_process_shard_reports_missing_columns_for_unsupported_tz_column_type(
      self,
  ):
    path = os.path.join(self.test_dir, "outcome_binary_tz.parquet")
    table = pa.Table.from_arrays(
        [
            pa.array([3.0]),
            pa.array([50.0]),
            pa.array([5.0]),
            pa.array([51.0]),
            pa.array([1000.0]),
            pa.array(["2025-06-15T10:00:00"]),
            pa.array([b"+02"], type=pa.binary()),
        ],
        names=[
            "lon_start",
            "lat_start",
            "lon_end",
            "lat_end",
            "sum_ef_mj",
            "time_start",
            "time_start_tz",
        ],
    )
    pq.write_table(table, path)
    result = self._process(path)
    self.assertEqual(
        result.outcome, shard_processor.ShardOutcome.MISSING_COLUMNS
    )
    self.assertTrue(result.is_failure)

  def test_extract_flight_arrays_out_of_range_and_nonfinite_timezone_offsets(
      self,
  ):
    table = pa.Table.from_arrays(
        [
            pa.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]),
            pa.array([50.0] * 8),
            pa.array([2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]),
            pa.array([51.0] * 8),
            pa.array(["2025-06-15T10:00:00"] * 8),
            pa.array(_NONFINITE_TZ_OFFSETS),
        ],
        names=_TIME_ZONE_COLUMNS,
    )
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=self.context.airports_map
    )
    self.assertIsNotNone(flight_data)
    # 3 valid rows (indices 0, 6, 7); 5 invalid rows (indices 1, 2, 3, 4, 5)
    self.assertEqual(flight_data.count, 3)
    self.assertEqual(flight_data.invalid_times_count, 5)
    self.assertEqual(flight_data.invalid_coords_count, 0)
    self.assertEqual(list(flight_data.hours), [12, 0, 20])

  def test_extract_flight_arrays_null_timezone_offsets_treated_as_utc(self):
    table = pa.Table.from_arrays(
        [
            pa.array([3.0, 4.0, 5.0]),
            pa.array([50.0, 50.0, 50.0]),
            pa.array([5.0, 6.0, 7.0]),
            pa.array([51.0, 51.0, 51.0]),
            pa.array(["2025-06-15T10:00:00"] * 3),
            pa.array([None, 3.0, None], type=pa.float64()),
        ],
        names=_TIME_ZONE_COLUMNS,
    )
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=self.context.airports_map
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, 3)
    self.assertEqual(flight_data.invalid_times_count, 0)
    self.assertEqual(list(flight_data.hours), [10, 13, 10])

  def test_extract_flight_arrays_all_null_timezone_column_treated_as_utc(self):
    null_col_table = pa.Table.from_arrays(
        [
            pa.array([3.0]),
            pa.array([50.0]),
            pa.array([5.0]),
            pa.array([51.0]),
            pa.array(["2025-06-15T10:00:00"]),
            pa.nulls(1),
        ],
        names=[
            "lon_start",
            "lat_start",
            "lon_end",
            "lat_end",
            "time_start",
            "time_start_tz",
        ],
    )
    null_col_data = shard_processor.extract_flight_arrays(
        _with_ef(null_col_table), airports_map=self.context.airports_map
    )
    self.assertIsNotNone(null_col_data)
    self.assertEqual(null_col_data.count, 1)
    self.assertEqual(null_col_data.invalid_times_count, 0)
    self.assertEqual(null_col_data.hours[0], 10)

  def test_extract_flight_arrays_fractional_timezone_offset_rounded(self):
    frac_table = pa.Table.from_arrays(
        [
            pa.array([3.0]),
            pa.array([50.0]),
            pa.array([5.0]),
            pa.array([51.0]),
            pa.array(["2025-06-15T10:00:00"]),
            pa.array([5.5]),
        ],
        names=[
            "lon_start",
            "lat_start",
            "lon_end",
            "lat_end",
            "time_start",
            "time_start_tz",
        ],
    )
    frac_data = shard_processor.extract_flight_arrays(
        _with_ef(frac_table), airports_map=self.context.airports_map
    )
    self.assertIsNotNone(frac_data)
    self.assertEqual(frac_data.count, 1)
    self.assertEqual(frac_data.hours[0], 16)

  def test_extract_flight_arrays_utc_months_and_local_hours_boundary_invariant(
      self,
  ):
    table = pa.Table.from_arrays(
        [
            pa.array([3.0, 4.0]),
            pa.array([50.0, 50.0]),
            pa.array([5.0, 6.0]),
            pa.array([51.0, 51.0]),
            pa.array([
                "2025-06-30T23:30:00Z",  # UTC month 202506, hour 23
                "2025-07-01T00:30:00Z",  # UTC month 202507, hour 0
            ]),
            pa.array([2.0, -4.0]),  # +2h (Athens), -4h (Santiago)
        ],
        names=[
            "lon_start",
            "lat_start",
            "lon_end",
            "lat_end",
            "time_start",
            "time_start_tz",
        ],
    )
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=self.context.airports_map
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, 2)
    # Row 0: 23:30 UTC + 2h -> local hour 1, UTC month remains 202506
    self.assertEqual((flight_data.months[0], flight_data.hours[0]), (202506, 1))
    # Row 1: 00:30 UTC - 4h -> local hour 20, UTC month remains 202507
    self.assertEqual(
        (flight_data.months[1], flight_data.hours[1]), (202507, 20)
    )

  def test_extract_flight_arrays_nan_filtering(self):
    table = pa.Table.from_arrays(
        [
            pa.array([3.0, float("nan"), 4.0]),
            pa.array([50.0, 50.0, float("nan")]),
            pa.array([5.0, 6.0, 7.0]),
            pa.array([51.0, 52.0, 53.0]),
            pa.array(["2025-06-15T10:00:00"] * 3),
        ],
        names=["lon_start", "lat_start", "lon_end", "lat_end", "time_start"],
    )
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=self.context.airports_map
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, 1)
    self.assertAlmostEqual(flight_data.lon_start[0], 3.0)

  def test_extract_flight_arrays_missing_coordinates(self):
    table = pa.Table.from_arrays(
        [pa.array([3.0]), pa.array([50.0])],
        names=["lon_start", "lat_start"],
    )
    self.assertIsNone(
        shard_processor.extract_flight_arrays(
            table, airports_map=self.context.airports_map
        )
    )

  def test_rasterize_flight_grid_slices(self):
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([3.0, 4.0], dtype=np.float64),
        lat_start=np.array([50.0, 50.5], dtype=np.float64),
        lon_end=np.array([5.0, 6.0], dtype=np.float64),
        lat_end=np.array([51.0, 51.5], dtype=np.float64),
        ef=np.array([5000.0, 10000.0], dtype=np.float64),
        months=np.array([202506, 202506], dtype=np.int32),
        hours=np.array([10, 10], dtype=np.int32),
        dep_countries=np.array(["BE", "BE"], dtype=object),
        arr_countries=np.array(["DE", "DE"], dtype=object),
    )
    slices = shard_processor.rasterize_flight_grid_slices(
        flight_data, _TEST_GRID_CONFIG
    )
    self.assertNotEmpty(slices)
    key, payload = slices[0]
    grid_type, month, hour = key
    self.assertIn(grid_type, ("density", "impact"))
    self.assertEqual((month, hour), (202506, 10))
    self.assertEqual(payload[0], "sparse")
    self.assertNotEmpty(payload[1])

  def test_rasterize_flight_grid_slices_antimeridian_density_conservation(
      self,
  ):
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([179.0], dtype=np.float64),
        lat_start=np.array([10.0], dtype=np.float64),
        lon_end=np.array([-179.0], dtype=np.float64),
        lat_end=np.array([10.0], dtype=np.float64),
        ef=np.array([1000.0], dtype=np.float64),
        months=np.array([202506], dtype=np.int32),
        hours=np.array([12], dtype=np.int32),
        dep_countries=np.array(["US"], dtype=object),
        arr_countries=np.array(["US"], dtype=object),
    )
    slices = shard_processor.rasterize_flight_grid_slices(
        flight_data, _TEST_GRID_CONFIG
    )
    density_slices = [
        seg_item for seg_item in slices if seg_item[0][0] == "density"
    ]
    self.assertNotEmpty(density_slices)
    _, payload = density_slices[0]
    total_density = float(np.sum(payload[3]))
    self.assertAlmostEqual(
        total_density,
        1.0,
        delta=0.02,
        msg=(
            "Antimeridian density double-counted in sparse slices:"
            f" {total_density}, expected ~1.0"
        ),
    )

  def test_attribute_spatial_segments_outgoing_and_crossings(self):
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([4.0], dtype=np.float64),
        lat_start=np.array([50.0], dtype=np.float64),
        lon_end=np.array([8.0], dtype=np.float64),
        lat_end=np.array([50.0], dtype=np.float64),
        ef=np.array([10000.0], dtype=np.float64),
        months=np.array([202506], dtype=np.int32),
        hours=np.array([12], dtype=np.int32),
        dep_countries=np.array(["BE"], dtype=object),
        arr_countries=np.array(["DE"], dtype=object),
    )
    country_accum, fir_accum = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )

    # 1. Outgoing attribution to departure country BE
    be_val = country_accum[("Belgium", "BE", 202506, 12)]
    self.assertAlmostEqual(be_val.outgoing_flight_impact, 10000.0)

    # 2. Both Belgium and Germany intersect the segment
    de_val = country_accum[("Germany", "DE", 202506, 12)]
    self.assertGreater(be_val.prop_sum_ef_mj, 0.0)
    self.assertGreater(de_val.prop_sum_ef_mj, 0.0)
    self.assertAlmostEqual(
        be_val.prop_sum_ef_mj + de_val.prop_sum_ef_mj, 10000.0, places=1
    )

    # 3. FIR attribution
    ebbu_key = ("BRUSSELS", "EBBU", "BE", "domestic", 202506, 12)
    self.assertGreater(fir_accum[ebbu_key].prop_sum_ef_mj, 0.0)

  def test_attribute_spatial_segments_unifies_country_name_for_same_iso(self):
    # Two features with ISO "BE" but distinct name strings ("Belgium" and
    # "Belgium-South"). Outgoing attribution and polygon intersections must
    # emit under a single canonical (c_name, "BE", month, hour) key.
    multi_name_feats = _make_multi_name_belgium_features()
    custom_c_idx = geo_utils.SpatialIndex(
        multi_name_feats, id_key="iso_a2", name_key="name"
    )
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([3.0], dtype=np.float64),
        lat_start=np.array([50.5], dtype=np.float64),
        lon_end=np.array([5.0], dtype=np.float64),
        lat_end=np.array([50.5], dtype=np.float64),
        ef=np.array([1000.0], dtype=np.float64),
        months=np.array([202506], dtype=np.int32),
        hours=np.array([12], dtype=np.int32),
        dep_countries=np.array(["BE"], dtype=object),
        arr_countries=np.array(["BE"], dtype=object),
    )
    country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=custom_c_idx,
        country_names_by_iso={"BE": "Belgium"},
        fir_index=self.context.fir_index,
    )
    be_keys = [k for k in country_accum if k[1] == "BE"]
    self.assertLen(be_keys, 1)
    self.assertEqual(be_keys[0], ("Belgium", "BE", 202506, 12))
    self.assertAlmostEqual(
        country_accum[be_keys[0]].prop_sum_ef_mj, 1000.0, places=1
    )
    self.assertAlmostEqual(
        country_accum[be_keys[0]].outgoing_flight_impact, 1000.0, places=1
    )

  @parameterized.named_parameters(
      ("eastbound", 179.0, -179.0),
      ("westbound", -179.0, 179.0),
  )
  def test_attribute_spatial_segments_antimeridian_crossing_does_not_smear(
      self, lon_start, lon_end
  ):
    # A trans-Pacific segment crossing the antimeridian at latitude 50.5N.
    # Without antimeridian splitting/wrapping before EPSG:6933 projection,
    # the segment is projected as a ~34,700 km line that cuts horizontally
    # across Europe and intersects Belgium and Germany.
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([lon_start], dtype=np.float64),
        lat_start=np.array([50.5], dtype=np.float64),
        lon_end=np.array([lon_end], dtype=np.float64),
        lat_end=np.array([50.5], dtype=np.float64),
        ef=np.array([10000.0], dtype=np.float64),
        months=np.array([202506], dtype=np.int32),
        hours=np.array([12], dtype=np.int32),
        dep_countries=np.array(["US"], dtype=object),
        arr_countries=np.array(["US"], dtype=object),
    )
    country_accum, fir_accum = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    be_entry = country_accum.get(("Belgium", "BE", 202506, 12))
    self.assertFalse(
        be_entry is not None and be_entry.prop_sum_ef_mj > 0.0,
        f"Antimeridian smearing in Belgium: {be_entry}",
    )
    de_entry = country_accum.get(("Germany", "DE", 202506, 12))
    self.assertFalse(
        de_entry is not None and de_entry.prop_sum_ef_mj > 0.0,
        f"Antimeridian smearing in Germany: {de_entry}",
    )
    self.assertEmpty(fir_accum)

  def test_attribute_spatial_segments_prime_meridian_crossing(self):
    # A country spanning the Prime Meridian (-5.0W to 2.0E)
    uk_feature = test_fixtures.make_country_feature(
        "United Kingdom",
        "GB",
        [[-5.0, 50.0], [2.0, 50.0], [2.0, 56.0], [-5.0, 56.0], [-5.0, 50.0]],
    )
    uk_index = geo_utils.SpatialIndex(
        [uk_feature], id_key="iso_a2", name_key="name"
    )
    dummy_fir_index = geo_utils.SpatialIndex(
        [], id_key="fir_icao", name_key="firname"
    )

    # Flight crossing the Prime Meridian within the UK: -2.0W to +1.0E
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([-2.0], dtype=np.float64),
        lat_start=np.array([52.0], dtype=np.float64),
        lon_end=np.array([1.0], dtype=np.float64),
        lat_end=np.array([52.0], dtype=np.float64),
        ef=np.array([5000.0], dtype=np.float64),
        months=np.array([202506], dtype=np.int32),
        hours=np.array([12], dtype=np.int32),
        dep_countries=np.array(["GB"], dtype=object),
        arr_countries=np.array(["GB"], dtype=object),
    )
    country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=uk_index,
        country_names_by_iso={"GB": "United Kingdom"},
        fir_index=dummy_fir_index,
    )
    gb_key = ("United Kingdom", "GB", 202506, 12)
    self.assertIn(gb_key, country_accum)
    # The segment is entirely inside the UK, so it should receive full 5000.0 MJ
    self.assertAlmostEqual(
        country_accum[gb_key].prop_sum_ef_mj, 5000.0, places=1
    )

  def test_attribute_spatial_segments_equator_crossing(self):
    # A country spanning the Equator (-5.0S to 5.0N)
    eq_coords = [
        [-82.0, -5.0],
        [-75.0, -5.0],
        [-75.0, 2.0],
        [-82.0, 2.0],
        [-82.0, -5.0],
    ]
    eq_feature = test_fixtures.make_country_feature("Ecuador", "EC", eq_coords)
    eq_index = geo_utils.SpatialIndex(
        [eq_feature], id_key="iso_a2", name_key="name"
    )
    dummy_fir_index = geo_utils.SpatialIndex(
        [], id_key="fir_icao", name_key="firname"
    )

    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([-78.0], dtype=np.float64),
        lat_start=np.array([-2.0], dtype=np.float64),
        lon_end=np.array([-78.0], dtype=np.float64),
        lat_end=np.array([1.0], dtype=np.float64),
        ef=np.array([4000.0], dtype=np.float64),
        months=np.array([202506], dtype=np.int32),
        hours=np.array([12], dtype=np.int32),
        dep_countries=np.array(["EC"], dtype=object),
        arr_countries=np.array(["EC"], dtype=object),
    )
    country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=eq_index,
        country_names_by_iso={"EC": "Ecuador"},
        fir_index=dummy_fir_index,
    )
    ec_key = ("Ecuador", "EC", 202506, 12)
    self.assertIn(ec_key, country_accum)
    self.assertAlmostEqual(
        country_accum[ec_key].prop_sum_ef_mj, 4000.0, places=1
    )

  def test_extract_flight_arrays_out_of_bounds_coordinates(self):
    # Table containing invalid coordinates: lat > 90, lon > 180
    table = pa.Table.from_arrays(
        [
            pa.array([3.0, 200.0, 4.0]),
            pa.array([50.0, 50.0, 95.0]),
            pa.array([5.0, 6.0, 7.0]),
            pa.array([51.0, 52.0, 53.0]),
            pa.array(["2025-06-15T10:00:00"] * 3),
        ],
        names=["lon_start", "lat_start", "lon_end", "lat_end", "time_start"],
    )
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=self.context.airports_map
    )
    # Out-of-bounds coordinates should be filtered out, leaving only the
    # valid row (3.0, 50.0 -> 5.0, 51.0).
    self.assertIsNotNone(flight_data)
    self.assertEqual(
        flight_data.count,
        1,
        msg=(
            "Out of bounds coordinates not filtered: count is"
            f" {flight_data.count}"
        ),
    )

  def test_attribute_spatial_segments_zero_length(self):
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([4.0], dtype=np.float64),
        lat_start=np.array([50.0], dtype=np.float64),
        lon_end=np.array([4.0], dtype=np.float64),
        lat_end=np.array([50.0], dtype=np.float64),
        ef=np.array([1000.0], dtype=np.float64),
        months=np.array([202506], dtype=np.int32),
        hours=np.array([12], dtype=np.int32),
        dep_countries=np.array(["BE"], dtype=object),
        arr_countries=np.array(["BE"], dtype=object),
    )
    country_accum, fir_accum = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    # Zero length geometry does not intersect, but outgoing attribution
    # is recorded
    be_key = ("Belgium", "BE", 202506, 12)
    self.assertIn(be_key, country_accum)
    self.assertEqual(country_accum[be_key].total_segment_count, 0)
    self.assertAlmostEqual(country_accum[be_key].outgoing_flight_impact, 1000.0)
    self.assertEmpty(fir_accum)

  def test_format_country_and_fir_records(self):
    country_accum = {
        ("Belgium", "BE", 202506, 10): shard_processor.CountryShardAccumulator(
            total_segment_count=1,
            contrail_segment_count=1,
            prop_sum_ef_mj=500.0,
            local_flight_impact=500.0,
            total_flight_impact=500.0,
            outgoing_flight_impact=1000.0,
            total_flight_distance_km=125.5,
            contrail_flight_distance_km=125.5,
        )
    }
    c_records = shard_processor.format_country_records(country_accum)
    self.assertEqual(
        c_records,
        [(
            ("Belgium", "BE", 202506, 10),
            (1, 1, 500.0, 500.0, 500.0, 1000.0, 125.5, 125.5),
        )],
    )

    fir_key = ("BRUSSELS FIR", "EBBU", "BE", "domestic", 202506, 10)
    fir_accum = {
        fir_key: shard_processor.FirShardAccumulator(
            total_segment_count=2,
            contrail_segment_count=1,
            prop_sum_ef_mj=300.0,
            total_flight_distance_km=240.0,
            contrail_flight_distance_km=110.0,
        )
    }
    f_records = shard_processor.format_fir_records(fir_accum)
    self.assertEqual(f_records, [(fir_key, (2, 1, 300.0, 240.0, 110.0))])

  def test_attribute_spatial_segments_internal_flight_distance(self):
    # Segment 0: wholly inside Belgium (ef=1000).
    # Segment 1: wholly inside Belgium (ef=0).
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([3.0, 3.0], dtype=np.float64),
        lat_start=np.array([50.0, 50.0], dtype=np.float64),
        lon_end=np.array([4.0, 4.0], dtype=np.float64),
        lat_end=np.array([50.0, 50.0], dtype=np.float64),
        ef=np.array([1000.0, 0.0], dtype=np.float64),
        months=np.array([202506, 202506], dtype=np.int32),
        hours=np.array([10, 10], dtype=np.int32),
        dep_countries=np.array(["BE", "BE"], dtype=object),
        arr_countries=np.array(["BE", "BE"], dtype=object),
    )
    country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    single_be_km = float(
        geo_utils.haversine_meters(
            np.array([3.0]), np.array([50.0]), np.array([4.0]), np.array([50.0])
        )[0]
        / 1000.0
    )
    be_h10 = country_accum[("Belgium", "BE", 202506, 10)]
    self.assertEqual(be_h10.total_segment_count, 2)
    self.assertEqual(be_h10.contrail_segment_count, 1)
    self.assertAlmostEqual(
        be_h10.total_flight_distance_km, 2.0 * single_be_km, places=5
    )
    self.assertAlmostEqual(
        be_h10.contrail_flight_distance_km, 1.0 * single_be_km, places=5
    )

  def test_attribute_spatial_segments_border_crossing_country_distance(self):
    flight_data = _make_border_crossing_flight_data()
    country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    crossing_km = float(
        geo_utils.haversine_meters(
            np.array([4.0]), np.array([50.0]), np.array([8.0]), np.array([50.0])
        )[0]
        / 1000.0
    )
    be_h11 = country_accum[("Belgium", "BE", 202506, 11)]
    de_h11 = country_accum[("Germany", "DE", 202506, 11)]
    self.assertAlmostEqual(
        be_h11.total_flight_distance_km, 0.5 * crossing_km, places=3
    )
    self.assertAlmostEqual(
        de_h11.total_flight_distance_km, 0.5 * crossing_km, places=3
    )
    self.assertAlmostEqual(
        be_h11.total_flight_distance_km + de_h11.total_flight_distance_km,
        crossing_km,
        places=5,
    )
    self.assertAlmostEqual(
        be_h11.contrail_flight_distance_km + de_h11.contrail_flight_distance_km,
        crossing_km,
        places=5,
    )

  def test_attribute_spatial_segments_border_crossing_non_intersected_dep(self):
    flight_data = _make_border_crossing_flight_data()
    country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    us_h11 = country_accum[("US", "US", 202506, 11)]
    self.assertEqual(us_h11.total_segment_count, 0)
    self.assertAlmostEqual(us_h11.outgoing_flight_impact, 2000.0, places=5)
    self.assertEqual(us_h11.total_flight_distance_km, 0.0)
    self.assertEqual(us_h11.contrail_flight_distance_km, 0.0)

  def test_attribute_spatial_segments_border_crossing_fir_distance(self):
    flight_data = _make_border_crossing_flight_data()
    _, fir_accum = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    crossing_km = float(
        geo_utils.haversine_meters(
            np.array([4.0]), np.array([50.0]), np.array([8.0]), np.array([50.0])
        )[0]
        / 1000.0
    )
    ebbu_h11 = fir_accum[("BRUSSELS", "EBBU", "BE", "domestic", 202506, 11)]
    edgg_h11 = fir_accum[("LANGEN", "EDGG", "DE", "domestic", 202506, 11)]
    self.assertAlmostEqual(
        ebbu_h11.total_flight_distance_km + edgg_h11.total_flight_distance_km,
        crossing_km,
        places=5,
    )
    self.assertAlmostEqual(
        ebbu_h11.contrail_flight_distance_km
        + edgg_h11.contrail_flight_distance_km,
        crossing_km,
        places=5,
    )

  def test_process_shard_end_to_end(self):
    pq_path = os.path.join(self.test_dir, "e2e_shard.parquet")
    table = pa.table({
        "lon_start": [3.0, 5.0],
        "lat_start": [50.0, 50.0],
        "lon_end": [4.0, 7.0],
        "lat_end": [51.0, 51.0],
        "sum_ef_mj": [2000.0, 4000.0],
        "time_start": ["2025-06-15T10:00:00", "2025-06-15T11:00:00"],
        "time_start_tz": [0, 0],
        "departure_airport_icao": ["EBBR", "EBBR"],
        "arrival_airport_icao": ["EDDF", "EDDF"],
    })
    pq.write_table(table, pq_path)

    result = shard_processor.process_shard(
        shard_input=pq_path,
        context=self.context,
        grid_config=_TEST_GRID_CONFIG,
    )
    self.assertIsNotNone(result)
    self.assertNotEmpty(result.grid_slices)
    self.assertNotEmpty(result.country_records)
    self.assertNotEmpty(result.fir_records)
    self.assertEqual(
        (
            result.invalid_coords_count,
            result.invalid_times_count,
            result.zero_len_count,
        ),
        (0, 0, 0),
    )

  def test_extract_flight_arrays_zero_length_segments_dropped(self):
    table = pa.Table.from_arrays(
        [
            pa.array([3.0, 4.0, 5.0]),
            pa.array([50.0, 50.0, 50.0]),
            # row 0 and row 2 are zero-length (start == end)
            pa.array([3.0, 4.5, 5.0]),
            pa.array([50.0, 50.5, 50.0]),
            pa.array([100.0, 200.0, 300.0]),
            pa.array(["2025-06-15T10:00:00"] * 3),
        ],
        names=[
            "lon_start",
            "lat_start",
            "lon_end",
            "lat_end",
            "sum_ef_mj",
            "time_start",
        ],
    )
    flight_data = shard_processor.extract_flight_arrays(
        table, airports_map=self.airports_map
    )
    self.assertIsNotNone(flight_data)
    # Only row 1 has non-zero length
    self.assertEqual(flight_data.count, 1)
    self.assertEqual(flight_data.zero_len_count, 2)
    self.assertEqual(
        (
            flight_data.lon_start[0],
            flight_data.lat_start[0],
            flight_data.lon_end[0],
            flight_data.lat_end[0],
        ),
        (4.0, 50.0, 4.5, 50.5),
    )
    self.assertEqual(flight_data.ef[0], 200.0)

  def test_extract_flight_arrays_all_zero_length_segments_preserves_counter(
      self,
  ):
    all_zero_table = pa.Table.from_arrays(
        [
            pa.array([10.0, 20.0]),
            pa.array([30.0, 40.0]),
            pa.array([10.0, 20.0]),
            pa.array([30.0, 40.0]),
            pa.array(["2025-06-15T10:00:00"] * 2),
        ],
        names=["lon_start", "lat_start", "lon_end", "lat_end", "time_start"],
    )
    all_zero_data = shard_processor.extract_flight_arrays(
        _with_ef(all_zero_table), airports_map=self.airports_map
    )
    self.assertIsNotNone(all_zero_data)
    self.assertEqual(all_zero_data.count, 0)
    self.assertEqual(all_zero_data.zero_len_count, 2)

  def test_extract_flight_arrays_antimeridian_and_polar_zero_length_dropped(
      self,
  ):
    # Tests that antimeridian-coincident (-180, lat) -> (+180, lat) and
    # polar-coincident (lon1, 90) -> (lon2, 90) / (lon1, -90) -> (lon2, -90)
    # segments are recognized as zero-length and dropped.
    table = pa.Table.from_arrays(
        [
            pa.array([10.0, -180.0, -50.0, 30.0]),
            pa.array([20.0, 15.0, 90.0, -90.0]),
            pa.array([10.5, 180.0, 120.0, -140.0]),
            pa.array([20.5, 15.0, 90.0, -90.0]),
            pa.array([100.0, 200.0, 300.0, 400.0]),
            pa.array(["2025-06-15T10:00:00"] * 4),
        ],
        names=[
            "lon_start",
            "lat_start",
            "lon_end",
            "lat_end",
            "sum_ef_mj",
            "time_start",
        ],
    )
    flight_data = shard_processor.extract_flight_arrays(
        table, airports_map=self.airports_map
    )
    self.assertIsNotNone(flight_data)
    # Only row 0 has non-zero length; rows 1, 2, 3 are zero-length
    self.assertEqual(flight_data.count, 1)
    self.assertEqual(flight_data.zero_len_count, 3)
    self.assertEqual(
        (
            flight_data.lon_start[0],
            flight_data.lat_start[0],
            flight_data.lon_end[0],
            flight_data.lat_end[0],
        ),
        (10.0, 20.0, 10.5, 20.5),
    )

  def test_extract_flight_arrays_airport_codes_normalization(self):
    # Tests lowercase, leading/trailing whitespace, and None/empty
    table = pa.Table.from_arrays(
        [
            pa.array([3.0, 4.0, 5.0, 6.0]),
            pa.array([50.0, 50.0, 50.0, 50.0]),
            pa.array([3.5, 4.5, 5.5, 6.5]),
            pa.array([50.5, 50.5, 50.5, 50.5]),
            # dep: lowercase, whitespace, uppercase, unknown
            pa.array(["ebbr", "  EDDF  ", "KJFK", "XXXX"]),
            # arr: whitespace lowercase, uppercase, None, empty
            pa.array([" eddf ", "KJFK", None, ""]),
            pa.array(["2025-06-15T10:00:00"] * 4),
        ],
        names=_AIRPORT_NORM_COLUMNS,
    )
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=self.airports_map
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, 4)
    self.assertEqual(list(flight_data.dep_countries), ["BE", "DE", "US", None])
    self.assertEqual(list(flight_data.arr_countries), ["DE", "US", None, None])

  def test_extract_flight_arrays_timestamp_validation_and_counters(self):
    # Valid/invalid timestamps, NaN/out-of-bounds coords, and counter checks.
    table = pa.Table.from_arrays(
        [
            pa.array([3.0, 4.0, 5.0, np.nan, 200.0]),
            pa.array([50.0, 50.0, 50.0, 50.0, 50.0]),
            pa.array([3.5, 4.5, 5.5, 5.5, 5.5]),
            pa.array([50.5, 50.5, 50.5, 50.5, 50.5]),
            pa.array([
                "2025-06-15T10:30:00",
                "2025-11-20T18:00:00",
                "corrupted-date",
                "2025-01-01T00:00:00",
                "2025-01-01T00:00:00",
            ]),
        ],
        names=["lon_start", "lat_start", "lon_end", "lat_end", "time_start"],
    )
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=self.airports_map
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, 2)
    self.assertEqual(
        (
            flight_data.invalid_coords_count,
            flight_data.invalid_times_count,
            flight_data.zero_len_count,
        ),
        (2, 1, 0),
    )
    self.assertEqual(list(flight_data.months), [202506, 202511])
    self.assertEqual(list(flight_data.hours), [10, 18])

  def test_extract_flight_arrays_null_and_arrow_timestamp_filtering(self):
    # Tests arrow native timestamp column with nulls and valid dates
    ts_array = pa.array(
        [
            1718445600000000,  # 2024-06-15T10:00:00 UTC
            None,  # null timestamp
            1718449200000000,  # 2024-06-15T11:00:00 UTC
        ],
        type=pa.timestamp("us"),
    )
    table = pa.Table.from_arrays(
        [
            pa.array([10.0, 11.0, 12.0]),
            pa.array([20.0, 21.0, 22.0]),
            pa.array([10.5, 11.5, 12.5]),
            pa.array([20.5, 21.5, 22.5]),
            ts_array,
        ],
        names=["lon_start", "lat_start", "lon_end", "lat_end", "time_start"],
    )
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=self.airports_map
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, 2)
    self.assertEqual(
        (flight_data.invalid_times_count, flight_data.invalid_coords_count),
        (1, 0),
    )
    self.assertEqual(list(flight_data.months), [202406, 202406])
    self.assertEqual(list(flight_data.hours), [10, 11])

  def test_extract_flight_arrays_filters_segments_outside_expected_year(self):
    # Segments from years != expected_year must be filtered and counted.
    table = pa.Table.from_arrays(
        [
            pa.array([3.0, 4.0, 5.0]),
            pa.array([50.0, 50.0, 50.0]),
            pa.array([3.5, 4.5, 5.5]),
            pa.array([50.5, 50.5, 50.5]),
            pa.array([
                "2024-01-15T10:00:00",
                "2025-01-15T10:00:00",
                "2026-01-15T10:00:00",
            ]),
        ],
        names=["lon_start", "lat_start", "lon_end", "lat_end", "time_start"],
    )
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=self.airports_map, expected_year=2025
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, 1)
    self.assertEqual(flight_data.outside_year_count, 2)
    self.assertEqual(flight_data.months[0], 202501)

  def test_attribute_spatial_segments_outgoing_belgium(self):
    flight_data = _build_outgoing_comprehensive_flight_data()
    country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    be_acc = country_accum[("Belgium", "BE", 202501, 12)]
    self.assertEqual(
        (be_acc.total_segment_count, be_acc.contrail_segment_count), (1, 1)
    )
    self.assertAlmostEqual(be_acc.prop_sum_ef_mj, 5000.0, places=1)
    self.assertAlmostEqual(be_acc.local_flight_impact, 5000.0, places=1)
    self.assertAlmostEqual(be_acc.total_flight_impact, 5000.0, places=1)
    self.assertAlmostEqual(be_acc.outgoing_flight_impact, 17000.0, places=5)

  def test_attribute_spatial_segments_outgoing_us(self):
    flight_data = _build_outgoing_comprehensive_flight_data()
    country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    us_acc = country_accum[("US", "US", 202501, 12)]
    self.assertEqual(
        (us_acc.total_segment_count, us_acc.contrail_segment_count), (0, 0)
    )
    self.assertEqual(
        (
            us_acc.prop_sum_ef_mj,
            us_acc.local_flight_impact,
            us_acc.total_flight_impact,
        ),
        (0.0, 0.0, 0.0),
    )
    self.assertAlmostEqual(us_acc.outgoing_flight_impact, 3000.0, places=5)
    self.assertEqual(
        (
            us_acc.total_flight_distance_km,
            us_acc.contrail_flight_distance_km,
        ),
        (0.0, 0.0),
    )

  def test_attribute_spatial_segments_outgoing_germany(self):
    flight_data = _build_outgoing_comprehensive_flight_data()
    country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    de_acc = country_accum[("Germany", "DE", 202501, 12)]
    self.assertEqual(
        (de_acc.total_segment_count, de_acc.contrail_segment_count), (4, 4)
    )
    self.assertAlmostEqual(de_acc.prop_sum_ef_mj, 15500.0, places=1)
    self.assertAlmostEqual(de_acc.local_flight_impact, 15500.0, places=1)
    self.assertAlmostEqual(de_acc.total_flight_impact, 15500.0, places=1)
    self.assertAlmostEqual(de_acc.outgoing_flight_impact, 500.0, places=5)

  def test_attribute_spatial_segments_zero_or_nan_ef_offshore_omits_outgoing(
      self,
  ):
    zero_ocean = shard_processor.ExtractedFlightData(
        lon_start=np.array([100.0, 100.0], dtype=np.float64),
        lat_start=np.array([10.0, 10.0], dtype=np.float64),
        lon_end=np.array([101.0, 101.0], dtype=np.float64),
        lat_end=np.array([11.0, 11.0], dtype=np.float64),
        ef=np.array([0.0, float("nan")], dtype=np.float64),
        months=np.array([202501, 202501], dtype=np.int32),
        hours=np.array([12, 12], dtype=np.int32),
        dep_countries=np.array(["US", "US"], dtype=object),
        arr_countries=np.array(["US", "US"], dtype=object),
    )
    c_zero, f_zero = shard_processor.attribute_spatial_segments(
        flight_data=zero_ocean,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    self.assertEmpty(c_zero)
    self.assertEmpty(f_zero)

  def test_attribute_spatial_segments_missing_departure_route(self):
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([4.0], dtype=np.float64),
        lat_start=np.array([50.0], dtype=np.float64),
        lon_end=np.array([8.0], dtype=np.float64),
        lat_end=np.array([50.0], dtype=np.float64),
        ef=np.array([2000.0], dtype=np.float64),
        months=np.array([202501], dtype=np.int32),
        hours=np.array([15], dtype=np.int32),
        dep_countries=np.array([None], dtype=object),
        arr_countries=np.array(["DE"], dtype=object),
    )
    c_mixed, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    be_h15 = c_mixed[("Belgium", "BE", 202501, 15)]
    de_h15 = c_mixed[("Germany", "DE", 202501, 15)]
    self.assertAlmostEqual(be_h15.prop_sum_ef_mj, 1000.0, places=1)
    self.assertEqual(
        (
            be_h15.local_flight_impact,
            be_h15.total_flight_impact,
            be_h15.outgoing_flight_impact,
        ),
        (0.0, 0.0, 0.0),
    )
    self.assertAlmostEqual(de_h15.prop_sum_ef_mj, 1000.0, places=1)
    self.assertEqual(
        (
            de_h15.local_flight_impact,
            de_h15.total_flight_impact,
            de_h15.outgoing_flight_impact,
        ),
        (0.0, 0.0, 0.0),
    )

  def test_attribute_spatial_segments_transit_route(self):
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([3.0], dtype=np.float64),
        lat_start=np.array([50.0], dtype=np.float64),
        lon_end=np.array([5.0], dtype=np.float64),
        lat_end=np.array([50.0], dtype=np.float64),
        ef=np.array([1200.0], dtype=np.float64),
        months=np.array([202501], dtype=np.int32),
        hours=np.array([16], dtype=np.int32),
        dep_countries=np.array(["FR"], dtype=object),
        arr_countries=np.array(["NL"], dtype=object),
    )
    c_mixed, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    be_h16 = c_mixed[("Belgium", "BE", 202501, 16)]
    fr_h16 = c_mixed[("FR", "FR", 202501, 16)]
    self.assertAlmostEqual(be_h16.prop_sum_ef_mj, 1200.0, places=1)
    self.assertAlmostEqual(be_h16.total_flight_impact, 1200.0, places=1)
    self.assertEqual(
        (be_h16.local_flight_impact, be_h16.outgoing_flight_impact), (0.0, 0.0)
    )
    self.assertEqual(fr_h16.total_segment_count, 0)
    self.assertAlmostEqual(fr_h16.outgoing_flight_impact, 1200.0, places=5)

  def test_attribute_spatial_segments_contrail_and_nan_ef_counts(self):
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([3.0, 3.0, 3.0], dtype=np.float64),
        lat_start=np.array([50.0, 50.5, 51.0], dtype=np.float64),
        lon_end=np.array([4.0, 4.0, 4.0], dtype=np.float64),
        lat_end=np.array([50.0, 50.5, 51.0], dtype=np.float64),
        ef=np.array([1500.0, 0.0, float("nan")], dtype=np.float64),
        months=np.array([202501, 202501, 202501], dtype=np.int32),
        hours=np.array([12, 12, 12], dtype=np.int32),
        dep_countries=np.array(["BE", "BE", "BE"], dtype=object),
        arr_countries=np.array(["BE", "BE", "BE"], dtype=object),
    )
    country_accum, fir_accum = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    be_acc = country_accum[("Belgium", "BE", 202501, 12)]
    self.assertEqual(
        (be_acc.total_segment_count, be_acc.contrail_segment_count), (3, 1)
    )
    self.assertAlmostEqual(be_acc.prop_sum_ef_mj, 1500.0, places=3)
    self.assertAlmostEqual(be_acc.outgoing_flight_impact, 1500.0, places=3)

    ebbu_acc = fir_accum[("BRUSSELS", "EBBU", "BE", "domestic", 202501, 12)]
    self.assertEqual(
        (ebbu_acc.total_segment_count, ebbu_acc.contrail_segment_count), (3, 1)
    )
    self.assertAlmostEqual(ebbu_acc.prop_sum_ef_mj, 1500.0, places=3)

  def _make_mock_spatial_index(
      self,
      metadata: list[dict[str, Any]],
      segment_indices: Sequence[int] = (),
      polygon_indices: Sequence[int] = (),
      proportions: Sequence[float] = (),
  ) -> geo_utils.SpatialIndex:
    spatial_index = mock.create_autospec(
        geo_utils.SpatialIndex, instance=True, spec_set=True
    )
    spatial_index.transformer = self.context.country_index.transformer
    spatial_index.metadata = metadata
    spatial_index.query_intersections_batch.return_value = (
        np.asarray(segment_indices, dtype=np.int64),
        np.asarray(polygon_indices, dtype=np.int64),
        np.asarray(proportions, dtype=np.float64),
    )
    return spatial_index

  def _make_belgium_flight_data(
      self,
      departure_countries: Sequence[Any],
      arrival_countries: Sequence[Any],
  ) -> shard_processor.ExtractedFlightData:
    num_segments = len(departure_countries)
    return shard_processor.ExtractedFlightData(
        lon_start=np.full(num_segments, 3.0, dtype=np.float64),
        lat_start=np.full(num_segments, 50.0, dtype=np.float64),
        lon_end=np.full(num_segments, 4.0, dtype=np.float64),
        lat_end=np.full(num_segments, 50.0, dtype=np.float64),
        ef=np.full(num_segments, 100.0, dtype=np.float64),
        months=np.full(num_segments, 202501, dtype=np.int32),
        hours=np.full(num_segments, 12, dtype=np.int32),
        dep_countries=np.array(departure_countries, dtype=object),
        arr_countries=np.array(arrival_countries, dtype=object),
    )

  def test_encode_iso_column_and_entity_tables(self):
    flight_data = self._make_belgium_flight_data(
        [None, "", "   ", " be ", "BE", "de", float("nan"), "be"],
        ["BE", "BE", "BE", "de", "   ", " be ", "BE", "DE"],
    )
    country_index = self._make_mock_spatial_index(
        metadata=[
            {"id": "be", "name": "Belgium Poly 1"},
            {"id": "BE", "name": "Belgium Poly 2"},
            {"id": "-99", "name": "Kosovo"},
            {"id": "-99", "name": "Somaliland"},
        ],
        segment_indices=[0, 3, 3, 4, 5, 7],
        polygon_indices=[0, 0, 1, 2, 3, 1],
        proportions=[1.0, 0.6, 0.6, 1.0, 1.0, 1.0],
    )
    country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=country_index,
        country_names_by_iso={"BE": "Belgium", "DE": "Germany", "-99": "Other"},
        fir_index=self._make_mock_spatial_index(metadata=[]),
    )
    expected_keys = {
        ("Belgium", "BE", 202501, 12),
        ("Germany", "DE", 202501, 12),
        ("Kosovo", "-99", 202501, 12),
        ("Somaliland", "-99", 202501, 12),
    }
    self.assertEqual(set(country_accum.keys()), expected_keys)
    belgium_metrics = country_accum[("Belgium", "BE", 202501, 12)]
    self.assertEqual(belgium_metrics.total_segment_count, 3)
    self.assertAlmostEqual(belgium_metrics.local_flight_impact, 200.0, places=3)
    self.assertAlmostEqual(
        belgium_metrics.outgoing_flight_impact, 300.0, places=3
    )

  def test_build_fir_entity_table_normalizes_and_deduplicates_polygons(self):
    flight_data = self._make_belgium_flight_data(["BE"], ["BE"])
    first_properties = {
        "firname": "BRUSSELS FIR",
        "fir_icao": "ebbu",
        "fir_to_country_iso2s": " BE ",
        "type": "Domestic",
    }
    second_properties = {
        "firname": "BRUSSELS FIR",
        "fir_icao": "EBBU",
        "fir_to_country_iso2s": "BE",
        "type": "domestic",
    }
    fir_index = self._make_mock_spatial_index(
        metadata=[
            {"id": "ebbu", "name": "B1", "properties": first_properties},
            {"id": "EBBU", "name": "B2", "properties": second_properties},
        ],
        segment_indices=[0, 0],
        polygon_indices=[0, 1],
        proportions=[0.6, 0.6],
    )
    _, fir_accum = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self._make_mock_spatial_index(metadata=[]),
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=fir_index,
    )
    brussels_key = ("BRUSSELS", "EBBU", "BE", "domestic", 202501, 12)
    self.assertEqual(list(fir_accum.keys()), [brussels_key])
    brussels_metrics = fir_accum[brussels_key]
    self.assertEqual(brussels_metrics.total_segment_count, 1)
    self.assertAlmostEqual(brussels_metrics.prop_sum_ef_mj, 100.0, places=3)

  def test_dedup_and_clamp_intersections(self):
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([177.0, 3.0], dtype=np.float64),
        lat_start=np.array([0.0, 50.0], dtype=np.float64),
        lon_end=np.array([-178.0, 4.0], dtype=np.float64),
        lat_end=np.array([0.0, 50.0], dtype=np.float64),
        ef=np.array([1000.0, 1000.0], dtype=np.float64),
        months=np.array([202501, 202501], dtype=np.int32),
        hours=np.array([10, 12], dtype=np.int32),
        dep_countries=np.array(["BE", "BE"], dtype=object),
        arr_countries=np.array(["DE", "DE"], dtype=object),
    )
    country_index = self._make_mock_spatial_index(
        metadata=[
            {"id": "BE", "name": "Belgium 1"},
            {"id": "BE", "name": "Belgium 2"},
            {"id": "DE", "name": "Germany"},
        ],
        segment_indices=[1, 0, 0, 2, 0],
        polygon_indices=[0, 0, 1, 1, 2],
        proportions=[0.5, 0.7, 0.6, 0.5, 0.25],
    )
    c_acc, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self._make_mock_spatial_index(metadata=[]),
    )
    b_h10 = c_acc[("Belgium", "BE", 202501, 10)].prop_sum_ef_mj
    b_h12 = c_acc[("Belgium", "BE", 202501, 12)].prop_sum_ef_mj
    g_h12 = c_acc[("Germany", "DE", 202501, 12)].prop_sum_ef_mj
    self.assertAlmostEqual(b_h10, 500.0, places=4)
    self.assertAlmostEqual(b_h12, 800.0, places=4)
    self.assertAlmostEqual(g_h12, 200.0, places=4)

  def test_dedup_and_clamp_intersections_shared_border_normalisation(self):
    # Segment 0 lies along the shared border (lon=6.0) of Belgium and Germany,
    # where both polygons report full coverage (proportion 1.0 -> normalized to
    # 0.5 each).
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([6.0], dtype=np.float64),
        lat_start=np.array([50.0], dtype=np.float64),
        lon_end=np.array([6.0], dtype=np.float64),
        lat_end=np.array([51.0], dtype=np.float64),
        ef=np.array([1000.0], dtype=np.float64),
        months=np.array([202501], dtype=np.int32),
        hours=np.array([12], dtype=np.int32),
        dep_countries=np.array(["BE"], dtype=object),
        arr_countries=np.array(["DE"], dtype=object),
    )
    country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    belgium_ef_mj = country_accum[("Belgium", "BE", 202501, 12)].prop_sum_ef_mj
    germany_ef_mj = country_accum[("Germany", "DE", 202501, 12)].prop_sum_ef_mj
    self.assertAlmostEqual(belgium_ef_mj, 500.0, places=4)
    self.assertAlmostEqual(germany_ef_mj, 500.0, places=4)
    self.assertAlmostEqual(belgium_ef_mj + germany_ef_mj, 1000.0, places=4)

  def _attribute_multipolygon_test_segments(
      self,
  ) -> tuple[dict[Any, Any], dict[Any, Any]]:
    c_idx, f_idx = _create_multipolygon_test_spatial_indices(self.test_dir)
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([3.0, 6.0], dtype=np.float64),
        lat_start=np.array([50.0, 50.0], dtype=np.float64),
        lon_end=np.array([5.0, 10.0], dtype=np.float64),
        lat_end=np.array([50.0, 50.0], dtype=np.float64),
        ef=np.array([1000.0, 2000.0], dtype=np.float64),
        months=np.array([202501, 202501], dtype=np.int32),
        hours=np.array([12, 12], dtype=np.int32),
        dep_countries=np.array(["BE", "DE"], dtype=object),
        arr_countries=np.array(["BE", "DE"], dtype=object),
    )
    return shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=c_idx,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=f_idx,
    )

  def test_attribute_spatial_segments_multipolygon_country_dedup(self):
    c_acc, _ = self._attribute_multipolygon_test_segments()
    be_row = c_acc[("Belgium", "BE", 202501, 12)]
    de_row = c_acc[("Germany", "DE", 202501, 12)]
    self.assertEqual(
        (
            be_row.total_segment_count,
            be_row.contrail_segment_count,
            de_row.total_segment_count,
            de_row.contrail_segment_count,
        ),
        (1, 1, 1, 1),
    )
    self.assertAlmostEqual(be_row.prop_sum_ef_mj, 1000.0, places=2)
    self.assertAlmostEqual(be_row.outgoing_flight_impact, 1000.0, places=2)
    self.assertAlmostEqual(de_row.prop_sum_ef_mj, 2000.0, places=1)
    self.assertAlmostEqual(de_row.outgoing_flight_impact, 2000.0, places=2)

  def test_attribute_spatial_segments_multipolygon_fir_dedup(self):
    _, f_acc = self._attribute_multipolygon_test_segments()
    ebbu_row = f_acc[("BRUSSELS", "EBBU", "BE", "domestic", 202501, 12)]
    self.assertEqual(ebbu_row.total_segment_count, 1)
    self.assertEqual(ebbu_row.contrail_segment_count, 1)
    self.assertAlmostEqual(ebbu_row.prop_sum_ef_mj, 1000.0, places=2)

  def test_attribute_spatial_segments_sentinel_minus99_territories(self):
    sentinel_idx = geo_utils.SpatialIndex(
        _make_sentinel_features(), id_key="iso_a2", name_key="name"
    )
    flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([20.5, 45.0], dtype=np.float64),
        lat_start=np.array([43.0, 9.0], dtype=np.float64),
        lon_end=np.array([21.5, 47.0], dtype=np.float64),
        lat_end=np.array([43.0, 9.0], dtype=np.float64),
        ef=np.array([400.0, 900.0], dtype=np.float64),
        months=np.array([202501, 202501], dtype=np.int32),
        hours=np.array([10, 10], dtype=np.int32),
        dep_countries=np.array(["BE", "DE"], dtype=object),
        arr_countries=np.array(["DE", "BE"], dtype=object),
    )
    c_acc, _ = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=sentinel_idx,
        country_names_by_iso={"-99": "ShouldNotBeUsed", "BE": "Belgium"},
        fir_index=self.context.fir_index,
    )
    self.assertIn(("Kosovo", "-99", 202501, 10), c_acc)
    self.assertIn(("Somaliland", "-99", 202501, 10), c_acc)
    self.assertAlmostEqual(
        c_acc[("Kosovo", "-99", 202501, 10)].prop_sum_ef_mj, 400.0, places=2
    )
    self.assertAlmostEqual(
        c_acc[("Somaliland", "-99", 202501, 10)].prop_sum_ef_mj, 900.0, places=2
    )

  def test_attribute_spatial_segments_multi_month_hour_interleaved_parity(self):
    flight_data = _make_interleaved_parity_flight_data()
    batch_c, batch_f = shard_processor.attribute_spatial_segments(
        flight_data=flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    expected_c, expected_f = _compute_single_segment_expected_accumulators(
        flight_data, self.context
    )
    self.assertEqual(set(batch_c.keys()), set(expected_c.keys()))
    _assert_spatial_accumulators_match_expected(batch_c, expected_c)
    self.assertEqual(set(batch_f.keys()), set(expected_f.keys()))
    _assert_spatial_accumulators_match_expected(batch_f, expected_f)

  def test_extract_airport_countries_dictionary_encoded_and_multi_chunk(self):
    # Verify that on a 400-row table with only a few unique airport strings,
    # airports_map is queried only once per unique dictionary entry rather than
    # once per row.
    num_rows = 400
    departure_column = pa.chunked_array([
        pa.array([" ebbr ", "EDDF", None, "XXXX"] * (num_rows // 8)),
        pa.array([" ebbr ", "EDDF", "   ", "KJFK"] * (num_rows // 8)),
    ])
    arrival_column = pa.DictionaryArray.from_arrays(
        pa.array([0, 1, 2, None] * (num_rows // 4), type=pa.int8()),
        pa.array([" kjfk ", "ebbr", ""]),
    )
    table = pa.table({
        "lon_start": [3.0] * num_rows,
        "lat_start": [50.0] * num_rows,
        "lon_end": [4.0] * num_rows,
        "lat_end": [51.0] * num_rows,
        "time_start": ["2025-06-15T10:00:00"] * num_rows,
        "departure_airport_icao": departure_column,
        "arrival_airport_icao": arrival_column,
    })
    counting_map = _CountingAirportsMap(self.airports_map)
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=counting_map
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, num_rows)
    # 4 non-blank unique entries in departure_column + 2 in arrival_column.
    self.assertLessEqual(len(counting_map.looked_up_keys), 10)
    self.assertEqual(
        (
            list(flight_data.dep_countries[:4]),
            list(
                flight_data.dep_countries[num_rows // 2 + 2 : num_rows // 2 + 4]
            ),
        ),
        (["BE", "DE", None, None], [None, "US"]),
    )
    self.assertEqual(
        list(flight_data.arr_countries[:4]), ["US", "BE", None, None]
    )

  def test_extract_airport_countries_binary_codes_and_null_column(self):
    binary_table = pa.table({
        "lon_start": [3.0, 4.0, 5.0],
        "lat_start": [50.0, 50.0, 50.0],
        "lon_end": [3.5, 4.5, 5.5],
        "lat_end": [50.5, 50.5, 50.5],
        "time_start": ["2025-06-15T10:00:00"] * 3,
        "dep_icao": pa.array([b"ebbr", b"  eddf ", b"   "], type=pa.binary()),
        "arr_icao": pa.array([None, None, None], type=pa.null()),
    })
    binary_data = shard_processor.extract_flight_arrays(
        _with_ef(binary_table), airports_map=self.airports_map
    )
    self.assertIsNotNone(binary_data)
    self.assertEqual(list(binary_data.dep_countries), ["BE", "DE", None])
    self.assertEqual(list(binary_data.arr_countries), [None, None, None])

  def _make_invalid_row_airport_table(self) -> pa.Table:
    # Row 0: valid (EBBR -> EDDF)
    # Row 1: invalid coordinate (lon=999.0), carries airport "DROPPED_COORD"
    # Row 2: outside expected_year (2024), carries airport "DROPPED_YEAR"
    # Row 3: zero-length segment, carries airport "DROPPED_ZERO"
    return pa.table({
        "lon_start": [3.0, 999.0, 4.0, 5.0],
        "lat_start": [50.0, 50.0, 50.0, 50.0],
        "lon_end": [4.0, 5.0, 5.0, 5.0],
        "lat_end": [51.0, 51.0, 51.0, 50.0],
        "time_start": [
            "2025-06-15T10:00:00",
            "2025-06-15T10:00:00",
            "2024-06-15T10:00:00",
            "2025-06-15T10:00:00",
        ],
        "dep_icao": ["EBBR", "DROPPED_COORD", "DROPPED_YEAR", "DROPPED_ZERO"],
        "arr_icao": ["EDDF", "DROPPED_COORD", "DROPPED_YEAR", "DROPPED_ZERO"],
    })

  def test_extract_flight_arrays_skips_airport_mapping_for_invalid_rows(self):
    table = self._make_invalid_row_airport_table()
    counting_map = _CountingAirportsMap(self.airports_map)
    flight_data = shard_processor.extract_flight_arrays(
        _with_ef(table), airports_map=counting_map, expected_year=2025
    )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, 1)
    self.assertEqual(list(flight_data.dep_countries), ["BE"])
    self.assertEqual(list(flight_data.arr_countries), ["DE"])
    # None of the airport codes from invalid/dropped rows should be looked up.
    self.assertEqual(counting_map.looked_up_keys, ["EBBR", "EDDF"])

  def test_extract_flight_arrays_skips_airport_mapping_when_all_rows_invalid(
      self,
  ):
    table = self._make_invalid_row_airport_table()
    empty_counting_map = _CountingAirportsMap(self.airports_map)
    empty_data = shard_processor.extract_flight_arrays(
        _with_ef(table.slice(1, 3)),
        airports_map=empty_counting_map,
        expected_year=2025,
    )
    self.assertIsNotNone(empty_data)
    self.assertEqual(empty_data.count, 0)
    self.assertEmpty(empty_counting_map.looked_up_keys)

  def test_extract_airport_countries_fallback_memoizes_unique_codes(self):
    num_rows = 120
    table = pa.table({
        "lon_start": [3.0] * num_rows,
        "lat_start": [50.0] * num_rows,
        "lon_end": [4.0] * num_rows,
        "lat_end": [51.0] * num_rows,
        "time_start": ["2025-06-15T10:00:00"] * num_rows,
        "dep_icao": [" ebbr ", "ebbr", " ebbr ", "EDDF", None, ""] * 20,
    })
    counting_map = _CountingAirportsMap(self.airports_map)
    with mock.patch.object(
        shard_processor.pa.types,
        "is_dictionary",
        side_effect=pa.ArrowInvalid("forced fallback"),
        autospec=True,
        spec_set=True,
    ):
      flight_data = shard_processor.extract_flight_arrays(
          _with_ef(table), airports_map=counting_map
      )
    self.assertIsNotNone(flight_data)
    self.assertEqual(flight_data.count, num_rows)
    # Only 3 non-empty unique strings (" ebbr ", "ebbr", "EDDF") -> 3 lookups.
    self.assertEqual(counting_map.looked_up_keys, ["EBBR", "EBBR", "EDDF"])
    self.assertEqual(
        list(flight_data.dep_countries[:6]),
        ["BE", "BE", "BE", "DE", None, None],
    )

  def test_encode_iso_column_large_array_arrow_fast_path_parity(self):
    departure_pattern = [" be ", "BE", "de", None, "", "   ", "-99", "US"]
    arrival_pattern = ["DE", "be", "BE", "US", "DE", "BE", "BE", None]
    small_data = self._make_belgium_flight_data(
        departure_pattern, arrival_pattern
    )
    large_data = self._make_belgium_flight_data(
        departure_pattern * 20, arrival_pattern * 20
    )
    small_country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=small_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    large_country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=large_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    self.assertEqual(
        set(large_country_accum.keys()), set(small_country_accum.keys())
    )
    belgium_key = ("Belgium", "BE", 202501, 12)
    np.testing.assert_allclose(
        large_country_accum[belgium_key].to_metrics_tuple(),
        np.asarray(small_country_accum[belgium_key].to_metrics_tuple()) * 20.0,
    )

  def test_encode_iso_column_large_all_null_array_fast_path(self):
    null_route_data = self._make_belgium_flight_data([None] * 80, [None] * 80)
    country_accum, _ = shard_processor.attribute_spatial_segments(
        flight_data=null_route_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    belgium_metrics = country_accum[("Belgium", "BE", 202501, 12)]
    self.assertEqual(belgium_metrics.total_segment_count, 80)
    self.assertAlmostEqual(belgium_metrics.prop_sum_ef_mj, 8000.0, places=3)
    self.assertEqual(belgium_metrics.local_flight_impact, 0.0)
    self.assertEqual(belgium_metrics.outgoing_flight_impact, 0.0)

  def test_extract_flight_arrays_sanitizes_non_finite_ef(self):
    table = _make_non_finite_ef_table()
    flight_data = shard_processor.extract_flight_arrays(
        table, airports_map=self.airports_map
    )
    self.assertIsNotNone(flight_data)
    np.testing.assert_array_equal(
        flight_data.ef, np.array([0.0, 0.0, 0.0, 0.0, 600.0], dtype=np.float64)
    )

  def test_attribute_spatial_segments_sanitizes_non_finite_ef(self):
    raw_flight_data = shard_processor.ExtractedFlightData(
        lon_start=np.array([3.0] * 5, dtype=np.float64),
        lat_start=np.array([50.0] * 5, dtype=np.float64),
        lon_end=np.array([4.0] * 5, dtype=np.float64),
        lat_end=np.array([51.0] * 5, dtype=np.float64),
        ef=np.array(
            [float("nan"), float("inf"), float("-inf"), 0.0, 600.0],
            dtype=np.float64,
        ),
        months=np.array([202506] * 5, dtype=np.int32),
        hours=np.array([10] * 5, dtype=np.int32),
        dep_countries=np.array(["BE"] * 5, dtype=object),
        arr_countries=np.array(["DE"] * 5, dtype=object),
    )
    c_accum, f_accum = shard_processor.attribute_spatial_segments(
        raw_flight_data,
        country_index=self.context.country_index,
        country_names_by_iso=self.context.country_names_by_iso,
        fir_index=self.context.fir_index,
    )
    be_vals = c_accum[("Belgium", "BE", 202506, 10)]
    self.assertEqual(be_vals.total_segment_count, 5)
    self.assertEqual(be_vals.contrail_segment_count, 1)
    self.assertAlmostEqual(be_vals.prop_sum_ef_mj, 600.0, places=4)
    self.assertAlmostEqual(be_vals.outgoing_flight_impact, 600.0, places=4)
    _assert_fir_metrics_finite(self, f_accum)

  def test_process_shard_sanitizes_non_finite_ef(self):
    pq_path = os.path.join(self.test_dir, "non_finite_ef.parquet")
    pq.write_table(_make_non_finite_ef_table(), pq_path)
    proc_result = self._process(pq_path)
    self.assertEqual(proc_result.outcome, shard_processor.ShardOutcome.OK)
    impact_slices = [
        slice_values
        for (g_type, _, _), (_, _, _, slice_values) in proc_result.grid_slices
        if g_type == "impact"
    ]
    self.assertLen(impact_slices, 1)
    self.assertAlmostEqual(float(np.sum(impact_slices[0])), 600.0, delta=0.5)


if __name__ == "__main__":
  absltest.main()
