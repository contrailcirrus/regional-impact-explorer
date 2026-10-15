"""Unit tests for geo_utils."""

from unittest import mock

from absl import flags
from absl.testing import absltest
from absl.testing import parameterized
import numpy as np
import shapely
import shapely.geometry
import shapely.ops

from data_extraction import geo_utils
from data_extraction import test_fixtures


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


def _straddler_feature() -> dict[str, object]:
  """A 4-degree-wide box straddling 180, from 178E to 178W at equator."""
  return {
      "type": "Feature",
      "properties": {"iso_a2": "XX", "name": "STRADDLER"},
      "geometry": {
          "type": "Polygon",
          "coordinates": [[
              [178.0, -2.0],
              [-178.0, -2.0],
              [-178.0, 2.0],
              [178.0, 2.0],
              [178.0, -2.0],
          ]],
      },
  }


def _antimeridian_shell_with_non_crossing_holes_feature(
    *, include_holes: bool = True
) -> dict[str, object]:
  """Builds an antimeridian-spanning shell with optional non-crossing holes."""
  shell = [
      [170.0, -10.0],
      [-170.0, -10.0],
      [-170.0, 10.0],
      [170.0, 10.0],
      [170.0, -10.0],
  ]
  hole_neg = [
      [-176.0, -2.0],
      [-176.0, 2.0],
      [-172.0, 2.0],
      [-172.0, -2.0],
      [-176.0, -2.0],
  ]
  hole_pos = [
      [172.0, -2.0],
      [172.0, 2.0],
      [176.0, 2.0],
      [176.0, -2.0],
      [172.0, -2.0],
  ]
  coords = [shell, hole_neg, hole_pos] if include_holes else [shell]
  return {
      "type": "Feature",
      "properties": {"iso_a2": "XX", "name": "NON_CROSSING_HOLES"},
      "geometry": {"type": "Polygon", "coordinates": coords},
  }


def _belgium_and_germany_index() -> geo_utils.SpatialIndex:
  feats = [
      test_fixtures.make_country_feature(
          "Belgium",
          "BE",
          [[2.0, 49.0], [6.0, 49.0], [6.0, 52.0], [2.0, 52.0], [2.0, 49.0]],
      ),
      test_fixtures.make_country_feature(
          "Germany",
          "DE",
          [[6.0, 49.0], [14.0, 49.0], [14.0, 55.0], [6.0, 55.0], [6.0, 49.0]],
      ),
  ]
  return geo_utils.SpatialIndex(feats, id_key="iso_a2", name_key="name")


def _query_single_line(
    spatial_index: geo_utils.SpatialIndex, line_proj: shapely.Geometry
) -> list[tuple[dict[str, object], float]]:
  lines = np.array([line_proj], dtype=object)
  lengths = np.array([line_proj.length], dtype=np.float64)
  _, poly_ids, proportions = spatial_index.query_intersections_batch(
      lines, lengths
  )
  return [
      (spatial_index.metadata[int(poly_id)], float(prop))
      for poly_id, prop in zip(poly_ids, proportions, strict=True)
  ]


class GeoUtilsTest(parameterized.TestCase):

  def test_haversine_distance_equator_one_degree(self):
    dist = geo_utils.haversine_meters(
        np.array([0.0]), np.array([0.0]), np.array([1.0]), np.array([0.0])
    )
    self.assertAlmostEqual(float(dist[0]), 111319.5, delta=500.0)

  def test_haversine_distance_identical_points_returns_zero(self):
    dist_zero = geo_utils.haversine_meters(
        np.array([10.0]), np.array([20.0]), np.array([10.0]), np.array([20.0])
    )
    self.assertEqual(float(dist_zero[0]), 0.0)

  def test_unwrap_ring_lons(self):
    coords = [[179.0, 0.0], [-179.0, 0.0], [-178.0, 1.0], [179.0, 1.0]]
    unwrapped = geo_utils.unwrap_ring_lons(coords)
    self.assertLen(unwrapped, 4)
    # -179 should become 181.0 to preserve continuity
    self.assertAlmostEqual(unwrapped[1][0], 181.0)
    self.assertAlmostEqual(unwrapped[2][0], 182.0)

  def test_antimeridian_polygon_area_is_not_smeared(self):
    feat = _straddler_feature()
    geometry = shapely.geometry.shape(feat["geometry"])

    # 4 x 4 degree box at equator is 16 deg^2 (~197,000 km^2 in EPSG:6933).
    self.assertAlmostEqual(
        geo_utils.split_at_antimeridian(geometry).area, 16.0, delta=1e-5
    )

    spatial_index = geo_utils.SpatialIndex(
        [feat], id_key="iso_a2", name_key="name"
    )
    self.assertEqual(spatial_index.count, 1)
    area = spatial_index.metadata[0]["area_sq_km"]
    self.assertAlmostEqual(area, 197000.0, delta=5000.0)

    # Without split, polygon smears across latitude band (50x larger).
    legacy = geo_utils.SpatialIndex(
        [feat], id_key="iso_a2", name_key="name", split_antimeridian=False
    )
    self.assertGreater(legacy.metadata[0]["area_sq_km"], 50.0 * area)

  def test_antimeridian_polygon_does_not_match_distant_segments(self):
    spatial_index = geo_utils.SpatialIndex(
        [_straddler_feature()], id_key="iso_a2", name_key="name"
    )
    far = shapely.geometry.LineString([(-30.0, 0.0), (-29.0, 0.5)])
    far_proj = shapely.ops.transform(spatial_index.transformer.transform, far)
    self.assertEmpty(_query_single_line(spatial_index, far_proj))

    legacy = geo_utils.SpatialIndex(
        [_straddler_feature()],
        id_key="iso_a2",
        name_key="name",
        split_antimeridian=False,
    )
    self.assertLen(_query_single_line(legacy, far_proj), 1)

  @parameterized.named_parameters(
      dict(testcase_name="east_of_antimeridian", lon_a=178.5, lon_b=179.5),
      dict(testcase_name="west_of_antimeridian", lon_a=-179.5, lon_b=-178.5),
  )
  def test_antimeridian_polygon_matches_near_segments(
      self, lon_a: float, lon_b: float
  ):
    spatial_index = geo_utils.SpatialIndex(
        [_straddler_feature()], id_key="iso_a2", name_key="name"
    )
    near = shapely.geometry.LineString([(lon_a, 0.0), (lon_b, 0.5)])
    near_proj = shapely.ops.transform(spatial_index.transformer.transform, near)
    self.assertLen(_query_single_line(spatial_index, near_proj), 1)

  @parameterized.named_parameters(
      dict(
          testcase_name="negative_start_hole",
          hole=[
              (-178.0, -2.0),
              (-178.0, 2.0),
              (178.0, 2.0),
              (178.0, -2.0),
              (-178.0, -2.0),
          ],
      ),
      dict(
          testcase_name="positive_start_hole",
          hole=[
              (178.0, -2.0),
              (178.0, 2.0),
              (-178.0, 2.0),
              (-178.0, -2.0),
              (178.0, -2.0),
          ],
      ),
  )
  def test_unwrap_polygon_anchors_antimeridian_crossing_holes(
      self, hole: list[tuple[float, float]]
  ):
    shell = [
        (170.0, -10.0),
        (-170.0, -10.0),
        (-170.0, 10.0),
        (170.0, 10.0),
        (170.0, -10.0),
    ]
    polygon = shapely.geometry.Polygon(shell, [hole])
    split = geo_utils.split_at_antimeridian(polygon)
    # 400 - 16 = 384 deg^2.
    self.assertAlmostEqual(split.area, 384.0, delta=1e-5)
    self.assertFalse(split.covers(shapely.geometry.Point(179.0, 0.0)))
    self.assertFalse(split.covers(shapely.geometry.Point(-179.0, 0.0)))
    self.assertTrue(split.covers(shapely.geometry.Point(175.0, 0.0)))
    self.assertTrue(split.covers(shapely.geometry.Point(-175.0, 0.0)))

  def test_unwrap_polygon_anchors_non_crossing_holes(self):
    # Shell spans 170 to -170 across the antimeridian.
    shell = [
        (170.0, -10.0),
        (-170.0, -10.0),
        (-170.0, 10.0),
        (170.0, 10.0),
        (170.0, -10.0),
    ]
    # Hole entirely on the negative side: [-176, -172] x [-2, 2], area 16 deg^2.
    hole_neg = [
        (-176.0, -2.0),
        (-176.0, 2.0),
        (-172.0, 2.0),
        (-172.0, -2.0),
        (-176.0, -2.0),
    ]
    # Hole entirely on the positive side: [172, 176] x [-2, 2], area 16 deg^2.
    hole_pos = [
        (172.0, -2.0),
        (172.0, 2.0),
        (176.0, 2.0),
        (176.0, -2.0),
        (172.0, -2.0),
    ]
    polygon = shapely.geometry.Polygon(shell, [hole_neg, hole_pos])
    split = geo_utils.split_at_antimeridian(polygon)
    # 400 - 16 - 16 = 368 deg^2.
    self.assertAlmostEqual(split.area, 368.0, delta=1e-5)
    self.assertFalse(split.covers(shapely.geometry.Point(-174.0, 0.0)))
    self.assertFalse(split.covers(shapely.geometry.Point(174.0, 0.0)))
    self.assertTrue(split.covers(shapely.geometry.Point(178.0, 0.0)))

  def test_spatial_index_with_antimeridian_hole(self):
    feat = {
        "type": "Feature",
        "properties": {"iso_a2": "XX", "name": "HOLE_STRADDLER"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [170.0, -10.0],
                    [-170.0, -10.0],
                    [-170.0, 10.0],
                    [170.0, 10.0],
                    [170.0, -10.0],
                ],
                [
                    [-178.0, -2.0],
                    [-178.0, 2.0],
                    [178.0, 2.0],
                    [178.0, -2.0],
                    [-178.0, -2.0],
                ],
            ],
        },
    }
    spatial_index = geo_utils.SpatialIndex(
        [feat], id_key="iso_a2", name_key="name"
    )
    self.assertEqual(spatial_index.count, 1)

    # Segment entirely inside the hole: should have zero intersections.
    hole_line = shapely.geometry.LineString([(179.0, -1.0), (179.0, 1.0)])
    hole_line_proj = shapely.ops.transform(
        spatial_index.transformer.transform, hole_line
    )
    self.assertEmpty(_query_single_line(spatial_index, hole_line_proj))

    # Segment in the solid part: should intersect with prop 1.0.
    solid_line = shapely.geometry.LineString([(175.0, -1.0), (175.0, 1.0)])
    solid_line_proj = shapely.ops.transform(
        spatial_index.transformer.transform, solid_line
    )
    hits = _query_single_line(spatial_index, solid_line_proj)
    self.assertLen(hits, 1)
    self.assertAlmostEqual(hits[0][1], 1.0)

  def test_spatial_index_preserves_non_crossing_holes_in_antimeridian_shell(
      self,
  ):
    holed_index = geo_utils.SpatialIndex(
        [_antimeridian_shell_with_non_crossing_holes_feature()],
        id_key="iso_a2",
        name_key="name",
    )
    solid_index = geo_utils.SpatialIndex(
        [
            _antimeridian_shell_with_non_crossing_holes_feature(
                include_holes=False
            )
        ],
        id_key="iso_a2",
        name_key="name",
    )
    self.assertAlmostEqual(
        holed_index.areas_by_id["XX"] / solid_index.areas_by_id["XX"],
        0.92,
        delta=0.01,
    )
    lines = np.array(
        [
            shapely.ops.transform(
                holed_index.transformer.transform,
                shapely.geometry.LineString([(-175.0, -1.0), (-173.0, 1.0)]),
            ),
            shapely.ops.transform(
                holed_index.transformer.transform,
                shapely.geometry.LineString([(173.0, -1.0), (175.0, 1.0)]),
            ),
            shapely.ops.transform(
                holed_index.transformer.transform,
                shapely.geometry.LineString([(178.0, -1.0), (178.0, 1.0)]),
            ),
        ],
        dtype=object,
    )
    seg_idx, _, props = holed_index.query_intersections_batch(
        lines, shapely.length(lines)
    )
    self.assertEqual(seg_idx.tolist(), [2])
    self.assertEqual(props.tolist(), [1.0])

  def test_split_at_antimeridian_preserves_polygon_with_zero_wrapped_area(self):
    shell = [
        (170.0, -2.0),
        (-170.0, -2.0),
        (-170.0, 2.0),
        (170.0, 2.0),
        (170.0, -2.0),
    ]
    hole = [
        (179.0, -1.95),
        (179.0, 1.95),
        (-179.0, 1.95),
        (-179.0, -1.95),
        (179.0, -1.95),
    ]
    polygon = shapely.geometry.Polygon(shell, [hole])
    self.assertLessEqual(polygon.area, 0.0)
    split = geo_utils.split_at_antimeridian(polygon)
    self.assertAlmostEqual(split.area, 72.2, delta=1e-5)

  def test_split_at_antimeridian_handles_geometry_collection_and_degenerate(
      self,
  ):
    straddler = shapely.geometry.shape(_straddler_feature()["geometry"])
    collection = shapely.geometry.GeometryCollection(
        [straddler, shapely.geometry.Point(0.0, 0.0)]
    )
    split_collection = geo_utils.split_at_antimeridian(collection)
    self.assertIsInstance(split_collection, shapely.geometry.MultiPolygon)
    self.assertAlmostEqual(split_collection.area, 16.0, delta=1e-5)
    point_only = shapely.geometry.GeometryCollection(
        [shapely.geometry.Point(0.0, 0.0)]
    )
    self.assertEqual(
        geo_utils.split_at_antimeridian(point_only),
        shapely.geometry.Polygon(),
    )
    degenerate = shapely.geometry.Polygon(
        [(179.0, 0.0), (-179.0, 0.0), (179.0, 0.0), (179.0, 0.0)]
    )
    self.assertTrue(geo_utils.split_at_antimeridian(degenerate).is_empty)

  def test_split_is_a_noop_for_ordinary_polygons(self):
    polygon = shapely.geometry.box(2.0, 49.0, 6.0, 52.0)
    split = geo_utils.split_at_antimeridian(polygon)
    self.assertTrue(split.equals(polygon))

  def test_geometry_containment_short_circuit_interior_segment(self):
    spatial_index = _belgium_and_germany_index()
    line_in = shapely.geometry.LineString([(3.0, 50.0), (4.0, 50.5)])
    line_in_proj = shapely.ops.transform(
        spatial_index.transformer.transform, line_in
    )
    results_in = _query_single_line(spatial_index, line_in_proj)
    self.assertLen(results_in, 1)
    meta, prop = results_in[0]
    self.assertEqual(meta["id"], "BE")
    self.assertEqual(prop, 1.0)

  def test_geometry_containment_straddling_segment_splits_proportionally(self):
    spatial_index = _belgium_and_germany_index()
    line_cross = shapely.geometry.LineString([(4.0, 50.0), (8.0, 50.0)])
    line_cross_proj = shapely.ops.transform(
        spatial_index.transformer.transform, line_cross
    )
    results_cross = _query_single_line(spatial_index, line_cross_proj)
    self.assertLen(results_cross, 2)
    ids = {results_cross[0][0]["id"], results_cross[1][0]["id"]}
    self.assertEqual(ids, {"BE", "DE"})
    self.assertAlmostEqual(
        results_cross[0][1] + results_cross[1][1], 1.0, places=5
    )

  def test_geometry_containment_exterior_segment_returns_empty(self):
    spatial_index = _belgium_and_germany_index()
    line_out = shapely.geometry.LineString([(100.0, 10.0), (101.0, 11.0)])
    line_out_proj = shapely.ops.transform(
        spatial_index.transformer.transform, line_out
    )
    self.assertEmpty(_query_single_line(spatial_index, line_out_proj))

  def test_query_intersections_batch(self):
    feats = [{
        "type": "Feature",
        "properties": {"iso_a2": "BE", "name": "Belgium"},
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
    }]
    spatial_index = geo_utils.SpatialIndex(
        feats, id_key="iso_a2", name_key="name"
    )
    line1 = shapely.geometry.LineString([(3.0, 50.0), (4.0, 50.5)])
    line2 = shapely.geometry.LineString([(100.0, 10.0), (101.0, 11.0)])
    line1_p = shapely.ops.transform(spatial_index.transformer.transform, line1)
    line2_p = shapely.ops.transform(spatial_index.transformer.transform, line2)

    lines = np.array([line1_p, line2_p], dtype=object)
    lengths = np.array([line1_p.length, line2_p.length], dtype=np.float64)

    seg_ids, unused_poly_ids, proportions = (
        spatial_index.query_intersections_batch(lines, lengths)
    )
    self.assertLen(seg_ids, 1)
    self.assertEqual(int(seg_ids[0]), 0)
    self.assertAlmostEqual(float(proportions[0]), 1.0)

  def _three_polygon_index(self) -> geo_utils.SpatialIndex:
    feats = [
        {
            "type": "Feature",
            "properties": {"iso_a2": "P1", "name": "Poly1"},
            "geometry": shapely.geometry.mapping(
                shapely.geometry.box(0.0, 0.0, 10.0, 10.0)
            ),
        },
        {
            "type": "Feature",
            "properties": {"iso_a2": "P2", "name": "Poly2"},
            "geometry": shapely.geometry.mapping(
                shapely.geometry.box(10.0, 0.0, 20.0, 10.0)
            ),
        },
        {
            "type": "Feature",
            "properties": {"iso_a2": "P3", "name": "Poly3"},
            "geometry": shapely.geometry.mapping(
                shapely.geometry.Polygon(
                    [(0.0, 15.0), (10.0, 15.0), (5.0, 25.0)]
                )
            ),
        },
    ]
    return geo_utils.SpatialIndex(feats, id_key="iso_a2", name_key="name")

  def test_query_intersections_batch_multiple_polygons(self):
    spatial_index = self._three_polygon_index()
    transform = spatial_index.transformer.transform
    lines_p = [
        shapely.ops.transform(
            transform, shapely.geometry.LineString([(2.0, 2.0), (8.0, 8.0)])
        ),
        shapely.ops.transform(
            transform, shapely.geometry.LineString([(5.0, 5.0), (15.0, 5.0)])
        ),
        shapely.ops.transform(
            transform, shapely.geometry.LineString([(5.0, 5.0), (5.0, -5.0)])
        ),
        shapely.ops.transform(
            transform, shapely.geometry.LineString([(1.0, 24.0), (2.0, 24.0)])
        ),
        shapely.ops.transform(
            transform,
            shapely.geometry.LineString([(100.0, 100.0), (101.0, 101.0)]),
        ),
    ]
    lines_arr = np.array(lines_p, dtype=object)
    lengths_arr = np.array(
        [
            lines_p[0].length,
            lines_p[1].length,
            lines_p[2].length,
            lines_p[3].length,
            lines_p[4].length,
        ],
        dtype=np.float64,
    )
    seg_ids, poly_ids, proportions = spatial_index.query_intersections_batch(
        lines_arr, lengths_arr
    )
    np.testing.assert_array_equal(seg_ids, [0, 1, 1, 2])
    np.testing.assert_array_equal(np.sort(poly_ids), [0, 0, 0, 1])
    np.testing.assert_allclose(proportions, [1.0, 0.5, 0.5, 0.5], atol=1e-3)

  def test_query_intersections_batch_empty_inputs(self):
    spatial_index = self._three_polygon_index()
    e_seg, e_poly, e_prop = spatial_index.query_intersections_batch(
        np.empty(0, dtype=object), np.empty(0, dtype=np.float64)
    )
    self.assertEmpty(e_seg)
    self.assertEqual(e_seg.dtype, np.int64)
    self.assertEqual(e_poly.dtype, np.int64)
    self.assertEqual(e_prop.dtype, np.float64)

  def test_split_at_antimeridian_polygon_touching_180_does_not_emit_sliver(
      self,
  ):
    polygon = shapely.geometry.box(170.0, -10.0, 180.0, 10.0)
    split = geo_utils.split_at_antimeridian(polygon)
    self.assertEqual(split.geom_type, "Polygon")
    self.assertGreaterEqual(split.bounds[0], 170.0)
    self.assertTrue(split.equals(polygon))

  def test_split_at_antimeridian_multipolygon_drops_1d_boundary_slivers(self):
    # Unwrapped polygon from 175 to 185, with a tail touching 540 (band 1 max)
    # or a MultiPolygon combining a 170..180 piece with a straddler.
    p1 = shapely.geometry.box(170.0, -10.0, 180.0, 10.0)
    p2 = shapely.geometry.shape(_straddler_feature()["geometry"])
    multi = shapely.geometry.MultiPolygon([p1, p2])
    split = geo_utils.split_at_antimeridian(multi)
    self.assertIn(split.geom_type, ("Polygon", "MultiPolygon"))
    parts = (
        list(split.geoms)
        if isinstance(split, shapely.geometry.MultiPolygon)
        else [split]
    )
    self.assertEqual({sub.geom_type for sub in parts}, {"Polygon"})

  def test_spatial_index_polygon_touching_180_has_bounded_envelope(self):
    feat = {
        "type": "Feature",
        "properties": {"iso_a2": "PAZA", "name": "FIR ANCHORAGE OCEANIC"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [159.0, 51.0],
                [180.0, 51.0],
                [180.0, 70.0],
                [159.0, 70.0],
                [159.0, 51.0],
            ]],
        },
    }
    spatial_index = geo_utils.SpatialIndex(
        [feat], id_key="iso_a2", name_key="name"
    )
    self.assertEqual(spatial_index.count, 1)
    indexed_geom = spatial_index._geometries[0]
    self.assertIn(indexed_geom.geom_type, ("Polygon", "MultiPolygon"))
    # Projected min_x must remain in the Eastern Hemisphere (> 0), not -180.
    self.assertGreater(indexed_geom.bounds[0], 0.0)

    # A segment along the western dateline (-180.0) must not intersect a
    # translated phantom LineString from +180.0.
    west_edge = shapely.geometry.LineString([(-180.0, 55.0), (-180.0, 60.0)])
    west_proj = shapely.ops.transform(
        spatial_index.transformer.transform, west_edge
    )
    self.assertEmpty(_query_single_line(spatial_index, west_proj))

  def test_spatial_index_prepares_geometries(self):
    feats = [
        {
            "type": "Feature",
            "properties": {"iso_a2": "BE", "name": "Belgium"},
            "geometry": shapely.geometry.mapping(
                shapely.geometry.box(2.0, 49.0, 6.0, 52.0)
            ),
        },
        {
            "type": "Feature",
            "properties": {"iso_a2": "FR", "name": "France"},
            "geometry": shapely.geometry.mapping(
                shapely.geometry.MultiPolygon([
                    shapely.geometry.box(-5.0, 42.0, 8.0, 51.0),
                    shapely.geometry.box(8.5, 41.3, 9.6, 43.0),
                ])
            ),
        },
    ]
    spatial_index = geo_utils.SpatialIndex(
        feats, id_key="iso_a2", name_key="name"
    )
    self.assertEqual(spatial_index.count, 2)
    self.assertTrue(
        all(shapely.is_prepared(g) for g in spatial_index._geometries)
    )
    self.assertTrue(
        bool(np.all(shapely.is_prepared(spatial_index._geometries_array)))
    )

  def test_spatial_index_handles_empty_or_non_polygonal_geometries(self):
    empty_idx = geo_utils.SpatialIndex(
        [
            {"type": "Feature", "properties": {}, "geometry": None},
            {
                "type": "Feature",
                "properties": {"iso_a2": "PT"},
                "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
            },
        ],
        id_key="iso_a2",
        name_key="name",
    )
    self.assertEqual(empty_idx.count, 0)
    self.assertIsNone(empty_idx._tree)
    self.assertEmpty(empty_idx.areas_by_id)
    self.assertEmpty(empty_idx.areas_by_name_and_id)
    self.assertEmpty(empty_idx.names_by_id)

  def _three_country_features(self) -> list[dict[str, object]]:
    return [
        {
            "type": "Feature",
            "properties": {"iso_a2": "BE", "name": "Belgium"},
            "geometry": shapely.geometry.mapping(
                shapely.geometry.box(2.0, 49.0, 6.0, 52.0)
            ),
        },
        {
            "type": "Feature",
            "properties": {"iso_a2": "DE", "name": "Germany"},
            "geometry": shapely.geometry.mapping(
                shapely.geometry.box(6.0, 47.0, 15.0, 55.0)
            ),
        },
        {
            "type": "Feature",
            "properties": {"iso_a2": "NL", "name": "Netherlands"},
            "geometry": shapely.geometry.mapping(
                shapely.geometry.box(3.3, 50.7, 7.2, 53.5)
            ),
        },
    ]

  def test_spatial_index_batch_vectorized_coordinate_projection(self):
    feats = self._three_country_features()
    call_count = 0
    real_from_crs = geo_utils.pyproj.Transformer.from_crs

    def _wrapped_from_crs(*args, **kwargs):
      transformer = real_from_crs(*args, **kwargs)
      orig_transform = transformer.transform

      def _counting_transform(xx, yy, *t_args, **t_kwargs):
        nonlocal call_count
        call_count += 1
        return orig_transform(xx, yy, *t_args, **t_kwargs)

      transformer.transform = _counting_transform
      return transformer

    with mock.patch.object(
        geo_utils.pyproj.Transformer,
        "from_crs",
        side_effect=_wrapped_from_crs,
        autospec=True,
        spec_set=True,
    ):
      spatial_index = geo_utils.SpatialIndex(
          feats, id_key="iso_a2", name_key="name"
      )

    self.assertEqual(call_count, 1)
    self.assertEqual(spatial_index.count, 3)

  def test_spatial_index_vectorized_projection_matches_individual_areas(self):
    feats = self._three_country_features()
    spatial_index = geo_utils.SpatialIndex(
        feats, id_key="iso_a2", name_key="name"
    )
    ref_transformer = geo_utils.pyproj.Transformer.from_crs(
        "EPSG:4326", "EPSG:6933", always_xy=True
    )
    actual_areas = [round(g.area, 3) for g in spatial_index._geometries]
    expected_areas = [
        round(
            shapely.ops.transform(
                ref_transformer.transform,
                shapely.geometry.shape(feat["geometry"]),
            ).area,
            3,
        )
        for feat in feats
    ]
    self.assertEqual(actual_areas, expected_areas)

  def _build_country_lookup_index(self) -> geo_utils.SpatialIndex:
    box_geom = shapely.geometry.mapping(
        shapely.geometry.box(2.0, 49.0, 4.0, 51.0)
    )
    country_feats = [
        {
            "type": "Feature",
            "properties": {"iso_a2": "be", "name": "Belgium"},
            "geometry": box_geom,
        },
        {
            "type": "Feature",
            "properties": {"iso_a2": "BE", "name": "Belgium"},
            "geometry": box_geom,
        },
        {
            "type": "Feature",
            "properties": {"iso_a2": "-99", "name": "Kosovo"},
            "geometry": box_geom,
        },
        {
            "type": "Feature",
            "properties": {"iso_a2": "-99", "name": "Somaliland"},
            "geometry": box_geom,
        },
    ]
    return geo_utils.SpatialIndex(
        country_feats, id_key="iso_a2", name_key="name"
    )

  def _build_fir_lookup_features(self) -> list[dict[str, object]]:
    box_geom = shapely.geometry.mapping(
        shapely.geometry.box(2.0, 49.0, 4.0, 51.0)
    )
    props_list = [
        {
            "fir_icao": "uhmm",
            "firname": "FIR MAGADAN EAST",
            "areasqkm": 12345.5,
        },
        {
            "fir_icao": "UHMM",
            "firname": "FIR MAGADAN OCEANIC EAST",
            "areasqkm": "67890.25",
        },
        {
            "fir_icao": "EBBU",
            "firname": "BRUSSELS FIR",
            "areasqkm": "invalid",
        },
        {
            "fir_icao": "EDGG",
            "firname": "LANGEN",
            "SQKMgeod": 111.0,
            "area_sq_km": 222.0,
        },
    ]
    return [
        {"type": "Feature", "properties": p, "geometry": box_geom}
        for p in props_list
    ]

  def test_spatial_index_caches_metadata_lookup_dict_instances(self):
    c_idx = self._build_country_lookup_index()
    self.assertIs(c_idx.areas_by_id, c_idx.areas_by_id)
    self.assertIs(c_idx.areas_by_name_and_id, c_idx.areas_by_name_and_id)
    self.assertIs(c_idx.names_by_id, c_idx.names_by_id)

  def test_spatial_index_country_area_and_name_lookups(self):
    c_idx = self._build_country_lookup_index()
    single_area = c_idx.metadata[0]["area_sq_km"]
    self.assertAlmostEqual(c_idx.areas_by_id["BE"], 2.0 * single_area, places=6)
    self.assertNotIn("be", c_idx.areas_by_id)
    self.assertAlmostEqual(
        c_idx.areas_by_name_and_id[("Belgium", "BE")],
        2.0 * single_area,
        places=6,
    )
    self.assertNotIn(("Belgium", "be"), c_idx.areas_by_name_and_id)
    self.assertEqual(
        (c_idx.metadata[0]["id"], c_idx.names_by_id),
        ("BE", {"BE": "Belgium"}),
    )

  def test_spatial_index_composite_key_disambiguates_sentinel_iso(self):
    c_idx = self._build_country_lookup_index()
    single_area = c_idx.metadata[0]["area_sq_km"]
    self.assertAlmostEqual(
        c_idx.areas_by_id["-99|KOSOVO"], single_area, places=6
    )
    self.assertAlmostEqual(
        c_idx.areas_by_id["-99|SOMALILAND"], single_area, places=6
    )

  def test_spatial_index_fir_area_property_overrides_and_fallbacks(self):
    f_idx = geo_utils.SpatialIndex(
        self._build_fir_lookup_features(),
        id_key="fir_icao",
        name_key="firname",
        area_property="areasqkm",
    )
    single_area = f_idx.metadata[2]["proj_area_sq_km"]
    self.assertAlmostEqual(
        f_idx.areas_by_name_and_id[("FIR MAGADAN EAST", "UHMM")], 12345.5
    )
    self.assertAlmostEqual(
        f_idx.areas_by_name_and_id[("FIR MAGADAN OCEANIC EAST", "UHMM")],
        67890.25,
    )
    self.assertAlmostEqual(
        f_idx.areas_by_name_and_id[("BRUSSELS FIR", "EBBU")],
        single_area,
        places=6,
    )
    self.assertAlmostEqual(
        f_idx.areas_by_name_and_id[("LANGEN", "EDGG")],
        single_area,
        places=6,
    )

  def test_spatial_index_fir_without_area_property_uses_geometry_area(self):
    f_idx_default = geo_utils.SpatialIndex(
        self._build_fir_lookup_features(),
        id_key="fir_icao",
        name_key="firname",
    )
    single_area = f_idx_default.metadata[0]["proj_area_sq_km"]
    self.assertAlmostEqual(
        f_idx_default.areas_by_name_and_id[("FIR MAGADAN EAST", "UHMM")],
        single_area,
        places=6,
    )

  def test_spatial_index_area_property_ratio_warning(self):
    # Box geom area is ~12,300 km^2. A property area of 100.0 km^2 produces a
    # ratio of ~123, which is well outside [0.5, 2.0] and triggers a warning.
    feat = {
        "type": "Feature",
        "properties": {
            "fir_icao": "UHMM",
            "firname": "FIR MAGADAN EAST",
            "areasqkm": 100.0,
        },
        "geometry": shapely.geometry.mapping(
            shapely.geometry.box(0.0, 0.0, 1.0, 1.0)
        ),
    }
    with self.assertLogs(level="WARNING") as logs:
      spatial_index = geo_utils.SpatialIndex(
          [feat],
          id_key="fir_icao",
          name_key="firname",
          area_property="areasqkm",
      )
    self.assertTrue(
        any("deviates significantly" in record for record in logs.output)
    )
    self.assertAlmostEqual(
        spatial_index.areas_by_name_and_id[("FIR MAGADAN EAST", "UHMM")], 100.0
    )
    self.assertAlmostEqual(spatial_index.metadata[0]["area_sq_km"], 100.0)
    self.assertGreater(spatial_index.metadata[0]["proj_area_sq_km"], 10000.0)

  def test_spatial_index_area_property_non_positive_or_invalid_warning(self):
    box_geom = shapely.geometry.mapping(
        shapely.geometry.box(0.0, 0.0, 1.0, 1.0)
    )
    feats = [
        {
            "type": "Feature",
            "properties": {
                "fir_icao": "UHMM",
                "firname": "FIR MAGADAN EAST",
                "areasqkm": 0.0,
            },
            "geometry": box_geom,
        },
        {
            "type": "Feature",
            "properties": {
                "fir_icao": "EBBU",
                "firname": "BRUSSELS FIR",
                "areasqkm": "not_a_number",
            },
            "geometry": box_geom,
        },
    ]
    with self.assertLogs(level="WARNING") as logs:
      spatial_index = geo_utils.SpatialIndex(
          feats,
          id_key="fir_icao",
          name_key="firname",
          area_property="areasqkm",
      )
    self.assertTrue(any("non-positive" in record for record in logs.output))
    self.assertTrue(any("non-numeric" in record for record in logs.output))
    proj_area = spatial_index.metadata[0]["proj_area_sq_km"]
    self.assertAlmostEqual(
        spatial_index.metadata[0]["area_sq_km"], proj_area, places=6
    )
    self.assertAlmostEqual(
        spatial_index.metadata[1]["area_sq_km"], proj_area, places=6
    )

  def test_spatial_index_area_property_missing_key_falls_back_to_proj_area(
      self,
  ):
    feat = {
        "type": "Feature",
        "properties": {"fir_icao": "EDGG", "firname": "LANGEN"},
        "geometry": shapely.geometry.mapping(
            shapely.geometry.box(0.0, 0.0, 1.0, 1.0)
        ),
    }
    spatial_index = geo_utils.SpatialIndex(
        [feat],
        id_key="fir_icao",
        name_key="firname",
        area_property="areasqkm",
    )
    proj_area = spatial_index.metadata[0]["proj_area_sq_km"]
    self.assertAlmostEqual(
        spatial_index.metadata[0]["area_sq_km"], proj_area, places=6
    )

  def test_spatial_index_rejects_missing_or_blank_id_key(self):
    feature = {
        "type": "Feature",
        "properties": {"iso_a2": "  ", "name": "NoIdLand"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [0.0, 0.0],
                [1.0, 0.0],
                [1.0, 1.0],
                [0.0, 1.0],
                [0.0, 0.0],
            ]],
        },
    }
    with self.assertRaisesRegex(ValueError, "missing or empty 'iso_a2'"):
      geo_utils.SpatialIndex([feature], id_key="iso_a2", name_key="name")

  def test_spatial_index_rejects_missing_or_blank_name_key(self):
    feature = {
        "type": "Feature",
        "properties": {"iso_a2": "NL", "name": ""},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [0.0, 0.0],
                [1.0, 0.0],
                [1.0, 1.0],
                [0.0, 1.0],
                [0.0, 0.0],
            ]],
        },
    }
    with self.assertRaisesRegex(ValueError, "missing or empty 'name'"):
      geo_utils.SpatialIndex([feature], id_key="iso_a2", name_key="name")

  def test_spatial_index_does_not_fall_back_to_uppercase_properties(self):
    feature = {
        "type": "Feature",
        "properties": {"ISO_A2": "FR", "NAME": "France"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [0.0, 0.0],
                [1.0, 0.0],
                [1.0, 1.0],
                [0.0, 1.0],
                [0.0, 0.0],
            ]],
        },
    }
    with self.assertRaisesRegex(ValueError, "missing or empty 'iso_a2'"):
      geo_utils.SpatialIndex([feature], id_key="iso_a2", name_key="name")

  def _l_shape_index(self) -> geo_utils.SpatialIndex:
    l_shape_feat = {
        "type": "Feature",
        "properties": {"iso_a2": "LS", "name": "LShape"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [0.0, 0.0],
                [10.0, 0.0],
                [10.0, 2.0],
                [2.0, 2.0],
                [2.0, 10.0],
                [0.0, 10.0],
                [0.0, 0.0],
            ]],
        },
    }
    return geo_utils.SpatialIndex(
        [l_shape_feat], id_key="iso_a2", name_key="name"
    )

  def test_query_intersections_batch_concave_bbox_miss_returns_empty(self):
    spatial_index = self._l_shape_index()
    bbox_miss = shapely.geometry.LineString([(5.0, 5.0), (8.0, 8.0)])
    bbox_miss_proj = shapely.ops.transform(
        spatial_index.transformer.transform, bbox_miss
    )
    self.assertIsNotNone(spatial_index._tree)
    self.assertLen(spatial_index._tree.query(bbox_miss_proj), 1)
    self.assertEmpty(_query_single_line(spatial_index, bbox_miss_proj))

  def test_query_intersections_batch_covered_fast_path(self):
    spatial_index = self._l_shape_index()
    inside_1 = shapely.ops.transform(
        spatial_index.transformer.transform,
        shapely.geometry.LineString([(0.5, 0.5), (1.5, 0.5)]),
    )
    inside_2 = shapely.ops.transform(
        spatial_index.transformer.transform,
        shapely.geometry.LineString([(0.5, 2.0), (0.5, 8.0)]),
    )
    covered_lines = np.array([inside_1, inside_2], dtype=object)
    covered_lens = np.array(
        [inside_1.length, inside_2.length], dtype=np.float64
    )
    seg_ids, poly_ids, proportions = spatial_index.query_intersections_batch(
        covered_lines, covered_lens
    )
    np.testing.assert_array_equal(seg_ids, [0, 1])
    np.testing.assert_array_equal(poly_ids, [0, 0])
    np.testing.assert_allclose(proportions, [1.0, 1.0])

  def test_query_intersections_batch_drops_zero_length_covered_segments(self):
    spatial_index = self._l_shape_index()
    zero_inside = shapely.ops.transform(
        spatial_index.transformer.transform,
        shapely.geometry.LineString([(0.5, 0.5), (0.5, 0.5)]),
    )
    inside = shapely.ops.transform(
        spatial_index.transformer.transform,
        shapely.geometry.LineString([(0.5, 0.5), (1.5, 0.5)]),
    )
    crossing = shapely.ops.transform(
        spatial_index.transformer.transform,
        shapely.geometry.LineString([(5.0, 0.5), (15.0, 0.5)]),
    )
    self.assertEmpty(_query_single_line(spatial_index, zero_inside))
    seg_ids, poly_ids, proportions = spatial_index.query_intersections_batch(
        np.array([zero_inside, inside, inside, crossing], dtype=object),
        np.array(
            [0.0, np.nan, inside.length, crossing.length], dtype=np.float64
        ),
    )
    np.testing.assert_array_equal(seg_ids, [2, 3])
    np.testing.assert_array_equal(poly_ids, [0, 0])
    np.testing.assert_allclose(proportions, [1.0, 0.5], rtol=1e-4)

  def test_query_intersections_batch_mixed_boundary_and_point_touch(self):
    spatial_index = self._l_shape_index()
    inside_1 = shapely.ops.transform(
        spatial_index.transformer.transform,
        shapely.geometry.LineString([(0.5, 0.5), (1.5, 0.5)]),
    )
    crossing = shapely.ops.transform(
        spatial_index.transformer.transform,
        shapely.geometry.LineString([(1.0, 5.0), (3.0, 5.0)]),
    )
    bbox_miss_proj = shapely.ops.transform(
        spatial_index.transformer.transform,
        shapely.geometry.LineString([(5.0, 5.0), (8.0, 8.0)]),
    )
    poly_proj = spatial_index._geometries[0]
    max_x, min_y, _, _ = poly_proj.bounds
    point_touch = shapely.geometry.LineString(
        [(max_x, min_y), (max_x + 10000.0, min_y - 10000.0)]
    )
    mixed_lines = np.array(
        [inside_1, crossing, bbox_miss_proj, point_touch], dtype=object
    )
    mixed_lens = np.array(
        [line.length for line in mixed_lines], dtype=np.float64
    )
    m_seg_ids, m_poly_ids, m_props = spatial_index.query_intersections_batch(
        mixed_lines, mixed_lens
    )
    np.testing.assert_array_equal(m_seg_ids, [0, 1])
    np.testing.assert_array_equal(m_poly_ids, [0, 0])
    self.assertAlmostEqual(float(m_props[0]), 1.0)
    self.assertAlmostEqual(float(m_props[1]), 0.5, places=2)

  def test_antimeridian_crossing_fraction_east_and_westbound(self):
    lon_start = np.array(
        [170.0, -170.0, -180.0, -179.9999999], dtype=np.float64
    )
    lon_end = np.array([-170.0, 170.0, 180.0, 179.9999999], dtype=np.float64)
    eastbound, fraction = geo_utils.antimeridian_crossing_fraction(
        lon_start, lon_end
    )
    np.testing.assert_array_equal(eastbound, [True, False, False, False])
    np.testing.assert_allclose(fraction, [0.5, 0.5, 0.5, 0.5], atol=1e-9)

  @parameterized.parameters(
      (179.9999999, -179.9999997, 0.25),
      (-179.9999997, 179.9999999, 0.75),
      (179.9999995, -179.9999995, 0.5),
  )
  def test_antimeridian_crossing_fraction_is_exact_for_tiny_spans(
      self, lon_start, lon_end, expected
  ):
    _, fraction = geo_utils.antimeridian_crossing_fraction(
        np.array([lon_start]), np.array([lon_end])
    )
    np.testing.assert_allclose(fraction, [expected], atol=1e-6)

  def test_pack_month_hour_keys_round_trips_months_and_hours(self):
    months = np.array([202501, 202512, 6], dtype=np.int32)
    hours = np.array([0, 23, 14], dtype=np.int32)
    packed = geo_utils.pack_month_hour_keys(months, hours)
    self.assertEqual(packed.dtype, np.int64)
    self.assertEqual(
        geo_utils.unpack_month_hour_keys(packed),
        [(202501, 0), (202512, 23), (6, 14)],
    )


if __name__ == "__main__":
  absltest.main()
