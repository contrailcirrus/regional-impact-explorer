"""Builds the synthetic dataset materialized by `server_test`.

Every number here is invented. Only identifiers are real (ISO codes, country
names, FIR ids and ICAO codes), because tests address entities by them and
identifiers are not data. Do not paste values from a pipeline run into this
file: the code is published without any data.

The files are laid out exactly like a published dataset, so tests exercise the
same path resolution as production:

    {year}/{dataset}/latest.json                       run pointer
    {year}/{dataset}/runs/{run_id}/<artifact>.json     pointed-to run

Derived fields (per capita, per km², shares, ratios, ranks) are computed from a
few seed values per entity with the pipeline's formulas, so they are mutually
consistent.
"""

from collections import abc
import dataclasses
import types
from typing import Any

from common import constants

DATASET = "example_dataset"

# Relative change of every impact metric per year (2024 is the base). Chosen so
# year-over-year expectations in the tests are round numbers.
YEAR_FACTOR: abc.Mapping[int, float] = types.MappingProxyType({
    2022: 0.9,
    2023: 0.95,
    2024: 1.0,
    2025: 0.8,
})
OUTGOING_YEAR_FACTOR: abc.Mapping[int, float] = types.MappingProxyType({
    2022: 0.9,
    2023: 0.95,
    2024: 1.0,
    2025: 0.9,
})

# GWP horizons relative to GWP50, and rounded world average for test fixtures.
GWP_FACTOR: abc.Mapping[int, float] = types.MappingProxyType(
    {20: 2.0, 50: 1.0, 100: 0.5}
)
WORLD_AVG_GHG_PER_CAPITA_TONNES = 6.0
_EF_MJ_PER_TONNE = 1.0e7
_SEGMENTS_PER_TONNE = 0.01
# Fraction of segments that form contrails, average segment length, and
# full-year observation coverage for synthetic fixtures.
_CONTRAIL_SEGMENT_SHARE = 0.1
_KM_PER_SEGMENT = 50.0
_MONTHS_COVERED = 12
_DAYS_COVERED = 365

_ISO_A3_BY_ISO2: abc.Mapping[str, str] = types.MappingProxyType({
    "AT": "AUT",
    "BE": "BEL",
    "CH": "CHE",
    "DE": "DEU",
    "FR": "FRA",
    "GB": "GBR",
    "NL": "NLD",
    "SO": "SOM",
    "US": "USA",
})

# Internal keys on `_fir_aggregate` dicts consumed by
# `_populate_country_fir_impacts`.
_KEY_TONNES = "tonnes"
_KEY_MEAN_RF = "mean_rf"

_FIR_SUMMARY_METRIC_KEYS = (
    constants.KEY_SEGMENT_COUNT,
    constants.KEY_CONTRAIL_SEGMENT_COUNT,
    constants.KEY_FLIGHT_DISTANCE_KM,
    constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM,
    constants.KEY_AREA_SQ_KM,
)


@dataclasses.dataclass(frozen=True)
class _Country:
  """Seed values for a synthetic country entry in the server test fixture."""

  iso_a2: str
  name: str
  population: int | None
  ghg_mtco2e: float | None
  area_sq_km: float
  co2e_gwp50_tonnes: float  # 2024 value.
  outgoing_co2e_gwp50_tonnes: float  # 2024 value.
  mean_rf_mw_per_sq_m: float


@dataclasses.dataclass(frozen=True)
class _Fir:
  """Seed values for a synthetic FIR entry in the server test fixture."""

  fir_id: str
  fir_icao: str
  firname: str
  type: str
  iso_a2: str
  area_sq_km: float
  co2e_gwp50_tonnes: float  # 2024 value.
  mean_rf_mw_per_sq_m: float


# Row order matters: tests read rows in file order.
_COUNTRIES: tuple[_Country, ...] = (
    _Country("BE", "Belgium", 10_000_000, 100.0, 30_000, 2.0e6, 1.0e6, 60.0),
    _Country("AT", "Austria", 9_000_000, 70.0, 80_000, 3.0e6, 1.2e6, 40.0),
    _Country(
        "NL", "Netherlands", 18_000_000, 150.0, 40_000, 1.5e6, 2.0e6, 50.0
    ),
    _Country("DE", "Germany", 80_000_000, 700.0, 350_000, 12e6, 8.0e6, 45.0),
    _Country("CH", "Switzerland", 8_000_000, 40.0, 40_000, 1.25e6, 1.5e6, 55.0),
    _Country("FR", "France", 65_000_000, 400.0, 550_000, 15e6, 6.0e6, 35.0),
    _Country(
        "GB", "United Kingdom", 67_000_000, 400.0, 240_000, 4.0e6, 9.0e6, 30.0
    ),
    _Country(
        constants.UNASSIGNED_ISO, "Kosovo", None, None, 10_000, 0.1e6, 0.0, 20.0
    ),
    _Country(
        "US", "United States", 330_000_000, 6000.0, 9.5e6, 40e6, 30e6, 10.0
    ),
    _Country(
        constants.UNASSIGNED_ISO,
        "N. Cyprus",
        None,
        None,
        3_000,
        0.02e6,
        0.0,
        15.0,
    ),
    _Country("SO", "Somalia", 17_000_000, 20.0, 600_000, 0.08e6, 0.01e6, 2.0),
    _Country(
        constants.UNASSIGNED_ISO,
        "Somaliland",
        None,
        None,
        170_000,
        0.01e6,
        0.0,
        1.0,
    ),
)

_FIRS: tuple[_Fir, ...] = (
    _Fir(
        "EHAA_AMSTERDAM",
        "EHAA",
        "AMSTERDAM",
        constants.FIR_TYPE_DOMESTIC,
        "NL",
        50_000,
        1.6e6,
        50.0,
    ),
    _Fir(
        "LFBB_BORDEAUX",
        "LFBB",
        "BORDEAUX",
        constants.FIR_TYPE_DOMESTIC,
        "FR",
        200_000,
        4.0e6,
        30.0,
    ),
    _Fir(
        "EDWW_BREMEN",
        "EDWW",
        "BREMEN",
        constants.FIR_TYPE_DOMESTIC,
        "DE",
        120_000,
        3.0e6,
        40.0,
    ),
    _Fir(
        "LFRR_BREST",
        "LFRR",
        "BREST",
        constants.FIR_TYPE_DOMESTIC,
        "FR",
        150_000,
        3.5e6,
        32.0,
    ),
    _Fir(
        "EBBU_BRUXELLES",
        "EBBU",
        "BRUXELLES",
        constants.FIR_TYPE_DOMESTIC,
        "BE",
        35_000,
        2.1e6,
        62.0,
    ),
    _Fir(
        "EDGG_LANGEN",
        "EDGG",
        "LANGEN",
        constants.FIR_TYPE_DOMESTIC,
        "DE",
        110_000,
        5.0e6,
        48.0,
    ),
    _Fir(
        "LFMM_MARSEILLE",
        "LFMM",
        "MARSEILLE",
        constants.FIR_TYPE_DOMESTIC,
        "FR",
        180_000,
        3.0e6,
        28.0,
    ),
    _Fir(
        "EDMM_MUNICH",
        "EDMM",
        "MUNICH",
        constants.FIR_TYPE_DOMESTIC,
        "DE",
        100_000,
        4.0e6,
        46.0,
    ),
    _Fir(
        "KZAK_OAKLAND_OCEANIC_EAST",
        "KZAK",
        "OAKLAND OCEANIC EAST",
        constants.FIR_TYPE_INTERNATIONAL,
        "US",
        20e6,
        10e6,
        5.0,
    ),
    _Fir(
        "KZAK_OAKLAND_OCEANIC_WEST",
        "KZAK",
        "OAKLAND OCEANIC WEST",
        constants.FIR_TYPE_INTERNATIONAL,
        "US",
        15e6,
        6.0e6,
        4.0,
    ),
    _Fir(
        "LFFF_PARIS",
        "LFFF",
        "PARIS",
        constants.FIR_TYPE_DOMESTIC,
        "FR",
        160_000,
        5.0e6,
        38.0,
    ),
    _Fir(
        "LFEE_REIMS",
        "LFEE",
        "REIMS",
        constants.FIR_TYPE_DOMESTIC,
        "FR",
        90_000,
        2.5e6,
        36.0,
    ),
    _Fir(
        "LSAS_SWITZERLAND",
        "LSAS",
        "SWITZERLAND",
        constants.FIR_TYPE_DOMESTIC,
        "CH",
        45_000,
        1.3e6,
        56.0,
    ),
    _Fir(
        "LOVV_WIEN",
        "LOVV",
        "WIEN",
        constants.FIR_TYPE_DOMESTIC,
        "AT",
        85_000,
        3.1e6,
        41.0,
    ),
)

# 24x12 hour-by-month matrices: one non-zero cell per country.
_TEMPORAL_CELLS: abc.Mapping[
    str, tuple[int, int, abc.Mapping[str, int | float]]
] = types.MappingProxyType({
    "CH": (
        12,
        5,
        types.MappingProxyType({
            constants.KEY_CO2E_GWP20_TONNES: 2500.0,
            constants.KEY_CO2E_GWP50_TONNES: 1250.5,
            constants.KEY_CO2E_GWP100_TONNES: 600.0,
            constants.KEY_CONTRAIL_SEGMENT_COUNT: 50,
            constants.KEY_SEGMENT_COUNT: 500,
            constants.KEY_TOTAL_FLIGHT_DISTANCE_KM: 25000.0,
            constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM: 2500.0,
        }),
    ),
    "DE": (
        14,
        5,
        types.MappingProxyType({
            constants.KEY_CO2E_GWP20_TONNES: 4000.0,
            constants.KEY_CO2E_GWP50_TONNES: 2000.0,
            constants.KEY_CO2E_GWP100_TONNES: 950.0,
            constants.KEY_CONTRAIL_SEGMENT_COUNT: 80,
            constants.KEY_SEGMENT_COUNT: 800,
            constants.KEY_TOTAL_FLIGHT_DISTANCE_KM: 40000.0,
            constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM: 4000.0,
        }),
    ),
})
_TEMPORAL_FIELDS = (
    constants.KEY_CO2E_GWP20_TONNES,
    constants.KEY_CO2E_GWP50_TONNES,
    constants.KEY_CO2E_GWP100_TONNES,
    constants.KEY_CONTRAIL_SEGMENT_COUNT,
    constants.KEY_SEGMENT_COUNT,
    constants.KEY_TOTAL_FLIGHT_DISTANCE_KM,
    constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM,
)
_TEMPORAL_NAMES: abc.Mapping[str, str] = types.MappingProxyType(
    {"CH": "Switzerland", "DE": "Germany"}
)

# Suffixes of numeric columns for which `metric_derivations` publishes a
# `<field>_rank` column.
_RANKED_SUFFIXES = (
    constants.KEY_MEAN_RF_MW_PER_SQ_M,
    constants.KEY_OUTGOING_FLIGHT_IMPACT,
    "_tonnes",
    "_vs_national_ghg",
    "_tonnes_per_capita",
)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
  """Returns `numerator / denominator` rounded, or `None` if undefined."""
  if numerator is None or not denominator:
    return None
  return round(numerator / denominator, 10)


def _round(value: float) -> float:
  """Rounds `value` to 6 decimal places for deterministic fixture output."""
  return round(value, 6)


def _add_ranks(rows: abc.Sequence[abc.MutableMapping[str, Any]]) -> None:
  """Adds a `<field>_rank` (1 = largest, `None` when missing) per ranked metric.

  Only the metric columns ranked by `metric_derivations` receive a
  `<field>_rank` column.

  Args:
    rows: Rows to rank in place.
  """
  numeric_fields: list[str] = []
  for row in rows:
    for key, value in row.items():
      if (
          isinstance(value, (int, float))
          and not isinstance(value, bool)
          and key.endswith(_RANKED_SUFFIXES)
          and key not in numeric_fields
      ):
        numeric_fields.append(key)
  for field in numeric_fields:
    rank_key = f"{field}_rank"
    present_rows = [row for row in rows if row.get(field) is not None]
    ordered_rows = sorted(present_rows, key=lambda row, f=field: -row[f])
    for row in rows:
      row[rank_key] = None
    for rank, row in enumerate(ordered_rows, start=1):
      row[rank_key] = rank


def _impact_block(
    prefix: str,
    tonnes_gwp50: float | None,
    total_gwp50: float,
    area: float | None,
    mean_rf: float | None,
    population: int | None,
    ghg_mtco2e: float | None,
    *,
    with_density: bool = True,
) -> dict[str, Any]:
  """Fields shared by national, outgoing, and FIR-attributed impact."""
  block: dict[str, Any] = {}
  if with_density:
    ef = None if tonnes_gwp50 is None else tonnes_gwp50 * _EF_MJ_PER_TONNE
    block[f"{prefix}{constants.KEY_IMPACT_DENSITY_SQ_KM}"] = _ratio(ef, area)
    block[f"{prefix}{constants.KEY_MEAN_RF_MW_PER_SQ_M}"] = mean_rf
  block[f"{prefix}{constants.KEY_CO2E_TONNES_SHARE}"] = _ratio(
      tonnes_gwp50, total_gwp50
  )
  for horizon, factor in GWP_FACTOR.items():
    tonnes = None if tonnes_gwp50 is None else _round(tonnes_gwp50 * factor)
    per_capita = _ratio(tonnes, population)
    base = f"{prefix}co2e_gwp{horizon}"
    block[f"{base}_tonnes"] = tonnes
    if with_density:
      block[f"{base}_tonnes_sq_km"] = _ratio(tonnes, area)
    block[f"{base}_vs_national_ghg"] = _ratio(
        tonnes, None if ghg_mtco2e is None else ghg_mtco2e * 1e6
    )
    block[f"{base}_tonnes_per_capita"] = per_capita
    block[f"{base}_per_capita_vs_world_avg"] = _ratio(
        per_capita, WORLD_AVG_GHG_PER_CAPITA_TONNES
    )
  return block


def _build_fir_row(fir: _Fir, *, factor: float, total: float) -> dict[str, Any]:
  """Builds a single synthetic per-FIR summary row."""
  tonnes = fir.co2e_gwp50_tonnes * factor
  segments = round(tonnes * _SEGMENTS_PER_TONNE)
  contrail_segments = round(segments * _CONTRAIL_SEGMENT_SHARE)
  row: dict[str, Any] = {
      constants.KEY_FIR_ID: fir.fir_id,
      constants.KEY_FIRNAME: fir.firname,
      constants.KEY_FIR_ICAO: fir.fir_icao,
      constants.KEY_FIR_TO_COUNTRY_ISO2S: [fir.iso_a2],
      constants.KEY_TYPE: fir.type,
      constants.KEY_MONTHS_COVERED: _MONTHS_COVERED,
      constants.KEY_DAYS_COVERED: _DAYS_COVERED,
      constants.KEY_SEGMENT_COUNT: segments,
      constants.KEY_CONTRAIL_SEGMENT_COUNT: contrail_segments,
      constants.KEY_TOTAL_FLIGHT_DISTANCE_KM: segments * _KM_PER_SEGMENT,
      constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM: (
          contrail_segments * _KM_PER_SEGMENT
      ),
      constants.KEY_AREA_SQ_KM: fir.area_sq_km,
      constants.KEY_IMPACT_DENSITY_SQ_KM: _ratio(
          tonnes * _EF_MJ_PER_TONNE, fir.area_sq_km
      ),
      constants.KEY_MEAN_RF_MW_PER_SQ_M: fir.mean_rf_mw_per_sq_m,
      constants.KEY_CO2E_TONNES_SHARE: _ratio(tonnes, total),
  }
  for horizon, gwp in GWP_FACTOR.items():
    base = f"co2e_gwp{horizon}_tonnes"
    row[base] = _round(tonnes * gwp)
    row[f"{base}_sq_km"] = _ratio(tonnes * gwp, fir.area_sq_km)
  return row


def _fir_rows(year: int) -> list[dict[str, Any]]:
  """Builds ranked synthetic per-FIR summary rows for `year`."""
  factor = YEAR_FACTOR[year]
  total = sum(f.co2e_gwp50_tonnes for f in _FIRS) * factor
  rows = [_build_fir_row(fir, factor=factor, total=total) for fir in _FIRS]
  _add_ranks(rows)
  return rows


def _fir_aggregate(
    iso_a2: str, fir_type: str | None, factor: float
) -> dict[str, Any]:
  """Sums the fixture FIRs of a country (`fir_type` None means all).

  The FIR identity lists are index-aligned and sorted by FIR ID, like the
  pipeline's.

  Args:
    iso_a2: Controlling country.
    fir_type: "domestic", "international", or None for both.
    factor: Year factor applied to the impact.

  Returns:
    The summed values and FIR identity lists.
  """
  firs = sorted(
      (
          fir
          for fir in _FIRS
          if fir.iso_a2 == iso_a2 and (fir_type is None or fir.type == fir_type)
      ),
      key=lambda fir: fir.fir_id,
  )
  tonnes = sum(f.co2e_gwp50_tonnes for f in firs) * factor
  area = sum(f.area_sq_km for f in firs)
  mean_rf = (
      _round(sum(f.mean_rf_mw_per_sq_m * f.area_sq_km for f in firs) / area)
      if area
      else None
  )
  segments = round(tonnes * _SEGMENTS_PER_TONNE)
  contrail_segments = round(segments * _CONTRAIL_SEGMENT_SHARE)
  return {
      _KEY_TONNES: tonnes,
      constants.KEY_AREA_SQ_KM: float(area),
      constants.KEY_SEGMENT_COUNT: segments,
      constants.KEY_CONTRAIL_SEGMENT_COUNT: contrail_segments,
      constants.KEY_FLIGHT_DISTANCE_KM: segments * _KM_PER_SEGMENT,
      constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM: (
          contrail_segments * _KM_PER_SEGMENT
      ),
      _KEY_MEAN_RF: mean_rf,
      constants.KEY_FIRNAME: [fir.firname for fir in firs],
      constants.KEY_FIR_ICAO: [fir.fir_icao for fir in firs],
      constants.KEY_FIR_ID: [fir.fir_id for fir in firs],
  }


def _populate_country_fir_summary(
    row: dict[str, Any],
    *,
    dom: dict[str, Any],
    intl: dict[str, Any],
    total: dict[str, Any],
) -> None:
  """Populates FIR summary fields on `row`."""
  # The pipeline publishes FIR summary and identity fields only for countries
  # that control at least one FIR.
  controls_firs = bool(total[constants.KEY_FIR_ID])
  for prefix, agg in (
      (constants.PREFIX_FIR_DOM, dom),
      (constants.PREFIX_FIR_INT, intl),
      (constants.PREFIX_FIR_TOTAL, total),
  ):
    for suffix in _FIR_SUMMARY_METRIC_KEYS:
      row[f"{prefix}{suffix}"] = agg[suffix] if controls_firs else None
    for field in (
        constants.KEY_FIR_ID,
        constants.KEY_FIRNAME,
        constants.KEY_FIR_ICAO,
    ):
      row[f"{prefix}{field}"] = agg[field] if controls_firs else None


def _populate_country_fir_impacts(
    row: dict[str, Any],
    country: _Country,
    *,
    dom: dict[str, Any],
    intl: dict[str, Any],
    total: dict[str, Any],
    total_fir: float,
    ef: float,
) -> None:
  """Populates FIR impact blocks and impact aliases on `row`."""
  controls_firs = bool(total[constants.KEY_FIR_ID])
  for prefix, agg in (
      (constants.PREFIX_FIR_INT, intl),
      (constants.PREFIX_FIR_DOM, dom),
      (constants.PREFIX_FIR_TOTAL, total),
  ):
    row.update(
        _impact_block(
            prefix,
            agg[_KEY_TONNES] if controls_firs else None,
            total_fir,
            agg[constants.KEY_AREA_SQ_KM] or None,
            agg[_KEY_MEAN_RF] if controls_firs else None,
            country.population,
            country.ghg_mtco2e,
        )
    )


def _populate_country_fir_fields(
    row: dict[str, Any],
    country: _Country,
    *,
    dom: dict[str, Any],
    intl: dict[str, Any],
    total: dict[str, Any],
    total_fir: float,
    ef: float,
) -> None:
  """Populates both FIR summary and impact fields on `row`."""
  _populate_country_fir_summary(row, dom=dom, intl=intl, total=total)
  _populate_country_fir_impacts(
      row, country, dom=dom, intl=intl, total=total, total_fir=total_fir, ef=ef
  )


def _country_flight_base_dict(
    country: _Country,
    *,
    ef: float,
    outgoing_ef: float,
    segments: int,
) -> dict[str, Any]:
  """Constructs identity, segment, and flight impact keys."""
  contrail_segments = round(segments * _CONTRAIL_SEGMENT_SHARE)
  return {
      constants.KEY_NAME: country.name,
      constants.KEY_ISO_A2: country.iso_a2,
      constants.KEY_ISO_A3: _ISO_A3_BY_ISO2.get(country.iso_a2, ""),
      constants.KEY_MONTHS_COVERED: _MONTHS_COVERED,
      constants.KEY_DAYS_COVERED: _DAYS_COVERED,
      constants.KEY_SEGMENT_COUNT: segments,
      constants.KEY_CONTRAIL_SEGMENT_COUNT: contrail_segments,
      constants.KEY_TOTAL_FLIGHT_DISTANCE_KM: segments * _KM_PER_SEGMENT,
      constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM: (
          contrail_segments * _KM_PER_SEGMENT
      ),
      constants.KEY_TOTAL_FLIGHT_IMPACT: _round(ef + outgoing_ef),
      constants.KEY_LOCAL_FLIGHT_IMPACT: ef,
      constants.KEY_OUTGOING_FLIGHT_IMPACT: outgoing_ef,
      constants.KEY_AREA_SQ_KM: country.area_sq_km,
      constants.KEY_GHG_MTCO2E: country.ghg_mtco2e,
      constants.KEY_POPULATION: country.population,
      constants.KEY_IN_COUNTRY_IMPACT: _ratio(ef, ef + outgoing_ef),
  }


def _build_country_flight_dict(
    country: _Country,
    *,
    national: float,
    outgoing: float,
    total_national: float,
    total_outgoing: float,
    ef: float,
    outgoing_ef: float,
    segments: int,
) -> dict[str, Any]:
  """Constructs base country identity, flight impact, and ratio fields."""
  nat_args = (country.population, country.ghg_mtco2e)
  return {
      **_country_flight_base_dict(
          country, ef=ef, outgoing_ef=outgoing_ef, segments=segments
      ),
      **_impact_block(
          "",
          national,
          total_national,
          country.area_sq_km,
          country.mean_rf_mw_per_sq_m,
          *nat_args,
      ),
      constants.KEY_OVER_REGION_VS_OUTGOING_IMPACT: _ratio(national, outgoing),
      **_impact_block(
          constants.PREFIX_OUTGOING_FLIGHT,
          outgoing,
          total_outgoing,
          None,
          None,
          *nat_args,
          with_density=False,
      ),
  }


def _build_country_row(
    country: _Country,
    *,
    factor: float,
    out_factor: float,
    totals: tuple[float, float, float],
) -> dict[str, Any]:
  """Builds a single synthetic per-country summary row."""
  total_national, total_outgoing, total_fir = totals
  national = country.co2e_gwp50_tonnes * factor
  outgoing = country.outgoing_co2e_gwp50_tonnes * out_factor
  ef = _round(national * _EF_MJ_PER_TONNE)
  outgoing_ef = _round(outgoing * _EF_MJ_PER_TONNE)
  segments = round(national * _SEGMENTS_PER_TONNE)
  dom = _fir_aggregate(country.iso_a2, constants.FIR_TYPE_DOMESTIC, factor)
  intl = _fir_aggregate(
      country.iso_a2, constants.FIR_TYPE_INTERNATIONAL, factor
  )
  total = _fir_aggregate(country.iso_a2, None, factor)
  row = _build_country_flight_dict(
      country,
      national=national,
      outgoing=outgoing,
      total_national=total_national,
      total_outgoing=total_outgoing,
      ef=ef,
      outgoing_ef=outgoing_ef,
      segments=segments,
  )
  _populate_country_fir_fields(
      row,
      country,
      dom=dom,
      intl=intl,
      total=total,
      total_fir=total_fir,
      ef=ef,
  )
  return row


def _country_rows(year: int) -> list[dict[str, Any]]:
  """Builds ranked synthetic per-country summary rows for `year`."""
  factor = YEAR_FACTOR[year]
  out_factor = OUTGOING_YEAR_FACTOR[year]
  totals = (
      sum(country.co2e_gwp50_tonnes for country in _COUNTRIES) * factor,
      sum(country.outgoing_co2e_gwp50_tonnes for country in _COUNTRIES)
      * out_factor,
      sum(f.co2e_gwp50_tonnes for f in _FIRS) * factor,
  )
  rows = [
      _build_country_row(
          country, factor=factor, out_factor=out_factor, totals=totals
      )
      for country in _COUNTRIES
  ]
  _add_ranks(rows)
  return rows


def _single_cell_matrix(
    hour: int, month: int, value: int | float
) -> list[list[int]] | list[list[float]]:
  """Returns a zero 24x12 matrix with `value` at `(hour, month)`."""
  if isinstance(value, int) and not isinstance(value, bool):
    int_grid = [[0] * 12 for _ in range(24)]
    int_grid[hour][month] = value
    return int_grid
  float_grid = [[0.0] * 12 for _ in range(24)]
  float_grid[hour][month] = float(value)
  return float_grid


def _temporal_rows() -> list[dict[str, Any]]:
  """Hour-by-month matrices for CH and DE."""
  rows: list[dict[str, Any]] = []
  for iso in ("CH", "DE"):
    hour, month, values = _TEMPORAL_CELLS[iso]
    row: dict[str, Any] = {
        constants.KEY_NAME: _TEMPORAL_NAMES[iso],
        constants.KEY_ISO_A2: iso,
    }
    for field in _TEMPORAL_FIELDS:
      row[field] = _single_cell_matrix(hour, month, values[field])
    rows.append(row)
  return rows


def run_id_for(year: int) -> str:
  """Returns the synthetic fixture run identifier for `year`.

  Args:
    year: Four-digit calendar year.

  Returns:
    The synthetic run identifier string.
  """
  return f"fixture-{year}"


def build_fixture() -> dict[str, Any]:
  """Returns `{relative path: JSON payload}` for every fixture file.

  Returns:
    Mapping of relative file path to JSON-serializable fixture payload.
  """
  files: dict[str, Any] = {}
  for year in YEAR_FACTOR:
    dataset_dir = f"{year}/{DATASET}"
    artifact_dir = f"{dataset_dir}/{constants.RUNS_DIR}/{run_id_for(year)}"
    files[f"{dataset_dir}/{constants.POINTER_FILENAME}"] = {
        constants.KEY_SCHEMA_VERSION: constants.POINTER_SCHEMA_VERSION,
        constants.KEY_RUN_ID: run_id_for(year),
        constants.KEY_PUBLISHED_AT: "",
    }
    files[f"{artifact_dir}/{constants.COUNTRY_EXPORT_FILENAME}"] = (
        _country_rows(year)
    )
    files[f"{artifact_dir}/{constants.FIR_EXPORT_FILENAME}"] = _fir_rows(year)
    files[f"{artifact_dir}/{constants.COUNTRY_TEMPORAL_EXPORT_FILENAME}"] = (
        _temporal_rows()
    )
  return files
