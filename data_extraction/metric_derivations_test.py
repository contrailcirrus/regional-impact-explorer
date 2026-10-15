"""Unit tests for metric_derivations module."""

from collections.abc import Mapping
import itertools
from typing import Any

from absl import flags
from absl.testing import absltest
from absl.testing import parameterized

from common import constants
from data_extraction import metric_derivations


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


_TIE_BREAK_COUNTRY_ROWS = (
    (("C", "CC", 202501, 12), (1, 1, 5.0, 0.0, 5.0, 0.0, 0.0, 0.0)),
    (("A", "AA", 202501, 12), (1, 1, 5.0, 0.0, 5.0, 0.0, 0.0, 0.0)),
    (("Kosovo", "-99", 202501, 12), (1, 1, 5.0, 0.0, 5.0, 0.0, 0.0, 0.0)),
    (
        ("Northern Cyprus", "-99", 202501, 12),
        (1, 1, 5.0, 0.0, 5.0, 0.0, 0.0, 0.0),
    ),
    (("B", "BB", 202501, 12), (1, 1, 9.0, 0.0, 9.0, 0.0, 0.0, 0.0)),
)

_TIE_BREAK_FIR_ROWS = (
    (
        ("FIR CCCC", "CCCC", "BE", "domestic", 202501, 12),
        (10, 5, 100.0, 0.0, 0.0),
    ),
    (
        ("FIR AAAA", "AAAA", "BE", "domestic", 202501, 12),
        (10, 5, 100.0, 0.0, 0.0),
    ),
    (
        ("FIR BBBB", "BBBB", "BE", "domestic", 202501, 12),
        (10, 5, 100.0, 0.0, 0.0),
    ),
)

_SAMPLE_COUNTRY_GROUPED_ROWS = (
    (
        ("Belgium", "BE", 202501, 12),
        (10, 5, 1000.0, 400.0, 800.0, 200.0, 0.0, 0.0),
    ),
    (
        ("Belgium", "BE", 202502, 14),
        (5, 2, 500.0, 200.0, 400.0, 100.0, 0.0, 0.0),
    ),
    (
        ("Germany", "DE", 202501, 8),
        (20, 10, 5000.0, 2000.0, 4000.0, 500.0, 0.0, 0.0),
    ),
)

_FLIGHT_DISTANCE_FIR_ROWS = (
    (
        ("BRUSSELS FIR", "EBBU", "BE", "domestic", 202501, 12),
        (10, 5, 1000.0, 450.5, 220.25),
    ),
    (
        ("SHANWICK OCEANIC", "EGGX", "BE,GB", "international", 202502, 14),
        (4, 1, 300.0, 800.0, 200.0),
    ),
)

_FLIGHT_DISTANCE_COUNTRY_ROWS = (
    (
        ("Belgium", "BE", 202501, 12),
        (10, 5, 1000.0, 400.0, 800.0, 200.0, 450.5, 220.25),
    ),
    (
        ("Belgium", "BE", 202502, 14),
        (5, 2, 500.0, 200.0, 400.0, 100.0, 149.5, 79.75),
    ),
)

_NEGATIVE_RANK_COUNTRY_ROWS = (
    (
        ("Belgium", "BE", 202501, 12),
        (10, 5, 1000.0, 400.0, 800.0, 200.0, 0.0, 0.0),
    ),
    (
        ("Iceland", "IS", 202501, 12),
        (10, 5, -500.0, -100.0, -400.0, -150.0, 0.0, 0.0),
    ),
    (
        ("Kosovo", "-99", 202501, 12),
        (5, 2, 100.0, 0.0, 100.0, 0.0, 0.0, 0.0),
    ),
)


def _finalize_net_cooling_and_zero_impact_countries() -> (
    dict[str, dict[str, Any]]
):
  """Returns finalized country rows keyed by ISO for cooling and zero impact."""
  country_grouped_rows = [
      (
          ("Iceland", "IS", 202501, 12),
          (10, 5, -500.0, -100.0, -400.0, -150.0, 0.0, 0.0),
      ),
      (
          ("ZeroImpact", "ZI", 202501, 12),
          (5, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
      ),
  ]
  country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
      country_grouped_rows=country_grouped_rows,
      fir_summary_by_iso={},
      country_areas={"IS": 103000.0, "ZI": 1000.0},
      baseline_stats={
          "IS": {"population": 370000.0, "ghg_mtco2e": 4.5},
          "ZI": {"population": 10000.0, "ghg_mtco2e": 1.0},
      },
      year=2025,
  )
  return {row["iso_a2"]: row for row in country_rows}


def _finalize_sample_country_and_temporal() -> (
    tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]
):
  """Returns sample finalized country and temporal rows keyed by iso_a2."""
  fir_summary = {
      "BE": {
          "fir_default_scope": "territorial",
          "territorial_total_co2e_gwp50_tonnes": 50.0,
          "default_total_co2e_gwp50_tonnes": 50.0,
          "default_mean_rf_mw_per_sq_m": 1.2,
          "default_area_sq_km": 30528.0,
      },
  }
  country_rows, temporal_rows = (
      metric_derivations.finalize_country_and_temporal_datasets(
          country_grouped_rows=list(_SAMPLE_COUNTRY_GROUPED_ROWS),
          fir_summary_by_iso=fir_summary,
          country_areas={"BE": 30528.0, "DE": 357022.0},
          baseline_stats={
              "BE": {"population": 11500000.0, "ghg_mtco2e": 100.0},
              "DE": {"population": 83000000.0, "ghg_mtco2e": 700.0},
          },
          year=2025,
      )
  )
  return (
      {row["iso_a2"]: row for row in country_rows},
      {row["iso_a2"]: row for row in temporal_rows},
  )


def _finalize_country_with_and_without_polygon() -> dict[str, dict[str, Any]]:
  """Returns finalized country rows for BE (with polygon) and SG (without)."""
  country_grouped_rows = [
      (
          ("Belgium", "BE", 202501, 12),
          (10, 5, 1000.0, 400.0, 800.0, 200.0, 0.0, 0.0),
      ),
      (("SG", "SG", 202501, 12), (3, 1, 0.0, 0.0, 0.0, 300.0, 0.0, 0.0)),
  ]
  baseline_stats = {
      "BE": {
          "iso_a3": "BEL",
          "name": "Belgium",
          "population": 11500000.0,
          "ghg_mtco2e": 100.0,
      },
      "SG": {
          "iso_a3": "SGP",
          "name": "Singapore",
          "population": 6000000.0,
          "ghg_mtco2e": 60.0,
      },
  }
  country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
      country_grouped_rows=country_grouped_rows,
      fir_summary_by_iso={},
      country_areas={"BE": 30528.0},
      baseline_stats=baseline_stats,
      year=2025,
  )
  return {row["iso_a2"]: row for row in country_rows}


def _finalize_flight_distance_datasets() -> dict[str, Any]:
  """Returns finalized FIR and country datasets with flight distance metrics."""
  fir_areas = {
      ("BRUSSELS FIR", "EBBU"): 30528.0,
      ("SHANWICK OCEANIC", "EGGX"): 100000.0,
  }
  fir_annual, fir_temporal, fir_summary = (
      metric_derivations.finalize_fir_dataset(
          list(_FLIGHT_DISTANCE_FIR_ROWS), {}, fir_areas, year=2025
      )
  )
  country_rows, temporal_rows = (
      metric_derivations.finalize_country_and_temporal_datasets(
          country_grouped_rows=list(_FLIGHT_DISTANCE_COUNTRY_ROWS),
          fir_summary_by_iso=fir_summary,
          country_areas={"BE": 30528.0, "GB": 242495.0},
          baseline_stats={},
          year=2025,
      )
  )
  return {
      "fir_annual": {row["fir_icao"]: row for row in fir_annual},
      "fir_temporal": {row["fir_icao"]: row for row in fir_temporal},
      "fir_summary": fir_summary,
      "country_rows": {row["iso_a2"]: row for row in country_rows},
      "temporal_rows": {row["iso_a2"]: row for row in temporal_rows},
  }


def _finalize_name_variant_and_sentinel_datasets() -> (
    tuple[list[dict[str, Any]], list[dict[str, Any]]]
):
  """Returns finalized rows for ISO name variants and sentinel -99 regions."""
  country_grouped_rows = [
      (
          ("Belgium", "BE", 202501, 12),
          (10, 5, 1000.0, 400.0, 800.0, 0.0, 0.0, 0.0),
      ),
      (("BE", "BE", 202501, 12), (0, 0, 0.0, 0.0, 0.0, 200.0, 0.0, 0.0)),
      (
          ("Kingdom of Belgium", "BE", 202502, 14),
          (5, 2, 500.0, 200.0, 400.0, 100.0, 0.0, 0.0),
      ),
      (
          ("Kosovo", "-99", 202501, 12),
          (3, 1, 300.0, 0.0, 300.0, 0.0, 0.0, 0.0),
      ),
      (
          ("N. Cyprus", "-99", 202501, 12),
          (2, 1, 100.0, 0.0, 100.0, 0.0, 0.0, 0.0),
      ),
  ]
  country_areas = {
      "BE": 30528.0,
      "-99": 189450.0,
      "-99|KOSOVO": 11232.0,
      "-99|N. CYPRUS": 3791.0,
  }
  return metric_derivations.finalize_country_and_temporal_datasets(
      country_grouped_rows=country_grouped_rows,
      fir_summary_by_iso={},
      country_areas=country_areas,
      baseline_stats={"BE": {"population": 11500000.0, "ghg_mtco2e": 100.0}},
      year=2025,
  )


def _finalize_sample_fir_dataset() -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
  """Returns sample finalized FIR annual, temporal, and country summary rows."""
  fir_grouped_rows = [
      (
          ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 10),
          (8, 4, 800.0, 0.0, 0.0),
      ),
      (
          ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 11),
          (2, 1, 200.0, 0.0, 0.0),
      ),
      (
          ("FIR LANGEN", "EDGG", "DE", "domestic", 202501, 10),
          (20, 10, 2500.0, 0.0, 0.0),
      ),
  ]
  annual_rows, temporal_rows, by_country_summary = (
      metric_derivations.finalize_fir_dataset(
          fir_grouped_rows=fir_grouped_rows,
          baseline_stats={
              "BE": {"population": 11500000.0, "ghg_mtco2e": 100.0},
              "DE": {"population": 83000000.0, "ghg_mtco2e": 700.0},
          },
          fir_areas={
              ("FIR BRUXELLES", "EBBU"): 30528.0,
              ("FIR LANGEN", "EDGG"): 100000.0,
          },
          year=2025,
      )
  )
  return (
      {row["fir_icao"]: row for row in annual_rows},
      {row["fir_icao"]: row for row in temporal_rows},
      by_country_summary,
  )


def _finalize_deduplicated_fir_dataset() -> (
    tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]
):
  """Returns finalized FIR datasets when duplicate fir_id rows are merged."""
  fir_grouped_rows = [
      (
          ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 10),
          (8, 4, 800.0, 100.0, 50.0),
      ),
      (
          ("FIR BRUXELLES", "EBBU", "BE,FR", "international", 202501, 11),
          (2, 1, 200.0, 20.0, 10.0),
      ),
  ]
  return metric_derivations.finalize_fir_dataset(
      fir_grouped_rows=fir_grouped_rows,
      baseline_stats={
          "BE": {"population": 11500000.0, "ghg_mtco2e": 100.0},
          "FR": {"population": 67000000.0, "ghg_mtco2e": 400.0},
      },
      fir_areas={("FIR BRUXELLES", "EBBU"): 30528.0},
      year=2025,
  )


_PARTIAL_YEAR_COUNTRY_ROWS = tuple(
    [
        (
            ("Belgium", "BE", 202500 + m, 12),
            (10, 5, 1000.0, 400.0, 800.0, 200.0, 0.0, 0.0),
        )
        for m in (1, 2, 3)
    ]
    + [(
        ("Germany", "DE", 202501, 12),
        (10, 5, 600.0, 200.0, 400.0, 100.0, 0.0, 0.0),
    )]
)

_PARTIAL_YEAR_FIR_ROWS = tuple(
    [
        (
            ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202500 + m, 12),
            (10, 5, 1000.0, 0.0, 0.0),
        )
        for m in (1, 2, 3)
    ]
    + [(
        ("FIR LANGEN", "EDGG", "DE", "domestic", 202501, 12),
        (10, 5, 600.0, 0.0, 0.0),
    )]
)


def _finalize_partial_year_mean_rf_datasets() -> (
    tuple[dict[str, Any], dict[str, Any], dict[str, Any]]
):
  """Returns finalized FIR and country rows for a 3-month partial year."""
  baseline = {
      "BE": {"population": 11000000.0, "ghg_mtco2e": 100.0},
      "DE": {"population": 83000000.0, "ghg_mtco2e": 700.0},
  }
  fir_areas = {
      ("FIR BRUXELLES", "EBBU"): 30000.0,
      ("FIR LANGEN", "EDGG"): 300000.0,
  }
  fir_annual, _, fir_summary = metric_derivations.finalize_fir_dataset(
      list(_PARTIAL_YEAR_FIR_ROWS), baseline, fir_areas, year=2025
  )
  country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
      country_grouped_rows=list(_PARTIAL_YEAR_COUNTRY_ROWS),
      fir_summary_by_iso=fir_summary,
      country_areas={"BE": 30000.0, "DE": 300000.0},
      baseline_stats=baseline,
      year=2025,
  )
  return (
      {row["fir_icao"]: row for row in fir_annual},
      fir_summary,
      {row["iso_a2"]: row for row in country_rows},
  )


def _finalize_negative_and_missing_rank_datasets() -> (
    tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]
):
  """Returns FIR and country rows with warming, cooling, and missing stats."""
  fir_grouped_rows = [
      (
          ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 12),
          (10, 5, 1000.0, 0.0, 0.0),
      ),
      (
          ("FIR REYKJAVIK", "BIRD", "IS", "domestic", 202501, 12),
          (10, 5, -500.0, 0.0, 0.0),
      ),
  ]
  baseline = {
      "BE": {"population": 11500000.0, "ghg_mtco2e": 100.0},
      "IS": {"population": 380000.0, "ghg_mtco2e": 5.0},
  }
  fir_areas = {
      ("FIR BRUXELLES", "EBBU"): 30528.0,
      ("FIR REYKJAVIK", "BIRD"): 500000.0,
  }
  fir_annual, _, fir_summary = metric_derivations.finalize_fir_dataset(
      fir_grouped_rows, baseline, fir_areas, year=2025
  )
  country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
      country_grouped_rows=list(_NEGATIVE_RANK_COUNTRY_ROWS),
      fir_summary_by_iso=fir_summary,
      country_areas={"BE": 30528.0, "IS": 103000.0, "-99|KOSOVO": 11232.0},
      baseline_stats=baseline,
      year=2025,
  )
  return (
      {row["fir_icao"]: row for row in fir_annual},
      {row["name"]: row for row in country_rows},
  )


def _finalize_countries_with_and_without_area() -> dict[str, dict[str, Any]]:
  """Returns finalized country rows for BE, IS (with area), and SG (no area)."""
  country_grouped_rows = [
      (
          ("Belgium", "BE", 202501, 12),
          (10, 5, 1000.0, 400.0, 800.0, 200.0, 0.0, 0.0),
      ),
      (
          ("Iceland", "IS", 202501, 12),
          (10, 5, -500.0, -100.0, -400.0, -150.0, 0.0, 0.0),
      ),
      (("SG", "SG", 202501, 12), (3, 1, 0.0, 0.0, 0.0, 300.0, 0.0, 0.0)),
  ]
  country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
      country_grouped_rows=country_grouped_rows,
      fir_summary_by_iso={},
      country_areas={"BE": 30528.0, "IS": 103000.0},
      baseline_stats={},
      year=2025,
  )
  return {row["iso_a2"]: row for row in country_rows}


def _finalize_firs_with_and_without_area() -> (
    tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]
):
  """Returns finalized FIR rows and country summaries with and without area."""
  fir_grouped_rows = [
      (
          ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 12),
          (10, 5, 1000.0, 0.0, 0.0),
      ),
      (
          ("FIR REYKJAVIK", "BIRD", "IS", "domestic", 202501, 12),
          (10, 5, -500.0, 0.0, 0.0),
      ),
      (
          ("FIR NOAREA", "XXXX", "BE", "international", 202501, 12),
          (10, 5, 200.0, 0.0, 0.0),
      ),
  ]
  fir_areas = {
      ("FIR BRUXELLES", "EBBU"): 30528.0,
      ("FIR REYKJAVIK", "BIRD"): 500000.0,
  }
  fir_annual, _, fir_summary = metric_derivations.finalize_fir_dataset(
      fir_grouped_rows, {}, fir_areas, year=2025
  )
  return {row["fir_icao"]: row for row in fir_annual}, fir_summary


# Two records for the same fir_id with conflicting country lists and types.
_EBBU_FR_ROW = (
    ("FIR BRUXELLES", "EBBU", "FR", "domestic", 202501, 10),
    (8, 4, 800.0, 100.0, 50.0),
)
_EBBU_BE_FR_ROW = (
    ("FIR BRUXELLES", "EBBU", "BE,FR", "international", 202501, 11),
    (2, 1, 200.0, 20.0, 10.0),
)


def _finalize_us_fir_summary() -> Mapping[str, Any]:
  """Returns the US FIR summary for two KZAK oceanic FIRs and KZNY."""
  fir_grouped = [
      (
          (
              "FIR OAKLAND OCEANIC WEST",
              "KZAK",
              "US",
              "international",
              202501,
              0,
          ),
          (1, 1, 10.0, 0.0, 0.0),
      ),
      (
          ("FIR NEW YORK", "KZNY", "US", "domestic", 202501, 0),
          (1, 1, 10.0, 0.0, 0.0),
      ),
      (
          (
              "FIR OAKLAND OCEANIC EAST",
              "KZAK",
              "US",
              "international",
              202501,
              0,
          ),
          (1, 1, 10.0, 0.0, 0.0),
      ),
  ]
  _, _, fir_summary = metric_derivations.finalize_fir_dataset(
      fir_grouped, {}, {}, year=2025
  )
  return fir_summary["US"]


def _finalize_bruxelles_two_month_fir_dataset() -> (
    tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]
):
  """Finalizes two monthly slices of a domestic FIR controlled by Belgium."""
  fir_grouped = [
      (
          ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 0),
          (100, 40, 10.0, 0.0, 0.0),
      ),
      (
          ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202502, 0),
          (100, 40, 10.0, 0.0, 0.0),
      ),
  ]
  return metric_derivations.finalize_fir_dataset(fir_grouped, {}, {}, year=2025)


def _finalize_one_bucket_annual_rows_by_kind() -> (
    dict[str, list[dict[str, Any]]]
):
  """Returns the annual country and FIR rows for a one-bucket input."""
  country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
      country_grouped_rows=[(
          ("Belgium", "BE", 202501, 12),
          (10, 4, 1000.0, 1000.0, 1000.0, 0.0, 0.0, 0.0),
      )],
      fir_summary_by_iso={},
      country_areas={"BE": 30528.0},
      baseline_stats={},
      year=2025,
  )
  fir_rows, _, _ = metric_derivations.finalize_fir_dataset(
      [(
          ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 12),
          (10, 4, 1000.0, 0.0, 0.0),
      )],
      {},
      {("FIR BRUXELLES", "EBBU"): 30000.0},
      year=2025,
  )
  return {"country": country_rows, "fir": fir_rows}


class MetricDerivationsTest(parameterized.TestCase):

  @parameterized.named_parameters(
      dict(
          testcase_name="positive_over_positive",
          numerator=10.0,
          denominator=50.0,
          expected=0.2,
      ),
      dict(
          testcase_name="negative_over_positive",
          numerator=-10.0,
          denominator=50.0,
          expected=-0.2,
      ),
      dict(
          testcase_name="positive_over_negative",
          numerator=10.0,
          denominator=-50.0,
          expected=-0.2,
      ),
      dict(
          testcase_name="negative_over_negative",
          numerator=-10.0,
          denominator=-50.0,
          expected=0.2,
      ),
  )
  def test_ratio_helper_finite_values(self, numerator, denominator, expected):
    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=[
            (
                ("Test", "TC", 202501, 12),
                (
                    10,
                    5,
                    numerator,
                    numerator,
                    denominator,
                    denominator,
                    0.0,
                    0.0,
                ),
            ),
        ],
        fir_summary_by_iso={},
        country_areas={"TC": 1000.0},
        baseline_stats={},
        year=2025,
    )
    row = country_rows[0]
    self.assertAlmostEqual(row["in_country_impact"], expected)
    self.assertAlmostEqual(row["over_region_vs_outgoing_impact"], expected)

  @parameterized.named_parameters(
      dict(
          testcase_name="positive_over_zero",
          numerator=10.0,
          denominator=0.0,
      ),
      dict(
          testcase_name="negative_over_zero",
          numerator=-10.0,
          denominator=0.0,
      ),
      dict(
          testcase_name="nan_denominator",
          numerator=10.0,
          denominator=float("nan"),
      ),
      dict(
          testcase_name="inf_denominator",
          numerator=10.0,
          denominator=float("inf"),
      ),
      dict(
          testcase_name="nan_numerator",
          numerator=float("nan"),
          denominator=50.0,
      ),
  )
  def test_ratio_helper_returns_none_for_zero_or_non_finite(
      self, numerator, denominator
  ):
    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=[
            (
                ("Test", "TC", 202501, 12),
                (
                    10,
                    5,
                    numerator,
                    numerator,
                    denominator,
                    denominator,
                    0.0,
                    0.0,
                ),
            ),
        ],
        fir_summary_by_iso={},
        country_areas={"TC": 1000.0},
        baseline_stats={},
        year=2025,
    )
    row = country_rows[0]
    self.assertIsNone(row["in_country_impact"])
    self.assertIsNone(row["over_region_vs_outgoing_impact"])

  def test_finalize_country_net_cooling_preserves_signed_ratios_and_shares(
      self,
  ):
    is_row = _finalize_net_cooling_and_zero_impact_countries()["IS"]
    self.assertAlmostEqual(is_row["in_country_impact"], 0.25)
    self.assertAlmostEqual(
        is_row["over_region_vs_outgoing_impact"], -500.0 / -150.0
    )
    self.assertAlmostEqual(is_row["co2e_tonnes_share"], 1.0)
    self.assertAlmostEqual(
        is_row["outgoing_flight_co2e_tonnes_share"], 1.0
    )

  def test_finalize_country_zero_denominator_returns_none_ratios_and_zero_share(
      self,
  ):
    zi_row = _finalize_net_cooling_and_zero_impact_countries()["ZI"]
    self.assertIsNone(zi_row["in_country_impact"])
    self.assertIsNone(zi_row["over_region_vs_outgoing_impact"])
    self.assertAlmostEqual(zi_row["co2e_tonnes_share"], 0.0)

  def test_finalize_country_share_metrics_none_when_global_sum_zero(self):
    zero_global_rows = [
        (
            ("CountryA", "CA", 202501, 12),
            (10, 5, 100.0, 50.0, 100.0, 100.0, 0.0, 0.0),
        ),
        (
            ("CountryB", "CB", 202501, 12),
            (10, 5, -100.0, -50.0, -100.0, -100.0, 0.0, 0.0),
        ),
    ]
    rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=zero_global_rows,
        fir_summary_by_iso={},
        country_areas={"CA": 1000.0, "CB": 1000.0},
        baseline_stats={},
        year=2025,
    )
    by_iso = {row["iso_a2"]: row for row in rows}
    self.assertIsNone(by_iso["CA"]["co2e_tonnes_share"])
    self.assertIsNone(by_iso["CA"]["outgoing_flight_co2e_tonnes_share"])
    self.assertIsNone(by_iso["CB"]["co2e_tonnes_share"])
    self.assertIsNone(by_iso["CB"]["outgoing_flight_co2e_tonnes_share"])

  def test_finalize_fir_dataset_net_cooling_share_and_zero_denominator(self):
    fir_grouped_rows = [
        (
            ("FIR REYKJAVIK", "BIRD", "IS", "oceanic", 202501, 10),
            (10, 5, -500.0, 100.0, 50.0),
        ),
        (
            ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 10),
            (10, 5, 1500.0, 200.0, 100.0),
        ),
    ]
    fir_areas = {
        ("FIR REYKJAVIK", "BIRD"): 100000.0,
        ("FIR BRUXELLES", "EBBU"): 30528.0,
    }
    baseline_stats = {
        "IS": {"population": 370000.0, "ghg_mtco2e": 4.5},
        "BE": {"population": 11500000.0, "ghg_mtco2e": 100.0},
    }
    annual_rows, _, summaries = metric_derivations.finalize_fir_dataset(
        fir_grouped_rows=fir_grouped_rows,
        baseline_stats=baseline_stats,
        fir_areas=fir_areas,
        year=2025,
    )
    is_fir = next(
        item_row for item_row in annual_rows if item_row["fir_icao"] == "BIRD"
    )
    self.assertIsNotNone(is_fir["co2e_tonnes_share"])
    self.assertAlmostEqual(is_fir["co2e_tonnes_share"], -0.5)

    is_summary = summaries["IS"]
    self.assertAlmostEqual(is_summary["fir_total_co2e_tonnes_share"], -0.5)
    # BIRD is not domestic, so it counts towards the international scope only.
    self.assertAlmostEqual(is_summary["fir_int_co2e_tonnes_share"], -0.5)
    self.assertAlmostEqual(is_summary["fir_dom_co2e_tonnes_share"], 0.0)

  def test_impact_density_is_published_without_rank(self):
    # Impact density is mean RF times the dataset-wide observation window, so a
    # separate rank would always equal `mean_rf_mw_per_sq_m_rank`.
    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=[(
            ("Belgium", "BE", 202501, 12),
            (10, 5, 1000.0, 400.0, 800.0, 200.0, 0.0, 0.0),
        )],
        fir_summary_by_iso={},
        country_areas={"BE": 30528.0},
        baseline_stats={},
        year=2025,
    )
    fir_rows, _, summaries = metric_derivations.finalize_fir_dataset(
        fir_grouped_rows=[(
            ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 10),
            (10, 5, 1500.0, 200.0, 100.0),
        )],
        baseline_stats={},
        fir_areas={("FIR BRUXELLES", "EBBU"): 30528.0},
        year=2025,
    )
    published_keys = (
        set(country_rows[0]) | set(fir_rows[0]) | set(summaries["BE"])
    )
    density_rank_keys = [
        key
        for key in published_keys
        if "impact_density" in key and key.endswith("_rank")
    ]
    self.assertEmpty(density_rank_keys)
    self.assertIsNotNone(country_rows[0]["impact_density_sq_km"])
    self.assertEqual(country_rows[0]["mean_rf_mw_per_sq_m_rank"], 1)
    self.assertEqual(fir_rows[0]["mean_rf_mw_per_sq_m_rank"], 1)

  def test_shares_are_horizon_free(self):
    # The share of the dataset total is the same for every GWP horizon, so it
    # is published once per scope instead of once per horizon.
    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=[(
            ("Belgium", "BE", 202501, 12),
            (10, 5, 1000.0, 400.0, 800.0, 200.0, 0.0, 0.0),
        )],
        fir_summary_by_iso={},
        country_areas={"BE": 30528.0},
        baseline_stats={},
        year=2025,
    )
    fir_rows, _, summaries = metric_derivations.finalize_fir_dataset(
        fir_grouped_rows=[(
            ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 10),
            (10, 5, 1500.0, 200.0, 100.0),
        )],
        baseline_stats={},
        fir_areas={("FIR BRUXELLES", "EBBU"): 30528.0},
        year=2025,
    )
    published_keys = (
        set(country_rows[0]) | set(fir_rows[0]) | set(summaries["BE"])
    )
    percent_keys = [key for key in published_keys if key.endswith("_percent")]
    self.assertEmpty(percent_keys)
    self.assertContainsSubset(
        {
            "co2e_tonnes_share",
            "outgoing_flight_co2e_tonnes_share",
            "fir_dom_co2e_tonnes_share",
            "fir_int_co2e_tonnes_share",
            "fir_total_co2e_tonnes_share",
        },
        published_keys,
    )
    self.assertAlmostEqual(country_rows[0]["co2e_tonnes_share"], 1.0)
    self.assertAlmostEqual(fir_rows[0]["co2e_tonnes_share"], 1.0)

  def test_finalize_country_and_temporal_datasets_empty(self):
    country_rows, temporal_rows = (
        metric_derivations.finalize_country_and_temporal_datasets(
            country_grouped_rows=[],
            fir_summary_by_iso={},
            country_areas={},
            baseline_stats={},
            year=2025,
        )
    )
    self.assertEmpty(country_rows)
    self.assertEmpty(temporal_rows)

  def test_finalize_country_dataset_aggregates_segment_and_impact_totals(self):
    country_by_iso, _ = _finalize_sample_country_and_temporal()
    self.assertCountEqual(country_by_iso.keys(), ["BE", "DE"])
    be_row = country_by_iso["BE"]
    self.assertEmpty({"total_segment_count", "country"} & be_row.keys())
    self.assertEqual(
        (be_row["segment_count"], be_row["contrail_segment_count"]),
        (15, 7),
    )
    self.assertAlmostEqual(
        be_row["co2e_gwp50_tonnes"], 1500.0 * constants.GWP50_TONNES_PER_MJ
    )
    self.assertEqual(
        (
            be_row["local_flight_impact"],
            be_row["total_flight_impact"],
            be_row["outgoing_flight_impact"],
        ),
        (600.0, 1200.0, 300.0),
    )

  @parameterized.parameters("gwp20", "gwp50", "gwp100")
  def test_finalize_country_dataset_ranks_co2e_across_gwp_horizons(self, gwp):
    country_by_iso, _ = _finalize_sample_country_and_temporal()
    de_row = country_by_iso["DE"]
    be_row = country_by_iso["BE"]
    self.assertEqual(
        (de_row[f"co2e_{gwp}_tonnes_rank"], be_row[f"co2e_{gwp}_tonnes_rank"]),
        (1, 2),
    )
    self.assertEmpty(
        {"prop_sum_ef_mj", "prop_sum_ef_mj_rank"}
        & (de_row.keys() | be_row.keys())
    )

  def test_finalize_country_dataset_does_not_rank_known_route_total_impact(
      self,
  ):
    country_by_iso, _ = _finalize_sample_country_and_temporal()
    de_row = country_by_iso["DE"]
    # `total_flight_impact` covers only segments with a known route, so it is
    # published as a value but not ranked.
    self.assertNotIn("total_impact_rank", de_row)
    self.assertIn("total_flight_impact", de_row)

  def test_finalize_country_dataset_populates_baseline_ghg_and_per_capita(self):
    country_by_iso, _ = _finalize_sample_country_and_temporal()
    be_row = country_by_iso["BE"]
    # The sample covers January and February only, so the annual GHG baseline
    # is scaled to the observed 59 days.
    self.assertAlmostEqual(
        be_row["co2e_gwp50_vs_national_ghg"],
        (1500.0 * constants.GWP50_TONNES_PER_MJ) / (100000000.0 * 59 / 365),
    )
    self.assertAlmostEqual(
        be_row["co2e_gwp50_tonnes_per_capita"],
        (1500.0 * constants.GWP50_TONNES_PER_MJ) / 11500000.0,
    )

  def test_finalize_temporal_dataset_emits_only_country_rows(self):
    _, temporal_by_iso = _finalize_sample_country_and_temporal()
    self.assertCountEqual(temporal_by_iso.keys(), ["BE", "DE"])

  def test_finalize_temporal_dataset_omits_internal_fields(self):
    _, temporal_by_iso = _finalize_sample_country_and_temporal()
    internal_fields = {
        "has_total_segments",
        "total_segment_count",
        "country",
        "prop_sum_ef_mj",
    }
    self.assertEmpty(internal_fields & temporal_by_iso["BE"].keys())

  def test_finalize_temporal_dataset_populates_country_matrices(self):
    _, temporal_by_iso = _finalize_sample_country_and_temporal()
    be_temporal = temporal_by_iso["BE"]
    self.assertIsInstance(be_temporal["segment_count"][0][0], int)
    self.assertIsInstance(be_temporal["contrail_segment_count"][0][0], int)
    self.assertIsInstance(be_temporal["segment_count"][12][0], int)
    self.assertIsInstance(be_temporal["contrail_segment_count"][12][0], int)
    self.assertEqual(
        (
            be_temporal["segment_count"][12][0],
            be_temporal["segment_count"][14][1],
        ),
        (10, 5),
    )
    self.assertAlmostEqual(
        be_temporal["co2e_gwp50_tonnes"][12][0],
        1000.0 * constants.GWP50_TONNES_PER_MJ,
    )
    self.assertAlmostEqual(
        be_temporal["co2e_gwp50_tonnes"][14][1],
        500.0 * constants.GWP50_TONNES_PER_MJ,
    )

  def test_country_without_polygon_resolves_baseline_metadata(self):
    by_iso = _finalize_country_with_and_without_polygon()
    self.assertEqual(by_iso["SG"]["iso_a3"], "SGP")
    self.assertEqual(by_iso["SG"]["name"], "Singapore")
    self.assertEqual(by_iso["BE"]["iso_a3"], "BEL")
    self.assertIsNotNone(by_iso["BE"]["co2e_gwp100_tonnes_per_capita"])

  @parameterized.parameters("gwp20", "gwp50", "gwp100")
  def test_country_without_polygon_has_no_over_region_ratios(self, gwp):
    sg = _finalize_country_with_and_without_polygon()["SG"]
    self.assertIsNone(sg[f"co2e_{gwp}_tonnes_per_capita"])
    self.assertIsNone(sg[f"co2e_{gwp}_vs_national_ghg"])
    self.assertIsNone(sg[f"co2e_{gwp}_per_capita_vs_world_avg"])
    self.assertIsNotNone(sg[f"outgoing_flight_co2e_{gwp}_vs_national_ghg"])

  def test_finalize_fir_flight_distance_km_annual_and_temporal(self):
    datasets = _finalize_flight_distance_datasets()
    ebbu_ann = datasets["fir_annual"]["EBBU"]
    ebbu_temp = datasets["fir_temporal"]["EBBU"]
    self.assertAlmostEqual(ebbu_ann["total_flight_distance_km"], 450.5)
    self.assertAlmostEqual(ebbu_ann["contrail_flight_distance_km"], 220.25)
    self.assertAlmostEqual(ebbu_temp["total_flight_distance_km"][12][0], 450.5)
    self.assertAlmostEqual(
        ebbu_temp["contrail_flight_distance_km"][12][0], 220.25
    )

  def test_finalize_fir_summary_flight_distance_km_by_scope(self):
    be_fir_summary = _finalize_flight_distance_datasets()["fir_summary"]["BE"]
    self.assertEqual(
        (
            be_fir_summary["fir_dom_flight_distance_km"],
            be_fir_summary["fir_dom_contrail_flight_distance_km"],
        ),
        (450.5, 220.25),
    )
    self.assertEqual(
        (
            be_fir_summary["fir_int_flight_distance_km"],
            be_fir_summary["fir_int_contrail_flight_distance_km"],
        ),
        (800.0, 200.0),
    )
    self.assertEqual(
        (
            be_fir_summary["fir_total_flight_distance_km"],
            be_fir_summary["fir_total_contrail_flight_distance_km"],
        ),
        (1250.5, 420.25),
    )

  def test_finalize_country_annual_flight_distance_km(self):
    be_row = _finalize_flight_distance_datasets()["country_rows"]["BE"]
    self.assertAlmostEqual(be_row["total_flight_distance_km"], 600.0)
    self.assertAlmostEqual(be_row["contrail_flight_distance_km"], 300.0)
    self.assertAlmostEqual(be_row["fir_total_flight_distance_km"], 1250.5)
    self.assertAlmostEqual(
        be_row["fir_total_contrail_flight_distance_km"], 420.25
    )

  def test_finalize_country_temporal_flight_distance_km(self):
    be_temp = _finalize_flight_distance_datasets()["temporal_rows"]["BE"]
    self.assertAlmostEqual(be_temp["total_flight_distance_km"][12][0], 450.5)
    self.assertAlmostEqual(
        be_temp["contrail_flight_distance_km"][12][0], 220.25
    )
    self.assertEqual(
        (
            be_temp["total_flight_distance_km"][14][1],
            be_temp["contrail_flight_distance_km"][14][1],
        ),
        (149.5, 79.75),
    )

  def test_finalize_country_annual_collapses_same_iso_name_variants(self):
    country_rows, _ = _finalize_name_variant_and_sentinel_datasets()
    be_annual = [row for row in country_rows if row["iso_a2"] == "BE"]
    self.assertLen(be_annual, 1)
    self.assertEqual(be_annual[0]["name"], "Belgium")
    self.assertEqual(be_annual[0]["segment_count"], 15)
    self.assertAlmostEqual(
        be_annual[0]["co2e_gwp50_tonnes"],
        1500.0 * constants.GWP50_TONNES_PER_MJ,
    )
    self.assertEqual(
        (be_annual[0]["outgoing_flight_impact"], be_annual[0]["area_sq_km"]),
        (300.0, 30528.0),
    )

  def test_finalize_country_temporal_collapses_same_iso_name_variants(self):
    _, temporal_rows = _finalize_name_variant_and_sentinel_datasets()
    be_temp = [row for row in temporal_rows if row["iso_a2"] == "BE"]
    self.assertLen(be_temp, 1)
    self.assertEqual(be_temp[0]["name"], "Belgium")
    self.assertAlmostEqual(
        be_temp[0]["co2e_gwp50_tonnes"][12][0],
        1000.0 * constants.GWP50_TONNES_PER_MJ,
    )
    self.assertAlmostEqual(
        be_temp[0]["co2e_gwp50_tonnes"][14][1],
        500.0 * constants.GWP50_TONNES_PER_MJ,
    )

  def test_finalize_country_preserves_distinct_sentinel_iso_territories(self):
    country_rows, _ = _finalize_name_variant_and_sentinel_datasets()
    neg99_annual = {
        row["name"]: row for row in country_rows if row["iso_a2"] == "-99"
    }
    self.assertEqual(set(neg99_annual.keys()), {"Kosovo", "N. Cyprus"})
    self.assertAlmostEqual(neg99_annual["Kosovo"]["area_sq_km"], 11232.0)
    self.assertAlmostEqual(neg99_annual["N. Cyprus"]["area_sq_km"], 3791.0)

  def test_standard_iso_uses_total_country_area_not_first_feature_area(
      self,
  ):
    country_grouped_rows = [(
        ("France", "FR", 202501, 12),
        (10, 5, 1000.0, 400.0, 800.0, 0.0, 0.0, 0.0),
    )]
    country_areas = {"FR": 640000.0, "FR|FRANCE": 550000.0}
    baseline_stats = {"FR": {"population": 67000000.0, "ghg_mtco2e": 400.0}}
    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=country_grouped_rows,
        fir_summary_by_iso={},
        country_areas=country_areas,
        baseline_stats=baseline_stats,
        year=2025,
    )
    fr_row = next(
        item_row for item_row in country_rows if item_row["iso_a2"] == "FR"
    )
    self.assertAlmostEqual(fr_row["area_sq_km"], 640000.0)
    self.assertIsNotNone(fr_row["impact_density_sq_km"])
    self.assertAlmostEqual(fr_row["impact_density_sq_km"], 1000.0 / 640000.0)

  def test_sentinel_iso_without_matching_composite_key_defaults_to_zero_area(
      self,
  ):
    country_grouped_rows = [(
        ("Unknown", "-99", 202501, 12),
        (10, 5, 1000.0, 400.0, 800.0, 0.0, 0.0, 0.0),
    )]
    country_areas = {"-99": 189450.0}
    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=country_grouped_rows,
        fir_summary_by_iso={},
        country_areas=country_areas,
        baseline_stats={},
        year=2025,
    )
    row = next(
        item_row for item_row in country_rows if item_row["iso_a2"] == "-99"
    )
    self.assertEqual(row["area_sq_km"], 0.0)
    self.assertIsNone(row["impact_density_sq_km"])

  @parameterized.named_parameters(
      dict(testcase_name="flown_over_country", iso="BE"),
      dict(testcase_name="fir_only_country", iso="DE"),
  )
  def test_null_population_baseline_leaves_per_capita_empty(self, iso):
    country_grouped_rows = [(
        ("Belgium", "BE", 202501, 12),
        (10, 5, 1000.0, 400.0, 800.0, 0.0, 0.0, 0.0),
    )]
    baseline_stats = {
        "BE": {"population": None, "ghg_mtco2e": 100.0},
        "DE": {"population": None, "ghg_mtco2e": None},
    }

    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=country_grouped_rows,
        fir_summary_by_iso={"DE": {}},
        country_areas={"BE": 30528.0},
        baseline_stats=baseline_stats,
        year=2025,
    )

    by_iso = {item_row["iso_a2"]: item_row for item_row in country_rows}
    self.assertCountEqual(by_iso, ["BE", "DE"])
    self.assertEqual(by_iso[iso]["population"], 0.0)
    self.assertIsNone(by_iso[iso]["co2e_gwp100_tonnes_per_capita"])

  def test_finalize_fir_dataset_requires_year(self):
    rows = [(
        ("FIR BRUXELLES", "EBBU", "BE", "domestic", 203101, 0),
        (1, 1, 1.0, 0.0, 0.0),
    )]
    # Bypass static type checking to verify runtime keyword-only `year` error.
    finalize_fir: Any = metric_derivations.finalize_fir_dataset
    with self.assertRaisesRegex(TypeError, "missing .* 'year'"):
      finalize_fir(rows, {}, {})

  def test_finalize_country_and_temporal_datasets_requires_year(self):
    # Bypass static type checking to verify runtime keyword-only `year` error.
    finalize_country: Any = (
        metric_derivations.finalize_country_and_temporal_datasets
    )
    with self.assertRaisesRegex(TypeError, "missing .* 'year'"):
      finalize_country(
          country_grouped_rows=[],
          fir_summary_by_iso={},
          country_areas={},
          baseline_stats={},
      )
    self.assertFalse(hasattr(metric_derivations, "parse_year"))

  def test_finalize_country_and_temporal_datasets_rejects_wrong_length_tuple(
      self,
  ):
    bad_country_rows: Any = [(
        ("Belgium", "BE", 202501, 12),
        (10, 5, 1000.0, 400.0, 800.0, 200.0, 0.0, 0.0, 999.0),
    )]
    with self.assertRaisesRegex(
        ValueError,
        r"(Expected 8 country aggregate values|too many values to unpack"
        r" \(expected 8)",
    ):
      metric_derivations.finalize_country_and_temporal_datasets(
          country_grouped_rows=bad_country_rows,
          fir_summary_by_iso={},
          country_areas={},
          baseline_stats={},
          year=2025,
      )

  def test_finalize_fir_dataset_rejects_wrong_length_tuple(self):
    bad_fir_rows: Any = [(
        ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 12),
        (10, 5, 1000.0, 0.0, 0.0, 999.0),
    )]
    with self.assertRaisesRegex(
        ValueError,
        r"(Expected 5 FIR aggregate values|too many values to unpack"
        r" \(expected 5)",
    ):
      metric_derivations.finalize_fir_dataset(
          fir_grouped_rows=bad_fir_rows,
          baseline_stats={},
          fir_areas={},
          year=2025,
      )

  def test_finalize_country_and_temporal_datasets_uses_given_year(self):
    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=[(
            ("Belgium", "BE", 204802, 12),
            (1, 1, 100.0, 50.0, 100.0, 10.0, 0.0, 0.0),
        )],
        fir_summary_by_iso={},
        country_areas={"BE": 30528.0},
        baseline_stats={},
        year=2048,
    )
    self.assertLen(country_rows, 1)
    self.assertEqual(country_rows[0]["segment_count"], 1)
    dropped, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=[(
            ("Belgium", "BE", 204802, 12),
            (1, 1, 100.0, 50.0, 100.0, 10.0, 0.0, 0.0),
        )],
        fir_summary_by_iso={},
        country_areas={"BE": 30528.0},
        baseline_stats={},
        year=2047,
    )
    self.assertEmpty(dropped)

  def test_finalize_fir_dataset_uses_given_year(self):
    fir_rows = [(
        ("FIR BRUXELLES", "EBBU", "BE", "domestic", 204802, 0),
        (1, 1, 1.0, 0.0, 0.0),
    )]
    kept, _, _ = metric_derivations.finalize_fir_dataset(
        fir_rows, {}, {("FIR BRUXELLES", "EBBU"): 1.0}, year=2048
    )
    self.assertLen(kept, 1)
    gone, _, _ = metric_derivations.finalize_fir_dataset(
        fir_rows, {}, {("FIR BRUXELLES", "EBBU"): 1.0}, year=2047
    )
    self.assertEmpty(gone)

  def test_finalize_fir_dataset_aggregates_annual_metrics(self):
    annual_by_icao, _, _ = _finalize_sample_fir_dataset()
    self.assertCountEqual(annual_by_icao.keys(), ["EBBU", "EDGG"])
    ebbu = annual_by_icao["EBBU"]
    self.assertEqual(
        (
            ebbu["fir_id"],
            ebbu["firname"],
            ebbu["segment_count"],
            ebbu["contrail_segment_count"],
        ),
        ("EBBU_BRUXELLES", "BRUXELLES", 10, 5),
    )
    self.assertNotIn("total_segment_count", ebbu)
    self.assertAlmostEqual(
        ebbu["co2e_gwp50_tonnes"], 1000.0 * constants.GWP50_TONNES_PER_MJ
    )

  @parameterized.parameters("gwp20", "gwp50", "gwp100")
  def test_finalize_fir_dataset_ranks_co2e_by_gwp_horizon(self, gwp):
    annual_by_icao, _, _ = _finalize_sample_fir_dataset()
    edgg = annual_by_icao["EDGG"]
    ebbu = annual_by_icao["EBBU"]
    self.assertEqual(
        (edgg["fir_id"], edgg["firname"]),
        ("EDGG_LANGEN", "LANGEN"),
    )
    self.assertEqual(
        (edgg[f"co2e_{gwp}_tonnes_rank"], ebbu[f"co2e_{gwp}_tonnes_rank"]),
        (1, 2),
    )
    self.assertEmpty(
        {"prop_sum_ef_mj", "prop_sum_ef_mj_rank"} & (edgg.keys() | ebbu.keys())
    )

  def test_finalize_fir_dataset_populates_temporal_matrices(self):
    _, temporal_by_icao, _ = _finalize_sample_fir_dataset()
    self.assertCountEqual(temporal_by_icao.keys(), ["EBBU", "EDGG"])
    ebbu_temp = temporal_by_icao["EBBU"]
    self.assertEqual(
        (ebbu_temp["fir_id"], ebbu_temp["firname"]),
        ("EBBU_BRUXELLES", "BRUXELLES"),
    )
    self.assertNotIn("prop_sum_ef_mj", ebbu_temp)
    self.assertAlmostEqual(
        ebbu_temp["co2e_gwp50_tonnes"][10][0],
        800.0 * constants.GWP50_TONNES_PER_MJ,
    )
    self.assertAlmostEqual(
        ebbu_temp["co2e_gwp50_tonnes"][11][0],
        200.0 * constants.GWP50_TONNES_PER_MJ,
    )

  def test_finalize_fir_dataset_publishes_country_lists(self):
    annual_by_icao, temporal_by_icao, _ = _finalize_sample_fir_dataset()
    self.assertEqual(annual_by_icao["EBBU"]["fir_to_country_iso2s"], ["BE"])
    self.assertEqual(temporal_by_icao["EBBU"]["fir_to_country_iso2s"], ["BE"])

  def test_finalize_fir_dataset_builds_country_summary(self):
    _, _, by_country_summary = _finalize_sample_fir_dataset()
    self.assertCountEqual(by_country_summary.keys(), ["BE", "DE"])
    be_summary = by_country_summary["BE"]
    self.assertEqual(
        (
            be_summary["fir_dom_fir_id"],
            be_summary["fir_dom_firname"],
            be_summary["fir_total_fir_id"],
            be_summary["fir_total_firname"],
            be_summary["fir_int_fir_id"],
        ),
        (
            ["EBBU_BRUXELLES"],
            ["BRUXELLES"],
            ["EBBU_BRUXELLES"],
            ["BRUXELLES"],
            [],
        ),
    )
    self.assertIsNotNone(be_summary["fir_total_co2e_gwp50_vs_national_ghg"])
    self.assertIsNotNone(be_summary["fir_total_co2e_gwp50_tonnes_per_capita"])
    self.assertEmpty(
        {
            "fir_total_prop_sum_ef_mj",
            "fir_dom_prop_sum_ef_mj",
            "fir_int_prop_sum_ef_mj",
        }
        & be_summary.keys()
    )

  def test_fir_multi_month_area_accumulation(self):
    fir_grouped_multi_month = [
        (
            ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 0),
            (10, 8, 10000.0, 0.0, 0.0),
        ),
        (
            ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202502, 0),
            (15, 12, 15000.0, 0.0, 0.0),
        ),
    ]
    fir_areas = {("FIR BRUXELLES", "EBBU"): 30000.0}
    baseline_stats = {"BE": {"population": 11000000.0, "ghg_mtco2e": 100.0}}
    (
        fir_annual_rows,
        unused_temporal,
        fir_summary,
    ) = metric_derivations.finalize_fir_dataset(
        fir_grouped_multi_month, baseline_stats, fir_areas, year=2025
    )
    self.assertEqual(fir_summary["BE"]["fir_total_area_sq_km"], 30000.0)
    self.assertLen(fir_annual_rows, 1)

  def test_finalize_fir_dataset_deduplicates_annual_row_and_merges_metadata(
      self,
  ):
    annual_rows, _, _ = _finalize_deduplicated_fir_dataset()
    self.assertLen(annual_rows, 1)
    row = annual_rows[0]
    self.assertEqual(
        (
            row["fir_id"],
            row["segment_count"],
            row["contrail_segment_count"],
            row["total_flight_distance_km"],
            row["contrail_flight_distance_km"],
        ),
        ("EBBU_BRUXELLES", 10, 5, 120.0, 60.0),
    )
    self.assertNotIn("total_segment_count", row)
    self.assertAlmostEqual(
        row["co2e_gwp50_tonnes"], 1000.0 * constants.GWP50_TONNES_PER_MJ
    )
    self.assertEqual(
        (row["fir_to_country_iso2s"], row["type"]),
        (["BE", "FR"], "international"),
    )

  def test_finalize_fir_dataset_deduplicates_temporal_row(self):
    _, temporal_rows, _ = _finalize_deduplicated_fir_dataset()
    self.assertLen(temporal_rows, 1)
    temp = temporal_rows[0]
    self.assertEqual(
        (temp["fir_id"], temp["fir_to_country_iso2s"], temp["type"]),
        ("EBBU_BRUXELLES", ["BE", "FR"], "international"),
    )
    self.assertAlmostEqual(
        temp["co2e_gwp50_tonnes"][10][0],
        800.0 * constants.GWP50_TONNES_PER_MJ,
    )
    self.assertAlmostEqual(
        temp["co2e_gwp50_tonnes"][11][0],
        200.0 * constants.GWP50_TONNES_PER_MJ,
    )

  def test_finalize_fir_dataset_deduplicated_fir_counts_area_once_per_country(
      self,
  ):
    _, _, by_country_summary = _finalize_deduplicated_fir_dataset()
    self.assertCountEqual(by_country_summary.keys(), ["BE", "FR"])
    self.assertAlmostEqual(
        by_country_summary["BE"]["fir_total_area_sq_km"], 30528.0
    )
    self.assertAlmostEqual(
        by_country_summary["BE"]["fir_total_co2e_gwp50_tonnes"],
        1000.0 * constants.GWP50_TONNES_PER_MJ,
    )
    self.assertAlmostEqual(
        by_country_summary["FR"]["fir_total_area_sq_km"], 30528.0
    )
    self.assertAlmostEqual(
        by_country_summary["FR"]["fir_total_co2e_gwp50_tonnes"],
        1000.0 * constants.GWP50_TONNES_PER_MJ,
    )

  @parameterized.named_parameters(
      dict(
          testcase_name="fr_first",
          fir_grouped_rows=[_EBBU_FR_ROW, _EBBU_BE_FR_ROW],
      ),
      dict(
          testcase_name="be_fr_first",
          fir_grouped_rows=[_EBBU_BE_FR_ROW, _EBBU_FR_ROW],
      ),
  )
  def test_finalize_fir_dataset_conflict_merge_is_order_independent(
      self, fir_grouped_rows
  ):
    # Conflicting country lists for one fir_id define no primary country, so
    # the merged list is sorted and does not depend on input row order.
    annual_rows, temporal_rows, _ = metric_derivations.finalize_fir_dataset(
        fir_grouped_rows=fir_grouped_rows,
        baseline_stats={},
        fir_areas={("FIR BRUXELLES", "EBBU"): 30528.0},
        year=2025,
    )
    self.assertEqual(annual_rows[0]["fir_to_country_iso2s"], ["BE", "FR"])
    self.assertEqual(temporal_rows[0]["fir_to_country_iso2s"], ["BE", "FR"])
    self.assertEqual(annual_rows[0]["type"], "international")

  @parameterized.parameters("gwp20", "gwp50", "gwp100")
  def test_fir_summary_emits_every_gwp_horizon(self, horizon):
    fir_grouped = [
        (
            ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 0),
            (10, 8, 10000.0, 0.0, 0.0),
        ),
        (
            ("FIR AMSTERDAM", "EHAA", "BE", "international", 202501, 0),
            (5, 4, 4000.0, 0.0, 0.0),
        ),
    ]
    fir_areas = {
        ("FIR BRUXELLES", "EBBU"): 30000.0,
        ("FIR AMSTERDAM", "EHAA"): 20000.0,
    }
    baseline_stats = {"BE": {"population": 11000000.0, "ghg_mtco2e": 100.0}}
    _, _, fir_summary = metric_derivations.finalize_fir_dataset(
        fir_grouped, baseline_stats, fir_areas, year=2025
    )
    summary = fir_summary["BE"]
    self.assertIn(f"fir_total_co2e_{horizon}_tonnes", summary)
    self.assertIn(f"fir_dom_co2e_{horizon}_tonnes", summary)
    self.assertIn(f"fir_int_co2e_{horizon}_tonnes", summary)

  def test_fir_multi_national_veto_power_double_accounting(self):
    fir_grouped = [
        (
            ("FIR DAKAR", "GOOO", "SN, ML", "domestic", 202501, 0),
            (100, 50, 50000.0, 0.0, 0.0),
        ),
    ]
    fir_areas = {("FIR DAKAR", "GOOO"): 150000.0}
    baseline_stats = {
        "SN": {"population": 17000000.0, "ghg_mtco2e": 50.0},
        "ML": {"population": 22000000.0, "ghg_mtco2e": 40.0},
    }
    fir_annual_rows, _, fir_summary = metric_derivations.finalize_fir_dataset(
        fir_grouped, baseline_stats, fir_areas, year=2025
    )
    self.assertLen(fir_annual_rows, 1)
    self.assertAlmostEqual(
        fir_annual_rows[0]["co2e_gwp50_tonnes"],
        50000.0 * constants.GWP50_TONNES_PER_MJ,
    )
    self.assertEqual(
        (
            fir_annual_rows[0]["area_sq_km"],
            fir_summary["SN"]["fir_total_area_sq_km"],
            fir_summary["ML"]["fir_total_area_sq_km"],
        ),
        (150000.0, 150000.0, 150000.0),
    )
    self.assertAlmostEqual(
        fir_summary["SN"]["fir_total_co2e_gwp50_tonnes"],
        50000.0 * constants.GWP50_TONNES_PER_MJ,
    )
    self.assertAlmostEqual(
        fir_summary["ML"]["fir_total_co2e_gwp50_tonnes"],
        50000.0 * constants.GWP50_TONNES_PER_MJ,
    )

  def test_fir_controlling_countries_are_published_as_a_list(self):
    # The FIR input carries the controlling countries as one comma-separated
    # string; annual and temporal rows publish them as a list of stripped,
    # upper-cased, unique codes in input order (the first is the primary).
    fir_grouped = [
        (
            ("FIR DAKAR", "GOOO", " sn, ML ,,SN", "domestic", 202501, 0),
            (100, 50, 50000.0, 0.0, 0.0),
        ),
    ]
    fir_areas = {("FIR DAKAR", "GOOO"): 150000.0}
    annual, temporal, fir_summary = metric_derivations.finalize_fir_dataset(
        fir_grouped, {}, fir_areas, year=2025
    )
    self.assertEqual(annual[0]["fir_to_country_iso2s"], ["SN", "ML"])
    self.assertEqual(temporal[0]["fir_to_country_iso2s"], ["SN", "ML"])
    # A code listed twice is attributed once.
    self.assertCountEqual(fir_summary, ["SN", "ML"])
    self.assertAlmostEqual(
        fir_summary["SN"]["fir_total_co2e_gwp50_tonnes"],
        50000.0 * constants.GWP50_TONNES_PER_MJ,
    )

  def test_country_fir_summary_domestic_lists_are_index_aligned(self):
    us = _finalize_us_fir_summary()
    self.assertEqual(us["fir_dom_fir_id"], ["KZNY_NEW_YORK"])
    self.assertEqual(us["fir_dom_firname"], ["NEW YORK"])
    self.assertEqual(us["fir_dom_fir_icao"], ["KZNY"])

  def test_country_fir_summary_international_lists_are_sorted(self):
    us = _finalize_us_fir_summary()
    self.assertEqual(
        us["fir_int_fir_id"],
        ["KZAK_OAKLAND_OCEANIC_EAST", "KZAK_OAKLAND_OCEANIC_WEST"],
    )
    self.assertEqual(
        us["fir_int_firname"], ["OAKLAND OCEANIC EAST", "OAKLAND OCEANIC WEST"]
    )
    # One entry per FIR: a shared ICAO code is not collapsed.
    self.assertEqual(us["fir_int_fir_icao"], ["KZAK", "KZAK"])

  def test_country_fir_summary_total_lists_are_sorted_by_fir_id(self):
    us = _finalize_us_fir_summary()
    self.assertEqual(
        us["fir_total_fir_id"],
        [
            "KZAK_OAKLAND_OCEANIC_EAST",
            "KZAK_OAKLAND_OCEANIC_WEST",
            "KZNY_NEW_YORK",
        ],
    )
    self.assertEqual(
        us["fir_total_firname"],
        ["OAKLAND OCEANIC EAST", "OAKLAND OCEANIC WEST", "NEW YORK"],
    )
    self.assertEqual(us["fir_total_fir_icao"], ["KZAK", "KZAK", "KZNY"])

  @parameterized.parameters("fir_id", "firname", "fir_icao")
  def test_country_fir_summary_scope_without_firs_is_an_empty_list(
      self, field
  ):
    fir_grouped = [
        (
            ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 0),
            (1, 1, 10.0, 0.0, 0.0),
        ),
    ]
    _, _, fir_summary = metric_derivations.finalize_fir_dataset(
        fir_grouped, {}, {}, year=2025
    )
    self.assertEqual(fir_summary["BE"][f"fir_int_{field}"], [])

  @parameterized.parameters(
      ("segment_count", 200),
      ("contrail_segment_count", 80),
  )
  def test_fir_annual_segment_counts_are_ints(self, field, expected):
    annual, _, _ = _finalize_bruxelles_two_month_fir_dataset()
    self.assertIsInstance(annual[0][field], int)
    self.assertEqual(annual[0][field], expected)

  # Belgium controls no international FIR, so its `fir_int_*` counts stay at
  # an int 0.
  @parameterized.parameters(
      ("fir_dom_segment_count", 200),
      ("fir_dom_contrail_segment_count", 80),
      ("fir_int_segment_count", 0),
      ("fir_int_contrail_segment_count", 0),
      ("fir_total_segment_count", 200),
      ("fir_total_contrail_segment_count", 80),
  )
  def test_country_fir_segment_counts_are_ints(self, field, expected):
    _, _, fir_summary = _finalize_bruxelles_two_month_fir_dataset()
    self.assertIsInstance(fir_summary["BE"][field], int)
    self.assertEqual(fir_summary["BE"][field], expected)

  @parameterized.parameters("country", "fir")
  def test_annual_rows_omit_month_placeholder(self, kind):
    # `month` on annual rows was a constant year * 100 + 1 sentinel.
    rows = _finalize_one_bucket_annual_rows_by_kind()[kind]
    self.assertNotEmpty(rows)
    self.assertEmpty([row for row in rows if "month" in row])

  def test_finalize_fir_dataset_filters_out_of_year_aggregate_rows(self):
    fir_grouped_rows = [
        (
            ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202412, 12),
            (99, 99, 9999.0, 0.0, 0.0),
        ),
        (
            ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 12),
            (10, 5, 1000.0, 0.0, 0.0),
        ),
        (
            ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202601, 12),
            (88, 88, 8888.0, 0.0, 0.0),
        ),
    ]
    fir_annual, fir_temporal, fir_summary = (
        metric_derivations.finalize_fir_dataset(
            fir_grouped_rows,
            {"BE": {"population": 11000000.0, "ghg_mtco2e": 100.0}},
            {("FIR BRUXELLES", "EBBU"): 30000.0},
            year=2025,
        )
    )
    self.assertEqual((len(fir_temporal), len(fir_annual)), (1, 1))
    self.assertAlmostEqual(
        fir_temporal[0]["co2e_gwp50_tonnes"][12][0],
        1000.0 * constants.GWP50_TONNES_PER_MJ,
    )
    self.assertAlmostEqual(fir_temporal[0]["co2e_gwp50_tonnes"][12][11], 0.0)
    self.assertAlmostEqual(
        fir_annual[0]["co2e_gwp50_tonnes"],
        1000.0 * constants.GWP50_TONNES_PER_MJ,
    )
    self.assertAlmostEqual(
        fir_summary["BE"]["fir_total_co2e_gwp50_tonnes"],
        1000.0 * constants.GWP50_TONNES_PER_MJ,
    )

  def test_finalize_country_and_temporal_filters_out_of_year_aggregate_rows(
      self,
  ):
    country_grouped_rows = [
        (
            ("Belgium", "BE", 202412, 12),
            (99, 99, 9999.0, 999.0, 999.0, 999.0, 0.0, 0.0),
        ),
        (
            ("Belgium", "BE", 202501, 12),
            (10, 5, 1000.0, 400.0, 800.0, 200.0, 0.0, 0.0),
        ),
        (
            ("Belgium", "BE", 202601, 12),
            (88, 88, 8888.0, 888.0, 888.0, 888.0, 0.0, 0.0),
        ),
    ]
    country_rows, temporal_rows = (
        metric_derivations.finalize_country_and_temporal_datasets(
            country_grouped_rows=country_grouped_rows,
            fir_summary_by_iso={},
            country_areas={"BE": 30528.0},
            baseline_stats={
                "BE": {"population": 11000000.0, "ghg_mtco2e": 100.0}
            },
            year=2025,
        )
    )
    self.assertLen(temporal_rows, 1)
    be_temp, *_ = temporal_rows
    self.assertAlmostEqual(
        be_temp["co2e_gwp50_tonnes"][12][0],
        1000.0 * constants.GWP50_TONNES_PER_MJ,
    )
    self.assertAlmostEqual(be_temp["co2e_gwp50_tonnes"][12][11], 0.0)
    self.assertEqual([row["segment_count"] for row in country_rows], [10])
    self.assertAlmostEqual(
        country_rows[0]["co2e_gwp50_tonnes"],
        1000.0 * constants.GWP50_TONNES_PER_MJ,
    )

  def test_fir_annual_mean_rf_uses_observed_months_and_emits_coverage_and_rank(
      self,
  ):
    expected_days = 31 + 28 + 31
    expected_divisor = expected_days * 86.4
    fir_by_icao, fir_summary, _ = _finalize_partial_year_mean_rf_datasets()
    ebbu = fir_by_icao["EBBU"]
    self.assertEqual((ebbu["months_covered"], ebbu["days_covered"]), (3, 90))
    self.assertAlmostEqual(
        ebbu["mean_rf_mw_per_sq_m"],
        3000.0 / (30000.0 * expected_divisor),
    )
    self.assertEqual(ebbu["mean_rf_mw_per_sq_m_rank"], 1)
    self.assertEqual(
        (
            fir_summary["BE"]["months_covered"],
            fir_summary["BE"]["days_covered"],
        ),
        (3, 90),
    )
    self.assertAlmostEqual(
        fir_summary["BE"]["fir_total_mean_rf_mw_per_sq_m"],
        3000.0 / (30000.0 * expected_divisor),
    )

  def test_country_annual_mean_rf_uses_observed_months_and_emits_rank(self):
    expected_days = 31 + 28 + 31
    expected_divisor = expected_days * 86.4
    _, _, country_by_iso = _finalize_partial_year_mean_rf_datasets()
    be_row = country_by_iso["BE"]
    de_row = country_by_iso["DE"]
    self.assertEqual(be_row["months_covered"], 3)
    self.assertEqual(be_row["days_covered"], expected_days)
    self.assertAlmostEqual(
        be_row["mean_rf_mw_per_sq_m"],
        3000.0 / (30000.0 * expected_divisor),
    )
    self.assertEqual(be_row["mean_rf_mw_per_sq_m_rank"], 1)
    self.assertEqual(de_row["mean_rf_mw_per_sq_m_rank"], 2)

  @parameterized.parameters((202501, 24), (202513, 12), (202501, -1))
  def test_finalize_fir_dataset_rejects_out_of_range_temporal_buckets(
      self, month, local_hour
  ):
    fir_row = (
        ("FIR BRUXELLES", "EBBU", "BE", "domestic", month, local_hour),
        (10, 5, 1000.0, 0.0, 0.0),
    )
    with self.assertRaisesRegex(
        ValueError, "Temporal bucket out of range for 'EBBU_"
    ):
      metric_derivations.finalize_fir_dataset(
          [fir_row],
          {},
          {("FIR BRUXELLES", "EBBU"): 30000.0},
          year=2025,
          observed_months=[1],
      )

  @parameterized.parameters((202501, 24), (202513, 12))
  def test_finalize_country_dataset_rejects_out_of_range_temporal_buckets(
      self, month, local_hour
  ):
    country_row = (
        ("Belgium", "BE", month, local_hour),
        (10, 5, 1000.0, 400.0, 800.0, 200.0, 0.0, 0.0),
    )
    with self.assertRaisesRegex(
        ValueError, "Temporal bucket out of range for 'BE'"
    ):
      metric_derivations.finalize_country_and_temporal_datasets(
          country_grouped_rows=[country_row],
          fir_summary_by_iso={},
          country_areas={"BE": 30528.0},
          baseline_stats={},
          year=2025,
      )

  def test_partial_year_country_ghg_ratios_use_observed_share_of_baselines(
      self,
  ):
    year_fraction = (31 + 28 + 31) / 365
    _, _, country_by_iso = _finalize_partial_year_mean_rf_datasets()
    be_row = country_by_iso["BE"]
    spatial_tonnes = 3000.0 * constants.GWP50_TONNES_PER_MJ
    outgoing_tonnes = 600.0 * constants.GWP50_TONNES_PER_MJ
    self.assertAlmostEqual(
        be_row["co2e_gwp50_vs_national_ghg"],
        spatial_tonnes / (1e8 * year_fraction),
    )
    self.assertAlmostEqual(
        be_row["outgoing_flight_co2e_gwp50_vs_national_ghg"],
        outgoing_tonnes / (1e8 * year_fraction),
    )
    self.assertAlmostEqual(
        be_row["co2e_gwp50_per_capita_vs_world_avg"],
        (spatial_tonnes / 11000000.0)
        / (constants.WORLD_AVG_GHG_PER_CAPITA_TONNES * year_fraction),
    )
    self.assertAlmostEqual(
        be_row["co2e_gwp50_tonnes_per_capita"], spatial_tonnes / 11000000.0
    )

  def test_partial_year_fir_scope_ghg_ratios_use_observed_share_of_baselines(
      self,
  ):
    year_fraction = (31 + 28 + 31) / 365
    _, fir_summary, _ = _finalize_partial_year_mean_rf_datasets()
    be_summary = fir_summary["BE"]
    scope_tonnes = 3000.0 * constants.GWP50_TONNES_PER_MJ
    self.assertAlmostEqual(
        be_summary["fir_total_co2e_gwp50_vs_national_ghg"],
        scope_tonnes / (1e8 * year_fraction),
    )
    self.assertAlmostEqual(
        be_summary["fir_total_co2e_gwp50_per_capita_vs_world_avg"],
        (scope_tonnes / 11000000.0)
        / (constants.WORLD_AVG_GHG_PER_CAPITA_TONNES * year_fraction),
    )

  def test_full_year_ghg_ratios_use_unscaled_annual_baselines(self):
    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=[
            (("Belgium", "BE", 202500 + month, 12), (1, 1, 100.0) + (0.0,) * 5)
            for month in range(1, 13)
        ],
        fir_summary_by_iso={},
        country_areas={"BE": 30000.0},
        baseline_stats={"BE": {"population": 11000000.0, "ghg_mtco2e": 100.0}},
        year=2025,
    )
    (be_row,) = country_rows
    self.assertEqual(be_row["days_covered"], 365)
    self.assertAlmostEqual(
        be_row["co2e_gwp50_vs_national_ghg"],
        1200.0 * constants.GWP50_TONNES_PER_MJ / 1e8,
    )

  def test_leap_year_partial_ghg_ratio_uses_days_of_a_366_day_year(self):
    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=[
            (("Belgium", "BE", 202402, 12), (1, 1, 100.0) + (0.0,) * 5)
        ],
        fir_summary_by_iso={},
        country_areas={"BE": 30000.0},
        baseline_stats={"BE": {"population": 11000000.0, "ghg_mtco2e": 100.0}},
        year=2024,
    )
    (be_row,) = country_rows
    self.assertEqual(be_row["days_covered"], 29)
    self.assertAlmostEqual(
        be_row["co2e_gwp50_vs_national_ghg"],
        100.0 * constants.GWP50_TONNES_PER_MJ / (1e8 * 29 / 366),
    )

  def test_fir_controlling_country_with_zero_domestic_segments_is_included(
      self,
  ):
    baseline = {
        "SN": {"population": 17000000.0, "ghg_mtco2e": 50.0},
        "ML": {"population": 22000000.0, "ghg_mtco2e": 40.0},
    }
    fir_row = (
        ("FIR DAKAR", "GOOO", "SN, ML", "domestic", 202501, 12),
        (100, 50, 50000.0, 0.0, 0.0),
    )
    _, _, fir_summary = metric_derivations.finalize_fir_dataset(
        [fir_row], baseline, {("FIR DAKAR", "GOOO"): 150000.0}, year=2025
    )
    senegal_row = (
        ("Senegal", "SN", 202501, 12),
        (20, 10, 10000.0, 5000.0, 8000.0, 2000.0, 0.0, 0.0),
    )
    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=[senegal_row],
        fir_summary_by_iso=fir_summary,
        country_areas={"SN": 196722.0, "ML": 1240192.0},
        baseline_stats=baseline,
        year=2025,
        country_names={"SN": "Senegal", "ML": "Mali"},
    )
    isos = {row["iso_a2"]: row for row in country_rows}
    self.assertCountEqual(isos.keys(), ["SN", "ML"])
    ml_row = isos["ML"]
    self.assertEqual(
        (ml_row["name"], ml_row["segment_count"], ml_row["co2e_gwp50_tonnes"]),
        ("Mali", 0, 0.0),
    )
    self.assertAlmostEqual(ml_row["area_sq_km"], 1240192.0)
    self.assertAlmostEqual(
        ml_row["fir_total_co2e_gwp50_tonnes"],
        50000.0 * constants.GWP50_TONNES_PER_MJ,
    )
    self.assertIn("fir_total_co2e_gwp100_tonnes_rank", ml_row)

  def test_assign_descending_ranks_handles_none_zero_negative_and_non_finite(
      self,
  ):
    ef_by_iso = {
        "MN": 50.0,
        "NC": -25.5,
        "ZR": 0.0,
        "PH": 100.0,
        "TA": 40.0,
        "TB": 40.0,
        "NA": float("nan"),
        "IN": float("inf"),
    }
    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=[
            ((iso, iso, 202501, 12), (1, 1, ef_mj, 0.0, 0.0, 0.0, 0.0, 0.0))
            for iso, ef_mj in ef_by_iso.items()
        ],
        fir_summary_by_iso={},
        country_areas=dict.fromkeys(ef_by_iso, 100.0),
        baseline_stats={
            iso: {"population": 1000} for iso in ef_by_iso if iso != "MN"
        },
        year=2025,
    )
    by_iso = {
        row["iso_a2"]: row["co2e_gwp50_tonnes_per_capita_rank"]
        for row in country_rows
    }
    expected = {
        "PH": 1,
        "TA": 2,
        "TB": 3,
        "ZR": 4,
        "NC": 5,
        "MN": None,
        "NA": None,
        "IN": None,
    }
    self.assertEqual(by_iso, expected)

  def test_assign_descending_ranks_ignores_boolean_metric_values(self):
    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=[
            (("PH", "PH", 202501, 12), (1, 1, 100.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
        ],
        fir_summary_by_iso={"BL": {"fir_total_co2e_gwp50_tonnes": True}},
        country_areas={"PH": 100.0, "BL": 100.0},
        baseline_stats={"PH": {"population": 1000}},
        year=2025,
    )
    fir_ranks = {
        row["iso_a2"]: row["fir_total_co2e_gwp50_tonnes_rank"]
        for row in country_rows
    }
    self.assertEqual(fir_ranks, {"BL": None, "PH": None})

  def test_finalize_datasets_ranks_finite_absolutes_including_negative(self):
    fir_by_icao, country_by_name = (
        _finalize_negative_and_missing_rank_datasets()
    )
    self.assertEqual(fir_by_icao["EBBU"]["co2e_gwp50_tonnes_rank"], 1)
    self.assertEqual(fir_by_icao["BIRD"]["co2e_gwp50_tonnes_rank"], 2)
    self.assertEqual(country_by_name["Belgium"]["co2e_gwp50_tonnes_rank"], 1)
    self.assertEqual(country_by_name["Kosovo"]["co2e_gwp50_tonnes_rank"], 2)
    self.assertEqual(country_by_name["Iceland"]["co2e_gwp50_tonnes_rank"], 3)

  @parameterized.parameters(
      "co2e_gwp50_tonnes_per_capita_rank",
      "co2e_gwp50_vs_national_ghg_rank",
      "outgoing_flight_co2e_gwp50_tonnes_per_capita_rank",
      "outgoing_flight_co2e_gwp50_vs_national_ghg_rank",
      "fir_total_co2e_gwp50_tonnes_rank",
      "fir_total_mean_rf_mw_per_sq_m_rank",
  )
  def test_finalize_country_ranks_negative_above_missing_none(self, field):
    _, by_name = _finalize_negative_and_missing_rank_datasets()
    self.assertEqual(by_name["Belgium"][field], 1)
    self.assertEqual(by_name["Iceland"][field], 2)
    self.assertIsNone(by_name["Kosovo"][field])

  @parameterized.parameters(itertools.permutations(_TIE_BREAK_COUNTRY_ROWS))
  def test_assign_descending_ranks_breaks_ties_by_id_independent_of_order(
      self, *rows
  ):
    expected = {
        ("BB", "B"): 1,
        ("-99", "Kosovo"): 2,
        ("-99", "Northern Cyprus"): 3,
        ("AA", "A"): 4,
        ("CC", "C"): 5,
    }
    country_rows, _ = metric_derivations.finalize_country_and_temporal_datasets(
        country_grouped_rows=list(rows),
        fir_summary_by_iso={},
        country_areas={},
        baseline_stats={},
        year=2025,
    )
    self.assertEqual(
        {
            (row["iso_a2"], row["name"]): row["co2e_gwp50_tonnes_rank"]
            for row in country_rows
        },
        expected,
    )

  @parameterized.parameters("impact_density_sq_km", "mean_rf_mw_per_sq_m")
  def test_country_without_area_omits_per_area_metric(self, value_field):
    by_iso = _finalize_countries_with_and_without_area()
    self.assertIsNone(by_iso["SG"][value_field])
    self.assertLess(by_iso["IS"][value_field], 0.0)

  def test_country_without_area_is_unranked_by_mean_rf(self):
    # Net cooling still ranks, behind the warming country.
    by_iso = _finalize_countries_with_and_without_area()
    self.assertEqual(
        (
            by_iso["SG"]["mean_rf_mw_per_sq_m_rank"],
            by_iso["BE"]["mean_rf_mw_per_sq_m_rank"],
            by_iso["IS"]["mean_rf_mw_per_sq_m_rank"],
        ),
        (None, 1, 2),
    )

  @parameterized.parameters("gwp20", "gwp50", "gwp100")
  def test_country_without_area_omits_co2e_per_sq_km_but_keeps_absolute_rank(
      self, gwp
  ):
    by_iso = _finalize_countries_with_and_without_area()
    self.assertIsNone(by_iso["SG"][f"co2e_{gwp}_tonnes_sq_km"])
    self.assertIsNotNone(by_iso["BE"][f"co2e_{gwp}_tonnes_sq_km"])
    self.assertIsNotNone(by_iso["SG"]["outgoing_flight_impact_rank"])

  @parameterized.parameters("impact_density_sq_km", "mean_rf_mw_per_sq_m")
  def test_fir_without_area_omits_per_area_metric(self, value_field):
    by_icao, _ = _finalize_firs_with_and_without_area()
    self.assertIsNone(by_icao["XXXX"][value_field])

  def test_fir_without_area_is_unranked_by_mean_rf(self):
    by_icao, _ = _finalize_firs_with_and_without_area()
    self.assertEqual(
        (
            by_icao["XXXX"]["mean_rf_mw_per_sq_m_rank"],
            by_icao["EBBU"]["mean_rf_mw_per_sq_m_rank"],
            by_icao["BIRD"]["mean_rf_mw_per_sq_m_rank"],
        ),
        (None, 1, 2),
    )

  def test_fir_without_area_omits_co2e_per_sq_km_but_ranks_absolute_co2e(self):
    by_icao, fir_summary = _finalize_firs_with_and_without_area()
    self.assertIsNone(by_icao["XXXX"]["co2e_gwp50_tonnes_sq_km"])
    self.assertEqual(by_icao["XXXX"]["co2e_gwp50_tonnes_rank"], 2)
    self.assertAlmostEqual(
        fir_summary["BE"]["fir_total_impact_density_sq_km"], 1200.0 / 30528.0
    )

  @parameterized.parameters("BE", "IS")
  def test_fir_summary_without_international_area_omits_int_per_area_metrics(
      self, iso
  ):
    _, fir_summary = _finalize_firs_with_and_without_area()
    summary = fir_summary[iso]
    self.assertIsNone(summary["fir_int_impact_density_sq_km"])
    self.assertIsNone(summary["fir_int_mean_rf_mw_per_sq_m"])
    self.assertIsNone(summary["fir_int_co2e_gwp50_tonnes_sq_km"])
    self.assertIsNotNone(summary["fir_dom_impact_density_sq_km"])

  @parameterized.parameters(itertools.permutations(_TIE_BREAK_FIR_ROWS))
  def test_fir_ranks_are_deterministic_for_ties(self, *fir_grouped_rows):
    fir_areas = {
        (f"FIR {icao}", icao): 1000.0 for icao in ("AAAA", "BBBB", "CCCC")
    }
    fir_annual, _, _ = metric_derivations.finalize_fir_dataset(
        list(fir_grouped_rows), {}, fir_areas, year=2025
    )
    self.assertEqual(
        {row["fir_id"]: row["mean_rf_mw_per_sq_m_rank"] for row in fir_annual},
        {"AAAA": 1, "BBBB": 2, "CCCC": 3},
    )

  @parameterized.named_parameters(
      dict(testcase_name="non_leap_year_2025", year=2025, expected_days=365),
      dict(testcase_name="leap_year_2024", year=2024, expected_days=366),
  )
  def test_mw_per_sq_m_divisor_for_months_empty_defaults_to_full_year(
      self, year, expected_days
  ):
    divisor, months_covered, days_covered = (
        metric_derivations.mw_per_sq_m_divisor_for_months(year, [])
    )
    self.assertEqual(months_covered, 0)
    self.assertEqual(days_covered, expected_days)
    self.assertAlmostEqual(divisor, expected_days * 86.4)

  @parameterized.named_parameters(
      dict(
          testcase_name="jan_2025",
          year=2025,
          months=[1],
          expected_months=1,
          expected_days=31,
      ),
      dict(
          testcase_name="feb_leap_2024",
          year=2024,
          months=[2],
          expected_months=1,
          expected_days=29,
      ),
      dict(
          testcase_name="feb_non_leap_2025",
          year=2025,
          months=[2],
          expected_months=1,
          expected_days=28,
      ),
      dict(
          testcase_name="jan_and_feb_2025",
          year=2025,
          months=[1, 2],
          expected_months=2,
          expected_days=59,
      ),
  )
  def test_mw_per_sq_m_divisor_for_months_single_and_multiple_months(
      self, year, months, expected_months, expected_days
  ):
    divisor, months_covered, days_covered = (
        metric_derivations.mw_per_sq_m_divisor_for_months(year, months)
    )
    self.assertEqual(months_covered, expected_months)
    self.assertEqual(days_covered, expected_days)
    self.assertAlmostEqual(divisor, expected_days * 86.4)

  def test_mw_per_sq_m_divisor_for_months_yyyymm_and_deduplication(self):
    months_input = [202501, 202501, 202503, 202402, 0, 13]
    div, months_covered, days_covered = (
        metric_derivations.mw_per_sq_m_divisor_for_months(2025, months_input)
    )
    self.assertEqual(months_covered, 2)
    self.assertEqual(days_covered, 62)
    self.assertAlmostEqual(div, 62 * 86.4)

  def test_published_country_and_fir_row_keys_match_shared_contract(self):
    datasets = _finalize_flight_distance_datasets()
    self.assertEqual(
        {frozenset(row) for row in datasets["fir_annual"].values()},
        {constants.PUBLISHED_FIR_ROW_KEYS},
    )
    self.assertEqual(
        {frozenset(row) for row in datasets["country_rows"].values()},
        {constants.PUBLISHED_COUNTRY_ROW_KEYS},
    )


if __name__ == "__main__":
  absltest.main()
