"""Shared constants, FIR ID helpers, and row types for pipeline and server."""

from collections.abc import Mapping
import re
import types
from typing import Final, TypeAlias, TypedDict

_FIR_PREFIX_RE = re.compile(
    r"^(?:FIR|UIR)(?:\s*[:\-_]\s*|\s+|$)", re.IGNORECASE
)
_FIR_SUFFIX_RE = re.compile(r"\s+(?:FIR|UIR)$", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")


def clean_fir_name(raw_name: str | None, fallback_icao: str = "") -> str:
  """Strips redundant leading/trailing FIR/UIR tokens and normalizes case."""
  cleaned = (raw_name or "").strip()
  cleaned = _FIR_PREFIX_RE.sub("", cleaned)
  cleaned = _FIR_SUFFIX_RE.sub("", cleaned)
  cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip().upper()
  if cleaned:
    return cleaned
  return (fallback_icao or "").strip().upper()


def make_fir_id(fir_icao: str | None, firname: str | None = "") -> str:
  """Constructs a canonical FIR identifier from ICAO code and FIR name."""
  icao = (fir_icao or "").strip().upper()
  name = clean_fir_name(firname)
  slug = _NON_ALNUM_RE.sub("_", name).strip("_")
  if icao and slug and slug != icao:
    return f"{icao}_{slug}"
  return icao or slug


def split_iso2_codes(raw_codes: str | None) -> list[str]:
  """Splits a FIR's comma-separated controlling-country codes into a list.

  The FIR input carries `fir_to_country_iso2s` as one string (e.g. "SN, ML");
  published rows carry it as a list. Codes are stripped and upper-cased, empty
  items are dropped and duplicates are removed, keeping the input order: the
  first code is the FIR's primary controlling country.

  Args:
    raw_codes: Comma-separated ISO 3166-1 alpha-2 codes, or None.

  Returns:
    The unique codes in input order; empty if there are none.
  """
  codes = (code.strip().upper() for code in (raw_codes or "").split(","))
  return list(dict.fromkeys(code for code in codes if code))


# Contrail GWP conversion multipliers (tonnes CO2e per MJ of energy forcing):
#   epsilon * 1e6 / (A_Earth * AGWP_CO2(H) * tau_yr) / 1e3
# with epsilon = 0.42 (dimensionless contrail ERF/RF efficacy factor),
# A_Earth = 5.10064472e14 m^2, tau_yr = 3.1536e7 s and AGWP_CO2(H) =
# 2.49e-14, 5.28e-14 and 9.17e-14 W m^-2 yr kg^-1 for H = 20, 50 and 100 years.
# The input energy forcing must be NON-effective (no ERF/RF applied): epsilon
# is the only place the pipeline applies ERF/RF, so feeding effective energy
# forcing would apply it twice. Mean RF, impact density and the raw impact
# grid values stay non-effective; every CO2e value (including the temporal
# matrices) includes ERF/RF.
# See "Scientific methodology & metric derivations" in
# data_extraction/README.md.
GWP20 = "gwp20"
GWP50 = "gwp50"
GWP100 = "gwp100"
GWP20_TONNES_PER_MJ = 1.048618e-6
GWP50_TONNES_PER_MJ = 0.4945187e-6
GWP100_TONNES_PER_MJ = 0.2847392e-6

# Canonical horizon -> multiplier mapping. Iterate this rather than writing the
# three horizons out by hand at each derived-metric site, so every derived
# column covers all three horizons.
GWP_TONNES_PER_MJ: Final[Mapping[str, float]] = types.MappingProxyType({
    GWP20: GWP20_TONNES_PER_MJ,
    GWP50: GWP50_TONNES_PER_MJ,
    GWP100: GWP100_TONNES_PER_MJ,
})

# Global baseline constants
WORLD_AVG_GHG_PER_CAPITA_TONNES = 6.00328575

# Shared directory name, run pointer, and published artifact filenames.
RUNS_DIR = "runs"
POINTER_FILENAME = "latest.json"
POINTER_SCHEMA_VERSION = 1
COUNTRIES_GEOJSON_FILENAME = "countries.geojson"
FIRS_GEOJSON_FILENAME = "firs.geojson"
COUNTRY_EXPORT_FILENAME = "country_fir_density_df_export_per_country.json"
FIR_EXPORT_FILENAME = "fir_density_df_export_per_country.json"
COUNTRY_TEMPORAL_EXPORT_FILENAME = (
    "country_temporal_density_df_export_per_country.json"
)
FIR_TEMPORAL_EXPORT_FILENAME = "fir_temporal_density_df_export_per_country.json"
SHARED_GRID_META_FILENAME = "grid_meta.json"

# Special entity identifiers, FIR classification tokens, and column prefixes.
UNASSIGNED_ISO = "-99"
FIR_TYPE_DOMESTIC = "domestic"
FIR_TYPE_INTERNATIONAL = "international"
FIR_SCOPE_TOTAL = "fir_total"
FIR_SCOPE_DOM = "fir_dom"
FIR_SCOPE_INT = "fir_int"
FIR_SCOPES: tuple[str, ...] = (FIR_SCOPE_TOTAL, FIR_SCOPE_DOM, FIR_SCOPE_INT)
PREFIX_OUTGOING_FLIGHT = "outgoing_flight_"
PREFIX_FIR_DOM = "fir_dom_"
PREFIX_FIR_INT = "fir_int_"
PREFIX_FIR_TOTAL = "fir_total_"

# Shared entity identity and baseline property keys.
KEY_NAME = "name"
KEY_ISO_A2 = "iso_a2"
KEY_ISO_A3 = "iso_a3"
KEY_FIR_ID = "fir_id"
KEY_FIR_ICAO = "fir_icao"
KEY_FIRNAME = "firname"
KEY_FIR_TO_COUNTRY_ISO2S = "fir_to_country_iso2s"
KEY_TYPE = "type"
KEY_AREA_SQ_KM = "area_sq_km"
KEY_AREASQKM = "areasqkm"
KEY_POPULATION = "population"
KEY_GHG_MTCO2E = "ghg_mtco2e"

# Shared temporal, segment, and derived rank/metric field keys and templates.
KEY_MONTH = "month"
KEY_LOCAL_HOUR = "local_hour"
KEY_SEGMENT_COUNT = "segment_count"
KEY_CONTRAIL_SEGMENT_COUNT = "contrail_segment_count"
KEY_PROP_SUM_EF_MJ = "prop_sum_ef_mj"
KEY_LOCAL_FLIGHT_IMPACT = "local_flight_impact"
KEY_TOTAL_FLIGHT_IMPACT = "total_flight_impact"
KEY_OUTGOING_FLIGHT_IMPACT = "outgoing_flight_impact"
KEY_OUTGOING_FLIGHT_IMPACT_RANK = "outgoing_flight_impact_rank"
KEY_FLIGHT_DISTANCE_KM = "flight_distance_km"
KEY_TOTAL_FLIGHT_DISTANCE_KM = "total_flight_distance_km"
KEY_CONTRAIL_FLIGHT_DISTANCE_KM = "contrail_flight_distance_km"
KEY_CO2E_GWP20_TONNES = "co2e_gwp20_tonnes"
KEY_CO2E_GWP50_TONNES = "co2e_gwp50_tonnes"
KEY_CO2E_GWP100_TONNES = "co2e_gwp100_tonnes"
KEY_MEAN_RF_MW_PER_SQ_M = "mean_rf_mw_per_sq_m"
KEY_MEAN_RF_MW_PER_SQ_M_RANK = "mean_rf_mw_per_sq_m_rank"
KEY_IMPACT_DENSITY_SQ_KM = "impact_density_sq_km"
KEY_IN_COUNTRY_IMPACT = "in_country_impact"
KEY_OVER_REGION_VS_OUTGOING_IMPACT = "over_region_vs_outgoing_impact"
KEY_MONTHS_COVERED = "months_covered"
KEY_DAYS_COVERED = "days_covered"
FIELD_CO2E_TONNES = "co2e_{gwp}_tonnes"
FIELD_CO2E_TONNES_RANK = "co2e_{gwp}_tonnes_rank"
FIELD_CO2E_TONNES_PER_CAPITA = "co2e_{gwp}_tonnes_per_capita"
FIELD_CO2E_TONNES_PER_CAPITA_RANK = "co2e_{gwp}_tonnes_per_capita_rank"
FIELD_CO2E_PER_CAPITA_VS_WORLD_AVG = "co2e_{gwp}_per_capita_vs_world_avg"
FIELD_CO2E_VS_NATIONAL_GHG = "co2e_{gwp}_vs_national_ghg"
FIELD_CO2E_VS_NATIONAL_GHG_RANK = "co2e_{gwp}_vs_national_ghg_rank"
# Fraction of the dataset total (the same for every GWP horizon), e.g. a
# country's over-region impact divided by the summed over-region impact of all
# countries. Prefixed with `outgoing_flight_` or `{scope}_` (`fir_total_`,
# `fir_dom_`, `fir_int_`) per scope.
KEY_CO2E_TONNES_SHARE = "co2e_tonnes_share"

# GeoJSON structural keys and geometry types.
GEOJSON_FEATURE_COLLECTION = "FeatureCollection"
GEOJSON_FEATURE = "Feature"
GEOJSON_FEATURES = "features"
GEOJSON_PROPERTIES = "properties"
GEOJSON_GEOMETRY = "geometry"
GEOJSON_COORDINATES = "coordinates"
GEOJSON_POLYGON = "Polygon"
GEOJSON_MULTIPOLYGON = "MultiPolygon"

# Grid metadata, config, and artifact naming keys.
KEY_MIN_LON = "min_lon"
KEY_MAX_LON = "max_lon"
KEY_MIN_LAT = "min_lat"
KEY_MAX_LAT = "max_lat"
KEY_STEP_SIZE_METERS = "step_size_meters"
KEY_WIDTH = "width"
KEY_HEIGHT = "height"
KEY_LON_STEP = "lon_step"
KEY_LAT_STEP = "lat_step"
KEY_BBOX = "bbox"
KEY_ORIGIN = "origin"
ORIGIN_LOWER_LEFT = "lower_left"
KEY_ENCODING = "encoding"
ENCODING_SIGNED_LOG10_REF_SCALED = "signed_log10_ref_scaled"
KEY_DTYPE = "dtype"
KEY_LODS = "lods"
KEY_LOD = "lod"
KEY_GRID_NAME = "grid_name"
GRID_BIN_GZ_SUFFIX = ".bin.gz"
GRID_FAMILY_TOTAL = "total"
GRID_FAMILY_MONTHLY = "monthly"
GRID_FAMILY_HOURLY = "hourly"
GRID_KIND_IMPACT = "impact"
GRID_KIND_DENSITY = "density"
GRID_ACC_SPARSE = "sparse"
GRID_ACC_DENSE = "dense"
TOTAL_GLOBAL_IMPACT_GRID_PREFIX = "total_global_impact_grid"
TOTAL_GLOBAL_DENSITY_GRID_PREFIX = "total_global_density_grid"
MONTHLY_GLOBAL_IMPACT_GRID_PREFIX = "monthly_global_impact_grid"
MONTHLY_GLOBAL_DENSITY_GRID_PREFIX = "monthly_global_density_grid"
HOURLY_GLOBAL_IMPACT_GRID_PREFIX = "hourly_global_impact_grid"
HOURLY_GLOBAL_DENSITY_GRID_PREFIX = "hourly_global_density_grid"

# Run pointer and manifest keys.
KEY_SCHEMA_VERSION = "schema_version"
KEY_RUN_ID = "run_id"
KEY_PUBLISHED_AT = "published_at"

# Canonical tuple shapes emitted by `shard_processor`, combined by
# `beam_stages.SumTupleCombineFn`, persisted by `intermediate_io`, and consumed
# by `derive` / `metric_derivations`. Plain tuples (not NamedTuple or
# @dataclass) are kept on the Beam wire so Beam uses its Cython TupleCoder and
# `SumTupleCombineFn` can combine values with
# `tuple(a + b for a, b in zip(...))` without reconstructor overhead or
# PickleCoder fallback.
#
# CountryKey: (name, iso_a2, month, local_hour), month as UTC YYYYMM
CountryKey: TypeAlias = tuple[str, str, int, int]
# CountryMetricsTuple: (segment_count, contrail_segment_count,
#   prop_sum_ef_mj, local_flight_impact, total_flight_impact,
#   outgoing_flight_impact, total_flight_distance_km,
#   contrail_flight_distance_km)
CountryMetricsTuple: TypeAlias = tuple[
    int, int, float, float, float, float, float, float
]
CountryRow: TypeAlias = tuple[CountryKey, CountryMetricsTuple]

# FirKey: (firname, fir_icao, fir_iso2, fir_type, month, local_hour), month as
# UTC YYYYMM
FirKey: TypeAlias = tuple[str, str, str, str, int, int]
# FirMetricsTuple: (segment_count, contrail_segment_count,
#   prop_sum_ef_mj, total_flight_distance_km, contrail_flight_distance_km)
FirMetricsTuple: TypeAlias = tuple[int, int, float, float, float]
FirRow: TypeAlias = tuple[FirKey, FirMetricsTuple]


class EncodedCountryRow(TypedDict):
  """Serialized JSONL dictionary shape for an intermediate CountryRow."""

  # Country display name.
  name: str
  # ISO 3166-1 alpha-2 code, e.g. "DE".
  iso_a2: str
  # UTC month as YYYYMM, e.g. 202401.
  month: int
  # Local hour of day, 0-23.
  local_hour: int
  # Number of flight segments in this bucket.
  segment_count: int
  # Number of those segments that formed a contrail.
  contrail_segment_count: int
  # Energy forcing of all segment portions inside the country, in MJ.
  prop_sum_ef_mj: float
  # Energy forcing inside the country from flights departing or arriving
  # there (known route only), in MJ.
  local_flight_impact: float
  # Energy forcing inside the country from flights with a known route, in MJ.
  total_flight_impact: float
  # Energy forcing anywhere from contrail segments of flights departing the
  # country, in MJ.
  outgoing_flight_impact: float
  # Total flown distance, in km.
  total_flight_distance_km: float
  # Flown distance that formed a contrail, in km.
  contrail_flight_distance_km: float


class EncodedFirRow(TypedDict):
  """Serialized JSONL dictionary shape for an intermediate FirRow."""

  # FIR display name.
  firname: str
  # ICAO FIR identifier, e.g. "EDGG".
  fir_icao: str
  # Comma-separated ISO2 codes of countries the FIR maps to, e.g. "DE".
  fir_to_country_iso2s: str
  # FIR classification: "domestic" or "international".
  type: str
  # UTC month as YYYYMM, e.g. 202401.
  month: int
  # Local hour of day, 0-23.
  local_hour: int
  # Number of flight segments in this bucket.
  segment_count: int
  # Number of those segments that formed a contrail.
  contrail_segment_count: int
  # Energy forcing of all segment portions inside the FIR, in MJ.
  prop_sum_ef_mj: float
  # Total flown distance, in km.
  total_flight_distance_km: float
  # Flown distance that formed a contrail, in km.
  contrail_flight_distance_km: float


class TemporalBundleRow(TypedDict, total=False):
  """Pre-aggregated 24x12 (hours x months) temporal matrix row."""

  # CO2-equivalent tonnes per [hour][month] at GWP20.
  co2e_gwp20_tonnes: list[list[float]]
  # CO2-equivalent tonnes per [hour][month] at GWP50.
  co2e_gwp50_tonnes: list[list[float]]
  # CO2-equivalent tonnes per [hour][month] at GWP100.
  co2e_gwp100_tonnes: list[list[float]]
  # Contrail-forming segments per [hour][month].
  contrail_segment_count: list[list[int]]
  # Segments per [hour][month].
  segment_count: list[list[int]]
  # Total flown distance per [hour][month], in km.
  total_flight_distance_km: list[list[float]]
  # Contrail-forming flown distance per [hour][month], in km.
  contrail_flight_distance_km: list[list[float]]
  # Region display name.
  name: str
  # ISO 3166-1 alpha-2 code (country rows only).
  iso_a2: str
  # FIR feature identifier (FIR rows only).
  fir_id: str
  # FIR display name (FIR rows only).
  firname: str
  # ICAO FIR identifier (FIR rows only).
  fir_icao: str
  # ISO2 codes of the FIR's controlling countries, primary first (FIR rows
  # only).
  fir_to_country_iso2s: list[str]
  # FIR classification: "domestic" or "international" (FIR rows only).
  type: str


def _impact_horizon_keys(prefix: str, *, with_density: bool) -> list[str]:
  """Returns the per-horizon GWP metric keys for an impact block `prefix`."""
  keys: list[str] = []
  for gwp in GWP_TONNES_PER_MJ:
    base = f"{prefix}co2e_{gwp}"
    keys.extend((
        f"{base}_tonnes",
        f"{base}_tonnes_rank",
        f"{base}_vs_national_ghg",
        f"{base}_vs_national_ghg_rank",
        f"{base}_tonnes_per_capita",
        f"{base}_tonnes_per_capita_rank",
        f"{base}_per_capita_vs_world_avg",
    ))
    if with_density:
      keys.append(f"{base}_tonnes_sq_km")
  return keys


_BASE_PUBLISHED_COUNTRY_KEYS: Final[tuple[str, ...]] = (
    KEY_NAME,
    KEY_ISO_A2,
    KEY_ISO_A3,
    KEY_MONTHS_COVERED,
    KEY_DAYS_COVERED,
    KEY_SEGMENT_COUNT,
    KEY_CONTRAIL_SEGMENT_COUNT,
    KEY_TOTAL_FLIGHT_DISTANCE_KM,
    KEY_CONTRAIL_FLIGHT_DISTANCE_KM,
    KEY_TOTAL_FLIGHT_IMPACT,
    KEY_LOCAL_FLIGHT_IMPACT,
    KEY_AREA_SQ_KM,
    KEY_GHG_MTCO2E,
    KEY_POPULATION,
    KEY_IMPACT_DENSITY_SQ_KM,
    KEY_IN_COUNTRY_IMPACT,
    KEY_MEAN_RF_MW_PER_SQ_M,
    KEY_MEAN_RF_MW_PER_SQ_M_RANK,
    KEY_OUTGOING_FLIGHT_IMPACT,
    KEY_OUTGOING_FLIGHT_IMPACT_RANK,
    KEY_OVER_REGION_VS_OUTGOING_IMPACT,
    KEY_CO2E_TONNES_SHARE,
    f"{PREFIX_OUTGOING_FLIGHT}{KEY_CO2E_TONNES_SHARE}",
)


def _build_published_country_row_keys() -> frozenset[str]:
  """Returns the canonical published column set for an annual country row."""
  keys: set[str] = {
      *_BASE_PUBLISHED_COUNTRY_KEYS,
      *_impact_horizon_keys("", with_density=True),
      *_impact_horizon_keys(PREFIX_OUTGOING_FLIGHT, with_density=False),
  }
  for scope in FIR_SCOPES:
    prefix = f"{scope}_"
    keys.update((
        f"{prefix}{KEY_SEGMENT_COUNT}",
        f"{prefix}{KEY_CONTRAIL_SEGMENT_COUNT}",
        f"{prefix}{KEY_FLIGHT_DISTANCE_KM}",
        f"{prefix}{KEY_CONTRAIL_FLIGHT_DISTANCE_KM}",
        f"{prefix}{KEY_AREA_SQ_KM}",
        f"{prefix}{KEY_CO2E_TONNES_SHARE}",
        f"{prefix}{KEY_FIR_ID}",
        f"{prefix}{KEY_FIRNAME}",
        f"{prefix}{KEY_FIR_ICAO}",
        f"{prefix}{KEY_IMPACT_DENSITY_SQ_KM}",
        f"{prefix}{KEY_MEAN_RF_MW_PER_SQ_M}",
        f"{prefix}{KEY_MEAN_RF_MW_PER_SQ_M_RANK}",
        *_impact_horizon_keys(prefix, with_density=True),
    ))
  return frozenset(keys)


def _build_published_fir_row_keys() -> frozenset[str]:
  """Returns the canonical published column set for an annual FIR row."""
  keys: set[str] = {
      KEY_FIR_ID,
      KEY_FIR_ICAO,
      KEY_FIRNAME,
      KEY_TYPE,
      KEY_FIR_TO_COUNTRY_ISO2S,
      KEY_MONTHS_COVERED,
      KEY_DAYS_COVERED,
      KEY_SEGMENT_COUNT,
      KEY_CONTRAIL_SEGMENT_COUNT,
      KEY_TOTAL_FLIGHT_DISTANCE_KM,
      KEY_CONTRAIL_FLIGHT_DISTANCE_KM,
      KEY_AREA_SQ_KM,
      KEY_IMPACT_DENSITY_SQ_KM,
      KEY_MEAN_RF_MW_PER_SQ_M,
      KEY_MEAN_RF_MW_PER_SQ_M_RANK,
      KEY_CO2E_TONNES_SHARE,
  }
  for gwp in GWP_TONNES_PER_MJ:
    base = f"co2e_{gwp}_tonnes"
    keys.update((base, f"{base}_sq_km", f"{base}_rank"))
  return frozenset(keys)


PUBLISHED_COUNTRY_ROW_KEYS: Final[frozenset[str]] = (
    _build_published_country_row_keys()
)
PUBLISHED_FIR_ROW_KEYS: Final[frozenset[str]] = _build_published_fir_row_keys()
