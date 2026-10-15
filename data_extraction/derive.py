"""The `derive` half of the pipeline: aggregates in, published artifacts out.

Everything here is a pure function of the reduce output plus geometry and
baseline metadata. Nothing here reads a flight segment, so nothing here costs
more than seconds regardless of how much data the run scanned.

That property is the entire point of the split. It only survives if this
module stays free of anything that scales with segment count — if you find
yourself wanting a PCollection in here, the thing you want probably belongs in
`beam_stages.py` instead.
"""

from collections.abc import Mapping, Sequence
import os
import types
from typing import Any, Final

from absl import logging

from common import artifact_io
from common import constants
from data_extraction import metric_derivations

KEY_COUNTRY_ROWS = "country_rows"
KEY_FIR_ANNUAL_ROWS = "fir_annual_rows"
KEY_TEMPORAL_ROWS = "temporal_rows"
KEY_FIR_TEMPORAL_ROWS = "fir_temporal_rows"

# Artifacts written inside the per-dataset directory.
_DATASET_ARTIFACTS: Final[Mapping[str, str]] = types.MappingProxyType({
    KEY_COUNTRY_ROWS: constants.COUNTRY_EXPORT_FILENAME,
    KEY_FIR_ANNUAL_ROWS: constants.FIR_EXPORT_FILENAME,
    KEY_FIR_TEMPORAL_ROWS: constants.FIR_TEMPORAL_EXPORT_FILENAME,
    KEY_TEMPORAL_ROWS: constants.COUNTRY_TEMPORAL_EXPORT_FILENAME,
})


def _check_aggregates_nonempty(
    country_items: Sequence[constants.CountryRow],
    fir_items: Sequence[constants.FirRow],
    *,
    year: int,
) -> None:
  """Refuses to derive a dataset from empty reduce output.

  Every attributed segment produces at least one FIR or country record, so
  empty aggregates mean no segment survived the reduce (for example, every
  row fell outside --input_year). Publishing that would replace the live
  dataset with zeros, so fail here; both binaries derive before they flip
  the run pointer.

  Args:
    country_items: Aggregated country records from the reduce stage.
    fir_items: Aggregated FIR records from the reduce stage.
    year: The calendar year being processed, used in the error message.

  Raises:
    ValueError: If both `country_items` and `fir_items` are empty.
  """
  if not country_items and not fir_items:
    raise ValueError(
        "No country or FIR aggregates to derive for year %d: no input segment"
        " survived the reduce. Check that --input_year matches the data and"
        " inspect the segment drop counters." % year
    )


def _collect_observed_months(
    country_items: Sequence[constants.CountryRow],
    fir_items: Sequence[constants.FirRow],
) -> set[int]:
  """Returns the union of months observed across country and FIR aggregates.

  Args:
    country_items: Aggregated country records from the reduce stage.
    fir_items: Aggregated FIR records from the reduce stage.

  Returns:
    The set of month numbers present in either set of aggregate keys.
  """
  observed_months = {int(month) for (_, _, month, _), _ in country_items}
  observed_months.update(int(month) for (_, _, _, _, month, _), _ in fir_items)
  return observed_months


def derive_summary_rows(
    country_items: Sequence[constants.CountryRow],
    fir_items: Sequence[constants.FirRow],
    *,
    country_areas: Mapping[str, float],
    fir_areas: Mapping[tuple[str, str], float] | None,
    baseline_stats: Mapping[str, Any],
    year: int,
    country_names: Mapping[str, str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
  """Turns reduce aggregates into the four published row sets.

  Separated from writing so it can be tested, and compared between two runs,
  without touching a filesystem.

  Args:
    country_items: Aggregated country records from the reduce stage.
    fir_items: Aggregated FIR records from the reduce stage.
    country_areas: Mapping of country ISO2 code to area in km^2.
    fir_areas: Optional mapping of (firname, fir_icao) to area in km^2.
    baseline_stats: Per-country population and GHG baseline statistics.
    year: The calendar year being processed.
    country_names: Optional mapping of country ISO2 code to canonical name.

  Returns:
    A mapping from artifact key to its list of serialized row dicts.

  Raises:
    ValueError: If both `country_items` and `fir_items` are empty.
  """
  _check_aggregates_nonempty(country_items, fir_items, year=year)
  shared_observed_months = _collect_observed_months(country_items, fir_items)

  (
      fir_annual_rows,
      fir_temporal_rows,
      fir_by_country_summary,
  ) = metric_derivations.finalize_fir_dataset(
      fir_items,
      baseline_stats,
      fir_areas,
      year=year,
      observed_months=shared_observed_months,
  )

  country_rows, temporal_rows = (
      metric_derivations.finalize_country_and_temporal_datasets(
          country_items,
          fir_summary_by_iso=fir_by_country_summary,
          country_areas=country_areas,
          baseline_stats=baseline_stats,
          year=year,
          observed_months=shared_observed_months,
          country_names=country_names,
      )
  )

  return {
      KEY_COUNTRY_ROWS: country_rows,
      KEY_TEMPORAL_ROWS: temporal_rows,
      KEY_FIR_ANNUAL_ROWS: fir_annual_rows,
      KEY_FIR_TEMPORAL_ROWS: fir_temporal_rows,
  }


def publish_summary_rows(
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    dataset_dir: str,
) -> list[str]:
  """Writes the derived row sets to their published dataset/run directory.

  Args:
    rows: Mapping from artifact key to rows produced by derive_summary_rows.
    dataset_dir: Target directory for per-dataset artifacts.

  Returns:
    A list of written artifact file paths.
  """
  written: list[str] = []

  for key, filename in _DATASET_ARTIFACTS.items():
    path = os.path.join(dataset_dir, filename)
    artifact_io.write_json_output(rows[key], path)
    written.append(path)

  return written


def derive_and_publish(
    country_items: Sequence[constants.CountryRow],
    fir_items: Sequence[constants.FirRow],
    *,
    country_areas: Mapping[str, float],
    fir_areas: Mapping[tuple[str, str], float] | None,
    baseline_stats: Mapping[str, Any],
    year: int,
    dataset_dir: str,
    country_names: Mapping[str, str] | None = None,
) -> list[str]:
  """Convenience wrapper over derive + publish.

  Args:
    country_items: Aggregated country records from the reduce stage.
    fir_items: Aggregated FIR records from the reduce stage.
    country_areas: Mapping of country ISO2 code to area in km^2.
    fir_areas: Optional mapping of (firname, fir_icao) to area in km^2.
    baseline_stats: Per-country population and GHG baseline statistics.
    year: The calendar year being processed.
    dataset_dir: Target directory for per-dataset artifacts.
    country_names: Optional mapping of country ISO2 code to canonical name.

  Returns:
    A list of all published artifact file paths.

  Raises:
    ValueError: If both `country_items` and `fir_items` are empty.
  """
  rows = derive_summary_rows(
      country_items,
      fir_items,
      country_areas=country_areas,
      fir_areas=fir_areas,
      baseline_stats=baseline_stats,
      year=year,
      country_names=country_names,
  )
  logging.info(
      "Derived %d country rows, %d temporal rows, %d annual FIR rows.",
      len(rows[KEY_COUNTRY_ROWS]),
      len(rows[KEY_TEMPORAL_ROWS]),
      len(rows[KEY_FIR_ANNUAL_ROWS]),
  )
  return publish_summary_rows(rows, dataset_dir=dataset_dir)
