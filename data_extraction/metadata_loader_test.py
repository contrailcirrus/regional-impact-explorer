"""Unit tests for metadata_loader."""

import json
import os
from unittest import mock

from absl import flags
from absl.testing import absltest
from absl.testing import parameterized

from common import constants
from data_extraction import metadata_loader


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


class MetadataLoaderTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.test_dir = self.create_tempdir().full_path

    self.airports_csv = os.path.join(self.test_dir, "airports.csv")
    with open(self.airports_csv, "w", encoding="utf-8") as f:
      f.write("gps_code,iso_country\nEBBR,BE\nEDDF,DE\nEGLL,GB\n")

    self.country_geojson = os.path.join(self.test_dir, "countries.geojson")
    with open(self.country_geojson, "w", encoding="utf-8") as f:
      json.dump({
          "type": "FeatureCollection",
          "features": [{
              "type": "Feature",
              "properties": {"name": "Belgium", "iso_a2": "BE"},
              "geometry": {
                  "type": "Polygon",
                  "coordinates": [[[2.0, 49.0], [6.0, 49.0], [6.0, 52.0],
                                   [2.0, 52.0], [2.0, 49.0]]],
              },
          }],
      }, f)

    self.firs_geojson = os.path.join(self.test_dir, "firs.geojson")
    with open(self.firs_geojson, "w", encoding="utf-8") as f:
      json.dump({
          "type": "FeatureCollection",
          "features": [{
              "type": "Feature",
              "properties": {
                  "firname": "FIR BRUXELLES",
                  "fir_icao": "EBBU",
                  "fir_to_country_iso2s": "BE",
                  "type": "domestic",
              },
              "geometry": {
                  "type": "Polygon",
                  "coordinates": [[[2.0, 49.0], [6.0, 49.0], [6.0, 52.0],
                                   [2.0, 52.0], [2.0, 49.0]]],
              },
          }],
      }, f)

  def test_load_airports_mapping(self):
    mapping = metadata_loader.load_airports_mapping(self.airports_csv)
    self.assertEqual(mapping.get("EBBR"), "BE")
    self.assertEqual(mapping.get("EDDF"), "DE")
    self.assertIsNone(mapping.get("NONEXISTENT"))

  def test_load_airports_mapping_raises_on_missing_file(self):
    # Missing file must fail loudly rather than silently zeroing route metrics.
    with self.assertRaisesRegex(
        RuntimeError, "Airports mapping file not found"
    ):
      metadata_loader.load_airports_mapping("/nonexistent/file.csv")

  def test_load_airports_mapping_propagates_existence_check_errors(self):
    # An unreadable input must not be reported as a missing one.
    with mock.patch.object(
        metadata_loader.fs,
        "exists",
        side_effect=PermissionError("denied"),
        autospec=True,
        spec_set=True,
    ):
      with self.assertRaisesRegex(PermissionError, "denied"):
        metadata_loader.load_airports_mapping(self.airports_csv)

  def test_load_airports_mapping_raises_on_empty_csv(self):
    empty_csv = os.path.join(self.test_dir, "empty_airports.csv")
    with open(empty_csv, "w", encoding="utf-8") as f:
      f.write("gps_code,iso_country\n")
    with self.assertRaisesRegex(RuntimeError, "yielded 0 airport entries"):
      metadata_loader.load_airports_mapping(empty_csv)

  def test_load_country_geojson(self):
    features = metadata_loader.load_country_geojson(self.country_geojson)
    self.assertLen(features, 1)
    self.assertEqual(features[0]["properties"]["name"], "Belgium")

  def test_load_country_geojson_normalizes_uppercase_properties(self):
    ne_geojson = self._write_firs(
        "ne_countries.geojson",
        [
            {"NAME": "Belgium", "ISO_A2": "be", "ISO_A3": "bel"},
            {"NAME": "France", "ISO_A2": "-99", "ISO_A3": "-99"},
        ],
    )
    features = metadata_loader.load_country_geojson(ne_geojson)
    self.assertLen(features, 2)
    self.assertEqual(
        features[0]["properties"],
        {"name": "Belgium", "iso_a2": "BE", "iso_a3": "BEL"},
    )
    self.assertEqual(
        features[1]["properties"],
        {"name": "France", "iso_a2": "-99", "iso_a3": "-99"},
    )

  def test_load_country_geojson_raises_on_missing_file(self):
    with self.assertRaisesRegex(
        RuntimeError, "Failed to load non-empty country GeoJSON features"
    ):
      metadata_loader.load_country_geojson("/nonexistent/countries.geojson")

  def test_load_country_geojson_raises_on_empty_file(self):
    empty_geojson = os.path.join(self.test_dir, "empty_countries.geojson")
    with open(empty_geojson, "w", encoding="utf-8") as f:
      json.dump({"type": "FeatureCollection", "features": []}, f)
    with self.assertRaisesRegex(
        RuntimeError,
        r"Failed to load non-empty country GeoJSON features.*0 features",
    ):
      metadata_loader.load_country_geojson(empty_geojson)

  def test_load_firs_geojson(self):
    features = metadata_loader.load_firs_geojson(self.firs_geojson)
    self.assertLen(features, 1)
    properties = features[0]["properties"]
    self.assertEqual(properties["fir_id"], "EBBU_BRUXELLES")
    self.assertEqual(properties["fir_icao"], "EBBU")
    self.assertEqual(properties["firname"], "BRUXELLES")
    self.assertEqual(properties["type"], "domestic")

  def test_load_firs_geojson_raises_on_missing_file(self):
    with self.assertRaisesRegex(
        RuntimeError, "Failed to load non-empty FIR GeoJSON features"
    ):
      metadata_loader.load_firs_geojson("/nonexistent/firs.geojson")

  def test_load_firs_geojson_raises_on_empty_file(self):
    empty_firs = os.path.join(self.test_dir, "empty_firs.geojson")
    with open(empty_firs, "w", encoding="utf-8") as f:
      json.dump({"type": "FeatureCollection", "features": []}, f)
    with self.assertRaisesRegex(
        RuntimeError,
        r"Failed to load non-empty FIR GeoJSON features.*0 features",
    ):
      metadata_loader.load_firs_geojson(empty_firs)

  def test_load_country_geojson_requires_explicit_path(self):
    # No bundled fallback: an unset path must fail loudly rather than silently
    # attribute against geometry the operator did not choose.
    with self.assertRaisesRegex(RuntimeError, "--country_geojson_path"):
      metadata_loader.load_country_geojson("")

  def test_load_firs_geojson_requires_explicit_path(self):
    with self.assertRaisesRegex(RuntimeError, "--firs_path"):
      metadata_loader.load_firs_geojson("")

  def test_load_country_baseline_stats_requires_a_source(self):
    with self.assertRaisesRegex(RuntimeError, "No country baseline source"):
      metadata_loader.load_country_baseline_stats(input_year=2025)

  @parameterized.named_parameters(
      dict(
          testcase_name="missing_population_file",
          population_path="/nonexistent/pop.csv",
          expected_error="Population baseline file not found",
      ),
      dict(
          testcase_name="missing_ghg_emissions_file",
          ghg_emissions_path="/nonexistent/emissions.json",
          expected_error="GHG emissions baseline file not found",
      ),
      dict(
          testcase_name="missing_custom_stats_file",
          country_stats_path="/nonexistent/custom_stats.json",
          expected_error="Custom baseline stats file not found",
      ),
  )
  def test_load_country_baseline_stats_raises_on_missing_source(
      self,
      expected_error,
      population_path=None,
      ghg_emissions_path=None,
      country_stats_path=None,
  ):
    with self.assertRaisesRegex(RuntimeError, expected_error):
      metadata_loader.load_country_baseline_stats(
          population_path=population_path,
          ghg_emissions_path=ghg_emissions_path,
          country_stats_path=country_stats_path,
          input_year=2025,
      )

  def test_load_country_baseline_stats_raises_on_empty_population_csv(self):
    # A population CSV with headers but zero valid rows must also raise.
    empty_pop = os.path.join(self.test_dir, "empty_pop.csv")
    with open(empty_pop, "w", encoding="utf-8") as f:
      f.write("Country Name,Country Code,Year,Value\n")
    with self.assertRaisesRegex(RuntimeError, r"yielded 0 country records"):
      metadata_loader.load_country_baseline_stats(
          population_path=empty_pop, input_year=2025
      )

  def _write_baseline_fixture(self) -> tuple[str, str]:
    pop_csv = os.path.join(self.test_dir, "pop.csv")
    with open(pop_csv, "w", encoding="utf-8") as f:
      f.write(
          "Country Name,Country Code,Year,Value\n"
          "Belgium,BEL,2023,11400000\n"
          "Belgium,BEL,2024,11500000\n"
          "Belgium,BEL,2026,99\n"
          "Malta,MLT,2024,560000\n"
          "World,WLD,2024,8000000000\n"
          "Kosovo,XKX,2024,1700000\n"
      )
    emissions_json = os.path.join(self.test_dir, "emissions.json")
    with open(emissions_json, "w", encoding="utf-8") as f:
      json.dump(
          {
              "data": [
                  {
                      "iso_code3": "BEL",
                      "sector": "Total excluding LULUCF",
                      "gas": "All GHG",
                      "data_source": "Climate Watch",
                      "emissions": [
                          {"year": 2022, "value": 110.0},
                          {"year": 2023, "value": 105.5},
                      ],
                  },
                  {
                      "iso_code3": "BEL",
                      "sector": "Energy",
                      "gas": "All GHG",
                      "data_source": "Climate Watch",
                      "emissions": [{"year": 2023, "value": 1.0}],
                  },
              ]
          },
          f,
      )
    return pop_csv, emissions_json

  def test_load_country_baseline_stats_joins_by_iso3_without_boundaries(self):
    pop_csv, emissions_json = self._write_baseline_fixture()
    stats = metadata_loader.load_country_baseline_stats(
        population_path=pop_csv,
        ghg_emissions_path=emissions_json,
        input_year=2025,
    )
    self.assertEqual(
        stats["BE"],
        {
            "iso_a3": "BEL",
            "name": "Belgium",
            "population": 11500000.0,
            "ghg_mtco2e": 105.5,
        },
    )
    self.assertEqual(stats["MT"]["iso_a3"], "MLT")
    self.assertEqual(stats["MT"]["population"], 560000.0)
    self.assertNotIn("ghg_mtco2e", stats["MT"])

  def test_load_country_baseline_stats_registers_iso_and_skips_non_iso(self):
    pop_csv, emissions_json = self._write_baseline_fixture()
    stats = metadata_loader.load_country_baseline_stats(
        population_path=pop_csv,
        ghg_emissions_path=emissions_json,
        input_year=2025,
    )
    self.assertEqual(stats["SG"], {"iso_a3": "SGP", "name": "Singapore"})
    self.assertNotIn("XK", stats)

  def test_load_country_baseline_stats_custom_overrides_per_field(self):
    pop_csv = os.path.join(self.test_dir, "pop.csv")
    with open(pop_csv, "w", encoding="utf-8") as f:
      f.write(
          "Country Name,Country Code,Year,Value\nBelgium,BEL,2024,11500000\n"
      )
    custom_json = os.path.join(self.test_dir, "custom.json")
    with open(custom_json, "w", encoding="utf-8") as f:
      json.dump(
          {
              "be": {"ghg_mtco2e": 100.0},
              "XK": {"population": 1700000, "ghg_mtco2e": 9.0},
          },
          f,
      )

    stats = metadata_loader.load_country_baseline_stats(
        population_path=pop_csv,
        country_stats_path=custom_json,
        input_year=2025,
    )
    self.assertEqual(stats["BE"]["population"], 11500000.0)
    self.assertEqual(stats["BE"]["ghg_mtco2e"], 100.0)
    self.assertEqual(stats["BE"]["iso_a3"], "BEL")
    self.assertEqual(
        stats["XK"], {"population": 1700000.0, "ghg_mtco2e": 9.0}
    )

  def test_load_country_baseline_stats_custom_file_alone_is_sufficient(self):
    custom_json = os.path.join(self.test_dir, "custom_only.json")
    with open(custom_json, "w", encoding="utf-8") as f:
      json.dump({"be": {"ghg_mtco2e": 100.0}}, f)
    stats = metadata_loader.load_country_baseline_stats(
        country_stats_path=custom_json, input_year=2025
    )
    self.assertEqual(stats["BE"]["ghg_mtco2e"], 100.0)
    self.assertNotIn("population", stats["BE"])

  def _write_firs(self, name, properties_per_feature):
    path = os.path.join(self.test_dir, name)
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[2.0, 49.0], [6.0, 49.0], [6.0, 52.0], [2.0, 52.0], [2.0, 49.0]]
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
      json.dump(
          {
              "type": "FeatureCollection",
              "features": [
                  {"type": "Feature", "properties": item, "geometry": polygon}
                  for item in properties_per_feature
              ],
          },
          f,
      )
    return path

  def _load_legacy_firs(self):
    path = self._write_firs(
        "firs_legacy.geojson",
        [{
            "icaocode": "EDGG",
            "ICAO": "EDGG",
            "FIR_NAME": "FIR LANGEN",
            "NAME": "LANGEN",
            "ISO2": "DE",
            "Country_So": "Germany",
            "F2LETTER": "ED",
            "SQKMgeod": 1234.0,
        }],
    )
    return metadata_loader.load_firs_geojson(path)

  def test_load_firs_geojson_ignores_other_spellings(self):
    firs = self._load_legacy_firs()
    properties = firs[0]["properties"]
    self.assertEqual(properties["fir_icao"], "")
    self.assertEqual(properties["firname"], "")
    self.assertEqual(properties["fir_to_country_iso2s"], "")
    self.assertEqual(properties["type"], "")
    self.assertNotIn("areasqkm", properties)

  def test_validate_fir_features_reports_all_missing_canonical_fields(self):
    firs = self._load_legacy_firs()
    with self.assertRaisesRegex(ValueError, "empty fir_icao") as ctx:
      metadata_loader.validate_fir_features(firs)
    message = str(ctx.exception)
    self.assertIn("empty fir_icao", message)
    self.assertIn("empty firname", message)
    self.assertIn("no country mapping", message)
    self.assertIn("invalid type", message)

  def test_load_firs_geojson_does_not_infer_missing_fields(self):
    # No name-based type guess and no filling from other features.
    path = self._write_firs(
        "firs_partial.geojson",
        [
            {
                "fir_icao": "EBBU",
                "firname": "FIR BRUXELLES",
                "fir_to_country_iso2s": "BE",
                "type": "domestic",
            },
            {"firname": "FIR BRUXELLES"},
            {
                "fir_icao": "EDGG",
                "firname": "FIR OCEANIC TEST",
                "fir_to_country_iso2s": "DE",
            },
        ],
    )
    firs = metadata_loader.load_firs_geojson(path)
    self.assertEqual(firs[1]["properties"]["fir_icao"], "")
    self.assertEqual(firs[1]["properties"]["fir_to_country_iso2s"], "")
    self.assertEqual(firs[1]["properties"]["type"], "")
    self.assertEqual(firs[2]["properties"]["type"], "")

  def test_fir_mapping_api_is_removed(self):
    self.assertFalse(hasattr(metadata_loader, "load_fir_reference_mappings"))
    self.assertFalse(hasattr(metadata_loader, "resolve_country_so_to_iso2"))
    self.assertFalse(hasattr(metadata_loader, "FirReferenceMetadata"))
    # Untyped handle: the bad keyword is deliberate.
    load_firs = getattr(metadata_loader, "load_firs_geojson")
    with self.assertRaisesRegex(
        TypeError, "unexpected keyword argument 'fir_mapping_path'"
    ):
      load_firs(self.firs_geojson, fir_mapping_path="/tmp/mapping.csv")

  @parameterized.named_parameters(
      dict(
          testcase_name="prefix_fir",
          raw_name="FIR BRUXELLES",
          fallback="",
          expected="BRUXELLES",
      ),
      dict(
          testcase_name="prefix_and_suffix_uir",
          raw_name="UIR:  rhein  uir ",
          fallback="",
          expected="RHEIN",
      ),
      dict(
          testcase_name="suffix_fir",
          raw_name="BRUSSELS FIR",
          fallback="",
          expected="BRUSSELS",
      ),
      dict(
          testcase_name="empty_uses_fallback",
          raw_name="",
          fallback="EBBU",
          expected="EBBU",
      ),
  )
  def test_clean_fir_name(self, raw_name, fallback, expected):
    self.assertEqual(constants.clean_fir_name(raw_name, fallback), expected)

  @parameterized.named_parameters(
      dict(
          testcase_name="oceanic",
          icao="CZQX",
          raw_name="FIR GANDER OCEANIC",
          expected="CZQX_GANDER_OCEANIC",
      ),
      dict(
          testcase_name="domestic",
          icao="CZQX",
          raw_name="FIR GANDER DOMESTIC",
          expected="CZQX_GANDER_DOMESTIC",
      ),
      dict(
          testcase_name="empty_name",
          icao="EBBU",
          raw_name="",
          expected="EBBU",
      ),
  )
  def test_make_fir_id(self, icao, raw_name, expected):
    self.assertEqual(constants.make_fir_id(icao, raw_name), expected)

  def test_load_firs_geojson_has_no_pipeline_output_fallback(self):
    # load_firs_geojson must never fall back to a pipeline output
    # artifact (`fir_density_df_export_per_country.json`).
    with (
        mock.patch.object(
            os.path, "exists", return_value=False, autospec=True, spec_set=True
        ),
        mock.patch.object(
            metadata_loader.fs,
            "exists",
            return_value=False,
            autospec=True,
            spec_set=True,
        ),
    ):
      with self.assertRaisesRegex(RuntimeError, "No FIR boundaries") as ctx:
        metadata_loader.load_firs_geojson("")
      self.assertNotIn(
          "fir_density_df_export_per_country.json", str(ctx.exception)
      )

  def test_load_firs_geojson_accepts_a_year_named_directory(self):
    # Boundary files are chosen independently of the processed year, so a
    # year in the path (e.g. the ICAO 2015 FIR file) is not a constraint.
    os.makedirs(os.path.join(self.test_dir, "2015"))
    path = self._write_firs(
        os.path.join("2015", "firs.geojson"),
        [{
            "fir_icao": "EBBU",
            "firname": "FIR BRUXELLES",
            "fir_to_country_iso2s": "BE",
            "type": "domestic",
        }],
    )
    firs = metadata_loader.load_firs_geojson(path)
    self.assertLen(firs, 1)
    self.assertEqual(firs[0]["properties"]["fir_icao"], "EBBU")
    metadata_loader.validate_fir_features(firs, expected_count=1)

  def _resolve_bundled_firs_path(self) -> str | None:
    return next(
        (
            item
            for item in metadata_loader.fs.resolve_resource_path(
                "data_extraction/testdata/global_firs.geojson"
            )
            if os.path.exists(item)
        ),
        None,
    )

  def _fir_summary_tuple(self, feature):
    props = feature["properties"]
    return (
        props["fir_icao"],
        props["firname"],
        props["fir_to_country_iso2s"],
        props.get("areasqkm"),
    )

  def test_load_synthetic_fir_fixture_domestic_regions(self):
    bundled_path = self._resolve_bundled_firs_path()
    self.assertIsNotNone(bundled_path)
    firs = metadata_loader.load_firs_geojson(bundled_path)
    domestic = {
        f["properties"]["fir_id"]: self._fir_summary_tuple(f)
        for f in firs
        if f["properties"]["type"] == "domestic"
    }
    self.assertEqual(
        domestic,
        {
            "EDXX_TESTLAND_NORTH": ("EDXX", "TESTLAND NORTH", "DE", 1000.0),
            "GOXX_TESTLAND_WEST": (
                "GOXX",
                "TESTLAND WEST",
                "SN, ML, MR, CI, BF",
                None,
            ),
            "KZXX_TESTLAND_EAST": ("KZXX", "TESTLAND EAST", "US", 3000.0),
            "UUXX_TESTLAND_ARCTIC": ("UUXX", "TESTLAND ARCTIC", "RU", None),
        },
    )

  def test_load_synthetic_fir_fixture_international_and_validation(self):
    bundled_path = self._resolve_bundled_firs_path()
    self.assertIsNotNone(bundled_path)
    firs = metadata_loader.load_firs_geojson(bundled_path)
    international = {
        f["properties"]["fir_id"]: self._fir_summary_tuple(f)
        for f in firs
        if f["properties"]["type"] == "international"
    }
    self.assertEqual(
        international,
        {
            "LPXX_TESTLAND_OCEANIC": ("LPXX", "TESTLAND OCEANIC", "PT", 2000.0),
            "KZXX_TESTLAND_EAST_OCEANIC": (
                "KZXX",
                "TESTLAND EAST OCEANIC",
                "US",
                4000.0,
            ),
            "ZZXX_SHARED_REGION": ("ZZXX", "SHARED REGION", "FR", 500.0),
        },
    )
    metadata_loader.validate_fir_features(firs, expected_count=len(firs))

  def _valid_fir(self, icao="EDXX", name="TESTLAND", **overrides):
    properties = {
        "fir_id": constants.make_fir_id(icao, name),
        "fir_icao": icao,
        "firname": name,
        "fir_to_country_iso2s": "DE",
        "type": "domestic",
        "areasqkm": 10.0,
    }
    properties.update(overrides)
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]],
        },
    }

  def test_validate_fir_features_accepts_valid_and_optional_area(self):
    no_area = self._valid_fir("LPXX", "OTHER")
    del no_area["properties"]["areasqkm"]
    metadata_loader.validate_fir_features(
        [self._valid_fir(), no_area], expected_count=2
    )

  def test_validate_fir_features_zero_expected_count_disables_count_check(self):
    metadata_loader.validate_fir_features([self._valid_fir()])

  def test_validate_fir_features_rejects_empty_feature_list(self):
    with self.assertRaisesRegex(ValueError, "no features"):
      metadata_loader.validate_fir_features([])

  def test_validate_fir_features_rejects_expected_count_mismatch(self):
    with self.assertRaisesRegex(ValueError, "expected 266 FIR features, got 1"):
      metadata_loader.validate_fir_features(
          [self._valid_fir()], expected_count=266
      )

  def test_validate_fir_features_rejects_duplicate_fir_id(self):
    with self.assertRaisesRegex(ValueError, "duplicate fir_id"):
      metadata_loader.validate_fir_features(
          [self._valid_fir(), self._valid_fir()]
      )

  @parameterized.named_parameters(
      dict(
          testcase_name="empty_fir_id",
          prop_key="fir_id",
          prop_value="",
          expected_message="empty fir_id",
      ),
      dict(
          testcase_name="empty_fir_icao",
          prop_key="fir_icao",
          prop_value="",
          expected_message="empty fir_icao",
      ),
      dict(
          testcase_name="empty_firname",
          prop_key="firname",
          prop_value="",
          expected_message="empty firname",
      ),
      dict(
          testcase_name="uncleaned_firname",
          prop_key="firname",
          prop_value="FIR TESTLAND",
          expected_message="firname not cleaned",
      ),
      dict(
          testcase_name="empty_country_mapping",
          prop_key="fir_to_country_iso2s",
          prop_value="",
          expected_message="no country mapping",
      ),
      dict(
          testcase_name="invalid_type",
          prop_key="type",
          prop_value="oceanic",
          expected_message="invalid type",
      ),
      dict(
          testcase_name="zero_areasqkm",
          prop_key="areasqkm",
          prop_value=0.0,
          expected_message="non-positive areasqkm",
      ),
      dict(
          testcase_name="non_numeric_areasqkm",
          prop_key="areasqkm",
          prop_value="n/a",
          expected_message="non-positive areasqkm",
      ),
  )
  def test_validate_fir_features_rejects_invalid_property(
      self, prop_key, prop_value, expected_message
  ):
    feat = self._valid_fir()
    feat["properties"][prop_key] = prop_value
    with self.assertRaisesRegex(ValueError, expected_message):
      metadata_loader.validate_fir_features([feat])

  def test_validate_fir_features_rejects_non_polygon_geometry(self):
    bad_geometry = self._valid_fir()
    bad_geometry["geometry"] = {"type": "Point", "coordinates": [0, 0]}
    with self.assertRaisesRegex(ValueError, "geometry is not a non-empty"):
      metadata_loader.validate_fir_features([bad_geometry])

  def test_validate_fir_features_caps_reported_problems(self):
    feats = [self._valid_fir(f"E{i:03d}", type="") for i in range(30)]
    with self.assertRaisesRegex(
        ValueError, r"failed 30 integrity check\(s\).*and 10 more"
    ):
      metadata_loader.validate_fir_features(feats)

  def test_load_boundaries_country_index_and_metadata(self):
    boundaries = metadata_loader.load_boundaries(
        firs_path=self.firs_geojson,
        country_geojson_path=self.country_geojson,
        expected_fir_count=1,
    )
    self.assertLen(boundaries.country_features, 1)
    self.assertEqual(boundaries.country_index.count, 1)
    self.assertEqual(boundaries.country_names, {"BE": "Belgium"})
    self.assertIn("BE", boundaries.country_areas)
    self.assertGreater(boundaries.country_areas["BE"], 0.0)

  def test_load_boundaries_fir_index_and_areas(self):
    boundaries = metadata_loader.load_boundaries(
        firs_path=self.firs_geojson,
        country_geojson_path=self.country_geojson,
        expected_fir_count=1,
    )
    self.assertLen(boundaries.fir_features, 1)
    self.assertEqual(boundaries.fir_index.count, 1)
    self.assertIn(("BRUXELLES", "EBBU"), boundaries.fir_areas)
    self.assertGreater(boundaries.fir_areas[("BRUXELLES", "EBBU")], 0.0)

  def test_load_boundaries_expected_fir_count_mismatch_raises(self):
    with self.assertRaisesRegex(ValueError, "expected 5 FIR features, got 1"):
      metadata_loader.load_boundaries(
          firs_path=self.firs_geojson,
          country_geojson_path=self.country_geojson,
          expected_fir_count=5,
      )

  def test_load_boundaries_empty_country_raises(self):
    empty_country = os.path.join(self.test_dir, "empty_countries.geojson")
    with open(empty_country, "w", encoding="utf-8") as f:
      json.dump({"type": "FeatureCollection", "features": []}, f)
    with self.assertRaisesRegex(
        RuntimeError, "Failed to load non-empty country GeoJSON features"
    ):
      metadata_loader.load_boundaries(
          firs_path=self.firs_geojson,
          country_geojson_path=empty_country,
      )

  def test_load_boundaries_zero_polygons_country_spatial_index_raises(self):
    country_file = os.path.join(self.test_dir, "invalid_poly_countries.geojson")
    with open(country_file, "w", encoding="utf-8") as f:
      json.dump(
          {
              "type": "FeatureCollection",
              "features": [{
                  "type": "Feature",
                  "properties": {"name": "EmptyCountry", "iso_a2": "EC"},
                  "geometry": {"type": "Polygon", "coordinates": []},
              }],
          },
          f,
      )
    with self.assertRaisesRegex(
        RuntimeError,
        "Country SpatialIndex initialized with 0 polygons; refusing to run",
    ):
      metadata_loader.load_boundaries(
          firs_path=self.firs_geojson,
          country_geojson_path=country_file,
      )

  def test_load_boundaries_empty_fir_raises(self):
    empty_fir = os.path.join(self.test_dir, "empty_firs.geojson")
    with open(empty_fir, "w", encoding="utf-8") as f:
      json.dump({"type": "FeatureCollection", "features": []}, f)
    with self.assertRaisesRegex(
        RuntimeError, "Failed to load non-empty FIR GeoJSON features"
    ):
      metadata_loader.load_boundaries(
          firs_path=empty_fir,
          country_geojson_path=self.country_geojson,
      )

  def test_load_boundaries_invalid_fir_raises(self):
    bad_fir = os.path.join(self.test_dir, "bad_firs.geojson")
    with open(bad_fir, "w", encoding="utf-8") as f:
      json.dump(
          {
              "type": "FeatureCollection",
              "features": [{
                  "type": "Feature",
                  "properties": {
                      "firname": "FIR BRUXELLES",
                      "fir_icao": "EBBU",
                      "fir_to_country_iso2s": "BE",
                      "type": "invalid_type",
                  },
                  "geometry": {
                      "type": "Polygon",
                      "coordinates": [[
                          [2.0, 49.0],
                          [6.0, 49.0],
                          [6.0, 52.0],
                          [2.0, 52.0],
                          [2.0, 49.0],
                      ]],
                  },
              }],
          },
          f,
      )
    with self.assertRaisesRegex(ValueError, "invalid type 'invalid_type'"):
      metadata_loader.load_boundaries(
          firs_path=bad_fir,
          country_geojson_path=self.country_geojson,
      )


if __name__ == "__main__":
  absltest.main()
