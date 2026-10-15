"""Fetches and normalizes external reference datasets for data extraction.

Downloads the public reference inputs consumed by `extract_pipeline_main` and
`derive_main`:
  1. `countries.geojson`: Natural Earth 1:110m Admin-0 Countries GeoJSON, with
     feature properties normalized to lowercase `name`, `iso_a2`, and `iso_a3`.
  2. `population.csv`: DataHub `datasets/population` CSV (derived from World
     Bank World Development Indicators `SP.POP.TOTL`).
  3. `ghg_emissions.json`: Climate Watch Historical Emissions API JSON filtered
     to `data_source="Climate Watch"` (`source_ids[]=274`), `gas="All GHG"`
     (`gas_ids[]=553`), and `sector="Total excluding LULUCF"`
     (`sector_ids[]=3208`).
  4. `airports.csv`: OurAirports global airport database CSV.

Usage:
  python -m data_extraction.fetch_reference_data --output_dir=/path/to/ref
"""

from collections.abc import Callable, Mapping, Sequence
import csv
import io
import json
import os
from typing import Any, TypeAlias
import urllib.parse
import urllib.request

from absl import app
from absl import flags
from absl import logging

from common import constants
from common import fs
from data_extraction import metadata_loader

DEFAULT_COUNTRY_GEOJSON_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_110m_admin_0_countries.geojson"
)
DEFAULT_POPULATION_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/population/"
    "main/data/population.csv"
)
DEFAULT_GHG_EMISSIONS_URL = (
    "https://www.climatewatchdata.org/api/v1/data/historical_emissions"
    "?source_ids[]=274&gas_ids[]=553&sector_ids[]=3208&per_page=500"
)
DEFAULT_AIRPORTS_CSV_URL = (
    "https://davidmegginson.github.io/ourairports-data/airports.csv"
)

COUNTRIES_FILENAME = "countries.geojson"
POPULATION_FILENAME = "population.csv"
GHG_EMISSIONS_FILENAME = "ghg_emissions.json"
AIRPORTS_FILENAME = "airports.csv"

_COL_COUNTRY_NAME = "Country Name"
_COL_COUNTRY_CODE = "Country Code"
_COL_YEAR = "Year"
_COL_VALUE = "Value"
_COL_ISO_COUNTRY = "iso_country"
_COL_GPS_CODE = "gps_code"
_COL_IDENT = "ident"
_COL_IATA_CODE = "iata_code"
_KEY_DATA = "data"
_KEY_SECTOR = "sector"
_KEY_GAS = "gas"
_KEY_DATA_SOURCE = "data_source"
_KEY_ISO_CODE3 = "iso_code3"
_CW_SECTOR_TOTAL_EXCL_LULUCF = "Total excluding LULUCF"
_CW_GAS_ALL_GHG = "All GHG"
_CW_SOURCE_CLIMATE_WATCH = "Climate Watch"

_REQUIRED_POPULATION_COLUMNS = frozenset(
    {_COL_COUNTRY_NAME, _COL_COUNTRY_CODE, _COL_YEAR, _COL_VALUE}
)
_REQUIRED_AIRPORTS_COLUMNS = frozenset({_COL_ISO_COUNTRY})
_AIRPORT_CODE_COLUMNS = frozenset({_COL_GPS_CODE, _COL_IDENT, _COL_IATA_CODE})
_DEFAULT_PER_PAGE = 500
_MAX_GHG_PAGES = 100
_USER_AGENT = "contrails-regional-dashboard-reference-fetcher/1.0"

UrlFetcher: TypeAlias = Callable[[str, float], bytes]

_OUTPUT_DIR = flags.DEFINE_string(
    "output_dir",
    "",
    "Directory where fetched reference files are written.",
)
_COUNTRY_GEOJSON_URL = flags.DEFINE_string(
    "country_geojson_url",
    DEFAULT_COUNTRY_GEOJSON_URL,
    "Upstream URL for Natural Earth admin-0 countries GeoJSON.",
)
_POPULATION_CSV_URL = flags.DEFINE_string(
    "population_csv_url",
    DEFAULT_POPULATION_CSV_URL,
    "Upstream URL for country population CSV (Country Name, Country Code,"
    " Year, Value).",
)
_GHG_EMISSIONS_URL = flags.DEFINE_string(
    "ghg_emissions_url",
    DEFAULT_GHG_EMISSIONS_URL,
    "Upstream URL for Climate Watch historical GHG emissions API.",
)
_AIRPORTS_CSV_URL = flags.DEFINE_string(
    "airports_csv_url",
    DEFAULT_AIRPORTS_CSV_URL,
    "Upstream URL for OurAirports airports.csv.",
)
_HTTP_TIMEOUT_SECONDS = flags.DEFINE_float(
    "http_timeout_seconds",
    60.0,
    "HTTP request timeout in seconds.",
)


def _urlopen_bytes(url: str, timeout_seconds: float) -> bytes:
  """Downloads raw bytes from an HTTP/HTTPS URL."""
  parsed = urllib.parse.urlsplit(url)
  if parsed.scheme not in ("http", "https"):
    raise ValueError(f"Unsupported URL scheme in {url!r}; expected http/https.")
  req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
  with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
    return resp.read()


def _extract_country_feature_properties(
    properties: Mapping[str, Any], index: int
) -> dict[str, Any]:
  """Extracts lowercase name, iso_a2, and optional iso_a3 properties."""
  name = str(
      properties.get(constants.KEY_NAME) or properties.get("NAME") or ""
  ).strip()
  iso_a2 = (
      str(
          properties.get(constants.KEY_ISO_A2) or properties.get("ISO_A2") or ""
      )
      .strip()
      .upper()
  )
  iso_a3 = (
      str(
          properties.get(constants.KEY_ISO_A3) or properties.get("ISO_A3") or ""
      )
      .strip()
      .upper()
  )
  if not name or not iso_a2:
    raise ValueError(
        f"Country feature {index} is missing name or iso_a2: {properties!r}"
    )
  norm_props: dict[str, Any] = {
      constants.KEY_NAME: name,
      constants.KEY_ISO_A2: iso_a2,
  }
  if iso_a3:
    norm_props[constants.KEY_ISO_A3] = iso_a3
  return norm_props


def _normalize_country_feature(feat: Any, index: int) -> dict[str, Any]:
  """Extracts and normalizes geometry and properties for a country feature."""
  geometry = (
      feat.get(constants.GEOJSON_GEOMETRY) if isinstance(feat, dict) else None
  )
  if not geometry:
    raise ValueError(f"Country feature {index} is missing geometry.")
  properties = feat.get(constants.GEOJSON_PROPERTIES) or {}
  norm_props = _extract_country_feature_properties(properties, index)
  return {
      constants.KEY_TYPE: constants.GEOJSON_FEATURE,
      constants.GEOJSON_PROPERTIES: norm_props,
      constants.GEOJSON_GEOMETRY: geometry,
  }


def normalize_country_geojson(payload: Mapping[str, Any]) -> dict[str, Any]:
  """Normalizes a country GeoJSON FeatureCollection to lowercase properties.

  Upstream Natural Earth GeoJSON stores country attributes under uppercase
  `NAME`, `ISO_A2`, and `ISO_A3` keys alongside ~160 unused fields. The
  extraction pipeline, backend server, and UI expect lowercase `name`,
  `iso_a2`, and `iso_a3`.

  Args:
    payload: Parsed GeoJSON FeatureCollection mapping.

  Returns:
    A FeatureCollection whose features carry normalized `name`, `iso_a2`, and
    `iso_a3` properties.

  Raises:
    ValueError: If `payload` is not a non-empty FeatureCollection.
  """
  raw_features = payload.get(constants.GEOJSON_FEATURES)
  if not isinstance(raw_features, list) or not raw_features:
    raise ValueError("Country GeoJSON must contain a non-empty features list.")

  normalized_features = [
      _normalize_country_feature(feat, index)
      for index, feat in enumerate(raw_features)
  ]
  return {
      constants.KEY_TYPE: constants.GEOJSON_FEATURE_COLLECTION,
      constants.GEOJSON_FEATURES: normalized_features,
  }


def validate_population_csv(csv_text: str) -> int:
  """Validates a population CSV and returns the number of valid rows."""
  reader = csv.DictReader(io.StringIO(csv_text))
  fieldnames = set(reader.fieldnames or ())
  missing = sorted(_REQUIRED_POPULATION_COLUMNS - fieldnames)
  if missing:
    raise ValueError(
        f"Population CSV is missing required columns: {', '.join(missing)}"
    )
  valid_rows = 0
  for row in reader:
    iso3 = (row.get(_COL_COUNTRY_CODE) or "").strip()
    cleaned_year = (row.get(_COL_YEAR) or "").strip()
    raw_value = (row.get(_COL_VALUE) or "").strip()
    if not (iso3 and cleaned_year and raw_value):
      continue
    try:
      int(cleaned_year)
      float(raw_value)
    except ValueError:
      continue
    valid_rows += 1
  if valid_rows == 0:
    raise ValueError("Population CSV contains 0 valid population rows.")
  return valid_rows


def validate_airports_csv(csv_text: str) -> int:
  """Validates an OurAirports CSV and returns the number of mapped airports."""
  reader = csv.DictReader(io.StringIO(csv_text))
  fieldnames = set(reader.fieldnames or ())
  if not _REQUIRED_AIRPORTS_COLUMNS.issubset(fieldnames):
    raise ValueError("Airports CSV is missing required column 'iso_country'.")
  if not (_AIRPORT_CODE_COLUMNS & fieldnames):
    raise ValueError(
        "Airports CSV must contain at least one of gps_code, ident, iata_code."
    )
  valid_rows = 0
  for row in reader:
    code = (
        row.get(_COL_GPS_CODE) or row.get(_COL_IDENT) or row.get(_COL_IATA_CODE)
    )
    country = row.get(_COL_ISO_COUNTRY)
    if code and code.strip() and country and country.strip():
      valid_rows += 1
  if valid_rows == 0:
    raise ValueError("Airports CSV contains 0 valid airport mappings.")
  return valid_rows


def _with_page_param(url: str, page: int) -> str:
  """Returns `url` with its `page` query parameter set to `page`."""
  parts = urllib.parse.urlsplit(url)
  query_items = [
      (k, param_value)
      for k, param_value in urllib.parse.parse_qsl(
          parts.query, keep_blank_values=True
      )
      if k != "page"
  ]
  query_items.append(("page", str(page)))
  new_query = urllib.parse.urlencode(query_items, safe="[]")
  return urllib.parse.urlunsplit(parts._replace(query=new_query))


def _extract_per_page(url: str) -> int:
  """Reads `per_page` from `url` query parameters, or `_DEFAULT_PER_PAGE`."""
  parts = urllib.parse.urlsplit(url)
  for key, param_value in urllib.parse.parse_qsl(parts.query):
    if key != "per_page":
      continue
    try:
      parsed = int(param_value)
    except ValueError:
      break
    if parsed > 0:
      return parsed
  return _DEFAULT_PER_PAGE


def fetch_ghg_emissions_records(
    base_url: str,
    timeout_seconds: float = 60.0,
    fetcher: UrlFetcher = _urlopen_bytes,
) -> dict[str, Any]:
  """Fetches up to 100 Climate Watch pages and verifies matching GHG records."""
  per_page = _extract_per_page(base_url)
  all_records: list[dict[str, Any]] = []
  for page in range(1, _MAX_GHG_PAGES + 1):
    page_url = base_url if page == 1 else _with_page_param(base_url, page)
    raw = fetcher(page_url, timeout_seconds)
    payload = json.loads(raw.decode("utf-8"))
    page_data = payload.get(_KEY_DATA)
    if not isinstance(page_data, list):
      raise ValueError(
          f"Climate Watch response from {page_url!r} has no 'data' list."
      )
    all_records.extend(page_data)
    if len(page_data) < per_page:
      break
  matching = [
      rec
      for rec in all_records
      if rec.get(_KEY_SECTOR) == _CW_SECTOR_TOTAL_EXCL_LULUCF
      and rec.get(_KEY_GAS) == _CW_GAS_ALL_GHG
      and rec.get(_KEY_DATA_SOURCE) == _CW_SOURCE_CLIMATE_WATCH
      and rec.get(_KEY_ISO_CODE3)
  ]
  if not matching:
    raise ValueError(
        "Climate Watch response contained 0 matching 'Total excluding LULUCF' /"
        " 'All GHG' / 'Climate Watch' country records."
    )
  return {_KEY_DATA: all_records}


def _write_text_atomic(path: str, content: str) -> None:
  """Writes UTF-8 text atomically to `path` via `fs`."""
  parent = os.path.dirname(path)
  if parent:
    fs.makedirs(parent)
  with fs.atomic_open_for_write(path, "w") as f:
    f.write(content)


def _fetch_countries(
    *,
    target_dir: str,
    url: str,
    timeout_seconds: float,
    fetcher: UrlFetcher,
) -> str:
  """Fetches, normalizes, writes, and validates the country GeoJSON.

  Args:
    target_dir: Destination directory for the written file.
    url: URL for Natural Earth admin-0 countries GeoJSON.
    timeout_seconds: Timeout in seconds for the HTTP request.
    fetcher: Callable `(url, timeout_seconds) -> bytes` for HTTP downloads.

  Returns:
    Path of the written countries GeoJSON file.
  """
  raw_geojson = json.loads(fetcher(url, timeout_seconds).decode("utf-8"))
  normalized = normalize_country_geojson(raw_geojson)
  countries_path = os.path.join(target_dir, COUNTRIES_FILENAME)
  _write_text_atomic(
      countries_path, json.dumps(normalized, separators=(",", ":"))
  )
  metadata_loader.load_country_geojson(countries_path)
  logging.info(
      "Wrote %d normalized country features to %s",
      len(normalized[constants.GEOJSON_FEATURES]),
      countries_path,
  )
  return countries_path


def _fetch_population(
    *,
    target_dir: str,
    url: str,
    timeout_seconds: float,
    fetcher: UrlFetcher,
) -> str:
  """Fetches, validates, and writes the population CSV.

  Args:
    target_dir: Destination directory for the written file.
    url: URL for the country population CSV.
    timeout_seconds: Timeout in seconds for the HTTP request.
    fetcher: Callable `(url, timeout_seconds) -> bytes` for HTTP downloads.

  Returns:
    Path of the written population CSV file.
  """
  pop_text = fetcher(url, timeout_seconds).decode("utf-8")
  row_count = validate_population_csv(pop_text)
  population_path = os.path.join(target_dir, POPULATION_FILENAME)
  _write_text_atomic(population_path, pop_text)
  logging.info("Wrote %d population rows to %s", row_count, population_path)
  return population_path


def _fetch_ghg_emissions(
    *,
    target_dir: str,
    url: str,
    timeout_seconds: float,
    fetcher: UrlFetcher,
) -> str:
  """Fetches all Climate Watch GHG emission pages and writes them as JSON.

  Args:
    target_dir: Destination directory for the written file.
    url: URL for the Climate Watch GHG emissions JSON API.
    timeout_seconds: Timeout in seconds per HTTP request.
    fetcher: Callable `(url, timeout_seconds) -> bytes` for HTTP downloads.

  Returns:
    Path of the written GHG emissions JSON file.
  """
  ghg_payload = fetch_ghg_emissions_records(
      url, timeout_seconds=timeout_seconds, fetcher=fetcher
  )
  ghg_path = os.path.join(target_dir, GHG_EMISSIONS_FILENAME)
  _write_text_atomic(ghg_path, json.dumps(ghg_payload))
  logging.info(
      "Wrote %d GHG emission records to %s",
      len(ghg_payload[_KEY_DATA]),
      ghg_path,
  )
  return ghg_path


def _fetch_airports(
    *,
    target_dir: str,
    url: str,
    timeout_seconds: float,
    fetcher: UrlFetcher,
) -> str:
  """Fetches, validates, writes, and loads the OurAirports CSV.

  Args:
    target_dir: Destination directory for the written file.
    url: URL for the OurAirports CSV.
    timeout_seconds: Timeout in seconds for the HTTP request.
    fetcher: Callable `(url, timeout_seconds) -> bytes` for HTTP downloads.

  Returns:
    Path of the written airports CSV file.
  """
  airports_text = fetcher(url, timeout_seconds).decode("utf-8")
  airport_count = validate_airports_csv(airports_text)
  airports_path = os.path.join(target_dir, AIRPORTS_FILENAME)
  _write_text_atomic(airports_path, airports_text)
  metadata_loader.load_airports_mapping(airports_path)
  logging.info("Wrote %d airport rows to %s", airport_count, airports_path)
  return airports_path


def fetch_all_reference_data(
    output_dir: str,
    *,
    country_geojson_url: str = DEFAULT_COUNTRY_GEOJSON_URL,
    population_csv_url: str = DEFAULT_POPULATION_CSV_URL,
    ghg_emissions_url: str = DEFAULT_GHG_EMISSIONS_URL,
    airports_csv_url: str = DEFAULT_AIRPORTS_CSV_URL,
    timeout_seconds: float = 60.0,
    fetcher: UrlFetcher = _urlopen_bytes,
) -> dict[str, str]:
  """Fetches, normalizes, validates, and writes reference data files.

  Args:
    output_dir: Destination directory for fetched files.
    country_geojson_url: URL for Natural Earth admin-0 countries GeoJSON.
    population_csv_url: URL for country population CSV.
    ghg_emissions_url: URL for Climate Watch GHG emissions JSON API.
    airports_csv_url: URL for OurAirports CSV.
    timeout_seconds: Timeout in seconds per HTTP request.
    fetcher: Callable `(url, timeout_seconds) -> bytes` for HTTP downloads.

  Returns:
    Mapping from dataset key (`countries`, `population`, `ghg_emissions`,
    `airports`) to the written file path.
  """
  target_dir = output_dir.strip()
  if not target_dir:
    raise ValueError("output_dir must not be empty.")
  fs.makedirs(target_dir)
  written: dict[str, str] = {}
  dataset_steps = (
      ("countries", country_geojson_url, _fetch_countries),
      ("population", population_csv_url, _fetch_population),
      ("ghg_emissions", ghg_emissions_url, _fetch_ghg_emissions),
      ("airports", airports_csv_url, _fetch_airports),
  )
  for dataset_key, url, fetch_step in dataset_steps:
    if url:
      written[dataset_key] = fetch_step(
          target_dir=target_dir,
          url=url,
          timeout_seconds=timeout_seconds,
          fetcher=fetcher,
      )
  return written


def main(argv: Sequence[str]) -> None:
  """Downloads and writes all public reference datasets to `--output_dir`."""
  if len(argv) > 1:
    raise app.UsageError(f"Unrecognized positional arguments: {argv[1:]!r}")
  if not _OUTPUT_DIR.value.strip():
    raise app.UsageError("--output_dir is required.")
  fetch_all_reference_data(
      _OUTPUT_DIR.value,
      country_geojson_url=_COUNTRY_GEOJSON_URL.value,
      population_csv_url=_POPULATION_CSV_URL.value,
      ghg_emissions_url=_GHG_EMISSIONS_URL.value,
      airports_csv_url=_AIRPORTS_CSV_URL.value,
      timeout_seconds=_HTTP_TIMEOUT_SECONDS.value,
  )


if __name__ == "__main__":
  app.run(main)
