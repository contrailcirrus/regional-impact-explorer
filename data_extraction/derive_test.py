"""Unit tests for derive module."""

import json
import os

from absl import flags
from absl.testing import absltest

from data_extraction import derive


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


class DeriveTest(absltest.TestCase):

  def setUp(self):
    super().setUp()
    self.test_dir = self.create_tempdir().full_path
    self.dataset_dir = os.path.join(self.test_dir, "dataset")
    self.year_dir = os.path.join(self.test_dir, "year")
    os.makedirs(self.dataset_dir, exist_ok=True)
    os.makedirs(self.year_dir, exist_ok=True)

    self.country_items = [
        (
            ("Belgium", "BE", 202501, 12),
            (10, 8, 10000.0, 5000.0, 10000.0, 5000.0, 0.0, 0.0),
        ),
        (
            ("Germany", "DE", 202501, 14),
            (50, 40, 50000.0, 25000.0, 50000.0, 25000.0, 0.0, 0.0),
        ),
    ]
    self.fir_items = [
        (
            ("FIR BRUXELLES", "EBBU", "BE", "domestic", 202501, 12),
            (10, 8, 10000.0, 0.0, 0.0),
        ),
        (
            ("FIR LANGEN", "EDGG", "DE", "international", 202501, 14),
            (50, 40, 50000.0, 0.0, 0.0),
        ),
    ]
    self.country_areas = {"BE": 30000.0, "DE": 350000.0}
    self.fir_areas = {
        ("FIR BRUXELLES", "EBBU"): 30000.0,
        ("FIR LANGEN", "EDGG"): 350000.0,
    }
    self.baseline_stats = {
        "BE": {"population": 11000000.0, "ghg_mtco2e": 100.0},
        "DE": {"population": 84000000.0, "ghg_mtco2e": 700.0},
    }

  def test_derive_summary_rows(self):
    rows = derive.derive_summary_rows(
        self.country_items,
        self.fir_items,
        country_areas=self.country_areas,
        fir_areas=self.fir_areas,
        baseline_stats=self.baseline_stats,
        year=2025,
    )
    self.assertCountEqual(
        rows.keys(),
        [
            "country_rows",
            "temporal_rows",
            "fir_annual_rows",
            "fir_temporal_rows",
        ],
    )
    self.assertLen(rows["country_rows"], 2)
    self.assertLen(rows["fir_annual_rows"], 2)

  def test_publish_summary_rows(self):
    rows = derive.derive_summary_rows(
        self.country_items,
        self.fir_items,
        country_areas=self.country_areas,
        fir_areas=self.fir_areas,
        baseline_stats=self.baseline_stats,
        year=2025,
    )
    written = derive.publish_summary_rows(
        rows,
        dataset_dir=self.dataset_dir,
    )
    self.assertNotEmpty(written)

    # Verify per-dataset artifacts exist
    country_export = os.path.join(
        self.dataset_dir, "country_fir_density_df_export_per_country.json"
    )
    self.assertTrue(os.path.exists(country_export))
    with open(country_export, "r", encoding="utf-8") as f:
      content = json.load(f)
    self.assertLen(content, 2)

  def test_derive_and_publish_convenience_wrapper(self):
    written = derive.derive_and_publish(
        self.country_items,
        self.fir_items,
        country_areas=self.country_areas,
        fir_areas=self.fir_areas,
        baseline_stats=self.baseline_stats,
        year=2025,
        dataset_dir=self.dataset_dir,
    )
    self.assertNotEmpty(written)
    self.assertCountEqual(
        os.listdir(self.dataset_dir), map(os.path.basename, written)
    )

  def test_derive_summary_rows_rejects_empty_aggregates(self):
    with self.assertRaisesRegex(ValueError, "No country or FIR aggregates"):
      derive.derive_summary_rows(
          [],
          [],
          country_areas=self.country_areas,
          fir_areas=self.fir_areas,
          baseline_stats=self.baseline_stats,
          year=2025,
      )

  def test_derive_summary_rows_accepts_fir_only_aggregates(self):
    rows = derive.derive_summary_rows(
        [],
        self.fir_items,
        country_areas=self.country_areas,
        fir_areas=self.fir_areas,
        baseline_stats=self.baseline_stats,
        year=2025,
    )
    self.assertLen(rows["fir_annual_rows"], 2)

  def test_derive_and_publish_writes_nothing_for_empty_aggregates(self):
    with self.assertRaisesRegex(ValueError, "--input_year"):
      derive.derive_and_publish(
          [],
          [],
          country_areas=self.country_areas,
          fir_areas=self.fir_areas,
          baseline_stats=self.baseline_stats,
          year=2025,
          dataset_dir=self.dataset_dir,
      )
    self.assertEmpty(os.listdir(self.dataset_dir))


if __name__ == "__main__":
  absltest.main()
