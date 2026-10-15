"""Geospatial utilities, coordinate reprojection, and spatial indexing."""

from collections.abc import Mapping, Sequence
import math
from typing import Any

from absl import logging
import numpy as np
import pyproj
import shapely
import shapely.affinity
import shapely.errors
import shapely.geometry
from shapely.strtree import STRtree

from common import constants


def haversine_meters(
    lon1: np.ndarray, lat1: np.ndarray, lon2: np.ndarray, lat2: np.ndarray
) -> np.ndarray:
  """Computes great-circle segment lengths in meters via Haversine.

  Args:
    lon1: Start longitudes in degrees.
    lat1: Start latitudes in degrees.
    lon2: End longitudes in degrees.
    lat2: End latitudes in degrees.

  Returns:
    Array of segment lengths in meters.
  """
  lon1_rad, lat1_rad, lon2_rad, lat2_rad = map(
      np.radians, [lon1, lat1, lon2, lat2]
  )
  dlon = lon2_rad - lon1_rad
  dlat = lat2_rad - lat1_rad
  haversine_a = (
      np.sin(dlat / 2.0) ** 2
      + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon / 2.0) ** 2
  )
  central_angle = 2 * np.arcsin(np.sqrt(np.clip(haversine_a, 0.0, 1.0)))
  return central_angle * 6371000.0  # Earth mean radius in meters


def antimeridian_crossing_fraction(
    lon_start: np.ndarray, lon_end: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
  """Computes direction and split fraction for antimeridian-crossing segments.

  Args:
    lon_start: 1D float array of start longitudes for crossing segments, in
      degrees within [-180, 180].
    lon_end: 1D float array of end longitudes for crossing segments, in degrees
      within [-180, 180].

  Returns:
    Tuple of `(eastbound, before_fraction)` where `eastbound` is a boolean array
    (`True` when crossing from +180 into -180, i.e. `lon_end - lon_start <
    -180`) and `before_fraction` is a float64 array in `[0.0, 1.0]` giving the
    fraction of the unwrapped longitude span before reaching +/-180.
  """
  lon_start_f64 = np.asarray(lon_start, dtype=np.float64)
  lon_end_f64 = np.asarray(lon_end, dtype=np.float64)
  eastbound = (lon_end_f64 - lon_start_f64) < -180.0
  dlon = np.where(
      eastbound,
      (lon_end_f64 + 360.0) - lon_start_f64,
      (lon_end_f64 - 360.0) - lon_start_f64,
  )
  before_span = np.where(
      eastbound, 180.0 - lon_start_f64, -180.0 - lon_start_f64
  )
  # For longitudes within [-180, 180], `|before_span| <= |dlon|`, so the exact
  # ratio needs no clamping of tiny spans (clamping the denominator alone
  # skewed it). A zero-span crossing (a segment lying on the antimeridian) is
  # split evenly.
  fraction = np.divide(
      before_span,
      dlon,
      out=np.full_like(dlon, 0.5),
      where=dlon != 0.0,
  )
  return eastbound, np.clip(fraction, 0.0, 1.0)


def pack_month_hour_keys(months: np.ndarray, hours: np.ndarray) -> np.ndarray:
  """Packs `(month, hour)` integer arrays into 64-bit composite sort keys.

  Args:
    months: 1D integer array of month values (1..12 or YYYYMM).
    hours: 1D integer array of local hour values (0..23).

  Returns:
    1D int64 array where the upper 32 bits hold `months` and the lower 32 bits
    hold the unsigned 32-bit representation of `hours`.
  """
  return (months.astype(np.int64) << 32) | (
      hours.astype(np.uint32).astype(np.int64)
  )


def unpack_month_hour_keys(keys: np.ndarray) -> list[tuple[int, int]]:
  """Unpacks 64-bit composite sort keys into `(month, hour)` integer pairs.

  Args:
    keys: 1D int64 array of composite keys produced by `pack_month_hour_keys`.

  Returns:
    List of `(month, hour)` integer tuples.
  """
  return [(int(k >> 32), int(np.int32(k & 0xFFFFFFFF))) for k in keys]


def unwrap_ring_lons(
    coords: Sequence[Sequence[float]],
) -> list[tuple[float, float]]:
  """Removes +/-360 jumps from a ring so its longitudes are continuous.

  A ring crossing the antimeridian stores consecutive vertices as, say,
  (179.9, ...) followed by (-179.9, ...). Those are one degree apart on the
  globe but 359.8 degrees apart as stored, so any planar operation treats the
  edge as spanning the whole world. Unwrapping rewrites the second vertex as
  180.1 and keeps the edge one degree long.

  Args:
    coords: Ring vertices as (lon, lat) pairs in EPSG:4326.

  Returns:
    The same ring with longitudes shifted into a continuous range, which may
    extend beyond [-180, 180].
  """
  first_lon, first_lat, *_ = coords[0]
  prev_lon = float(first_lon)
  out = [(prev_lon, float(first_lat))]
  for point in coords[1:]:
    raw_lon, raw_lat, *_ = point
    lon = float(raw_lon)
    # Shift lon into the window (prev_lon - 180, prev_lon + 180].
    lon -= 360.0 * math.floor((lon - prev_lon + 180.0) / 360.0)
    out.append((lon, float(raw_lat)))
    prev_lon = lon
  return out


def _unwrap_polygon(
    polygon: shapely.geometry.Polygon,
) -> shapely.geometry.Polygon:
  """Applies `unwrap_ring_lons` to a polygon's exterior and anchors holes to it.

  Each hole is unwrapped and then shifted by the multiple of 360 degrees that
  minimizes the longitudinal distance between the exterior's midpoint and the
  hole's midpoint. This prevents interior rings from landing in an adjacent
  360-degree band and being discarded or absorbed by `make_valid`.

  Args:
    polygon: A Polygon whose exterior and interior rings are in EPSG:4326.

  Returns:
    An unwrapped Polygon with exterior and interior rings aligned in longitude.
  """
  ext_coords = list(polygon.exterior.coords)
  if not ext_coords:
    return polygon
  ext = unwrap_ring_lons(ext_coords)
  ext_lons = [lon for lon, _ in ext]
  ext_mid = 0.5 * (min(ext_lons) + max(ext_lons))
  holes = []
  for ring in polygon.interiors:
    ring_coords = list(ring.coords)
    if not ring_coords:
      continue
    hole_coords = unwrap_ring_lons(ring_coords)
    h_lons = [lon for lon, _ in hole_coords]
    h_mid = 0.5 * (min(h_lons) + max(h_lons))
    k = round((ext_mid - h_mid) / 360.0)
    holes.append([(lon + 360.0 * k, lat) for lon, lat in hole_coords])
  return shapely.geometry.Polygon(ext, holes)


def _extract_polygons(
    geometry: shapely.Geometry,
    *,
    require_positive_area: bool = True,
) -> list[shapely.geometry.Polygon]:
  """Extracts non-empty 2D Polygon components from any Shapely geometry.

  Args:
    geometry: Any Shapely geometry.
    require_positive_area: When True, omits polygons with `area <= 0.0`.

  Returns:
    Non-empty Polygon components extracted from `geometry`.
  """
  if geometry.is_empty:
    return []
  if isinstance(geometry, shapely.geometry.Polygon):
    if require_positive_area and geometry.area <= 0.0:
      return []
    return [geometry]
  if isinstance(
      geometry,
      (
          shapely.geometry.MultiPolygon,
          shapely.geometry.GeometryCollection,
      ),
  ):
    out: list[shapely.geometry.Polygon] = []
    for sub in geometry.geoms:
      out.extend(
          _extract_polygons(sub, require_positive_area=require_positive_area)
      )
    return out
  return []


def _as_polygonal(geometry: shapely.Geometry) -> shapely.Geometry:
  """Normalizes a geometry to Polygon or MultiPolygon, dropping 0D/1D."""
  polygons = _extract_polygons(geometry)
  if not polygons:
    return shapely.geometry.Polygon()
  if len(polygons) == 1:
    return polygons[0]
  return shapely.geometry.MultiPolygon(polygons)


def _clip_to_longitude_bands(
    polygon: shapely.geometry.Polygon,
) -> list[shapely.geometry.Polygon]:
  """Cuts an unwrapped polygon at every +/-180 crossing and shifts it back.

  Args:
    polygon: A Polygon whose longitudes may extend beyond [-180, 180], as
      produced by `_unwrap_polygon`.

  Returns:
    Polygon parts, each translated by a multiple of 360 degrees so that it
    lies within [-180, 180].
  """
  min_lon, _, max_lon, _ = polygon.bounds
  if min_lon >= -180.0 and max_lon <= 180.0:
    # Boundary-inclusive fast path: a polygon ending at +/-180.0 never
    # crossed the antimeridian and must not be clipped against an adjacent
    # band (which would produce a 1D LineString sliver along +/-180.0).
    return [polygon]

  pieces: list[shapely.geometry.Polygon] = []
  first_band = int(math.floor((min_lon + 180.0) / 360.0))
  last_band = int(math.floor((max_lon + 180.0) / 360.0))
  for band in range(first_band, last_band + 1):
    band_min = band * 360.0 - 180.0
    band_max = band * 360.0 + 180.0
    if max_lon <= band_min or min_lon >= band_max:
      continue
    clip = shapely.geometry.box(band_min, -90.0, band_max, 90.0)
    part = polygon.intersection(clip)
    for poly_part in _extract_polygons(part):
      if band != 0:
        poly_part = shapely.affinity.translate(poly_part, xoff=-band * 360.0)
      pieces.append(poly_part)
  return pieces


def split_at_antimeridian(geometry: shapely.Geometry) -> shapely.Geometry:
  """Cuts a lon/lat polygon into parts that each stay within [-180, 180].

  `pyproj` transforms vertices, not edges, so projecting a ring that crosses
  +/-180 produces a polygon smeared across the full width of the map. That
  inflates its area (Nadi East measured 24.7x its true size) and, because the
  smeared polygon goes into the spatial index, makes it intersect flight
  segments on the opposite side of the planet. Splitting the ring at the
  antimeridian before projection fixes both.

  Args:
    geometry: A Polygon, MultiPolygon, or GeometryCollection in EPSG:4326. Other
      geometry types are returned unchanged.

  Returns:
    An equivalent Polygon or MultiPolygon whose parts stay within [-180, 180].
  """
  if not isinstance(
      geometry,
      (
          shapely.geometry.Polygon,
          shapely.geometry.MultiPolygon,
          shapely.geometry.GeometryCollection,
      ),
  ):
    return geometry
  polygons = _extract_polygons(geometry, require_positive_area=False)
  if not polygons:
    return shapely.geometry.Polygon()

  pieces: list[shapely.geometry.Polygon] = []
  for polygon in polygons:
    unwrapped: shapely.Geometry = _unwrap_polygon(polygon)
    if not unwrapped.is_valid:
      unwrapped = shapely.make_valid(unwrapped)
    for sub_poly in _extract_polygons(unwrapped):
      pieces.extend(_clip_to_longitude_bands(sub_poly))

  if not pieces:
    return shapely.geometry.Polygon()
  if len(pieces) == 1:
    return pieces[0]
  merged = shapely.unary_union(pieces)
  if not merged.is_valid:
    merged = shapely.make_valid(merged)
  return _as_polygonal(merged)


def _extract_candidate_polygonal_geometries(
    features: Sequence[Mapping[str, Any]],
    *,
    split_antimeridian: bool,
) -> tuple[list[shapely.Geometry], list[tuple[int, Mapping[str, Any]]]]:
  """Parses GeoJSON features into valid polygonal geometries before projection.

  Args:
    features: Raw GeoJSON feature dictionaries.
    split_antimeridian: Whether to split polygons crossing +/-180 longitude.

  Returns:
    Tuple of `(candidate_geoms, candidate_features)` where each entry in
    `candidate_features` is `(original_index, feature_dict)`.
  """
  candidate_geoms: list[shapely.Geometry] = []
  candidate_features: list[tuple[int, Mapping[str, Any]]] = []

  for index, feature in enumerate(features):
    raw_geometry = feature.get(constants.GEOJSON_GEOMETRY)
    if not raw_geometry:
      continue
    geometry = shapely.geometry.shape(raw_geometry)
    # Cut rings that cross +/-180 before running `make_valid` or projecting.
    # Running `make_valid` on wrapped coordinates first would treat non-crossing
    # interior holes of an antimeridian-spanning shell as outside the shell and
    # convert them into exterior polygons that `unary_union` absorbs.
    if split_antimeridian:
      geometry = split_at_antimeridian(geometry)
    elif not geometry.is_valid:
      geometry = shapely.make_valid(geometry)
    geometry = _as_polygonal(geometry)
    if geometry.is_empty:
      continue

    candidate_geoms.append(geometry)
    candidate_features.append((index, feature))

  return candidate_geoms, candidate_features


def _resolve_feature_area_sq_km(
    *,
    orig_index: int,
    identifier: str,
    name: str,
    properties: Mapping[str, Any],
    proj_area_sq_km: float,
    area_property: str | None,
) -> float:
  """Resolves authoritative feature area in km², or projected fallback."""
  if area_property is None:
    return proj_area_sq_km
  raw_prop_area = properties.get(area_property)
  if raw_prop_area in (None, ""):
    return proj_area_sq_km
  prop_val, problem = _parse_positive_area(raw_prop_area)
  if prop_val is None:
    logging.warning(
        "Feature at index %d (%s, %r) has %s %s: %r;"
        " using projected area %.1f km².",
        orig_index,
        identifier,
        name,
        problem,
        area_property,
        raw_prop_area,
        proj_area_sq_km,
    )
    return proj_area_sq_km

  _warn_if_projected_area_deviates(
      orig_index=orig_index,
      identifier=identifier,
      name=name,
      area_property=area_property,
      prop_val=prop_val,
      proj_area_sq_km=proj_area_sq_km,
  )
  return prop_val


def _parse_positive_area(raw_prop_area: Any) -> tuple[float | None, str]:
  """Parses an authoritative area property value.

  Args:
    raw_prop_area: The raw property value; expected to be numeric.

  Returns:
    `(value, "")` for a positive number, otherwise `(None, problem)` where
    `problem` is `"non-numeric"` or `"non-positive"`.
  """
  try:
    prop_val = float(raw_prop_area)
  except (TypeError, ValueError):
    return None, "non-numeric"
  if prop_val <= 0.0:
    return None, "non-positive"
  return prop_val, ""


def _warn_if_projected_area_deviates(
    *,
    orig_index: int,
    identifier: str,
    name: str,
    area_property: str,
    prop_val: float,
    proj_area_sq_km: float,
) -> None:
  """Logs a warning if projected and authoritative areas differ by >2x.

  Args:
    orig_index: Index of the feature in the input collection.
    identifier: The feature's identifier.
    name: The feature's display name.
    area_property: Name of the authoritative area property.
    prop_val: Positive authoritative area in km².
    proj_area_sq_km: Projected polygon area in km².
  """
  ratio = proj_area_sq_km / prop_val
  if ratio < 0.5 or ratio > 2.0:
    logging.warning(
        "Feature at index %d (%s, %r) projected area %.1f km²"
        " deviates significantly from %s %.1f km² (ratio %.3f,"
        " expected [0.5, 2.0]).",
        orig_index,
        identifier,
        name,
        proj_area_sq_km,
        area_property,
        prop_val,
        ratio,
    )


def _clean_projected_polygon(
    proj_geom: shapely.Geometry | None,
) -> shapely.Geometry | None:
  """Repairs a projected geometry and reduces it to its polygonal parts.

  Args:
    proj_geom: A projected geometry, or `None` if projection produced nothing.

  Returns:
    A valid Polygon or MultiPolygon with positive area, or `None` if nothing
    polygonal with positive area remains.
  """
  if proj_geom is None or proj_geom.is_empty:
    return None
  if not proj_geom.is_valid:
    proj_geom = shapely.make_valid(proj_geom)
  proj_geom = _as_polygonal(proj_geom)
  if proj_geom.is_empty or proj_geom.area <= 0.0:
    return None
  return proj_geom


def _feature_identity(
    *,
    orig_index: int,
    properties: Mapping[str, Any],
    id_key: str,
    name_key: str,
) -> tuple[str, str]:
  """Reads and validates a feature's identifier and display name.

  Args:
    orig_index: Index of the feature in the input collection, for errors.
    properties: The feature's GeoJSON properties.
    id_key: Property name containing the entity identifier.
    name_key: Property name containing the entity display name.

  Returns:
    The stripped, uppercase `identifier` and stripped `name` as
    `(identifier, name)`.

  Raises:
    ValueError: If either property is missing or blank.
  """
  identifier = str(properties.get(id_key) or "").strip().upper()
  if not identifier:
    raise ValueError(
        f"Feature at index {orig_index} has missing or empty {id_key!r}"
        f" property: {properties!r}"
    )
  name = str(properties.get(name_key) or "").strip()
  if not name:
    raise ValueError(
        f"Feature at index {orig_index} ({identifier}) has missing or"
        f" empty {name_key!r} property: {properties!r}"
    )
  return identifier, name


def _build_strtree(
    geometries: Sequence[shapely.Geometry],
) -> tuple[np.ndarray, STRtree | None]:
  """Prepares geometries and builds an STRtree over them.

  Args:
    geometries: Projected polygon geometries to index.

  Returns:
    The prepared geometries as a 1D object array, and the tree over them, or
    `None` if there are no geometries.
  """
  geometries_array = np.array(geometries, dtype=object)
  if geometries_array.size > 0:
    shapely.prepare(geometries_array)
  tree = STRtree(geometries_array) if geometries_array.size > 0 else None
  return geometries_array, tree


class SpatialIndex:
  """In-memory Shapely STRtree index for fast polygon intersections."""

  def __init__(
      self,
      features: Sequence[Mapping[str, Any]],
      *,
      id_key: str = constants.KEY_ISO_A2,
      name_key: str = constants.KEY_NAME,
      src_crs: str = "EPSG:4326",
      target_crs: str = "EPSG:6933",
      split_antimeridian: bool = True,
      area_property: str | None = None,
  ):
    """Initializes SpatialIndex with a collection of GeoJSON features.

    Args:
      features: List of GeoJSON feature dictionaries. Polygonal features must
        have non-empty properties for `id_key` and `name_key`.
      id_key: Property name containing the entity identifier (e.g. 'iso_a2' or
        'fir_icao'). Values are stripped and normalized to uppercase in all
        indexed metadata and lookup maps.
      name_key: Property name containing the entity display name (e.g. 'name' or
        'firname').
      src_crs: Source coordinate reference system (default EPSG:4326).
      target_crs: Equal-area projection CRS (default EPSG:6933).
      split_antimeridian: If True, split polygons crossing the antimeridian.
      area_property: Optional property name containing authoritative feature
        area in km² (e.g. 'areasqkm'). When provided and positive, this value
        overrides the projected polygon area, subject to a consistency check
        against the projected geometry area.

    Raises:
      ValueError: If a polygonal feature has missing or blank `id_key` or
        `name_key`.
    """
    self._transformer = pyproj.Transformer.from_crs(
        src_crs, target_crs, always_xy=True
    )
    self._geometries: list[shapely.Geometry] = []
    self._metadata: list[dict[str, Any]] = []
    self._areas_by_id: dict[str, float] = {}
    self._areas_by_name_and_id: dict[tuple[str, str], float] = {}
    self._names_by_id: dict[str, str] = {}

    self._index_features(
        features,
        id_key=id_key,
        name_key=name_key,
        split_antimeridian=split_antimeridian,
        area_property=area_property,
    )
    self._geometries_array, self._tree = _build_strtree(self._geometries)

  def _index_features(
      self,
      features: Sequence[Mapping[str, Any]],
      *,
      id_key: str,
      name_key: str,
      split_antimeridian: bool,
      area_property: str | None,
  ) -> None:
    """Parses, projects, and registers every polygonal feature.

    Args:
      features: Raw GeoJSON feature dictionaries.
      id_key: Property name containing the entity identifier.
      name_key: Property name containing the entity display name.
      split_antimeridian: Whether to split polygons crossing +/-180 longitude.
      area_property: Optional property name containing authoritative area.

    Raises:
      ValueError: If a polygonal feature has missing or blank `id_key` or
        `name_key`.
    """
    candidate_geoms, candidate_features = (
        _extract_candidate_polygonal_geometries(
            features, split_antimeridian=split_antimeridian
        )
    )
    if not candidate_geoms:
      return
    proj_geoms = shapely.transform(candidate_geoms, self._transform_coords)
    for (orig_index, feature), proj_geom in zip(
        candidate_features, proj_geoms, strict=True
    ):
      self._register_projected_feature(
          orig_index=orig_index,
          feature=feature,
          proj_geom=proj_geom,
          id_key=id_key,
          name_key=name_key,
          area_property=area_property,
      )

  def _transform_coords(self, coords: np.ndarray) -> np.ndarray:
    """Reprojects an (N, 2) lon/lat coordinate array to `target_crs`."""
    if coords.size == 0:
      return coords
    tx, ty = self._transformer.transform(coords[:, 0], coords[:, 1])
    return np.column_stack((tx, ty)).astype(np.float64, copy=False)

  def _register_projected_feature(
      self,
      *,
      orig_index: int,
      feature: Mapping[str, Any],
      proj_geom: shapely.Geometry | None,
      id_key: str,
      name_key: str,
      area_property: str | None,
  ) -> None:
    """Validates and indexes a single projected polygon feature."""
    proj_geom = _clean_projected_polygon(proj_geom)
    if proj_geom is None:
      return

    properties = feature.get(constants.GEOJSON_PROPERTIES) or {}
    identifier, name = _feature_identity(
        orig_index=orig_index,
        properties=properties,
        id_key=id_key,
        name_key=name_key,
    )
    proj_area_sq_km = proj_geom.area / 1e6
    area_sq_km = _resolve_feature_area_sq_km(
        orig_index=orig_index,
        identifier=identifier,
        name=name,
        properties=properties,
        proj_area_sq_km=proj_area_sq_km,
        area_property=area_property,
    )
    self._store_feature(
        proj_geom=proj_geom,
        identifier=identifier,
        name=name,
        properties=properties,
        area_sq_km=area_sq_km,
        proj_area_sq_km=proj_area_sq_km,
    )

  def _store_feature(
      self,
      *,
      proj_geom: shapely.Geometry,
      identifier: str,
      name: str,
      properties: Mapping[str, Any],
      area_sq_km: float,
      proj_area_sq_km: float,
  ) -> None:
    """Appends a validated feature's geometry, metadata, and area lookups.

    Args:
      proj_geom: The cleaned projected polygon geometry.
      identifier: The feature's stripped, uppercase identifier.
      name: The feature's stripped display name.
      properties: The feature's GeoJSON properties.
      area_sq_km: The resolved (authoritative or projected) area in km².
      proj_area_sq_km: The projected polygon area in km².
    """
    item_meta = {
        "id": identifier,
        constants.KEY_NAME: name,
        constants.GEOJSON_PROPERTIES: properties,
        constants.KEY_AREA_SQ_KM: area_sq_km,
        "proj_area_sq_km": proj_area_sq_km,
    }
    self._geometries.append(proj_geom)
    self._metadata.append(item_meta)
    self._accumulate_area_lookups(
        identifier=identifier, name=name, area_sq_km=area_sq_km
    )

  def _accumulate_area_lookups(
      self, *, identifier: str, name: str, area_sq_km: float
  ) -> None:
    """Adds one polygon's area and name to the per-entity lookup tables.

    Args:
      identifier: The feature's stripped, uppercase identifier.
      name: The feature's stripped display name.
      area_sq_km: The polygon's resolved area in km².
    """
    name_upper = name.upper()
    self._areas_by_id[identifier] = (
        self._areas_by_id.get(identifier, 0.0) + area_sq_km
    )
    if name_upper and identifier.startswith("-"):
      comp_key = f"{identifier}|{name_upper}"
      self._areas_by_id[comp_key] = (
          self._areas_by_id.get(comp_key, 0.0) + area_sq_km
      )

    name_id_key = (name, identifier)
    self._areas_by_name_and_id[name_id_key] = (
        self._areas_by_name_and_id.get(name_id_key, 0.0) + area_sq_km
    )

    if identifier and not identifier.startswith("-") and name:
      self._names_by_id.setdefault(identifier, name)

  @property
  def transformer(self) -> pyproj.Transformer:
    """Returns the coordinate transformer from `src_crs` to `target_crs`.

    Returns:
      The pyproj.Transformer instance used to project coordinates.
    """
    return self._transformer

  @property
  def metadata(self) -> list[dict[str, Any]]:
    """Returns indexed feature metadata dicts aligned with `_geometries`.

    Returns:
      List of metadata dicts corresponding to the indexed geometries, where each
      dict's `'id'` entry is normalized to stripped uppercase.
    """
    return self._metadata

  @property
  def areas_by_id(self) -> dict[str, float]:
    """Total area per uppercase identifier, summed over an entity's polygons.

    Polygon area does not depend on flight data, so the derivation stage joins
    it from here rather than carrying it through the per-segment reduce. See
    `finalize_country_and_temporal_datasets`. Also records
    `"{ID_UPPER}|{NAME_UPPER}"` keys for sentinel territories (identifiers
    starting with `"-"`, e.g. `"-99"` like Kosovo, N. Cyprus, Somaliland) so
    they retain their individual polygon areas.

    Returns:
      Mapping from uppercase identifier string to total area in square
      kilometres.
    """
    return self._areas_by_id

  @property
  def areas_by_name_and_id(self) -> dict[tuple[str, str], float]:
    """Total area per (name, uppercase_identifier) pair.

    FIRs need this because one ICAO code can own several named regions, for
    example UHMM covers both `MAGADAN EAST` and `MAGADAN OCEANIC EAST` (names
    are indexed after `constants.clean_fir_name`).

    Returns:
      Mapping from `(name, id_upper)` tuple to total area in square kilometres.
    """
    return self._areas_by_name_and_id

  @property
  def names_by_id(self) -> dict[str, str]:
    """Canonical display name per uppercase ID (excluding `-` sentinels).

    Returns:
      Mapping from uppercase identifier string to canonical display name.
    """
    return self._names_by_id

  @property
  def count(self) -> int:
    """Returns the number of indexed polygon geometries.

    Returns:
      Integer count of indexed geometries in the spatial index.
    """
    return len(self._geometries)

  def query_intersections_batch(
      self, lines: np.ndarray, line_lengths: np.ndarray
  ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Queries intersecting polygons and length proportions for `lines`.

    Args:
      lines: 1D numpy object array of Shapely LineString geometries in
        target_crs.
      line_lengths: 1D numpy float array of original segment lengths. Segments
        with non-positive or `NaN` lengths are excluded.

    Returns:
      Tuple of `(seg_indices, poly_indices, proportions)` 1D numpy arrays for
      pairs with positive intersection proportion.
    """
    if self._tree is None or lines.size == 0:
      return _empty_intersection_batch()

    pairs = self._tree.query(lines)
    if pairs.shape[1] == 0:
      return _empty_intersection_batch()

    segment_indices, polygon_indices = pairs
    candidate_lines = lines[segment_indices]
    candidate_polygons = self._geometries_array[polygon_indices]

    hit = _filter_intersecting_pairs(
        candidate_polygons=candidate_polygons, candidate_lines=candidate_lines
    )
    if not np.any(hit):
      return _empty_intersection_batch()

    if not np.all(hit):
      segment_indices = segment_indices[hit]
      polygon_indices = polygon_indices[hit]
      candidate_lines = candidate_lines[hit]
      candidate_polygons = candidate_polygons[hit]

    is_covered = _compute_covered_mask(
        candidate_polygons=candidate_polygons, candidate_lines=candidate_lines
    )
    candidate_lengths = line_lengths[segment_indices]
    with np.errstate(invalid="ignore"):
      positive_length = candidate_lengths > 0.0
    proportions = np.where(positive_length, 1.0, 0.0)
    boundary_mask = ~is_covered & positive_length
    if np.any(boundary_mask):
      proportions[boundary_mask] = _compute_boundary_proportions(
          boundary_lines=candidate_lines[boundary_mask],
          boundary_polygons=candidate_polygons[boundary_mask],
          original_lengths=candidate_lengths[boundary_mask],
      )

    return _drop_zero_proportions(
        segment_indices=segment_indices,
        polygon_indices=polygon_indices,
        proportions=proportions,
    )


def _empty_intersection_batch() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
  """Returns empty (seg_indices, poly_indices, proportions) arrays."""
  empty_i = np.empty(0, dtype=np.int64)
  empty_f = np.empty(0, dtype=np.float64)
  return empty_i, empty_i, empty_f


def _drop_zero_proportions(
    *,
    segment_indices: np.ndarray,
    polygon_indices: np.ndarray,
    proportions: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
  """Removes segment/polygon pairs whose length proportion is zero.

  Args:
    segment_indices: Segment index of each pair.
    polygon_indices: Polygon index of each pair.
    proportions: Length proportion of each pair.

  Returns:
    The `(seg_indices, poly_indices, proportions)` arrays restricted to pairs
    with a positive proportion; the inputs are returned as-is if none are
    dropped.
  """
  valid_mask = proportions > 0.0
  if np.all(valid_mask):
    return segment_indices, polygon_indices, proportions
  return (
      segment_indices[valid_mask],
      polygon_indices[valid_mask],
      proportions[valid_mask],
  )


def _compute_covered_mask(
    *, candidate_polygons: np.ndarray, candidate_lines: np.ndarray
) -> np.ndarray:
  """Returns a mask of pairs whose polygon fully covers its line.

  If the vectorized predicate fails, every pair is treated as not covered so
  that its proportion is computed from the clipped length instead.

  Args:
    candidate_polygons: 1D object array of projected polygons.
    candidate_lines: 1D object array of projected lines, aligned with
      `candidate_polygons`.

  Returns:
    1D boolean array aligned with the inputs.
  """
  try:
    return shapely.covers(candidate_polygons, candidate_lines)
  except (
      shapely.errors.GEOSException,
      shapely.errors.ShapelyError,
      ValueError,
  ):
    return np.zeros(len(candidate_polygons), dtype=bool)


def _compute_boundary_proportions(
    *,
    boundary_lines: np.ndarray,
    boundary_polygons: np.ndarray,
    original_lengths: np.ndarray,
) -> np.ndarray:
  """Computes clipped-length proportions for boundary-crossing pairs.

  Args:
    boundary_lines: 1D object array of projected lines that are not fully
      covered by their paired polygon.
    boundary_polygons: 1D object array of polygons aligned with
      `boundary_lines`.
    original_lengths: Original segment length for each pair.

  Returns:
    1D float array of proportions in [0, 1]; pairs with a non-positive
    original length get 0.
  """
  clipped_len = _compute_boundary_intersection_lengths(
      boundary_lines=boundary_lines, boundary_polygons=boundary_polygons
  )
  with np.errstate(divide="ignore", invalid="ignore"):
    proportions = np.where(
        original_lengths > 0.0, clipped_len / original_lengths, 0.0
    )
  return np.clip(proportions, 0.0, 1.0)


# Minimum seconds between repeated GEOS-failure warnings from the per-pair
# fallbacks below.
_GEOS_FAILURE_LOG_INTERVAL_SECONDS = 60


def _pair_intersects(polygon: Any, line: Any) -> bool:
  """Tests one polygon-line pair, retrying on `make_valid` copies on error."""
  try:
    return polygon.intersects(line)
  except (
      shapely.errors.GEOSException,
      shapely.errors.ShapelyError,
      ValueError,
  ):
    pass
  try:
    return shapely.make_valid(polygon).intersects(shapely.make_valid(line))
  except (
      shapely.errors.GEOSException,
      shapely.errors.ShapelyError,
      ValueError,
  ) as e:
    # Rate-limited: an invalid polygon would otherwise log once per segment
    # in the reduce hot path.
    logging.log_every_n_seconds(
        logging.WARNING,
        "Dropping polygon-line pair: intersects() failed even after"
        " make_valid (%s); projected line bounds %s",
        _GEOS_FAILURE_LOG_INTERVAL_SECONDS,
        e,
        line.bounds,
    )
    return False


def _filter_intersecting_pairs(
    *, candidate_polygons: np.ndarray, candidate_lines: np.ndarray
) -> np.ndarray:
  """Returns boolean mask of polygon-line candidate pairs that intersect."""
  try:
    return shapely.intersects(candidate_polygons, candidate_lines)
  except (
      shapely.errors.GEOSException,
      shapely.errors.ShapelyError,
      ValueError,
  ):
    hit = np.zeros(len(candidate_polygons), dtype=bool)
    for idx_h, (polygon, line) in enumerate(
        zip(candidate_polygons, candidate_lines, strict=True)
    ):
      hit[idx_h] = _pair_intersects(polygon, line)
    return hit


def _pair_intersection_length(line: Any, polygon: Any) -> float:
  """Clips one line to one polygon, retrying on `make_valid` copies on error."""
  try:
    return line.intersection(polygon).length
  except (
      shapely.errors.GEOSException,
      shapely.errors.ShapelyError,
      ValueError,
  ):
    pass
  try:
    return (
        shapely.make_valid(line).intersection(shapely.make_valid(polygon))
    ).length
  except (
      shapely.errors.GEOSException,
      shapely.errors.ShapelyError,
      ValueError,
  ) as e:
    logging.log_every_n_seconds(
        logging.WARNING,
        "Attributing zero length to polygon-line pair: intersection() failed"
        " even after make_valid (%s); projected line bounds %s",
        _GEOS_FAILURE_LOG_INTERVAL_SECONDS,
        e,
        line.bounds,
    )
    return 0.0


def _compute_boundary_intersection_lengths(
    *, boundary_lines: np.ndarray, boundary_polygons: np.ndarray
) -> np.ndarray:
  """Computes intersection lengths for boundary-crossing pairs."""
  try:
    clipped = shapely.intersection(boundary_lines, boundary_polygons)
    return shapely.length(clipped)
  except (
      shapely.errors.GEOSException,
      shapely.errors.ShapelyError,
      ValueError,
  ):
    clipped_len = np.zeros(len(boundary_lines), dtype=np.float64)
    for idx_b, (line, polygon) in enumerate(
        zip(boundary_lines, boundary_polygons, strict=True)
    ):
      clipped_len[idx_b] = _pair_intersection_length(line, polygon)
    return clipped_len
