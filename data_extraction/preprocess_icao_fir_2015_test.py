"""Unit tests for preprocess_icao_fir_2015."""

import json
import os
import struct
import types
from typing import Any
from unittest import mock

from absl import flags
from absl.testing import absltest
from absl.testing import parameterized

from data_extraction import metadata_loader
from data_extraction import preprocess_icao_fir_2015 as converter

# Clockwise unit square: an outer ring in ESRI winding order.
_SQUARE = types.MappingProxyType({
    "parts": (0,),
    "points": ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)),
})


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


class ResolveCountryTest(parameterized.TestCase):

  @parameterized.named_parameters(
      dict(
          testcase_name="oceanic_spain_lppo",
          country_so="Oceanic Spain",
          f2letter="LP",
          icao="LPPO",
          expected="PT",
      ),
      dict(
          testcase_name="oceanic_ireland_eggx",
          country_so="Oceanic Ireland",
          f2letter="EG",
          icao="EGGX",
          expected="GB, IE",
      ),
      dict(
          testcase_name="cocesna_mhtg",
          country_so="COCESNA",
          f2letter="MH",
          icao="MHTG",
          expected="GT, SV, HN, NI, CR, BZ",
      ),
      dict(
          testcase_name="roberts_glrb",
          country_so="Senegal",
          f2letter="GL",
          icao="GLRB",
          expected="LR, SL, GN",
      ),
      dict(
          testcase_name="russian_federation_umkk",
          country_so="Russian Federation",
          f2letter="UM",
          icao="UMKK",
          expected="RU",
      ),
      dict(
          testcase_name="turkmenistan_utaa",
          country_so="Turkmenistan",
          f2letter="UT",
          icao="UTAA",
          expected="TM",
      ),
      dict(
          testcase_name="unknown_country",
          country_so="Nowhere",
          f2letter="QQ",
          icao="QQQQ",
          expected="",
      ),
  )
  def test_resolve_country_so_to_iso2(
      self, country_so, f2letter, icao, expected
  ):
    self.assertEqual(
        converter.resolve_country_so_to_iso2(country_so, f2letter, icao),
        expected,
    )

  @parameterized.named_parameters(
      dict(
          testcase_name="oceanic_country_so",
          country_so="Oceanic Portugal",
          firname="SANTA MARIA",
          expected="international",
      ),
      dict(
          testcase_name="arctic_firname",
          country_so="Russian Federation",
          firname="ARCTIC",
          expected="international",
      ),
      dict(
          testcase_name="domestic_germany",
          country_so="Germany",
          firname="LANGEN",
          expected="domestic",
      ),
  )
  def test_derive_fir_type(self, country_so, firname, expected):
    self.assertEqual(converter.derive_fir_type(country_so, firname), expected)


class ConvertShapefileTest(absltest.TestCase):

  def _convert_sample_shapefile(self) -> dict[str, Any]:
    """Converts a stubbed 3-record shapefile into a GeoJSON dict."""
    records = [
        {
            "ICAOCODE": "EDGG",
            "FIR_NAME": "FIR LANGEN",
            "Country_So": "Germany",
            "F2LETTER": "ED",
            "SQKMgeod": 100.0,
        },
        {
            "ICAOCODE": "LPPO",
            "FIR_NAME": "SANTA MARIA OCEANIC",
            "Country_So": "Oceanic Portugal",
            "F2LETTER": "LP",
            "SQKMgeod": 200.0,
        },
        # Blank ICAO rows are dropped.
        {"ICAOCODE": "", "FIR_NAME": "NOTHING", "SQKMgeod": 1.0},
    ]
    with (
        mock.patch.object(
            converter,
            "read_dbf",
            return_value=records,
            autospec=True,
            spec_set=True,
        ),
        mock.patch.object(
            converter,
            "read_shp",
            return_value=[_SQUARE] * len(records),
            autospec=True,
            spec_set=True,
        ),
    ):
      return converter.convert_shapefile_to_geojson("x.shp", "x.dbf")

  def test_convert_shapefile_populates_canonical_properties(self):
    geojson = self._convert_sample_shapefile()
    features = geojson["features"]
    by_id = {f["properties"]["fir_id"]: f["properties"] for f in features}
    self.assertCountEqual(by_id, ["EDGG_LANGEN", "LPPO_SANTA_MARIA_OCEANIC"])
    self.assertEqual(by_id["EDGG_LANGEN"]["fir_to_country_iso2s"], "DE")
    self.assertEqual(by_id["EDGG_LANGEN"]["type"], "domestic")
    self.assertEqual(by_id["EDGG_LANGEN"]["areasqkm"], 100.0)
    self.assertEqual(
        (
            by_id["LPPO_SANTA_MARIA_OCEANIC"]["fir_to_country_iso2s"],
            by_id["LPPO_SANTA_MARIA_OCEANIC"]["type"],
        ),
        ("PT", "international"),
    )

  def test_convert_shapefile_omits_legacy_property_aliases(self):
    geojson = self._convert_sample_shapefile()
    features = geojson["features"]
    by_id = {f["properties"]["fir_id"]: f["properties"] for f in features}
    self.assertNotIn("icaocode", by_id["EDGG_LANGEN"])
    self.assertNotIn("area_sq_km", by_id["EDGG_LANGEN"])
    self.assertNotIn("icaocode", by_id["LPPO_SANTA_MARIA_OCEANIC"])
    self.assertNotIn("area_sq_km", by_id["LPPO_SANTA_MARIA_OCEANIC"])

  def test_convert_shapefile_output_passes_loader_validation(self):
    geojson = self._convert_sample_shapefile()
    path = os.path.join(self.create_tempdir().full_path, "firs.geojson")
    with open(path, "w", encoding="utf-8") as f:
      json.dump(geojson, f)
    loaded = metadata_loader.load_firs_geojson(path)
    metadata_loader.validate_fir_features(loaded, expected_count=2)
    self.assertLen(loaded, 2)

  def test_convert_shapefile_skips_groups_with_only_empty_shapes(self):
    records = [
        {"ICAOCODE": "AAAA", "FIR_NAME": "EMPTY", "SQKMgeod": 10.0},
        {
            "ICAOCODE": "EDGG",
            "FIR_NAME": "FIR LANGEN",
            "Country_So": "Germany",
            "F2LETTER": "ED",
            "SQKMgeod": 100.0,
        },
    ]
    shapes = [{"parts": [], "points": []}, _SQUARE]
    with (
        mock.patch.object(
            converter,
            "read_dbf",
            return_value=records,
            autospec=True,
            spec_set=True,
        ),
        mock.patch.object(
            converter,
            "read_shp",
            return_value=shapes,
            autospec=True,
            spec_set=True,
        ),
    ):
      geojson = converter.convert_shapefile_to_geojson("x.shp", "x.dbf")
    self.assertLen(geojson["features"], 1)
    self.assertEqual(geojson["features"][0]["properties"]["fid"], 1)
    self.assertEqual(
        geojson["features"][0]["properties"]["fir_id"], "EDGG_LANGEN"
    )


class GeometryAndShapeTest(parameterized.TestCase):

  def test_signed_ring_area_lonlat_winding_sign(self):
    # Standard clockwise square: area > 0
    cw = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)]
    self.assertGreater(converter.signed_ring_area_lonlat(cw), 0.0)

    # Standard counter-clockwise square: area < 0
    ccw = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]
    self.assertLess(converter.signed_ring_area_lonlat(ccw), 0.0)

  def test_signed_ring_area_lonlat_closes_unclosed_ring(self):
    unclosed_cw = [(0.0, 10.0), (0.0, 20.0), (10.0, 20.0), (10.0, 10.0)]
    self.assertAlmostEqual(
        converter.signed_ring_area_lonlat(unclosed_cw), 100.0, delta=1e-5
    )

  def test_signed_ring_area_lonlat_antimeridian_unwrap(self):
    # Antimeridian-crossing clockwise outer ring
    # (178, -2) -> (178, 2) -> (-178, 2) -> (-178, -2) -> (178, -2).
    # Without unwrap, shoelace evaluates to ~ -1424.
    # With unwrap, it evaluates to +16.0 (clockwise).
    antimeridian_cw = [
        (178.0, -2.0),
        (178.0, 2.0),
        (-178.0, 2.0),
        (-178.0, -2.0),
        (178.0, -2.0),
    ]
    area = converter.signed_ring_area_lonlat(antimeridian_cw)
    self.assertAlmostEqual(area, 16.0, delta=1e-5)

  @parameterized.named_parameters(
      dict(testcase_name="empty_ring", ring=[]),
      dict(testcase_name="two_points", ring=[(0.0, 0.0), (1.0, 1.0)]),
  )
  def test_signed_ring_area_lonlat_degenerate_returns_zero(self, ring):
    self.assertEqual(converter.signed_ring_area_lonlat(ring), 0.0)

  def test_shape_to_polygons_antimeridian_outer_ring(self):
    part1 = [(10.0, 0.0), (10.0, 1.0), (11.0, 1.0), (11.0, 0.0), (10.0, 0.0)]
    part2 = [
        (178.0, -2.0),
        (178.0, 2.0),
        (-178.0, 2.0),
        (-178.0, -2.0),
        (178.0, -2.0),
    ]
    shape = {"parts": [0, len(part1)], "points": part1 + part2}
    polygons = converter.shape_to_polygons(shape, tol_meters=0.0)
    # Both parts are outer rings: must return 2 distinct Polygons.
    self.assertLen(polygons, 2)
    self.assertLen(polygons[0], 1)
    self.assertLen(polygons[1], 1)

  def test_shape_to_polygons_hole_order_independence(self):
    # Outer A at [0, 10] x [0, 10]
    outer_a = [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0), (0.0, 0.0)]
    # Outer B at [20, 30] x [0, 10]
    outer_b = [
        (20.0, 0.0),
        (20.0, 10.0),
        (30.0, 10.0),
        (30.0, 0.0),
        (20.0, 0.0),
    ]
    # Hole belonging to Outer A whose first vertex touches Outer A's boundary
    # (0.0, 5.0), placed AFTER Outer B in the parts list.
    hole_a = [(0.0, 5.0), (8.0, 2.0), (8.0, 8.0), (2.0, 8.0), (0.0, 5.0)]
    shape = {"parts": [0, 5, 10], "points": outer_a + outer_b + hole_a}
    polygons = converter.shape_to_polygons(shape, tol_meters=0.0)
    self.assertLen(polygons, 2)
    # The hole must be attached to outer_a (polys[0]), not outer_b (polys[1])
    self.assertLen(polygons[0], 2)
    self.assertLen(polygons[1], 1)

  def test_shape_to_polygons_assigns_holes_to_innermost_nested_outer_ring(self):
    outer_large = [
        (0.0, 0.0),
        (0.0, 20.0),
        (20.0, 20.0),
        (20.0, 0.0),
        (0.0, 0.0),
    ]
    hole_large = [
        (2.0, 2.0),
        (18.0, 2.0),
        (18.0, 18.0),
        (2.0, 18.0),
        (2.0, 2.0),
    ]
    outer_island = [
        (4.0, 4.0),
        (4.0, 16.0),
        (16.0, 16.0),
        (16.0, 4.0),
        (4.0, 4.0),
    ]
    hole_island = [
        (6.0, 6.0),
        (14.0, 6.0),
        (14.0, 14.0),
        (6.0, 14.0),
        (6.0, 6.0),
    ]
    shape = {
        "parts": [0, 5, 10, 15],
        "points": outer_large + hole_large + outer_island + hole_island,
    }
    polygons = converter.shape_to_polygons(shape, tol_meters=0.0)
    self.assertLen(polygons, 2)
    self.assertLen(polygons[0], 2)
    self.assertLen(polygons[1], 2)
    self.assertEqual(
        (polygons[0][1][0], polygons[1][1][0]), ([2.0, 2.0], [6.0, 6.0])
    )

  def test_shape_to_polygons_ignores_orphan_hole_outside_all_outers(self):
    outer_a = [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0), (0.0, 0.0)]
    orphan_hole = [
        (50.0, 50.0),
        (55.0, 50.0),
        (55.0, 55.0),
        (50.0, 55.0),
        (50.0, 50.0),
    ]
    shape = {"parts": [0, 5], "points": outer_a + orphan_hole}
    polygons = converter.shape_to_polygons(shape, tol_meters=0.0)
    self.assertLen(polygons, 1)
    self.assertLen(polygons[0], 1)

  def test_shape_to_polygons_assigns_hole_to_pinched_self_touching_outer(self):
    # Clockwise bow-tie with a larger left lobe [0, 10]x[0, 10] and a smaller
    # clockwise right lobe [10, 14]x[3, 7] meeting at (10, 5), which is invalid
    # in raw Shapely until repaired by make_valid.
    pinched_outer = [
        (0.0, 0.0),
        (0.0, 10.0),
        (10.0, 10.0),
        (10.0, 5.0),
        (10.0, 7.0),
        (14.0, 7.0),
        (14.0, 3.0),
        (10.0, 3.0),
        (10.0, 5.0),
        (10.0, 0.0),
        (0.0, 0.0),
    ]
    hole = [(2.0, 2.0), (8.0, 2.0), (8.0, 8.0), (2.0, 8.0), (2.0, 2.0)]
    shape = {"parts": [0, len(pinched_outer)], "points": pinched_outer + hole}
    polygons = converter.shape_to_polygons(shape, tol_meters=0.0)
    self.assertLen(polygons, 1)
    self.assertLen(polygons[0], 2)
    self.assertEqual(polygons[0][1][0], [2.0, 2.0])

  @parameterized.named_parameters(
      dict(testcase_name="empty_dict", shape={}),
      dict(
          testcase_name="empty_parts_and_points",
          shape={"parts": [], "points": []},
      ),
  )
  def test_shape_to_polygons_null_or_empty(self, shape):
    self.assertEmpty(converter.shape_to_polygons(shape))

  def test_simplify_closed_ring(self):
    # Collinear points along the axes should be simplified
    ring = [
        (0.0, 0.0),
        (0.0, 0.5),
        (0.0, 1.0),
        (1.0, 1.0),
        (1.0, 0.0),
        (0.5, 0.0),
        (0.0, 0.0),
    ]
    simplified = converter.simplify_closed_ring(ring, tol_meters=500.0)
    # Redundant points are removed, but the ring remains closed.
    self.assertLen(simplified, 5)
    self.assertEqual(simplified[0], simplified[-1])

  def test_simplify_closed_ring_closes_unclosed_rings(self):
    unclosed_short = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
    closed_zero_tol = converter.simplify_closed_ring(
        unclosed_short, tol_meters=0.0
    )
    unclosed_six = [
        (0.0, 0.0),
        (0.0, 0.5),
        (0.0, 1.0),
        (1.0, 1.0),
        (1.0, 0.0),
        (0.5, 0.0),
    ]
    closed_rdp = converter.simplify_closed_ring(unclosed_six, tol_meters=500.0)
    self.assertEqual(closed_zero_tol[0], closed_zero_tol[-1])
    self.assertLen(closed_zero_tol, 5)
    self.assertEqual(closed_rdp[0], closed_rdp[-1])
    self.assertLen(closed_rdp, 5)

  @parameterized.named_parameters(
      dict(testcase_name="empty_ring", ring=[]),
      dict(testcase_name="single_point", ring=[(0.0, 0.0)]),
      dict(testcase_name="two_points", ring=[(0.0, 0.0), (1.0, 1.0)]),
      dict(
          testcase_name="three_point_closed_segment",
          ring=[(0.0, 0.0), (1.0, 1.0), (0.0, 0.0)],
      ),
  )
  def test_simplify_closed_ring_degenerate_returns_empty(self, ring):
    self.assertEmpty(converter.simplify_closed_ring(ring, tol_meters=0.0))

  def test_simplify_closed_ring_preserves_non_collinear_antimeridian_vertex(
      self,
  ):
    # Without longitude unwrapping, the wrapped chord from (179, 0) to
    # (-179, 10) spans -358 deg in lon, placing (178, 0.02815) within 500 m of
    # the false chord (10 * 1/358 ~= 0.02793 deg) even though it deviates by
    # >111 km from the true unwrapped chord from (179, 0) to (181, 10).
    ring = [
        (179.0, 0.0),
        (178.0, 0.02815),
        (-179.0, 10.0),
        (-179.0, 0.0),
        (179.0, -5.0),
        (179.0, 0.0),
    ]
    simplified = converter.simplify_closed_ring(ring, tol_meters=500.0)
    self.assertIn([178.0, 0.02815], simplified)
    self.assertLen(simplified, 6)

  def test_simplify_closed_ring_drops_collinear_antimeridian_vertices(self):
    ring = [
        (179.0, 45.0),
        (179.5, 45.0),
        (-179.5, 45.0),
        (-179.0, 45.0),
        (-179.0, 50.0),
        (179.0, 50.0),
        (179.0, 45.0),
    ]
    simplified = converter.simplify_closed_ring(ring, tol_meters=500.0)
    self.assertEqual(
        simplified,
        [
            [179.0, 45.0],
            [-179.0, 45.0],
            [-179.0, 50.0],
            [179.0, 50.0],
            [179.0, 45.0],
        ],
    )

  def test_simplify_closed_ring_shared_border_is_independent_of_start_lon(self):
    west_ring = [
        (90.0, 40.0),
        (100.0, 40.0),
        (102.0, 42.02),
        (104.0, 44.0),
        (104.0, 50.0),
        (90.0, 50.0),
        (90.0, 40.0),
    ]
    east_ring = [
        (130.0, 40.0),
        (100.0, 40.0),
        (102.0, 42.02),
        (104.0, 44.0),
        (104.0, 50.0),
        (130.0, 50.0),
        (130.0, 40.0),
    ]
    west_simplified = converter.simplify_closed_ring(
        west_ring, tol_meters=3000.0
    )
    east_simplified = converter.simplify_closed_ring(
        east_ring, tol_meters=3000.0
    )
    self.assertIn([102.0, 42.02], west_simplified)
    self.assertIn([102.0, 42.02], east_simplified)


def _encode_dbf_cell(
    field_spec: tuple[str, str, int, int], value: str | float
) -> bytes:
  """Encodes a single DBF field value according to its field specification."""
  _, field_type, length, dec = field_spec
  if field_type == "N":
    return f"{float(value):>{length}.{dec}f}".encode("ascii")
  return str(value).encode("cp1252").ljust(length, b" ")


def _build_shp_header(*, file_code: int = 9994, shape_type: int = 5) -> bytes:
  """Builds a 100-byte ESRI Shapefile header."""
  return struct.pack(">i28x", file_code) + struct.pack("<i64x", shape_type)


def _pack_shp_record(rec_num: int, payload: bytes) -> bytes:
  """Packs a big-endian 8-byte SHP record header and its payload."""
  return struct.pack(">2i", rec_num, len(payload) // 2) + payload


def _build_polygon_shp_payload() -> bytes:
  """Builds a binary Type-5 Polygon SHP record payload for a unit square."""
  points = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)]
  header = struct.pack("<i4d2i", 5, 0.0, 0.0, 1.0, 1.0, 1, len(points))
  parts = struct.pack("<i", 0)
  coords = b"".join(struct.pack("<2d", x, y) for x, y in points)
  return header + parts + coords


class ShapefileReaderTest(parameterized.TestCase):

  def _write_sample_dbf(self) -> str:
    """Writes a sample DBF file with one active and one deleted record."""
    fields = [
        ("ICAOCODE", "C", 4, 0),
        ("FIR_NAME", "C", 20, 0),
        ("SQKMgeod", "N", 10, 2),
    ]
    rec_len = 1 + sum(f[2] for f in fields)
    hdr_len = 32 + 32 * len(fields) + 1
    rows = [
        (b" ", ("SKED", "BOGOTÁ", 1.5)),
        (b"*", ("XXXX", "DELETED", 2.0)),
    ]

    dbf_path = os.path.join(self.create_tempdir().full_path, "test.dbf")
    with open(dbf_path, "wb") as f:
      f.write(
          struct.pack("<B3BIHH20x", 3, 26, 1, 1, len(rows), hdr_len, rec_len)
      )
      for name, field_type, ln, dec in fields:
        f.write(
            struct.pack(
                "<11sc4xBB14x",
                name.encode("ascii"),
                field_type.encode("ascii"),
                ln,
                dec,
            )
        )
      f.write(b"\x0d")
      for status_byte, row in rows:
        f.write(status_byte)
        f.write(b"".join(_encode_dbf_cell(s, v) for s, v in zip(fields, row)))
      f.write(b"\x1a")
    return dbf_path

  def test_read_dbf_skips_deleted_records_by_default(self):
    dbf_path = self._write_sample_dbf()
    records = converter.read_dbf(dbf_path)
    self.assertLen(records, 1)
    self.assertEqual(records[0]["ICAOCODE"], "SKED")
    self.assertEqual(records[0]["FIR_NAME"], "BOGOTÁ")
    self.assertEqual(records[0]["SQKMgeod"], 1.5)

  def test_read_dbf_keeps_deleted_placeholders_when_requested(self):
    dbf_path = self._write_sample_dbf()
    recs_with_placeholders = converter.read_dbf(
        dbf_path, keep_deleted_placeholders=True
    )
    self.assertLen(recs_with_placeholders, 2)
    self.assertEmpty(recs_with_placeholders[1])

  @parameterized.named_parameters(
      dict(
          testcase_name="truncated_header",
          raw_bytes=b"\x00" * 16,
          expected_regex=r"Truncated DBF header",
      ),
      dict(
          testcase_name="invalid_header_metadata",
          raw_bytes=struct.pack("<4xIHH20x", 1, 16, 0),
          expected_regex=r"Invalid DBF header metadata",
      ),
      dict(
          testcase_name="truncated_field_descriptor",
          raw_bytes=struct.pack("<4xIHH20x", 1, 65, 10) + b"SHORT_DESC",
          expected_regex=r"Truncated DBF field descriptor",
      ),
      dict(
          testcase_name="truncated_field_descriptor_at_boundary",
          raw_bytes=struct.pack("<4xIHH20x", 1, 65, 10),
          expected_regex=r"Truncated DBF field descriptor",
      ),
      dict(
          testcase_name="truncated_record",
          raw_bytes=struct.pack("<4xIHH20x", 1, 33, 10) + b"\x0dshort",
          expected_regex=r"Truncated DBF record",
      ),
  )
  def test_read_dbf_rejects_corrupt_headers(
      self, raw_bytes: bytes, expected_regex: str
  ):
    dbf_path = os.path.join(self.create_tempdir().full_path, "corrupt.dbf")
    with open(dbf_path, "wb") as f:
      f.write(raw_bytes)
    with self.assertRaisesRegex(ValueError, expected_regex):
      converter.read_dbf(dbf_path)

  def test_read_shp_parses_null_and_polygon_records(self):
    shp_path = os.path.join(self.create_tempdir().full_path, "valid.shp")
    null_record = _pack_shp_record(1, struct.pack("<i", 0))
    poly_record = _pack_shp_record(2, _build_polygon_shp_payload())
    with open(shp_path, "wb") as f:
      f.write(_build_shp_header() + null_record + poly_record)
    shapes = converter.read_shp(shp_path)
    self.assertLen(shapes, 2)
    self.assertEqual(shapes[0], {"parts": [], "points": []})
    self.assertEqual(shapes[1]["parts"], [0])
    self.assertLen(shapes[1]["points"], 5)

  @parameterized.named_parameters(
      dict(
          testcase_name="truncated_file_header",
          raw_bytes=b"\x00" * 50,
          expected_regex=r"Truncated shapefile header",
      ),
      dict(
          testcase_name="invalid_file_code",
          raw_bytes=_build_shp_header(file_code=1234),
          expected_regex=r"Invalid shapefile file code 1234",
      ),
      dict(
          testcase_name="unsupported_header_shape_type",
          raw_bytes=_build_shp_header(shape_type=1),
          expected_regex=r"Unsupported shapefile header shape_type 1",
      ),
      dict(
          testcase_name="truncated_record_header",
          raw_bytes=_build_shp_header() + b"\x00" * 4,
          expected_regex=r"Truncated shapefile record header",
      ),
      dict(
          testcase_name="invalid_record_number",
          raw_bytes=_build_shp_header() + struct.pack(">2i", 0, 2),
          expected_regex=r"Invalid shapefile record header",
      ),
      dict(
          testcase_name="invalid_record_content_length",
          raw_bytes=_build_shp_header() + struct.pack(">2i", 1, 1),
          expected_regex=r"Invalid shapefile record header",
      ),
      dict(
          testcase_name="truncated_record_payload",
          raw_bytes=(
              _build_shp_header()
              + struct.pack(">2i", 1, 4)
              + struct.pack("<i", 5)
          ),
          expected_regex=r"Truncated shapefile record payload",
      ),
      dict(
          testcase_name="unsupported_shape_type",
          raw_bytes=(
              _build_shp_header()
              + _pack_shp_record(1, struct.pack("<i2d", 1, 0.0, 0.0))
          ),
          expected_regex=r"Unsupported shapefile shape_type 1",
      ),
      dict(
          testcase_name="truncated_polygon_header",
          raw_bytes=(
              _build_shp_header()
              + _pack_shp_record(1, struct.pack("<i", 5) + b"\x00" * 20)
          ),
          expected_regex=r"Truncated polygon record header",
      ),
      dict(
          testcase_name="negative_polygon_counts",
          raw_bytes=(
              _build_shp_header()
              + _pack_shp_record(
                  1, struct.pack("<i4d2i", 5, 0.0, 0.0, 1.0, 1.0, -1, 5)
              )
          ),
          expected_regex=r"Invalid polygon counts",
      ),
      dict(
          testcase_name="truncated_polygon_coordinates",
          raw_bytes=(
              _build_shp_header()
              + _pack_shp_record(
                  1,
                  struct.pack("<i4d2i", 5, 0.0, 0.0, 1.0, 1.0, 1, 5)
                  + struct.pack("<i", 0)
                  + b"\x00" * 16,
              )
          ),
          expected_regex=r"Truncated polygon coordinates",
      ),
      dict(
          testcase_name="invalid_part_offsets",
          raw_bytes=(
              _build_shp_header()
              + _pack_shp_record(
                  1,
                  struct.pack("<i4d2i", 5, 0.0, 0.0, 1.0, 1.0, 2, 2)
                  + struct.pack("<2i", 0, 2)
                  + b"\x00" * 32,
              )
          ),
          expected_regex=r"Invalid polygon part offsets",
      ),
  )
  def test_read_shp_rejects_corrupt_or_unsupported_records(
      self, raw_bytes: bytes, expected_regex: str
  ):
    shp_path = os.path.join(self.create_tempdir().full_path, "corrupt.shp")
    with open(shp_path, "wb") as f:
      f.write(raw_bytes)
    with self.assertRaisesRegex(ValueError, expected_regex):
      converter.read_shp(shp_path)

  def test_convert_shapefile_record_count_mismatch_raises(self):
    records = [{
        "ICAOCODE": "AAAA",
        "FIR_NAME": "A",
        "Country_So": "DE",
        "SQKMgeod": 1.0,
    }]
    with (
        mock.patch.object(
            converter,
            "read_dbf",
            return_value=records,
            autospec=True,
            spec_set=True,
        ),
        mock.patch.object(
            converter, "read_shp", return_value=[], autospec=True, spec_set=True
        ),
    ):
      with self.assertRaisesRegex(
          ValueError,
          r"Record count mismatch between DBF \(\d+\) and SHP \(\d+\)",
      ):
        converter.convert_shapefile_to_geojson("x.shp", "x.dbf")

  def test_main_insufficient_args_raises_system_exit(self):
    with self.assertRaisesRegex(
        SystemExit, r"Usage: preprocess_icao_fir_2015\.py"
    ):
      converter.main(["preprocess_icao_fir_2015.py", "x.shp"])

  def test_main_runs_conversion_and_logs(self):
    out_dir = self.create_tempdir().full_path
    out_path = os.path.join(out_dir, "output.geojson")
    dummy_geojson = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"fir_id": "TEST"}}],
    }
    with mock.patch.object(
        converter,
        "convert_shapefile_to_geojson",
        return_value=dummy_geojson,
        autospec=True,
        spec_set=True,
    ):
      converter.main(
          ["preprocess_icao_fir_2015.py", "dummy.shp", "dummy.dbf", out_path]
      )

    self.assertTrue(os.path.exists(out_path))
    with open(out_path, "r", encoding="utf-8") as f:
      loaded = json.load(f)
    self.assertEqual(loaded, dummy_geojson)


if __name__ == "__main__":
  absltest.main()
