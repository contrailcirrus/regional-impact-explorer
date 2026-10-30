"""Unit tests for the read-only google-cloud-storage `fs` backend."""

import io
import os

from absl.testing import absltest
from absl.testing import parameterized
from google.api_core import exceptions as api_exceptions

from common import fs
from common import gcs_backend
from common import gcs_testing

_ROOT = "gs://bucket/root"


class GcsBackendTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.client = gcs_testing.FakeClient()
    self.backend = gcs_backend.GcsBackend(client=self.client)
    self.client.put(f"{_ROOT}/2026/ds/latest.json", '{"run_id": "r1"}')
    self.client.put(f"{_ROOT}/2026/ds/runs/r1/grid.bin.gz", b"\x1f\x8b")
    self.client.put(f"{_ROOT}/reference/airports.csv", "icao\n")
    self.client.put(f"{_ROOT}/top.txt", "top")

  def test_reads_text_and_binary(self):
    with self.backend.open(f"{_ROOT}/2026/ds/latest.json", "r") as f:
      self.assertEqual(f.read(), '{"run_id": "r1"}')
    with self.backend.open(f"{_ROOT}/2026/ds/runs/r1/grid.bin.gz", "rb") as f:
      self.assertEqual(f.read(), b"\x1f\x8b")

  def test_missing_object_raises_file_not_found(self):
    with self.assertRaises(FileNotFoundError):
      self.backend.open(f"{_ROOT}/missing.json", "r")

  def test_storage_errors_surface_as_os_errors(self):
    self.client.error = api_exceptions.Forbidden("no access")
    with self.assertRaisesRegex(OSError, "no access"):
      self.backend.listdir(_ROOT)
    with self.assertRaises(OSError):
      self.backend.exists(f"{_ROOT}/top.txt")
    with self.assertRaises(OSError):
      self.backend.open(f"{_ROOT}/top.txt", "r")

  @parameterized.parameters(
      (f"{_ROOT}/top.txt", True),
      (f"{_ROOT}/2026", True),
      (f"{_ROOT}/2026/", True),
      ("gs://bucket", True),
      ("gs://bucket/", True),
      (f"{_ROOT}/2027", False),
      (f"{_ROOT}/top", False),
      ("gs://empty/", False),
  )
  def test_exists(self, path, expected):
    self.assertEqual(self.backend.exists(path), expected)

  @parameterized.parameters(
      (_ROOT, ["2026", "reference", "top.txt"]),
      (f"{_ROOT}/", ["2026", "reference", "top.txt"]),
      (f"{_ROOT}/2026/ds", ["latest.json", "runs"]),
      ("gs://bucket", ["root"]),
      (f"{_ROOT}/2027", []),
  )
  def test_listdir(self, path, expected):
    self.assertEqual(self.backend.listdir(path), expected)

  def test_listdir_ignores_directory_placeholder_objects(self):
    self.client.put(f"{_ROOT}/2026/", b"")
    self.assertEqual(self.backend.listdir(f"{_ROOT}/2026"), ["ds"])

  def test_local_paths_are_read_from_local_disk(self):
    root = self.create_tempdir()
    root.create_file("a.json", content="{}")
    path = os.path.join(root.full_path, "a.json")
    with self.backend.open(f"file://{path}", "r") as f:
      self.assertEqual(f.read(), "{}")
    with self.backend.open(path, "rb") as f:
      self.assertEqual(f.read(), b"{}")
    self.assertTrue(self.backend.exists(path))
    self.assertEqual(self.backend.listdir(root.full_path), ["a.json"])

  def test_other_schemes_are_rejected(self):
    with self.assertRaisesRegex(ValueError, "Unsupported filesystem scheme"):
      self.backend.exists("s3://bucket/key")

  @parameterized.parameters("w", "wb", "a")
  def test_write_modes_are_unsupported(self, mode):
    with self.assertRaises(io.UnsupportedOperation):
      self.backend.open(f"{_ROOT}/new.json", mode)

  def test_writes_through_fs_are_refused(self):
    fs.register_filesystem_backend(self.backend)
    self.addCleanup(fs.register_filesystem_backend, None)
    with self.assertRaises(io.UnsupportedOperation):
      with fs.atomic_open_for_write(f"{_ROOT}/2026/ds/latest.json") as f:
        f.write("{}")
    for operation in (
        lambda: fs.rename(f"{_ROOT}/top.txt", f"{_ROOT}/moved.txt"),
        lambda: fs.remove(f"{_ROOT}/top.txt"),
        lambda: fs.makedirs(f"{_ROOT}/new"),
        lambda: fs.isdir(_ROOT),
        lambda: fs.glob(f"{_ROOT}/*"),
    ):
      with self.assertRaises(io.UnsupportedOperation):
        operation()
    with self.backend.open(f"{_ROOT}/2026/ds/latest.json", "r") as f:
      self.assertEqual(f.read(), '{"run_id": "r1"}')


if __name__ == "__main__":
  absltest.main()
