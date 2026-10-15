"""Metric definitions, specifications, and ranking projections.

Defines the declarative metric table that backs the /rankings and /series
endpoints, machine-readable unit identifiers, and metric resolution utilities.

This module is deliberately free of web-framework imports so that offline
tooling (CLIs, notebooks) can reuse the metric table without depending on
FastAPI. The extraction pipeline does not import it. Callers that serve HTTP
map the exceptions raised here onto status codes; see the exception handlers in
server.py.
"""

from collections import abc
import dataclasses
import math
import types
from typing import Any, overload

from common import constants

# Machine-readable unit identifiers. The server does not render numbers; the
# client maps these to display text and to an Intl.NumberFormat configuration.
UNIT_TONNES_CO2E = "tonnes_co2e"
UNIT_TONNES_CO2E_PER_CAPITA = "tonnes_co2e_per_capita"
UNIT_MILLIWATTS_PER_SQ_M = "milliwatts_per_square_metre"
UNIT_RATIO_OF_NATIONAL_GHG = "ratio_of_national_ghg"
UNIT_RATIO_OF_WORLD_AVG_GHG_PER_CAPITA = "ratio_of_world_avg_ghg_per_capita"
UNIT_MEGATONNES_CO2E = "megatonnes_co2e"
UNIT_SQUARE_KILOMETRES = "square_kilometres"

SUPPORTED_GWPS: tuple[str, ...] = (
    constants.GWP20,
    constants.GWP50,
    constants.GWP100,
)
_GWP_PLACEHOLDER = "{gwp}"


class MetricError(Exception):
  """Base class for metric resolution and validation failures."""


class UnknownMetricError(MetricError):
  """Raised when a requested metric name matches no spec ID."""


class UnsupportedGwpError(MetricError):
  """Raised when a requested GWP horizon is outside SUPPORTED_GWPS."""


class MissingColumnError(MetricError):
  """Raised when a dataset does not carry the column backing a metric."""


@dataclasses.dataclass(frozen=True)
class MetricSpec:
  """A published metric, expressed as the dataset columns that back it.

  Attributes:
    id: Canonical metric identifier, echoed back in the response.
    title: Human-readable English title. The GWP horizon is appended by
      `resolved_title` for horizon-dependent metrics.
    unit: One of the UNIT_* identifiers above.
    field: Dataset column holding the value. A `{gwp}` placeholder marks the
      metric as horizon-dependent.
    rank_field: Dataset column holding a precomputed rank, if one exists.
    secondary_field: Dataset column for supporting figures shown with the value.
    secondary_unit: Unit identifier for `secondary_field`.
  """

  id: str
  title: str
  unit: str
  field: str
  rank_field: str | None = None
  secondary_field: str | None = None
  secondary_unit: str | None = None

  @property
  def is_gwp_dependent(self) -> bool:
    """Indicates whether the backing column varies by GWP horizon.

    Returns:
      True when `field` carries a `{gwp}` placeholder, meaning the metric
      resolves to a different dataset column per horizon.
    """
    return _GWP_PLACEHOLDER in self.field

  @overload
  def resolve(self, template: str, gwp: str) -> str:
    """Substitutes the horizon into a non-None column template."""
    ...

  @overload
  def resolve(self, template: None, gwp: str) -> None:
    """Returns None when the column template is None."""
    ...

  @overload
  def resolve(self, template: str | None, gwp: str) -> str | None:
    """Substitutes the horizon into an optional column template."""
    ...

  def resolve(self, template: str | None, gwp: str) -> str | None:
    """Substitutes the horizon into a column template.

    Args:
      template: Column name, optionally carrying a `{gwp}` placeholder.
      gwp: Normalized GWP horizon, one of SUPPORTED_GWPS.

    Returns:
      The concrete column name, or None when `template` is None.
    """
    return template.format(gwp=gwp) if template else None

  def resolved_title(self, gwp: str) -> str:
    """Returns the display title, suffixed with the horizon when relevant.

    Args:
      gwp: Normalized GWP horizon, one of SUPPORTED_GWPS.

    Returns:
      The title, with the uppercased horizon appended for horizon-dependent
      metrics.
    """
    if not self.is_gwp_dependent:
      return self.title
    return f"{self.title} ({gwp.upper()})"


COUNTRY_METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        id=constants.KEY_MEAN_RF_MW_PER_SQ_M,
        title="Contrail Mean Radiative Forcing",
        unit=UNIT_MILLIWATTS_PER_SQ_M,
        field=constants.KEY_MEAN_RF_MW_PER_SQ_M,
        rank_field=constants.KEY_MEAN_RF_MW_PER_SQ_M_RANK,
    ),
    MetricSpec(
        id="co2e_gwp_tonnes",
        title="Contrail Impact Absolute",
        unit=UNIT_TONNES_CO2E,
        field=constants.FIELD_CO2E_TONNES,
        rank_field=constants.FIELD_CO2E_TONNES_RANK,
    ),
    MetricSpec(
        id="co2e_gwp_tonnes_per_capita",
        title="Contrail Impact Per Capita",
        unit=UNIT_TONNES_CO2E_PER_CAPITA,
        field=constants.FIELD_CO2E_TONNES_PER_CAPITA,
        rank_field=constants.FIELD_CO2E_TONNES_PER_CAPITA_RANK,
        secondary_field=constants.FIELD_CO2E_PER_CAPITA_VS_WORLD_AVG,
        secondary_unit=UNIT_RATIO_OF_WORLD_AVG_GHG_PER_CAPITA,
    ),
    MetricSpec(
        id="co2e_gwp_vs_national_ghg",
        title="vs. National GHG Emissions",
        unit=UNIT_RATIO_OF_NATIONAL_GHG,
        field=constants.FIELD_CO2E_VS_NATIONAL_GHG,
        rank_field=constants.FIELD_CO2E_VS_NATIONAL_GHG_RANK,
        secondary_field=constants.KEY_GHG_MTCO2E,
        secondary_unit=UNIT_MEGATONNES_CO2E,
    ),
    MetricSpec(
        id="outgoing_flight_co2e_gwp_tonnes",
        title="Outgoing Flights Contrail Impact",
        unit=UNIT_TONNES_CO2E,
        field=(
            f"{constants.PREFIX_OUTGOING_FLIGHT}{constants.FIELD_CO2E_TONNES}"
        ),
        rank_field=(
            f"{constants.PREFIX_OUTGOING_FLIGHT}"
            f"{constants.FIELD_CO2E_TONNES_RANK}"
        ),
    ),
    MetricSpec(
        id="outgoing_flight_co2e_gwp_tonnes_per_capita",
        title="Outgoing Impact Per Capita",
        unit=UNIT_TONNES_CO2E_PER_CAPITA,
        field=(
            f"{constants.PREFIX_OUTGOING_FLIGHT}"
            f"{constants.FIELD_CO2E_TONNES_PER_CAPITA}"
        ),
        rank_field=(
            f"{constants.PREFIX_OUTGOING_FLIGHT}"
            f"{constants.FIELD_CO2E_TONNES_PER_CAPITA_RANK}"
        ),
        secondary_field=(
            f"{constants.PREFIX_OUTGOING_FLIGHT}"
            f"{constants.FIELD_CO2E_PER_CAPITA_VS_WORLD_AVG}"
        ),
        secondary_unit=UNIT_RATIO_OF_WORLD_AVG_GHG_PER_CAPITA,
    ),
    MetricSpec(
        id="outgoing_flight_co2e_gwp_vs_national_ghg",
        title="Outgoing vs. National GHG",
        unit=UNIT_RATIO_OF_NATIONAL_GHG,
        field=(
            f"{constants.PREFIX_OUTGOING_FLIGHT}"
            f"{constants.FIELD_CO2E_VS_NATIONAL_GHG}"
        ),
        rank_field=(
            f"{constants.PREFIX_OUTGOING_FLIGHT}"
            f"{constants.FIELD_CO2E_VS_NATIONAL_GHG_RANK}"
        ),
        secondary_field=constants.KEY_GHG_MTCO2E,
        secondary_unit=UNIT_MEGATONNES_CO2E,
    ),
    MetricSpec(
        id="fir_total_co2e_gwp_tonnes",
        title="Controlled FIRs Contrail Impact",
        unit=UNIT_TONNES_CO2E,
        field=f"{constants.PREFIX_FIR_TOTAL}{constants.FIELD_CO2E_TONNES}",
        rank_field=(
            f"{constants.PREFIX_FIR_TOTAL}{constants.FIELD_CO2E_TONNES_RANK}"
        ),
    ),
    MetricSpec(
        id="fir_total_mean_rf_mw_per_sq_m",
        title="Controlled FIRs Mean Radiative Forcing",
        unit=UNIT_MILLIWATTS_PER_SQ_M,
        field=(
            f"{constants.PREFIX_FIR_TOTAL}{constants.KEY_MEAN_RF_MW_PER_SQ_M}"
        ),
        rank_field=(
            f"{constants.PREFIX_FIR_TOTAL}"
            f"{constants.KEY_MEAN_RF_MW_PER_SQ_M_RANK}"
        ),
    ),
    MetricSpec(
        id="fir_total_co2e_gwp_tonnes_per_capita",
        title="Controlled FIRs Impact Per Capita",
        unit=UNIT_TONNES_CO2E_PER_CAPITA,
        field=(
            f"{constants.PREFIX_FIR_TOTAL}"
            f"{constants.FIELD_CO2E_TONNES_PER_CAPITA}"
        ),
        rank_field=(
            f"{constants.PREFIX_FIR_TOTAL}"
            f"{constants.FIELD_CO2E_TONNES_PER_CAPITA_RANK}"
        ),
        secondary_field=(
            f"{constants.PREFIX_FIR_TOTAL}"
            f"{constants.FIELD_CO2E_PER_CAPITA_VS_WORLD_AVG}"
        ),
        secondary_unit=UNIT_RATIO_OF_WORLD_AVG_GHG_PER_CAPITA,
    ),
    MetricSpec(
        id="fir_total_co2e_gwp_vs_national_ghg",
        title="Controlled FIRs vs. National GHG",
        unit=UNIT_RATIO_OF_NATIONAL_GHG,
        field=(
            f"{constants.PREFIX_FIR_TOTAL}"
            f"{constants.FIELD_CO2E_VS_NATIONAL_GHG}"
        ),
        rank_field=(
            f"{constants.PREFIX_FIR_TOTAL}"
            f"{constants.FIELD_CO2E_VS_NATIONAL_GHG_RANK}"
        ),
        secondary_field=constants.KEY_GHG_MTCO2E,
        secondary_unit=UNIT_MEGATONNES_CO2E,
    ),
)

FIR_ROW_METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        id="fir_individual_co2e_gwp_tonnes",
        title="FIR Contrail Impact",
        unit=UNIT_TONNES_CO2E,
        field=constants.FIELD_CO2E_TONNES,
        rank_field=constants.FIELD_CO2E_TONNES_RANK,
        secondary_field=constants.KEY_AREA_SQ_KM,
        secondary_unit=UNIT_SQUARE_KILOMETRES,
    ),
    MetricSpec(
        id="fir_individual_mean_rf_mw_per_sq_m",
        title="FIR Mean Radiative Forcing",
        unit=UNIT_MILLIWATTS_PER_SQ_M,
        field=constants.KEY_MEAN_RF_MW_PER_SQ_M,
        rank_field=constants.KEY_MEAN_RF_MW_PER_SQ_M_RANK,
        secondary_field=constants.KEY_AREA_SQ_KM,
        secondary_unit=UNIT_SQUARE_KILOMETRES,
    ),
)


def index_metrics(
    specs: abc.Sequence[MetricSpec],
) -> abc.Mapping[str, MetricSpec]:
  """Builds an ID -> spec lookup, rejecting invalid or duplicate IDs.

  Args:
    specs: Metric specifications to index by ID.

  Returns:
    A mapping from every metric ID to its specification.

  Raises:
    ValueError: If an ID is empty, not normalized (lowercase, stripped), or
      claimed by more than one specification.
  """
  index: dict[str, MetricSpec] = {}
  for spec in specs:
    if not spec.id or spec.id != spec.id.lower().strip():
      raise ValueError(
          f"Metric ID {spec.id!r} for {spec.title!r} must be non-empty, "
          "lowercase, and stripped."
      )
    if (existing := index.get(spec.id)) is not None:
      raise ValueError(
          f"Metric ID {spec.id!r} is claimed by both {existing.title!r} and "
          f"{spec.title!r}."
      )
    index[spec.id] = spec
  return types.MappingProxyType(index)


def _index_metrics_by_unit(
    specs: abc.Sequence[MetricSpec],
) -> abc.Mapping[str, MetricSpec]:
  """Builds a unit -> spec lookup, rejecting unit collisions at import time."""
  by_unit: dict[str, MetricSpec] = {}
  for spec in specs:
    if (existing := by_unit.get(spec.unit)) is not None:
      raise ValueError(
          f"Metric unit {spec.unit!r} is claimed by both {existing.id!r} and "
          f"{spec.id!r}."
      )
    by_unit[spec.unit] = spec
  return types.MappingProxyType(by_unit)


COUNTRY_METRIC_INDEX: abc.Mapping[str, MetricSpec] = index_metrics(
    COUNTRY_METRICS
)
FIR_ROW_METRIC_INDEX: abc.Mapping[str, MetricSpec] = index_metrics(
    FIR_ROW_METRICS
)
RANKING_METRIC_INDEX: abc.Mapping[str, MetricSpec] = index_metrics(
    (*COUNTRY_METRICS, *FIR_ROW_METRICS)
)
_FIR_ROW_METRIC_BY_UNIT: abc.Mapping[str, MetricSpec] = _index_metrics_by_unit(
    FIR_ROW_METRICS
)
_SUPPORTED_FIR_SERIES_METRIC_IDS: tuple[str, ...] = tuple(
    sorted((
        *FIR_ROW_METRIC_INDEX,
        *(
            spec.id
            for spec in COUNTRY_METRICS
            if spec.unit in _FIR_ROW_METRIC_BY_UNIT
        ),
    ))
)


def normalize_gwp(gwp: str) -> str:
  """Validates and canonicalizes a GWP horizon.

  Args:
    gwp: Requested horizon, in any casing and with optional surrounding space.

  Returns:
    The lowercased horizon, guaranteed to be a member of SUPPORTED_GWPS.

  Raises:
    UnsupportedGwpError: If the horizon is not one of SUPPORTED_GWPS.
  """
  value = (gwp or "").lower().strip()
  if value not in SUPPORTED_GWPS:
    raise UnsupportedGwpError(
        f"Unsupported gwp {gwp!r}. Expected one of {', '.join(SUPPORTED_GWPS)}."
    )
  return value


def lookup_metric(
    index: abc.Mapping[str, MetricSpec],
    metric: str,
) -> MetricSpec:
  """Looks up a metric specification by canonical ID.

  Args:
    index: Mapping produced by `index_metrics`.
    metric: Requested metric ID, in any casing.

  Returns:
    The matching metric specification.

  Raises:
    UnknownMetricError: If no specification claims the requested ID.
  """
  spec = index.get((metric or "").lower().strip())
  if spec is None:
    raise UnknownMetricError(
        f"Unknown metric {metric!r}. Supported: {', '.join(sorted(index))}."
    )
  return spec


def project_to_fir_row_metric(metric: str) -> MetricSpec:
  """Maps a requested metric onto the per-FIR row equivalent.

  Country-level metrics are projected onto the FIR-row metric sharing their
  unit, so that a caller asking for a country metric on a FIR entity receives
  the comparable per-FIR column rather than an error.

  Args:
    metric: Requested metric ID, in any casing.

  Returns:
    The per-FIR metric specification backing the request.

  Raises:
    UnknownMetricError: If the ID matches neither a FIR-row metric nor a
      country metric with a per-FIR row equivalent.
  """
  metric_clean = (metric or "").lower().strip()
  spec = FIR_ROW_METRIC_INDEX.get(metric_clean)
  if spec is not None:
    return spec
  supported = ", ".join(_SUPPORTED_FIR_SERIES_METRIC_IDS)
  country_spec = COUNTRY_METRIC_INDEX.get(metric_clean)
  if country_spec is None:
    raise UnknownMetricError(
        f"Unknown metric {metric!r}. Supported for FIR series: {supported}."
    )
  fir_spec = _FIR_ROW_METRIC_BY_UNIT.get(country_spec.unit)
  if fir_spec is None:
    raise UnknownMetricError(
        f"Metric {metric!r} (unit {country_spec.unit!r}) has no per-FIR row "
        f"equivalent. Supported for FIR series: {supported}."
    )
  return fir_spec


def coerce_float(value: Any) -> float | None:
  """Returns a finite float, or None when the value is absent or unusable.

  Args:
    value: Raw dataset value of unknown type.

  Returns:
    The value as a finite float, or None for None, booleans, non-numeric
    values, NaN, and infinities.
  """
  if value is None or isinstance(value, bool):
    return None
  try:
    number = float(value)
  except (TypeError, ValueError):
    return None
  if math.isnan(number) or math.isinf(number):
    return None
  return number


def require_column(
    rows: abc.Sequence[abc.Mapping[str, Any]],
    column: str,
    *,
    metric: str,
    gwp: str,
    year: int,
    dataset: str,
) -> None:
  """Fails loudly when a dataset does not carry the requested column.

  Args:
    rows: Dataset rows to inspect for the column.
    column: Concrete (horizon-resolved) column name the metric is backed by.
    metric: Metric ID, used only to build the error message.
    gwp: Normalized GWP horizon, used only to build the error message.
    year: Dataset year, used only to build the error message.
    dataset: Dataset identifier, used only to build the error message.

  Raises:
    MissingColumnError: If no row carries the column. Silently substituting a
      different horizon would publish a number under the wrong label.
  """
  if any(column in row for row in rows):
    return
  raise MissingColumnError(
      f"Metric {metric!r} at {gwp} is backed by column {column!r}, which "
      f"dataset {dataset!r} does not provide for {year}. No substitute "
      "horizon is served. Re-run the extraction pipeline for this dataset, "
      "or request a horizon it does provide."
  )
