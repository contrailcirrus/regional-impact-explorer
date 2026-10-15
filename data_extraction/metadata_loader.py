"""Loads airport mappings, boundary GeoJSON, and country baseline stats."""

from collections.abc import Iterable, Mapping, MutableMapping, Sequence
import csv
import dataclasses
import importlib.resources
import json
import os
from typing import Any, NamedTuple
from absl import logging
import pycountry
from common import constants
from common import fs
from data_extraction import geo_utils


_COL_GPS_CODE = "gps_code"
_COL_IDENT = "ident"
_COL_IATA_CODE = "iata_code"
_COL_ISO_COUNTRY = "iso_country"

_NE_KEY_NAME = "NAME"
_NE_KEY_ISO_A2 = "ISO_A2"
_NE_KEY_ISO_A3 = "ISO_A3"

_COL_COUNTRY_CODE = "Country Code"
_COL_YEAR = "Year"
_COL_VALUE = "Value"

_CW_KEY_DATA = "data"
_CW_KEY_SECTOR = "sector"
_CW_KEY_GAS = "gas"
_CW_KEY_DATA_SOURCE = "data_source"
_CW_KEY_ISO_CODE3 = "iso_code3"
_CW_KEY_EMISSIONS = "emissions"
_CW_KEY_YEAR = "year"
_CW_KEY_VALUE = "value"
_CW_SECTOR_TOTAL_EXCL_LULUCF = "Total excluding LULUCF"
_CW_GAS_ALL_GHG = "All GHG"
_CW_SOURCE_CLIMATE_WATCH = "Climate Watch"


class _YearValue(NamedTuple):
  """Observation year and numeric baseline value for an ISO-3 country."""

  year: int
  value: float


def _path_exists(path: str | None) -> bool:
  """Checks whether `path` exists via `fs`, falling back to `os.path`.

  Errors from the existence check propagate: an unreadable input must not be
  reported as a missing one.

  Args:
    path: Input path, or None.

  Returns:
    True if `path` is set and exists.
  """
  if not path:
    return False
  return fs.exists(path) or os.path.exists(path)


def _read_airports_csv(source_path: str) -> dict[str, str]:
  """Parses an OurAirports-style CSV into an airport-to-country mapping.

  The airport code is the first non-empty of `gps_code`, `ident` and
  `iata_code`; rows without a code or `iso_country` are skipped.

  Args:
    source_path: Path to the CSV file.

  Returns:
    Mapping of uppercase airport code to uppercase ISO-2 country code.
  """
  airport_to_country: dict[str, str] = {}
  with fs.open_file(source_path, "r") as f:
    reader = csv.DictReader(f)
    for row in reader:
      icao = (
          row.get(_COL_GPS_CODE)
          or row.get(_COL_IDENT)
          or row.get(_COL_IATA_CODE)
      )
      country_iso = row.get(_COL_ISO_COUNTRY)
      if icao and country_iso:
        airport_to_country[icao.strip().upper()] = country_iso.strip().upper()
  return airport_to_country


def load_airports_mapping(source_path: str) -> dict[str, str]:
  """Loads airport ICAO to ISO2 country code mapping from CSV.

  Args:
    source_path: Path to OurAirports CSV mapping file.

  Returns:
    Mapping of uppercase airport ICAO code to ISO-2 country code.

  Raises:
    RuntimeError: If the file does not exist, cannot be parsed, or yields zero
      airport mappings.
  """
  if not source_path or not _path_exists(source_path):
    raise RuntimeError(f"Airports mapping file not found: {source_path!r}")

  try:
    airport_to_country = _read_airports_csv(source_path)
    logging.info(
        "Loaded %d airport mappings from %s",
        len(airport_to_country),
        source_path,
    )
  except (OSError, fs.Error, csv.Error, ValueError) as e:
    raise RuntimeError(
        f"Could not load airports mapping from {source_path!r}: {e}"
    ) from e

  if not airport_to_country:
    raise RuntimeError(
        f"Airports mapping file {source_path!r} yielded 0 airport entries."
    )

  return airport_to_country


_MISSING = object()


def _normalize_country_properties(properties: MutableMapping[str, Any]) -> None:
  """Normalizes one country feature's properties in place.

  Natural Earth uppercase keys (`NAME`, `ISO_A2`, `ISO_A3`) are removed and
  folded into lowercase `name`, `iso_a2` and `iso_a3`, with existing lowercase
  values taking precedence. ISO codes are upper-cased. `iso_a3` is only written
  when a value or either spelling of the key was present.

  Args:
    properties: The feature's `properties` mapping; modified in place.
  """
  raw_name = properties.pop(_NE_KEY_NAME, None)
  raw_iso2 = properties.pop(_NE_KEY_ISO_A2, None)
  raw_iso3 = properties.pop(_NE_KEY_ISO_A3, None)
  properties[constants.KEY_NAME] = str(
      properties.get(constants.KEY_NAME) or raw_name or ""
  ).strip()
  properties[constants.KEY_ISO_A2] = (
      str(properties.get(constants.KEY_ISO_A2) or raw_iso2 or "")
      .strip()
      .upper()
  )
  existing_iso3 = properties.get(constants.KEY_ISO_A3, _MISSING)
  iso3_val = None if existing_iso3 is _MISSING else existing_iso3
  iso_a3 = str(iso3_val or raw_iso3 or "").strip().upper()
  if iso_a3 or existing_iso3 is not _MISSING or raw_iso3 is not None:
    properties[constants.KEY_ISO_A3] = iso_a3


def _normalize_fir_properties(properties: MutableMapping[str, Any]) -> None:
  """Normalizes one FIR feature's properties in place and derives `fir_id`.

  Upper-cases `fir_icao` and `fir_to_country_iso2s`, cleans `firname`,
  lower-cases `type`, and sets `fir_id` from the ICAO code and cleaned name.

  Args:
    properties: The feature's `properties` mapping; modified in place.
  """
  icao = str(properties.get(constants.KEY_FIR_ICAO) or "").strip().upper()
  name = constants.clean_fir_name(
      str(properties.get(constants.KEY_FIRNAME) or "")
  )
  properties[constants.KEY_FIR_ID] = constants.make_fir_id(icao, name)
  properties[constants.KEY_FIR_ICAO] = icao
  properties[constants.KEY_FIRNAME] = name
  properties[constants.KEY_FIR_TO_COUNTRY_ISO2S] = (
      str(properties.get(constants.KEY_FIR_TO_COUNTRY_ISO2S) or "")
      .strip()
      .upper()
  )
  properties[constants.KEY_TYPE] = (
      str(properties.get(constants.KEY_TYPE) or "").strip().lower()
  )


def _read_geojson_features(path: str) -> list[dict[str, Any]]:
  """Returns the `features` list of the GeoJSON at `path`, `[]` if absent."""
  if not _path_exists(path):
    return []
  with fs.open_file(path, "r") as f:
    data = json.load(f)
  return data.get(constants.GEOJSON_FEATURES) or []


def load_country_geojson(
    custom_path: str | None = None,
) -> list[dict[str, Any]]:
  """Loads world country boundaries GeoJSON features and normalizes properties.

  There is no bundled fallback: the caller must supply the boundaries (via
  `--country_geojson_path`), and the normalized features from the active stage
  are published alongside the metrics as `countries.geojson`.
  Upstream Natural Earth uppercase property names (`NAME`, `ISO_A2`, `ISO_A3`)
  are normalized to lowercase (`name`, `iso_a2`, `iso_a3`).

  Args:
    custom_path: Path to a GeoJSON FeatureCollection whose features carry
      `iso_a2`/`ISO_A2` and `name`/`NAME` properties (e.g. Natural Earth admin-0
      countries). If `None` or empty, raises `RuntimeError`.

  Returns:
    The file's normalized `features` list.

  Raises:
    RuntimeError: If no path is given, or the file is missing, unreadable, or
      contains zero features.
  """
  path = (custom_path or "").strip()
  if not path:
    raise RuntimeError(
        "No country boundaries configured; pass --country_geojson_path."
    )

  if not _path_exists(path):
    raise RuntimeError(
        f"Failed to load non-empty country GeoJSON features from {path!r}: "
        "file does not exist"
    )

  error = ""
  try:
    features = _read_geojson_features(path)
    if not features:
      error = "0 features"
    else:
      for feat in features:
        _normalize_country_properties(
            feat.setdefault(constants.GEOJSON_PROPERTIES, {})
        )
      logging.info("Loaded %d country features from %s", len(features), path)
      return features
  except (OSError, fs.Error, json.JSONDecodeError, ValueError) as e:
    logging.warning(
        "Failed to load country geojson from %s: %s", path, e, exc_info=True
    )
    error = str(e)

  raise RuntimeError(
      f"Failed to load non-empty country GeoJSON features from {path!r}: "
      f"{error}"
  )


def load_firs_geojson(source_path: str) -> list[dict[str, Any]]:
  """Loads FIR / UIR boundaries and normalizes their properties.

  Every feature must carry its own `fir_icao`, `firname`,
  `fir_to_country_iso2s` and `type`; `areasqkm` is optional. Nothing is looked
  up or inferred: there is no mapping file, no embedded country table and no
  name-based type guess. Values are only normalized (ICAO and ISO codes
  upper-cased, the name cleaned, the type lower-cased) and `fir_id` is derived
  from the ICAO code and name. Missing or invalid values are left for
  `validate_fir_features` to report.

  Args:
    source_path: Path to the FIR GeoJSON (`--firs_path`).

  Returns:
    The normalized features.

  Raises:
    RuntimeError: If no path is given, or the file is missing, unreadable, or
      contains zero features.
  """
  path = (source_path or "").strip()
  if not path:
    raise RuntimeError("No FIR boundaries configured; pass --firs_path.")

  if not _path_exists(path):
    raise RuntimeError(
        f"Failed to load non-empty FIR GeoJSON features from {path!r}: "
        "file does not exist"
    )

  error = ""
  try:
    features = _read_geojson_features(path)
    if not features:
      error = "0 features"
    else:
      for feat in features:
        _normalize_fir_properties(
            feat.setdefault(constants.GEOJSON_PROPERTIES, {})
        )
      logging.info("Loaded %d FIR features from %s", len(features), path)
      return features
  except (
      OSError,
      fs.Error,
      json.JSONDecodeError,
      KeyError,
      ValueError,
  ) as e:
    logging.warning(
        "Failed to load FIR geojson from %s: %s", path, e, exc_info=True
    )
    error = str(e)

  raise RuntimeError(
      f"Failed to load non-empty FIR GeoJSON features from {path!r}: {error}"
  )


_FIR_TYPES = (constants.FIR_TYPE_DOMESTIC, constants.FIR_TYPE_INTERNATIONAL)
_FIR_GEOMETRY_TYPES = (
    constants.GEOJSON_POLYGON,
    constants.GEOJSON_MULTIPOLYGON,
)
_MAX_REPORTED_FIR_PROBLEMS = 20


def _fir_area_problem(label: str, properties: Mapping[str, Any]) -> str | None:
  """Returns a problem message if a present `areasqkm` is not positive.

  Args:
    label: Human-readable feature label used as the message prefix.
    properties: The feature's normalized properties.

  Returns:
    The problem message, or `None` if `areasqkm` is absent or positive.
  """
  # Absent areas are fine: SpatialIndex computes them from the geometry.
  raw_area = properties.get(constants.KEY_AREASQKM)
  if raw_area in (None, ""):
    return None
  try:
    area = float(raw_area)
  except (TypeError, ValueError):
    area = 0.0
  if not area > 0.0:
    return f"{label}: non-positive areasqkm {raw_area!r}"
  return None


def _fir_attribute_problems(label: str, feat: Mapping[str, Any]) -> list[str]:
  """Checks one FIR feature's attributes and geometry, excluding `fir_id`.

  Args:
    label: Human-readable feature label used as the message prefix.
    feat: A normalized FIR GeoJSON feature.

  Returns:
    Problem messages in check order; empty if the feature is valid.
  """
  problems: list[str] = []
  properties = feat.get(constants.GEOJSON_PROPERTIES) or {}
  if not properties.get(constants.KEY_FIR_ICAO):
    problems.append(f"{label}: empty fir_icao")
  firname = str(properties.get(constants.KEY_FIRNAME) or "")
  if not firname:
    problems.append(f"{label}: empty firname")
  elif firname.startswith("FIR "):
    problems.append(f"{label}: firname not cleaned: {firname!r}")
  if not properties.get(constants.KEY_FIR_TO_COUNTRY_ISO2S):
    problems.append(f"{label}: no country mapping (fir_to_country_iso2s)")
  if properties.get(constants.KEY_TYPE) not in _FIR_TYPES:
    problems.append(
        f"{label}: invalid type {properties.get(constants.KEY_TYPE)!r}"
    )
  geometry = feat.get(constants.GEOJSON_GEOMETRY) or {}
  if geometry.get(constants.KEY_TYPE) not in _FIR_GEOMETRY_TYPES or not (
      geometry.get(constants.GEOJSON_COORDINATES)
  ):
    problems.append(
        f"{label}: geometry is not a non-empty Polygon/MultiPolygon"
    )
  area_problem = _fir_area_problem(label, properties)
  if area_problem:
    problems.append(area_problem)
  return problems


def validate_fir_features(
    features: Sequence[Mapping[str, Any]], expected_count: int = 0
) -> None:
  """Checks normalized FIR features before any flight data is touched.

  Runs on the output of `load_firs_geojson`. A FIR that fails here would
  otherwise surface much later as a silently dropped or misattributed row in
  the published metrics.

  Args:
    features: Features as returned by `load_firs_geojson`.
    expected_count: If positive, the exact number of features required. Pins a
      known dataset so a truncated or swapped input fails fast.

  Raises:
    ValueError: Listing every problem found (capped).
  """
  if not features:
    raise ValueError("FIR input contains no features.")

  problems: list[str] = []
  if expected_count > 0 and len(features) != expected_count:
    problems.append(
        f"expected {expected_count} FIR features, got {len(features)}"
    )

  seen_fir_ids: set[str] = set()
  for index, feat in enumerate(features):
    properties = feat.get(constants.GEOJSON_PROPERTIES) or {}
    fir_id = str(properties.get(constants.KEY_FIR_ID) or "")
    label = f"feature {index} ({fir_id or 'no fir_id'})"
    if not fir_id:
      problems.append(f"{label}: empty fir_id")
    elif fir_id in seen_fir_ids:
      problems.append(f"{label}: duplicate fir_id")
    seen_fir_ids.add(fir_id)
    problems.extend(_fir_attribute_problems(label, feat))

  if problems:
    shown = problems[:_MAX_REPORTED_FIR_PROBLEMS]
    more = len(problems) - len(shown)
    raise ValueError(
        f"FIR input failed {len(problems)} integrity check(s): "
        + "; ".join(shown)
        + (f"; ... and {more} more" if more else "")
    )


@dataclasses.dataclass(frozen=True)
class Boundaries:
  """Container for loaded and indexed country and FIR boundaries."""

  country_features: list[dict[str, Any]]
  fir_features: list[dict[str, Any]]
  country_index: geo_utils.SpatialIndex
  fir_index: geo_utils.SpatialIndex

  @property
  def country_names(self) -> dict[str, str]:
    """Mapping from ISO-2 country code to canonical country name."""
    return self.country_index.names_by_id

  @property
  def country_areas(self) -> dict[str, float]:
    """Mapping from ISO-2 country code to land area in square kilometers."""
    return self.country_index.areas_by_id

  @property
  def fir_areas(self) -> dict[tuple[str, str], float]:
    """Mapping from `(firname, fir_icao)` to FIR area in square kilometers."""
    return self.fir_index.areas_by_name_and_id


def load_boundaries(
    firs_path: str,
    country_geojson_path: str | None = None,
    expected_fir_count: int = 0,
) -> Boundaries:
  """Loads, validates, and indexes country and FIR boundaries.

  Args:
    firs_path: Path to the FIR GeoJSON file (`--firs_path`).
    country_geojson_path: Optional path to country GeoJSON boundaries
      (`--country_geojson_path`).
    expected_fir_count: If positive, exact number of expected FIR features.

  Returns:
    A frozen Boundaries instance containing features and spatial indexes.

  Raises:
    RuntimeError: If either country or FIR boundaries are empty or fail to
    index.
    ValueError: If FIR features fail validation checks.
  """
  country_features = load_country_geojson(country_geojson_path)
  country_index = geo_utils.SpatialIndex(
      country_features,
      id_key=constants.KEY_ISO_A2,
      name_key=constants.KEY_NAME,
  )
  if country_index.count == 0:
    raise RuntimeError(
        "Country SpatialIndex initialized with 0 polygons; refusing to run "
        "with empty country boundaries."
    )

  fir_features = load_firs_geojson(firs_path)
  validate_fir_features(fir_features, expected_count=expected_fir_count)
  fir_index = geo_utils.SpatialIndex(
      fir_features,
      id_key=constants.KEY_FIR_ICAO,
      name_key=constants.KEY_FIRNAME,
      area_property=constants.KEY_AREASQKM,
  )
  if fir_index.count == 0:
    raise RuntimeError(
        "FIR SpatialIndex initialized with 0 polygons; refusing to run "
        "with empty FIR boundaries."
    )

  return Boundaries(
      country_features=country_features,
      fir_features=fir_features,
      country_index=country_index,
      fir_index=fir_index,
  )


def _keep_latest_year_value(
    latest: MutableMapping[str, _YearValue],
    *,
    iso3: str,
    year: int,
    value: float,
) -> None:
  """Stores `(year, value)` for `iso3` unless a later year is already kept."""
  previous = latest.get(iso3)
  if previous is None or year > previous.year:
    latest[iso3] = _YearValue(year=year, value=value)


def _collect_latest_population(
    pop_by_iso3: MutableMapping[str, _YearValue],
    *,
    rows: Iterable[Mapping[str, str | None]],
    input_year: int,
) -> None:
  """Adds the latest population at or before `input_year` per ISO3 in place.

  Rows with a non-numeric year or value are skipped. Results are written as
  rows are read, so a read error midway keeps the rows seen so far.

  Args:
    pop_by_iso3: Mapping of ISO-3 code to the selected year and value.
    rows: Population CSV rows keyed by column header.
    input_year: Latest calendar year to consider.
  """
  for row in rows:
    iso3 = (row.get(_COL_COUNTRY_CODE) or "").strip().upper()
    try:
      yr = int(row.get(_COL_YEAR) or "")
      numeric_value = float(row.get(_COL_VALUE) or "")
    except (ValueError, TypeError):
      continue
    if iso3 and yr <= input_year:
      _keep_latest_year_value(
          pop_by_iso3, iso3=iso3, year=yr, value=numeric_value
      )


def _load_population_by_iso3(
    path: str, input_year: int
) -> dict[str, _YearValue]:
  """Reads the latest population value at or before `input_year` per ISO3."""
  if not _path_exists(path):
    raise RuntimeError(f"Population baseline file not found: {path!r}")
  pop_by_iso3: dict[str, _YearValue] = {}
  try:
    with fs.open_file(path, "r") as f:
      _collect_latest_population(
          pop_by_iso3, rows=csv.DictReader(f), input_year=input_year
      )
  except (OSError, fs.Error, csv.Error, ValueError) as e:
    raise RuntimeError(
        f"Could not load population baseline from {path!r}: {e}"
    ) from e
  if not pop_by_iso3:
    raise RuntimeError(
        f"Population baseline file {path!r} yielded 0 country records "
        f"for input_year <= {input_year}."
    )
  logging.info(
      "Loaded population for %d ISO3 codes from %s", len(pop_by_iso3), path
  )
  return pop_by_iso3


def _latest_climate_watch_ghg_by_iso3(
    cw_data: Mapping[str, Any], input_year: int
) -> dict[str, _YearValue]:
  """Selects the latest total-GHG value at or before `input_year` per ISO3.

  Only Climate Watch records for `Total excluding LULUCF` / `All GHG` are
  considered. Emission entries with a non-numeric year or value are skipped.

  Args:
    cw_data: Parsed Climate Watch JSON document with a `data` record list.
    input_year: Latest calendar year to consider.

  Returns:
    Mapping of ISO-3 code to the selected year and value.
  """
  ghg_by_iso3: dict[str, _YearValue] = {}
  for record in cw_data.get(_CW_KEY_DATA, []):
    if (
        record.get(_CW_KEY_SECTOR) != _CW_SECTOR_TOTAL_EXCL_LULUCF
        or record.get(_CW_KEY_GAS) != _CW_GAS_ALL_GHG
        or record.get(_CW_KEY_DATA_SOURCE) != _CW_SOURCE_CLIMATE_WATCH
    ):
      continue
    iso3 = (record.get(_CW_KEY_ISO_CODE3) or "").strip().upper()
    for entry in record.get(_CW_KEY_EMISSIONS, []):
      try:
        yr = int(entry.get(_CW_KEY_YEAR))
        numeric_value = float(entry.get(_CW_KEY_VALUE))
      except (ValueError, TypeError):
        continue
      if iso3 and yr <= input_year:
        _keep_latest_year_value(
            ghg_by_iso3, iso3=iso3, year=yr, value=numeric_value
        )
  return ghg_by_iso3


def _load_ghg_by_iso3(path: str, input_year: int) -> dict[str, _YearValue]:
  """Reads the latest Climate Watch total GHG (Mt CO2e) per ISO3."""
  if not _path_exists(path):
    raise RuntimeError(f"GHG emissions baseline file not found: {path!r}")
  ghg_by_iso3: dict[str, _YearValue] = {}
  try:
    with fs.open_file(path, "r") as f:
      cw_data = json.load(f)
    ghg_by_iso3 = _latest_climate_watch_ghg_by_iso3(cw_data, input_year)
  except (OSError, fs.Error, json.JSONDecodeError, KeyError, ValueError) as e:
    raise RuntimeError(
        f"Could not load GHG emissions baseline from {path!r}: {e}"
    ) from e
  if not ghg_by_iso3:
    raise RuntimeError(
        f"GHG emissions baseline file {path!r} yielded 0 country records "
        f"for input_year <= {input_year}."
    )
  logging.info(
      "Loaded Climate Watch GHG emissions for %d ISO3 codes from %s",
      len(ghg_by_iso3),
      path,
  )
  return ghg_by_iso3


def _country_registry() -> dict[str, dict[str, Any]]:
  """Returns one entry per ISO 3166-1 country keyed by alpha-2.

  Each entry carries `iso_a3` and a display `name`. This is the ISO2<->ISO3
  bridge for the population and GHG sources (which are keyed by ISO3) and is
  independent of the boundary GeoJSON, so countries that only appear through
  FIRs or airports (e.g. Malta, Singapore) still join.

  Returns:
    Mapping from uppercase ISO 3166-1 alpha-2 code to a dictionary containing
    `iso_a3` and `name`.

  Raises:
    FileNotFoundError: If the bundled `iso3166-1.json` resource is missing.
    KeyError: If a required ISO 3166-1 JSON key is missing.
    json.JSONDecodeError: If the bundled ISO 3166-1 JSON is malformed.
  """
  # pycountry.countries reads its database with a plain open() on a path under
  # the package, which fails when the package is inside a zip archive. Read
  # the same file through importlib.resources, which works in both layouts.
  raw = (
      importlib.resources.files(pycountry)
      .joinpath("databases", "iso3166-1.json")
      .read_text(encoding="utf-8")
  )
  registry: dict[str, dict[str, Any]] = {}
  for country in json.loads(raw)["3166-1"]:
    registry[country["alpha_2"].upper()] = {
        constants.KEY_ISO_A3: country["alpha_3"].upper(),
        constants.KEY_NAME: (
            country.get("common_name") or country[constants.KEY_NAME]
        ),
    }
  if not registry:
    raise RuntimeError("pycountry ISO 3166-1 database is empty.")
  return registry


def _merge_iso3_baseline_field(
    *,
    stats: Mapping[str, MutableMapping[str, Any]],
    iso3_to_iso2: Mapping[str, str],
    field: str,
    loaded: Mapping[str, _YearValue],
) -> None:
  """Merges ISO-3 keyed baseline values into the ISO-2 stats dictionary."""
  unmapped: list[str] = []
  for iso3, year_val in loaded.items():
    a2 = iso3_to_iso2.get(iso3)
    if a2:
      stats[a2][field] = _coerce_baseline_value(field, year_val.value)
    else:
      unmapped.append(iso3)
  if unmapped:
    unmapped.sort()
    # Mostly regional aggregates (WLD, EUU, ...) plus non-ISO codes such as
    # Kosovo's XKX; supply those via --country_stats_path if needed.
    logging.info(
        "%s: %d ISO3 codes are not ISO 3166-1 countries and were skipped: %s",
        field,
        len(unmapped),
        ", ".join(unmapped),
    )


def _coerce_baseline_value(field: str, value: Any) -> int | float:
  """Coerces a parsed baseline value to its published type.

  Population is a head count, so it is rounded to an `int` (source CSVs and
  overrides may spell it as `8700000.0`). Every other baseline field, such as
  `ghg_mtco2e`, stays a `float`.

  Args:
    field: Baseline field name, e.g. `population` or `ghg_mtco2e`.
    value: Parsed numeric value (or numeric string).

  Returns:
    `round(value)` as an `int` for `population`, else `float(value)`.
  """
  if field == constants.KEY_POPULATION:
    return int(round(float(value)))
  return float(value)


def _merge_custom_country_stats(
    stats: MutableMapping[str, dict[str, Any]], custom_data: Mapping[str, Any]
) -> None:
  """Overwrites `stats` fields with the non-null values in `custom_data`."""
  for iso, item in custom_data.items():
    entry = stats.setdefault(iso.strip().upper(), {})
    for field in (constants.KEY_POPULATION, constants.KEY_GHG_MTCO2E):
      if (raw_value := item.get(field)) is not None:
        entry[field] = _coerce_baseline_value(field, raw_value)


def _apply_custom_country_stats_overrides(
    *,
    stats: MutableMapping[str, dict[str, Any]],
    country_stats_path: str,
) -> None:
  """Loads and merges custom ISO-2 baseline overrides from a JSON file."""
  if not _path_exists(country_stats_path):
    raise RuntimeError(
        f"Custom baseline stats file not found: {country_stats_path!r}"
    )

  try:
    with fs.open_file(country_stats_path, "r") as f:
      custom_data = json.load(f)
    if not custom_data:
      raise RuntimeError(
          f"Custom baseline stats file {country_stats_path!r} is empty."
      )
    _merge_custom_country_stats(stats, custom_data)
    logging.info("Updated baseline stats from %s", country_stats_path)
  except (
      OSError,
      fs.Error,
      json.JSONDecodeError,
      KeyError,
      ValueError,
      AttributeError,
  ) as e:
    raise RuntimeError(
        f"Error reading custom baseline stats from {country_stats_path!r}: {e}"
    ) from e


def _load_iso3_baseline_sources(
    *,
    population_path: str | None,
    ghg_emissions_path: str | None,
    input_year: int,
) -> dict[str, dict[str, _YearValue]]:
  """Loads the ISO-3 keyed population and GHG baseline sources.

  Both sources are read before any of them is merged; an unconfigured source
  yields an empty mapping.

  Args:
    population_path: Optional path to population CSV file.
    ghg_emissions_path: Optional path to Climate Watch GHG emissions JSON.
    input_year: Latest calendar year to consider.

  Returns:
    Mapping of baseline field name (`population`, then `ghg_mtco2e`) to the
    per-ISO-3 values loaded for it.
  """
  pop_data = {}
  if population_path:
    pop_data = _load_population_by_iso3(population_path, input_year)

  ghg_data = {}
  if ghg_emissions_path:
    ghg_data = _load_ghg_by_iso3(ghg_emissions_path, input_year)

  return {
      constants.KEY_POPULATION: pop_data,
      constants.KEY_GHG_MTCO2E: ghg_data,
  }


def load_country_baseline_stats(
    population_path: str | None = None,
    ghg_emissions_path: str | None = None,
    country_stats_path: str | None = None,
    *,
    input_year: int,
) -> dict[str, dict[str, Any]]:
  """Returns per-country baseline stats keyed by ISO2.

  Every ISO 3166-1 country gets an entry with `iso_a3` and `name`.
  `population` and `ghg_mtco2e` (Mt CO2e) are present only for countries the
  sources cover, taking the latest value at or before `input_year`. They are
  never back-filled; downstream metrics treat an absent value as unknown.

  Sources, applied in order:
    1. `population_path`: CSV with `Country Code` (ISO3), `Year`, `Value`.
    2. `ghg_emissions_path`: Climate Watch JSON (`Total excluding LULUCF`,
       `All GHG`, `Climate Watch`), keyed by `iso_code3`.
    3. `country_stats_path`: JSON `{ISO2: {population, ghg_mtco2e}}`; overrides
       per field and may add codes outside ISO 3166-1 (e.g. `XK`).

  Args:
    population_path: Optional path to population CSV file.
    ghg_emissions_path: Optional path to Climate Watch GHG emissions JSON.
    country_stats_path: Optional path to JSON file overriding country stats.
    input_year: Calendar year for baseline attribution.

  Returns:
    Dictionary mapping ISO-2 codes to baseline statistics dictionaries.

  Raises:
    RuntimeError: If no source is configured, or a configured source is
      missing, unreadable, or yields zero records.
  """
  if not (population_path or ghg_emissions_path or country_stats_path):
    raise RuntimeError(
        "No country baseline source configured; pass at least one of"
        " --population_path, --ghg_emissions_path, or --country_stats_path."
    )

  stats = _country_registry()
  iso3_to_iso2 = {
      entry_stats[constants.KEY_ISO_A3]: a2 for a2, entry_stats in stats.items()
  }

  for field, loaded in _load_iso3_baseline_sources(
      population_path=population_path,
      ghg_emissions_path=ghg_emissions_path,
      input_year=input_year,
  ).items():
    _merge_iso3_baseline_field(
        stats=stats,
        iso3_to_iso2=iso3_to_iso2,
        field=field,
        loaded=loaded,
    )

  if country_stats_path:
    _apply_custom_country_stats_overrides(
        stats=stats,
        country_stats_path=country_stats_path,
    )

  return stats
