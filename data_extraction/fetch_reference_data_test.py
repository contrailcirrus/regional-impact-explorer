"""Unit tests for fetch_reference_data."""

import json
from typing import Any

from absl import app
from absl import flags
from absl.testing import absltest
from absl.testing import flagsaver

from data_extraction import fetch_reference_data
from data_extraction import metadata_loader


def _polygon_feature(
    properties: dict[str, Any], ring: list[list[float]]
) -> dict[str, Any]:
  """Builds a GeoJSON Polygon Feature dict for test fixtures."""
  return {
      "type": "Feature",
      "properties": properties,
      "geometry": {"type": "Polygon", "coordinates": [ring]},
  }


def _sample_natural_earth_geojson() -> dict[str, Any]:
  return {
      "type": "FeatureCollection",
      "features": [
          _polygon_feature(
              {
                  "NAME": "Belgium",
                  "ISO_A2": "be",
                  "ISO_A3": "bel",
                  "POP_EST": 11500000,
              },
              [[2.0, 49.0], [6.0, 49.0], [6.0, 52.0], [2.0, 52.0], [2.0, 49.0]],
          ),
          _polygon_feature(
              {"NAME": "France", "ISO_A2": "-99", "ISO_A3": "-99"},
              [
                  [-5.0, 42.0],
                  [8.0, 42.0],
                  [8.0, 51.0],
                  [-5.0, 51.0],
                  [-5.0, 42.0],
              ],
          ),
      ],
  }


_SAMPLE_POPULATION_CSV = (
    "Country Name,Country Code,Year,Value\n"
    "Belgium,BEL,2023,11400000\n"
    "Belgium,BEL,2024,11500000\n"
)


def _sample_ghg_json() -> dict[str, Any]:
  return {
      "data": [{
          "iso_code3": "BEL",
          "sector": "Total excluding LULUCF",
          "gas": "All GHG",
          "data_source": "Climate Watch",
          "emissions": [
              {"year": 2022, "value": 110.0},
              {"year": 2023, "value": 105.5},
          ],
      }]
  }


_SAMPLE_AIRPORTS_CSV = (
    "ident,gps_code,iata_code,iso_country\nEBBR,EBBR,BRU,BE\nEDDF,EDDF,FRA,DE\n"
)


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


class FetchReferenceDataTest(absltest.TestCase):

  def test_normalize_country_geojson_converts_uppercase_properties(self):
    normalized = fetch_reference_data.normalize_country_geojson(
        _sample_natural_earth_geojson()
    )
    self.assertEqual(normalized["type"], "FeatureCollection")
    self.assertLen(normalized["features"], 2)
    self.assertEqual(
        normalized["features"][0]["properties"],
        {"name": "Belgium", "iso_a2": "BE", "iso_a3": "BEL"},
    )
    self.assertEqual(
        normalized["features"][1]["properties"],
        {"name": "France", "iso_a2": "-99", "iso_a3": "-99"},
    )

  def test_normalize_country_geojson_rejects_invalid_payloads(self):
    with self.assertRaisesRegex(ValueError, "non-empty features"):
      fetch_reference_data.normalize_country_geojson({"features": []})

    with self.assertRaisesRegex(ValueError, "missing geometry"):
      fetch_reference_data.normalize_country_geojson(
          {"features": [{"type": "Feature", "properties": {"NAME": "X"}}]}
      )

    with self.assertRaisesRegex(ValueError, "missing name or iso_a2"):
      fetch_reference_data.normalize_country_geojson({
          "features": [{
              "type": "Feature",
              "properties": {"NAME": "Nowhere"},
              "geometry": {"type": "Polygon", "coordinates": [[[0, 0]]]},
          }]
      })

  def test_validate_population_csv_checks_columns_and_rows(self):
    self.assertEqual(
        fetch_reference_data.validate_population_csv(_SAMPLE_POPULATION_CSV), 2
    )
    with self.assertRaisesRegex(ValueError, "missing required columns"):
      fetch_reference_data.validate_population_csv(
          "Entity,Code,Year,Population"
          " (historical)\nBelgium,BEL,2024,11500000\n"
      )
    with self.assertRaisesRegex(ValueError, "0 valid population rows"):
      fetch_reference_data.validate_population_csv(
          "Country Name,Country Code,Year,Value\n"
      )

  def test_validate_airports_csv_checks_columns_and_rows(self):
    self.assertEqual(
        fetch_reference_data.validate_airports_csv(_SAMPLE_AIRPORTS_CSV), 2
    )
    with self.assertRaisesRegex(ValueError, "iso_country"):
      fetch_reference_data.validate_airports_csv("ident,name\nEBBR,Brussels\n")
    with self.assertRaisesRegex(ValueError, "0 valid airport mappings"):
      fetch_reference_data.validate_airports_csv("gps_code,iso_country\n")

  def test_fetch_ghg_emissions_records_paginates_until_short_page(self):
    responses = {
        "https://example.org/api?per_page=2": (
            json.dumps({
                "data": [
                    _sample_ghg_json()["data"][0],
                    {
                        "iso_code3": "FRA",
                        "sector": "Total excluding LULUCF",
                        "gas": "All GHG",
                        "data_source": "Climate Watch",
                        "emissions": [{"year": 2023, "value": 400.0}],
                    },
                ]
            }).encode("utf-8")
        ),
        "https://example.org/api?per_page=2&page=2": (
            json.dumps({
                "data": [{
                    "iso_code3": "DEU",
                    "sector": "Total excluding LULUCF",
                    "gas": "All GHG",
                    "data_source": "Climate Watch",
                    "emissions": [{"year": 2023, "value": 680.0}],
                }]
            }).encode("utf-8")
        ),
    }

    result = fetch_reference_data.fetch_ghg_emissions_records(
        "https://example.org/api?per_page=2",
        fetcher=lambda url, timeout: responses.pop(url),
    )
    self.assertEmpty(responses)
    self.assertLen(result["data"], 3)

  def _fetch_sample_reference_data(self) -> dict[str, str]:
    """Runs fetch_all_reference_data against stubbed in-memory responses."""
    out_dir = self.create_tempdir().full_path
    responses = {
        "https://example.org/countries.geojson": (
            json.dumps(_sample_natural_earth_geojson()).encode("utf-8")
        ),
        "https://example.org/population.csv": _SAMPLE_POPULATION_CSV.encode(
            "utf-8"
        ),
        "https://example.org/ghg.json": (
            json.dumps(_sample_ghg_json()).encode("utf-8")
        ),
        "https://example.org/airports.csv": _SAMPLE_AIRPORTS_CSV.encode(
            "utf-8"
        ),
    }
    return fetch_reference_data.fetch_all_reference_data(
        out_dir,
        country_geojson_url="https://example.org/countries.geojson",
        population_csv_url="https://example.org/population.csv",
        ghg_emissions_url="https://example.org/ghg.json",
        airports_csv_url="https://example.org/airports.csv",
        fetcher=lambda url, timeout: responses[url],
    )

  def test_fetch_all_reference_data_writes_loader_compatible_countries(self):
    written = self._fetch_sample_reference_data()
    countries = metadata_loader.load_country_geojson(written["countries"])
    self.assertLen(countries, 2)
    self.assertEqual(countries[0]["properties"]["iso_a2"], "BE")
    self.assertEqual(countries[0]["properties"]["name"], "Belgium")

  def test_fetch_all_reference_data_writes_loader_compatible_stats_and_airports(
      self,
  ):
    written = self._fetch_sample_reference_data()
    stats = metadata_loader.load_country_baseline_stats(
        population_path=written["population"],
        ghg_emissions_path=written["ghg_emissions"],
        input_year=2025,
    )
    self.assertEqual(stats["BE"]["population"], 11500000.0)
    self.assertEqual(stats["BE"]["ghg_mtco2e"], 105.5)

    airports = metadata_loader.load_airports_mapping(written["airports"])
    self.assertEqual(airports, {"EBBR": "BE", "EDDF": "DE"})

  def test_main_validates_flags(self):
    with self.assertRaisesRegex(app.UsageError, "Unrecognized positional"):
      fetch_reference_data.main(["fetch_reference_data", "extra"])
    with flagsaver.flagsaver(output_dir="   "):
      with self.assertRaisesRegex(app.UsageError, "--output_dir is required"):
        fetch_reference_data.main(["fetch_reference_data"])

  def test_set_up_module_marks_unparsed_flags_for_standalone_runners(self):
    saved_flags = flagsaver.save_flag_values()
    self.addCleanup(flagsaver.restore_flag_values, saved_flags)
    self.addCleanup(flags.FLAGS.mark_as_parsed)
    flags.FLAGS.unparse_flags()
    self.assertFalse(flags.FLAGS.is_parsed())
    setUpModule()
    with flagsaver.flagsaver(output_dir="   "):
      with self.assertRaisesRegex(app.UsageError, "--output_dir is required"):
        fetch_reference_data.main(["fetch_reference_data"])


if __name__ == "__main__":
  absltest.main()
