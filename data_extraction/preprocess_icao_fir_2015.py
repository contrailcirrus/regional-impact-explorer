"""Preprocesses `ICAO_FIR_2015.zip` (`ICAOFIRFULallDensify`) into GeoJSON.

Reads the ESRI Shapefile (.shp and .dbf), drops blank-ICAO polygons, dissolves
multi-part sectors sharing the same `(ICAOCODE, clean_fir_name(FIR_NAME,
ICAOCODE))` into a `Polygon` (single outer ring) or `MultiPolygon` (multiple
outer rings), resolves controlling country ISO-2 codes from `Country_So`,
`F2LETTER`, and `ICAOCODE` (`EGGX` override and `ICAOCODE[:2]` fallback),
classifies each dissolved FIR group as international when any sub-record's
`Country_So`, cleaned `FIR_NAME`, or `NOM_COMP` mentions `OCEANIC` or `ARCTIC`
(domestic otherwise), and simplifies polygon rings at a 500-meter tolerance
while preserving geodesic areas.

The output has the properties the pipeline requires on every FIR (`fir_icao`,
`firname`, `fir_to_country_iso2s`, `type`, `areasqkm`). All ICAO-2015-specific
knowledge lives here; `--firs_path` accepts any file with those properties.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
import itertools
import json
import math
import os
import struct
import sys
import types
from typing import Any, BinaryIO, Final, NamedTuple

from absl import logging
import shapely
import shapely.geometry

from common import constants
from data_extraction import geo_utils

# The tables below describe the ICAO 2015 release only. They translate its
# `Country_So` and `F2LETTER` attributes into ISO 3166-1 alpha-2 codes, which
# this converter writes into `fir_to_country_iso2s`. The pipeline itself reads
# only that property and never consults these tables.
_COUNTRY_SO_TO_ISO2: Final[Mapping[str, str]] = types.MappingProxyType({
    "Afghanistan": "AF",
    "Albania": "AL",
    "Algeria": "DZ",
    "Angola": "AO",
    "Argentina": "AR",
    "Armenia": "AM",
    "Australia": "AU",
    "Austria": "AT",
    "Azerbaijan": "AZ",
    "Bahrain": "BH",
    "Bangladesh": "BD",
    "Belarus": "BY",
    "Belgium": "BE",
    "Bolivia": "BO",
    "Bosnia and Herzegovina": "BA",
    "Botswana": "BW",
    "Brazil": "BR",
    "Bulgaria": "BG",
    "COCESNA": "GT, SV, HN, NI, CR, BZ",
    "Cambodia": "KH",
    "Canada": "CA",
    "Cape Verne": "CV",
    "Chad": "TD",
    "Chile": "CL",
    "China": "CN",
    "Colombia": "CO",
    "Congo": "CG",
    "Croatia": "HR",
    "Cuba": "CU",
    "Curacao": "CW",
    "Cyprus": "CY",
    "Czech Republic": "CZ",
    "Democratic People's Republic of Korea": "KP",
    "Democratic Republic of the Congo": "CD",
    "Denmark": "DK",
    "Dominican Republic": "DO",
    "Ecuador": "EC",
    "Egypt": "EG",
    "Eritrea": "ER",
    "Estonia": "EE",
    "Ethiopia": "ET",
    "Fiji": "FJ",
    "Finland": "FI",
    "France": "FR",
    "French Guiana (France)": "GF",
    "French Polynesia (France)": "PF",
    "Georgia": "GE",
    "Germany": "DE",
    "Ghana": "GH",
    "Greece": "GR",
    "Greenland (Denmark)": "GL",
    "Guyana": "GY",
    "Haiti": "HT",
    "Hungary": "HU",
    "Iceland": "IS",
    "India": "IN",
    "Indonesia": "ID",
    "Iran (Islamic Republic of)": "IR",
    "Iraq": "IQ",
    "Ireland": "IE",
    "Israel": "IL",
    "Italy": "IT",
    "Jamaica": "JM",
    "Japan": "JP",
    "Jordan": "JO",
    "Kazakhstan": "KZ",
    "Kenya": "KE",
    "Kuwait": "KW",
    "Kyrgyzstan": "KG",
    "Lao People's Democratic Republic": "LA",
    "Latvia": "LV",
    "Lebanon": "LB",
    "Libya": "LY",
    "Lithuania": "LT",
    "Madagascar": "MG",
    "Malawi": "MW",
    "Malaysia": "MY",
    "Maldives": "MV",
    "Malta": "MT",
    "Mauritius": "MU",
    "Mexico": "MX",
    "Mongolia": "MN",
    "Morocco": "MA",
    "Mozambique": "MZ",
    "Myanmar": "MM",
    "Namibia": "NA",
    "Nauru": "NR",
    "Nepal": "NP",
    "Netherlands": "NL",
    "New Zealand": "NZ",
    "Niger": "NE",
    "Nigeria": "NG",
    "Norway": "NO",
    "Oman": "OM",
    "Pakistan": "PK",
    "Panama": "PA",
    "Papua New Guinea": "PG",
    "Paraguay": "PY",
    "Peru": "PE",
    "Philippines": "PH",
    "Poland": "PL",
    "Portugal": "PT",
    "Puerto Rico (United States)": "US",
    "Republic of Korea": "KR",
    "Republic of Moldova": "MD",
    "Romania": "RO",
    "Russian Federation": "RU",
    "Saudi Arabia": "SA",
    "Senegal": "SN",
    "Serbia": "RS",
    "Seychelles": "SC",
    "Singapore": "SG",
    "Slovakia": "SK",
    "Slovenia": "SI",
    "Solomon Islands": "SB",
    "Somalia": "SO",
    "South Africa": "ZA",
    "Spain": "ES",
    "Sri Lanka": "LK",
    "Sudan": "SD",
    "Suriname": "SR",
    "Sweden": "SE",
    "Switzerland": "CH",
    "Syrian Arab Republic": "SY",
    "Taiwan (china)": "TW",
    "Tajikistan": "TJ",
    "Thailand": "TH",
    "Trinidad and Tobago": "TT",
    "Tunisia": "TN",
    "Turkey": "TR",
    "Turkmenistan": "TM",
    "Uganda": "UG",
    "Ukraine": "UA",
    "United Arab Emirates": "AE",
    "United Kingdom": "GB",
    "United Republic of Tanzania": "TZ",
    "United States": "US",
    "Uruguay": "UY",
    "Uzbekistan": "UZ",
    "Venezuela": "VE",
    "Viet Nam": "VN",
    "Yemen": "YE",
    "Zambia": "ZM",
    "Zimbabwe": "ZW",
})

_F2LETTER_ISO2_OVERRIDES: Final[Mapping[str, str]] = types.MappingProxyType({
    "LP": "PT",
    "HB": "BI",
    "HR": "RW",
    "LW": "MK",
    "VH": "HK",
    "GL": "LR, SL, GN",
    "GO": "SN, ML, MR, CI, BF",
    "KZ": "US",
    "PA": "US",
})


_DBF_COUNTRY_SO = "Country_So"
_DBF_NOM_COMP = "NOM_COMP"
_DBF_CENTLAT = "centlat"
_DBF_CENTLONG = "centlong"
_SHP_PARTS = "parts"
_SHP_POINTS = "points"
_DBF_ICAOCODE = "ICAOCODE"
_DBF_FIR_NAME = "FIR_NAME"
_DBF_F2LETTER = "F2LETTER"
_DBF_SQKMGEOD = "SQKMgeod"
_DBF_PERIMEKM = "PERIMEkm"
_DBF_REGION = "REGION"
_DBF_FIR_UPPER = "FIR_Upper"
_DBF_FIR_LOWER = "FIR_Lower"


def resolve_country_so_to_iso2(
    country_so: str, f2letter: str = "", icao: str = ""
) -> str:
  """Resolves Country_So and F2LETTER from ICAO_FIR_2015 to ISO-2 code(s)."""
  raw_country_so = (country_so or "").strip()
  icao_prefix = (f2letter or (icao or "")[:2]).strip().upper()
  icao_upper = (icao or "").strip().upper()

  if icao_upper == "EGGX" or (
      icao_prefix == "EG" and "IRELAND" in raw_country_so.upper()
  ):
    return "GB, IE"
  if (iso2 := _F2LETTER_ISO2_OVERRIDES.get(icao_prefix)) is not None:
    return iso2

  cleaned_country_so = raw_country_so
  if cleaned_country_so.lower().startswith("oceanic"):
    cleaned_country_so = cleaned_country_so[7:].strip()

  if (iso2 := _COUNTRY_SO_TO_ISO2.get(cleaned_country_so)) is not None:
    return iso2
  if (iso2 := _COUNTRY_SO_TO_ISO2.get(raw_country_so)) is not None:
    return iso2

  clean_lower = cleaned_country_so.lower()
  for k, iso2_code in _COUNTRY_SO_TO_ISO2.items():
    if k.lower() == clean_lower:
      return iso2_code
  return ""


def derive_fir_type(country_so: str, fir_name: str, nom_comp: str = "") -> str:
  """Derives domestic vs. international classification from shapefile fields."""
  combined = f"{country_so} {fir_name} {nom_comp}".upper()
  if "OCEANIC" in combined or "ARCTIC" in combined:
    return constants.FIR_TYPE_INTERNATIONAL
  return constants.FIR_TYPE_DOMESTIC


class _DbfField(NamedTuple):
  """A DBF field descriptor from the file header.

  Attributes:
    name: Field name.
    ftype: Single-character DBF field type code (e.g. `C`, `N`, `F`).
    length: Field width in bytes within each record.
    decimals: Number of decimal places for numeric fields.
  """

  name: str
  ftype: str
  length: int
  decimals: int


def _read_dbf_encoding(path: str) -> str:
  """Returns the text encoding declared by the DBF's sibling `.cpg` file.

  Args:
    path: Path to the `.dbf` file.

  Returns:
    The `.cpg` encoding name, or `cp1252` when the `.cpg` file is missing,
    unreadable, or empty.
  """
  cpg_path = os.path.splitext(path)[0] + ".cpg"
  encoding = "cp1252"
  if not os.path.exists(cpg_path):
    return encoding
  try:
    with open(cpg_path, "r", encoding="utf-8") as f:
      cpg_enc = f.read().strip()
      if cpg_enc:
        encoding = cpg_enc
  except (OSError, UnicodeDecodeError):
    pass
  return encoding


_DBF_HEADER_BYTES = 32
_DBF_FIELD_DESCRIPTOR_BYTES = 32
_SHP_FILE_HEADER_BYTES = 100
_SHP_FILE_CODE = 9994
_SHP_RECORD_HEADER_BYTES = 8
_SHP_SHAPE_TYPE_BYTES = 4
_SHP_POLYGON_HEADER_BYTES = 44
_SHP_POINT_BYTES = 16
_POLYGON_SHAPE_TYPES = frozenset({5, 15, 25})


def _read_dbf_fields(f: BinaryIO, header_length: int) -> list[_DbfField]:
  """Reads the 32-byte field descriptors that follow the DBF file header.

  Args:
    f: DBF file positioned just after the 32-byte file header.
    header_length: Total header length in bytes, including field descriptors.

  Returns:
    The field descriptors in record order.

  Raises:
    ValueError: If the file ends before `header_length - 1` or a field
      descriptor is truncated.
  """
  fields = []
  while f.tell() < header_length - 1:
    descriptor = f.read(_DBF_FIELD_DESCRIPTOR_BYTES)
    if descriptor and descriptor[0] == 0x0D:
      break
    if len(descriptor) < _DBF_FIELD_DESCRIPTOR_BYTES:
      raise ValueError("Truncated DBF field descriptor")
    name = descriptor[:11].split(b"\x00")[0].decode("ascii", errors="replace")
    fields.append(
        _DbfField(
            name=name,
            ftype=chr(descriptor[11]),
            length=descriptor[16],
            decimals=descriptor[17],
        )
    )
  return fields


def _parse_dbf_value(text: str, field: _DbfField) -> Any:
  """Converts a stripped DBF cell to `float`/`int` for numeric fields.

  Args:
    text: The decoded, whitespace-stripped cell text.
    field: The descriptor of the field the cell belongs to.

  Returns:
    0.0 for blank or `*` numeric cells, a `float` or `int` for other numeric
    cells, and `text` unchanged for non-numeric fields.
  """
  if field.ftype not in ("N", "F"):
    return text
  if not text or text == "*":
    return 0.0
  if field.decimals > 0 or "." in text or "e" in text.lower():
    return float(text)
  return int(text)


def _decode_dbf_record(
    record: bytes, *, fields: Sequence[_DbfField], encoding: str
) -> dict[str, Any]:
  """Decodes one active DBF record into a `{field name: value}` row.

  Args:
    record: The raw record bytes, starting with the deletion flag byte.
    fields: The field descriptors in record order.
    encoding: Text encoding to try first; falls back to UTF-8 with replacement
      characters on decode errors.

  Returns:
    The parsed row.
  """
  offset = 1
  row: dict[str, Any] = {}
  for field in fields:
    raw = record[offset : offset + field.length]
    try:
      text = raw.decode(encoding).strip()
    except UnicodeDecodeError:
      text = raw.decode("utf-8", errors="replace").strip()
    row[field.name] = _parse_dbf_value(text, field)
    offset += field.length
  return row


def read_dbf(
    path: str, *, keep_deleted_placeholders: bool = False
) -> list[dict[str, Any]]:
  """Reads a DBF file into a list of row dictionaries."""
  encoding = _read_dbf_encoding(path)

  with open(path, "rb") as f:
    header = f.read(_DBF_HEADER_BYTES)
    if len(header) < _DBF_HEADER_BYTES:
      raise ValueError(f"Truncated DBF header in {path}")
    number_of_records, header_length, record_length = struct.unpack(
        "<4xIHH20x", header
    )
    if header_length < _DBF_HEADER_BYTES or record_length < 1:
      raise ValueError(f"Invalid DBF header metadata in {path}")
    fields = _read_dbf_fields(f, header_length)
    f.seek(header_length)
    records = []
    for _ in range(number_of_records):
      raw_record = f.read(record_length)
      if len(raw_record) < record_length:
        raise ValueError(f"Truncated DBF record in {path}")
      # In DBF, the first byte of each record is the deletion flag.
      # 0x2A ('*') indicates a deleted record; 0x20 (' ') indicates active.
      if raw_record[0] != 0x2A:
        records.append(
            _decode_dbf_record(raw_record, fields=fields, encoding=encoding)
        )
      elif keep_deleted_placeholders:
        records.append({})
    return records


def _validate_shp_part_offsets(
    parts: Sequence[int], num_points: int, path: str
) -> None:
  """Validates that ESRI polygon part start indices are in bounds and sorted."""
  if not parts:
    return
  if (
      parts[0] != 0
      or parts[-1] >= num_points
      or any(a >= b for a, b in itertools.pairwise(parts))
  ):
    raise ValueError(f"Invalid polygon part offsets {list(parts)} in {path}")


def _parse_shp_record(content: bytes, path: str) -> dict[str, Any]:
  """Parses a single ESRI Shapefile record payload into parts and points."""
  shape_type = struct.unpack("<i", content[:_SHP_SHAPE_TYPE_BYTES])[0]
  if shape_type == 0:
    return {_SHP_PARTS: [], _SHP_POINTS: []}
  if shape_type not in _POLYGON_SHAPE_TYPES:
    raise ValueError(f"Unsupported shapefile shape_type {shape_type} in {path}")
  if len(content) < _SHP_POLYGON_HEADER_BYTES:
    raise ValueError(f"Truncated polygon record header in {path}")
  _, _, _, _, num_parts, num_points = struct.unpack(
      "<4d2i", content[_SHP_SHAPE_TYPE_BYTES:_SHP_POLYGON_HEADER_BYTES]
  )
  if num_parts < 0 or num_points < 0 or (num_points > 0 and num_parts == 0):
    raise ValueError(
        f"Invalid polygon counts (num_parts={num_parts},"
        f" num_points={num_points}) in {path}"
    )
  points_offset = _SHP_POLYGON_HEADER_BYTES + 4 * num_parts
  if len(content) < points_offset + _SHP_POINT_BYTES * num_points:
    raise ValueError(f"Truncated polygon coordinates in {path}")
  parts = struct.unpack(
      f"<{num_parts}i", content[_SHP_POLYGON_HEADER_BYTES:points_offset]
  )
  _validate_shp_part_offsets(parts, num_points, path)
  points = [
      struct.unpack(
          "<2d",
          content[
              points_offset
              + i * _SHP_POINT_BYTES : points_offset
              + (i + 1) * _SHP_POINT_BYTES
          ],
      )
      for i in range(num_points)
  ]
  return {_SHP_PARTS: list(parts), _SHP_POINTS: points}


def _validate_shp_file_header(file_header: bytes, path: str) -> None:
  """Validates the 100-byte ESRI Shapefile file header."""
  if len(file_header) < _SHP_FILE_HEADER_BYTES:
    raise ValueError(f"Truncated shapefile header in {path}")
  file_code = struct.unpack(">i", file_header[:4])[0]
  if file_code != _SHP_FILE_CODE:
    raise ValueError(f"Invalid shapefile file code {file_code} in {path}")
  header_shape_type = struct.unpack("<i", file_header[32:36])[0]
  if header_shape_type not in _POLYGON_SHAPE_TYPES:
    raise ValueError(
        f"Unsupported shapefile header shape_type {header_shape_type} in {path}"
    )


def read_shp(path: str) -> list[dict[str, Any]]:
  """Reads an ESRI Shapefile into a list of shape dictionaries."""
  with open(path, "rb") as f:
    _validate_shp_file_header(f.read(_SHP_FILE_HEADER_BYTES), path)
    shapes = []
    while True:
      record_header = f.read(_SHP_RECORD_HEADER_BYTES)
      if not record_header:
        break
      if len(record_header) < _SHP_RECORD_HEADER_BYTES:
        raise ValueError(f"Truncated shapefile record header in {path}")
      record_number, content_length_words = struct.unpack(">2i", record_header)
      content_bytes = content_length_words * 2
      if record_number <= 0 or content_bytes < _SHP_SHAPE_TYPE_BYTES:
        raise ValueError(
            f"Invalid shapefile record header (record_number={record_number},"
            f" length={content_bytes} bytes) in {path}"
        )
      content = f.read(content_bytes)
      if len(content) < content_bytes:
        raise ValueError(f"Truncated shapefile record payload in {path}")
      shapes.append(_parse_shp_record(content, path))
    return shapes


# Approximate meters per degree of longitude at the equator (scaled by
# cos(latitude)) and per degree of latitude, for local planar distances.
_METERS_PER_DEGREE_LON_AT_EQUATOR = 111320.0
_METERS_PER_DEGREE_LAT = 110574.0


def _round_ring(ring: Sequence[Sequence[float]]) -> list[list[float]]:
  """Returns `ring` as a closed `[lon, lat]` list rounded to 6 decimals."""
  out = [[round(point[0], 6), round(point[1], 6)] for point in ring]
  if out and out[0] != out[-1]:
    out.append(list(out[0]))
  return out


def _project_ring_to_meters(
    ring: Sequence[Sequence[float]],
) -> list[tuple[float, float]]:
  """Unwraps longitudes and projects `[lon, lat]` vertices to planar meters."""
  return [
      (
          lon * _METERS_PER_DEGREE_LON_AT_EQUATOR * math.cos(math.radians(lat)),
          lat * _METERS_PER_DEGREE_LAT,
      )
      for lon, lat in geo_utils.unwrap_ring_lons(ring)
  ]


def _farthest_point_from_chord(
    points_meters: Sequence[tuple[float, float]], *, start: int, end: int
) -> tuple[int, float]:
  """Finds the interior point farthest from the segment `start`-`end`.

  Args:
    points_meters: Ring vertices in planar meters.
    start: Index of the segment's first endpoint.
    end: Index of the segment's last endpoint.

  Returns:
    The index of the farthest point strictly between `start` and `end` and its
    squared distance to the segment, or `(-1, -1.0)` if there is none.
  """
  x1, y1 = points_meters[start]
  x2, y2 = points_meters[end]
  dx = x2 - x1
  dy = y2 - y1
  len_sq = dx * dx + dy * dy
  max_d_sq = -1.0
  max_k = -1
  for k in range(start + 1, end):
    px, py = points_meters[k]
    if len_sq == 0.0:
      d_sq = (px - x1) ** 2 + (py - y1) ** 2
    else:
      projection_factor = max(
          0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len_sq)
      )
      proj_x = x1 + projection_factor * dx
      proj_y = y1 + projection_factor * dy
      d_sq = (px - proj_x) ** 2 + (py - proj_y) ** 2
    if d_sq > max_d_sq:
      max_d_sq = d_sq
      max_k = k
  return max_k, max_d_sq


def _rdp_keep_mask(
    points_meters: Sequence[tuple[float, float]], tol_meters: float
) -> list[bool]:
  """Runs Ramer-Douglas-Peucker on a closed ring split at its midpoint.

  Args:
    points_meters: Ring vertices in planar meters.
    tol_meters: Maximum allowed deviation in meters.

  Returns:
    One flag per vertex; `True` marks vertices kept in the simplified ring.
  """
  keep = [False] * len(points_meters)
  keep[0] = True
  keep[-1] = True
  mid = len(points_meters) // 2
  keep[mid] = True
  stack = [(0, mid), (mid, len(points_meters) - 1)]
  tol_sq = tol_meters * tol_meters

  while stack:
    i, j = stack.pop()
    if j - i <= 1:
      continue
    max_k, max_d_sq = _farthest_point_from_chord(points_meters, start=i, end=j)
    if max_d_sq > tol_sq and max_k != -1:
      keep[max_k] = True
      stack.append((i, max_k))
      stack.append((max_k, j))
  return keep


def simplify_closed_ring(
    ring: Sequence[Sequence[float]], tol_meters: float = 500.0
) -> list[list[float]]:
  """Simplifies a closed `[lon, lat]` ring via Ramer-Douglas-Peucker.

  Args:
    ring: Closed or unclosed sequence of `[lon, lat]` vertices in degrees.
    tol_meters: Maximum perpendicular chord deviation in unwrapped planar meters
      below which intermediate vertices are dropped.

  Returns:
    Simplified closed ring of `[lon, lat]` coordinate pairs rounded to 6 decimal
    places, with longitudes preserved in their original wrapped `[-180, 180]`
    representation, or `[]` if `ring` has fewer than 3 vertices or fewer than 4
    vertices after closing.
  """
  if len(ring) < 3:
    return []
  first_pt = (round(ring[0][0], 6), round(ring[0][1], 6))
  last_pt = (round(ring[-1][0], 6), round(ring[-1][1], 6))
  closed_ring = [*ring, ring[0]] if first_pt != last_pt else list(ring)
  if len(closed_ring) < 4:
    return []
  if len(closed_ring) <= 5 or tol_meters <= 0.0:
    return _round_ring(closed_ring)

  keep = _rdp_keep_mask(_project_ring_to_meters(closed_ring), tol_meters)
  out = [
      [round(closed_ring[index][0], 6), round(closed_ring[index][1], 6)]
      for index, k in enumerate(keep)
      if k
  ]
  if len(out) < 4:
    return _round_ring(closed_ring)
  return out


def signed_ring_area_lonlat(ring: Sequence[Sequence[float]]) -> float:
  """Computes signed planar area using shoelace formula on unwrapped longitudes.

  Longitudes are unwrapped so rings crossing +/-180 are continuous, preventing
  outer rings that cross the antimeridian from having inverted sign and being
  misidentified as holes.

  Args:
    ring: Sequence of (lon, lat) vertex coordinates.

  Returns:
    Positive for clockwise rings (ESRI outer) and negative for
    counter-clockwise rings (ESRI hole).
  """
  if len(ring) < 3:
    return 0.0
  is_closed = (ring[0][0], ring[0][1]) == (ring[-1][0], ring[-1][1])
  closed_ring = ring if is_closed else [*ring, ring[0]]
  if len(closed_ring) < 4:
    return 0.0
  unwrapped = geo_utils.unwrap_ring_lons(closed_ring)
  total = 0.0
  for pt1, pt2 in itertools.pairwise(unwrapped):
    x1, y1 = pt1[0], pt1[1]
    x2, y2 = pt2[0], pt2[1]
    total += (x2 - x1) * (y2 + y1)
  return total / 2.0


class _OuterRing(NamedTuple):
  """An exterior ring with its unwrapped coordinates and assigned holes.

  Attributes:
    ring: Simplified `[lon, lat]` ring as written to GeoJSON.
    unwrapped: `ring` with longitudes unwrapped across the antimeridian.
    holes: Hole rings assigned to this exterior ring.
  """

  ring: list[list[float]]
  unwrapped: list[tuple[float, float]]
  holes: list[list[list[float]]]


class _HoleRing(NamedTuple):
  """An interior ring with its unwrapped coordinates.

  Attributes:
    ring: Simplified `[lon, lat]` ring as written to GeoJSON.
    unwrapped: `ring` with longitudes unwrapped across the antimeridian.
  """

  ring: list[list[float]]
  unwrapped: list[tuple[float, float]]


def _split_rings_by_orientation(
    points: Sequence[tuple[float, float]],
    *,
    parts: Sequence[int],
    tol_meters: float,
) -> tuple[list[_OuterRing], list[_HoleRing]]:
  """Simplifies each ESRI part and classifies it by winding order.

  Args:
    points: All vertices of the ESRI Polygon record.
    parts: Start index of each part, followed by `len(points)`.
    tol_meters: Simplification tolerance in meters.

  Returns:
    The clockwise (outer) rings and counter-clockwise (hole) rings in input
    order. Rings that simplify to fewer than 4 vertices or have zero area are
    dropped.
  """
  outers: list[_OuterRing] = []
  holes: list[_HoleRing] = []
  for start_index, end_index in itertools.pairwise(parts):
    raw_ring = points[start_index:end_index]
    simplified_ring = simplify_closed_ring(raw_ring, tol_meters=tol_meters)
    if len(simplified_ring) < 4:
      continue
    unwrapped = geo_utils.unwrap_ring_lons(simplified_ring)
    area = signed_ring_area_lonlat(simplified_ring)
    if area > 0:
      # Outer ring (ESRI clockwise)
      outers.append(_OuterRing(simplified_ring, unwrapped, []))
    elif area < 0:
      # Inner hole (ESRI counter-clockwise)
      holes.append(_HoleRing(simplified_ring, unwrapped))
  return outers, holes


def _promote_largest_hole(
    holes: Sequence[_HoleRing],
) -> tuple[_OuterRing, list[_HoleRing]]:
  """Treats the largest ring by absolute area as the outer ring.

  Args:
    holes: Non-empty counter-clockwise rings of a record with no clockwise ring.

  Returns:
    The promoted outer ring and the remaining holes, largest first.
  """
  holes_by_area = sorted(
      holes,
      key=lambda hole_entry: abs(signed_ring_area_lonlat(hole_entry[0])),
      reverse=True,
  )
  first_ring, first_unwrapped = holes_by_area[0]
  return _OuterRing(first_ring, first_unwrapped, []), holes_by_area[1:]


def _lon_midpoint(unwrapped: Sequence[tuple[float, float]]) -> float:
  """Returns the midpoint of a ring's unwrapped longitude extent."""
  lons = [lon for lon, _ in unwrapped]
  return 0.5 * (min(lons) + max(lons))


def _valid_unwrapped_polygon(
    unwrapped: Sequence[tuple[float, float]],
) -> shapely.Geometry:
  """Builds a valid Shapely geometry from an unwrapped ring."""
  poly = shapely.geometry.Polygon(unwrapped)
  return poly if poly.is_valid else shapely.make_valid(poly)


def _find_enclosing_outer_holes(
    hole: _HoleRing,
    outer_polys: Sequence[
        tuple[shapely.Geometry, float, list[list[list[float]]]]
    ],
) -> list[list[list[float]]] | None:
  """Finds the `holes` list of the smallest outer ring that covers `hole`.

  Args:
    hole: Interior hole ring to assign.
    outer_polys: Outer ring geometries, longitude midpoints, and hole lists,
      sorted in ascending area order.

  Returns:
    The `holes` list of the innermost enclosing outer ring, or `None` if no
    outer ring covers `hole`.
  """
  hole_poly = _valid_unwrapped_polygon(hole.unwrapped)
  hole_area = hole_poly.area
  hole_rep = hole_poly.representative_point()
  if hole_area <= 0.0 or hole_rep.is_empty:
    return None
  hole_midpoint = _lon_midpoint(hole.unwrapped)
  for poly_ext, ext_mid, outer_holes in outer_polys:
    if poly_ext.area <= hole_area:
      continue
    k = round((ext_mid - hole_midpoint) / 360.0)
    hole_point = shapely.geometry.Point(hole_rep.x + 360.0 * k, hole_rep.y)
    if poly_ext.covers(hole_point):
      return outer_holes
  return None


def _assign_holes_to_outers(
    outers: Sequence[_OuterRing], holes: Sequence[_HoleRing]
) -> None:
  """Appends each hole to the smallest outer ring that covers it, in place.

  The hole's representative point is shifted by a multiple of 360 degrees
  towards each outer ring's longitude midpoint before the containment test.
  Outer rings are checked in ascending area order so holes inside nested islands
  bind to the innermost enclosing shell; holes covered by no outer ring are
  omitted rather than attached to an arbitrary shell.

  Args:
    outers: Non-empty outer rings; their `holes` lists are mutated.
    holes: Hole rings to assign.
  """
  if not holes:
    return
  outer_polys = sorted(
      (
          (
              _valid_unwrapped_polygon(outer.unwrapped),
              _lon_midpoint(outer.unwrapped),
              outer.holes,
          )
          for outer in outers
      ),
      key=lambda item: item[0].area,
  )
  for hole in holes:
    target_holes = _find_enclosing_outer_holes(hole, outer_polys)
    if target_holes is not None:
      target_holes.append(hole.ring)


def shape_to_polygons(
    shape: Mapping[str, Any], tol_meters: float = 500.0
) -> list[list[list[list[float]]]]:
  """Converts an ESRI Polygon record into GeoJSON Polygon coordinate arrays.

  Assigns holes to outer rings based on geometric containment in unwrapped
  coordinate space rather than assuming holes immediately follow their
  respective outer ring.

  Args:
    shape: Dictionary with 'parts' and 'points' from an ESRI Shapefile record.
    tol_meters: Simplification tolerance in meters.

  Returns:
    List of GeoJSON Polygon coordinate lists (each with exterior and holes).
  """
  points = shape.get(_SHP_POINTS)
  if not points:
    return []
  parts = list(shape.get(_SHP_PARTS, [])) + [len(points)]
  outers, holes = _split_rings_by_orientation(
      points, parts=parts, tol_meters=tol_meters
  )

  if not outers:
    if not holes:
      return []
    # If no ring was clockwise, treat the largest ring by absolute area
    # as outer.
    largest, holes = _promote_largest_hole(holes)
    outers.append(largest)

  _assign_holes_to_outers(outers, holes)
  return [[outer.ring] + outer.holes for outer in outers]


def _group_shapefile_records_by_fir(
    *,
    records: Sequence[Mapping[str, Any]],
    shapes: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], list[tuple[Mapping[str, Any], Mapping[str, Any]]]]:
  """Groups aligned DBF records and SHP geometries by `(icao, fir_name)`."""
  grouped: dict[
      tuple[str, str], list[tuple[Mapping[str, Any], Mapping[str, Any]]]
  ] = defaultdict(list)
  for rec, shp in zip(records, shapes, strict=True):
    icao = str(rec.get(_DBF_ICAOCODE) or "").strip().upper()
    if not icao:
      continue
    fir_name = constants.clean_fir_name(str(rec.get(_DBF_FIR_NAME) or ""), icao)
    grouped[(icao, fir_name)].append((rec, shp))
  return grouped


def _build_geojson_polygon_geometry(
    all_polygons: Sequence[Sequence[Sequence[Sequence[float]]]],
) -> dict[str, Any]:
  """Constructs a Polygon or MultiPolygon GeoJSON geometry dictionary."""
  if not all_polygons:
    raise ValueError(
        "Cannot build GeoJSON Polygon geometry from empty polygon list."
    )
  if len(all_polygons) == 1:
    return {
        constants.KEY_TYPE: constants.GEOJSON_POLYGON,
        constants.GEOJSON_COORDINATES: all_polygons[0],
    }
  return {
      constants.KEY_TYPE: constants.GEOJSON_MULTIPOLYGON,
      constants.GEOJSON_COORDINATES: list(all_polygons),
  }


def _select_country_so(
    items_sorted: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> str:
  """Returns the first non-blank, non-'Oceanic' `Country_So` by area.

  Args:
    items_sorted: `(record, shape)` pairs sorted by geodesic area descending.

  Returns:
    The selected `Country_So`, or the largest record's (possibly blank or
    'Oceanic') value when no other candidate exists.
  """
  best_country_so = str(items_sorted[0][0].get(_DBF_COUNTRY_SO) or "").strip()
  for record, _ in items_sorted:
    cs_candidate = str(record.get(_DBF_COUNTRY_SO) or "").strip()
    if cs_candidate and cs_candidate.lower() != "oceanic":
      return cs_candidate
  return best_country_so


def _derive_group_fir_type(
    items: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    fir_name: str,
) -> str:
  """Returns 'international' if any item is international, else 'domestic'."""
  any_oceanic = any(
      derive_fir_type(
          str(record.get(_DBF_COUNTRY_SO) or ""),
          fir_name,
          str(record.get(_DBF_NOM_COMP) or ""),
      )
      == constants.FIR_TYPE_INTERNATIONAL
      for record, _ in items
  )
  return (
      constants.FIR_TYPE_INTERNATIONAL
      if any_oceanic
      else constants.FIR_TYPE_DOMESTIC
  )


def _build_dissolved_fir_properties(
    *,
    fid: int,
    icao: str,
    fir_name: str,
    items_sorted: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> dict[str, Any]:
  """Builds the GeoJSON properties for a dissolved (ICAO, FIR name) group.

  Args:
    fid: 1-based feature id.
    icao: The group's ICAO code.
    fir_name: The group's cleaned FIR name.
    items_sorted: `(record, shape)` pairs sorted by geodesic area descending;
      the first record supplies the per-FIR metadata.

  Returns:
    The feature properties, including the pipeline-required `fir_icao`,
    `firname`, `fir_to_country_iso2s`, `type`, and `areasqkm`.
  """
  primary_rec = items_sorted[0][0]
  best_country_so = _select_country_so(items_sorted)
  f2letter = str(primary_rec.get(_DBF_F2LETTER) or icao[:2]).strip().upper()
  iso2s = resolve_country_so_to_iso2(best_country_so, f2letter, icao)
  total_area_sq_km = sum(
      float(record.get(_DBF_SQKMGEOD) or 0.0) for record, _ in items_sorted
  )
  total_perim_km = sum(
      float(record.get(_DBF_PERIMEKM) or 0.0) for record, _ in items_sorted
  )
  return {
      "fid": fid,
      constants.KEY_FIR_ID: constants.make_fir_id(icao, fir_name),
      constants.KEY_FIR_ICAO: icao,
      constants.KEY_FIRNAME: fir_name,
      "nom_comp": str(primary_rec.get(_DBF_NOM_COMP) or "").strip(),
      "region": str(primary_rec.get(_DBF_REGION) or "").strip(),
      "kind": "FIR",
      constants.KEY_TYPE: _derive_group_fir_type(items_sorted, fir_name),
      "country_so": best_country_so,
      "f2letter": f2letter,
      constants.KEY_FIR_TO_COUNTRY_ISO2S: iso2s,
      constants.KEY_AREASQKM: round(total_area_sq_km, 3),
      "perimekm": round(total_perim_km, 3),
      _DBF_CENTLAT: round(float(primary_rec.get(_DBF_CENTLAT) or 0.0), 6),
      _DBF_CENTLONG: round(float(primary_rec.get(_DBF_CENTLONG) or 0.0), 6),
      "fir_upper": str(primary_rec.get(_DBF_FIR_UPPER) or "").strip(),
      "fir_lower": str(primary_rec.get(_DBF_FIR_LOWER) or "").strip(),
  }


def _build_dissolved_fir_feature(
    *,
    fid: int,
    icao: str,
    fir_name: str,
    items: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    tol_meters: float,
) -> dict[str, Any] | None:
  """Builds a dissolved GeoJSON Feature, or None if all shapes are empty."""
  # Sort sub-records by geodesic area descending so dominant polygon governs
  # metadata (e.g. UTTR Uzbekistan over exclave, KZAK Oceanic US over
  # 'Oceanic').
  items_sorted = sorted(
      items,
      key=lambda item: float(item[0].get(_DBF_SQKMGEOD) or 0.0),
      reverse=True,
  )
  all_polygons: list[list[list[list[float]]]] = []
  for _, shp in items_sorted:
    polygons = shape_to_polygons(shp, tol_meters=tol_meters)
    if polygons:
      all_polygons.extend(polygons)
  if not all_polygons:
    return None

  properties = _build_dissolved_fir_properties(
      fid=fid, icao=icao, fir_name=fir_name, items_sorted=items_sorted
  )
  return {
      constants.KEY_TYPE: constants.GEOJSON_FEATURE,
      constants.GEOJSON_PROPERTIES: properties,
      constants.GEOJSON_GEOMETRY: _build_geojson_polygon_geometry(all_polygons),
  }


def convert_shapefile_to_geojson(
    shp_path: str, dbf_path: str, tol_meters: float = 500.0
) -> dict[str, Any]:
  """Converts `ICAOFIRFULallDensify` shapefile into dissolved GeoJSON."""
  records = read_dbf(dbf_path, keep_deleted_placeholders=True)
  shapes = read_shp(shp_path)

  if len(records) != len(shapes):
    raise ValueError(
        f"Record count mismatch between DBF ({len(records)}) and SHP"
        f" ({len(shapes)})"
    )

  grouped = _group_shapefile_records_by_fir(records=records, shapes=shapes)
  features: list[dict[str, Any]] = []
  for (icao, fir_name), items in sorted(grouped.items()):
    feature = _build_dissolved_fir_feature(
        fid=len(features) + 1,
        icao=icao,
        fir_name=fir_name,
        items=items,
        tol_meters=tol_meters,
    )
    if feature is not None:
      features.append(feature)
  return {
      constants.KEY_TYPE: constants.GEOJSON_FEATURE_COLLECTION,
      constants.GEOJSON_FEATURES: features,
  }


def main(argv: Sequence[str]) -> None:
  """Converts ICAO FIR shapefile and writes canonical GeoJSON."""
  if len(argv) < 4:
    raise SystemExit(
        "Usage: preprocess_icao_fir_2015.py <shp_path> <dbf_path> <out_geojson>"
    )
  geojson = convert_shapefile_to_geojson(argv[1], argv[2], tol_meters=500.0)
  with open(argv[3], "w", encoding="utf-8") as f:
    json.dump(geojson, f, separators=(",", ":"))
  logging.info(
      "Wrote %d features to %s",
      len(geojson[constants.GEOJSON_FEATURES]),
      argv[3],
  )


if __name__ == "__main__":
  main(sys.argv)
