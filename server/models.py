"""Pydantic request and response schemas for regional contrails dashboard API.

Provides strongly-typed schemas for country and FIR contrail impact metrics,
rankings, multi-year time series, grid availability, and temporal matrices.
"""

import pydantic


class CountryMetricsResponse(pydantic.BaseModel):
  """Pydantic model representing contrail impact metrics for a country."""

  model_config = pydantic.ConfigDict(extra="forbid")

  name: str | None = None
  iso_a2: str | None = None
  iso_a3: str | None = None
  months_covered: int | None = None
  days_covered: int | None = None
  segment_count: int | None = None
  contrail_segment_count: int | None = None
  total_flight_distance_km: float | None = None
  contrail_flight_distance_km: float | None = None
  total_flight_impact: float | None = None
  local_flight_impact: float | None = None
  area_sq_km: float | None = None
  ghg_mtco2e: float | None = None
  population: int | None = None
  impact_density_sq_km: float | None = None
  in_country_impact: float | None = None
  mean_rf_mw_per_sq_m: float | None = None
  mean_rf_mw_per_sq_m_rank: int | None = None
  outgoing_flight_impact: float | None = None
  outgoing_flight_impact_rank: int | None = None
  over_region_vs_outgoing_impact: float | None = None
  # Fraction of the dataset total (signed, identical for every GWP horizon);
  # None when the dataset total is zero.
  co2e_tonnes_share: float | None = None
  outgoing_flight_co2e_tonnes_share: float | None = None

  co2e_gwp20_tonnes: float | None = None
  co2e_gwp20_tonnes_rank: int | None = None
  co2e_gwp20_tonnes_sq_km: float | None = None
  co2e_gwp20_vs_national_ghg: float | None = None
  co2e_gwp20_vs_national_ghg_rank: int | None = None
  co2e_gwp20_tonnes_per_capita: float | None = None
  co2e_gwp20_tonnes_per_capita_rank: int | None = None
  co2e_gwp20_per_capita_vs_world_avg: float | None = None

  co2e_gwp50_tonnes: float | None = None
  co2e_gwp50_tonnes_rank: int | None = None
  co2e_gwp50_tonnes_sq_km: float | None = None
  co2e_gwp50_vs_national_ghg: float | None = None
  co2e_gwp50_vs_national_ghg_rank: int | None = None
  co2e_gwp50_tonnes_per_capita: float | None = None
  co2e_gwp50_tonnes_per_capita_rank: int | None = None
  co2e_gwp50_per_capita_vs_world_avg: float | None = None

  co2e_gwp100_tonnes: float | None = None
  co2e_gwp100_tonnes_rank: int | None = None
  co2e_gwp100_tonnes_sq_km: float | None = None
  co2e_gwp100_vs_national_ghg: float | None = None
  co2e_gwp100_vs_national_ghg_rank: int | None = None
  co2e_gwp100_tonnes_per_capita: float | None = None
  co2e_gwp100_tonnes_per_capita_rank: int | None = None
  co2e_gwp100_per_capita_vs_world_avg: float | None = None

  outgoing_flight_co2e_gwp20_tonnes: float | None = None
  outgoing_flight_co2e_gwp20_tonnes_rank: int | None = None
  outgoing_flight_co2e_gwp20_vs_national_ghg: float | None = None
  outgoing_flight_co2e_gwp20_vs_national_ghg_rank: int | None = None
  outgoing_flight_co2e_gwp20_tonnes_per_capita: float | None = None
  outgoing_flight_co2e_gwp20_tonnes_per_capita_rank: int | None = None
  outgoing_flight_co2e_gwp20_per_capita_vs_world_avg: float | None = None

  outgoing_flight_co2e_gwp50_tonnes: float | None = None
  outgoing_flight_co2e_gwp50_tonnes_rank: int | None = None
  outgoing_flight_co2e_gwp50_vs_national_ghg: float | None = None
  outgoing_flight_co2e_gwp50_vs_national_ghg_rank: int | None = None
  outgoing_flight_co2e_gwp50_tonnes_per_capita: float | None = None
  outgoing_flight_co2e_gwp50_tonnes_per_capita_rank: int | None = None
  outgoing_flight_co2e_gwp50_per_capita_vs_world_avg: float | None = None

  outgoing_flight_co2e_gwp100_tonnes: float | None = None
  outgoing_flight_co2e_gwp100_tonnes_rank: int | None = None
  outgoing_flight_co2e_gwp100_vs_national_ghg: float | None = None
  outgoing_flight_co2e_gwp100_vs_national_ghg_rank: int | None = None
  outgoing_flight_co2e_gwp100_tonnes_per_capita: float | None = None
  outgoing_flight_co2e_gwp100_tonnes_per_capita_rank: int | None = None
  outgoing_flight_co2e_gwp100_per_capita_vs_world_avg: float | None = None

  # The FIRs attributed to the country per scope (domestic, international,
  # total): one entry per FIR, index-aligned across `_fir_id`, `_firname` and
  # `_fir_icao` and sorted by FIR ID. Empty for a scope without FIRs; None for
  # a country that controls no FIR at all.
  fir_dom_segment_count: int | None = None
  fir_dom_contrail_segment_count: int | None = None
  fir_dom_flight_distance_km: float | None = None
  fir_dom_contrail_flight_distance_km: float | None = None
  fir_dom_area_sq_km: float | None = None
  fir_dom_co2e_tonnes_share: float | None = None
  fir_dom_fir_id: list[str] | None = None
  fir_dom_firname: list[str] | None = None
  fir_dom_fir_icao: list[str] | None = None
  fir_dom_impact_density_sq_km: float | None = None
  fir_dom_mean_rf_mw_per_sq_m: float | None = None
  fir_dom_mean_rf_mw_per_sq_m_rank: int | None = None
  fir_dom_co2e_gwp20_tonnes: float | None = None
  fir_dom_co2e_gwp20_tonnes_rank: int | None = None
  fir_dom_co2e_gwp20_tonnes_sq_km: float | None = None
  fir_dom_co2e_gwp20_vs_national_ghg: float | None = None
  fir_dom_co2e_gwp20_vs_national_ghg_rank: int | None = None
  fir_dom_co2e_gwp20_tonnes_per_capita: float | None = None
  fir_dom_co2e_gwp20_tonnes_per_capita_rank: int | None = None
  fir_dom_co2e_gwp20_per_capita_vs_world_avg: float | None = None
  fir_dom_co2e_gwp50_tonnes: float | None = None
  fir_dom_co2e_gwp50_tonnes_rank: int | None = None
  fir_dom_co2e_gwp50_tonnes_sq_km: float | None = None
  fir_dom_co2e_gwp50_vs_national_ghg: float | None = None
  fir_dom_co2e_gwp50_vs_national_ghg_rank: int | None = None
  fir_dom_co2e_gwp50_tonnes_per_capita: float | None = None
  fir_dom_co2e_gwp50_tonnes_per_capita_rank: int | None = None
  fir_dom_co2e_gwp50_per_capita_vs_world_avg: float | None = None
  fir_dom_co2e_gwp100_tonnes: float | None = None
  fir_dom_co2e_gwp100_tonnes_rank: int | None = None
  fir_dom_co2e_gwp100_tonnes_sq_km: float | None = None
  fir_dom_co2e_gwp100_vs_national_ghg: float | None = None
  fir_dom_co2e_gwp100_vs_national_ghg_rank: int | None = None
  fir_dom_co2e_gwp100_tonnes_per_capita: float | None = None
  fir_dom_co2e_gwp100_tonnes_per_capita_rank: int | None = None
  fir_dom_co2e_gwp100_per_capita_vs_world_avg: float | None = None

  fir_int_segment_count: int | None = None
  fir_int_contrail_segment_count: int | None = None
  fir_int_flight_distance_km: float | None = None
  fir_int_contrail_flight_distance_km: float | None = None
  fir_int_area_sq_km: float | None = None
  fir_int_co2e_tonnes_share: float | None = None
  fir_int_fir_id: list[str] | None = None
  fir_int_firname: list[str] | None = None
  fir_int_fir_icao: list[str] | None = None
  fir_int_impact_density_sq_km: float | None = None
  fir_int_mean_rf_mw_per_sq_m: float | None = None
  fir_int_mean_rf_mw_per_sq_m_rank: int | None = None
  fir_int_co2e_gwp20_tonnes: float | None = None
  fir_int_co2e_gwp20_tonnes_rank: int | None = None
  fir_int_co2e_gwp20_tonnes_sq_km: float | None = None
  fir_int_co2e_gwp20_vs_national_ghg: float | None = None
  fir_int_co2e_gwp20_vs_national_ghg_rank: int | None = None
  fir_int_co2e_gwp20_tonnes_per_capita: float | None = None
  fir_int_co2e_gwp20_tonnes_per_capita_rank: int | None = None
  fir_int_co2e_gwp20_per_capita_vs_world_avg: float | None = None
  fir_int_co2e_gwp50_tonnes: float | None = None
  fir_int_co2e_gwp50_tonnes_rank: int | None = None
  fir_int_co2e_gwp50_tonnes_sq_km: float | None = None
  fir_int_co2e_gwp50_vs_national_ghg: float | None = None
  fir_int_co2e_gwp50_vs_national_ghg_rank: int | None = None
  fir_int_co2e_gwp50_tonnes_per_capita: float | None = None
  fir_int_co2e_gwp50_tonnes_per_capita_rank: int | None = None
  fir_int_co2e_gwp50_per_capita_vs_world_avg: float | None = None
  fir_int_co2e_gwp100_tonnes: float | None = None
  fir_int_co2e_gwp100_tonnes_rank: int | None = None
  fir_int_co2e_gwp100_tonnes_sq_km: float | None = None
  fir_int_co2e_gwp100_vs_national_ghg: float | None = None
  fir_int_co2e_gwp100_vs_national_ghg_rank: int | None = None
  fir_int_co2e_gwp100_tonnes_per_capita: float | None = None
  fir_int_co2e_gwp100_tonnes_per_capita_rank: int | None = None
  fir_int_co2e_gwp100_per_capita_vs_world_avg: float | None = None

  fir_total_segment_count: int | None = None
  fir_total_contrail_segment_count: int | None = None
  fir_total_flight_distance_km: float | None = None
  fir_total_contrail_flight_distance_km: float | None = None
  fir_total_area_sq_km: float | None = None
  fir_total_co2e_tonnes_share: float | None = None
  fir_total_fir_id: list[str] | None = None
  fir_total_firname: list[str] | None = None
  fir_total_fir_icao: list[str] | None = None
  fir_total_impact_density_sq_km: float | None = None
  fir_total_mean_rf_mw_per_sq_m: float | None = None
  fir_total_mean_rf_mw_per_sq_m_rank: int | None = None
  fir_total_co2e_gwp20_tonnes: float | None = None
  fir_total_co2e_gwp20_tonnes_rank: int | None = None
  fir_total_co2e_gwp20_tonnes_sq_km: float | None = None
  fir_total_co2e_gwp20_vs_national_ghg: float | None = None
  fir_total_co2e_gwp20_vs_national_ghg_rank: int | None = None
  fir_total_co2e_gwp20_tonnes_per_capita: float | None = None
  fir_total_co2e_gwp20_tonnes_per_capita_rank: int | None = None
  fir_total_co2e_gwp20_per_capita_vs_world_avg: float | None = None
  fir_total_co2e_gwp50_tonnes: float | None = None
  fir_total_co2e_gwp50_tonnes_rank: int | None = None
  fir_total_co2e_gwp50_tonnes_sq_km: float | None = None
  fir_total_co2e_gwp50_vs_national_ghg: float | None = None
  fir_total_co2e_gwp50_vs_national_ghg_rank: int | None = None
  fir_total_co2e_gwp50_tonnes_per_capita: float | None = None
  fir_total_co2e_gwp50_tonnes_per_capita_rank: int | None = None
  fir_total_co2e_gwp50_per_capita_vs_world_avg: float | None = None
  fir_total_co2e_gwp100_tonnes: float | None = None
  fir_total_co2e_gwp100_tonnes_rank: int | None = None
  fir_total_co2e_gwp100_tonnes_sq_km: float | None = None
  fir_total_co2e_gwp100_vs_national_ghg: float | None = None
  fir_total_co2e_gwp100_vs_national_ghg_rank: int | None = None
  fir_total_co2e_gwp100_tonnes_per_capita: float | None = None
  fir_total_co2e_gwp100_tonnes_per_capita_rank: int | None = None
  fir_total_co2e_gwp100_per_capita_vs_world_avg: float | None = None


class FirManagingCountry(pydantic.BaseModel):
  """Country that controls or co-controls a Flight Information Region."""

  model_config = pydantic.ConfigDict(extra="forbid")

  iso_a2: str
  name: str


class FirMetricsResponse(pydantic.BaseModel):
  """Pydantic model representing contrail impact metrics for a FIR."""

  model_config = pydantic.ConfigDict(extra="forbid")

  fir_id: str | None = None
  fir_icao: str | None = None
  firname: str | None = None
  type: str | None = None
  # ISO alpha-2 codes of the controlling countries; the first is the primary.
  fir_to_country_iso2s: list[str] | None = None
  months_covered: int | None = None
  days_covered: int | None = None
  segment_count: int | None = None
  contrail_segment_count: int | None = None
  total_flight_distance_km: float | None = None
  contrail_flight_distance_km: float | None = None
  area_sq_km: float | None = None
  impact_density_sq_km: float | None = None
  mean_rf_mw_per_sq_m: float | None = None
  mean_rf_mw_per_sq_m_rank: int | None = None
  co2e_gwp20_tonnes: float | None = None
  co2e_gwp50_tonnes: float | None = None
  co2e_gwp100_tonnes: float | None = None
  co2e_gwp20_tonnes_sq_km: float | None = None
  co2e_gwp50_tonnes_sq_km: float | None = None
  co2e_gwp100_tonnes_sq_km: float | None = None
  co2e_gwp20_tonnes_rank: int | None = None
  co2e_gwp50_tonnes_rank: int | None = None
  co2e_gwp100_tonnes_rank: int | None = None
  # Fraction of the all-FIR total (signed, identical for every GWP horizon);
  # None when the total is zero.
  co2e_tonnes_share: float | None = None
  countries: list[FirManagingCountry] | None = None


class AvailableYearsResponse(pydantic.BaseModel):
  """Years with published data for the active dataset, sorted ascending.

  There is no server-side default year: clients pick one of these years and
  pass it explicitly on every year-scoped request.
  """

  model_config = pydantic.ConfigDict(extra="forbid")

  years: list[int]


class RankingItem(pydantic.BaseModel):
  """A single country or FIR entry in a ranking list."""

  model_config = pydantic.ConfigDict(extra="forbid")

  # 1-based position among entries with a value; None when `value` is None
  # (e.g. a per-area metric for an entity without area), shown as "N/A".
  rank: int | None
  name: str
  iso_a2: str
  fir_id: str | None = None
  fir_icao: str | None = None
  firname: str | None = None
  type: str | None = None
  country_name: str | None = None
  countries: list[FirManagingCountry] | None = None
  value: float | None = None
  secondary_value: float | None = None


class RankingsResponse(pydantic.BaseModel):
  """Response model for sorted country or FIR rankings by metric."""

  model_config = pydantic.ConfigDict(extra="forbid")

  metric: str
  title: str
  year: int
  gwp: str
  unit: str
  secondary_unit: str | None = None
  isFirRanking: bool | None = None
  items: list[RankingItem]


class TimeSeriesPoint(pydantic.BaseModel):
  """A single year observation in a multi-year metric series."""

  model_config = pydantic.ConfigDict(extra="forbid")

  year: int
  value: float | None = None


class TimeSeriesResponse(pydantic.BaseModel):
  """Response model for a multi-year time series of a metric."""

  model_config = pydantic.ConfigDict(extra="forbid")

  metric: str
  metric_title: str
  unit: str
  iso_a2: str | None = None
  country_name: str | None = None
  fir_id: str | None = None
  fir_icao: str | None = None
  firname: str | None = None
  gwp: str
  points: list[TimeSeriesPoint]


class GridAvailabilityResponse(pydantic.BaseModel):
  """Availability of total, monthly, and hourly grids for a year."""

  model_config = pydantic.ConfigDict(extra="forbid")

  year: int
  has_total: bool
  has_monthly: bool
  has_hourly: bool
  months_count: int
  hours_count: int


class TemporalMatrixResponse(pydantic.BaseModel):
  """24x12 temporal matrix of contrail metrics across hours and months."""

  model_config = pydantic.ConfigDict(extra="forbid")

  year: int
  iso_a2: str
  name: str
  is_fir: bool = False
  fir_id: str | None = None
  fir_icao: str | None = None
  fir_name: str | None = None
  months: list[str]
  hours: list[str]
  co2e_gwp20_tonnes: list[list[float]] | None = None
  co2e_gwp50_tonnes: list[list[float]]
  co2e_gwp100_tonnes: list[list[float]] | None = None
  segment_count: list[list[int]]
  contrail_segment_count: list[list[int]]
  total_flight_distance_km: list[list[float]] | None = None
  contrail_flight_distance_km: list[list[float]] | None = None
