"""Unit tests for the Regional Contrails Dashboard API server."""

import asyncio
from collections import abc
from concurrent import futures
import gzip
import json
import math
import os
import tempfile
import types
from typing import Any, TypeAlias
import unittest
from unittest import mock

from absl import flags
from absl.testing import absltest
from absl.testing import parameterized
import fastapi
from fastapi import testclient
import pydantic

from common import artifact_io
from common import constants
from common import fs
from server import data_loader
from server import metrics
from server import models
from server import server
from server import static_assets
from server import testdata_fixture


def _publish_run_dir(root, year, run_id="test-run"):
  """Publishes a minimal run for `year` under `root`.

  Points `{root}/{year}/{DATASET}/latest.json` at the run and creates the run
  directory with an empty per-country export unless one exists, since that
  file is what makes a year available to every year-scoped endpoint.

  Args:
    root: Data root, to assign to `data_loader.DATA_DIR`.
    year: Dataset year directory name.
    run_id: Run directory name under `runs/`.

  Returns:
    The run directory.
  """
  artifact_io.write_run_pointer(
      root, year=int(year), dataset=testdata_fixture.DATASET, run_id=run_id
  )
  run_dir = artifact_io.run_output_dir(
      root, year=int(year), dataset=testdata_fixture.DATASET, run_id=run_id
  )
  os.makedirs(run_dir, exist_ok=True)
  country_export = os.path.join(run_dir, constants.COUNTRY_EXPORT_FILENAME)
  if not os.path.exists(country_export):
    with open(country_export, "w", encoding="utf-8") as f:
      json.dump([], f)
  return run_dir


# The 24x12 matrices of a published temporal bundle row (see
# `metric_derivations._make_empty_temporal_bundle`).
_TEMPORAL_BUNDLE_FIELDS = (
    "co2e_gwp20_tonnes",
    "co2e_gwp50_tonnes",
    "co2e_gwp100_tonnes",
    "contrail_segment_count",
    "segment_count",
    "total_flight_distance_km",
    "contrail_flight_distance_km",
)


_TEMPORAL_INT_BUNDLE_FIELDS = frozenset({
    "contrail_segment_count",
    "segment_count",
})


def _temporal_bundle(identity, cells):
  """Builds one published temporal row: identity fields plus 24x12 matrices.

  Args:
    identity: Non-matrix fields, e.g. `{"iso_a2": "CH", "name": "Switzerland"}`.
    cells: `{(hour, month): {field: value}}` with `month` in 1-12. Unlisted
      cells and fields are zero.

  Returns:
    The row, as the pipeline publishes it.
  """
  row: dict[str, Any] = dict(identity)
  for field in _TEMPORAL_BUNDLE_FIELDS:
    zero = 0 if field in _TEMPORAL_INT_BUNDLE_FIELDS else 0.0
    row[field] = [[zero] * 12 for _ in range(24)]
  for (hour, month), values in cells.items():
    for field, value in values.items():
      row[field][hour][month - 1] = value
  return row


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()
  testdata_dir = tempfile.TemporaryDirectory()
  unittest.addModuleCleanup(testdata_dir.cleanup)
  for relative_path, payload in testdata_fixture.build_fixture().items():
    path = os.path.join(testdata_dir.name, relative_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fixture_file:
      json.dump(payload, fixture_file)
  unittest.enterModuleContext(
      mock.patch.object(data_loader, "DATA_DIR", testdata_dir.name)
  )
  unittest.enterModuleContext(
      mock.patch.object(
          data_loader, "_active_dataset", testdata_fixture.DATASET
      )
  )
  data_loader.configure_dataset(testdata_fixture.DATASET)
  data_loader.clear_all_caches()
  unittest.addModuleCleanup(data_loader.clear_all_caches)


_CountryLoader: TypeAlias = abc.Callable[
    [int | str, str | None], data_loader.CountryYearData
]


def _country_loader_failing_for(
    failing_year: int | str, real_loader: _CountryLoader
) -> _CountryLoader:
  """Wraps `real_loader` so that `failing_year` has an unreadable artifact."""

  def _loader(
      year: int | str, dataset: str | None = None
  ) -> data_loader.CountryYearData:
    if int(year) == int(failing_year):
      raise data_loader.ArtifactUnavailableError(f"unreadable {year}")
    return real_loader(year, dataset)

  return _loader


class DashboardTestBase(parameterized.TestCase):
  """Shared fixture: a test client over a cache-cleared server."""

  def setUp(self):
    super().setUp()
    self.client = testclient.TestClient(server.app)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)

  def publish_dataset(self, year, artifacts):
    """Publishes a dataset run for `year` under a new data root.

    An empty per-country export is added unless given, since that file is what
    makes a year available.

    Args:
      year: Dataset year directory name.
      artifacts: Mapping of artifact filename to JSON payload.

    Returns:
      The data root, to assign to `data_loader.DATA_DIR`.
    """
    root = self.create_tempdir().full_path
    dataset_dir = _publish_run_dir(root, year)
    os.makedirs(dataset_dir, exist_ok=True)
    artifacts = {constants.COUNTRY_EXPORT_FILENAME: [], **artifacts}
    for filename, payload in artifacts.items():
      with open(
          os.path.join(dataset_dir, filename), "w", encoding="utf-8"
      ) as f:
        json.dump(payload, f)
    return root

  def assertYearOverYear(self, points, year, expected_pct, places=1):
    """Asserts the change into `year` from the preceding point.

    The server used to ship yoy_percent/yoy_text/yoy_direction, which no client
    ever read, and whose `prev_value` tracking silently spanned two years across
    a data gap. The series is now raw values only, so the expectation is checked
    here instead.

    Args:
      points: Time series points, each carrying a "year" and a "value".
      year: The year whose change from its predecessor is under test.
      expected_pct: Expected percentage change, where 10.0 means +10%.
      places: Decimal places of tolerance for the comparison.
    """
    by_year = {point["year"]: point["value"] for point in points}
    years = sorted(by_year)
    index = years.index(year)
    self.assertGreater(index, 0, f"{year} has no preceding point.")
    previous = by_year[years[index - 1]]
    current = by_year[year]
    self.assertIsNotNone(previous)
    self.assertIsNotNone(current)
    self.assertAlmostEqual(
        ((current - previous) / abs(previous)) * 100.0, expected_pct, places
    )


class CountryEndpointsTest(DashboardTestBase):
  """Tests the /country and /countries endpoints."""

  def test_get_country_by_iso_standard(self):
    response = self.client.get("/country/2025/BE")
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertEqual(data.get("iso_a2"), "BE")
    self.assertEqual(data.get("name"), "Belgium")

  def test_get_country_by_iso_case_insensitive(self):
    response = self.client.get("/country/2025/be")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json().get("iso_a2"), "BE")

  def test_get_country_by_iso_duplicate_identifier(self):
    # -99 is shared by Kosovo, N. Cyprus, and Somaliland in Natural
    # Earth shapefiles. The endpoint must return any matching country
    # row cleanly, without a 500.
    response = self.client.get("/country/2025/-99")
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertEqual(data.get("iso_a2"), "-99")
    self.assertIn(data.get("name"), ("Kosovo", "N. Cyprus", "Somaliland"))

  def test_list_countries_keeps_every_duplicate_iso_row(self):
    # The three -99 territories share one identifier; the list must not
    # collapse them into one row.
    response = self.client.get("/countries/2025")
    self.assertEqual(response.status_code, 200)
    rows = [
        item_row
        for item_row in response.json()
        if item_row.get("iso_a2") == "-99"
    ]
    self.assertGreaterEqual(len(rows), 3)

  def test_get_country_not_found(self):
    response = self.client.get("/country/2025/ZZ_UNKNOWN")
    self.assertEqual(response.status_code, 404)

  def test_invalid_iso_format(self):
    response = self.client.get("/country/2025/INVALID$$$CODE!@")
    self.assertEqual(response.status_code, 400)

  @parameterized.parameters("2022", "2023", "2024", "2025")
  def test_get_country_by_iso_and_year_success_belgium(self, year):
    response = self.client.get(f"/country/{year}/BE")
    self.assertEqual(response.status_code, 200, f"Failed for year {year}")
    data = response.json()
    self.assertEqual((data.get("iso_a2"), data.get("name")), ("BE", "Belgium"))
    self.assertIn("co2e_gwp50_tonnes_per_capita", data)
    self.assertIn("population", data)
    self.assertIn("fir_total_co2e_gwp50_tonnes", data)

  @parameterized.parameters("2022", "2023", "2024", "2025")
  def test_get_country_by_iso_and_year_success_austria(self, year):
    res_at = self.client.get(f"/country/{year}/AT")
    self.assertEqual(res_at.status_code, 200, f"Failed for year {year}")
    data_at = res_at.json()
    self.assertEqual(
        (data_at.get("fir_total_firname"), data_at.get("fir_total_fir_id")),
        (["WIEN"], ["LOVV_WIEN"]),
    )
    self.assertGreater(data_at.get("fir_total_co2e_gwp50_tonnes", 0), 0)
    self.assertIsNotNone(data_at.get("outgoing_flight_impact_rank"))
    self.assertIsNotNone(data_at.get("fir_total_co2e_gwp50_tonnes_rank"))

  def test_get_country_2025_includes_world_avg_fields(self):
    response = self.client.get("/country/2025/BE")
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertIn("co2e_gwp50_per_capita_vs_world_avg", data)
    self.assertIn("outgoing_flight_co2e_gwp50_per_capita_vs_world_avg", data)
    self.assertIn("fir_total_co2e_gwp50_per_capita_vs_world_avg", data)
    self.assertIsNotNone(data.get("co2e_gwp50_per_capita_vs_world_avg"))

  @parameterized.parameters("2021", "2020", "invalid_year")
  def test_get_country_by_iso_and_year_unsupported(self, year):
    # Year outside 2022-2025 should return 400 Bad Request
    response = self.client.get(f"/country/{year}/BE")
    self.assertEqual(response.status_code, 400)

  def test_get_country_unsupported_year_error_lists_available_years(self):
    response = self.client.get("/country/2021/BE")
    self.assertEqual(response.status_code, 400)
    self.assertIn("Available years are", response.json().get("detail", ""))

  def test_get_country_by_iso_and_year_not_found(self):
    # Valid year but country not found should return 404
    response = self.client.get("/country/2024/ZZ")
    self.assertEqual(response.status_code, 404)

  def test_get_country_by_iso(self):
    response = self.client.get("/country/2025/CH")
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertEqual(data.get("name"), "Switzerland")
    self.assertEqual(data.get("iso_a2"), "CH")

  @parameterized.parameters("Switzerland", "switzerland")
  def test_get_country_by_name_returns_404(self, name):
    response = self.client.get(f"/country/2025/{name}")
    self.assertEqual(response.status_code, 404)

  @parameterized.parameters("756", "276", "840", "56", "99999")
  def test_get_country_by_numeric_iso_returns_404(self, numeric):
    self.assertEqual(
        self.client.get(f"/country/2025/{numeric}").status_code,
        404,
    )

  def test_list_countries_exhaustive(self):
    response = self.client.get("/countries/2025")
    self.assertEqual(response.status_code, 200)
    rows = response.json()
    self.assertGreaterEqual(len(rows), 10)
    self.assertIn("DE", [row.get("iso_a2") for row in rows])

  def test_years_endpoint(self):
    resp = self.client.get("/years")
    self.assertEqual(resp.status_code, 200)
    data = resp.json()
    self.assertEqual(data, {"years": [2022, 2023, 2024, 2025]})

  def test_country_metrics_response_model(self):
    resp = models.CountryMetricsResponse(
        name="Testland",
        iso_a2="TL",
        iso_a3="TLD",
        co2e_gwp50_tonnes=100.0,
    )
    self.assertEqual(resp.name, "Testland")
    self.assertEqual(resp.iso_a2, "TL")
    self.assertEqual(resp.iso_a3, "TLD")
    self.assertEqual(resp.co2e_gwp50_tonnes, 100.0)
    for model_cls in (
        models.CountryMetricsResponse,
        models.FirManagingCountry,
        models.FirMetricsResponse,
        models.AvailableYearsResponse,
        models.RankingItem,
        models.RankingsResponse,
        models.TimeSeriesPoint,
        models.TimeSeriesResponse,
        models.GridAvailabilityResponse,
        models.TemporalMatrixResponse,
    ):
      with self.subTest(model=model_cls.__name__):
        with self.assertRaises(pydantic.ValidationError):
          model_cls.model_validate({"extra_custom_metric": 42.0})

  @parameterized.named_parameters(
      (
          "fir_metrics_response",
          models.FirMetricsResponse,
          {
              "countries": [{
                  "iso_a2": "CH",
                  "name": "Switzerland",
                  "extra_key": "rejected",
              }]
          },
      ),
      (
          "ranking_item",
          models.RankingItem,
          {
              "name": "FIR ZURICH",
              "countries": [{
                  "iso_a2": "CH",
                  "name": "Switzerland",
                  "extra_key": "rejected",
              }],
          },
      ),
  )
  def test_fir_managing_country_forbids_extra_fields(
      self, model_cls: Any, payload: dict[str, Any]
  ):
    with self.assertRaisesRegex(pydantic.ValidationError, r"extra_key"):
      model_cls.model_validate(payload)

  def test_fixture_country_and_fir_keys_match_response_models(self):
    self.assertEqual(
        set(models.CountryMetricsResponse.model_fields),
        constants.PUBLISHED_COUNTRY_ROW_KEYS,
    )
    self.assertEqual(
        set(models.FirMetricsResponse.model_fields) - {"countries"},
        constants.PUBLISHED_FIR_ROW_KEYS,
    )
    fixture = testdata_fixture.build_fixture()
    run_id = testdata_fixture.run_id_for(2025)
    run_prefix = (
        f"2025/{testdata_fixture.DATASET}/{constants.RUNS_DIR}/{run_id}"
    )
    country_rows = fixture[f"{run_prefix}/{constants.COUNTRY_EXPORT_FILENAME}"]
    self.assertNotEmpty(country_rows)
    for row in country_rows:
      self.assertEqual(set(row), constants.PUBLISHED_COUNTRY_ROW_KEYS)
      models.CountryMetricsResponse.model_validate(row)
    fir_rows = fixture[f"{run_prefix}/{constants.FIR_EXPORT_FILENAME}"]
    self.assertNotEmpty(fir_rows)
    for row in fir_rows:
      self.assertEqual(set(row), constants.PUBLISHED_FIR_ROW_KEYS)
      models.FirMetricsResponse.model_validate(row)
    temporal_rows = fixture[
        f"{run_prefix}/{constants.COUNTRY_TEMPORAL_EXPORT_FILENAME}"
    ]
    self.assertNotEmpty(temporal_rows)
    for row in temporal_rows:
      self.assertLessEqual(
          set(row), set(models.TemporalMatrixResponse.model_fields)
      )
      self.assertLessEqual(
          set(row), set(constants.TemporalBundleRow.__annotations__)
      )

  def test_single_country_endpoint_omits_energy_forcing_fields(self):
    country = self.client.get("/country/2025/BE")
    self.assertEqual(country.status_code, 200)
    leaked = [k for k in country.json() if "prop_sum_ef_mj" in k]
    self.assertEmpty(leaked)

  def test_countries_endpoint_omits_energy_forcing_fields(self):
    countries = self.client.get("/countries/2025")
    self.assertEqual(countries.status_code, 200)
    self.assertNotEmpty(countries.json())
    for row in countries.json():
      leaked = [key for key in row if "prop_sum_ef_mj" in key]
      self.assertEmpty(leaked)

  def test_firs_endpoint_omits_energy_forcing_fields(self):
    firs = self.client.get("/firs/2025")
    self.assertEqual(firs.status_code, 200)
    self.assertNotEmpty(firs.json())
    for row in firs.json():
      leaked = [key for key in row if "prop_sum_ef_mj" in key]
      self.assertEmpty(leaked)

  def test_no_silent_2025_fallback_for_missing_year(self):
    # The year is discovered, but its artifacts cannot be read.
    with mock.patch.object(
        data_loader,
        "load_json",
        return_value=None,
        autospec=True,
        spec_set=True,
    ):
      data_loader.clear_all_caches()
      with self.assertRaisesRegex(
          data_loader.ArtifactUnavailableError,
          r"Failed to load per-country JSON data for year 2024",
      ):
        data_loader.get_country_data_for_year("2024")
      with self.assertRaisesRegex(
          data_loader.ArtifactUnavailableError,
          r"Failed to load FIR JSON data for year 2024",
      ):
        data_loader.get_fir_data_for_year("2024")

  @parameterized.parameters("/country/2024/BE", "/countries/2024", "/firs/2024")
  def test_unreadable_required_artifact_returns_503_with_detail(self, url):
    self.enter_context(
        mock.patch.object(
            data_loader,
            "load_json",
            return_value=None,
            autospec=True,
            spec_set=True,
        )
    )
    data_loader.clear_all_caches()
    response = self.client.get(url)
    self.assertEqual(response.status_code, 503)
    self.assertIn("year 2024", response.json()["detail"])

  def test_series_turns_an_unreadable_year_into_a_gap(self):
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_country_data_for_year",
            autospec=True,
            spec_set=True,
            side_effect=_country_loader_failing_for(
                2023, data_loader.get_country_data_for_year
            ),
        )
    )
    response = self.client.get(
        "/series/mean_rf_mw_per_sq_m", params={"iso": "BE"}
    )
    self.assertEqual(response.status_code, 200)
    points = response.json()["points"]
    by_year = {point["year"]: point["value"] for point in points}
    self.assertIsNone(by_year[2023])
    self.assertIsNotNone(by_year[2025])

  def test_series_does_not_swallow_unrelated_runtime_errors(self):
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_country_data_for_year",
            autospec=True,
            spec_set=True,
            side_effect=RuntimeError(
                "dictionary changed size during iteration"
            ),
        )
    )
    with self.assertRaisesRegex(RuntimeError, "dictionary changed size"):
      self.client.get("/series/mean_rf_mw_per_sq_m", params={"iso": "BE"})

  @parameterized.parameters("/country/2025/BE", "/countries/2025")
  def test_malformed_country_row_returns_503_naming_row_and_field(self, url):
    row = {"iso_a2": "BE", "name": "Belgium", "mean_rf_mw_per_sq_m": "abc"}
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_country_data_for_year",
            autospec=True,
            spec_set=True,
            return_value=([row], {"BE": row}),
        )
    )
    response = self.client.get(url)
    self.assertEqual(response.status_code, 503)
    detail = response.json()["detail"]
    self.assertIn(constants.COUNTRY_EXPORT_FILENAME, detail)
    self.assertIn("'BE'", detail)
    self.assertIn("mean_rf_mw_per_sq_m", detail)
    self.assertNotIn("abc", detail)

  def test_malformed_fir_row_returns_503_naming_row_and_field(self):
    row = {
        "fir_id": "EBBU-1",
        "fir_to_country_iso2s": ["BE"],
        "fir_icao": 123,
    }
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_fir_data_for_year",
            autospec=True,
            spec_set=True,
            return_value=[row],
        )
    )
    response = self.client.get("/firs/2025")
    self.assertEqual(response.status_code, 503)
    detail = response.json()["detail"]
    self.assertIn(constants.FIR_EXPORT_FILENAME, detail)
    self.assertIn("'EBBU-1'", detail)
    self.assertIn("fir_icao", detail)
    self.assertNotIn("123", detail)

  @parameterized.parameters(
      ("/country/2025/BE", "BE", "unexpected_country_field"),
      ("/countries/2025", "BE", "unexpected_country_field"),
      ("/firs/2025", "EBBU-1", "unexpected_fir_field"),
  )
  def test_unexpected_extra_field_returns_503_naming_row_and_field(
      self, url, identity, extra_field
  ):
    country = {
        "iso_a2": "BE",
        "name": "Belgium",
        "unexpected_country_field": 1.0,
    }
    fir = {
        "fir_id": "EBBU-1",
        "fir_to_country_iso2s": ["BE"],
        "unexpected_fir_field": 1.0,
    }
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_country_data_for_year",
            autospec=True,
            spec_set=True,
            return_value=([country], {"BE": country}),
        )
    )
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_fir_data_for_year",
            autospec=True,
            spec_set=True,
            return_value=[fir],
        )
    )
    response = self.client.get(url)
    self.assertEqual(response.status_code, 503)
    detail = response.json()["detail"]
    self.assertIn(f"'{identity}'", detail)
    self.assertIn(extra_field, detail)

  @parameterized.parameters(
      "fir_dom_segment_count",
      "fir_dom_contrail_segment_count",
      "fir_int_segment_count",
      "fir_int_contrail_segment_count",
      "fir_total_segment_count",
      "fir_total_contrail_segment_count",
  )
  def test_country_segment_counts_are_served_as_ints(self, field):
    country = self.client.get("/country/2025/CH").json()
    self.assertIsInstance(country[field], int)

  @parameterized.parameters("segment_count", "contrail_segment_count")
  def test_fir_segment_counts_are_served_as_ints(self, field):
    firs = self.client.get("/firs/2025").json()
    self.assertNotEmpty(firs)
    self.assertEmpty(
        [fir["fir_id"] for fir in firs if not isinstance(fir[field], int)]
    )

  @parameterized.parameters(
      "co2e_tonnes_share",
      "outgoing_flight_co2e_tonnes_share",
      "fir_dom_co2e_tonnes_share",
      "fir_int_co2e_tonnes_share",
      "fir_total_co2e_tonnes_share",
  )
  def test_country_serves_horizon_free_share(self, field):
    country = self.client.get("/country/2025/CH").json()
    self.assertBetween(country[field], 0.0, 1.0)

  def test_fir_shares_sum_to_one(self):
    firs = self.client.get("/firs/2025").json()
    self.assertNotEmpty(firs)
    self.assertAlmostEqual(sum(fir["co2e_tonnes_share"] for fir in firs), 1.0)

  @parameterized.parameters("/countries/2025", "/firs/2025")
  def test_list_endpoints_serve_no_per_horizon_percent(self, url):
    rows = self.client.get(url).json()
    self.assertNotEmpty(rows)
    keys = set().union(*rows)
    self.assertEmpty({key for key in keys if key.endswith("_percent")})

  @parameterized.parameters("/countries/2025", "/firs/2025")
  def test_list_endpoints_serve_no_impact_density_rank(self, url):
    rows = self.client.get(url).json()
    self.assertNotEmpty(rows)
    keys = set().union(*rows)
    self.assertEmpty(
        {key for key in keys if "impact_density" in key and "rank" in key}
    )
    self.assertContainsSubset({"mean_rf_mw_per_sq_m_rank"}, keys)

  @parameterized.parameters(
      ("/country/2025/BE", "fir_total_segment_count"),
      ("/firs/2025", "segment_count"),
  )
  def test_fractional_segment_count_returns_503(self, url, field):
    country = {
        "iso_a2": "BE",
        "name": "Belgium",
        "fir_total_segment_count": 10.5,
    }
    fir = {"fir_id": "EBBU-1", "segment_count": 10.5}
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_country_data_for_year",
            autospec=True,
            spec_set=True,
            return_value=([country], {"BE": country}),
        )
    )
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_fir_data_for_year",
            autospec=True,
            spec_set=True,
            return_value=[fir],
        )
    )
    response = self.client.get(url)
    self.assertEqual(response.status_code, 503)
    self.assertIn(field, response.json()["detail"])

  def test_malformed_rows_are_not_cached(self):
    bad = {"iso_a2": "BE", "mean_rf_mw_per_sq_m": "abc"}
    with mock.patch.object(
        data_loader,
        "get_country_data_for_year",
        autospec=True,
        spec_set=True,
        return_value=([bad], {"BE": bad}),
    ):
      self.assertEqual(self.client.get("/countries/2025").status_code, 503)
    self.assertEqual(self.client.get("/countries/2025").status_code, 200)

  def test_loader_does_not_synthesize_name_from_firname(self):
    data_root = self.publish_dataset(
        "2025",
        {
            constants.COUNTRY_EXPORT_FILENAME: [
                {"iso_a2": "BE", "name": "Belgium"}
            ],
            constants.FIR_EXPORT_FILENAME: [
                {"fir_id": "EBBU", "firname": "BRUSSELS"}
            ],
        },
    )
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", data_root))
    data_loader.clear_all_caches()
    (fir_row,) = data_loader.get_fir_data_for_year("2025")
    self.assertNotIn("name", fir_row)

  def test_fir_names_come_only_from_firname(self):
    country_row = {"iso_a2": "BE"}
    fir_rows = [{
        "fir_id": "EBBU",
        "fir_icao": "EBBU",
        "name": "LEGACY NAME",
        "fir_to_country_iso2s": ["BE"],
        "mean_rf_mw_per_sq_m": 1.0,
    }]
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_country_data_for_year",
            autospec=True,
            spec_set=True,
            return_value=([country_row], {"BE": country_row}),
        )
    )
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_fir_data_for_year",
            autospec=True,
            spec_set=True,
            return_value=fir_rows,
        )
    )
    response = self.client.get(
        "/rankings/2025/fir_individual_mean_rf_mw_per_sq_m"
    )
    self.assertEqual(response.status_code, 200)
    (item,) = response.json()["items"]
    self.assertEqual(item["firname"], "")
    self.assertEqual(item["name"], "EBBU")
    self.assertEqual(item["country_name"], "BE")
    self.assertEqual(item["countries"], [{"iso_a2": "BE", "name": "BE"}])

  def test_input_length_validation(self):
    # Country ISO length > 64
    resp = self.client.get(f"/country/2025/{'A' * 65}")
    self.assertEqual(resp.status_code, 400)

    # Time series parameter lengths
    res_iso = self.client.get(
        "/series/mean_rf_mw_per_sq_m",
        params={"iso": "A" * 65},
    )
    self.assertEqual(res_iso.status_code, 400)

    res_fir_id = self.client.get(
        "/series/mean_rf_mw_per_sq_m",
        params={"fir_id": "A" * 129},
    )
    self.assertEqual(res_fir_id.status_code, 400)

  @parameterized.parameters(
      "../../etc",
      "evil/path",
      "foo..bar",
      "dataset with spaces",
      "dataset;drop",
  )
  def test_invalid_dataset_format_rejected(self, bad_dataset):
    with self.assertRaisesRegex(
        data_loader.DataValidationError, r"Invalid dataset format"
    ):
      data_loader.clean_dataset(bad_dataset)


_MOCK_COUNTRY_NEGATIVE_ROWS: tuple[dict[str, Any], ...] = (
    {
        "name": "Kosovo",
        "iso_a2": "-99",
        "co2e_gwp50_tonnes_per_capita": None,
        "co2e_gwp50_tonnes_per_capita_rank": 2,
    },
    {
        "name": "Iceland",
        "iso_a2": "IS",
        "co2e_gwp50_tonnes_per_capita": -0.05,
        "co2e_gwp50_tonnes_per_capita_rank": 4,
    },
    {
        "name": "Belgium",
        "iso_a2": "BE",
        "co2e_gwp50_tonnes_per_capita": 0.25,
        "co2e_gwp50_tonnes_per_capita_rank": 1,
    },
    {
        "name": "Tuvalu",
        "iso_a2": "TV",
        "co2e_gwp50_tonnes_per_capita": 0.0,
        "co2e_gwp50_tonnes_per_capita_rank": 3,
    },
)

_MOCK_FIR_NEGATIVE_ROWS: tuple[dict[str, Any], ...] = (
    {
        "fir_id": "XXXX_MISSING",
        "fir_icao": "XXXX",
        "firname": "MISSING",
        "fir_to_country_iso2s": ["BE"],
        "type": "domestic",
        "co2e_gwp50_tonnes": None,
        "co2e_gwp50_tonnes_rank": 2,
    },
    {
        "fir_id": "BIRD_REYKJAVIK",
        "fir_icao": "BIRD",
        "firname": "REYKJAVIK",
        "fir_to_country_iso2s": ["IS"],
        "type": "domestic",
        "co2e_gwp50_tonnes": -120.0,
        "co2e_gwp50_tonnes_rank": 3,
    },
    {
        "fir_id": "EBBU_BRUXELLES",
        "fir_icao": "EBBU",
        "firname": "BRUXELLES",
        "fir_to_country_iso2s": ["BE"],
        "type": "domestic",
        "co2e_gwp50_tonnes": 500.0,
        "co2e_gwp50_tonnes_rank": 1,
    },
)

_MOCK_COUNTRY_TIED_ROWS: tuple[dict[str, Any], ...] = (
    {"name": "Zeta", "iso_a2": "ZZ", "mean_rf_mw_per_sq_m": None},
    {"name": "Alpha", "iso_a2": "AA", "mean_rf_mw_per_sq_m": None},
    {
        "name": "A Tie Second",
        "iso_a2": "TA",
        "mean_rf_mw_per_sq_m": 2.0,
        "mean_rf_mw_per_sq_m_rank": 3,
    },
    {
        "name": "Z Tie First",
        "iso_a2": "TB",
        "mean_rf_mw_per_sq_m": 2.0,
        "mean_rf_mw_per_sq_m_rank": 2,
    },
    {"name": "Cooling", "iso_a2": "CO", "mean_rf_mw_per_sq_m": -1.0},
    {"name": "Top", "iso_a2": "TP", "mean_rf_mw_per_sq_m": 5.0},
    {"name": "Unranked B", "iso_a2": "UB", "mean_rf_mw_per_sq_m": 0.5},
    {"name": "Unranked A", "iso_a2": "UA", "mean_rf_mw_per_sq_m": 0.5},
)

_MOCK_FIR_TIED_ROWS: tuple[dict[str, Any], ...] = (
    {
        "fir_id": "NOAREA_B",
        "fir_icao": "NOAB",
        "firname": "NO AREA",
        "fir_to_country_iso2s": ["BE"],
        "type": "international",
        "mean_rf_mw_per_sq_m": None,
    },
    {
        "fir_id": "NOAREA_A",
        "fir_icao": "NOAA",
        "firname": "NO AREA",
        "fir_to_country_iso2s": ["BE"],
        "type": "international",
        "mean_rf_mw_per_sq_m": None,
    },
    {
        "fir_id": "ALPHA_TIED",
        "fir_icao": "ALPH",
        "firname": "ALPHA",
        "fir_to_country_iso2s": ["BE"],
        "type": "domestic",
        "mean_rf_mw_per_sq_m": 3.0,
        "mean_rf_mw_per_sq_m_rank": 2,
    },
    {
        "fir_id": "ZETA_TIED",
        "fir_icao": "ZETA",
        "firname": "ZETA",
        "fir_to_country_iso2s": ["BE"],
        "type": "domestic",
        "mean_rf_mw_per_sq_m": 3.0,
        "mean_rf_mw_per_sq_m_rank": 1,
    },
)


class RankingsEndpointTest(DashboardTestBase):
  """Tests the /rankings endpoint and the metric registry behind it."""

  def test_get_rankings_mean_rf_metadata(self):
    response = self.client.get("/rankings/2025/mean_rf_mw_per_sq_m")
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertEqual(data.get("metric"), "mean_rf_mw_per_sq_m")
    self.assertIn("Mean Radiative Forcing", data.get("title", ""))
    self.assertEqual(data.get("unit"), metrics.UNIT_MILLIWATTS_PER_SQ_M)

  def test_get_rankings_mean_rf_country_item(self):
    response = self.client.get("/rankings/2025/mean_rf_mw_per_sq_m")
    self.assertEqual(response.status_code, 200)
    items = response.json().get("items", [])
    self.assertGreaterEqual(len(items), 9)
    self.assertEqual(items[0].get("rank"), 1)
    de_item = next((it for it in items if it.get("iso_a2") == "DE"), None)
    self.assertIsNotNone(de_item)
    self.assertIsInstance(de_item.get("value"), float)

  def test_get_rankings_absolute_gwp(self):
    response = self.client.get("/rankings/2025/co2e_gwp_tonnes?gwp=gwp50")
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertIn("GWP50", data.get("title", ""))
    items = data.get("items", [])
    self.assertGreaterEqual(len(items), 9)

  def test_get_rankings_outgoing_per_capita_metadata(self):
    response = self.client.get(
        "/rankings/2025/outgoing_flight_co2e_gwp_tonnes_per_capita?gwp=gwp50"
    )
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertIn("Outgoing", data.get("title", ""))
    self.assertIn("Per Capita", data.get("title", ""))
    self.assertEqual(
        data.get("secondary_unit"),
        metrics.UNIT_RATIO_OF_WORLD_AVG_GHG_PER_CAPITA,
    )

  def test_get_rankings_outgoing_per_capita_country_item(self):
    response = self.client.get(
        "/rankings/2025/outgoing_flight_co2e_gwp_tonnes_per_capita?gwp=gwp50"
    )
    self.assertEqual(response.status_code, 200)
    items = response.json().get("items", [])
    self.assertGreaterEqual(len(items), 9)
    self.assertEqual(items[0].get("rank"), 1)
    gb_item = next((it for it in items if it.get("iso_a2") == "GB"), None)
    self.assertIsNotNone(gb_item)
    self.assertIsInstance(gb_item.get("secondary_value"), float)

  def test_metric_registry_keys_are_unique(self):
    self.assertLen(metrics.COUNTRY_METRIC_INDEX, len(metrics.COUNTRY_METRICS))
    self.assertEqual(
        dict(metrics.COUNTRY_METRIC_INDEX),
        {spec.id: spec for spec in metrics.COUNTRY_METRICS},
    )
    self.assertLen(metrics.FIR_ROW_METRIC_INDEX, len(metrics.FIR_ROW_METRICS))
    self.assertEqual(
        dict(metrics.FIR_ROW_METRIC_INDEX),
        {spec.id: spec for spec in metrics.FIR_ROW_METRICS},
    )
    self.assertEqual(
        dict(metrics.RANKING_METRIC_INDEX),
        {**metrics.COUNTRY_METRIC_INDEX, **metrics.FIR_ROW_METRIC_INDEX},
    )

  @parameterized.named_parameters(
      (
          "duplicate_id",
          (metrics.COUNTRY_METRICS[0], metrics.COUNTRY_METRICS[0]),
          "is claimed by both",
      ),
      (
          "mixed_case_id",
          (
              metrics.MetricSpec(
                  id="Mixed_Case", title="T", unit="u", field="f"
              ),
          ),
          "must be non-empty, lowercase, and stripped",
      ),
      (
          "whitespace_id",
          (metrics.MetricSpec(id=" padded ", title="T", unit="u", field="f"),),
          "must be non-empty, lowercase, and stripped",
      ),
      (
          "empty_id",
          (metrics.MetricSpec(id="", title="T", unit="u", field="f"),),
          "must be non-empty, lowercase, and stripped",
      ),
  )
  def test_index_metrics_rejects_invalid_or_duplicate_ids(
      self, specs, expected_pattern
  ):
    with self.assertRaisesRegex(ValueError, expected_pattern):
      metrics.index_metrics(specs)

  def test_rankings_rejects_unknown_metric(self):
    with mock.patch.object(
        data_loader,
        "get_country_data_for_year",
        autospec=True,
        spec_set=True,
    ) as mock_load:
      response = self.client.get("/rankings/2025/not_a_metric")
    self.assertEqual(response.status_code, 404)
    mock_load.assert_not_called()
    detail = response.json()["detail"]
    self.assertIn("co2e_gwp_tonnes", detail)
    self.assertIn("fir_individual_co2e_gwp_tonnes", detail)

  def test_rankings_rejects_unsupported_gwp(self):
    # Previously coerced to gwp50 and answered 200 with the wrong horizon.
    response = self.client.get("/rankings/2025/co2e_gwp_tonnes?gwp=gwp42")
    self.assertEqual(response.status_code, 422)
    self.assertIn("gwp42", response.json().get("detail", ""))

  def test_rankings_refuses_to_substitute_a_missing_horizon(self):
    # The dataset-versioning failure mode: a column present for gwp50
    # only must produce an error, never a gwp50 number under a gwp20 label.
    rows, iso_index = data_loader.get_country_data_for_year("2025")
    stripped = [
        {k: field_value for k, field_value in row.items() if "gwp20" not in k}
        for row in rows
    ]
    with mock.patch.object(
        data_loader,
        "get_country_data_for_year",
        return_value=(stripped, iso_index),
        autospec=True,
        spec_set=True,
    ):
      response = self.client.get("/rankings/2025/co2e_gwp_tonnes?gwp=gwp20")
    self.assertEqual(response.status_code, 422)
    detail = response.json().get("detail", "")
    self.assertIn("co2e_gwp20_tonnes", detail)

  def test_rankings_emit_machine_units_not_rendered_strings(self):
    response = self.client.get("/rankings/2025/co2e_gwp_tonnes?gwp=gwp50")
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertEqual(data["unit"], metrics.UNIT_TONNES_CO2E)
    self.assertEqual(data["gwp"], "gwp50")
    top_items = data["items"][:5]
    formatted = [item for item in top_items if "formatted_value" in item]
    self.assertEmpty(formatted)
    non_float = [
        item for item in top_items if not isinstance(item["value"], float)
    ]
    self.assertEmpty(non_float)

  def test_rankings_zero_value_is_not_treated_as_missing(self):
    # `row.get(a) or row.get(b)` used to fall through on a genuine 0.0.
    self.assertEqual(metrics.coerce_float(0.0), 0.0)

  @parameterized.parameters(None, float("nan"), "not a number")
  def test_coerce_float_returns_none_for_invalid_values(self, invalid_value):
    self.assertIsNone(metrics.coerce_float(invalid_value))

  @parameterized.parameters(*(
      spec.id for spec in (*metrics.COUNTRY_METRICS, *metrics.FIR_ROW_METRICS)
  ))
  def test_rankings_all_metric_branches(self, metric):
    response = self.client.get(f"/rankings/2025/{metric}?gwp=gwp50")
    self.assertEqual(response.status_code, 200, f"Failed for metric: {metric}")
    data = response.json()
    self.assertEqual(data["metric"], metric)
    self.assertIn("items", data)
    self.assertGreaterEqual(len(data["items"]), 9)
    self.assertEqual(data["items"][0]["rank"], 1)

  @parameterized.parameters(
      "impact_density",
      "impact_density_sq_km",
      "contrail_mean_radiative_forcing",
      "impact_absolute",
      "contrail_impact_absolute",
      "absolute",
      "impact_per_capita",
      "contrail_impact_per_capita",
      "per_capita",
      "vs_national_ghg",
      "national_ghg_comparison",
      "outgoing_impact",
      "outgoing_rank",
      "outgoing",
      "outgoing_flight_impact",
      "outgoing_flight_co2e_tonnes_per_capita",
      "outgoing_per_capita",
      "outgoing_impact_per_capita",
      "outgoing_flight_co2e_vs_national_ghg",
      "outgoing_vs_national_ghg",
      "outgoing_national_ghg",
      "fir_total_impact",
      "fir_total_rank",
      "fir_impact",
      "fir_density",
      "fir_density_rank",
      "fir_per_capita",
      "fir_vs_national_ghg",
      "fir_individual",
      "fir_individual_impact",
      "fir_abs",
      "fir_co2e_gwp_tonnes",
      "fir_individual_mean_rf",
      "fir_individual_density",
      "fir_density_individual",
      "fir_mean_rf_mw_per_sq_m",
      "fir_dom_co2e_Vs_ghg",
      "fir_int_co2e_Vs_ghg",
  )
  def test_rankings_and_series_reject_legacy_metric_aliases(self, alias):
    rankings_res = self.client.get(f"/rankings/2025/{alias}?gwp=gwp50")
    self.assertEqual(
        rankings_res.status_code, 404, f"Rankings accepted {alias}"
    )
    series_res = self.client.get(
        f"/series/{alias}", params={"iso": "CH", "gwp": "gwp50"}
    )
    self.assertEqual(series_res.status_code, 404, f"Series accepted {alias}")
    fir_series_res = self.client.get(
        f"/series/{alias}",
        params={"fir_id": "LSAS_SWITZERLAND", "gwp": "gwp50"},
    )
    self.assertEqual(
        fir_series_res.status_code, 404, f"FIR series accepted {alias}"
    )

  @parameterized.parameters("gwp20", "gwp50", "gwp100")
  def test_rankings_supported_gwp_horizons(self, gwp):
    resp = self.client.get(f"/rankings/2025/co2e_gwp_tonnes?gwp={gwp}")
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.json()["gwp"], gwp)

  def test_rankings_rejects_invalid_gwp_fallback(self):
    resp = self.client.get(
        "/rankings/2025/co2e_gwp_tonnes?gwp=invalid_gwp_fallback"
    )
    self.assertEqual(resp.status_code, 422)

  def test_country_rankings_sorts_negative_values_ahead_of_none(self):
    mock_country_rows = [dict(row) for row in _MOCK_COUNTRY_NEGATIVE_ROWS]
    with mock.patch.object(
        data_loader,
        "get_country_data_for_year",
        return_value=(mock_country_rows, {}),
        autospec=True,
        spec_set=True,
    ):
      resp = self.client.get(
          "/rankings/2025/co2e_gwp_tonnes_per_capita?gwp=gwp50"
      )
    self.assertEqual(resp.status_code, 200)
    items = resp.json()["items"]
    self.assertEqual(
        [it["name"] for it in items],
        ["Belgium", "Tuvalu", "Iceland", "Kosovo"],
    )
    self.assertEqual([it["rank"] for it in items], [1, 2, 3, None])
    self.assertIsNone(items[3]["value"])

  def test_fir_rankings_sorts_negative_values_ahead_of_none(self):
    mock_fir_rows = [dict(row) for row in _MOCK_FIR_NEGATIVE_ROWS]
    with mock.patch.object(
        data_loader,
        "get_fir_data_for_year",
        return_value=mock_fir_rows,
        autospec=True,
        spec_set=True,
    ):
      fir_res = self.client.get(
          "/rankings/2025/fir_individual_co2e_gwp_tonnes?gwp=gwp50"
      )
    self.assertEqual(fir_res.status_code, 200)
    fir_items = fir_res.json()["items"]
    self.assertEqual(
        [it["fir_id"] for it in fir_items],
        ["EBBU_BRUXELLES", "BIRD_REYKJAVIK", "XXXX_MISSING"],
    )
    self.assertEqual([it["rank"] for it in fir_items], [1, 2, None])
    self.assertIsNone(fir_items[2]["value"])

  @parameterized.named_parameters(
      ("forward", list(_MOCK_COUNTRY_TIED_ROWS)),
      ("reversed", list(reversed(_MOCK_COUNTRY_TIED_ROWS))),
  )
  def test_country_rankings_leave_missing_per_area_unranked_and_order_ties(
      self, mock_country_rows
  ):
    # Countries without area have a None mean RF: they must be listed last,
    # unranked, in a stable order. Ties follow the pipeline's precomputed
    # rank, falling back to the name when no precomputed rank exists.
    expected_names = [
        "Top",
        "Z Tie First",
        "A Tie Second",
        "Unranked A",
        "Unranked B",
        "Cooling",
        "Alpha",
        "Zeta",
    ]
    with mock.patch.object(
        data_loader,
        "get_country_data_for_year",
        return_value=(mock_country_rows, {}),
        autospec=True,
        spec_set=True,
    ):
      resp = self.client.get("/rankings/2025/mean_rf_mw_per_sq_m")
    self.assertEqual(resp.status_code, 200)
    items = resp.json()["items"]
    self.assertEqual([it["name"] for it in items], expected_names)
    self.assertEqual(
        [it["rank"] for it in items], [1, 2, 3, 4, 5, 6, None, None]
    )

  @parameterized.named_parameters(
      ("forward", list(_MOCK_FIR_TIED_ROWS)),
      ("reversed", list(reversed(_MOCK_FIR_TIED_ROWS))),
  )
  def test_fir_rankings_leave_missing_per_area_unranked_and_order_ties(
      self, mock_fir_rows
  ):
    with mock.patch.object(
        data_loader,
        "get_fir_data_for_year",
        return_value=mock_fir_rows,
        autospec=True,
        spec_set=True,
    ):
      resp = self.client.get(
          "/rankings/2025/fir_individual_mean_rf_mw_per_sq_m"
      )
    self.assertEqual(resp.status_code, 200)
    items = resp.json()["items"]
    self.assertEqual(
        [it["fir_id"] for it in items],
        ["ZETA_TIED", "ALPHA_TIED", "NOAREA_A", "NOAREA_B"],
    )
    self.assertEqual([it["rank"] for it in items], [1, 2, None, None])

  def test_fir_rankings_pass_type_through_without_inventing_one(self):
    fir_rows = [
        {
            "fir_id": "TYPED",
            "fir_icao": "TYPD",
            "firname": "TYPED",
            "fir_to_country_iso2s": ["BE"],
            "type": "international",
            "mean_rf_mw_per_sq_m": 2.0,
        },
        {
            "fir_id": "UNTYPED",
            "fir_icao": "UNTY",
            "firname": "UNTYPED",
            "fir_to_country_iso2s": ["BE"],
            "mean_rf_mw_per_sq_m": 1.0,
        },
    ]
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_fir_data_for_year",
            autospec=True,
            spec_set=True,
            return_value=fir_rows,
        )
    )
    response = self.client.get(
        "/rankings/2025/fir_individual_mean_rf_mw_per_sq_m"
    )
    self.assertEqual(response.status_code, 200)
    items = response.json()["items"]
    types_by_fir = {item["fir_id"]: item["type"] for item in items}
    self.assertEqual(
        types_by_fir, {"TYPED": "international", "UNTYPED": None}
    )

  def _mock_dakar_fir(self, fir_to_country_iso2s: Any) -> None:
    """Serves one Dakar FIR row controlled by Senegal and Mali."""
    fir_rows = [{
        "fir_id": "GOOO_DAKAR",
        "fir_icao": "GOOO",
        "firname": "DAKAR",
        "fir_to_country_iso2s": fir_to_country_iso2s,
        "type": "domestic",
        "mean_rf_mw_per_sq_m": 1.0,
    }]
    country_index = {
        "SN": {"iso_a2": "SN", "name": "Senegal"},
        "ML": {"iso_a2": "ML", "name": "Mali"},
    }
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_country_data_for_year",
            autospec=True,
            spec_set=True,
            return_value=(list(country_index.values()), country_index),
        )
    )
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_fir_data_for_year",
            autospec=True,
            spec_set=True,
            return_value=fir_rows,
        )
    )

  def test_multi_country_fir_lists_every_controlling_country(self):
    self._mock_dakar_fir(["SN", "ML"])
    firs = self.client.get("/firs/2025")
    self.assertEqual(firs.status_code, 200)
    (fir,) = firs.json()
    self.assertEqual(fir["fir_to_country_iso2s"], ["SN", "ML"])
    self.assertEqual(
        fir["countries"],
        [
            {"iso_a2": "SN", "name": "Senegal"},
            {"iso_a2": "ML", "name": "Mali"},
        ],
    )

  def test_multi_country_fir_ranking_uses_the_primary_country(self):
    self._mock_dakar_fir(["SN", "ML"])
    rankings = self.client.get(
        "/rankings/2025/fir_individual_mean_rf_mw_per_sq_m"
    )
    self.assertEqual(rankings.status_code, 200)
    (item,) = rankings.json()["items"]
    self.assertEqual(item["iso_a2"], "SN")
    self.assertEqual(item["country_name"], "Senegal")
    self.assertEqual(
        [country["iso_a2"] for country in item["countries"]], ["SN", "ML"]
    )

  @parameterized.parameters(
      "/firs/2025", "/rankings/2025/fir_individual_mean_rf_mw_per_sq_m"
  )
  def test_fir_row_with_comma_joined_countries_returns_503(self, url):
    # Runs that predate the list format are rejected, not split.
    self._mock_dakar_fir("SN, ML")
    response = self.client.get(url)
    self.assertEqual(response.status_code, 503)
    detail = response.json()["detail"]
    self.assertIn("'GOOO_DAKAR'", detail)
    self.assertIn("fir_to_country_iso2s", detail)


class TimeSeriesEndpointTest(DashboardTestBase):
  """Tests the /series endpoint across countries, FIRs, and horizons."""

  def test_fir_series_reports_the_primary_controlling_country(self):
    fir_row = {
        "fir_id": "GOOO_DAKAR",
        "fir_icao": "GOOO",
        "firname": "DAKAR",
        "fir_to_country_iso2s": ["SN", "ML"],
        "mean_rf_mw_per_sq_m": 1.0,
    }
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_fir_row_by_identifier",
            autospec=True,
            spec_set=True,
            return_value=fir_row,
        )
    )
    response = self.client.get(
        "/series/mean_rf_mw_per_sq_m", params={"fir_id": "GOOO_DAKAR"}
    )
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()["iso_a2"], "SN")

  def test_series_rejects_unsupported_gwp(self):
    response = self.client.get(
        "/series/co2e_gwp_tonnes", params={"iso": "DE", "gwp": "gwp42"}
    )
    self.assertEqual(response.status_code, 422)

  def test_get_series_country_national_impact_metadata(self):
    response = self.client.get(
        "/series/co2e_gwp_tonnes", params={"iso": "CH", "gwp": "GWP50"}
    )
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertEqual(data["metric"], "co2e_gwp_tonnes")
    self.assertEqual(
        (data["iso_a2"], data["country_name"]), ("CH", "Switzerland")
    )
    self.assertEqual(data["unit"], metrics.UNIT_TONNES_CO2E)
    self.assertEqual(data["gwp"], "gwp50")

  def test_get_series_country_national_impact_points(self):
    response = self.client.get(
        "/series/co2e_gwp_tonnes", params={"iso": "CH", "gwp": "GWP50"}
    )
    self.assertEqual(response.status_code, 200)
    points = response.json()["points"]
    self.assertLen(points, 4)
    years = [point_item["year"] for point_item in points]
    self.assertEqual(years, [2022, 2023, 2024, 2025])
    p_2025 = [
        point_item for point_item in points if point_item["year"] == 2025
    ][0]
    self.assertIsNotNone(p_2025["value"])
    self.assertYearOverYear(points, 2025, -20.0)

  def test_get_series_country_outgoing_impact(self):
    response = self.client.get(
        "/series/outgoing_flight_co2e_gwp_tonnes",
        params={"iso": "CH", "gwp": "GWP50"},
    )
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertEqual(data["iso_a2"], "CH")
    points = data["points"]
    p_2025 = [
        point_item for point_item in points if point_item["year"] == 2025
    ][0]
    self.assertIsNotNone(p_2025["value"])
    self.assertYearOverYear(points, 2025, -10.0)

  def test_get_series_country_fir_total_impact(self):
    response = self.client.get(
        "/series/fir_total_co2e_gwp_tonnes",
        params={"iso": "CH", "gwp": "GWP50"},
    )
    self.assertEqual(response.status_code, 200)
    data = response.json()
    points = data["points"]
    p_2025 = [
        point_item for point_item in points if point_item["year"] == 2025
    ][0]
    self.assertIsNotNone(p_2025["value"])
    self.assertYearOverYear(points, 2025, -20.0)

  def test_get_series_fir_individual_impact_metadata(self):
    response = self.client.get(
        "/series/fir_individual_co2e_gwp_tonnes",
        params={"fir_id": "LSAS_SWITZERLAND", "gwp": "GWP50"},
    )
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertEqual(data["fir_id"], "LSAS_SWITZERLAND")
    self.assertEqual(data["fir_icao"], "LSAS")
    self.assertEqual(data["firname"], "SWITZERLAND")

  def test_get_series_fir_individual_impact_points(self):
    response = self.client.get(
        "/series/fir_individual_co2e_gwp_tonnes",
        params={"fir_id": "LSAS_SWITZERLAND", "gwp": "GWP50"},
    )
    self.assertEqual(response.status_code, 200)
    points = response.json()["points"]
    self.assertLen(points, 4)
    p_2025 = [
        point_item for point_item in points if point_item["year"] == 2025
    ][0]
    self.assertIsNotNone(p_2025["value"])
    self.assertYearOverYear(points, 2025, -20.0)

  def test_get_series_missing_params(self):
    response = self.client.get("/series/co2e_gwp_tonnes")
    self.assertEqual(response.status_code, 400)

  def test_get_series_not_found(self):
    response = self.client.get(
        "/series/co2e_gwp_tonnes", params={"iso": "ZZ_NONEXISTENT"}
    )
    self.assertEqual(response.status_code, 404)

  def test_get_series_mean_rf_units(self):
    res_rf = self.client.get(
        "/series/mean_rf_mw_per_sq_m", params={"iso": "CH"}
    )
    self.assertEqual(res_rf.status_code, 200)
    data_rf = res_rf.json()
    self.assertEqual(data_rf["unit"], metrics.UNIT_MILLIWATTS_PER_SQ_M)
    self.assertEqual(data_rf["metric_title"], "Contrail Mean Radiative Forcing")

  def test_get_series_mean_rf_points(self):
    res_rf = self.client.get(
        "/series/mean_rf_mw_per_sq_m", params={"iso": "CH"}
    )
    self.assertEqual(res_rf.status_code, 200)
    data_rf = res_rf.json()
    self.assertNotEmpty(data_rf["points"])
    self.assertIsInstance(data_rf["points"][-1]["value"], float)
    self.assertNotIn("formatted_value", data_rf["points"][-1])

  def test_get_series_per_capita_units(self):
    res_cap = self.client.get(
        "/series/co2e_gwp_tonnes_per_capita", params={"iso": "CH"}
    )
    self.assertEqual(res_cap.status_code, 200)
    data_cap = res_cap.json()
    self.assertEqual(data_cap["unit"], metrics.UNIT_TONNES_CO2E_PER_CAPITA)
    self.assertIsInstance(data_cap["points"][-1]["value"], float)

  def test_get_series_vs_national_ghg_ratio_units(self):
    # vs National GHG. The value is a ratio, not a percentage: the server no
    # longer multiplies by 100 on the caller's behalf.
    res_ghg = self.client.get(
        "/series/co2e_gwp_vs_national_ghg", params={"iso": "CH"}
    )
    self.assertEqual(res_ghg.status_code, 200)
    data_ghg = res_ghg.json()
    self.assertEqual(data_ghg["unit"], metrics.UNIT_RATIO_OF_NATIONAL_GHG)
    self.assertLess(abs(data_ghg["points"][-1]["value"]), 1.0)

  def test_get_series_fir_individual_mean_rf_units(self):
    res_fir_rf = self.client.get(
        "/series/fir_individual_mean_rf_mw_per_sq_m",
        params={"fir_id": "LSAS_SWITZERLAND"},
    )
    self.assertEqual(res_fir_rf.status_code, 200)
    data_fir_rf = res_fir_rf.json()
    self.assertEqual(data_fir_rf["unit"], metrics.UNIT_MILLIWATTS_PER_SQ_M)
    self.assertEqual(data_fir_rf["fir_id"], "LSAS_SWITZERLAND")
    self.assertEqual(data_fir_rf["fir_icao"], "LSAS")

  def test_get_series_gwp_variations_response_structure(self):
    res_gwp20 = self.client.get(
        "/series/co2e_gwp_tonnes", params={"iso": "CH", "gwp": "GWP20"}
    )
    self.assertEqual(res_gwp20.status_code, 200)
    self.assertEqual(res_gwp20.json()["gwp"], "gwp20")

    res_gwp100 = self.client.get(
        "/series/co2e_gwp_tonnes", params={"iso": "CH", "gwp": "GWP100"}
    )
    self.assertEqual(res_gwp100.status_code, 200)
    self.assertEqual(res_gwp100.json()["gwp"], "gwp100")

  def test_get_series_gwp_variations_values_order(self):
    res_gwp20 = self.client.get(
        "/series/co2e_gwp_tonnes", params={"iso": "CH", "gwp": "GWP20"}
    )
    res_gwp100 = self.client.get(
        "/series/co2e_gwp_tonnes", params={"iso": "CH", "gwp": "GWP100"}
    )
    self.assertEqual(res_gwp20.status_code, 200)
    self.assertEqual(res_gwp100.status_code, 200)
    val_20 = [p for p in res_gwp20.json()["points"] if p["year"] == 2025][0][
        "value"
    ]
    val_100 = [p for p in res_gwp100.json()["points"] if p["year"] == 2025][0][
        "value"
    ]
    self.assertIsNotNone(val_20)
    self.assertIsNotNone(val_100)
    self.assertGreater(val_20, val_100)

  def test_get_series_lookup_by_fir_id(self):
    res_fir = self.client.get(
        "/series/fir_individual_co2e_gwp_tonnes",
        params={"fir_id": "LSAS_SWITZERLAND"},
    )
    self.assertEqual(res_fir.status_code, 200)
    data_fir = res_fir.json()
    self.assertEqual(data_fir["fir_id"], "LSAS_SWITZERLAND")
    self.assertEqual(data_fir["fir_icao"], "LSAS")
    self.assertEqual(data_fir["firname"], "SWITZERLAND")

  @parameterized.parameters("756", "Switzerland")
  def test_get_series_rejects_numeric_and_name_iso(self, invalid_iso):
    res = self.client.get(
        "/series/co2e_gwp_tonnes", params={"iso": invalid_iso}
    )
    self.assertEqual(res.status_code, 404)

  def test_get_series_disambiguates_multi_sector_fir_identities(self):
    res_east = self.client.get(
        "/series/fir_individual_co2e_gwp_tonnes",
        params={"fir_id": "KZAK_OAKLAND_OCEANIC_EAST"},
    )
    res_west = self.client.get(
        "/series/fir_individual_co2e_gwp_tonnes",
        params={"fir_id": "KZAK_OAKLAND_OCEANIC_WEST"},
    )
    self.assertEqual(res_east.status_code, 200)
    self.assertEqual(res_west.status_code, 200)
    east_data, west_data = res_east.json(), res_west.json()
    self.assertEqual(east_data["fir_id"], "KZAK_OAKLAND_OCEANIC_EAST")
    self.assertEqual(west_data["fir_id"], "KZAK_OAKLAND_OCEANIC_WEST")
    self.assertEqual(
        (east_data["fir_icao"], west_data["fir_icao"]), ("KZAK", "KZAK")
    )

  def test_get_series_disambiguates_multi_sector_fir_points_and_names(self):
    res_east = self.client.get(
        "/series/fir_individual_co2e_gwp_tonnes",
        params={"fir_id": "KZAK_OAKLAND_OCEANIC_EAST"},
    )
    res_west = self.client.get(
        "/series/fir_individual_co2e_gwp_tonnes",
        params={"fir_id": "KZAK_OAKLAND_OCEANIC_WEST"},
    )
    self.assertEqual(res_east.status_code, 200)
    self.assertEqual(res_west.status_code, 200)
    east_data, west_data = res_east.json(), res_west.json()
    self.assertEqual(east_data["firname"], "OAKLAND OCEANIC EAST")
    self.assertEqual(west_data["firname"], "OAKLAND OCEANIC WEST")
    self.assertNotEqual(east_data["points"], west_data["points"])

  @parameterized.parameters(
      "co2e_gwp_tonnes_per_capita",
      "co2e_gwp_vs_national_ghg",
  )
  def test_get_series_rejects_country_only_metric_for_fir(
      self, country_only_metric
  ):
    # Country-only metrics without a per-FIR row unit equivalent must raise
    # UnknownMetricError (404) rather than silently falling back to tonnes_co2e.
    with self.assertRaisesRegex(
        metrics.UnknownMetricError, r"has no per-FIR row equivalent"
    ):
      metrics.project_to_fir_row_metric(country_only_metric)
    response_unmappable = self.client.get(
        f"/series/{country_only_metric}",
        params={"fir_id": "LSAS_SWITZERLAND"},
    )
    self.assertEqual(response_unmappable.status_code, 404)
    detail = response_unmappable.json()["detail"]
    self.assertIn("co2e_gwp_tonnes", detail)
    self.assertIn("fir_individual_co2e_gwp_tonnes", detail)

  def test_get_series_fir_metric_requires_fir_id(self):
    # Requesting a FIR-row metric without fir_id must return HTTP 400.
    response_missing_fir = self.client.get(
        "/series/fir_individual_co2e_gwp_tonnes",
        params={"iso": "CH"},
    )
    self.assertEqual(response_missing_fir.status_code, 400)
    self.assertIn("requires 'fir_id'", response_missing_fir.json()["detail"])

  def test_get_series_routes_to_fir_when_both_iso_and_fir_id_supplied(self):
    # Supplying both iso and fir_id with a projectable metric must route to the
    # FIR row rather than ignoring fir_id and returning country rows.
    response_fir_only = self.client.get(
        "/series/co2e_gwp_tonnes",
        params={"fir_id": "LSAS_SWITZERLAND"},
    )
    response_both = self.client.get(
        "/series/co2e_gwp_tonnes",
        params={"iso": "CH", "fir_id": "LSAS_SWITZERLAND"},
    )
    self.assertEqual(response_fir_only.status_code, 200)
    self.assertEqual(response_both.status_code, 200)
    self.assertEqual(response_both.json()["fir_id"], "LSAS_SWITZERLAND")
    self.assertEqual(
        response_both.json()["points"], response_fir_only.json()["points"]
    )


class GridEndpointTest(DashboardTestBase):
  """Tests the /grid metadata, binary, and availability endpoints."""

  @parameterized.parameters(
      "/grid/2021/total_global_impact_grid/meta",
      "/grid/2021/total_global_impact_grid/binary",
      "/grid/2021/availability",
  )
  def test_grid_endpoints_reject_unsupported_year_with_400(self, url):
    response = self.client.get(url)
    self.assertEqual(response.status_code, 400)
    self.assertIn("Available years are", response.json()["detail"])

  @parameterized.named_parameters(
      ("meta", lambda: data_loader.get_grid_meta("2021", prefix="grid")),
      (
          "meta_json",
          lambda: data_loader.get_grid_meta_json("2021", prefix="grid"),
      ),
      (
          "binary",
          lambda: data_loader.get_grid_binary_payload("2021", prefix="grid"),
      ),
      ("availability", lambda: data_loader.get_grid_availability("2021")),
  )
  def test_grid_loaders_reject_unsupported_year(self, call):
    with self.assertRaisesRegex(
        data_loader.DataValidationError, "not available"
    ):
      call()

  @parameterized.parameters(
      "/grid/2025/no_such_grid/meta", "/grid/2025/no_such_grid/binary"
  )
  def test_grid_meta_and_binary_not_found_in_available_year(self, url):
    response = self.client.get(url)
    self.assertEqual(response.status_code, 404)

  def _create_synthetic_grid_fixture(
      self, temp_dir: str, year: str = "2024"
  ) -> bytes:
    year_dir = _publish_run_dir(temp_dir, year)
    os.makedirs(year_dir, exist_ok=True)
    meta_content = {
        "width": 10,
        "height": 5,
        "bbox": {
            "min_lon": -180.0,
            "max_lon": 180.0,
            "min_lat": -90.0,
            "max_lat": 90.0,
        },
        "lon_step": 36.0,
        "lat_step": 36.0,
        "min_value": 0.0,
        "max_value": 100.0,
        "dtype": "float32",
        "origin": "lower_left",
    }
    with open(
        os.path.join(year_dir, "grid_meta.json"), "w", encoding="utf-8"
    ) as f:
      json.dump(meta_content, f)

    dummy_bin_bytes = b"\x00\x00\x80\x3f" * 50  # 50 float32 numbers (1.0)
    compressed = gzip.compress(dummy_bin_bytes, compresslevel=6)
    with open(
        os.path.join(year_dir, "total_global_impact_grid.bin.gz"), "wb"
    ) as f:
      f.write(compressed)
    with open(
        os.path.join(year_dir, "total_global_density_grid.bin.gz"), "wb"
    ) as f:
      f.write(compressed)
    return dummy_bin_bytes

  def test_grid_meta_success(self):
    temp_dir = self.create_tempdir().full_path
    self._create_synthetic_grid_fixture(temp_dir)
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", temp_dir))

    meta_res = self.client.get("/grid/2024/total_global_impact_grid/meta")
    self.assertEqual(meta_res.status_code, 200)
    meta_json = meta_res.json()
    self.assertEqual(meta_json["width"], 10)
    self.assertEqual(meta_json["height"], 5)
    self.assertNotIn("binary_endpoint", meta_json)

  def test_grid_binary_success(self):
    temp_dir = self.create_tempdir().full_path
    dummy_bytes = self._create_synthetic_grid_fixture(temp_dir)
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", temp_dir))

    bin_res = self.client.get("/grid/2024/total_global_impact_grid/binary")
    self.assertEqual(bin_res.status_code, 200)
    self.assertEqual(
        bin_res.headers["content-type"], "application/octet-stream"
    )
    self.assertEqual(bin_res.content, dummy_bytes)

  def test_grid_yearless_filename_route_returns_404(self):
    temp_dir = self.create_tempdir().full_path
    self._create_synthetic_grid_fixture(temp_dir)
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", temp_dir))

    fb_res = self.client.get(
        "/grid/binary/total_global_impact_grid.bin.gz",
        params={"year": "2024"},
    )
    self.assertEqual(fb_res.status_code, 404)

  def test_grid_density_meta_and_binary_success(self):
    temp_dir = self.create_tempdir().full_path
    dummy_bytes = self._create_synthetic_grid_fixture(temp_dir)
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", temp_dir))

    d_meta = self.client.get("/grid/2024/total_global_density_grid/meta")
    self.assertEqual(d_meta.status_code, 200)
    self.assertEqual(d_meta.json()["width"], 10)

    d_bin = self.client.get("/grid/2024/total_global_density_grid/binary")
    self.assertEqual(d_bin.status_code, 200)
    self.assertEqual(d_bin.content, dummy_bytes)

  def test_grid_availability_success(self):
    temp_dir = self.create_tempdir().full_path
    self._create_synthetic_grid_fixture(temp_dir)
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", temp_dir))

    avail_res = self.client.get("/grid/2024/availability")
    self.assertEqual(avail_res.status_code, 200)
    avail_data = avail_res.json()
    self.assertTrue(avail_data.get("has_total"))
    self.assertFalse(avail_data.get("has_monthly"))
    self.assertFalse(avail_data.get("has_hourly"))

  @parameterized.parameters(
      "2024$", "abcd", "2024a", "999", "10000", "２０２４"
  )
  def test_grid_invalid_year_rejected(self, bad_year):
    res_meta = self.client.get(
        f"/grid/{bad_year}/total_global_impact_grid/meta"
    )
    self.assertEqual(res_meta.status_code, 400)
    res_bin = self.client.get(
        f"/grid/{bad_year}/total_global_impact_grid/binary"
    )
    self.assertEqual(res_bin.status_code, 400)
    res_avail = self.client.get(f"/grid/{bad_year}/availability")
    self.assertEqual(res_avail.status_code, 400)

  def test_grid_invalid_prefix_rejected(self):
    res_bad_prefix = self.client.get(
        "/grid/2024/total_global_impact_grid!/binary"
    )
    self.assertEqual(res_bad_prefix.status_code, 400)

  def _publish_precompressed_grid(self):
    temp_dir = self.create_tempdir()
    year_dir = _publish_run_dir(temp_dir.full_path, "2024")
    os.makedirs(year_dir, exist_ok=True)
    bin_gz_file = os.path.join(year_dir, "total_global_impact_grid.bin.gz")
    dummy_bytes = b"CONTRAILS_GRID_RAW_DATA_12345" * 100
    precompressed = gzip.compress(dummy_bytes, compresslevel=6)
    with open(bin_gz_file, "wb") as f:
      f.write(precompressed)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    self.enter_context(
        mock.patch.object(data_loader, "DATA_DIR", temp_dir.full_path)
    )
    return dummy_bytes

  def test_grid_binary_precompressed_gzip_response_headers(self):
    dummy_bytes = self._publish_precompressed_grid()
    resp = self.client.get(
        "/grid/2024/total_global_impact_grid/binary",
        headers={
            "Accept-Encoding": "gzip",
            "Origin": "http://localhost:5173",
        },
    )
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.headers.get("content-encoding"), "gzip")
    self.assertIn("accept-encoding", (resp.headers.get("vary") or "").lower())
    self.assertIn(
        "x-grid-meta",
        (resp.headers.get("access-control-expose-headers") or "").lower(),
    )
    self.assertEqual(resp.content, dummy_bytes)

  def test_grid_binary_precompressed_gzip_cache_hit(self):
    self._publish_precompressed_grid()
    resp = self.client.get(
        "/grid/2024/total_global_impact_grid/binary",
        headers={"Accept-Encoding": "gzip"},
    )
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(data_loader._read_gzip_grid_file.cache_info().currsize, 1)
    self.assertFalse(hasattr(data_loader, "_GRID_BINARY_RAW_CACHE"))

  @parameterized.parameters(
      "identity",
      "gzip;q=0, identity;q=1.0",
      "gzip;q=0, *",
      "*;q=1, gzip;q=0",
      "*;q=0",
      "br",
  )
  def test_grid_binary_identity_accept_encoding(self, identity_header):
    dummy_bytes = self._publish_precompressed_grid()
    res_identity = self.client.get(
        "/grid/2024/total_global_impact_grid/binary",
        headers={"Accept-Encoding": identity_header},
    )
    self.assertEqual(res_identity.status_code, 200)
    self.assertIsNone(res_identity.headers.get("content-encoding"))
    self.assertIn(
        "accept-encoding",
        (res_identity.headers.get("vary") or "").lower(),
    )
    self.assertEqual(res_identity.content, dummy_bytes)

  @parameterized.parameters("*", "x-gzip", "gzip;q=0.5, *;q=0", "GZIP;Q=1")
  def test_grid_binary_gzip_accept_encoding(self, gzip_header):
    dummy_bytes = self._publish_precompressed_grid()
    res_gzip = self.client.get(
        "/grid/2024/total_global_impact_grid/binary",
        headers={"Accept-Encoding": gzip_header},
    )
    self.assertEqual(res_gzip.status_code, 200)
    self.assertEqual(res_gzip.headers.get("content-encoding"), "gzip")
    self.assertEqual(res_gzip.content, dummy_bytes)

  def test_grid_binary_identity_corrupt_gzip_returns_503(self):
    temp_dir = self.create_tempdir()
    year_dir = _publish_run_dir(temp_dir.full_path, "2024")
    os.makedirs(year_dir, exist_ok=True)
    bin_gz_file = os.path.join(year_dir, "total_global_impact_grid.bin.gz")
    with open(bin_gz_file, "wb") as out_file:
      out_file.write(b"\x1f\x8b\x08\x00corrupt-gzip-payload")
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    self.enter_context(
        mock.patch.object(data_loader, "DATA_DIR", temp_dir.full_path)
    )
    resp = self.client.get(
        "/grid/2024/total_global_impact_grid/binary",
        headers={"Accept-Encoding": "identity"},
    )
    self.assertEqual(resp.status_code, 503)
    self.assertIn("decompress", resp.json()["detail"])

  def test_grid_binary_gzip_cache_capacity_for_24h_loops(self):
    self.assertEqual(data_loader._read_gzip_grid_file.cache_info().maxsize, 128)

  def _create_direct_api_grid_fixture(self, temp_dir: str) -> bytes:
    year_dir = _publish_run_dir(temp_dir, "2024")
    os.makedirs(year_dir, exist_ok=True)
    with open(
        os.path.join(year_dir, "grid_meta.json"), "w", encoding="utf-8"
    ) as f:
      json.dump({"width": 4, "height": 2}, f)
    dummy_bin_bytes = b"\x00\x00\x80\x3f" * 8
    with open(os.path.join(year_dir, "test_prefix.bin.gz"), "wb") as f:
      f.write(gzip.compress(dummy_bin_bytes, compresslevel=6))
    return dummy_bin_bytes

  def test_grid_data_loader_rejects_invalid_identifiers(self):
    with self.assertRaisesRegex(
        data_loader.DataValidationError, r"Invalid year format"
    ):
      data_loader.get_grid_meta("2024!", prefix="test_prefix")
    with self.assertRaisesRegex(
        data_loader.DataValidationError, r"Invalid grid prefix format"
    ):
      data_loader.get_grid_binary_payload("2024", prefix="bad$prefix")

  def test_grid_data_loader_returns_meta_and_payload(self):
    temp_dir = self.create_tempdir().full_path
    dummy_bin_bytes = self._create_direct_api_grid_fixture(temp_dir)
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", temp_dir))

    meta_res = data_loader.get_grid_meta("2024", prefix="test_prefix")
    self.assertIsNotNone(meta_res)
    self.assertEqual(meta_res.get("width"), 4)

    payload_gzip = data_loader.get_grid_binary_payload(
        "2024", prefix="test_prefix"
    )
    self.assertIsNotNone(payload_gzip)
    self.assertEqual(gzip.decompress(payload_gzip), dummy_bin_bytes)

  def test_grid_data_loader_nonexistent_prefix_returns_none(self):
    temp_dir = self.create_tempdir().full_path
    self._create_direct_api_grid_fixture(temp_dir)
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", temp_dir))

    self.assertIsNone(
        data_loader.get_grid_meta("2024", prefix="nonexistent_prefix")
    )
    self.assertIsNone(
        data_loader.get_grid_binary_payload("2024", prefix="nonexistent_prefix")
    )


def _make_france_fir_bundle(
    fir_id: str, firname: str, fir_icao: str, count: int, co2e: float
) -> dict[str, Any]:
  fir = {
      "fir_id": fir_id,
      "firname": firname,
      "fir_icao": fir_icao,
      "fir_to_country_iso2s": ["FR"],
      "type": "domestic",
  }
  return _temporal_bundle(
      fir, {(14, 6): {"segment_count": count, "co2e_gwp50_tonnes": co2e}}
  )


def _make_kzak_bundle(
    fir_id: str, firname: str, co2e: float, count: float
) -> dict[str, Any]:
  fir = {
      "fir_id": fir_id,
      "fir_icao": "KZAK",
      "firname": firname,
      "fir_to_country_iso2s": ["US"],
  }
  return _temporal_bundle(
      fir, {(14, 6): {"co2e_gwp50_tonnes": co2e, "segment_count": count}}
  )


def _temporal_test_countries() -> list[dict[str, str]]:
  """Returns minimal per-country export rows for temporal test datasets.

  `/temporal` only serves countries that the year's country export lists.

  Returns:
    Fresh CH, DE and FR country rows.
  """
  return [
      {"iso_a2": "CH", "name": "Switzerland"},
      {"iso_a2": "DE", "name": "Germany"},
      {"iso_a2": "FR", "name": "France"},
  ]


class TemporalEndpointTest(DashboardTestBase):
  """Tests the /temporal matrix endpoint and its aggregation helpers."""

  def _setup_country_temporal_dataset(self) -> None:
    dummy_temporal = [
        _temporal_bundle(
            {"name": "Switzerland", "iso_a2": "CH"},
            {
                (18, 10): {
                    "segment_count": 500,
                    "contrail_segment_count": 400,
                    "co2e_gwp50_tonnes": 1250.5,
                }
            },
        ),
        _temporal_bundle(
            {"name": "Germany", "iso_a2": "DE"},
            {
                (18, 10): {
                    "segment_count": 800,
                    "contrail_segment_count": 650,
                    "co2e_gwp50_tonnes": 2000.0,
                }
            },
        ),
    ]
    data_root = self.publish_dataset(
        "2024",
        {
            constants.COUNTRY_EXPORT_FILENAME: _temporal_test_countries(),
            constants.COUNTRY_TEMPORAL_EXPORT_FILENAME: dummy_temporal,
        },
    )
    data_loader.clear_all_caches()
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", data_root))

  def test_temporal_endpoint_country_filter_by_iso(self):
    self._setup_country_temporal_dataset()
    res_ch = self.client.get("/temporal/2024", params={"iso": "CH"})
    self.assertEqual(res_ch.status_code, 200)
    data_ch = res_ch.json()
    self.assertEqual(
        (data_ch["iso_a2"], data_ch["name"]), ("CH", "Switzerland")
    )
    self.assertLen(data_ch["hours"], 24)
    self.assertLen(data_ch["months"], 12)
    self.assertEqual(data_ch["co2e_gwp50_tonnes"][18][9], 1250.5)

  def test_temporal_endpoint_country_filter_segment_counts(self):
    self._setup_country_temporal_dataset()
    res_ch = self.client.get("/temporal/2024", params={"iso": "CH"})
    self.assertEqual(res_ch.status_code, 200)
    data_ch = res_ch.json()
    self.assertEqual(data_ch["segment_count"][18][9], 500)
    self.assertNotIn("total_segment_count", data_ch)
    self.assertEqual(data_ch["contrail_segment_count"][18][9], 400)

  @parameterized.parameters("Switzerland", "756")
  def test_temporal_endpoint_country_unmatched_returns_404(self, non_iso):
    self._setup_country_temporal_dataset()
    res = self.client.get("/temporal/2024", params={"iso": non_iso})
    self.assertEqual(res.status_code, 404)
    self.assertIn("not found", res.json()["detail"])

  @parameterized.named_parameters(
      ("no_params", {}),
      ("empty_iso", {"iso": ""}),
      ("blank_iso", {"iso": "  "}),
      ("empty_iso_and_fir", {"iso": "", "fir_id": ""}),
  )
  def test_temporal_endpoint_without_entity_returns_400(self, params):
    self._setup_country_temporal_dataset()
    res = self.client.get("/temporal/2024", params=params)
    self.assertEqual(res.status_code, 400)
    self.assertIn("require an 'iso' or 'fir_id'", res.json()["detail"])

  def test_temporal_endpoint_ignores_legacy_global_row(self):
    legacy_global_row = _temporal_bundle(
        {"name": "Global", "iso_a2": "GLOBAL"},
        {(18, 10): {"co2e_gwp50_tonnes": 3250.5}},
    )
    data_root = self.publish_dataset(
        "2024",
        {
            constants.COUNTRY_EXPORT_FILENAME: _temporal_test_countries(),
            constants.COUNTRY_TEMPORAL_EXPORT_FILENAME: [legacy_global_row],
        },
    )
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", data_root))
    res = self.client.get("/temporal/2024", params={"iso": "GLOBAL"})
    self.assertEqual(res.status_code, 404)

  def test_real_temporal_switzerland(self):
    resp = self.client.get("/temporal/2025", params={"iso": "CH"})
    self.assertEqual(resp.status_code, 200)
    data = resp.json()
    self.assertEqual(data["iso_a2"], "CH")
    self.assertEqual(data["name"], "Switzerland")
    co2e_sum = sum(sum(row) for row in data["co2e_gwp50_tonnes"])
    self.assertGreater(co2e_sum, 0)

  def test_real_temporal_known_country_without_rows_returns_zero_matrix(self):
    # Belgium is in the fixture's country export but has no temporal row, so
    # the title comes from the export name.
    resp = self.client.get("/temporal/2025", params={"iso": "BE"})
    data = resp.json()
    self.assertEqual(
        (resp.status_code, data["iso_a2"], data["name"]),
        (200, "BE", "Belgium"),
    )
    co2e_sum = sum(sum(row) for row in data["co2e_gwp50_tonnes"])
    self.assertEqual(co2e_sum, 0.0)

  def test_real_temporal_switzerland_rejects_country_name_as_iso(self):
    res_name = self.client.get("/temporal/2025", params={"iso": "Switzerland"})
    self.assertEqual(res_name.status_code, 404)

  @staticmethod
  def _build_france_temporal_datasets() -> tuple[list[Any], list[Any]]:
    country_data = [
        _temporal_bundle(
            {"name": "France", "iso_a2": "FR"},
            {
                (14, 6): {
                    "segment_count": 1000,
                    "co2e_gwp50_tonnes": 3000.0,
                }
            },
        )
    ]
    fir_data = [
        _make_france_fir_bundle(
            "LFBB_BORDEAUX", "BORDEAUX", "LFBB", 400, 1200.0
        ),
        _make_france_fir_bundle("LFRR_BREST", "BREST", "LFRR", 600, 1800.0),
    ]
    return country_data, fir_data

  def _setup_fir_temporal_dataset(self) -> None:
    country_data, fir_data = self._build_france_temporal_datasets()
    data_root = self.publish_dataset(
        "2025",
        {
            constants.COUNTRY_EXPORT_FILENAME: _temporal_test_countries(),
            constants.COUNTRY_TEMPORAL_EXPORT_FILENAME: country_data,
            constants.FIR_TEMPORAL_EXPORT_FILENAME: fir_data,
        },
    )
    data_loader.clear_all_caches()
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", data_root))

  def test_fir_temporal_endpoint_canonical_fir_id(self):
    self._setup_fir_temporal_dataset()
    res_icao = self.client.get(
        "/temporal/2025", params={"fir_id": "LFBB_BORDEAUX", "iso": "FR"}
    )
    self.assertEqual(res_icao.status_code, 200)
    data_icao = res_icao.json()
    self.assertTrue(data_icao["is_fir"])
    self.assertEqual(
        (data_icao["fir_id"], data_icao["fir_icao"]),
        ("LFBB_BORDEAUX", "LFBB"),
    )
    self.assertEqual(data_icao["name"], "FIR BORDEAUX (LFBB)")
    self.assertEqual(data_icao["co2e_gwp50_tonnes"][14][5], 1200.0)

  def test_fir_temporal_endpoint_raw_name_falls_back_to_country(self):
    self._setup_fir_temporal_dataset()
    res_name = self.client.get(
        "/temporal/2025", params={"fir_id": "Brest FIR", "iso": "FR"}
    )
    self.assertEqual(res_name.status_code, 200)
    data_name = res_name.json()
    self.assertFalse(data_name["is_fir"])
    self.assertEqual(data_name["name"], "France")

  def test_fir_temporal_endpoint_second_canonical_fir_id(self):
    self._setup_fir_temporal_dataset()
    res_lfrr = self.client.get(
        "/temporal/2025", params={"fir_id": "LFRR_BREST", "iso": "FR"}
    )
    self.assertEqual(res_lfrr.status_code, 200)
    data_lfrr = res_lfrr.json()
    self.assertTrue(data_lfrr["is_fir"])
    self.assertEqual(data_lfrr["fir_id"], "LFRR_BREST")
    self.assertEqual(data_lfrr["co2e_gwp50_tonnes"][14][5], 1800.0)

  def test_fir_temporal_endpoint_nonexistent_fir_falls_back_to_country(self):
    self._setup_fir_temporal_dataset()
    res_missing_fir = self.client.get(
        "/temporal/2025", params={"fir_id": "NON_EXISTENT_FIR", "iso": "FR"}
    )
    self.assertEqual(res_missing_fir.status_code, 200)
    data_fallback = res_missing_fir.json()
    self.assertFalse(data_fallback["is_fir"])
    self.assertEqual(data_fallback["name"], "France")
    self.assertEqual(data_fallback["co2e_gwp50_tonnes"][14][5], 3000.0)

  @parameterized.parameters("GLOBAL", "WORLD", "ALL")
  def test_temporal_former_global_aliases_return_404(self, alias):
    resp = self.client.get("/temporal/2025", params={"iso": alias})
    self.assertEqual(resp.status_code, 404)
    self.assertIn(f"'{alias}' not found", resp.json()["detail"])

  def test_temporal_former_global_view_alias_is_malformed_iso(self):
    resp = self.client.get("/temporal/2025", params={"iso": "GLOBAL VIEW"})
    self.assertEqual(resp.status_code, 400)
    self.assertIn("Invalid ISO identifier", resp.json()["detail"])

  def test_temporal_lowercase_iso(self):
    res_de = self.client.get("/temporal/2025", params={"iso": "de"})
    self.assertEqual(res_de.status_code, 200)
    self.assertEqual(res_de.json()["iso_a2"], "DE")

  @parameterized.parameters("germany", "276", "UNKNOWN_COUNTRY_XYZ")
  def test_temporal_unmatched_iso_returns_404(self, unmatched_iso):
    # Country names, numeric ISO codes, and unknown codes do not match under
    # strict ISO matching.
    resp = self.client.get("/temporal/2025", params={"iso": unmatched_iso})
    self.assertEqual(resp.status_code, 404)

  def test_temporal_invalid_year_rejected(self):
    res_bad = self.client.get("/temporal/invalid_year!")
    self.assertEqual(res_bad.status_code, 400)

  def test_temporal_segment_count_country(self):
    country_rows = [
        _temporal_bundle(
            {"iso_a2": "DE", "name": "Germany"},
            {
                (14, 6): {
                    "co2e_gwp50_tonnes": 120.0,
                    "contrail_segment_count": 25,
                    "segment_count": 500,
                }
            },
        )
    ]
    data_loader.clear_all_caches()
    with mock.patch.object(
        data_loader,
        "get_temporal_raw_data",
        return_value=country_rows,
        autospec=True,
        spec_set=True,
    ):
      response = self.client.get("/temporal/2025", params={"iso": "DE"})
      self.assertEqual(response.status_code, 200)
      data = response.json()
      self.assertNotIn("has_total_segments", data)
      self.assertNotIn("total_segment_count", data)
      self.assertIsInstance(data["contrail_segment_count"][14][5], int)
      self.assertIsInstance(data["segment_count"][14][5], int)
      self.assertEqual(data["contrail_segment_count"][14][5], 25)
      self.assertEqual(data["segment_count"][14][5], 500)

  def test_temporal_segment_count_fir(self):
    fir_rows = [
        _temporal_bundle(
            {
                "fir_id": "EDGG_LANGEN",
                "fir_icao": "EDGG",
                "firname": "LANGEN",
                "fir_to_country_iso2s": ["DE"],
            },
            {
                (10, 7): {
                    "co2e_gwp50_tonnes": 80.0,
                    "contrail_segment_count": 18,
                    "segment_count": 350,
                }
            },
        )
    ]
    data_loader.clear_all_caches()
    with mock.patch.object(
        data_loader,
        "get_fir_temporal_raw_data",
        return_value=fir_rows,
        autospec=True,
        spec_set=True,
    ):
      response = self.client.get(
          "/temporal/2025", params={"fir_id": "EDGG_LANGEN"}
      )
      self.assertEqual(response.status_code, 200)
      data = response.json()
      self.assertEqual((data["is_fir"], data["fir_id"]), (True, "EDGG_LANGEN"))
      self.assertNotIn("total_segment_count", data)
      self.assertIsInstance(data["contrail_segment_count"][10][6], int)
      self.assertIsInstance(data["segment_count"][10][6], int)
      self.assertEqual(data["contrail_segment_count"][10][6], 18)
      self.assertEqual(data["segment_count"][10][6], 350)

  def test_temporal_fir_fallback_to_country_when_fir_rows_empty(self):
    country_rows = [
        _temporal_bundle(
            {"iso_a2": "FR", "name": "France"},
            {(12, 5): {"co2e_gwp50_tonnes": 55.0}},
        )
    ]
    data_loader.clear_all_caches()
    with (
        mock.patch.object(
            data_loader,
            "get_temporal_raw_data",
            return_value=country_rows,
            autospec=True,
            spec_set=True,
        ),
        mock.patch.object(
            data_loader,
            "get_fir_temporal_raw_data",
            return_value=[],
            autospec=True,
            spec_set=True,
        ),
    ):
      res_fallback = self.client.get(
          "/temporal/2025", params={"iso": "FR", "fir_id": "LFBB_BORDEAUX"}
      )
      self.assertEqual(res_fallback.status_code, 200)
      data_fallback = res_fallback.json()
      self.assertFalse(data_fallback["is_fir"])
      self.assertEqual(data_fallback["name"], "France")
      self.assertEqual(data_fallback["co2e_gwp50_tonnes"][12][4], 55.0)
      self.assertIsNone(
          data_loader.get_cached_temporal_matrix((
              data_loader.resolve_dataset_dir(2025, testdata_fixture.DATASET),
              2025,
              testdata_fixture.DATASET,
              "FR",
              "LFBB_BORDEAUX",
          ))
      )

  def test_temporal_fir_serves_fir_matrix_when_rows_available(self):
    fir_rows = [
        _temporal_bundle(
            {
                "fir_id": "LFBB_BORDEAUX",
                "fir_icao": "LFBB",
                "firname": "BORDEAUX",
                "fir_to_country_iso2s": ["FR"],
            },
            {(12, 5): {"co2e_gwp50_tonnes": 30.0, "segment_count": 70.0}},
        )
    ]
    data_loader.clear_all_caches()
    with mock.patch.object(
        data_loader,
        "get_fir_temporal_raw_data",
        return_value=fir_rows,
        autospec=True,
        spec_set=True,
    ):
      res_fir = self.client.get(
          "/temporal/2025",
          params={"iso": "FR", "fir_id": "LFBB_BORDEAUX"},
      )
      self.assertEqual(res_fir.status_code, 200)
      data_fir = res_fir.json()
      self.assertTrue(data_fir["is_fir"])
      self.assertEqual(data_fir["fir_id"], "LFBB_BORDEAUX")
      self.assertEqual(data_fir["name"], "FIR BORDEAUX (LFBB)")
      self.assertEqual(data_fir["co2e_gwp50_tonnes"][12][4], 30.0)

  def _publish_nested_grid_meta_fixture(self, root: str) -> None:
    run_dir = _publish_run_dir(root, "2025")
    os.makedirs(run_dir, exist_ok=True)
    meta = {
        "width": 10,
        "height": 5,
        "bbox": {
            "min_lon": -180.0,
            "max_lon": 180.0,
            "min_lat": -90.0,
            "max_lat": 90.0,
        },
        "value_max_ref_scaled": {"total_global_impact_grid": 42.0},
    }
    with open(
        os.path.join(run_dir, "grid_meta.json"), "w", encoding="utf-8"
    ) as f:
      json.dump(meta, f)
    with open(
        os.path.join(run_dir, "total_global_impact_grid.bin.gz"), "wb"
    ) as f:
      f.write(gzip.compress(b"\x00" * 8))

  def test_get_grid_meta_returns_deep_copy_of_nested_dicts(self):
    root = self.create_tempdir().full_path
    self._publish_nested_grid_meta_fixture(root)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with mock.patch.object(data_loader, "DATA_DIR", root):
      meta1 = data_loader.get_grid_meta(
          "2025", prefix="total_global_impact_grid"
      )
      self.assertIsNotNone(meta1)
      meta1["bbox"]["min_lon"] = 999.0
      meta1["value_max_ref_scaled"]["mutated"] = -1.0

      meta2 = data_loader.get_grid_meta(
          "2025", prefix="total_global_impact_grid"
      )
      self.assertIsNotNone(meta2)
      self.assertEqual(meta2["bbox"]["min_lon"], -180.0)
      self.assertNotIn("mutated", meta2["value_max_ref_scaled"])

  def _setup_kzak_fir_temporal(self) -> None:
    fir_rows = [
        _make_kzak_bundle(
            "KZAK_OAKLAND_OCEANIC_EAST",
            "OAKLAND OCEANIC EAST",
            100.0,
            500.0,
        ),
        _make_kzak_bundle(
            "KZAK_OAKLAND_OCEANIC_WEST",
            "OAKLAND OCEANIC WEST",
            250.0,
            800.0,
        ),
    ]
    data_loader.clear_all_caches()
    self.enter_context(
        mock.patch.object(
            data_loader,
            "get_fir_temporal_raw_data",
            return_value=fir_rows,
            autospec=True,
            spec_set=True,
        )
    )

  def test_temporal_fir_sector_disambiguation_west(self):
    self._setup_kzak_fir_temporal()
    res_west = self.client.get(
        "/temporal/2025",
        params={"iso": "US", "fir_id": "KZAK_OAKLAND_OCEANIC_WEST"},
    )
    self.assertEqual(res_west.status_code, 200)
    data_west = res_west.json()
    self.assertTrue(data_west["is_fir"])
    self.assertEqual(data_west["fir_id"], "KZAK_OAKLAND_OCEANIC_WEST")
    self.assertEqual(data_west["name"], "FIR OAKLAND OCEANIC WEST (KZAK)")
    self.assertEqual(data_west["co2e_gwp50_tonnes"][14][5], 250.0)

  def test_temporal_fir_sector_disambiguation_east(self):
    self._setup_kzak_fir_temporal()
    res_east = self.client.get(
        "/temporal/2025",
        params={"iso": "US", "fir_id": "KZAK_OAKLAND_OCEANIC_EAST"},
    )
    self.assertEqual(res_east.status_code, 200)
    data_east = res_east.json()
    self.assertTrue(data_east["is_fir"])
    self.assertEqual(data_east["fir_id"], "KZAK_OAKLAND_OCEANIC_EAST")
    self.assertEqual(data_east["name"], "FIR OAKLAND OCEANIC EAST (KZAK)")
    self.assertEqual(data_east["co2e_gwp50_tonnes"][14][5], 100.0)

  def test_temporal_unknown_fir_without_iso_returns_404(self):
    # A FIR-only query that matches no FIR has no country to fall back to.
    resp = self.client.get(
        "/temporal/2025",
        params={"fir_id": "NONEXISTENT_FIR_XYZ_123"},
    )
    self.assertEqual(resp.status_code, 404)
    self.assertIn("not found", resp.json().get("detail", "").lower())

  def test_temporal_matrix_caching(self):
    data_loader.clear_all_caches()

    res1 = self.client.get("/temporal/2025", params={"iso": "CH"})
    self.assertEqual(res1.status_code, 200)
    self.assertLen(data_loader._TEMPORAL_MATRIX_CACHE, 1)

    # Lower-case ISO should hit the normalized cache entry
    res2 = self.client.get("/temporal/2025", params={"iso": " ch "})
    self.assertEqual(res2.status_code, 200)
    self.assertLen(data_loader._TEMPORAL_MATRIX_CACHE, 1)
    self.assertEqual(res1.json(), res2.json())

  def test_temporal_data_caching(self):
    data_loader.clear_all_caches()
    records1 = data_loader.get_temporal_raw_data("2025")
    self.assertEqual(data_loader._load_tabular_json.cache_info().currsize, 1)
    records2 = data_loader.get_temporal_raw_data("2025")
    self.assertIs(records1, records2)

  def test_temporal_helpers_and_direct_cache_fir_metadata(self):
    fir_rows = [
        _temporal_bundle(
            {
                "fir_id": "EHAA_AMSTERDAM",
                "fir_icao": "EHAA",
                "firname": "AMSTERDAM",
                "fir_to_country_iso2s": ["NL"],
            },
            {(12, 1): {"co2e_gwp50_tonnes": 10.0}},
        ),
    ]
    with mock.patch.object(
        data_loader,
        "get_fir_temporal_raw_data",
        return_value=fir_rows,
        autospec=True,
        spec_set=True,
    ):
      fir_resp = server.get_temporal_metrics(
          2025, iso=None, fir_id="EHAA_AMSTERDAM"
      )
    self.assertTrue(fir_resp.is_fir)
    self.assertEqual(fir_resp.fir_id, "EHAA_AMSTERDAM")
    self.assertEqual(fir_resp.fir_name, "AMSTERDAM")
    self.assertEqual(fir_resp.fir_icao, "EHAA")
    self.assertEqual(fir_resp.iso_a2, "NL")

  def test_temporal_helpers_and_direct_cache_matrix_values(self):
    fir_rows = [
        _temporal_bundle(
            {
                "fir_id": "EHAA_AMSTERDAM",
                "fir_icao": "EHAA",
                "firname": "AMSTERDAM",
                "fir_to_country_iso2s": ["NL"],
            },
            {(12, 1): {"co2e_gwp50_tonnes": 10.0}},
        ),
    ]
    with mock.patch.object(
        data_loader,
        "get_fir_temporal_raw_data",
        return_value=fir_rows,
        autospec=True,
        spec_set=True,
    ):
      fir_resp = server.get_temporal_metrics(
          2025, iso=None, fir_id="EHAA_AMSTERDAM"
      )
    self.assertEqual(fir_resp.co2e_gwp50_tonnes[12][0], 10.0)
    self.assertEqual(fir_resp.co2e_gwp50_tonnes[14][0], 0.0)

  def test_temporal_country_row_matching_and_direct_cache(self):
    country_rows = [
        _temporal_bundle(
            {"iso_a2": "FR", "name": "France"},
            {(10, 1): {"co2e_gwp50_tonnes": 50.0}},
        ),
        _temporal_bundle(
            {"iso_a2": "DE", "name": "Germany"},
            {(11, 1): {"co2e_gwp50_tonnes": 60.0}},
        ),
    ]
    with mock.patch.object(
        data_loader,
        "get_temporal_raw_data",
        return_value=country_rows,
        autospec=True,
        spec_set=True,
    ):
      country_resp = server.get_temporal_metrics(2025, iso="FR", fir_id=None)
    self.assertFalse(country_resp.is_fir)
    self.assertEqual(country_resp.name, "France")
    self.assertEqual(country_resp.iso_a2, "FR")
    self.assertEqual(country_resp.co2e_gwp50_tonnes[10][0], 50.0)
    self.assertEqual(country_resp.co2e_gwp50_tonnes[11][0], 0.0)

  def test_sum_temporal_matrices_sanitizes_nan_inf_and_invalid_values(self):
    corrupt = _temporal_bundle(
        {"iso_a2": "CH"},
        {
            (6, 3): {
                "co2e_gwp50_tonnes": float("nan"),
                "segment_count": "Infinity",
                "contrail_segment_count": "corrupt",
            }
        },
    )
    corrupt_bools = _temporal_bundle(
        {"iso_a2": "CH"},
        {
            (6, 3): {
                "co2e_gwp50_tonnes": True,
                "segment_count": float("-inf"),
                "contrail_segment_count": False,
            }
        },
    )
    with mock.patch.object(
        data_loader,
        "get_temporal_raw_data",
        return_value=[corrupt, corrupt_bools],
        autospec=True,
        spec_set=True,
    ):
      sums = server.get_temporal_metrics(2025, iso="CH", fir_id=None)
    self.assertEqual(sums.co2e_gwp50_tonnes[6][2], 0.0)
    self.assertEqual(sums.segment_count[6][2], 0)
    self.assertEqual(sums.contrail_segment_count[6][2], 0)
    self.assertIs(type(sums.segment_count[6][2]), int)
    self.assertIs(type(sums.contrail_segment_count[6][2]), int)

  def test_sum_temporal_matrices_preserves_valid_cell_with_corrupt_rows(self):
    corrupt_cell = {
        "co2e_gwp50_tonnes": float("nan"),
        "segment_count": "Infinity",
        "contrail_segment_count": "corrupt",
    }
    bool_cell = {
        "co2e_gwp50_tonnes": True,
        "segment_count": float("-inf"),
        "contrail_segment_count": False,
    }
    # A valid row in the same cell survives unpoisoned.
    valid_cell = {
        "co2e_gwp50_tonnes": 42.5,
        "segment_count": 10,
        "contrail_segment_count": 3,
    }
    rows = [
        _temporal_bundle({"iso_a2": "CH"}, {(6, 3): cell})
        for cell in (corrupt_cell, valid_cell, bool_cell)
    ]
    with mock.patch.object(
        data_loader,
        "get_temporal_raw_data",
        return_value=rows,
        autospec=True,
        spec_set=True,
    ):
      sums = server.get_temporal_metrics(2025, iso="CH", fir_id=None)
    self.assertEqual(sums.co2e_gwp50_tonnes[6][2], 42.5)
    self.assertEqual(sums.segment_count[6][2], 10)
    self.assertEqual(sums.contrail_segment_count[6][2], 3)
    self.assertIs(type(sums.segment_count[6][2]), int)
    self.assertIs(type(sums.contrail_segment_count[6][2]), int)

  def test_sum_temporal_matrices_rejects_flat_row(self):
    flat_row = {"iso_a2": "CH", "month": "202501", "local_hour": 2}
    with mock.patch.object(
        data_loader,
        "get_temporal_raw_data",
        return_value=[flat_row],
        autospec=True,
        spec_set=True,
    ):
      with self.assertRaisesRegex(
          data_loader.ArtifactUnavailableError, r"co2e_gwp50_tonnes"
      ):
        server.get_temporal_metrics(2025, iso="CH", fir_id=None)

  @parameterized.named_parameters(
      (
          "non_list_required_matrix",
          {
              "iso_a2": "CH",
              "co2e_gwp50_tonnes": 123.0,
              "segment_count": [[1] * 12 for _ in range(24)],
              "contrail_segment_count": [[1] * 12 for _ in range(24)],
          },
          "co2e_gwp50_tonnes",
      ),
      (
          "non_list_optional_matrix",
          {
              "iso_a2": "CH",
              "co2e_gwp50_tonnes": [[1.0] * 12 for _ in range(24)],
              "segment_count": [[1] * 12 for _ in range(24)],
              "contrail_segment_count": [[1] * 12 for _ in range(24)],
              "total_flight_distance_km": "not-a-matrix",
          },
          "total_flight_distance_km",
      ),
      (
          "truncated_outer_matrix",
          {
              "iso_a2": "CH",
              "co2e_gwp50_tonnes": [[1.0] * 12 for _ in range(10)],
              "segment_count": [[1] * 12 for _ in range(24)],
              "contrail_segment_count": [[1] * 12 for _ in range(24)],
          },
          "co2e_gwp50_tonnes",
      ),
      (
          "non_list_hour_row",
          {
              "iso_a2": "CH",
              "co2e_gwp50_tonnes": ["corrupt"] * 24,
              "segment_count": [[1] * 12 for _ in range(24)],
              "contrail_segment_count": [[1] * 12 for _ in range(24)],
          },
          "co2e_gwp50_tonnes",
      ),
      (
          "truncated_hour_row",
          {
              "iso_a2": "CH",
              "co2e_gwp50_tonnes": [[1.0] * 12 for _ in range(24)],
              "segment_count": [[1] * 11 for _ in range(24)],
              "contrail_segment_count": [[1] * 12 for _ in range(24)],
          },
          "segment_count",
      ),
  )
  def test_temporal_endpoint_rejects_non_list_matrix_with_503(
      self, bad_row: dict[str, Any], expected_field: str
  ):
    with mock.patch.object(
        data_loader,
        "get_temporal_raw_data",
        return_value=[bad_row],
        autospec=True,
        spec_set=True,
    ):
      resp = self.client.get("/temporal/2025", params={"iso": "CH"})
    self.assertEqual(resp.status_code, 503)
    self.assertIn(expected_field, resp.json()["detail"])

  def test_sum_temporal_matrices_handles_missing_optional_fields(self):
    minimal_row = {
        "iso_a2": "CH",
        "co2e_gwp50_tonnes": [[1.0] * 12 for _ in range(24)],
        "segment_count": [[2] * 12 for _ in range(24)],
        "contrail_segment_count": [[1] * 12 for _ in range(24)],
        "co2e_gwp20_tonnes": None,
        "total_flight_distance_km": None,
    }
    with mock.patch.object(
        data_loader,
        "get_temporal_raw_data",
        return_value=[minimal_row],
        autospec=True,
        spec_set=True,
    ):
      sums = server.get_temporal_metrics(2025, iso="CH", fir_id=None)
    self.assertIsNotNone(sums.co2e_gwp20_tonnes)
    self.assertIsNotNone(sums.total_flight_distance_km)
    self.assertEqual(sums.co2e_gwp20_tonnes[0][0], 0.0)
    self.assertEqual(sums.total_flight_distance_km[0][0], 0.0)

  def test_sum_temporal_matrices_clamps_oversized_dimensions(self):
    oversized_row = {
        "iso_a2": "CH",
        "co2e_gwp50_tonnes": [[1.0] * 15 for _ in range(26)],
        "segment_count": [[2] * 12 for _ in range(24)],
        "contrail_segment_count": [[1] * 12 for _ in range(24)],
    }
    with mock.patch.object(
        data_loader,
        "get_temporal_raw_data",
        return_value=[oversized_row],
        autospec=True,
        spec_set=True,
    ):
      sums = server.get_temporal_metrics(2025, iso="CH", fir_id=None)
    self.assertEqual(sums.co2e_gwp50_tonnes[0][0], 1.0)
    self.assertLen(sums.co2e_gwp50_tonnes, 24)
    self.assertLen(sums.co2e_gwp50_tonnes[0], 12)

  def test_temporal_response_builder_fir_metadata(self):
    fir_identity = {
        "fir_id": "LFBB_BORDEAUX",
        "fir_icao": "LFBB",
        "firname": "Bordeaux FIR",
        "fir_to_country_iso2s": ["FR"],
    }
    cell = {
        "co2e_gwp50_tonnes": 100.0,
        "contrail_segment_count": 12,
        "segment_count": 50,
    }
    rows = [_temporal_bundle(fir_identity, {(2, 1): cell})] * 2
    with mock.patch.object(
        data_loader,
        "get_fir_temporal_raw_data",
        return_value=rows,
        autospec=True,
        spec_set=True,
    ):
      fir_resp = server.get_temporal_metrics(
          2025, iso=None, fir_id="LFBB_BORDEAUX"
      )
    self.assertTrue(fir_resp.is_fir)
    self.assertEqual(fir_resp.iso_a2, "FR")
    self.assertEqual(fir_resp.name, "FIR Bordeaux FIR (LFBB)")
    self.assertEqual(fir_resp.fir_icao, "LFBB")
    self.assertEqual(fir_resp.fir_name, "Bordeaux FIR")

  def test_temporal_response_builder_matrices(self):
    fir_identity = {
        "fir_id": "LFBB_BORDEAUX",
        "fir_icao": "LFBB",
        "firname": "Bordeaux FIR",
        "fir_to_country_iso2s": ["FR"],
    }
    cell = {
        "co2e_gwp50_tonnes": 100.0,
        "contrail_segment_count": 12,
        "segment_count": 50,
    }
    rows = [_temporal_bundle(fir_identity, {(2, 1): cell})] * 2
    with mock.patch.object(
        data_loader,
        "get_fir_temporal_raw_data",
        return_value=rows,
        autospec=True,
        spec_set=True,
    ):
      fir_resp = server.get_temporal_metrics(
          2025, iso=None, fir_id="LFBB_BORDEAUX"
      )
    self.assertEqual(fir_resp.co2e_gwp50_tonnes[2][0], 200.0)
    self.assertEqual(fir_resp.segment_count[2][0], 100)
    self.assertNotIn("total_segment_count", fir_resp.model_dump())
    self.assertEqual(fir_resp.contrail_segment_count[2][0], 24)
    self.assertEqual((fir_resp.months[0], len(fir_resp.hours)), ("202501", 24))

  def test_temporal_endpoint_survives_nan_and_inf_cells_end_to_end(self):
    nan_rows = [
        _temporal_bundle(
            {"iso_a2": "CH", "name": "Switzerland"},
            {
                (9, 7): {
                    "co2e_gwp50_tonnes": float("nan"),
                    "segment_count": float("nan"),
                    "contrail_segment_count": "Infinity",
                }
            },
        ),
        _temporal_bundle(
            {"iso_a2": "CH", "name": "Switzerland"},
            {
                (9, 7): {
                    "co2e_gwp50_tonnes": 15.0,
                    "segment_count": 4,
                    "contrail_segment_count": 1,
                }
            },
        ),
    ]
    data_loader.clear_all_caches()
    with mock.patch.object(
        data_loader,
        "get_temporal_raw_data",
        return_value=nan_rows,
        autospec=True,
        spec_set=True,
    ):
      response = self.client.get("/temporal/2025", params={"iso": "CH"})
      self.assertEqual(response.status_code, 200)
      payload = response.json()
      self.assertEqual(payload["co2e_gwp50_tonnes"][9][6], 15.0)
      self.assertNotIn("prop_sum_ef_mj", payload)
      self.assertEqual(payload["segment_count"][9][6], 4.0)
      self.assertEqual(payload["contrail_segment_count"][9][6], 1.0)

  def _publish_nan_inf_dataset(self, root: str) -> None:
    run_dir = _publish_run_dir(root, "2025")
    os.makedirs(run_dir, exist_ok=True)
    with open(
        os.path.join(run_dir, constants.COUNTRY_EXPORT_FILENAME),
        "w",
        encoding="utf-8",
    ) as f:
      json.dump(
          [{
              "iso_a2": "CH",
              "name": "Switzerland",
              "mean_rf_mw_per_sq_m": float("nan"),
              "co2e_gwp50_tonnes": float("inf"),
              "co2e_gwp50_tonnes_rank": float("nan"),
              "area_sq_km": float("-inf"),
          }],
          f,
          allow_nan=True,
      )
    with open(
        os.path.join(run_dir, "fir_density_df_export_per_country.json"),
        "w",
        encoding="utf-8",
    ) as f:
      json.dump(
          [{
              "fir_id": "LSAS_SWITZERLAND",
              "fir_icao": "LSAS",
              "firname": "SWITZERLAND",
              "fir_to_country_iso2s": ["CH"],
              "mean_rf_mw_per_sq_m": float("nan"),
              "co2e_gwp50_tonnes": float("-inf"),
              "co2e_gwp50_tonnes_rank": float("nan"),
              "area_sq_km": float("inf"),
          }],
          f,
          allow_nan=True,
      )

  def test_single_country_endpoint_sanitizes_nan_and_inf_floats(self):
    root = self.create_tempdir().full_path
    self._publish_nan_inf_dataset(root)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with mock.patch.object(data_loader, "DATA_DIR", root):
      res_country = self.client.get("/country/2025/CH")
      self.assertEqual(res_country.status_code, 200)
      country_json = res_country.json()
      self.assertIsNone(country_json["mean_rf_mw_per_sq_m"])
      self.assertIsNone(country_json["co2e_gwp50_tonnes"])
      self.assertIsNone(country_json["co2e_gwp50_tonnes_rank"])
      self.assertIsNone(country_json["area_sq_km"])

  def test_countries_endpoint_sanitizes_nan_and_inf_floats(self):
    root = self.create_tempdir().full_path
    self._publish_nan_inf_dataset(root)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with mock.patch.object(data_loader, "DATA_DIR", root):
      res_countries = self.client.get("/countries/2025")
      self.assertEqual(res_countries.status_code, 200)
      self.assertLen(res_countries.json(), 1)
      self.assertIsNone(res_countries.json()[0]["mean_rf_mw_per_sq_m"])

  def test_firs_endpoint_sanitizes_nan_and_inf_floats(self):
    root = self.create_tempdir().full_path
    self._publish_nan_inf_dataset(root)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with mock.patch.object(data_loader, "DATA_DIR", root):
      res_firs = self.client.get("/firs/2025")
      self.assertEqual(res_firs.status_code, 200)
      self.assertLen(res_firs.json(), 1)
      self.assertIsNone(res_firs.json()[0]["mean_rf_mw_per_sq_m"])
      self.assertIsNone(res_firs.json()[0]["co2e_gwp50_tonnes_rank"])
      self.assertIsNone(res_firs.json()[0]["area_sq_km"])


async def _exercise_server_lifespan() -> None:
  """Runs the server lifespan context manager briefly to trigger preload."""
  async with server.lifespan(server.app):
    await asyncio.sleep(0.05)


class UnsupportedYearAndLifespanTest(DashboardTestBase):
  """Tests unsupported-year handling and lifespan resilience."""

  def test_temporal_unsupported_year_returns_400_and_lifespan_survives_error(
      self,
  ):
    res_bad_year = self.client.get("/temporal/1999")
    self.assertEqual(res_bad_year.status_code, 400)
    self.assertIn("Year '1999' is not available", res_bad_year.json()["detail"])

    with mock.patch.object(
        data_loader,
        "preload_all_data",
        side_effect=RuntimeError("Simulated preload failure"),
        autospec=True,
        spec_set=True,
    ):
      asyncio.run(_exercise_server_lifespan())

  def test_lifespan_preload_survives_artifact_unavailable_error(self):
    with mock.patch.object(
        data_loader,
        "preload_all_data",
        side_effect=data_loader.ArtifactUnavailableError(
            "Simulated unreadable artifact during preload"
        ),
        autospec=True,
        spec_set=True,
    ):
      asyncio.run(_exercise_server_lifespan())
    self.assertEmpty(server._BACKGROUND_TASKS)


class StaticAssetTest(DashboardTestBase):
  """Tests the static asset routes and relative path resolution."""

  def test_serve_favicon(self):
    response = self.client.get("/favicon.ico")
    self.assertEqual(response.status_code, 200)
    self.assertIn("image", response.headers.get("content-type", ""))

    response_svg = self.client.get("/favicon.svg")
    self.assertEqual(response_svg.status_code, 200)
    self.assertIn("svg", response_svg.headers.get("content-type", ""))

  def test_serve_static_index(self):
    response = self.client.get("/")
    self.assertEqual(response.status_code, 200)
    self.assertIn("<html", response.text.lower())

  def test_serve_index_prefers_ui_dist_dir_when_present(self):
    dist_dir = self.create_tempdir().full_path
    dist_index = os.path.join(dist_dir, "index.html")
    with open(dist_index, "w", encoding="utf-8") as f:
      f.write("<!doctype html><html><body>Vite Production Build</body></html>")

    with mock.patch.object(static_assets, "UI_DIST_DIR", dist_dir):
      response = self.client.get("/")
      self.assertEqual(response.status_code, 200)
      self.assertIn("Vite Production Build", response.text)

  def test_serve_vite_asset_serves_dist_assets_with_etag_and_gzip(self):
    dist_dir = self.create_tempdir().full_path
    assets_dir = os.path.join(dist_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    js_file = os.path.join(assets_dir, "app-12345.js")
    with open(js_file, "w", encoding="utf-8") as f:
      f.write("console.log('vite asset');\n")

    with mock.patch.object(static_assets, "UI_DIST_DIR", dist_dir):
      static_assets.clear_static_asset_caches()
      res1 = self.client.get("/assets/app-12345.js")
      self.assertEqual(
          (res1.status_code, res1.headers.get("content-encoding")),
          (200, "gzip"),
      )
      self.assertIn("javascript", res1.headers.get("content-type", ""))
      self.assertIn("console.log('vite asset');", res1.text)
      etag = res1.headers.get("etag")
      self.assertIsNotNone(etag)

      res304 = self.client.get(
          "/assets/app-12345.js", headers={"If-None-Match": etag}
      )
      self.assertEqual(res304.status_code, 304)

  def test_serve_vite_asset_rejects_path_traversal_and_missing_files(self):
    dist_dir = self.create_tempdir().full_path
    os.makedirs(os.path.join(dist_dir, "assets"), exist_ok=True)
    secret_file = os.path.join(dist_dir, "secret.txt")
    with open(secret_file, "w", encoding="utf-8") as f:
      f.write("secret")

    with mock.patch.object(static_assets, "UI_DIST_DIR", dist_dir):
      static_assets.clear_static_asset_caches()
      res_missing = self.client.get("/assets/does_not_exist.js")
      self.assertEqual(res_missing.status_code, 404)

      with self.assertRaisesRegex(
          fastapi.HTTPException, "Asset not found"
      ) as ctx:
        static_assets.serve_vite_asset("../secret.txt")
      self.assertEqual(ctx.exception.status_code, 404)

  def test_serve_vite_asset_rejects_symlinks_escaping_assets_dir(self):
    dist_dir = self.create_tempdir().full_path
    assets_dir = os.path.join(dist_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    secret = self.create_tempfile(content="secret").full_path
    os.symlink(secret, os.path.join(assets_dir, "leak.js"))
    with mock.patch.object(static_assets, "UI_DIST_DIR", dist_dir):
      static_assets.clear_static_asset_caches()
      response = self.client.get("/assets/leak.js")
    self.assertEqual(response.status_code, 404)

  def test_serve_vite_asset_media_type_follows_requested_name(self):
    dist_dir = self.create_tempdir().full_path
    assets_dir = os.path.join(dist_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    blob = os.path.join(assets_dir, "blob123")
    with open(blob, "wb") as f:
      f.write(b"x")
    os.symlink(blob, os.path.join(assets_dir, "app.js"))
    with mock.patch.object(static_assets, "UI_DIST_DIR", dist_dir):
      static_assets.clear_static_asset_caches()
      response = self.client.get("/assets/app.js")
    self.assertEqual(
        (response.status_code, response.headers["content-type"].split(";")[0]),
        (200, "text/javascript"),
    )

  @parameterized.named_parameters(
      ("js", "app.js", "text/javascript"),
      ("mjs", "chunk.mjs", "text/javascript"),
      ("css", "style.css", "text/css"),
      ("wasm", "module.wasm", "application/wasm"),
      ("woff2", "font.woff2", "font/woff2"),
      ("sourcemap", "app.js.map", "application/json"),
  )
  def test_serve_vite_asset_pins_media_types_independent_of_host(
      self, filename, expected_type
  ):
    dist_dir = self.create_tempdir().full_path
    assets_dir = os.path.join(dist_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    with open(os.path.join(assets_dir, filename), "wb") as f:
      f.write(b"x")

    # Simulate a host whose mime.types maps everything to text/plain.
    with (
        mock.patch.object(static_assets, "UI_DIST_DIR", dist_dir),
        mock.patch.object(
            static_assets.mimetypes,
            "guess_type",
            return_value=("text/plain", None),
            autospec=True,
            spec_set=True,
        ),
    ):
      static_assets.clear_static_asset_caches()
      response = self.client.get(f"/assets/{filename}")
      self.assertEqual(response.status_code, 200)
      self.assertEqual(
          response.headers["content-type"].split(";")[0], expected_type
      )

  def test_static_payload_cache_holds_a_vite_build(self):
    self.assertEqual(
        static_assets.load_static_payload_cached.cache_info().maxsize, 64
    )

  def _publish_boundaries(self) -> str:
    """Writes boundary fixtures into a temp run dir and routes the server to it.

    Returns:
      The temp dataset directory the server now resolves boundaries from.
    """
    dataset_dir = self.create_tempdir().full_path
    artifact_io.write_boundary_geojson(
        dataset_dir,
        [{
            "type": "Feature",
            "properties": {"name": "Kosovo", "iso_a2": "-99"},
            "geometry": None,
        }],
        [{
            "type": "Feature",
            "properties": {"fir_id": "EDGG", "fir_icao": "EDGG"},
            "geometry": None,
        }],
    )
    # Discover the years from the real testdata first: the patch below would
    # otherwise hide their exports too. The discovery result is cached.
    data_loader.get_available_years()
    patcher = mock.patch.object(
        data_loader,
        "resolve_dataset_dir",
        return_value=dataset_dir,
        autospec=True,
        spec_set=True,
    )
    self.mock_resolve_dataset_dir = patcher.start()
    self.addCleanup(patcher.stop)
    return dataset_dir

  def test_serve_boundary_geojson_from_active_run(self):
    self._publish_boundaries()
    countries = self.client.get("/countries/2025.json")
    self.assertEqual(countries.status_code, 200)
    self.assertEqual(
        countries.headers.get("content-type"), "application/geo+json"
    )
    self.assertEqual(countries.json()["type"], "FeatureCollection")
    self.assertEqual(
        countries.json()["features"][0]["properties"]["name"], "Kosovo"
    )

  @parameterized.parameters(
      "identity",
      "gzip;q=0, identity;q=1.0",
      "*;q=1, gzip;q=0",
      "*;q=0",
  )
  def test_boundary_geojson_honors_identity_accept_encoding(
      self, accept_encoding: str
  ):
    self._publish_boundaries()
    resp = self.client.get(
        "/countries/2025.json",
        headers={"Accept-Encoding": accept_encoding},
    )
    self.assertEqual(resp.status_code, 200)
    self.assertIsNone(resp.headers.get("content-encoding"))
    self.assertIn("Accept-Encoding", resp.headers.get("vary", ""))
    self.assertEqual(resp.json()["type"], "FeatureCollection")

  @parameterized.parameters(
      "gzip",
      "x-gzip",
      "*;q=0.8",
      "*;q=0.8, *;q=0",
      "gzip;q=0.5, *;q=0",
      "gzip;q=-0.5",
      "gzip;q=1.5",
      "gzip;q=nan, gzip;q=0.8",
  )
  def test_boundary_geojson_honors_gzip_accept_encoding(
      self, accept_encoding: str
  ):
    self._publish_boundaries()
    resp = self.client.get(
        "/countries/2025.json",
        headers={"Accept-Encoding": accept_encoding},
    )
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.headers.get("content-encoding"), "gzip")
    self.assertIn("Accept-Encoding", resp.headers.get("vary", ""))
    self.assertEqual(resp.json()["type"], "FeatureCollection")

  def test_serve_firs_boundary_geojson_from_active_run(self):
    self._publish_boundaries()
    firs = self.client.get("/firs/2025.json")
    self.assertEqual(firs.status_code, 200)
    self.assertEqual(firs.json()["features"][0]["properties"]["fir_id"], "EDGG")

  @parameterized.parameters("countries", "firs")
  def test_list_route_serves_json_array_without_geojson_shadowing(
      self, resource
  ):
    listing = self.client.get(f"/{resource}/2025")
    self.assertEqual(listing.status_code, 200)
    self.assertEqual(listing.headers.get("content-type"), "application/json")
    self.assertIsInstance(listing.json(), list)
    self.assertNotEmpty(listing.json())

  @parameterized.parameters("countries", "firs")
  def test_boundary_geojson_route_serves_feature_collection(self, resource):
    self._publish_boundaries()
    geojson = self.client.get(f"/{resource}/2025.json")
    self.assertEqual(geojson.status_code, 200)
    self.assertEqual(
        geojson.headers.get("content-type"), "application/geo+json"
    )
    self.assertEqual(geojson.json()["type"], "FeatureCollection")

  @parameterized.parameters("/countries/2023.json", "/firs/2023.json")
  def test_boundary_geojson_reads_the_requested_years_run(self, route):
    self._publish_boundaries()
    resp = self.client.get(route)
    self.assertEqual(resp.status_code, 200)
    self.mock_resolve_dataset_dir.assert_called_with(
        2023, testdata_fixture.DATASET
    )

  @parameterized.parameters("/countries.json", "/firs.json")
  def test_boundary_geojson_old_query_style_routes_are_gone(self, route):
    resp = self.client.get(route, params={"year": "2025"})
    self.assertEqual(resp.status_code, 404)

  @parameterized.parameters("/countries/1999.json", "/firs/1999.json")
  def test_boundary_geojson_rejects_an_unknown_year(self, route):
    resp = self.client.get(route)
    self.assertEqual(resp.status_code, 400)
    self.assertIn("Year '1999' is not available", resp.json()["detail"])

  @parameterized.parameters("/countries/2025.json", "/firs/2025.json")
  def test_serve_boundary_geojson_missing_from_run_returns_404(self, route):
    empty_dir = self.create_tempdir().full_path
    data_loader.get_available_years()  # Cache years before hiding the run.
    with mock.patch.object(
        data_loader,
        "resolve_dataset_dir",
        return_value=empty_dir,
        autospec=True,
        spec_set=True,
    ):
      resp = self.client.get(route)
      self.assertEqual(resp.status_code, 404)
      self.assertIn("re-derive", resp.json()["detail"])

  def test_unreadable_boundary_geojson_returns_503_and_is_not_cached(self):
    self._publish_boundaries()
    static_assets.clear_static_asset_caches()
    data_loader.get_available_years()  # Cache years before reads fail.
    real_open_file = fs.open_file
    with mock.patch.object(
        fs,
        "open_file",
        side_effect=fs.Error("Simulated storage error"),
        autospec=True,
        spec_set=True,
    ):
      failed = self.client.get("/countries/2025.json")
    with mock.patch.object(
        fs,
        "open_file",
        side_effect=real_open_file,
        autospec=True,
        spec_set=True,
    ):
      recovered = self.client.get("/countries/2025.json")
    self.assertEqual((failed.status_code, recovered.status_code), (503, 200))
    static_assets.clear_static_asset_caches()

  def test_boundary_geojson_exists_check_failure_is_503_and_not_cached(self):
    self._publish_boundaries()
    static_assets.clear_static_asset_caches()
    data_loader.get_available_years()  # Cache years before checks fail.
    real_exists = fs.exists
    with mock.patch.object(
        fs,
        "exists",
        side_effect=fs.Error("Simulated storage error"),
        autospec=True,
        spec_set=True,
    ):
      failed = self.client.get("/countries/2025.json")
    with mock.patch.object(
        fs, "exists", side_effect=real_exists, autospec=True, spec_set=True
    ):
      recovered = self.client.get("/countries/2025.json")
    self.assertEqual((failed.status_code, recovered.status_code), (503, 200))
    static_assets.clear_static_asset_caches()

  def test_safe_exists_and_resolve_static_file(self):
    self.assertTrue(
        data_loader.safe_exists(
            os.path.join(static_assets._PACKAGE_DIR, "ui/index.html")
        )
    )
    self.assertFalse(
        data_loader.safe_exists("path/to/nonexistent/file/abc123xyz.txt")
    )

    path = static_assets.resolve_static_file("ui/index.html")
    self.assertIsNotNone(path)
    self.assertTrue(os.path.exists(path))

    no_path = static_assets.resolve_static_file("invalid/nonexistent/file.xyz")
    self.assertIsNone(no_path)

  def test_script_dir_defined(self):
    self.assertTrue(hasattr(static_assets, "_SCRIPT_DIR"))
    self.assertTrue(os.path.isdir(static_assets._SCRIPT_DIR))

  def test_configure_cors_origin_regex(self):
    self.addCleanup(
        server.configure_cors_origin_regex,
        server.DEFAULT_CORS_ORIGIN_REGEX,
    )
    custom_regex = r"https://example\.org"
    server.configure_cors_origin_regex(custom_regex)
    cors_mw = [
        middleware
        for middleware in server.app.user_middleware
        if middleware.cls is server.cors.CORSMiddleware
    ]
    self.assertLen(cors_mw, 1)
    self.assertEqual(cors_mw[0].kwargs["allow_origin_regex"], custom_regex)

  def test_cors_preflight_allows_only_safe_methods_without_credentials(self):
    resp = self.client.options(
        "/years",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(
        resp.headers.get("access-control-allow-origin"),
        "http://localhost:5173",
    )
    self.assertIsNone(resp.headers.get("access-control-allow-credentials"))
    self.assertNotIn("POST", resp.headers.get("access-control-allow-methods"))

  def test_cors_preflight_rejects_unsafe_methods(self):
    resp = self.client.options(
        "/years",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "DELETE",
        },
    )
    self.assertEqual(resp.status_code, 400)

  def test_cors_preflight_rejects_non_safelisted_request_headers(self):
    resp = self.client.options(
        "/years",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Custom",
        },
    )
    self.assertEqual(resp.status_code, 400)

  def test_gzip_middleware_configured_with_compresslevel_5(self):
    gzip_mw = [
        middleware
        for middleware in server.app.user_middleware
        if middleware.cls is server.gzip_middleware.GZipMiddleware
    ]
    self.assertLen(gzip_mw, 1)
    self.assertEqual(gzip_mw[0].kwargs.get("minimum_size"), 1000)
    self.assertEqual(gzip_mw[0].kwargs.get("compresslevel"), 5)

  def test_resolve_static_file_memoizes_path_and_clears_with_caches(self):
    first_path = static_assets.resolve_static_file("ui/index.html")
    self.assertIsNotNone(first_path)
    with mock.patch.object(
        data_loader,
        "safe_exists",
        side_effect=data_loader.safe_exists,
        autospec=True,
        spec_set=True,
    ) as mock_exists:
      second_path = static_assets.resolve_static_file("ui/index.html")
      self.assertEqual(second_path, first_path)
      mock_exists.assert_not_called()

    data_loader.clear_all_caches()
    self.assertEqual(static_assets.resolve_static_file.cache_info().currsize, 0)

  def test_firs_geojson_precompressed_response_headers_and_etag(self):
    self._publish_boundaries()
    res1 = self.client.get("/firs/2025.json")
    self.assertEqual(res1.status_code, 200)
    self.assertEqual(res1.headers.get("content-encoding"), "gzip")
    self.assertEqual(res1.headers.get("cache-control"), "public, no-cache")
    self.assertIn("Accept-Encoding", res1.headers.get("vary", ""))
    self.assertRegex(res1.headers.get("etag", ""), r'^".+"$')

  def test_firs_geojson_warm_cache_makes_zero_io_calls(self):
    self._publish_boundaries()
    etag = self.client.get("/firs/2025.json").headers.get("etag")

    with (
        mock.patch.object(
            fs,
            "open_file",
            side_effect=fs.open_file,
            autospec=True,
            spec_set=True,
        ) as mock_open,
        mock.patch.object(
            data_loader,
            "safe_exists",
            side_effect=data_loader.safe_exists,
            autospec=True,
            spec_set=True,
        ) as mock_exists,
        mock.patch.object(
            gzip,
            "compress",
            side_effect=gzip.compress,
            autospec=True,
            spec_set=True,
        ) as mock_compress,
    ):
      res2 = self.client.get("/firs/2025.json")
      self.assertEqual(res2.status_code, 200)
      self.assertEqual(res2.headers.get("etag"), etag)
      mock_open.assert_not_called()
      mock_exists.assert_not_called()
      mock_compress.assert_not_called()

  @parameterized.named_parameters(
      ("exact", lambda etag: etag),
      ("weak", lambda etag: f"W/{etag}"),
      ("multiple", lambda etag: f'"other", {etag}'),
      ("wildcard", lambda unused_etag: "*"),
  )
  def test_firs_geojson_etag_304(self, make_header):
    self._publish_boundaries()
    etag = self.client.get("/firs/2025.json").headers.get("etag")
    self.assertIsNotNone(etag)
    res_304 = self.client.get(
        "/firs/2025.json",
        headers={"If-None-Match": make_header(etag)},
    )
    self.assertEqual(res_304.status_code, 304)
    self.assertEqual(res_304.content, b"")
    self.assertEqual(res_304.headers.get("etag"), etag)

  def test_firs_geojson_identity_encoding(self):
    self._publish_boundaries()
    res_identity = self.client.get(
        "/firs/2025.json",
        headers={"Accept-Encoding": "identity"},
    )
    self.assertEqual(res_identity.status_code, 200)
    self.assertNotIn("content-encoding", res_identity.headers)
    self.assertEqual(res_identity.json().get("type"), "FeatureCollection")


class DataLoaderTest(DashboardTestBase):
  """Tests the loader helpers, path resolution, and memoization."""

  def test_preload_year_data(self):
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with mock.patch.object(
        data_loader,
        "get_available_years",
        return_value=[2025, 9999],
        autospec=True,
        spec_set=True,
    ):
      data_loader.preload_all_data()
      with (
          mock.patch.object(
              fs,
              "open_file",
              side_effect=AssertionError("unexpected disk read"),
              autospec=True,
              spec_set=True,
          ),
          mock.patch.object(
              fs,
              "exists",
              side_effect=AssertionError("unexpected disk stat"),
              autospec=True,
              spec_set=True,
          ),
      ):
        rows, _ = data_loader.get_country_data_for_year(2025)
        fir_row = data_loader.get_fir_row_by_identifier(
            2025, fir_id="EHAA_AMSTERDAM"
        )
        grid_avail = data_loader.get_grid_availability(2025)
    self.assertNotEmpty(rows)
    self.assertIsNotNone(fir_row)
    self.assertEqual(grid_avail["year"], 2025)

  def test_safe_exists_handles_exceptions(self):
    with mock.patch.object(
        fs,
        "exists",
        side_effect=fs.exists,
        autospec=True,
        spec_set=True,
    ) as mock_fs_exists:
      self.assertFalse(data_loader.safe_exists("some/relative/nonexistent.txt"))
      mock_fs_exists.assert_called_once()

    with mock.patch.object(
        fs,
        "exists",
        side_effect=RuntimeError("Filesystem access error"),
        autospec=True,
        spec_set=True,
    ):
      self.assertFalse(data_loader.safe_exists("some/file/path.txt"))

  def test_strict_safe_exists_raises_unavailable_on_check_failure(self):
    with mock.patch.object(
        fs,
        "exists",
        side_effect=RuntimeError("Filesystem access error"),
        autospec=True,
        spec_set=True,
    ):
      with self.assertRaisesRegex(
          data_loader.ArtifactUnavailableError,
          "Could not check whether 'path.txt' exists: RuntimeError",
      ):
        data_loader.safe_exists("some/file/path.txt", strict=True)

  def test_fs_error_in_json_and_grid_loaders_raises_unavailable(self):
    temp_dir = self.create_tempdir().full_path
    year_dir = _publish_run_dir(temp_dir, "2024")
    os.makedirs(year_dir, exist_ok=True)
    json_path = os.path.join(year_dir, "test.json")
    with open(json_path, "w", encoding="utf-8") as f:
      f.write("{}")
    bin_path = os.path.join(year_dir, "test_prefix.bin.gz")
    with open(bin_path, "wb") as f:
      f.write(b"\x1f\x8b")

    data_loader.clear_all_caches()
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", temp_dir))
    # Discover the year (which reads latest.json) before reads start failing.
    self.assertEqual(data_loader.get_available_years(), [2024])
    with mock.patch.object(
        fs,
        "open_file",
        side_effect=fs.Error("Simulated storage error"),
        autospec=True,
        spec_set=True,
    ):
      with self.assertRaisesRegex(
          data_loader.ArtifactUnavailableError, "Could not read test json"
      ):
        data_loader.load_json(json_path, "test json")
      with self.assertRaisesRegex(
          data_loader.ArtifactUnavailableError, "Could not read grid"
      ):
        data_loader.get_grid_binary_payload(2024, prefix="test_prefix")
    # The failure is not cached: the next read succeeds.
    self.assertEqual(
        data_loader.get_grid_binary_payload(2024, prefix="test_prefix"),
        b"\x1f\x8b",
    )
    data_loader.clear_all_caches()

  def test_unreadable_year_listing_raises_and_is_not_memoized(self):
    data_loader.clear_all_caches()
    real_listdir = fs.listdir
    with mock.patch.object(
        fs,
        "listdir",
        side_effect=fs.Error("Simulated storage error"),
        autospec=True,
        spec_set=True,
    ):
      resp = self.client.get("/years")
    self.assertEqual(resp.status_code, 503)
    with mock.patch.object(
        fs, "listdir", side_effect=real_listdir, autospec=True, spec_set=True
    ):
      self.assertNotEmpty(data_loader.get_available_years())
    data_loader.clear_all_caches()

  def test_corrupt_optional_temporal_artifact_raises_unavailable(self):
    temp_dir = self.create_tempdir().full_path
    run_dir = _publish_run_dir(temp_dir, "2024")
    with open(
        os.path.join(run_dir, constants.COUNTRY_TEMPORAL_EXPORT_FILENAME),
        "w",
        encoding="utf-8",
    ) as f:
      f.write("[{truncated")
    data_loader.clear_all_caches()
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", temp_dir))
    with self.assertRaisesRegex(
        data_loader.ArtifactUnavailableError, "Could not read temporal records"
    ):
      data_loader.get_temporal_raw_data(2024)
    data_loader.clear_all_caches()

  def test_get_country_data_caching(self):
    data_loader.clear_all_caches()
    rows1, map1 = data_loader.get_country_data_for_year(2025)
    self.assertEqual(
        data_loader._get_country_data_cached.cache_info().currsize, 1
    )
    rows2, map2 = data_loader.get_country_data_for_year("2025")
    self.assertIs(rows1, rows2)
    self.assertIs(map1, map2)

  def test_fir_data_caching(self):
    data_loader.clear_all_caches()
    rows1 = data_loader.get_fir_data_for_year(2025)
    self.assertEqual(data_loader._load_tabular_json.cache_info().currsize, 1)
    rows2 = data_loader.get_fir_data_for_year("2025")
    self.assertIs(rows1, rows2)

  def test_resolve_dataset_dir(self):
    data_loader.clear_all_caches()
    with mock.patch.object(
        artifact_io, "resolve_dataset_dir", autospec=True, spec_set=True
    ) as mock_resolve:
      ds = testdata_fixture.DATASET
      mock_resolve.return_value = f"/test/data/2025/{ds}/runs/run-1"
      resp = data_loader.resolve_dataset_dir(2025, ds)
      self.assertEqual(resp, f"/test/data/2025/{ds}/runs/run-1")
      mock_resolve.assert_called_once_with(data_loader.DATA_DIR, 2025, ds)

      # Second call returns the cached value without calling
      # resolve_dataset_dir again, including when passed as a string.
      res2 = data_loader.resolve_dataset_dir(2025, ds)
      self.assertEqual(res2, f"/test/data/2025/{ds}/runs/run-1")
      res3 = data_loader.resolve_dataset_dir("2025", ds)
      self.assertEqual(res3, f"/test/data/2025/{ds}/runs/run-1")
      mock_resolve.assert_called_once()
      self.assertEqual(
          data_loader._resolve_dataset_dir_cached.cache_info().currsize, 1
      )

  @parameterized.parameters(
      "",
      "abcd",
      "999",
      "10000",
      "2024a",
      "２０２５",
      999,
      10000,
      True,
      None,
      2024.5,
  )
  def test_resolve_dataset_dir_rejects_invalid_year(self, bad_year: Any):
    with self.assertRaisesRegex(
        data_loader.DataValidationError, r"Invalid year format"
    ):
      data_loader.resolve_dataset_dir(bad_year, testdata_fixture.DATASET)

  def test_resolve_dataset_dir_maps_malformed_pointer_to_503(self):
    root = self.publish_dataset(
        "2025",
        {
            constants.COUNTRY_EXPORT_FILENAME: [
                {"iso_a2": "CH", "name": "Switzerland"}
            ]
        },
    )
    pointer_dir = os.path.join(root, "2024", testdata_fixture.DATASET)
    os.makedirs(pointer_dir, exist_ok=True)
    with open(
        os.path.join(pointer_dir, "latest.json"), "w", encoding="utf-8"
    ) as out_file:
      json.dump(["not", "a", "dict"], out_file)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with mock.patch.object(data_loader, "DATA_DIR", root):
      with self.assertRaisesRegex(
          data_loader.ArtifactUnavailableError, r"Malformed run pointer"
      ):
        data_loader.resolve_dataset_dir(2024, testdata_fixture.DATASET)
      years_resp = self.client.get("/years")
      resp = self.client.get("/countries/2024")
    self.assertEqual(
        (years_resp.status_code, years_resp.json()),
        (200, {"years": [2025]}),
    )
    self.assertEqual(resp.status_code, 503)
    self.assertIn("Malformed run pointer", resp.json()["detail"])

  @parameterized.named_parameters(
      ("non_list_root", {"iso_a2": "CH"}),
      ("non_dict_element", ["not_a_dict"]),
  )
  def test_load_tabular_json_rejects_non_list_or_non_dict_rows(
      self, bad_payload: Any
  ):
    with mock.patch.object(
        data_loader,
        "load_json",
        return_value=bad_payload,
        autospec=True,
        spec_set=True,
    ):
      data_loader.clear_all_caches()
      resp = self.client.get("/countries/2024")
    self.assertEqual(resp.status_code, 503)
    self.assertIn("expected a JSON list of objects", resp.json()["detail"])

  def test_validate_fir_id_helper(self):
    self.assertEqual(
        data_loader.validate_fir_id("  lsas_switzerland  "),
        "LSAS_SWITZERLAND",
    )
    self.assertIsNone(data_loader.validate_fir_id(None))
    self.assertIsNone(data_loader.validate_fir_id(""))
    self.assertIsNone(data_loader.validate_fir_id("   "))

  def test_get_fir_row_by_identifier_single_fir(self):
    data_loader.clear_all_caches()
    row = data_loader.get_fir_row_by_identifier(2025, fir_id="LOVV_WIEN")
    self.assertIsNotNone(row)
    self.assertEqual(str(row.get("fir_id")).upper(), "LOVV_WIEN")
    self.assertEqual(str(row.get("fir_icao")).upper(), "LOVV")
    self.assertEqual(str(row.get("firname")), "WIEN")

  def test_get_fir_row_by_identifier_multi_sector_disambiguation(self):
    data_loader.clear_all_caches()
    row_east = data_loader.get_fir_row_by_identifier(
        2025, fir_id="KZAK_OAKLAND_OCEANIC_EAST"
    )
    row_west = data_loader.get_fir_row_by_identifier(
        2025, fir_id="KZAK_OAKLAND_OCEANIC_WEST"
    )
    self.assertIsNotNone(row_east)
    self.assertIsNotNone(row_west)
    self.assertEqual(row_east.get("firname"), "OAKLAND OCEANIC EAST")
    self.assertEqual(row_west.get("firname"), "OAKLAND OCEANIC WEST")

  def test_get_fir_row_by_identifier_nonexistent_returns_none(self):
    data_loader.clear_all_caches()
    self.assertIsNone(
        data_loader.get_fir_row_by_identifier(2025, fir_id="ZZZZ_UNKNOWN")
    )
    self.assertIsNone(data_loader.get_fir_row_by_identifier(2025))

  @parameterized.named_parameters(
      ("float_val", 12.5, 12.5),
      ("numeric_str", "45.2", 45.2),
      ("none", None, 0.0),
      ("true", True, 0.0),
      ("false", False, 0.0),
      ("non_numeric_str", "non-numeric", 0.0),
      ("nan", math.nan, 0.0),
      ("inf", math.inf, 0.0),
  )
  def test_safe_rank_metric(self, raw_value, expected):
    self.assertEqual(data_loader.safe_rank_metric(raw_value), expected)

  def test_preload_workers_bounded(self):
    with (
        mock.patch.object(
            data_loader, "_preload_year_data", autospec=True, spec_set=True
        ),
        mock.patch.object(
            data_loader,
            "get_available_years",
            return_value=(2025,),
            autospec=True,
            spec_set=True,
        ),
        mock.patch.object(
            futures,
            "ThreadPoolExecutor",
            side_effect=futures.ThreadPoolExecutor,
            autospec=True,
            spec_set=True,
        ) as mock_exec,
    ):
      data_loader.preload_all_data()
      self.assertTrue(mock_exec.called)
      _, kwargs = mock_exec.call_args
      max_workers = kwargs.get("max_workers")
      self.assertLessEqual(max_workers, 4)

  def test_available_years_are_discovered_from_the_data_root(self):
    root = self.create_tempdir().full_path
    ds = testdata_fixture.DATASET
    export = constants.COUNTRY_EXPORT_FILENAME

    def touch(*parts):
      path = os.path.join(root, *parts)
      os.makedirs(os.path.dirname(path), exist_ok=True)
      with open(path, "w", encoding="utf-8") as f:
        f.write("[]")

    touch("2019", ds, export)  # No run pointer: not published.
    artifact_io.write_run_pointer(root, year=2031, dataset=ds, run_id="run_a")
    touch("2031", ds, "runs", "run_a", export)  # Run-pointer layout.
    touch("2020", "other_dataset", export)  # Another dataset only.
    os.makedirs(os.path.join(root, "2021", ds))  # No export.
    touch("20222", ds, export)  # Not a year.
    touch("notes", ds, export)

    data_loader.clear_all_caches()
    with mock.patch.object(data_loader, "DATA_DIR", root):
      self.assertEqual(data_loader.get_available_years(), [2031])
      self.assertEqual(data_loader.require_supported_year(2031), 2031)
      with self.assertRaisesRegex(
          data_loader.DataValidationError, "years are 2031"
      ):
        data_loader.require_supported_year(2019)
    data_loader.clear_all_caches()

  def test_no_years_when_the_data_root_is_missing(self):
    missing = os.path.join(self.create_tempdir().full_path, "absent")
    data_loader.clear_all_caches()
    with mock.patch.object(data_loader, "DATA_DIR", missing):
      self.assertEmpty(data_loader.get_available_years())
      with self.assertRaisesRegex(data_loader.DataValidationError, "none"):
        data_loader.require_supported_year(2025)
      resp = self.client.get("/years")
      self.assertEqual(resp.status_code, 200)
      self.assertEqual(resp.json(), {"years": []})
    data_loader.clear_all_caches()

  def test_load_json_handles_failures(self):
    temp_dir = self.create_tempdir()
    corrupt_file = os.path.join(temp_dir.full_path, "corrupt.json")
    with open(corrupt_file, "w", encoding="utf-8") as f:
      f.write("{invalid json...")

    valid_file = os.path.join(temp_dir.full_path, "valid.json")
    with open(valid_file, "w", encoding="utf-8") as f:
      json.dump({"status": "ok"}, f)

    non_existent = os.path.join(temp_dir.full_path, "does_not_exist.json")

    self.assertEqual(
        data_loader.load_json(valid_file, "test json"), {"status": "ok"}
    )
    self.assertIsNone(data_loader.load_json(non_existent, "missing file"))
    self.assertIsNone(data_loader.load_json(None, "unpublished dataset"))
    with self.assertRaisesRegex(
        data_loader.ArtifactUnavailableError,
        "Could not read corrupt file: JSONDecodeError",
    ):
      data_loader.load_json(corrupt_file, "corrupt file")

  def _publish_fir_index_fixture(self, temp_dir: str) -> None:
    dataset_dir = _publish_run_dir(temp_dir, "2024")
    os.makedirs(dataset_dir, exist_ok=True)
    with open(
        os.path.join(dataset_dir, constants.COUNTRY_EXPORT_FILENAME),
        "w",
        encoding="utf-8",
    ) as f:
      f.write("[]")
    fir_file = os.path.join(
        dataset_dir, "fir_density_df_export_per_country.json"
    )
    fir_rows = [
        {
            "fir_id": "LFEE_REIMS",
            "fir_icao": "LFEE",
            "firname": "REIMS",
            "name": "REIMS",
        },
        {
            "fir_id": "EDWW_BREMEN",
            "fir_icao": "EDWW",
            "firname": "BREMEN",
            "name": "BREMEN",
        },
    ]
    with open(fir_file, "w", encoding="utf-8") as f:
      json.dump(fir_rows, f)

  def test_fir_index_fast_lookup_by_fir_id(self):
    temp_dir = self.create_tempdir().full_path
    self._publish_fir_index_fixture(temp_dir)
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", temp_dir))
    data_loader.clear_all_caches()

    row = data_loader.get_fir_row_by_identifier(year=2024, fir_id="LFEE_REIMS")
    self.assertIsNotNone(row)
    self.assertEqual(row.get("fir_id"), "LFEE_REIMS")
    self.assertEqual(row.get("fir_icao"), "LFEE")

    # Case-insensitive fir_id lookup
    row_lower = data_loader.get_fir_row_by_identifier(
        year=2024, fir_id="edww_bremen"
    )
    self.assertIsNotNone(row_lower)
    self.assertEqual(row_lower.get("fir_icao"), "EDWW")


class DataLoaderCacheApiTest(parameterized.TestCase):
  """Tests the module-level cache accessors."""

  def test_temporal_matrix_round_trips_through_cache(self):
    key = ("/runs/r1", 2025, testdata_fixture.DATASET, "DE", None)
    sentinel = object()
    data_loader.cache_temporal_matrix(key, sentinel)
    self.addCleanup(data_loader.clear_all_caches)

    self.assertIs(data_loader.get_cached_temporal_matrix(key), sentinel)

  def test_temporal_matrix_cache_isolates_entries_by_run_dir(self):
    self.addCleanup(data_loader.clear_all_caches)
    key_run_1 = ("/runs/r1", 2025, testdata_fixture.DATASET, "CH", None)
    key_run_2 = ("/runs/r2", 2025, testdata_fixture.DATASET, "CH", None)
    sentinel_1 = object()
    sentinel_2 = object()
    data_loader.cache_temporal_matrix(key_run_1, sentinel_1)
    data_loader.cache_temporal_matrix(key_run_2, sentinel_2)

    self.assertIs(data_loader.get_cached_temporal_matrix(key_run_1), sentinel_1)
    self.assertIs(data_loader.get_cached_temporal_matrix(key_run_2), sentinel_2)

  def test_temporal_matrix_miss_returns_none(self):
    data_loader.clear_all_caches()

    self.assertIsNone(
        data_loader.get_cached_temporal_matrix(
            ("/runs/r1", 2025, testdata_fixture.DATASET, "NOPE", None)
        )
    )

  def test_clear_all_caches_empties_every_cache(self):
    data_loader.cache_temporal_matrix(("/runs/r1", 2025, "d", None, None), 1)
    data_loader.get_country_data_for_year(2025)

    data_loader.clear_all_caches()

    self.assertIsNone(
        data_loader.get_cached_temporal_matrix(
            ("/runs/r1", 2025, "d", None, None)
        )
    )
    non_zero = [
        fn
        for fn in data_loader._CACHED_FUNCTIONS
        if fn.cache_info().currsize != 0
    ]
    self.assertEmpty(non_zero)

  @staticmethod
  def _concurrent_temporal_worker(worker_id: int) -> None:
    for i in range(320):
      key = (
          "/runs/r1",
          2025,
          testdata_fixture.DATASET,
          f"ISO_{i % 300}",
          None,
      )
      data_loader.cache_temporal_matrix(key, (worker_id, i))
      _ = data_loader.get_cached_temporal_matrix(key)

  def test_temporal_matrix_cache_thread_safe_concurrent_access(self):
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)

    with futures.ThreadPoolExecutor(max_workers=8) as executor:
      futs = [
          executor.submit(self._concurrent_temporal_worker, worker_num)
          for worker_num in range(8)
      ]
      futures.wait(futs)

    self.assertLessEqual(
        len(data_loader._TEMPORAL_MATRIX_CACHE),
        data_loader._MAX_TEMPORAL_CACHE_ENTRIES,
    )

  def _assert_frozen_mapping(self, mapping: Any, key: str, value: Any) -> None:
    self.assertIsInstance(mapping, types.MappingProxyType)
    mutable_view: Any = mapping
    with self.assertRaisesRegex(TypeError, "does not support item assignment"):
      mutable_view[key] = value

  def test_cached_country_and_fir_loaders_return_immutable_containers(self):
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)

    country_data = data_loader.get_country_data_for_year("2025")
    self.assertIsInstance(country_data.rows, tuple)
    self._assert_frozen_mapping(country_data.iso_index, "CH", {})
    self._assert_frozen_mapping(country_data.rows[0], "name", "Mutated")

    fir_rows = data_loader.get_fir_data_for_year("2025")
    self.assertIsInstance(fir_rows, tuple)
    self._assert_frozen_mapping(fir_rows[0], "firname", "Mutated")

  def test_cached_temporal_loaders_return_immutable_containers(self):
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)

    temporal_rows = data_loader.get_temporal_raw_data("2025")
    self.assertIsInstance(temporal_rows, tuple)
    self._assert_frozen_mapping(temporal_rows[0], "name", "Mutated")
    self.assertIsInstance(temporal_rows[0]["co2e_gwp50_tonnes"], tuple)
    self.assertIsInstance(temporal_rows[0]["co2e_gwp50_tonnes"][0], tuple)

    fir_temporal_rows = data_loader.get_fir_temporal_raw_data("2025")
    self.assertIsInstance(fir_temporal_rows, tuple)

  def _setup_dual_prefix_grid_fixture(self, temp_dir: str) -> None:
    year_dir = _publish_run_dir(temp_dir, "2024")
    os.makedirs(year_dir, exist_ok=True)
    with open(
        os.path.join(year_dir, "grid_meta.json"), "w", encoding="utf-8"
    ) as f:
      json.dump({"width": 16, "height": 8}, f)
    dummy_gz = gzip.compress(b"\x00\x00" * 8, compresslevel=6)
    with open(os.path.join(year_dir, "prefix_a.bin.gz"), "wb") as f:
      f.write(dummy_gz)
    with open(os.path.join(year_dir, "prefix_b.bin.gz"), "wb") as f:
      f.write(dummy_gz)

  def test_get_grid_meta_caches_shared_json_and_returns_defensive_copy(self):
    temp_dir = self.create_tempdir().full_path
    self._setup_dual_prefix_grid_fixture(temp_dir)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with (
        mock.patch.object(data_loader, "DATA_DIR", temp_dir),
        mock.patch.object(
            data_loader,
            "load_json",
            side_effect=data_loader.load_json,
            autospec=True,
            spec_set=True,
        ) as mock_load_json,
        mock.patch.object(
            data_loader,
            "safe_exists",
            side_effect=data_loader.safe_exists,
            autospec=True,
            spec_set=True,
        ) as mock_exists,
    ):
      meta_a1 = data_loader.get_grid_meta("2024", prefix="prefix_a")
      self.assertIsNotNone(meta_a1)
      mock_load_json.assert_called_once()
      exists_calls_after_first = mock_exists.call_count

      meta_a1["width"] = 9999
      meta_a2 = data_loader.get_grid_meta("2024", prefix="prefix_a")
      self.assertIsNotNone(meta_a2)
      self.assertEqual(meta_a2["width"], 16)
      self.assertEqual(
          (mock_load_json.call_count, mock_exists.call_count),
          (1, exists_calls_after_first),
      )

  def test_get_grid_meta_second_prefix_reuses_cached_json(self):
    temp_dir = self.create_tempdir().full_path
    self._setup_dual_prefix_grid_fixture(temp_dir)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with (
        mock.patch.object(data_loader, "DATA_DIR", temp_dir),
        mock.patch.object(
            data_loader,
            "load_json",
            side_effect=data_loader.load_json,
            autospec=True,
            spec_set=True,
        ) as mock_load_json,
    ):
      _ = data_loader.get_grid_meta("2024", prefix="prefix_a")
      mock_load_json.assert_called_once()
      meta_b = data_loader.get_grid_meta("2024", prefix="prefix_b")
      self.assertIsNotNone(meta_b)
      self.assertEqual(meta_b["grid_name"], "prefix_b")
      mock_load_json.assert_called_once()

  def _setup_lod_fixture(self, temp_dir: str) -> tuple[bytes, bytes]:
    year_dir = _publish_run_dir(temp_dir, "2024")
    os.makedirs(year_dir, exist_ok=True)
    with open(
        os.path.join(year_dir, "grid_meta.json"), "w", encoding="utf-8"
    ) as f:
      json.dump(
          {
              "width": 16,
              "height": 8,
              "lods": {
                  "0": {"width": 4, "height": 2, "lod": 0},
                  "1": {"width": 8, "height": 4, "lod": 1},
              },
          },
          f,
      )
    base_bytes = b"\x00\x01" * 16
    lod0_bytes = b"\x02\x03" * 4
    with open(os.path.join(year_dir, "prefix_a.bin.gz"), "wb") as f:
      f.write(gzip.compress(base_bytes, compresslevel=6))
    with open(os.path.join(year_dir, "prefix_a_lod0.bin.gz"), "wb") as f:
      f.write(gzip.compress(lod0_bytes, compresslevel=6))
    return base_bytes, lod0_bytes

  def test_grid_binary_lod0_cold_request_fs_checks(self):
    temp_dir = self.create_tempdir().full_path
    _, lod0_bytes = self._setup_lod_fixture(temp_dir)
    client = testclient.TestClient(server.app)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with (
        mock.patch.object(data_loader, "DATA_DIR", temp_dir),
        mock.patch.object(
            data_loader,
            "load_json",
            side_effect=data_loader.load_json,
            autospec=True,
            spec_set=True,
        ) as mock_load_json,
        mock.patch.object(
            data_loader,
            "safe_exists",
            side_effect=data_loader.safe_exists,
            autospec=True,
            spec_set=True,
        ) as mock_exists,
        mock.patch.object(
            fs,
            "open_file",
            side_effect=fs.open_file,
            autospec=True,
            spec_set=True,
        ),
    ):
      res_cold = client.get("/grid/2024/prefix_a/binary", params={"lod": 0})
      self.assertEqual(res_cold.status_code, 200)
      self.assertEqual(res_cold.content, lod0_bytes)
      mock_load_json.assert_called_once()
      # 1 safe_exists for the per-country export (year discovery) + 1 for
      # grid_meta.json + 1 for prefix_a_lod0.bin.gz.
      self.assertEqual(mock_exists.call_count, 3)

  def test_grid_binary_lod0_warm_request_zero_fs_checks(self):
    temp_dir = self.create_tempdir().full_path
    _, lod0_bytes = self._setup_lod_fixture(temp_dir)
    client = testclient.TestClient(server.app)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with (
        mock.patch.object(data_loader, "DATA_DIR", temp_dir),
        mock.patch.object(
            data_loader,
            "load_json",
            side_effect=data_loader.load_json,
            autospec=True,
            spec_set=True,
        ) as mock_load_json,
        mock.patch.object(
            data_loader,
            "safe_exists",
            side_effect=data_loader.safe_exists,
            autospec=True,
            spec_set=True,
        ) as mock_exists,
        mock.patch.object(
            fs,
            "open_file",
            side_effect=fs.open_file,
            autospec=True,
            spec_set=True,
        ) as mock_open,
    ):
      client.get("/grid/2024/prefix_a/binary", params={"lod": 0})
      open_cold = mock_open.call_count

      res_warm = client.get("/grid/2024/prefix_a/binary", params={"lod": 0})
      self.assertEqual(res_warm.status_code, 200)
      self.assertEqual(res_warm.content, lod0_bytes)
      mock_load_json.assert_called_once()
      self.assertEqual(mock_exists.call_count, 3)
      self.assertEqual(mock_open.call_count, open_cold)

  def test_grid_binary_base_and_unsupported_lod_share_cache(self):
    temp_dir = self.create_tempdir().full_path
    base_bytes, _ = self._setup_lod_fixture(temp_dir)
    client = testclient.TestClient(server.app)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with (
        mock.patch.object(data_loader, "DATA_DIR", temp_dir),
        mock.patch.object(
            data_loader,
            "safe_exists",
            side_effect=data_loader.safe_exists,
            autospec=True,
            spec_set=True,
        ) as mock_exists,
        mock.patch.object(
            fs,
            "open_file",
            side_effect=fs.open_file,
            autospec=True,
            spec_set=True,
        ) as mock_open,
    ):
      res_base = client.get("/grid/2024/prefix_a/binary")
      self.assertEqual(res_base.status_code, 200)
      self.assertEqual(res_base.content, base_bytes)
      exists_after_base = mock_exists.call_count
      open_after_base = mock_open.call_count

      res_lod2 = client.get("/grid/2024/prefix_a/binary", params={"lod": 2})
      self.assertEqual(res_lod2.status_code, 200)
      self.assertEqual(res_lod2.content, base_bytes)
      self.assertEqual(
          (mock_exists.call_count, mock_open.call_count),
          (exists_after_base, open_after_base),
      )

  def test_grid_binary_missing_lod_falls_back_to_base_and_caches(self):
    temp_dir = self.create_tempdir().full_path
    base_bytes, _ = self._setup_lod_fixture(temp_dir)
    client = testclient.TestClient(server.app)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with (
        mock.patch.object(data_loader, "DATA_DIR", temp_dir),
        mock.patch.object(
            data_loader,
            "safe_exists",
            side_effect=data_loader.safe_exists,
            autospec=True,
            spec_set=True,
        ) as mock_exists,
        mock.patch.object(
            fs,
            "open_file",
            side_effect=fs.open_file,
            autospec=True,
            spec_set=True,
        ) as mock_open,
    ):
      res_lod1 = client.get("/grid/2024/prefix_a/binary", params={"lod": 1})
      self.assertEqual(res_lod1.status_code, 200)
      self.assertEqual(res_lod1.content, base_bytes)
      exists_after_lod1 = mock_exists.call_count
      open_after_lod1 = mock_open.call_count

      res_lod1_warm = client.get(
          "/grid/2024/prefix_a/binary", params={"lod": 1}
      )
      self.assertEqual(res_lod1_warm.status_code, 200)
      self.assertEqual(res_lod1_warm.content, base_bytes)
      self.assertEqual(
          (mock_exists.call_count, mock_open.call_count),
          (exists_after_lod1, open_after_lod1),
      )

  def test_endpoint_response_caches_reuse_identical_instances(self):
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)

    countries1 = server.list_countries(2025)
    countries2 = server.list_countries(2025)
    self.assertIs(countries1, countries2)

    firs1 = server.list_firs(2025)
    firs2 = server.list_firs(2025)
    self.assertIs(firs1, firs2)

    country_rank1 = server.get_country_rankings(
        2025, "co2e_gwp_tonnes", "gwp50"
    )
    country_rank2 = server.get_country_rankings(
        2025, "  CO2E_GWP_TONNES  ", "GWP50"
    )
    self.assertIs(country_rank1, country_rank2)

    fir_rank1 = server.get_country_rankings(
        2025, "fir_individual_co2e_gwp_tonnes", "gwp50"
    )
    fir_rank2 = server.get_country_rankings(
        2025, "fir_individual_co2e_gwp_tonnes", "GWP50"
    )
    self.assertIs(fir_rank1, fir_rank2)

  def test_clear_all_caches_invalidates_endpoint_response_caches(self):
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)

    countries1 = server.list_countries(2025)
    firs1 = server.list_firs(2025)
    country_rank1 = server.get_country_rankings(
        2025, "co2e_gwp_tonnes", "gwp50"
    )

    data_loader.clear_all_caches()
    self.assertIsNot(server.list_countries(2025), countries1)
    self.assertIsNot(server.list_firs(2025), firs1)
    self.assertIsNot(
        server.get_country_rankings(2025, "co2e_gwp_tonnes", "gwp50"),
        country_rank1,
    )

  def test_grid_binary_read_failure_returns_503(self):
    temp_dir = self.create_tempdir().full_path
    run_dir = _publish_run_dir(temp_dir, "2025")
    os.makedirs(run_dir, exist_ok=True)
    grid_path = os.path.join(run_dir, "total_global_impact_grid.bin.gz")
    with open(grid_path, "wb") as f:
      f.write(gzip.compress(b"\x01\x02\x03\x04" * 4, compresslevel=6))

    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", temp_dir))
    # Discover the year (which reads latest.json) before reads start failing.
    self.assertEqual(data_loader.get_available_years(), [2025])
    client = testclient.TestClient(server.app)
    with mock.patch.object(
        fs,
        "open_file",
        side_effect=fs.Error("Simulated read failure"),
        autospec=True,
        spec_set=True,
    ):
      resp = client.get("/grid/2025/total_global_impact_grid/binary")
    self.assertEqual(resp.status_code, 503)


class ArtifactPathResolutionTest(parameterized.TestCase):
  """Tests that artifacts are read only from the published run directory."""

  def test_artifact_path_is_inside_the_resolved_run_dir(self):
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    ds = testdata_fixture.DATASET
    run_dir = f"/fake/data/2025/{ds}/runs/20260918_120000"
    with (
        mock.patch.object(
            data_loader,
            "resolve_dataset_dir",
            return_value=run_dir,
            autospec=True,
            spec_set=True,
        ),
        mock.patch.object(
            data_loader,
            "get_available_years",
            return_value=[2025],
            autospec=True,
            spec_set=True,
        ),
        mock.patch.object(
            data_loader,
            "safe_exists",
            return_value=False,
            autospec=True,
            spec_set=True,
        ) as mock_exists,
    ):
      self.assertIsNone(
          data_loader.get_grid_binary_payload(2025, prefix="prefix", dataset=ds)
      )
      mock_exists.assert_any_call(
          os.path.join(run_dir, "prefix.bin.gz"), strict=True
      )

  def test_unpublished_dataset_has_no_artifacts(self):
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    ds = testdata_fixture.DATASET
    with mock.patch.object(
        data_loader,
        "resolve_dataset_dir",
        return_value=None,
        autospec=True,
        spec_set=True,
    ):
      self.assertEmpty(data_loader.get_available_years(ds))
      with self.assertRaisesRegex(
          data_loader.DataValidationError, "not available"
      ):
        data_loader.get_grid_binary_payload("2025", prefix="prefix", dataset=ds)
      with self.assertRaisesRegex(
          data_loader.DataValidationError, "not available"
      ):
        data_loader.get_grid_availability("2025", ds)

  def test_flat_layout_without_a_pointer_is_not_served(self):
    root = self.create_tempdir().full_path
    flat_dir = os.path.join(root, "2025", testdata_fixture.DATASET)
    os.makedirs(flat_dir, exist_ok=True)
    with open(
        os.path.join(flat_dir, constants.COUNTRY_EXPORT_FILENAME),
        "w",
        encoding="utf-8",
    ) as f:
      f.write("[]")
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with mock.patch.object(data_loader, "DATA_DIR", root):
      self.assertIsNone(
          data_loader.resolve_dataset_dir("2025", testdata_fixture.DATASET)
      )
      self.assertEmpty(
          data_loader.get_available_years(testdata_fixture.DATASET)
      )

  def _write_single_run_dir_fixture(
      self, run_dir: str, co2e: float, fir_name: str, width: int, payload: bytes
  ) -> None:
    os.makedirs(run_dir, exist_ok=True)
    with open(
        os.path.join(run_dir, constants.COUNTRY_EXPORT_FILENAME),
        "w",
        encoding="utf-8",
    ) as f:
      json.dump([{"iso_a2": "CH", "co2e_gwp50_tonnes": co2e}], f)
    with open(
        os.path.join(run_dir, "fir_density_df_export_per_country.json"),
        "w",
        encoding="utf-8",
    ) as f:
      json.dump([{"fir_id": "LOVV_WIEN", "firname": fir_name}], f)
    with open(
        os.path.join(run_dir, constants.COUNTRY_TEMPORAL_EXPORT_FILENAME),
        "w",
        encoding="utf-8",
    ) as out_file:
      json.dump(
          [
              _temporal_bundle(
                  {"iso_a2": "CH", "name": "Switzerland"},
                  {(6, 3): {"co2e_gwp50_tonnes": co2e}},
              )
          ],
          out_file,
      )
    with open(
        os.path.join(run_dir, "grid_meta.json"), "w", encoding="utf-8"
    ) as f:
      json.dump({"width": width, "height": 5}, f)
    with open(
        os.path.join(run_dir, "total_global_impact_grid.bin.gz"), "wb"
    ) as f:
      f.write(gzip.compress(payload))

  def _setup_dual_run_fixtures(self, root: str, ds: str) -> None:
    run_a = os.path.join(root, "2025", ds, "runs", "run-a")
    run_b = os.path.join(root, "2025", ds, "runs", "run-b")
    self._write_single_run_dir_fixture(run_a, 100.0, "WIEN_A", 10, b"\x01" * 8)
    self._write_single_run_dir_fixture(run_b, 200.0, "WIEN_B", 20, b"\x02" * 8)
    with open(
        os.path.join(run_b, "monthly_global_impact_grid_01.bin.gz"), "wb"
    ) as f:
      f.write(gzip.compress(b"\x02" * 8))

  def test_downstream_tabular_caches_key_on_initial_run_dir(self):
    root = self.create_tempdir().full_path
    ds = testdata_fixture.DATASET
    self._setup_dual_run_fixtures(root, ds)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with mock.patch.object(data_loader, "DATA_DIR", root):
      _publish_run_dir(root, "2025", run_id="run-a")
      _, iso_map_a = data_loader.get_country_data_for_year("2025", ds)
      self.assertEqual(iso_map_a["CH"]["co2e_gwp50_tonnes"], 100.0)
      fir_row_a = data_loader.get_fir_row_by_identifier(
          "2025", fir_id="LOVV_WIEN", dataset=ds
      )
      self.assertIsNotNone(fir_row_a)
      self.assertEqual(fir_row_a["firname"], "WIEN_A")

  def test_downstream_grid_caches_key_on_initial_run_dir(self):
    root = self.create_tempdir().full_path
    ds = testdata_fixture.DATASET
    self._setup_dual_run_fixtures(root, ds)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with mock.patch.object(data_loader, "DATA_DIR", root):
      _publish_run_dir(root, "2025", run_id="run-a")
      meta_a = data_loader.get_grid_meta(
          "2025", prefix="total_global_impact_grid", dataset=ds
      )
      self.assertIsNotNone(meta_a)
      self.assertEqual(meta_a["width"], 10)
      payload_a = data_loader.get_grid_binary_payload(
          "2025", prefix="total_global_impact_grid", dataset=ds
      )
      self.assertIsNotNone(payload_a)
      self.assertEqual(gzip.decompress(payload_a), b"\x01" * 8)
      avail_a = data_loader.get_grid_availability("2025", ds)
      self.assertEqual(
          (avail_a["has_total"], avail_a["has_monthly"]), (True, False)
      )

  def test_downstream_tabular_caches_switch_after_resolve_cache_clear(self):
    root = self.create_tempdir().full_path
    ds = testdata_fixture.DATASET
    self._setup_dual_run_fixtures(root, ds)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with mock.patch.object(data_loader, "DATA_DIR", root):
      _publish_run_dir(root, "2025", run_id="run-a")
      _ = data_loader.get_country_data_for_year("2025", ds)
      _ = data_loader.get_fir_row_by_identifier(
          "2025", fir_id="LOVV_WIEN", dataset=ds
      )

      # Re-point latest.json to run-b and clear only the dataset-dir cache:
      # all downstream caches must isolate run-b from run-a via run_dir keying.
      _publish_run_dir(root, "2025", run_id="run-b")
      data_loader._resolve_dataset_dir_cached.cache_clear()

      _, iso_map_b = data_loader.get_country_data_for_year("2025", ds)
      self.assertEqual(iso_map_b["CH"]["co2e_gwp50_tonnes"], 200.0)
      fir_row_b = data_loader.get_fir_row_by_identifier(
          "2025", fir_id="LOVV_WIEN", dataset=ds
      )
      self.assertIsNotNone(fir_row_b)
      self.assertEqual(fir_row_b["firname"], "WIEN_B")

  def test_endpoint_caches_switch_after_resolve_cache_clear(self):
    root = self.create_tempdir().full_path
    ds = testdata_fixture.DATASET
    self._setup_dual_run_fixtures(root, ds)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with mock.patch.object(data_loader, "DATA_DIR", root):
      _publish_run_dir(root, "2025", run_id="run-a")
      temporal_a = server.get_temporal_metrics(2025, iso="CH", fir_id=None)
      countries_a = server.list_countries(2025)
      self.assertEqual(temporal_a.co2e_gwp50_tonnes[6][2], 100.0)
      self.assertEqual(countries_a[0].co2e_gwp50_tonnes, 100.0)

      _publish_run_dir(root, "2025", run_id="run-b")
      data_loader._resolve_dataset_dir_cached.cache_clear()

      temporal_b = server.get_temporal_metrics(2025, iso="CH", fir_id=None)
      countries_b = server.list_countries(2025)
      self.assertEqual(temporal_b.co2e_gwp50_tonnes[6][2], 200.0)
      self.assertEqual(countries_b[0].co2e_gwp50_tonnes, 200.0)

  def test_downstream_grid_caches_switch_after_resolve_cache_clear(self):
    root = self.create_tempdir().full_path
    ds = testdata_fixture.DATASET
    self._setup_dual_run_fixtures(root, ds)
    data_loader.clear_all_caches()
    self.addCleanup(data_loader.clear_all_caches)
    with mock.patch.object(data_loader, "DATA_DIR", root):
      _publish_run_dir(root, "2025", run_id="run-a")
      _ = data_loader.get_grid_meta(
          "2025", prefix="total_global_impact_grid", dataset=ds
      )
      _ = data_loader.get_grid_binary_payload(
          "2025", prefix="total_global_impact_grid", dataset=ds
      )

      _publish_run_dir(root, "2025", run_id="run-b")
      data_loader._resolve_dataset_dir_cached.cache_clear()

      meta_b = data_loader.get_grid_meta(
          "2025", prefix="total_global_impact_grid", dataset=ds
      )
      self.assertIsNotNone(meta_b)
      self.assertEqual(meta_b["width"], 20)
      payload_b = data_loader.get_grid_binary_payload(
          "2025", prefix="total_global_impact_grid", dataset=ds
      )
      self.assertIsNotNone(payload_b)
      self.assertEqual(gzip.decompress(payload_b), b"\x02" * 8)
      avail_b = data_loader.get_grid_availability("2025", ds)
      self.assertTrue(avail_b["has_monthly"])


_SHARED_GRID_META_FIXTURE = types.MappingProxyType({
    "grid_name": "global_grid",
    "width": 3600,
    "height": 1800,
    "min_lon": -180.0,
    "max_lon": 180.0,
    "min_lat": -90.0,
    "max_lat": 90.0,
    "lon_step": 0.1,
    "lat_step": 0.1,
    "bbox": {
        "min_lon": -180.0,
        "max_lon": 180.0,
        "min_lat": -90.0,
        "max_lat": 90.0,
    },
    "dtype": "float16",
    "encoding": "signed_log10_ref_scaled",
    "lod": 2,
    "origin": "lower_left",
    "lods": {
        "0": {
            "width": 900,
            "height": 450,
            "lon_step": 0.4,
            "lat_step": 0.4,
            "lod": 0,
        },
        "1": {
            "width": 1800,
            "height": 900,
            "lon_step": 0.2,
            "lat_step": 0.2,
            "lod": 1,
        },
        "2": {
            "width": 3600,
            "height": 1800,
            "lon_step": 0.1,
            "lat_step": 0.1,
            "lod": 2,
        },
    },
    "value_max_ref_scaled": {"density": 2.5e6, "impact": 2.25e11},
})


def _load_fir_row_for_test(yr: str) -> dict[str, Any] | None:
  return data_loader.get_fir_row_by_identifier(yr, fir_id="LFBB_BORDEAUX")


class DomainExceptionMappingTest(parameterized.TestCase):
  """Pins the domain-exception to HTTP status contract."""

  def setUp(self):
    super().setUp()
    self.client = testclient.TestClient(server.app)

  def test_data_validation_error_is_not_an_http_exception_or_value_error(self):
    err = data_loader.DataValidationError("x")
    self.assertNotIsInstance(err, fastapi.HTTPException)
    self.assertNotIsInstance(err, ValueError)
    self.assertIsInstance(err, Exception)

  def test_artifact_unavailable_error_is_not_a_runtime_error(self):
    # Handlers for missing artifacts must not swallow Python's own
    # RuntimeErrors (e.g. "dictionary changed size during iteration").
    err = data_loader.ArtifactUnavailableError("x")
    self.assertNotIsInstance(err, RuntimeError)

  @parameterized.parameters(
      metrics.UnknownMetricError,
      metrics.UnsupportedGwpError,
      metrics.MissingColumnError,
  )
  def test_metric_errors_share_a_common_base(self, error_type):
    self.assertTrue(issubclass(error_type, metrics.MetricError))

  @parameterized.parameters(
      "bad$dataset", "org_cocip_²", "ｄａｔａｓｅｔ", "ds_⅓"
  )
  def test_clean_dataset_raises_domain_error(self, bad_ds):
    with self.assertRaisesRegex(
        data_loader.DataValidationError, r"Invalid dataset format"
    ):
      data_loader.clean_dataset(bad_ds)
    with self.assertRaisesRegex(
        data_loader.DatasetConfigError, r"Invalid dataset name"
    ):
      data_loader.configure_dataset(bad_ds)

  @parameterized.parameters("prefix_²", "grid_⅓", "ｇｒｉｄ")
  def test_require_path_safe_token_raises_domain_error(self, bad_prefix):
    with self.assertRaisesRegex(
        data_loader.DataValidationError, r"Invalid grid prefix format"
    ):
      data_loader.require_path_safe_token(bad_prefix, "grid prefix")

  def test_resolve_iso_code_canonicalizes(self):
    self.assertEqual(data_loader.resolve_iso_code(" ch "), "CH")
    # No embedded country table: numeric codes pass through untranslated.
    self.assertEqual(data_loader.resolve_iso_code("756"), "756")
    self.assertEqual(data_loader.resolve_iso_code("-99"), "-99")

  @parameterized.parameters(
      "",
      "   ",
      "bad$iso",
      "United States",
      "A" * 65,
      "ＣＨ",
      "DE²",
      "⅓",
  )
  def test_resolve_iso_code_rejects_invalid(self, bad_iso):
    with self.assertRaisesRegex(
        data_loader.DataValidationError, r"Invalid ISO identifier format"
    ):
      data_loader.resolve_iso_code(bad_iso)

  @parameterized.product(
      unsupported_year=("1999", "2021", "2026"),
      loader_call=(
          data_loader.get_country_data_for_year,
          data_loader.get_fir_data_for_year,
          _load_fir_row_for_test,
          data_loader.get_temporal_raw_data,
          data_loader.get_fir_temporal_raw_data,
      ),
  )
  def test_unsupported_year_raises_domain_error(
      self, unsupported_year, loader_call
  ):
    data_loader.clear_all_caches()
    with self.assertRaisesRegex(
        data_loader.DataValidationError, "Available years are"
    ):
      loader_call(unsupported_year)
    self.assertEqual(data_loader._load_tabular_json.cache_info().currsize, 0)

  @parameterized.parameters(
      "", "2025/../../etc", "2025$bad", None, 2024.5, True, 999, 10000
  )
  def test_direct_loaders_reject_invalid_year(self, bad_year: Any):
    with self.assertRaisesRegex(
        data_loader.DataValidationError, r"is not available for dataset"
    ):
      data_loader.get_temporal_raw_data(bad_year)
    with self.assertRaisesRegex(
        data_loader.DataValidationError, r"is not available for dataset"
    ):
      data_loader.get_fir_temporal_raw_data(bad_year)
    with self.assertRaisesRegex(
        data_loader.DataValidationError, r"Invalid year format"
    ):
      data_loader.get_grid_availability(bad_year)

  def test_direct_loaders_validate_fir_identifiers(self):
    with self.assertRaisesRegex(
        data_loader.DataValidationError, r"Invalid FIR ID length"
    ):
      data_loader.get_fir_row_by_identifier("2025", fir_id="A" * 129)
    self.assertEqual(
        data_loader.validate_fir_id("  lfbb_bordeaux  "),
        "LFBB_BORDEAUX",
    )
    self.assertIsNone(data_loader.validate_fir_id("   "))

  def test_normalize_gwp_raises_domain_error(self):
    with self.assertRaisesRegex(
        metrics.UnsupportedGwpError, r"Unsupported gwp 'gwp42'"
    ):
      metrics.normalize_gwp("gwp42")

  def test_lookup_metric_raises_domain_error(self):
    with self.assertRaisesRegex(
        metrics.UnknownMetricError, r"Unknown metric 'no_such_metric'"
    ):
      metrics.lookup_metric(metrics.COUNTRY_METRIC_INDEX, "no_such_metric")

  def test_require_column_raises_domain_error(self):
    with self.assertRaisesRegex(
        metrics.MissingColumnError, r"is backed by column 'co2e_gwp20_tonnes'"
    ):
      metrics.require_column(
          [{"other": 1}],
          "co2e_gwp20_tonnes",
          metric="co2e_gwp_tonnes",
          gwp="gwp20",
          year=2025,
          dataset=testdata_fixture.DATASET,
      )

  def test_unsupported_year_surfaces_as_http_400(self):
    response = self.client.get("/countries/1999")

    self.assertEqual(response.status_code, 400)
    self.assertIn("Available years are", response.json()["detail"])

  def test_unknown_metric_surfaces_as_http_404(self):
    response = self.client.get("/rankings/2025/no_such_metric")

    self.assertEqual(response.status_code, 404)
    self.assertIn("Unknown metric", response.json()["detail"])

  @parameterized.parameters(
      "prop_sum_ef_mj",
      "energy_forcing",
      "total_flight_impact",
      "fir_prop_sum_ef_mj",
  )
  def test_removed_ef_mj_metric_and_aliases_return_404(self, metric):
    # prop_sum_ef_mj (and its aliases) was removed in favour of CO2e metrics.
    response = self.client.get(f"/rankings/2025/{metric}")
    self.assertEqual(response.status_code, 404, metric)
    self.assertIn("Unknown metric", response.json()["detail"])

  @parameterized.parameters(*metrics.SUPPORTED_GWPS)
  def test_individual_fir_ranking_uses_co2e_rank_for_selected_gwp(self, gwp):
    spec = metrics.lookup_metric(
        metrics.FIR_ROW_METRIC_INDEX, "fir_individual_co2e_gwp_tonnes"
    )
    self.assertEqual(
        spec.resolve(spec.rank_field, gwp), f"co2e_{gwp}_tonnes_rank"
    )

  def test_unsupported_gwp_surfaces_as_http_422(self):
    response = self.client.get("/rankings/2025/co2e_gwp_tonnes?gwp=gwp42")

    self.assertEqual(response.status_code, 422)
    self.assertIn("gwp42", response.json()["detail"])

  def test_temporal_country_preaggregated_24x12_matrix(self):
    res_ch = self.client.get("/temporal/2025", params={"iso": "CH"})
    data_ch = res_ch.json()
    self.assertEqual((res_ch.status_code, data_ch["iso_a2"]), (200, "CH"))
    self.assertAlmostEqual(data_ch["co2e_gwp20_tonnes"][12][5], 2500.0)
    self.assertAlmostEqual(data_ch["co2e_gwp50_tonnes"][12][5], 1250.5)
    self.assertAlmostEqual(data_ch["co2e_gwp100_tonnes"][12][5], 600.0)
    self.assertAlmostEqual(data_ch["segment_count"][12][5], 500.0)
    self.assertNotIn("total_segment_count", data_ch)

  def test_temporal_country_preaggregated_matrix_omits_ef_mj(self):
    res_ch = self.client.get("/temporal/2025", params={"iso": "CH"})
    self.assertEqual(res_ch.status_code, 200)
    self.assertNotIn("prop_sum_ef_mj", res_ch.json())

  def _setup_shared_grid_meta_fixture(self, tmpdir, year="2024"):
    year_dir = _publish_run_dir(tmpdir, year)
    os.makedirs(year_dir, exist_ok=True)
    with open(
        os.path.join(year_dir, "grid_meta.json"), "w", encoding="utf-8"
    ) as f:
      json.dump(dict(_SHARED_GRID_META_FIXTURE), f)
    with open(
        os.path.join(year_dir, "hourly_global_impact_grid_00.bin.gz"), "wb"
    ) as f:
      f.write(gzip.compress(b"\x00\x00" * 4, compresslevel=6))
    with open(
        os.path.join(year_dir, "hourly_global_impact_grid_00_lod0.bin.gz"), "wb"
    ) as f:
      f.write(gzip.compress(b"\x00\x00" * 4, compresslevel=6))
    return _SHARED_GRID_META_FIXTURE

  def test_single_shared_grid_meta_lod0_response(self):
    tmpdir = self.create_tempdir().full_path
    shared_meta = self._setup_shared_grid_meta_fixture(tmpdir)
    with mock.patch.object(data_loader, "DATA_DIR", tmpdir):
      data_loader.clear_all_caches()
      self.addCleanup(data_loader.clear_all_caches)
      meta_lod0 = self.client.get(
          "/grid/2024/hourly_global_impact_grid_00/meta", params={"lod": 0}
      ).json()
      self.assertEqual(meta_lod0["width"], 900)
      self.assertEqual(meta_lod0["height"], 450)
      self.assertEqual(meta_lod0["lod"], 0)
      self.assertEqual(
          meta_lod0["grid_name"], "hourly_global_impact_grid_00_lod0"
      )
      self.assertEqual(
          meta_lod0["value_max_ref_scaled"],
          shared_meta["value_max_ref_scaled"],
      )

  def test_single_shared_grid_binary_and_x_grid_meta_header(self):
    tmpdir = self.create_tempdir().full_path
    shared_meta = self._setup_shared_grid_meta_fixture(tmpdir)
    with mock.patch.object(data_loader, "DATA_DIR", tmpdir):
      data_loader.clear_all_caches()
      self.addCleanup(data_loader.clear_all_caches)
      avail = self.client.get("/grid/2024/availability").json()
      self.assertTrue(avail["has_hourly"])
      bin_res = self.client.get(
          "/grid/2024/hourly_global_impact_grid_00/binary", params={"lod": 0}
      )
      self.assertEqual(bin_res.status_code, 200)
      header_meta = json.loads(bin_res.headers["X-Grid-Meta"])
      self.assertEqual(header_meta["width"], 900)
      self.assertEqual(header_meta["height"], 450)
      self.assertEqual(
          header_meta["value_max_ref_scaled"],
          shared_meta["value_max_ref_scaled"],
      )

  def test_list_firs_response(self):
    res_firs = self.client.get("/firs/2025")
    self.assertEqual(res_firs.status_code, 200)
    (lsas,) = [
        item_row
        for item_row in res_firs.json()
        if item_row["fir_id"] == "LSAS_SWITZERLAND"
    ]
    self.assertEqual(lsas["fir_icao"], "LSAS")
    self.assertEqual(lsas["firname"], "SWITZERLAND")
    self.assertEqual(
        lsas["countries"], [{"iso_a2": "CH", "name": "Switzerland"}]
    )

  def test_fir_rankings_response(self):
    res_rank = self.client.get(
        "/rankings/2025/fir_individual_co2e_gwp_tonnes",
        params={"gwp": "GWP50"},
    )
    self.assertEqual(res_rank.status_code, 200)
    rank_data = res_rank.json()
    self.assertEqual(
        (rank_data["isFirRanking"], rank_data["unit"]),
        (True, metrics.UNIT_TONNES_CO2E),
    )
    self.assertNotEmpty(rank_data["items"])
    self.assertEqual(rank_data["items"][0]["rank"], 1)
    self.assertIsNotNone(rank_data["items"][0]["fir_id"])

  def test_fir_rankings_item_names_omit_fir_prefix(self):
    res_rank = self.client.get(
        "/rankings/2025/fir_individual_co2e_gwp_tonnes",
        params={"gwp": "GWP50"},
    )
    self.assertEqual(res_rank.status_code, 200)
    fir_prefixed_items = [
        item["name"]
        for item in res_rank.json()["items"]
        if str(item["name"]).upper().startswith("FIR ")
    ]
    self.assertEmpty(fir_prefixed_items)


class DatasetConfigurationTest(DashboardTestBase):
  """The served dataset comes only from configuration, never from code."""

  def setUp(self):
    super().setUp()
    # Restore the module-level dataset and the environment after each test.
    self.enter_context(
        mock.patch.object(
            data_loader, "_active_dataset", data_loader._active_dataset
        )
    )
    self.enter_context(mock.patch.dict(os.environ, {}, clear=False))
    os.environ.pop(data_loader.DATASET_ENV_VAR, None)
    self.addCleanup(data_loader.clear_all_caches)

  def unconfigure(self):
    data_loader._active_dataset = None

  def publish_two_datasets(self):
    """Publishes `alpha` for 2024 and `beta` for 2025 under a new root."""
    root = self.create_tempdir().full_path
    for year, dataset, iso in (("2024", "alpha", "AA"), ("2025", "beta", "BB")):
      artifact_io.write_run_pointer(
          root, year=int(year), dataset=dataset, run_id="test-run"
      )
      dataset_dir = artifact_io.run_output_dir(
          root, year=int(year), dataset=dataset, run_id="test-run"
      )
      os.makedirs(dataset_dir, exist_ok=True)
      with open(
          os.path.join(dataset_dir, constants.COUNTRY_EXPORT_FILENAME),
          "w",
          encoding="utf-8",
      ) as f:
        json.dump([{"iso_a2": iso, "name": f"Country {iso}"}], f)
    self.enter_context(mock.patch.object(data_loader, "DATA_DIR", root))
    data_loader.clear_all_caches()
    return root

  # --- active_dataset / configure_dataset ---

  def test_active_dataset_raises_when_unconfigured(self):
    self.unconfigure()
    with self.assertRaisesRegex(
        data_loader.DatasetConfigError, data_loader.DATASET_ENV_VAR
    ):
      data_loader.active_dataset()

  def test_config_error_is_not_swallowed_as_bad_input_or_missing_data(self):
    self.assertFalse(issubclass(data_loader.DatasetConfigError, RuntimeError))
    self.assertFalse(
        issubclass(
            data_loader.DatasetConfigError, data_loader.DataValidationError
        )
    )

  def test_configure_dataset_strips_and_returns_the_name(self):
    self.assertEqual(data_loader.configure_dataset("  my-data_1 "), "my-data_1")
    self.assertEqual(data_loader.active_dataset(), "my-data_1")

  def test_configure_dataset_accepts_the_maximum_length(self):
    name = "x" * 64
    self.assertEqual(data_loader.configure_dataset(name), name)

  @parameterized.parameters(
      "", "   ", "../escape", "a/b", "a.b", "a b", "x" * 65
  )
  def test_configure_dataset_rejects_invalid_names_and_keeps_the_previous(
      self, bad
  ):
    data_loader.configure_dataset("previous")
    with self.assertRaisesRegex(
        data_loader.DatasetConfigError, "Invalid dataset name"
    ):
      data_loader.configure_dataset(bad)
    self.assertEqual(data_loader.active_dataset(), "previous")

  # --- configure_dataset_from_env ---

  def test_configure_from_env_fails_clearly_when_unset(self):
    self.unconfigure()
    with self.assertRaisesRegex(
        data_loader.DatasetConfigError, data_loader.DATASET_ENV_VAR
    ) as ctx:
      data_loader.configure_dataset_from_env({})
    message = str(ctx.exception)
    self.assertIn("is not set", message)
    self.assertIn(data_loader.DATA_DIR, message)
    self.assertIn("--dataset", message)
    self.assertIsNone(data_loader._active_dataset)

  def test_configure_from_env_treats_blank_as_unset(self):
    self.unconfigure()
    with self.assertRaisesRegex(data_loader.DatasetConfigError, "is not set"):
      data_loader.configure_dataset_from_env(
          {data_loader.DATASET_ENV_VAR: "   "}
      )
    self.assertIsNone(data_loader._active_dataset)

  def test_configure_from_env_rejects_invalid_values_and_names_the_var(self):
    self.unconfigure()
    with self.assertRaisesRegex(
        data_loader.DatasetConfigError, data_loader.DATASET_ENV_VAR
    ) as ctx:
      data_loader.configure_dataset_from_env(
          {data_loader.DATASET_ENV_VAR: "../escape"}
      )
    self.assertIn(data_loader.DATASET_ENV_VAR, str(ctx.exception))
    self.assertIn("Invalid dataset name", str(ctx.exception))
    self.assertIsNone(data_loader._active_dataset)

  def test_configure_from_env_strips_and_sets_the_value(self):
    self.unconfigure()
    self.assertEqual(
        data_loader.configure_dataset_from_env(
            {data_loader.DATASET_ENV_VAR: " my_ds "}
        ),
        "my_ds",
    )
    self.assertEqual(data_loader.active_dataset(), "my_ds")

  def test_configure_from_env_reads_os_environ_by_default(self):
    self.unconfigure()
    os.environ[data_loader.DATASET_ENV_VAR] = "from_os_environ"
    self.assertEqual(
        data_loader.configure_dataset_from_env(), "from_os_environ"
    )

  # --- ensure_dataset_configured ---

  def test_ensure_prefers_explicit_configuration_over_the_env(self):
    data_loader.configure_dataset("explicit")
    os.environ[data_loader.DATASET_ENV_VAR] = "from_env"
    self.assertEqual(data_loader.ensure_dataset_configured(), "explicit")
    self.assertEqual(data_loader.active_dataset(), "explicit")

  def test_ensure_reads_the_env_when_unconfigured(self):
    self.unconfigure()
    os.environ[data_loader.DATASET_ENV_VAR] = "from_env"
    self.assertEqual(data_loader.ensure_dataset_configured(), "from_env")
    self.assertEqual(data_loader.active_dataset(), "from_env")

  def test_ensure_raises_when_unconfigured_and_env_unset(self):
    self.unconfigure()
    with self.assertRaisesRegex(
        data_loader.DatasetConfigError, data_loader.DATASET_ENV_VAR
    ):
      data_loader.ensure_dataset_configured()

  # --- loaders resolve the dataset at call time ---

  @parameterized.named_parameters(
      ("get_available_years", data_loader.get_available_years),
      (
          "require_supported_year",
          lambda: data_loader.require_supported_year("2024"),
      ),
      (
          "get_country_data_for_year",
          lambda: data_loader.get_country_data_for_year("2024"),
      ),
      (
          "get_fir_data_for_year",
          lambda: data_loader.get_fir_data_for_year("2024"),
      ),
      (
          "get_temporal_raw_data",
          lambda: data_loader.get_temporal_raw_data("2024"),
      ),
      (
          "get_fir_temporal_raw_data",
          lambda: data_loader.get_fir_temporal_raw_data("2024"),
      ),
      (
          "get_fir_row_by_identifier",
          lambda: data_loader.get_fir_row_by_identifier("2024", fir_id="X"),
      ),
      (
          "get_grid_meta",
          lambda: data_loader.get_grid_meta("2024", prefix="grid"),
      ),
      (
          "get_grid_binary_payload",
          lambda: data_loader.get_grid_binary_payload("2024", prefix="grid"),
      ),
      (
          "get_grid_availability",
          lambda: data_loader.get_grid_availability("2024"),
      ),
      ("preload_all_data", data_loader.preload_all_data),
  )
  def test_every_loader_raises_config_error_when_unconfigured(self, call):
    self.unconfigure()
    with self.assertRaisesRegex(
        data_loader.DatasetConfigError, r"No dataset is configured"
    ):
      call()

  def test_loaders_follow_the_dataset_configured_after_import(self):
    # Guards against `dataset=<module constant>` default arguments, which are
    # frozen at import time and would ignore configuration done at startup.
    self.publish_two_datasets()

    data_loader.configure_dataset("alpha")
    self.assertEqual(data_loader.get_available_years(), [2024])
    _, index = data_loader.get_country_data_for_year(2024)
    self.assertIn("AA", index)

    data_loader.configure_dataset("beta")
    self.assertEqual(data_loader.get_available_years(), [2025])
    _, index = data_loader.get_country_data_for_year(2025)
    self.assertIn("BB", index)
    with self.assertRaisesRegex(
        data_loader.DataValidationError, "for dataset 'beta'"
    ):
      data_loader.get_country_data_for_year(2024)

  def test_an_explicit_dataset_argument_overrides_the_configured_one(self):
    self.publish_two_datasets()
    data_loader.configure_dataset("beta")
    self.assertEqual(data_loader.get_available_years("alpha"), [2024])
    _, index = data_loader.get_country_data_for_year(2024, "alpha")
    self.assertIn("AA", index)

  def test_an_explicit_empty_dataset_is_rejected_not_defaulted(self):
    with self.assertRaisesRegex(
        data_loader.DataValidationError, r"Invalid dataset format"
    ):
      data_loader.clean_dataset("")

  def test_routes_serve_the_configured_dataset(self):
    self.publish_two_datasets()
    data_loader.configure_dataset("alpha")
    self.assertEqual(
        self.client.get("/years").json(),
        {"years": [2024]},
    )
    data_loader.configure_dataset("beta")
    self.assertEqual(
        self.client.get("/years").json(),
        {"years": [2025]},
    )

  @parameterized.parameters(
      "/years", "/country/2024/CH", "/countries/2024.json"
  )
  def test_routes_return_503_when_unconfigured(self, path):
    self.unconfigure()
    resp = self.client.get(path)
    self.assertEqual(resp.status_code, 503)
    self.assertIn(data_loader.DATASET_ENV_VAR, resp.json()["detail"])

  # --- startup ---

  def test_lifespan_aborts_startup_when_unconfigured(self):
    self.unconfigure()
    with self.assertRaisesRegex(
        data_loader.DatasetConfigError, data_loader.DATASET_ENV_VAR
    ):
      with testclient.TestClient(server.app):
        pass

  def test_lifespan_aborts_startup_on_an_invalid_env_value(self):
    self.unconfigure()
    os.environ[data_loader.DATASET_ENV_VAR] = "../escape"
    with self.assertRaisesRegex(
        data_loader.DatasetConfigError, "Invalid dataset name"
    ):
      with testclient.TestClient(server.app):
        pass

  def test_lifespan_configures_the_dataset_from_the_env(self):
    self.unconfigure()
    os.environ[data_loader.DATASET_ENV_VAR] = testdata_fixture.DATASET
    with testclient.TestClient(server.app) as client:
      resp = client.get("/years")
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(
        resp.json()["years"],
        sorted(testdata_fixture.YEAR_FACTOR),
    )
    self.assertEqual(data_loader.active_dataset(), testdata_fixture.DATASET)

  def test_lifespan_keeps_an_explicitly_configured_dataset(self):
    data_loader.configure_dataset(testdata_fixture.DATASET)
    os.environ[data_loader.DATASET_ENV_VAR] = "ignored"
    with testclient.TestClient(server.app):
      pass
    self.assertEqual(data_loader.active_dataset(), testdata_fixture.DATASET)

  def test_main_exits_with_a_usage_error_when_unconfigured(self):
    self.unconfigure()
    with mock.patch.object(
        server.uvicorn, "run", autospec=True, spec_set=True
    ) as mock_run:
      with self.assertRaisesRegex(
          server.absl_app.UsageError, data_loader.DATASET_ENV_VAR
      ):
        server.main(["server"])
    mock_run.assert_not_called()

  def test_main_exits_with_a_usage_error_on_an_invalid_env_value(self):
    self.unconfigure()
    os.environ[data_loader.DATASET_ENV_VAR] = "a/b"
    with mock.patch.object(
        server.uvicorn, "run", autospec=True, spec_set=True
    ) as mock_run:
      with self.assertRaisesRegex(
          server.absl_app.UsageError, "Invalid dataset name"
      ):
        server.main(["server"])
    mock_run.assert_not_called()

  def test_main_configures_from_the_env_and_starts_the_server(self):
    self.unconfigure()
    os.environ[data_loader.DATASET_ENV_VAR] = "my_ds"
    with mock.patch.object(
        server.uvicorn, "run", autospec=True, spec_set=True
    ) as mock_run:
      server.main(["server"])
    mock_run.assert_called_once()
    self.assertIs(mock_run.call_args.args[0], server.app)
    self.assertEqual(data_loader.active_dataset(), "my_ds")


class ExplicitYearTest(DashboardTestBase):
  """Every year-scoped route takes the year as a path segment; none defaults.

  The year is always the segment right after the resource name
  (`/{resource}/{year}/...`). On multi-entity endpoints (`/series`, `/temporal`,
  `/rankings`), filters such as `iso`, `fir_id`, and `gwp` are passed as query
  parameters (whereas `/country/{year}/{iso}` takes `iso` in the path). A
  request without a year simply matches no route.
  """

  # Routes that are not scoped to a single year: `/series` spans every year,
  # the rest are static assets.
  _YEAR_FREE_ROUTES = frozenset({
      "/years",
      "/series/{metric}",
      "/",
      "/index.html",
      "/favicon.ico",
      "/favicon.svg",
      "/assets/{asset_path:path}",
  })

  # Query-style (and iso-first) URLs the API used to serve.
  _REMOVED_URLS = (
      "/countries?year=2025",
      "/firs?year=2025",
      "/fir/LSAS_SWITZERLAND?year=2025",
      "/rankings/co2e_gwp_tonnes?year=2025&gwp=gwp50",
      "/countries.json?year=2025",
      "/firs.json?year=2025",
  )

  def _api_route_paths(self):
    return [
        route.path
        for route in server.app.routes
        if isinstance(route, fastapi.routing.APIRoute)
    ]

  def test_year_is_the_segment_after_the_resource_on_every_scoped_route(self):
    scoped = [
        path
        for path in self._api_route_paths()
        if path not in self._YEAR_FREE_ROUTES
    ]
    self.assertNotEmpty(scoped)
    invalid_segments = [
        path
        for path in scoped
        if len(path.split("/")) < 3
        or path.split("/")[2] not in ("{year}", "{year}.json")
    ]
    self.assertEmpty(invalid_segments)

  @parameterized.parameters(
      "/countries/{year}",
      "/countries/{year}.json",
      "/firs/{year}",
      "/firs/{year}.json",
      "/country/{year}/{iso}",
      "/rankings/{year}/{metric}",
      "/temporal/{year}",
      "/grid/{year}/availability",
  )
  def test_expected_year_scoped_routes_are_registered(self, path):
    self.assertIn(path, set(self._api_route_paths()))

  def test_openapi_marks_year_a_required_path_param_without_a_default(self):
    spec = self.client.get("/openapi.json").json()
    templates = [path for path in spec["paths"] if "{year}" in path]
    self.assertNotEmpty(templates)
    invalid_year_params = [
        template
        for template in templates
        if not any(
            p.get("name") == "year"
            and p.get("in") == "path"
            and p.get("required")
            and p.get("schema", {}).get("type") == "integer"
            and "default" not in p.get("schema", {})
            for p in spec["paths"][template]["get"]["parameters"]
        )
    ]
    self.assertEmpty(invalid_year_params)
    query_year_params = [
        template
        for template in templates
        if any(
            p.get("name") == "year" and p.get("in") == "query"
            for p in spec["paths"][template]["get"]["parameters"]
        )
    ]
    self.assertEmpty(query_year_params)

  @parameterized.parameters(
      "/countries/{year}",
      "/firs/{year}",
      "/country/{year}/{iso}",
      "/rankings/{year}/{metric}",
  )
  def test_openapi_year_descriptions_point_at_years(self, template):
    spec = self.client.get("/openapi.json").json()
    params = spec["paths"][template]["get"]["parameters"]
    (year_param,) = [
        point_item for point_item in params if point_item["name"] == "year"
    ]
    self.assertIn("/years", year_param["description"])

  @parameterized.parameters(*_REMOVED_URLS)
  def test_old_query_style_urls_return_404(self, url):
    self.assertEqual(self.client.get(url).status_code, 404)

  def test_old_query_style_routes_omitted_from_openapi(self):
    self.assertFalse(hasattr(server, "get_fir_by_id"))
    spec_paths = set(self.client.get("/openapi.json").json()["paths"])
    removed_routes = {
        "/countries",
        "/firs",
        "/fir/{fir_id}",
        "/rankings/{metric}",
        "/country/{iso}/{year}",
        "/countries.json",
        "/firs.json",
    }
    self.assertEmpty(removed_routes.intersection(spec_paths))

  def test_iso_first_country_url_is_read_as_an_unsupported_year(self):
    # `/country/CH/2025` now parses as year="CH", iso="2025".
    resp = self.client.get("/country/CH/2025")
    self.assertEqual(resp.status_code, 400)
    self.assertIn("Available years are", resp.json()["detail"])

  @parameterized.parameters(
      "/countries",
      "/firs",
      "/countries/",
      "/country/CH",
      "/rankings/co2e_gwp_tonnes",
  )
  def test_missing_year_matches_no_route(self, url):
    self.assertIn(self.client.get(url).status_code, (404, 405))

  @parameterized.parameters(
      "/countries/1999",
      "/firs/1999",
      "/country/1999/CH",
      "/rankings/1999/co2e_gwp_tonnes",
      "/countries/1999.json",
      "/firs/1999.json",
  )
  def test_unsupported_path_year_is_rejected_with_400(self, url):
    resp = self.client.get(url)
    self.assertEqual(resp.status_code, 400, resp.text)

  def test_openapi_descriptions_name_no_specific_year(self):
    spec_text = self.client.get("/openapi.json").text
    matched_years = [
        year for year in testdata_fixture.YEAR_FACTOR if str(year) in spec_text
    ]
    self.assertEmpty(matched_years)

  @parameterized.parameters(2022, 2025)
  def test_country_route_serves_the_requested_year(self, year):
    country = self.client.get(f"/country/{year}/CH")
    self.assertEqual(country.status_code, 200)
    self.assertEqual(country.json()["iso_a2"], "CH")

  @parameterized.parameters(2022, 2025)
  def test_rankings_route_serves_the_requested_year(self, year):
    rankings = self.client.get(f"/rankings/{year}/co2e_gwp_tonnes")
    self.assertEqual(rankings.status_code, 200)
    self.assertEqual(rankings.json()["year"], year)

  @parameterized.parameters("2022", "2025")
  def test_countries_route_serves_the_requested_year(self, year):
    countries = self.client.get(f"/countries/{year}")
    self.assertEqual(countries.status_code, 200)
    self.assertIn("CH", [item_row["iso_a2"] for item_row in countries.json()])

  @parameterized.parameters("2022", "2025")
  def test_firs_route_serves_the_requested_year(self, year):
    firs = self.client.get(f"/firs/{year}")
    self.assertEqual(firs.status_code, 200)
    self.assertNotEmpty(firs.json())

  def test_years_response_has_no_default_year(self):
    data = self.client.get("/years").json()
    self.assertEqual(set(data), {"years"})
    self.assertEqual(data["years"], sorted(data["years"]))

  def test_data_loader_has_no_default_year_helper(self):
    self.assertFalse(hasattr(data_loader, "get_default_year"))

  def test_grid_meta_has_no_binary_endpoint(self):
    root = self.create_tempdir().full_path
    year_dir = _publish_run_dir(root, "2024")
    os.makedirs(year_dir, exist_ok=True)
    with open(
        os.path.join(year_dir, "grid_meta.json"), "w", encoding="utf-8"
    ) as f:
      json.dump({"width": 2, "height": 1}, f)
    with open(os.path.join(year_dir, "g.bin.gz"), "wb") as f:
      f.write(gzip.compress(b"\x00" * 8))
    data_loader.clear_all_caches()
    with mock.patch.object(data_loader, "DATA_DIR", root):
      meta = data_loader.get_grid_meta("2024", prefix="g")
      self.assertIsNotNone(meta)
      self.assertEqual(meta["grid_name"], "g")
      self.assertNotIn("binary_endpoint", meta)
      resp = self.client.get("/grid/2024/g/meta")
      self.assertEqual(resp.status_code, 200)
      self.assertNotIn("binary_endpoint", resp.json())

  @parameterized.named_parameters(
      ("no_params", {}),
      ("with_year", {"year": "2024"}),
  )
  def test_filename_grid_route_is_removed(self, params):
    resp = self.client.get(
        "/grid/binary/total_global_impact_grid.bin.gz", params=params
    )
    self.assertEqual(resp.status_code, 404)
    self.assertFalse(hasattr(server, "get_grid_binary_by_filename"))

  @parameterized.product(
      path=("/countries_meta", "/firs_meta"),
      params=({}, {"year": "2025"}),
  )
  def test_meta_count_routes_return_404(self, path, params):
    resp = self.client.get(path, params=params)
    self.assertEqual(resp.status_code, 404, resp.text)

  def test_meta_count_handlers_and_models_are_removed(self):
    # The UI derives entity totals from /countries and /firs; there is no
    # separate count endpoint and no hardcoded roster size to fall back to.
    self.assertFalse(hasattr(server, "get_countries_meta"))
    self.assertFalse(hasattr(server, "get_firs_meta"))
    self.assertFalse(hasattr(server, "_compute_meta_count"))
    self.assertFalse(hasattr(models, "CountriesMetaResponse"))
    self.assertFalse(hasattr(models, "FirsMetaResponse"))

  def test_meta_count_routes_omitted_from_openapi(self):
    spec_paths = self.client.get("/openapi.json").json()["paths"]
    self.assertNotIn("/countries_meta", spec_paths)
    self.assertNotIn("/firs_meta", spec_paths)


if __name__ == "__main__":
  absltest.main()
