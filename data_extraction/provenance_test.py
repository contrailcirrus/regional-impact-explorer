"""Tests for provenance: reference input copies and provenance.json."""

import hashlib
import json
import os
import subprocess
from unittest import mock

from absl.testing import absltest

from data_extraction import intermediate_io
from data_extraction import provenance


def _write(path: str, content: bytes) -> str:
  with open(path, "wb") as f:
    f.write(content)
  return path


class ProvenanceTest(absltest.TestCase):

  def setUp(self):
    super().setUp()
    self.src_dir = self.create_tempdir().full_path
    self.run_dir = os.path.join(self.create_tempdir().full_path, "run")

  def test_copy_reference_inputs_copies_bytes_and_hashes(self):
    content = b"ident,iso_country\nEBBR,BE\n" * 1000
    source = _write(os.path.join(self.src_dir, "airports.csv"), content)

    copied = provenance.copy_reference_inputs(
        self.run_dir, {"airports": source}
    )

    ref = copied["airports"]
    self.assertEqual(
        (ref.source, ref.relative_path, ref.size_bytes, ref.sha256),
        (
            source,
            os.path.join("inputs", "airports", "airports.csv"),
            len(content),
            hashlib.sha256(content).hexdigest(),
        ),
    )
    self.assertEqual(ref.path, os.path.join(self.run_dir, ref.relative_path))
    with open(ref.path, "rb") as f:
      self.assertEqual(f.read(), content)

  def test_copy_reference_inputs_skips_empty_paths(self):
    source = _write(os.path.join(self.src_dir, "firs.geojson"), b"{}")

    copied = provenance.copy_reference_inputs(
        self.run_dir, {"firs": source, "country_stats": ""}
    )

    self.assertEqual(list(copied), ["firs"])

  def test_copy_reference_inputs_keeps_same_basenames_apart(self):
    first = os.path.join(self.create_tempdir().full_path, "data.csv")
    second = os.path.join(self.create_tempdir().full_path, "data.csv")
    _write(first, b"first")
    _write(second, b"second")

    copied = provenance.copy_reference_inputs(
        self.run_dir, {"population": first, "country_stats": second}
    )

    with open(copied["population"].path, "rb") as f:
      self.assertEqual(f.read(), b"first")
    with open(copied["country_stats"].path, "rb") as f:
      self.assertEqual(f.read(), b"second")

  def test_copy_reference_inputs_checks_every_source_before_writing(self):
    present = _write(os.path.join(self.src_dir, "firs.geojson"), b"{}")
    missing = os.path.join(self.src_dir, "airports.csv")

    with self.assertRaisesRegex(ValueError, "airports="):
      provenance.copy_reference_inputs(
          self.run_dir, {"firs": present, "airports": missing}
      )
    self.assertFalse(os.path.exists(self.run_dir))

  def test_git_state_is_none_without_git(self):
    with mock.patch.object(
        subprocess, "run", side_effect=FileNotFoundError("git")
    ):
      self.assertIsNone(provenance.git_state())

  def test_git_state_reports_commit_and_dirty_flag(self):
    def fake_run(args, **kwargs):
      del kwargs
      stdout = "abc123\n" if args[1] == "rev-parse" else " M file.py\n"
      return subprocess.CompletedProcess(args, 0, stdout=stdout)

    with mock.patch.object(subprocess, "run", side_effect=fake_run):
      self.assertEqual(
          provenance.git_state(), {"commit": "abc123", "dirty": True}
      )

  def test_write_provenance_round_trips_build_provenance(self):
    source = _write(os.path.join(self.src_dir, "firs.geojson"), b"{}")
    copied = provenance.copy_reference_inputs(self.run_dir, {"firs": source})

    with mock.patch.object(
        provenance, "git_state", return_value={"commit": "abc", "dirty": False}
    ):
      document = provenance.build_provenance(
          stage=provenance.STAGE_REDUCE,
          dataset="example_dataset",
          year=2025,
          run_id="run-a",
          publish_run_id="run-b",
          reference_inputs=copied,
          segments={"path": "/segments/*.parquet", "file_count": 1},
      )
    path = provenance.write_provenance(self.run_dir, document)

    with open(path, "r", encoding="utf-8") as f:
      written = json.load(f)
    self.assertEqual(path, os.path.join(self.run_dir, "provenance.json"))
    self.assertContainsSubset(
        {
            "schema_version": provenance.SCHEMA_VERSION,
            "stage": "reduce",
            "dataset": "example_dataset",
            "year": 2025,
            "run_id": "run-a",
            "publish_run_id": "run-b",
            "git": {"commit": "abc", "dirty": False},
            "segments": {"path": "/segments/*.parquet", "file_count": 1},
        },
        written,
    )
    self.assertEqual(
        written["reference_inputs"]["firs"],
        {
            "source": source,
            "copy": os.path.join("inputs", "firs", "firs.geojson"),
            "sha256": hashlib.sha256(b"{}").hexdigest(),
            "size_bytes": 2,
        },
    )

  def test_intermediate_provenance_round_trip(self):
    root = self.create_tempdir().full_path
    self.assertIsNone(intermediate_io.read_provenance(root, "run-a"))

    intermediate_io.write_provenance({"stage": "reduce"}, root, "run-a")

    self.assertEqual(
        intermediate_io.read_provenance(root, "run-a"), {"stage": "reduce"}
    )


if __name__ == "__main__":
  absltest.main()
