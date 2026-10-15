"""Shared test fixtures for data_extraction unit and integration tests."""

from collections.abc import Mapping, Sequence
import json
import os
from typing import Any
from unittest import mock

from absl.testing import absltest
import pandas as pd

from common import constants
from data_extraction import grid_publish
from data_extraction import metadata_loader


def read_value_max(dataset_dir: str) -> Mapping[str, float]:
  """Returns the shared grid `value_max` dictionary from `dataset_dir`.

  Args:
    dataset_dir: Path to a published dataset run directory.

  Returns:
    Mapping of grid metric type (`"density"`, `"impact"`) to its max value.
  """
  path = os.path.join(dataset_dir, constants.SHARED_GRID_META_FILENAME)
  with open(path, "r", encoding="utf-8") as f:
    return json.load(f)[grid_publish.VALUE_MAX_META_KEY]


def make_country_feature(
    name: str, iso2: str, coords: Sequence[Sequence[float]]
) -> dict[str, Any]:
  """Builds a single Polygon GeoJSON Feature for a country boundary.

  Args:
    name: Display name of the country.
    iso2: ISO 3166-1 alpha-2 country code.
    coords: Sequence of `[lon, lat]` coordinate pairs forming the exterior ring.

  Returns:
    A GeoJSON Feature dictionary with `"name"` and `"iso_a2"` properties.
  """
  return {
      "type": "Feature",
      "properties": {"name": name, "iso_a2": iso2},
      "geometry": {"type": "Polygon", "coordinates": [coords]},
  }


def write_test_countries_geojson(test_dir: str) -> str:
  """Writes a two-country (Belgium, Germany) GeoJSON fixture to `test_dir`.

  Args:
    test_dir: Directory where `countries.geojson` will be written.

  Returns:
    Path to the written `countries.geojson` file.
  """
  path = os.path.join(test_dir, "countries.geojson")
  features = [
      make_country_feature(
          "Belgium",
          "BE",
          [[2.0, 49.0], [6.0, 49.0], [6.0, 52.0], [2.0, 52.0], [2.0, 49.0]],
      ),
      make_country_feature(
          "Germany",
          "DE",
          [[6.0, 49.0], [14.0, 49.0], [14.0, 55.0], [6.0, 55.0], [6.0, 49.0]],
      ),
  ]
  with open(path, "w", encoding="utf-8") as f:
    json.dump({"type": "FeatureCollection", "features": features}, f)
  return path


def make_fir_feature(
    name: str,
    icao: str,
    iso2s: str,
    fir_type: str,
    coords: Sequence[Sequence[float]],
) -> dict[str, Any]:
  """Builds a single Polygon GeoJSON Feature for an FIR boundary.

  Args:
    name: FIR display name (`firname`).
    icao: 4-letter ICAO FIR code (`fir_icao`).
    iso2s: Comma-separated ISO2 codes of controlling countries.
    fir_type: FIR classification (`"domestic"` or `"international"`).
    coords: Sequence of `[lon, lat]` coordinate pairs forming the exterior ring.

  Returns:
    A GeoJSON Feature dictionary representing the FIR boundary.
  """
  return {
      "type": "Feature",
      "properties": {
          "firname": name,
          "fir_icao": icao,
          "fir_to_country_iso2s": iso2s,
          "type": fir_type,
      },
      "geometry": {"type": "Polygon", "coordinates": [coords]},
  }


def write_test_firs_geojson(test_dir: str) -> str:
  """Writes a two-FIR (EBBU, EDGG) GeoJSON fixture to `test_dir`.

  Args:
    test_dir: Directory where `firs.geojson` will be written.

  Returns:
    Path to the written `firs.geojson` file.
  """
  path = os.path.join(test_dir, "firs.geojson")
  features = [
      make_fir_feature(
          "FIR BRUXELLES",
          "EBBU",
          "BE",
          "domestic",
          [[2.0, 49.0], [6.0, 49.0], [6.0, 52.0], [2.0, 52.0], [2.0, 49.0]],
      ),
      make_fir_feature(
          "FIR LANGEN",
          "EDGG",
          "DE",
          "international",
          [[6.0, 49.0], [14.0, 49.0], [14.0, 55.0], [6.0, 55.0], [6.0, 49.0]],
      ),
  ]
  with open(path, "w", encoding="utf-8") as f:
    json.dump({"type": "FeatureCollection", "features": features}, f)
  return path


def write_test_airports_csv(test_dir: str) -> str:
  """Writes a minimal four-airport mapping CSV fixture to `test_dir`.

  Args:
    test_dir: Directory where `airports.csv` will be written.

  Returns:
    Path to the written `airports.csv` file.
  """
  path = os.path.join(test_dir, "airports.csv")
  with open(path, "w", encoding="utf-8") as f:
    f.write("gps_code,iso_country\nEBBR,BE\nEDDF,DE\nEGLL,GB\nKJFK,US\n")
  return path


def write_test_baseline_data(test_dir: str) -> tuple[str, str]:
  """Writes World Bank population CSV and Climate Watch GHG JSON fixtures.

  Args:
    test_dir: Directory where `population.csv` and `emissions.json` are written.

  Returns:
    Tuple of `(population_csv_path, ghg_emissions_json_path)`.
  """
  pop_path = os.path.join(test_dir, "population.csv")
  with open(pop_path, "w", encoding="utf-8") as f:
    f.write(
        "Country Name,Country Code,Year,Value\n"
        "Belgium,BEL,2024,1000000\n"
        "Germany,DEU,2024,8000000\n"
    )
  ghg_path = os.path.join(test_dir, "emissions.json")
  with open(ghg_path, "w", encoding="utf-8") as f:
    json.dump(
        {
            "data": [
                {
                    "iso_code3": iso3,
                    "data_source": "Climate Watch",
                    "sector": "Total excluding LULUCF",
                    "gas": "All GHG",
                    "emissions": [{"year": 2023, "value": value}],
                }
                for iso3, value in (("BEL", 10.0), ("DEU", 80.0))
            ]
        },
        f,
    )
  return pop_path, ghg_path


def write_test_parquet_segments(test_dir: str) -> str:
  """Writes a four-row Parquet flight-segments fixture to `test_dir`.

  Args:
    test_dir: Directory where `test_segments.pq` will be written.

  Returns:
    Path to the written `test_segments.pq` file.
  """
  pq_path = os.path.join(test_dir, "test_segments.pq")
  mock_df = pd.DataFrame({
      "lon_start": [3.0, 4.0, 100.0, float("nan")],
      "lat_start": [50.0, 50.0, 10.0, 50.0],
      "lon_end": [5.0, 8.0, 101.0, 52.0],
      "lat_end": [51.0, 50.0, 11.0, 52.0],
      "sum_ef_mj": [1000.0, 2000.0, 500.0, 100.0],
      "time_start": [
          "2025-01-15T12:00:00",
          "2025-01-15T14:00:00",
          "2025-02-10T18:00:00",
          "2025-01-15T12:00:00",
      ],
      "time_start_tz": [1, 1, 0, 0],
      "departure_airport_icao": ["EBBR", "EBBR", "KJFK", "EBBR"],
      "arrival_airport_icao": ["EBBR", "EDDF", "KJFK", "EDDF"],
  })
  mock_df.to_parquet(pq_path)
  return pq_path


def forbid_reference_data_reads(
    test_case: absltest.TestCase,
) -> list[mock.Mock]:
  """Patches reference-data loaders on `metadata_loader` to fail if called.

  Args:
    test_case: Active test case instance used to register context managers.

  Returns:
    List of installed mock loader objects.
  """
  return [
      test_case.enter_context(
          mock.patch.object(
              metadata_loader,
              name,
              autospec=True,
              spec_set=True,
              side_effect=AssertionError(f"{name} must not be called"),
          )
      )
      for name in (
          "load_boundaries",
          "load_country_geojson",
          "load_firs_geojson",
          "load_country_baseline_stats",
      )
  ]


def assert_loaders_not_called(loaders: Sequence[mock.Mock]) -> None:
  """Asserts that none of the provided mock loaders were invoked.

  Args:
    loaders: Sequence of mock loader objects returned by
      `forbid_reference_data_reads`.
  """
  for loader in loaders:
    loader.assert_not_called()
