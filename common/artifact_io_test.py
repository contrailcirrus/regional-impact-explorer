"""Unit tests for artifact_io module."""

import json
import os
from typing import Any
from unittest import mock

from absl import flags
from absl.testing import absltest
from absl.testing import parameterized

from common import artifact_io
from common import constants
from common import fs


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


class ArtifactIOTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.root = self.create_tempdir().full_path

  @parameterized.parameters(
      (2031, "2031"),
      (999, "0999"),
      (0, "0000"),
  )
  def test_year_dir_name_is_always_four_digits(self, year: int, expected: str):
    self.assertEqual(artifact_io.year_dir_name(year), expected)

  @parameterized.parameters(
      (-1,),
      (10000,),
      ("2031",),
      (2031.0,),
      (True,),
      (None,),
  )
  def test_year_dir_name_rejects_non_int_and_out_of_range(self, year: Any):
    with self.assertRaisesRegex(ValueError, "not a four-digit year"):
      artifact_io.year_dir_name(year)

  def test_path_helpers_spell_the_year_with_four_digits(self):
    self.assertEqual(
        artifact_io.dataset_root("/r", 999, "ds"),
        os.path.join("/r", "0999", "ds"),
    )
    self.assertEqual(
        artifact_io.run_output_dir("/r", year=2031, dataset="ds", run_id="run"),
        os.path.join("/r", "2031", "ds", constants.RUNS_DIR, "run"),
    )
    self.assertEqual(
        artifact_io.pointer_path("/r", 2031, "ds"),
        os.path.join("/r", "2031", "ds", constants.POINTER_FILENAME),
    )

  def test_dataset_root_rejects_invalid_year(self):
    invalid_year: Any = "2031"
    with self.assertRaisesRegex(ValueError, "is not a four-digit year"):
      artifact_io.dataset_root("/r", invalid_year, "ds")

  @parameterized.parameters(
      ("run-a",),
      ("run_a",),
      ("2026-01-02T03-04-05Z-unversioned",),
      ("2026-01-02T03-04-05Z-v1.2.3",),
      ("a",),
      ("..a",),
      ("a" * artifact_io.MAX_RUN_ID_LENGTH,),
  )
  def test_validate_run_id_accepts_typical_ids(self, run_id: str):
    self.assertEqual(artifact_io.validate_run_id(run_id), run_id)

  @parameterized.parameters(
      ("",),
      (".",),
      ("..",),
      ("/abs",),
      ("/tmp/x",),
      ("a/b",),
      ("../x",),
      ("..%2f",),
      ("..%2Fetc",),
      ("a\\b",),
      ("a b",),
      ("run\n",),
      ("run:1",),
      ("café",),
      ("２０２５",),  # Full-width digits.
      ("٢٠٢٥",),  # Arabic-Indic digits.
      ("a" * (artifact_io.MAX_RUN_ID_LENGTH + 1),),
  )
  def test_validate_run_id_rejects_unsafe_ids(self, run_id: str):
    with self.assertRaisesRegex(ValueError, "is invalid"):
      artifact_io.validate_run_id(run_id)

  @parameterized.parameters(
      (None,),
      (1,),
      (b"run-a",),
  )
  def test_validate_run_id_rejects_non_strings(self, bad_run_id: Any):
    with self.assertRaisesRegex(ValueError, "is invalid"):
      artifact_io.validate_run_id(bad_run_id)

  def test_validate_run_id_error_names_the_label(self):
    with self.assertRaisesRegex(ValueError, r"--publish_run_id='\.\.'"):
      artifact_io.validate_run_id("..", label="--publish_run_id")

  @parameterized.parameters(
      ("..",),
      ("/tmp/x",),
      ("a/../..",),
  )
  def test_run_output_dir_rejects_ids_that_escape_the_runs_dir(
      self, run_id: str
  ):
    with self.assertRaisesRegex(ValueError, "is invalid"):
      artifact_io.run_output_dir(
          self.root, year=2025, dataset="ds", run_id=run_id
      )

  def test_write_run_pointer_rejects_unsafe_ids_and_writes_nothing(self):
    with self.assertRaisesRegex(ValueError, "is invalid"):
      artifact_io.write_run_pointer(
          self.root, year=2025, dataset="ds", run_id="../x"
      )
    self.assertEmpty(os.listdir(self.root))

  def test_require_new_run_dir_returns_an_absent_dir_without_creating_it(self):
    root = self.create_tempdir().full_path
    run_dir = artifact_io.require_new_run_dir(
        root, year=2025, dataset="ds", run_id="run-a"
    )
    self.assertEqual(
        run_dir,
        artifact_io.run_output_dir(
            root, year=2025, dataset="ds", run_id="run-a"
        ),
    )
    self.assertFalse(os.path.exists(run_dir))

  def test_require_new_run_dir_refuses_an_existing_run(self):
    root = self.create_tempdir().full_path
    run_dir = artifact_io.run_output_dir(
        root, year=2025, dataset="ds", run_id="run-a"
    )
    # Even an empty directory counts: a failed earlier attempt may have left
    # it behind, and its id may already be what `latest.json` names.
    os.makedirs(run_dir)
    with self.assertRaisesRegex(ValueError, "'run-a' already exists"):
      artifact_io.require_new_run_dir(
          root, year=2025, dataset="ds", run_id="run-a"
      )
    self.assertEqual(
        artifact_io.require_new_run_dir(
            root, year=2025, dataset="ds", run_id="run-b"
        ),
        artifact_io.run_output_dir(
            root, year=2025, dataset="ds", run_id="run-b"
        ),
    )

  def test_require_new_run_dir_validates_the_id(self):
    root = self.create_tempdir().full_path
    with self.assertRaisesRegex(ValueError, "is invalid"):
      artifact_io.require_new_run_dir(
          root, year=2025, dataset="ds", run_id=".."
      )

  def test_pointer_round_trip(self):
    path = artifact_io.write_run_pointer(
        self.root,
        year=2025,
        dataset="example_dataset",
        run_id="2026-01-02T03-04-05Z-test",
    )
    self.assertTrue(os.path.exists(path))
    self.assertEqual(
        artifact_io.read_run_pointer(self.root, 2025, "example_dataset"),
        "2026-01-02T03-04-05Z-test",
    )

  def test_write_boundary_geojson(self):
    run_dir = artifact_io.run_output_dir(
        self.root, year=2025, dataset="org", run_id="run-a"
    )
    country = {
        "type": "Feature",
        "properties": {"name": "Kosovo", "iso_a2": "-99"},
        "geometry": {"type": "Point", "coordinates": [21.0, 42.6]},
    }
    fir = {
        "type": "Feature",
        "properties": {"fir_id": "EBBU_BRUXELLES", "fir_icao": "EBBU"},
        "geometry": {"type": "Point", "coordinates": [4.0, 50.0]},
    }
    written = artifact_io.write_boundary_geojson(run_dir, [country], [fir])
    self.assertEqual(
        written,
        [
            os.path.join(run_dir, constants.COUNTRIES_GEOJSON_FILENAME),
            os.path.join(run_dir, constants.FIRS_GEOJSON_FILENAME),
        ],
    )
    with open(written[0], encoding="utf-8") as f:
      raw = f.read()
    # Compact: no indentation, which roughly doubles multi-MB GeoJSON.
    self.assertNotIn("\n", raw)
    self.assertEqual(
        json.loads(raw), {"type": "FeatureCollection", "features": [country]}
    )
    with open(written[1], encoding="utf-8") as f:
      self.assertEqual(json.load(f)["features"], [fir])

  def test_pointer_flip_is_last_write_wins(self):
    artifact_io.write_run_pointer(
        self.root, year=2025, dataset="example_dataset", run_id="run-1"
    )
    artifact_io.write_run_pointer(
        self.root, year=2025, dataset="example_dataset", run_id="run-2"
    )
    self.assertEqual(
        artifact_io.read_run_pointer(self.root, 2025, "example_dataset"),
        "run-2",
    )

  def test_pointer_is_per_dataset(self):
    artifact_io.write_run_pointer(
        self.root, year=2025, dataset="example_dataset", run_id="run-a"
    )
    artifact_io.write_run_pointer(
        self.root, year=2025, dataset="watch_obs", run_id="run-b"
    )
    self.assertEqual(
        artifact_io.read_run_pointer(self.root, 2025, "example_dataset"),
        "run-a",
    )
    self.assertEqual(
        artifact_io.read_run_pointer(self.root, 2025, "watch_obs"), "run-b"
    )

  def test_missing_pointer_is_not_an_error(self):
    self.assertIsNone(
        artifact_io.read_run_pointer(self.root, 2025, "unpublished")
    )

  def test_pointer_with_foreign_schema_version_is_ignored(self):
    path = artifact_io.pointer_path(self.root, 2025, "example_dataset")
    artifact_io.write_json_output(
        {"schema_version": 99, "run_id": "run-from-the-future"}, path
    )
    self.assertIsNone(
        artifact_io.read_run_pointer(self.root, 2025, "example_dataset")
    )

  def test_unreadable_pointer_is_ignored(self):
    path = artifact_io.pointer_path(self.root, 2025, "example_dataset")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
      f.write("{not json")
    self.assertIsNone(
        artifact_io.read_run_pointer(self.root, 2025, "example_dataset")
    )

  def _write_raw_pointer(self, payload: Any) -> None:
    path = artifact_io.pointer_path(self.root, 2025, "example_dataset")
    artifact_io.write_json_output(payload, path)

  @parameterized.named_parameters(
      ("empty_list", []),
      ("non_empty_list", ["run-a"]),
      ("string", "run-a"),
      ("integer", 1),
      ("none", None),
  )
  def test_pointer_that_is_not_an_object_is_rejected(self, payload: Any):
    self._write_raw_pointer(payload)
    with self.assertRaisesRegex(ValueError, "not a JSON object"):
      artifact_io.read_run_pointer(self.root, 2025, "example_dataset")

  @parameterized.named_parameters(
      ("string", "1"),
      ("none", None),
      ("boolean", True),
      ("float", 1.0),
      ("list", [1]),
  )
  def test_pointer_with_non_integer_schema_version_is_rejected(
      self, version: Any
  ):
    self._write_raw_pointer({"schema_version": version, "run_id": "run-a"})
    with self.assertRaisesRegex(ValueError, "non-integer schema_version"):
      artifact_io.read_run_pointer(self.root, 2025, "example_dataset")

  @parameterized.named_parameters(
      (
          "missing_run_id",
          {"schema_version": constants.POINTER_SCHEMA_VERSION},
      ),
      (
          "none_run_id",
          {
              "schema_version": constants.POINTER_SCHEMA_VERSION,
              "run_id": None,
          },
      ),
      (
          "int_run_id",
          {
              "schema_version": constants.POINTER_SCHEMA_VERSION,
              "run_id": 123,
          },
      ),
      (
          "list_run_id",
          {
              "schema_version": constants.POINTER_SCHEMA_VERSION,
              "run_id": ["run-a"],
          },
      ),
  )
  def test_pointer_without_a_string_run_id_is_rejected(
      self, payload: dict[str, Any]
  ):
    self._write_raw_pointer(payload)
    with self.assertRaisesRegex(ValueError, "no string run_id"):
      artifact_io.read_run_pointer(self.root, 2025, "example_dataset")

  @parameterized.parameters(
      ("",),
      (".",),
      ("..",),
      ("/tmp/x",),
      ("../../etc",),
      ("a/b",),
  )
  def test_pointer_with_an_unsafe_run_id_is_rejected(self, run_id: str):
    self._write_raw_pointer({
        "schema_version": constants.POINTER_SCHEMA_VERSION,
        "run_id": run_id,
    })
    with self.assertRaisesRegex(ValueError, "run_id in run pointer"):
      artifact_io.read_run_pointer(self.root, 2025, "example_dataset")
    with self.assertRaisesRegex(ValueError, "is invalid"):
      artifact_io.resolve_dataset_dir(self.root, 2025, "example_dataset")

  def test_resolve_returns_none_without_pointer(self):
    self.assertIsNone(
        artifact_io.resolve_dataset_dir(self.root, 2025, "unpublished")
    )

  def test_resolve_follows_pointer_when_present(self):
    artifact_io.write_run_pointer(
        self.root, year=2025, dataset="example_dataset", run_id="run-a"
    )
    self.assertEqual(
        artifact_io.resolve_dataset_dir(self.root, 2025, "example_dataset"),
        artifact_io.run_output_dir(
            self.root, year=2025, dataset="example_dataset", run_id="run-a"
        ),
    )

  def test_run_dirs_do_not_collide_across_runs(self):
    dir_a = artifact_io.run_output_dir(
        self.root, year=2025, dataset="example_dataset", run_id="run-a"
    )
    dir_b = artifact_io.run_output_dir(
        self.root, year=2025, dataset="example_dataset", run_id="run-b"
    )
    self.assertNotEqual(dir_a, dir_b)
    root = artifact_io.dataset_root(self.root, 2025, "example_dataset")
    self.assertStartsWith(dir_a, root)
    self.assertStartsWith(dir_b, root)

  def test_pointer_sits_outside_every_run_dir(self):
    pointer = artifact_io.pointer_path(self.root, 2025, "example_dataset")
    run = artifact_io.run_output_dir(
        self.root, year=2025, dataset="example_dataset", run_id="run-a"
    )
    self.assertFalse(pointer.startswith(run))

  def test_write_and_read_json_output(self):
    target = os.path.join(self.root, "subdir", "test.json")
    payload = {"hello": "world", "num": 42}
    artifact_io.write_json_output(payload, target)
    self.assertTrue(os.path.exists(target))

  def test_write_json_output_sanitizes_non_finite_floats(self):
    target = os.path.join(self.root, "subdir", "non_finite.json")
    payload = {
        "nan_val": float("nan"),
        "pos_inf": float("inf"),
        "neg_inf": float("-inf"),
        "finite_val": 123.45,
        "nested": [float("nan"), {"inf_in_dict": float("inf"), "ok": -5.0}],
    }
    artifact_io.write_json_output(payload, target)
    with open(target, "r", encoding="utf-8") as f:
      raw = f.read()
    self.assertNotIn("NaN", raw)
    self.assertNotIn("Infinity", raw)

    def _reject_non_standard(token: str):
      raise AssertionError(f"Non-RFC-8259 float token found: {token}")

    parsed = json.loads(raw, parse_constant=_reject_non_standard)
    self.assertEqual(
        parsed,
        {
            "nan_val": 0.0,
            "pos_inf": 0.0,
            "neg_inf": 0.0,
            "finite_val": 123.45,
            "nested": [0.0, {"inf_in_dict": 0.0, "ok": -5.0}],
        },
    )

  def test_write_and_read_binary_output(self):
    target = os.path.join(self.root, "subdir", "test.bin")
    data = b"\x00\x01\x02\x03"
    artifact_io.write_binary_output(data, target)
    self.assertTrue(os.path.exists(target))
    with open(target, "rb") as f:
      self.assertEqual(f.read(), data)

  def _make_failing_write_open(self, orig_open: Any):
    """Returns an open_file wrapper that raises OSError on write modes."""

    def _failing_write_open(path: str, mode: str = "r"):
      handle = orig_open(path, mode)
      if "w" in mode:
        handle.close()
        raise OSError("Simulated worker cancellation during write")
      return handle

    return _failing_write_open

  def test_interrupted_write_preserves_existing_outputs(self):
    bin_target = os.path.join(self.root, "subdir", "grid.bin.gz")
    artifact_io.write_binary_output(b"original-binary", bin_target)

    with mock.patch.object(
        fs,
        "open_file",
        side_effect=self._make_failing_write_open(fs.open_file),
        autospec=True,
        spec_set=True,
    ):
      with self.assertRaisesRegex(
          OSError, "Simulated worker cancellation during write"
      ):
        artifact_io.write_binary_output(b"corrupted", bin_target)

    with open(bin_target, "rb") as f:
      self.assertEqual(f.read(), b"original-binary")
    self.assertEqual(os.listdir(os.path.dirname(bin_target)), ["grid.bin.gz"])


if __name__ == "__main__":
  absltest.main()
