"""Derived metric calculations, GWP conversions, rankings, and rollups.

This module is pure computation with zero Apache Beam or runner dependencies.
Both the Beam pipeline and the standalone derive binary use it to turn
intermediate aggregates into published country and FIR summary datasets.
"""

import calendar
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
import dataclasses
import math
from typing import Any, NamedTuple, TypedDict

from absl import logging

from common import constants

# 86,400 s/day expressed in kiloseconds. Dividing accumulated energy forcing in
# MJ by kiloseconds yields kW, and kW per km^2 equals mW per m^2, the unit of
# the published mean radiative forcing columns.
_KILOSECONDS_PER_DAY = 86.4

# Converts national GHG baselines published in MtCO2e to tonnes.
_TONNES_PER_MEGATONNE = 1e6


class _RankedRowItem(NamedTuple):
  """Finite metric value paired with its mutable row dictionary for ranking."""

  metric_value: float
  row: MutableMapping[str, Any]


@dataclasses.dataclass(slots=True)
class _CountryTemporalAccumulator:
  """Mutable country metric accumulator for one `(month, local_hour)` bucket."""

  total_segment_count: int = 0
  contrail_segment_count: int = 0
  proportioned_ef_mj: float = 0.0
  local_ef_mj: float = 0.0
  total_ef_mj: float = 0.0
  outgoing_ef_mj: float = 0.0
  total_distance_km: float = 0.0
  contrail_distance_km: float = 0.0


@dataclasses.dataclass(slots=True)
class _FirAnnualAccumulator:
  """Mutable accumulator for annual FIR segment and distance totals."""

  total_segment_count: int = 0
  contrail_segment_count: int = 0
  prop_sum_ef_mj: float = 0.0
  total_flight_distance_km: float = 0.0
  contrail_flight_distance_km: float = 0.0


class _FirMetadataEntry(TypedDict):
  """Normalized metadata for a single FIR aggregated across rows."""

  firname: str
  fir_icao: str
  # Controlling-country ISO-2 codes in input order; the first is primary.
  fir_iso2: list[str]
  fir_type: str
  area: float


def mw_per_sq_m_divisor_for_months(
    year: int, months: Iterable[int]
) -> tuple[float, int, int]:
  """Divisor, months_covered, and days_covered for a set of observed months.

  When a dataset spans only a subset of the calendar year, dividing accumulated
  energy forcing by 365 (or 366) days understates annual-summary mean radiative
  forcing by `days_in_year / days_covered`. This helper sums the exact days of
  the distinct calendar months present in the dataset for `year` (falling back
  to the full calendar year only when `months` is empty).

  Args:
    year: Calendar year.
    months: Observed month values (either 1..12 or YYYYMM).

  Returns:
    Tuple of `(divisor, months_covered, days_covered)`.
  """
  year_number = int(year)
  valid_months: set[int] = set()
  for m_raw in months:
    month_number = int(m_raw)
    if month_number >= 100:
      if (month_number // 100) != year_number:
        continue
      m_idx = month_number % 100
    else:
      m_idx = month_number
    if 1 <= m_idx <= 12:
      valid_months.add(m_idx)
  if valid_months:
    months_covered = len(valid_months)
    days_covered = sum(
        calendar.monthrange(year_number, month_item)[1]
        for month_item in valid_months
    )
    return (
        float(days_covered) * _KILOSECONDS_PER_DAY,
        months_covered,
        days_covered,
    )
  days_covered = 366 if calendar.isleap(year_number) else 365
  return float(days_covered) * _KILOSECONDS_PER_DAY, 0, days_covered


def _observed_fraction_of_year(year: int, days_covered: int) -> float:
  """Returns the share of calendar `year` covered by `days_covered` days.

  CO2e totals cover only the observed days, while the national GHG and world
  per-capita baselines they are compared against are annual. Scaling those
  baselines by this fraction keeps the ratios comparable for partial-year
  datasets; a full year gives exactly 1.0.

  Args:
    year: Calendar year.
    days_covered: Observed days, as returned by
      `mw_per_sq_m_divisor_for_months`.

  Returns:
    `days_covered / days_in_year`, in (0, 1] for valid inputs.

  Raises:
    ValueError: If `days_covered` is not in [1, days_in_year].
  """
  days_in_year = 366 if calendar.isleap(int(year)) else 365
  if not 1 <= days_covered <= days_in_year:
    raise ValueError(
        f"days_covered={days_covered} is outside [1, {days_in_year}] for {year}"
    )
  return days_covered / days_in_year


def _log_missing_baselines(
    country_rows: Sequence[Mapping[str, Any]],
    baseline_stats: Mapping[str, Any],
) -> None:
  """Logs countries whose per-capita or vs-GHG metrics lack baselines."""
  missing: dict[str, list[str]] = {
      constants.KEY_POPULATION: [],
      constants.KEY_GHG_MTCO2E: [],
  }
  for row in country_rows:
    iso = str(row.get(constants.KEY_ISO_A2) or "")
    if not iso or iso.startswith("-"):
      continue
    c_stats = baseline_stats.get(iso, {})
    for field, isos in missing.items():
      if c_stats.get(field) is None:
        isos.append(iso)
  for field, isos in missing.items():
    if isos:
      logging.warning(
          "%d published countries have no %s baseline (per-capita / "
          "vs-national-GHG metrics stay empty): %s",
          len(isos),
          field,
          ", ".join(sorted(isos)),
      )


def _make_empty_24x12() -> list[list[float]]:
  """Returns a zero-initialized 24x12 (hours x months) float matrix."""
  return [[0.0] * 12 for _ in range(24)]


def _make_empty_24x12_int() -> list[list[int]]:
  """Returns a zero-initialized 24x12 (hours x months) integer matrix."""
  return [[0] * 12 for _ in range(24)]


def _make_empty_temporal_bundle(
    iso_a2: str,
    name: str,
    *,
    fir_id: str | None = None,
    firname: str | None = None,
    fir_icao: str | None = None,
    fir_to_country_iso2s: Sequence[str] | None = None,
    fir_type: str | None = None,
) -> dict[str, Any]:
  """Builds an empty pre-aggregated 24x12 temporal matrix dictionary."""
  bundle: dict[str, Any] = {
      constants.KEY_CO2E_GWP20_TONNES: _make_empty_24x12(),
      constants.KEY_CO2E_GWP50_TONNES: _make_empty_24x12(),
      constants.KEY_CO2E_GWP100_TONNES: _make_empty_24x12(),
      constants.KEY_CONTRAIL_SEGMENT_COUNT: _make_empty_24x12_int(),
      constants.KEY_SEGMENT_COUNT: _make_empty_24x12_int(),
      constants.KEY_TOTAL_FLIGHT_DISTANCE_KM: _make_empty_24x12(),
      constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM: _make_empty_24x12(),
  }
  if fir_icao is not None or firname is not None or fir_id is not None:
    bundle[constants.KEY_FIR_ID] = fir_id or constants.make_fir_id(
        fir_icao, firname
    )
    bundle[constants.KEY_FIRNAME] = firname or ""
    bundle[constants.KEY_FIR_ICAO] = fir_icao or ""
    bundle[constants.KEY_FIR_TO_COUNTRY_ISO2S] = list(
        fir_to_country_iso2s or []
    )
    bundle[constants.KEY_TYPE] = fir_type or constants.FIR_TYPE_DOMESTIC
  else:
    bundle[constants.KEY_NAME] = name
    bundle[constants.KEY_ISO_A2] = iso_a2
  return bundle


def _accumulate_into_temporal_bundle(
    bundle: MutableMapping[str, Any],
    *,
    month: int,
    local_hour: int,
    total_segment_count: int,
    contrail_cnt: int,
    proportioned_ef_mj: float,
    total_distance_km: float = 0.0,
    contrail_km: float = 0.0,
) -> None:
  """Adds a (month, local_hour) observation into a 24x12 temporal bundle.

  Args:
    bundle: Temporal bundle from `_make_empty_temporal_bundle` to update in
      place.
    month: Month as 1..12 or YYYYMM.
    local_hour: Local hour of day, 0..23.
    total_segment_count: Segments in the bucket.
    contrail_cnt: Contrail-forming segments in the bucket.
    proportioned_ef_mj: Attributed energy forcing in MJ.
    total_distance_km: Attributed flight distance in km.
    contrail_km: Attributed contrail-forming flight distance in km.

  Raises:
    ValueError: If the month or hour is out of range. Such a row means the
      upstream aggregation is broken; dropping it would silently lose impact
      from the heatmap while the annual totals still count it.
  """
  month_number = int(month)
  m_idx = (month_number % 100) if month_number >= 100 else month_number
  hour_number = int(local_hour)
  if not (1 <= m_idx <= 12 and 0 <= hour_number <= 23):
    entity = bundle.get(constants.KEY_FIR_ID) or bundle.get(
        constants.KEY_ISO_A2
    )
    raise ValueError(
        f"Temporal bucket out of range for {entity!r}: month={month!r},"
        f" local_hour={local_hour!r}"
    )
  col_idx = m_idx - 1
  row_idx = hour_number
  ef = float(proportioned_ef_mj)
  for key, multiplier in (
      (constants.KEY_CO2E_GWP20_TONNES, constants.GWP20_TONNES_PER_MJ),
      (constants.KEY_CO2E_GWP50_TONNES, constants.GWP50_TONNES_PER_MJ),
      (constants.KEY_CO2E_GWP100_TONNES, constants.GWP100_TONNES_PER_MJ),
  ):
    bundle[key][row_idx][col_idx] += ef * multiplier
  bundle[constants.KEY_CONTRAIL_SEGMENT_COUNT][row_idx][col_idx] += int(
      contrail_cnt
  )
  bundle[constants.KEY_SEGMENT_COUNT][row_idx][col_idx] += int(
      total_segment_count
  )
  bundle[constants.KEY_TOTAL_FLIGHT_DISTANCE_KM][row_idx][col_idx] += float(
      total_distance_km
  )
  bundle[constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM][row_idx][col_idx] += float(
      contrail_km
  )


def _per_area(value: float, area: float, divisor: float = 1.0) -> float | None:
  """Returns `value / (area * divisor)`, or `None` if `area` is not > 0.

  An entity without a positive area (e.g. a country missing from the boundary
  file, or a country with no domestic FIR) has an undefined per-area metric.
  `None` keeps it unranked and displayed as "N/A"; `0.0` would rank it
  above every net-cooling (negative-impact) entity.

  Args:
    value: Numerator (e.g. energy forcing in MJ or tonnes CO2e).
    area: Area in km².
    divisor: Extra denominator factor (e.g. the mW/m² unit divisor).
  """
  if area > 0:
    return value / (area * divisor)
  return None


def _ratio(numerator: float, denominator: float) -> float | None:
  """Returns `numerator / denominator`, or `None` if denominator is 0 or NaN.

  Energy forcing and resulting CO2e impacts can be negative (net-cooling).
  Returning `None` when the denominator is 0 (or non-finite) preserves
  meaningful signed ratios for cooling entities, while preventing division
  by zero or undefined ranks.

  Args:
    numerator: Numerator value.
    denominator: Denominator value.

  Returns:
    Signed ratio, or None if the denominator is 0 or either input is non-finite.
  """
  if (
      denominator != 0.0
      and math.isfinite(denominator)
      and math.isfinite(numerator)
  ):
    return numerator / denominator
  return None


def _assign_descending_ranks(
    rows: Sequence[MutableMapping[str, Any]],
    rank_fields: Sequence[tuple[str, str]],
    tiebreak_fields: Sequence[str],
) -> None:
  """Assigns unique 1-based descending ranks to rows with finite values.

  Rows where the metric value is `None`, a boolean, or non-finite receive
  `None` as their rank so missing data is never coerced to `0.0` and ranked
  ahead of net-cooling (negative-impact) entities.

  Tied values are ordered by the `tiebreak_fields` values (compared as
  strings) ascending, so ranks are deterministic regardless of the
  runner-dependent input row order.

  Args:
    rows: Mutable row dictionaries to update in place.
    rank_fields: Sequence of `(value_field, rank_field)` column name pairs.
    tiebreak_fields: Columns that together identify an entity, used in order to
      break ties between equal metric values.
  """
  for val_field, rank_field in rank_fields:
    ranked_pairs: list[_RankedRowItem] = []
    for item in rows:
      raw_val = item.get(val_field)
      if raw_val is None or isinstance(raw_val, bool):
        item[rank_field] = None
        continue
      try:
        numeric_val = float(raw_val)
      except (TypeError, ValueError):
        item[rank_field] = None
        continue
      if not math.isfinite(numeric_val):
        item[rank_field] = None
        continue
      ranked_pairs.append(_RankedRowItem(metric_value=numeric_val, row=item))

    ranked_pairs.sort(
        key=lambda entry: (
            -entry.metric_value,
            tuple(str(entry.row.get(f, "")) for f in tiebreak_fields),
        )
    )
    for rank_idx, entry in enumerate(ranked_pairs, start=1):
      entry.row[rank_field] = rank_idx


def _initial_observed_months(observed_months: Iterable[int] | None) -> set[int]:
  """Returns the caller-supplied observed months as a mutable set of ints."""
  if observed_months is None:
    return set()
  return {int(month_item) for month_item in observed_months}


def _is_out_of_year(month_number: int, year: int) -> bool:
  """Returns whether a YYYYMM `month_number` falls outside `year`.

  Plain 1..12 month values carry no year and are never out of year.

  Args:
    month_number: Month value, either 1..12 or YYYYMM.
    year: The calendar year being processed.
  """
  return month_number >= 100 and (month_number // 100) != year


def _log_dropped_out_of_year_rows(
    dropped_count: int, year: int, *, row_kind: str
) -> None:
  """Warns about aggregate rows dropped for falling outside `year`."""
  if dropped_count > 0:
    logging.warning(
        "Filtered %d out-of-year %s aggregate rows (expected year %d).",
        dropped_count,
        row_kind,
        year,
    )


def _register_country_entity(
    canonical_names: MutableMapping[Any, str],
    country_name: str,
    iso_a2: str,
) -> tuple[Any, str]:
  """Normalizes a country's identity and records its preferred display name.

  Official ISO-2 codes identify an entity on their own. Unofficial codes that
  start with `-` (e.g. `-99`) are shared by several territories, so those
  entities are keyed by `(iso, NAME)` instead. A real name replaces a
  previously recorded name that merely echoed the ISO code.

  Args:
    canonical_names: Entity key to display name mapping, updated in place.
    country_name: Raw country name from the aggregate row.
    iso_a2: Raw ISO-2 code from the aggregate row.

  Returns:
    Tuple of `(entity_key, iso_clean)`.
  """
  iso_clean = str(iso_a2).strip().upper()
  name_clean = str(country_name).strip() or iso_clean
  entity_key: Any = (
      (iso_clean, name_clean.upper())
      if iso_clean.startswith("-")
      else iso_clean
  )
  existing_name = canonical_names.get(entity_key)
  if existing_name is None or (
      existing_name.upper() == iso_clean and name_clean.upper() != iso_clean
  ):
    canonical_names[entity_key] = name_clean
  return entity_key, iso_clean


def _country_accumulator_from_metrics(
    metrics: constants.CountryMetricsTuple,
) -> _CountryTemporalAccumulator:
  """Converts one country metrics tuple into a typed accumulator."""
  (
      total_segment_count,
      contrail_cnt,
      proportioned_ef_mj,
      local_ef,
      total_ef,
      out_ef,
      total_distance_km,
      contrail_km,
  ) = metrics
  return _CountryTemporalAccumulator(
      total_segment_count=int(total_segment_count),
      contrail_segment_count=int(contrail_cnt),
      proportioned_ef_mj=float(proportioned_ef_mj),
      local_ef_mj=float(local_ef),
      total_ef_mj=float(total_ef),
      outgoing_ef_mj=float(out_ef),
      total_distance_km=float(total_distance_km),
      contrail_distance_km=float(contrail_km),
  )


def _merge_country_temporal_totals(
    merged_temporal_inputs: MutableMapping[
        tuple[Any, str, int, int], _CountryTemporalAccumulator
    ],
    temporal_key: tuple[Any, str, int, int],
    totals: _CountryTemporalAccumulator,
) -> None:
  """Stores `totals` under `temporal_key`, adding to any existing totals."""
  existing = merged_temporal_inputs.get(temporal_key)
  if existing is None:
    merged_temporal_inputs[temporal_key] = totals
    return
  existing.total_segment_count += totals.total_segment_count
  existing.contrail_segment_count += totals.contrail_segment_count
  existing.proportioned_ef_mj += totals.proportioned_ef_mj
  existing.local_ef_mj += totals.local_ef_mj
  existing.total_ef_mj += totals.total_ef_mj
  existing.outgoing_ef_mj += totals.outgoing_ef_mj
  existing.total_distance_km += totals.total_distance_km
  existing.contrail_distance_km += totals.contrail_distance_km


def _aggregate_country_temporal_inputs(
    country_grouped_rows: Sequence[constants.CountryRow],
    year: int,
    observed_months: Iterable[int] | None = None,
) -> tuple[
    dict[tuple[Any, str, int, int], _CountryTemporalAccumulator],
    dict[Any, str],
    set[int],
]:
  """Filters country rows by year and aggregates metrics by temporal key."""
  canonical_names: dict[Any, str] = {}
  merged_temporal_inputs: dict[
      tuple[Any, str, int, int], _CountryTemporalAccumulator
  ] = {}
  dropped_out_of_year_rows = 0
  in_year_months = _initial_observed_months(observed_months)

  for (
      country_name,
      iso_a2,
      month,
      local_hour,
  ), metrics in country_grouped_rows:
    month_number = int(month)
    if _is_out_of_year(month_number, year):
      dropped_out_of_year_rows += 1
      continue
    in_year_months.add(month_number)
    entity_key, iso_clean = _register_country_entity(
        canonical_names, country_name, iso_a2
    )
    _merge_country_temporal_totals(
        merged_temporal_inputs,
        (entity_key, iso_clean, month_number, int(local_hour)),
        _country_accumulator_from_metrics(metrics),
    )

  _log_dropped_out_of_year_rows(
      dropped_out_of_year_rows, year, row_kind="country"
  )
  return merged_temporal_inputs, canonical_names, in_year_months


def _resolve_country_display_name(
    name: str, iso_clean: str, c_stats: Mapping[str, Any]
) -> str:
  """Prefers the baseline name when `name` merely echoes the ISO code."""
  if name.upper() == iso_clean and (
      baseline_name := c_stats.get(constants.KEY_NAME)
  ):
    return str(baseline_name)
  return name


def _country_area(
    country_areas: Mapping[str, float], iso_clean: str, canonical_name: str
) -> float:
  """Looks up a country's area in km^2.

  Unofficial `-` ISO codes are shared by several territories, so their areas
  are keyed by `"<iso>|<NAME>"`.

  Args:
    country_areas: Mapping of area keys to area in km^2.
    iso_clean: Normalized ISO-2 code.
    canonical_name: Display name of the country.

  Returns:
    The area in km^2, or 0.0 when the country has no known area.
  """
  if iso_clean.startswith("-"):
    return country_areas.get(f"{iso_clean}|{canonical_name.upper()}", 0.0)
  return country_areas.get(iso_clean, 0.0)


def _make_initial_country_annual_row(
    *,
    canonical_name: str,
    iso_clean: str,
    c_stats: Mapping[str, Any],
    area: float,
) -> dict[str, Any]:
  """Creates a zero-filled annual country row carrying baseline statistics."""
  return {
      constants.KEY_NAME: canonical_name,
      constants.KEY_ISO_A2: iso_clean,
      constants.KEY_ISO_A3: str(c_stats.get(constants.KEY_ISO_A3) or ""),
      constants.KEY_SEGMENT_COUNT: 0,
      constants.KEY_CONTRAIL_SEGMENT_COUNT: 0,
      constants.KEY_TOTAL_FLIGHT_DISTANCE_KM: 0.0,
      constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM: 0.0,
      constants.KEY_PROP_SUM_EF_MJ: 0.0,
      constants.KEY_TOTAL_FLIGHT_IMPACT: 0.0,
      constants.KEY_LOCAL_FLIGHT_IMPACT: 0.0,
      constants.KEY_OUTGOING_FLIGHT_IMPACT: 0.0,
      constants.KEY_AREA_SQ_KM: area,
      constants.KEY_GHG_MTCO2E: float(
          c_stats.get(constants.KEY_GHG_MTCO2E) or 0.0
      ),
      constants.KEY_POPULATION: int(c_stats.get(constants.KEY_POPULATION) or 0),
  }


def _add_totals_to_country_annual_row(
    row: MutableMapping[str, Any], totals: _CountryTemporalAccumulator
) -> None:
  """Adds one temporal bucket's totals into an annual country row."""
  row[constants.KEY_SEGMENT_COUNT] += totals.total_segment_count
  row[constants.KEY_CONTRAIL_SEGMENT_COUNT] += totals.contrail_segment_count
  row[constants.KEY_TOTAL_FLIGHT_DISTANCE_KM] += totals.total_distance_km
  row[constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM] += totals.contrail_distance_km
  row[constants.KEY_PROP_SUM_EF_MJ] += totals.proportioned_ef_mj
  row[constants.KEY_TOTAL_FLIGHT_IMPACT] += totals.total_ef_mj
  row[constants.KEY_LOCAL_FLIGHT_IMPACT] += totals.local_ef_mj
  row[constants.KEY_OUTGOING_FLIGHT_IMPACT] += totals.outgoing_ef_mj


def _build_initial_country_annual_rows(
    merged_temporal_inputs: Mapping[
        tuple[Any, str, int, int], _CountryTemporalAccumulator
    ],
    canonical_names: Mapping[Any, str],
    *,
    country_areas: Mapping[str, float],
    baseline_stats: Mapping[str, Any],
) -> dict[Any, dict[str, Any]]:
  """Sums temporal buckets into one annual row per country entity."""
  country_annual_agg: dict[Any, dict[str, Any]] = {}
  for (entity_key, iso_clean, _, _), totals in merged_temporal_inputs.items():
    annual_row = country_annual_agg.get(entity_key)
    if annual_row is None:
      c_stats = baseline_stats.get(iso_clean, {})
      canonical_name = _resolve_country_display_name(
          canonical_names.get(entity_key, iso_clean), iso_clean, c_stats
      )
      annual_row = _make_initial_country_annual_row(
          canonical_name=canonical_name,
          iso_clean=iso_clean,
          c_stats=c_stats,
          area=_country_area(country_areas, iso_clean, canonical_name),
      )
      country_annual_agg[entity_key] = annual_row
    _add_totals_to_country_annual_row(annual_row, totals)
  return country_annual_agg


def _build_country_temporal_rows(
    merged_temporal_inputs: Mapping[
        tuple[Any, str, int, int], _CountryTemporalAccumulator
    ],
    canonical_names: Mapping[Any, str],
    baseline_stats: Mapping[str, Any],
) -> list[dict[str, Any]]:
  """Builds one 24x12 temporal bundle per country entity.

  Args:
    merged_temporal_inputs: Country totals keyed by `(entity_key, iso, month,
      local_hour)`.
    canonical_names: Entity key to display name mapping.
    baseline_stats: Per-country baseline statistics, used for display names.

  Returns:
    One bundle per country entity. Empty if there are no inputs.
  """
  entity_temporal_bundles: dict[Any, dict[str, Any]] = {}
  for (
      entity_key,
      iso_clean,
      month,
      local_hour,
  ), totals in merged_temporal_inputs.items():
    bundle = entity_temporal_bundles.get(entity_key)
    if bundle is None:
      canonical_name = _resolve_country_display_name(
          canonical_names.get(entity_key, iso_clean),
          iso_clean,
          baseline_stats.get(iso_clean, {}),
      )
      bundle = _make_empty_temporal_bundle(iso_clean, canonical_name)
      entity_temporal_bundles[entity_key] = bundle
    _accumulate_into_temporal_bundle(
        bundle,
        month=month,
        local_hour=local_hour,
        total_segment_count=totals.total_segment_count,
        contrail_cnt=totals.contrail_segment_count,
        proportioned_ef_mj=totals.proportioned_ef_mj,
        total_distance_km=totals.total_distance_km,
        contrail_km=totals.contrail_distance_km,
    )
  return list(entity_temporal_bundles.values())


def _ensure_fir_controlling_countries(
    country_annual_agg: MutableMapping[Any, dict[str, Any]],
    *,
    fir_summary_by_iso: Mapping[str, Mapping[str, Any]],
    country_names: Mapping[str, str] | None,
    baseline_stats: Mapping[str, Any],
    country_areas: Mapping[str, float],
) -> None:
  """Ensures FIR-controlling countries receive a row even with 0 segments."""
  country_names_map = country_names or {}
  for fir_iso_raw in sorted(fir_summary_by_iso.keys()):
    iso_clean = str(fir_iso_raw or "").strip().upper()
    if (
        not iso_clean
        or iso_clean.startswith("-")
        or iso_clean in country_annual_agg
    ):
      continue
    c_stats = baseline_stats.get(iso_clean, {})
    canonical_name = str(
        country_names_map.get(iso_clean)
        or c_stats.get(constants.KEY_NAME)
        or iso_clean
    ).strip()
    country_annual_agg[iso_clean] = _make_initial_country_annual_row(
        canonical_name=canonical_name,
        iso_clean=iso_clean,
        c_stats=c_stats,
        area=_country_area(country_areas, iso_clean, canonical_name),
    )


_GWP_HORIZON_FACTORS = (
    (20, constants.GWP20_TONNES_PER_MJ),
    (50, constants.GWP50_TONNES_PER_MJ),
    (100, constants.GWP100_TONNES_PER_MJ),
)


def _vs_world_avg(
    per_capita: float | None, year_fraction: float
) -> float | None:
  """Compares observed-period per-capita CO2e with the world average.

  Args:
    per_capita: CO2e tonnes per capita over the observed days, or None.
    year_fraction: Observed share of the year; scales the annual world average.

  Returns:
    The ratio, or None when `per_capita` is None.
  """
  if per_capita is None:
    return None
  return per_capita / (
      constants.WORLD_AVG_GHG_PER_CAPITA_TONNES * year_fraction
  )


def _populate_country_spatial_gwp_metrics(
    row: MutableMapping[str, Any],
    *,
    proportioned_ef_mj: float,
    global_prop_sum_ef: float,
    area: float,
    population: int,
    ghg_tonnes: float,
    year_fraction: float,
) -> None:
  """Populates over-region GWP tonnes, share, density, GHG ratio, per-capita.

  Args:
    row: Country row to update in place.
    proportioned_ef_mj: Energy forcing over the country's territory, in MJ.
    global_prop_sum_ef: Energy forcing summed over all countries, in MJ.
    area: Country area in km^2.
    population: Country population (0 when unknown).
    ghg_tonnes: Annual national GHG baseline in tonnes (0 when unknown).
    year_fraction: Observed share of the year; scales the annual baselines.
  """
  row[constants.KEY_CO2E_TONNES_SHARE] = _ratio(
      proportioned_ef_mj, global_prop_sum_ef
  )
  # A country with no boundary polygon (area 0, e.g. microstates absent from a
  # coarse boundary file) cannot receive over-region impact, so its ratio is
  # unknown, not 0; emitting 0 would rank it ahead of net-cooling countries
  # instead of leaving it unranked.
  ratio_ghg = ghg_tonnes * year_fraction if area > 0 else 0.0
  ratio_population = population if area > 0 else 0

  for horizon, factor in _GWP_HORIZON_FACTORS:
    gwp_tonnes = proportioned_ef_mj * factor
    per_capita = (
        (gwp_tonnes / ratio_population) if ratio_population > 0 else None
    )
    row[f"co2e_gwp{horizon}_tonnes"] = gwp_tonnes
    row[f"co2e_gwp{horizon}_tonnes_sq_km"] = _per_area(gwp_tonnes, area)
    row[f"co2e_gwp{horizon}_vs_national_ghg"] = (
        (gwp_tonnes / ratio_ghg) if ratio_ghg > 0 else None
    )
    row[f"co2e_gwp{horizon}_tonnes_per_capita"] = per_capita
    row[f"co2e_gwp{horizon}_per_capita_vs_world_avg"] = _vs_world_avg(
        per_capita, year_fraction
    )


def _populate_country_outgoing_gwp_metrics(
    row: MutableMapping[str, Any],
    *,
    outgoing_ef: float,
    global_out_ef: float,
    population: int,
    ghg_tonnes: float,
    year_fraction: float,
) -> None:
  """Populates outgoing-flight GWP tonnes, share, GHG ratio, and per-capita.

  Args:
    row: Country row to update in place.
    outgoing_ef: Energy forcing of flights departing the country, in MJ.
    global_out_ef: Outgoing energy forcing summed over all countries, in MJ.
    population: Country population (0 when unknown).
    ghg_tonnes: Annual national GHG baseline in tonnes (0 when unknown).
    year_fraction: Observed share of the year; scales the annual baselines.
  """
  period_ghg_tonnes = ghg_tonnes * year_fraction
  row[
      f"{constants.PREFIX_OUTGOING_FLIGHT}{constants.KEY_CO2E_TONNES_SHARE}"
  ] = _ratio(outgoing_ef, global_out_ef)
  for horizon, factor in _GWP_HORIZON_FACTORS:
    outgoing_gwp_tonnes = outgoing_ef * factor
    per_capita = (outgoing_gwp_tonnes / population) if population > 0 else None
    row[f"outgoing_flight_co2e_gwp{horizon}_tonnes"] = outgoing_gwp_tonnes
    row[f"outgoing_flight_co2e_gwp{horizon}_vs_national_ghg"] = (
        (outgoing_gwp_tonnes / period_ghg_tonnes)
        if period_ghg_tonnes > 0
        else None
    )
    row[f"outgoing_flight_co2e_gwp{horizon}_tonnes_per_capita"] = per_capita
    row[f"outgoing_flight_co2e_gwp{horizon}_per_capita_vs_world_avg"] = (
        _vs_world_avg(per_capita, year_fraction)
    )


def _populate_country_ratios_and_rf(
    row: MutableMapping[str, Any],
    *,
    proportioned_ef_mj: float,
    area: float,
    local_ef: float,
    total_ef: float,
    outgoing_ef: float,
    months_covered: int,
    days_covered: int,
    annual_divisor: float,
) -> None:
  """Populates density, impact ratios, observation coverage, and mean RF."""
  row[constants.KEY_IMPACT_DENSITY_SQ_KM] = _per_area(proportioned_ef_mj, area)
  row[constants.KEY_IN_COUNTRY_IMPACT] = _ratio(local_ef, total_ef)
  row[constants.KEY_OVER_REGION_VS_OUTGOING_IMPACT] = _ratio(
      proportioned_ef_mj, outgoing_ef
  )
  row[constants.KEY_MONTHS_COVERED] = months_covered
  row[constants.KEY_DAYS_COVERED] = days_covered
  row[constants.KEY_MEAN_RF_MW_PER_SQ_M] = _per_area(
      proportioned_ef_mj, area, annual_divisor
  )


def _compute_single_country_annual_metrics(
    row: MutableMapping[str, Any],
    *,
    global_prop_sum_ef: float,
    global_out_ef: float,
    months_covered: int,
    days_covered: int,
    annual_divisor: float,
    year_fraction: float,
) -> None:
  """Populates derived country metrics on `row` and pops `prop_sum_ef_mj`."""
  area = row[constants.KEY_AREA_SQ_KM]
  population = row[constants.KEY_POPULATION]
  ghg_tonnes = row[constants.KEY_GHG_MTCO2E] * _TONNES_PER_MEGATONNE
  proportioned_ef_mj = row.pop(constants.KEY_PROP_SUM_EF_MJ, 0.0)
  outgoing_ef = row[constants.KEY_OUTGOING_FLIGHT_IMPACT]

  _populate_country_ratios_and_rf(
      row,
      proportioned_ef_mj=proportioned_ef_mj,
      area=area,
      local_ef=row[constants.KEY_LOCAL_FLIGHT_IMPACT],
      total_ef=row[constants.KEY_TOTAL_FLIGHT_IMPACT],
      outgoing_ef=outgoing_ef,
      months_covered=months_covered,
      days_covered=days_covered,
      annual_divisor=annual_divisor,
  )
  _populate_country_spatial_gwp_metrics(
      row,
      proportioned_ef_mj=proportioned_ef_mj,
      global_prop_sum_ef=global_prop_sum_ef,
      area=area,
      population=population,
      ghg_tonnes=ghg_tonnes,
      year_fraction=year_fraction,
  )
  _populate_country_outgoing_gwp_metrics(
      row,
      outgoing_ef=outgoing_ef,
      global_out_ef=global_out_ef,
      population=population,
      ghg_tonnes=ghg_tonnes,
      year_fraction=year_fraction,
  )


def _enrich_country_annual_metrics(
    country_rows: Sequence[MutableMapping[str, Any]],
    *,
    global_prop_sum_ef: float,
    global_out_ef: float,
    months_covered: int,
    days_covered: int,
    annual_divisor: float,
    year_fraction: float,
    fir_summary_by_iso: Mapping[str, Mapping[str, Any]],
) -> None:
  """Computes derived metrics and attaches FIR summaries for country rows."""
  for row in country_rows:
    _compute_single_country_annual_metrics(
        row,
        global_prop_sum_ef=global_prop_sum_ef,
        global_out_ef=global_out_ef,
        months_covered=months_covered,
        days_covered=days_covered,
        annual_divisor=annual_divisor,
        year_fraction=year_fraction,
    )
    country_iso = row[constants.KEY_ISO_A2]
    if (fir_summary := fir_summary_by_iso.get(country_iso)) is not None:
      for k, summary_value in fir_summary.items():
        row[k] = summary_value


def _build_country_rank_fields() -> tuple[tuple[str, str], ...]:
  """Returns `(value_field, rank_field)` pairs for annual country rankings."""
  fields: list[tuple[str, str]] = [
      (
          constants.KEY_MEAN_RF_MW_PER_SQ_M,
          constants.KEY_MEAN_RF_MW_PER_SQ_M_RANK,
      ),
      (
          constants.KEY_OUTGOING_FLIGHT_IMPACT,
          constants.KEY_OUTGOING_FLIGHT_IMPACT_RANK,
      ),
  ]
  for prefix in ("", constants.PREFIX_OUTGOING_FLIGHT):
    for template in (
        constants.FIELD_CO2E_TONNES,
        constants.FIELD_CO2E_VS_NATIONAL_GHG,
        constants.FIELD_CO2E_TONNES_PER_CAPITA,
    ):
      for gwp in constants.GWP_TONNES_PER_MJ:
        metric = f"{prefix}{template.format(gwp=gwp)}"
        fields.append((metric, f"{metric}_rank"))
  return tuple(fields)


# Country metrics ranked directly from the country's own annual totals.
# Impact density (and its FIR-scope variants) is not ranked: it equals mean RF
# times the dataset-wide observation window (days x 86.4 ks), so its rank would
# always equal `mean_rf_mw_per_sq_m_rank`.
_COUNTRY_RANK_FIELDS: tuple[tuple[str, str], ...] = _build_country_rank_fields()


def _country_fir_scope_rank_fields() -> list[tuple[str, str]]:
  """Returns `(value_field, rank_field)` pairs for country FIR-scope metrics."""
  rank_fields: list[tuple[str, str]] = []
  for scope in constants.FIR_SCOPES:
    for gwp in constants.GWP_TONNES_PER_MJ:
      base = f"{scope}_co2e_{gwp}"
      rank_fields.append((f"{base}_tonnes", f"{base}_tonnes_rank"))
      rank_fields.append(
          (f"{base}_tonnes_per_capita", f"{base}_tonnes_per_capita_rank")
      )
      rank_fields.append(
          (f"{base}_vs_national_ghg", f"{base}_vs_national_ghg_rank")
      )
    rank_fields.append((
        f"{scope}_mean_rf_mw_per_sq_m",
        f"{scope}_mean_rf_mw_per_sq_m_rank",
    ))
  return rank_fields


def _assign_country_ranks(
    country_rows: Sequence[MutableMapping[str, Any]],
) -> None:
  """Assigns descending ranks to country rows across all metric dimensions."""
  rank_fields = [*_COUNTRY_RANK_FIELDS, *_country_fir_scope_rank_fields()]
  _assign_descending_ranks(
      country_rows,
      rank_fields,
      tiebreak_fields=(constants.KEY_ISO_A2, constants.KEY_NAME),
  )


def _derive_and_rank_country_rows(
    country_rows: Sequence[MutableMapping[str, Any]],
    *,
    year: int,
    in_year_months: Iterable[int],
    fir_summary_by_iso: Mapping[str, Mapping[str, Any]],
    baseline_stats: Mapping[str, Any],
) -> None:
  """Computes derived metrics and ranks for annual country rows in place.

  Args:
    country_rows: Annual country rows to update.
    year: The calendar year being processed.
    in_year_months: Observed months, used for the mean RF observation window.
    fir_summary_by_iso: FIR summary fields to attach per country ISO2 code.
    baseline_stats: Per-country population and GHG baseline statistics.
  """
  annual_divisor, months_covered, days_covered = mw_per_sq_m_divisor_for_months(
      year, in_year_months
  )
  _log_missing_baselines(country_rows, baseline_stats)
  global_prop_sum_ef = sum(
      row[constants.KEY_PROP_SUM_EF_MJ] for row in country_rows
  )
  global_out_ef = sum(
      row[constants.KEY_OUTGOING_FLIGHT_IMPACT] for row in country_rows
  )

  _enrich_country_annual_metrics(
      country_rows,
      global_prop_sum_ef=global_prop_sum_ef,
      global_out_ef=global_out_ef,
      months_covered=months_covered,
      days_covered=days_covered,
      annual_divisor=annual_divisor,
      year_fraction=_observed_fraction_of_year(year, days_covered),
      fir_summary_by_iso=fir_summary_by_iso,
  )
  _assign_country_ranks(country_rows)


def _complete_country_annual_rows(
    country_annual_agg: dict[str, Any],
    *,
    fir_summary_by_iso: Mapping[str, Mapping[str, Any]],
    country_names: Mapping[str, str] | None,
    baseline_stats: Mapping[str, Any],
    country_areas: Mapping[str, float],
    year: int,
    in_year_months: Iterable[int],
) -> list[dict[str, Any]]:
  """Inserts missing FIR-controlling countries and ranks all country rows."""
  _ensure_fir_controlling_countries(
      country_annual_agg,
      fir_summary_by_iso=fir_summary_by_iso,
      country_names=country_names,
      baseline_stats=baseline_stats,
      country_areas=country_areas,
  )
  country_rows = list(country_annual_agg.values())
  _derive_and_rank_country_rows(
      country_rows,
      year=year,
      in_year_months=in_year_months,
      fir_summary_by_iso=fir_summary_by_iso,
      baseline_stats=baseline_stats,
  )
  return country_rows


def finalize_country_and_temporal_datasets(
    country_grouped_rows: Sequence[constants.CountryRow],
    *,
    fir_summary_by_iso: Mapping[str, Mapping[str, Any]],
    country_areas: Mapping[str, float],
    baseline_stats: Mapping[str, Any],
    year: int,
    observed_months: Iterable[int] | None = None,
    country_names: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
  """Computes derived metrics, GWP conversions, ranks, and temporal matrices.

  Args:
    country_grouped_rows: Pre-aggregated country records of key -> values.
    fir_summary_by_iso: Summary of FIR metrics rolled up per country ISO2 code.
    country_areas: Mapping of country ISO2 code to area in km^2.
    baseline_stats: Per-country population and GHG baseline statistics.
    year: The calendar year being processed.
    observed_months: Optional dataset-wide observed months to combine with the
      months present in `country_grouped_rows` when computing the annual mean RF
      observation window.
    country_names: Optional mapping of ISO2 code to country name override.

  Returns:
    A tuple of (country_annual_rows, temporal_rows).
  """
  merged_temporal_inputs, canonical_names, in_year_months = (
      _aggregate_country_temporal_inputs(
          country_grouped_rows, year, observed_months
      )
  )

  country_annual_agg = _build_initial_country_annual_rows(
      merged_temporal_inputs,
      canonical_names,
      country_areas=country_areas,
      baseline_stats=baseline_stats,
  )
  temporal_rows = _build_country_temporal_rows(
      merged_temporal_inputs, canonical_names, baseline_stats
  )

  country_rows = _complete_country_annual_rows(
      country_annual_agg,
      fir_summary_by_iso=fir_summary_by_iso,
      country_names=country_names,
      baseline_stats=baseline_stats,
      country_areas=country_areas,
      year=year,
      in_year_months=in_year_months,
  )
  return country_rows, temporal_rows


def _filter_fir_rows_by_year(
    fir_grouped_rows: Sequence[constants.FirRow],
    year: int,
    observed_months: Iterable[int] | None = None,
) -> tuple[list[constants.FirRow], set[int], float]:
  """Filters FIR rows to the target year and sums global energy forcing."""
  in_year_fir_rows: list[constants.FirRow] = []
  dropped_out_of_year_rows = 0
  in_year_months = _initial_observed_months(observed_months)
  global_fir_ef = 0.0
  for (raw_firname, raw_icao, fir_iso2, fir_type, month, local_hour), (
      total_segment_count,
      contrail_cnt,
      proportioned_ef_mj,
      total_distance_km,
      contrail_km,
  ) in fir_grouped_rows:
    month_number = int(month)
    if _is_out_of_year(month_number, year):
      dropped_out_of_year_rows += 1
      continue
    in_year_months.add(month_number)
    global_fir_ef += float(proportioned_ef_mj)
    in_year_fir_rows.append((
        (raw_firname, raw_icao, fir_iso2, fir_type, month, local_hour),
        (
            total_segment_count,
            contrail_cnt,
            proportioned_ef_mj,
            total_distance_km,
            contrail_km,
        ),
    ))
  _log_dropped_out_of_year_rows(dropped_out_of_year_rows, year, row_kind="FIR")
  return in_year_fir_rows, in_year_months, global_fir_ef


class _FirIdentity(NamedTuple):
  """Normalized identity, controlling countries, type, and area of a FIR."""

  fir_id: str
  firname: str
  fir_icao: str
  clean_iso2: list[str]
  clean_type: str
  area: float


def _resolve_fir_identity(
    *,
    raw_firname: str | None,
    raw_icao: str | None,
    fir_iso2: str | None,
    fir_type: str | None,
    areas: Mapping[tuple[str, str], float],
) -> _FirIdentity:
  """Normalizes raw FIR key fields and looks up the FIR's area.

  The area is looked up by the cleaned FIR name first, then by the raw name,
  and defaults to 0.0.

  Args:
    raw_firname: FIR name as stored in the aggregate row.
    raw_icao: FIR ICAO code as stored in the aggregate row.
    fir_iso2: Comma-separated controlling-country ISO-2 codes.
    fir_type: FIR type (`domestic` or `international`).
    areas: Mapping from `(firname, fir_icao)` to area in km^2.

  Returns:
    The normalized `_FirIdentity`.
  """
  fir_icao = str(raw_icao or "").strip().upper()
  firname = constants.clean_fir_name(str(raw_firname or ""), fir_icao)
  return _FirIdentity(
      fir_id=constants.make_fir_id(fir_icao, firname),
      firname=firname,
      fir_icao=fir_icao,
      clean_iso2=constants.split_iso2_codes(fir_iso2),
      clean_type=(str(fir_type or constants.FIR_TYPE_DOMESTIC).strip().lower()),
      area=float(
          areas.get((firname, fir_icao))
          or areas.get((str(raw_firname or ""), fir_icao))
          or 0.0
      ),
  )


def _upsert_fir_metadata(
    fir_meta: MutableMapping[str, _FirMetadataEntry],
    identity: _FirIdentity,
) -> None:
  """Inserts or merges FIR metadata keyed by `fir_id`."""
  fir_id = identity.fir_id
  clean_iso2 = identity.clean_iso2
  clean_type = identity.clean_type
  area = identity.area
  existing = fir_meta.get(fir_id)
  if existing is None:
    fir_meta[fir_id] = {
        "firname": identity.firname,
        "fir_icao": identity.fir_icao,
        "fir_iso2": list(clean_iso2),
        "fir_type": clean_type,
        "area": area,
    }
    return
  if existing["fir_iso2"] != clean_iso2 or existing["fir_type"] != clean_type:
    logging.warning(
        "FIR %s encountered with conflicting metadata: (%s, %s) vs"
        " (%s, %s). Merging on fir_id; merged country list is sorted and"
        " has no primary country.",
        fir_id,
        existing["fir_iso2"],
        existing["fir_type"],
        clean_iso2,
        clean_type,
    )
    # Conflicting inputs define no primary country, so the merged list is
    # sorted to make the output independent of input row order.
    existing["fir_iso2"] = sorted(set(existing["fir_iso2"]) | set(clean_iso2))
    if constants.FIR_TYPE_INTERNATIONAL in (existing["fir_type"], clean_type):
      existing["fir_type"] = constants.FIR_TYPE_INTERNATIONAL
  if existing["area"] == 0.0 and area > 0.0:
    existing["area"] = area


def _get_or_create_fir_temporal_bundle(
    fir_temporal_bundles: MutableMapping[str, dict[str, Any]],
    identity: _FirIdentity,
) -> dict[str, Any]:
  """Returns the FIR's temporal bundle, creating an empty one if needed."""
  bundle = fir_temporal_bundles.get(identity.fir_id)
  if bundle is None:
    bundle = _make_empty_temporal_bundle(
        "",
        "",
        fir_id=identity.fir_id,
        firname=identity.firname,
        fir_icao=identity.fir_icao,
        fir_to_country_iso2s=identity.clean_iso2,
        fir_type=identity.clean_type,
    )
    fir_temporal_bundles[identity.fir_id] = bundle
  return bundle


def _add_fir_annual_metrics(
    fir_annual_agg: MutableMapping[str, _FirAnnualAccumulator],
    fir_id: str,
    fir_metrics: constants.FirMetricsTuple,
) -> None:
  """Adds one FIR metrics tuple into the FIR's annual accumulator."""
  (
      total_segment_count,
      contrail_cnt,
      proportioned_ef_mj,
      total_distance_km,
      contrail_km,
  ) = fir_metrics
  annual_acc = fir_annual_agg.get(fir_id)
  if annual_acc is None:
    annual_acc = _FirAnnualAccumulator()
    fir_annual_agg[fir_id] = annual_acc
  annual_acc.total_segment_count += int(total_segment_count)
  annual_acc.contrail_segment_count += int(contrail_cnt)
  annual_acc.prop_sum_ef_mj += float(proportioned_ef_mj)
  annual_acc.total_flight_distance_km += float(total_distance_km)
  annual_acc.contrail_flight_distance_km += float(contrail_km)


def _sync_fir_bundle_metadata(
    fir_temporal_bundles: Mapping[str, MutableMapping[str, Any]],
    fir_meta: Mapping[str, _FirMetadataEntry],
) -> None:
  """Copies merged FIR country codes and type onto each temporal bundle."""
  for fir_id, bundle in fir_temporal_bundles.items():
    bundle[constants.KEY_FIR_TO_COUNTRY_ISO2S] = list(
        fir_meta[fir_id]["fir_iso2"]
    )
    bundle[constants.KEY_TYPE] = fir_meta[fir_id]["fir_type"]


def _process_fir_observation(
    fir_row: constants.FirRow,
    *,
    areas: Mapping[tuple[str, str], float],
    fir_temporal_bundles: dict[str, dict[str, Any]],
    fir_annual_agg: dict[str, _FirAnnualAccumulator],
    fir_meta: dict[str, _FirMetadataEntry],
) -> None:
  """Processes an observation into metadata, bundles, and annual accumulator."""
  (
      raw_firname,
      raw_icao,
      fir_iso2,
      fir_type,
      month,
      local_hour,
  ), fir_metrics = fir_row
  identity = _resolve_fir_identity(
      raw_firname=raw_firname,
      raw_icao=raw_icao,
      fir_iso2=fir_iso2,
      fir_type=fir_type,
      areas=areas,
  )
  _upsert_fir_metadata(fir_meta, identity)
  bundle = _get_or_create_fir_temporal_bundle(fir_temporal_bundles, identity)
  _accumulate_into_temporal_bundle(
      bundle,
      month=month,
      local_hour=local_hour,
      total_segment_count=fir_metrics[0],
      contrail_cnt=fir_metrics[1],
      proportioned_ef_mj=fir_metrics[2],
      total_distance_km=fir_metrics[3],
      contrail_km=fir_metrics[4],
  )
  _add_fir_annual_metrics(fir_annual_agg, identity.fir_id, fir_metrics)


def _aggregate_fir_annual_and_temporal(
    in_year_fir_rows: Sequence[constants.FirRow],
    areas: Mapping[tuple[str, str], float],
) -> tuple[
    dict[str, _FirAnnualAccumulator],
    dict[str, _FirMetadataEntry],
    list[dict[str, Any]],
]:
  """Aggregates FIR records by fir_id into annual sums and temporal bundles."""
  fir_temporal_bundles: dict[str, dict[str, Any]] = {}
  fir_annual_agg: dict[str, _FirAnnualAccumulator] = {}
  fir_meta: dict[str, _FirMetadataEntry] = {}

  for fir_row in in_year_fir_rows:
    _process_fir_observation(
        fir_row,
        areas=areas,
        fir_temporal_bundles=fir_temporal_bundles,
        fir_annual_agg=fir_annual_agg,
        fir_meta=fir_meta,
    )

  _sync_fir_bundle_metadata(fir_temporal_bundles, fir_meta)
  return fir_annual_agg, fir_meta, list(fir_temporal_bundles.values())


def _populate_fir_gwp_metrics(
    row: MutableMapping[str, Any],
    *,
    tot_ef: float,
    fir_share: float | None,
    area: float,
) -> None:
  """Populates the share and the per-horizon fields on an annual FIR row."""
  row[constants.KEY_CO2E_TONNES_SHARE] = fir_share
  for horizon, factor in _GWP_HORIZON_FACTORS:
    gwp_tonnes = tot_ef * factor
    row[f"co2e_gwp{horizon}_tonnes"] = gwp_tonnes
    row[f"co2e_gwp{horizon}_tonnes_sq_km"] = _per_area(gwp_tonnes, area)


def _build_fir_base_row(
    fir_id: str,
    meta: _FirMetadataEntry,
    annual_acc: _FirAnnualAccumulator,
    *,
    months_covered: int,
    days_covered: int,
    annual_divisor: float,
) -> dict[str, Any]:
  """Constructs common identity and flight metrics for an annual FIR row."""
  area = meta["area"]
  tot_ef = annual_acc.prop_sum_ef_mj
  return {
      constants.KEY_FIR_ID: fir_id,
      constants.KEY_FIRNAME: meta["firname"],
      constants.KEY_FIR_ICAO: meta["fir_icao"],
      constants.KEY_FIR_TO_COUNTRY_ISO2S: list(meta["fir_iso2"]),
      constants.KEY_TYPE: meta["fir_type"],
      constants.KEY_MONTHS_COVERED: months_covered,
      constants.KEY_DAYS_COVERED: days_covered,
      constants.KEY_SEGMENT_COUNT: annual_acc.total_segment_count,
      constants.KEY_CONTRAIL_SEGMENT_COUNT: annual_acc.contrail_segment_count,
      constants.KEY_TOTAL_FLIGHT_DISTANCE_KM: (
          annual_acc.total_flight_distance_km
      ),
      constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM: (
          annual_acc.contrail_flight_distance_km
      ),
      constants.KEY_AREA_SQ_KM: area,
      constants.KEY_IMPACT_DENSITY_SQ_KM: _per_area(tot_ef, area),
      constants.KEY_MEAN_RF_MW_PER_SQ_M: _per_area(
          tot_ef, area, annual_divisor
      ),
  }


def _build_single_fir_annual_row(
    fir_id: str,
    meta: _FirMetadataEntry,
    annual_acc: _FirAnnualAccumulator,
    *,
    global_fir_ef: float,
    months_covered: int,
    days_covered: int,
    annual_divisor: float,
) -> dict[str, Any]:
  """Constructs a single annual FIR dictionary record."""
  row = _build_fir_base_row(
      fir_id,
      meta,
      annual_acc,
      months_covered=months_covered,
      days_covered=days_covered,
      annual_divisor=annual_divisor,
  )
  _populate_fir_gwp_metrics(
      row,
      tot_ef=annual_acc.prop_sum_ef_mj,
      fir_share=_ratio(annual_acc.prop_sum_ef_mj, global_fir_ef),
      area=meta["area"],
  )
  return row


def _make_empty_country_fir_summary(
    *, months_covered: int, days_covered: int
) -> dict[str, Any]:
  """Creates a zero-initialized country-level FIR summary dictionary."""
  summary: dict[str, Any] = {
      constants.KEY_MONTHS_COVERED: months_covered,
      constants.KEY_DAYS_COVERED: days_covered,
  }
  for scope in constants.FIR_SCOPES:
    prefix = f"{scope}_"
    summary[f"{prefix}prop_sum_ef_mj"] = 0.0
    summary[f"{prefix}{constants.KEY_SEGMENT_COUNT}"] = 0
    summary[f"{prefix}{constants.KEY_CONTRAIL_SEGMENT_COUNT}"] = 0
    summary[f"{prefix}{constants.KEY_FLIGHT_DISTANCE_KM}"] = 0.0
    summary[f"{prefix}{constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM}"] = 0.0
    summary[f"{prefix}{constants.KEY_AREA_SQ_KM}"] = 0.0
    summary[f"{prefix}{constants.KEY_FIR_ID}"] = []
    summary[f"{prefix}{constants.KEY_FIRNAME}"] = []
    summary[f"{prefix}{constants.KEY_FIR_ICAO}"] = []
  return summary


def _accumulate_fir_scope_totals(
    country_summary: MutableMapping[str, Any],
    *,
    prefix: str,
    fir_id: str,
    firname: str,
    fir_icao: str,
    area: float,
    total_ef: float,
    total_segment_count: int,
    contrail_count: int,
    total_distance_km: float,
    contrail_distance_km: float,
) -> None:
  """Adds FIR metrics and IDs into a domestic or international scope prefix."""
  country_summary[f"{prefix}_prop_sum_ef_mj"] += total_ef
  country_summary[f"{prefix}_segment_count"] += total_segment_count
  country_summary[f"{prefix}_contrail_segment_count"] += contrail_count
  country_summary[f"{prefix}_flight_distance_km"] += total_distance_km
  country_summary[
      f"{prefix}_contrail_flight_distance_km"
  ] += contrail_distance_km
  country_summary[f"{prefix}_area_sq_km"] += area
  country_summary[f"{prefix}_fir_id"].append(fir_id)
  country_summary[f"{prefix}_firname"].append(firname)
  country_summary[f"{prefix}_fir_icao"].append(fir_icao)


def _accumulate_fir_overall_totals(
    country_summary: MutableMapping[str, Any],
    *,
    area: float,
    annual_acc: _FirAnnualAccumulator,
) -> None:
  """Adds one FIR's annual totals into the country's `fir_total_*` fields."""
  country_summary["fir_total_prop_sum_ef_mj"] += annual_acc.prop_sum_ef_mj
  country_summary["fir_total_segment_count"] += annual_acc.total_segment_count
  country_summary[
      "fir_total_contrail_segment_count"
  ] += annual_acc.contrail_segment_count
  country_summary[
      "fir_total_flight_distance_km"
  ] += annual_acc.total_flight_distance_km
  country_summary[
      "fir_total_contrail_flight_distance_km"
  ] += annual_acc.contrail_flight_distance_km
  country_summary["fir_total_area_sq_km"] += area


def _accumulate_single_fir_into_country_summary(
    fir_by_country_summary: MutableMapping[str, dict[str, Any]],
    *,
    fir_id: str,
    meta: _FirMetadataEntry,
    annual_acc: _FirAnnualAccumulator,
    months_covered: int,
    days_covered: int,
) -> None:
  """Accumulates one FIR's metrics into controlling countries' summaries."""
  scope_prefix = (
      constants.FIR_SCOPE_DOM
      if meta["fir_type"] == constants.FIR_TYPE_DOMESTIC
      else constants.FIR_SCOPE_INT
  )
  for iso in meta["fir_iso2"]:
    country_summary = fir_by_country_summary.get(iso)
    if country_summary is None:
      country_summary = _make_empty_country_fir_summary(
          months_covered=months_covered, days_covered=days_covered
      )
      fir_by_country_summary[iso] = country_summary
    _accumulate_fir_scope_totals(
        country_summary,
        prefix=scope_prefix,
        fir_id=fir_id,
        firname=meta["firname"],
        fir_icao=meta["fir_icao"],
        area=meta["area"],
        total_ef=annual_acc.prop_sum_ef_mj,
        total_segment_count=annual_acc.total_segment_count,
        contrail_count=annual_acc.contrail_segment_count,
        total_distance_km=annual_acc.total_flight_distance_km,
        contrail_distance_km=annual_acc.contrail_flight_distance_km,
    )
    _accumulate_fir_overall_totals(
        country_summary, area=meta["area"], annual_acc=annual_acc
    )


def _build_fir_annual_rows_and_country_acc(
    fir_annual_agg: Mapping[str, _FirAnnualAccumulator],
    *,
    fir_meta: Mapping[str, _FirMetadataEntry],
    global_fir_ef: float,
    months_covered: int,
    days_covered: int,
    annual_divisor: float,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
  """Constructs annual FIR rows and accumulates country FIR summaries."""
  fir_annual_rows: list[dict[str, Any]] = []
  fir_by_country_summary: dict[str, dict[str, Any]] = {}

  for fir_id, annual_acc in fir_annual_agg.items():
    meta = fir_meta[fir_id]
    fir_annual_rows.append(
        _build_single_fir_annual_row(
            fir_id,
            meta,
            annual_acc,
            global_fir_ef=global_fir_ef,
            months_covered=months_covered,
            days_covered=days_covered,
            annual_divisor=annual_divisor,
        )
    )
    _accumulate_single_fir_into_country_summary(
        fir_by_country_summary,
        fir_id=fir_id,
        meta=meta,
        annual_acc=annual_acc,
        months_covered=months_covered,
        days_covered=days_covered,
    )

  return fir_annual_rows, fir_by_country_summary


def _assign_fir_ranks(
    fir_annual_rows: Sequence[MutableMapping[str, Any]],
) -> None:
  """Assigns descending ranks to annual FIR rows."""
  fir_rank_fields = [
      (f"co2e_{gwp}_tonnes", f"co2e_{gwp}_tonnes_rank")
      for gwp in constants.GWP_TONNES_PER_MJ
  ]
  fir_rank_fields.append((
      constants.KEY_MEAN_RF_MW_PER_SQ_M,
      constants.KEY_MEAN_RF_MW_PER_SQ_M_RANK,
  ))
  _assign_descending_ranks(
      fir_annual_rows, fir_rank_fields, tiebreak_fields=(constants.KEY_FIR_ID,)
  )


class _FirScopeMember(NamedTuple):
  """Identity of one FIR attributed to a country's FIR scope."""

  fir_id: str
  firname: str
  fir_icao: str


def _fir_scope_members(
    country_fir_summary: Mapping[str, Any], scope: str
) -> list[_FirScopeMember]:
  """Returns the FIRs accumulated into `scope`, one member per FIR record."""
  return [
      _FirScopeMember(fir_id=fir_id, firname=firname, fir_icao=fir_icao)
      for fir_id, firname, fir_icao in zip(
          country_fir_summary[f"{scope}_{constants.KEY_FIR_ID}"],
          country_fir_summary[f"{scope}_{constants.KEY_FIRNAME}"],
          country_fir_summary[f"{scope}_{constants.KEY_FIR_ICAO}"],
          strict=True,
      )
  ]


def _finalize_fir_identity_lists(
    country_fir_summary: MutableMapping[str, Any],
) -> None:
  """Sorts each scope's FIR identity lists and fills the `fir_total` ones.

  One entry per FIR, index-aligned across the `_fir_id`, `_firname` and
  `_fir_icao` lists and sorted by FIR ID so the output does not depend on the
  runner's row order. A scope without FIRs keeps empty lists.

  Args:
    country_fir_summary: Country FIR summary to update in place.
  """
  domestic = _fir_scope_members(country_fir_summary, constants.FIR_SCOPE_DOM)
  international = _fir_scope_members(
      country_fir_summary, constants.FIR_SCOPE_INT
  )
  members_by_scope = {
      constants.FIR_SCOPE_DOM: domestic,
      constants.FIR_SCOPE_INT: international,
      constants.FIR_SCOPE_TOTAL: domestic + international,
  }
  for scope, members in members_by_scope.items():
    ordered = sorted(set(members))
    country_fir_summary[f"{scope}_{constants.KEY_FIR_ID}"] = [
        member.fir_id for member in ordered
    ]
    country_fir_summary[f"{scope}_{constants.KEY_FIRNAME}"] = [
        member.firname for member in ordered
    ]
    country_fir_summary[f"{scope}_{constants.KEY_FIR_ICAO}"] = [
        member.fir_icao for member in ordered
    ]


def _populate_fir_scope_density_metrics(
    country_fir_summary: MutableMapping[str, Any],
    scope: str,
    annual_divisor: float,
) -> None:
  """Sets a FIR scope's impact density and mean radiative forcing fields."""
  scope_area = country_fir_summary[f"{scope}_area_sq_km"]
  scope_ef = country_fir_summary[f"{scope}_prop_sum_ef_mj"]
  country_fir_summary[f"{scope}_impact_density_sq_km"] = _per_area(
      scope_ef, scope_area
  )
  country_fir_summary[f"{scope}_mean_rf_mw_per_sq_m"] = _per_area(
      scope_ef, scope_area, annual_divisor
  )


def _populate_fir_scope_gwp_metrics(
    country_fir_summary: MutableMapping[str, Any],
    *,
    scope: str,
    global_fir_ef: float,
    population: int,
    ghg_tonnes: float,
    year_fraction: float,
) -> None:
  """Sets a FIR scope's GWP tonnes, share, density, and per-capita fields.

  Args:
    country_fir_summary: Country FIR summary to update in place.
    scope: Field prefix: `fir_total`, `fir_dom`, or `fir_int`.
    global_fir_ef: Energy forcing summed over all FIRs, in MJ.
    population: Country population (0 when unknown).
    ghg_tonnes: Annual national GHG baseline in tonnes (0 when unknown).
    year_fraction: Observed share of the year; scales the annual baselines.
  """
  period_ghg_tonnes = ghg_tonnes * year_fraction
  scope_ef = country_fir_summary[f"{scope}_prop_sum_ef_mj"]
  scope_area = country_fir_summary[f"{scope}_area_sq_km"]
  country_fir_summary[f"{scope}_{constants.KEY_CO2E_TONNES_SHARE}"] = _ratio(
      scope_ef, global_fir_ef
  )
  for gwp_key, tonnes_per_mj in constants.GWP_TONNES_PER_MJ.items():
    tonnes = scope_ef * tonnes_per_mj
    base = f"{scope}_co2e_{gwp_key}"

    country_fir_summary[f"{base}_tonnes"] = tonnes
    country_fir_summary[f"{base}_tonnes_sq_km"] = _per_area(tonnes, scope_area)

    per_capita = (tonnes / population) if population > 0 else None
    country_fir_summary[f"{base}_tonnes_per_capita"] = per_capita
    country_fir_summary[f"{base}_per_capita_vs_world_avg"] = _vs_world_avg(
        per_capita, year_fraction
    )
    country_fir_summary[f"{base}_vs_national_ghg"] = (
        (tonnes / period_ghg_tonnes) if period_ghg_tonnes > 0 else None
    )


def _enrich_fir_country_summaries(
    fir_by_country_summary: Mapping[str, MutableMapping[str, Any]],
    *,
    baseline_stats: Mapping[str, Any],
    global_fir_ef: float,
    annual_divisor: float,
    year_fraction: float,
) -> None:
  """Computes densities, mean RF, and GWP fields for country FIR summaries."""
  for iso, country_fir_summary in fir_by_country_summary.items():
    _finalize_fir_identity_lists(country_fir_summary)

    for scope in constants.FIR_SCOPES:
      _populate_fir_scope_density_metrics(
          country_fir_summary, scope, annual_divisor
      )

    c_stats = baseline_stats.get(iso, {})
    population = int(c_stats.get(constants.KEY_POPULATION) or 0)
    ghg_tonnes = (
        float(c_stats.get(constants.KEY_GHG_MTCO2E) or 0.0)
        * _TONNES_PER_MEGATONNE
    )
    for scope in constants.FIR_SCOPES:
      _populate_fir_scope_gwp_metrics(
          country_fir_summary,
          scope=scope,
          global_fir_ef=global_fir_ef,
          population=population,
          ghg_tonnes=ghg_tonnes,
          year_fraction=year_fraction,
      )
      country_fir_summary.pop(f"{scope}_{constants.KEY_PROP_SUM_EF_MJ}", None)


def finalize_fir_dataset(
    fir_grouped_rows: Sequence[constants.FirRow],
    baseline_stats: Mapping[str, Any],
    fir_areas: Mapping[tuple[str, str], float] | None = None,
    *,
    year: int,
    observed_months: Iterable[int] | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
]:
  """Computes FIR rankings, GWP equivalents, annual rollups, and country totals.

  Args:
    fir_grouped_rows: `((firname, fir_icao, fir_iso2, fir_type, month,
      local_hour), (segments, contrail_segments, prop_ef_mj, total_km,
      contrail_km))` pairs produced by SumTupleCombineFn.
    baseline_stats: Per-country population and GHG baselines.
    fir_areas: Optional mapping from `(firname, fir_icao)` to polygon area in
      km^2, normally `SpatialIndex.areas_by_name_and_id`. If `None` or missing a
      FIR, that FIR gets an area of 0.
    year: The calendar year being processed, used to drop out-of-year rows and
      to determine the annual divisor for mean radiative forcing. Required:
      there is no default year.
    observed_months: Optional dataset-wide observed months to combine with the
      months present in `fir_grouped_rows` when computing the annual mean RF
      observation window.

  Returns:
    A tuple of (annual_rows, temporal_rows, by_country_summary).
  """
  areas = fir_areas or {}
  in_year_fir_rows, in_year_months, global_fir_ef = _filter_fir_rows_by_year(
      fir_grouped_rows, year, observed_months
  )
  annual_divisor, months_covered, days_covered = mw_per_sq_m_divisor_for_months(
      year, in_year_months
  )

  fir_annual_agg, fir_meta, fir_temporal_rows = (
      _aggregate_fir_annual_and_temporal(in_year_fir_rows, areas)
  )
  fir_annual_rows, fir_by_country_summary = (
      _build_fir_annual_rows_and_country_acc(
          fir_annual_agg,
          fir_meta=fir_meta,
          global_fir_ef=global_fir_ef,
          months_covered=months_covered,
          days_covered=days_covered,
          annual_divisor=annual_divisor,
      )
  )

  _assign_fir_ranks(fir_annual_rows)
  _enrich_fir_country_summaries(
      fir_by_country_summary,
      baseline_stats=baseline_stats,
      global_fir_ef=global_fir_ef,
      annual_divisor=annual_divisor,
      year_fraction=_observed_fraction_of_year(year, days_covered),
  )

  return fir_annual_rows, fir_temporal_rows, fir_by_country_summary
