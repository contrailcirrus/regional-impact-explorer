"""Unit tests for filesystem and runtime abstraction (`fs`)."""

from collections.abc import Callable
import functools
import io
import os
import sys
from typing import Any
from unittest import mock

from absl import flags
from absl import logging
from absl.testing import absltest
from absl.testing import parameterized
from apache_beam.io import filesystem as beam_filesystem
from apache_beam.io import filesystems as beam_filesystems

from common import fs

# URIs with a scheme that no filesystem backend handles.
_UNKNOWN_SCHEME_FILE = "unknown://my-bucket/data/file.txt"
_UNKNOWN_SCHEME_DIR = "unknown://my-bucket/data"


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


class _FakeStorageError(Exception):
  pass


def _create_dir_then_raise_exists(path: str) -> None:
  """Simulates a concurrent writer creating `path` first."""
  os.makedirs(path)
  raise FileExistsError(path)


def _fake_backend() -> mock.MagicMock:
  backend = mock.MagicMock(spec=fs.FilesystemBackend)
  backend.error_type = _FakeStorageError
  return backend


def _raise_from_provider(error: BaseException) -> str:
  raise error


def _buggy_provider() -> str:
  raise TypeError("provider bug")


def _atomic_write_noop(uri: str) -> None:
  with fs.atomic_open_for_write(uri):
    pass


class FsTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.test_dir = self.create_tempdir().full_path

  def tearDown(self):
    fs.register_filesystem_backend(None)
    fs.register_version_tag_provider(None)
    fs.register_resource_resolver(None)
    super().tearDown()

  def test_local_directory_operations(self):
    sub_dir = os.path.join(self.test_dir, "nested", "dir")
    fs.makedirs(sub_dir)
    self.assertTrue(fs.exists(sub_dir))
    self.assertTrue(fs.isdir(sub_dir))

  def test_local_file_operations(self):
    sub_dir = os.path.join(self.test_dir, "nested", "dir")
    fs.makedirs(sub_dir)
    file_path = os.path.join(sub_dir, "sample.txt")
    with fs.open_file(file_path, "w") as f:
      f.write("hello contrails")

    self.assertTrue(fs.exists(file_path))
    self.assertFalse(fs.isdir(file_path))
    self.assertEqual(fs.listdir(sub_dir), ["sample.txt"])
    with fs.open_file(file_path, "r") as f:
      self.assertEqual(f.read(), "hello contrails")

  def test_ensure_parent_dir(self):
    target = os.path.join(self.test_dir, "a", "b", "c.txt")
    self.assertFalse(fs.exists(os.path.dirname(target)))
    fs.ensure_parent_dir(target)
    self.assertTrue(fs.exists(os.path.dirname(target)))
    # Calling it again when the directory exists is a no-op.
    fs.ensure_parent_dir(target)
    self.assertTrue(fs.exists(os.path.dirname(target)))
    # No error for bare filename with no directory.
    fs.ensure_parent_dir("bare_file.txt")

  @parameterized.parameters("file://", "FILE://", "File://")
  def test_file_uri_is_local_regardless_of_case(self, prefix):
    file_path = os.path.join(self.test_dir, "uri.txt")
    with open(file_path, "w", encoding="utf-8") as f:
      f.write("local")
    uri = f"{prefix}{file_path}"
    self.assertTrue(fs.exists(uri))
    with fs.open_file(uri, "r") as f:
      self.assertEqual(f.read(), "local")

  def test_ensure_parent_dir_raises_when_directory_cannot_be_created(self):
    target = os.path.join(self.test_dir, "denied", "c.txt")
    with mock.patch.object(
        fs,
        "makedirs",
        side_effect=PermissionError("denied"),
        autospec=True,
        spec_set=True,
    ):
      with self.assertRaisesRegex(PermissionError, "denied"):
        fs.ensure_parent_dir(target)

  def test_ensure_parent_dir_tolerates_a_concurrent_creator(self):
    target = os.path.join(self.test_dir, "raced", "c.txt")
    with mock.patch.object(
        fs,
        "makedirs",
        side_effect=_create_dir_then_raise_exists,
        autospec=True,
        spec_set=True,
    ):
      fs.ensure_parent_dir(target)
    self.assertTrue(fs.isdir(os.path.dirname(target)))

  def test_glob_handles_literal_at_sign_without_shard_mangling(self):
    literal_at_file = os.path.join(self.test_dir, "data@2024.parquet")
    with fs.open_file(literal_at_file, "wb") as f:
      f.write(b"parquet")

    matched = fs.glob(literal_at_file)
    self.assertEqual(matched, [literal_at_file])

    pattern = os.path.join(self.test_dir, "*@*.parquet")
    self.assertEqual(fs.glob(pattern), [literal_at_file])

  def test_file_uri_scheme_local_operations(self):
    file_path = os.path.join(self.test_dir, "file_uri.txt")
    uri = f"file://{file_path}"
    with fs.open_file(uri, "w") as f:
      f.write("content via file uri")

    self.assertTrue(fs.exists(uri))
    self.assertFalse(fs.isdir(uri))

    with fs.open_file(uri, "r") as f:
      self.assertEqual(f.read(), "content via file uri")

    sub_dir = os.path.join(self.test_dir, "file_uri_dir")
    dir_uri = f"file://{sub_dir}"
    fs.makedirs(dir_uri)
    self.assertTrue(fs.isdir(dir_uri))

  @parameterized.named_parameters(
      ("open_read", lambda: fs.open_file(_UNKNOWN_SCHEME_FILE, "r")),
      ("open_write", lambda: fs.open_file(_UNKNOWN_SCHEME_FILE, "w")),
      ("exists", lambda: fs.exists(_UNKNOWN_SCHEME_FILE)),
      ("makedirs", lambda: fs.makedirs(_UNKNOWN_SCHEME_DIR)),
      (
          "rename",
          lambda: fs.rename(
              _UNKNOWN_SCHEME_FILE, "unknown://my-bucket/dst.txt"
          ),
      ),
      ("remove", lambda: fs.remove(_UNKNOWN_SCHEME_FILE)),
      ("isdir", lambda: fs.isdir(_UNKNOWN_SCHEME_DIR)),
      ("listdir", lambda: fs.listdir(_UNKNOWN_SCHEME_DIR)),
      ("glob", lambda: fs.glob("unknown://my-bucket/*.txt")),
      (
          "atomic_open_for_write",
          lambda: _atomic_write_noop(_UNKNOWN_SCHEME_FILE),
      ),
  )
  def test_unknown_uri_scheme_rejected(self, operation: Callable[[], Any]):
    with self.assertRaisesRegex(ValueError, "Unsupported filesystem scheme"):
      operation()
    self.assertFalse(os.path.exists(os.path.join(os.getcwd(), "unknown:")))

  def _make_fake_beam_fs(self) -> mock.MagicMock:
    """Creates a mocked Beam FileSystems facade for remote URI tests."""
    fake_fs = mock.create_autospec(beam_filesystem.FileSystem, instance=True)
    fake_fs.has_dirs.return_value = False
    fake_beam_fs = mock.create_autospec(beam_filesystems.FileSystems)
    fake_beam_fs.get_filesystem.return_value = fake_fs
    fake_beam_fs.exists.return_value = True
    fake_beam_fs.create.side_effect = lambda *args, **kwargs: io.BytesIO()
    fake_beam_fs.open.side_effect = lambda *args, **kwargs: io.BytesIO(
        b"remote bytes"
    )
    match_entry = beam_filesystem.FileMetadata(
        "custom://bucket/dir/item.json", size_in_bytes=0
    )
    fake_beam_fs.match.return_value = [
        beam_filesystem.MatchResult("custom://bucket/dir/*", [match_entry])
    ]
    return fake_beam_fs

  def test_padded_uri_is_not_treated_as_remote(self):
    fake_beam_fs = self._make_fake_beam_fs()
    with mock.patch.object(fs, "_beam_file_systems", fake_beam_fs):
      self.assertFalse(fs.exists("  custom://bucket/dir/item.json"))
    fake_beam_fs.exists.assert_not_called()

  def test_remote_uri_read_and_write_delegates_to_beam_filesystems(self):
    fake_beam_fs = self._make_fake_beam_fs()
    remote_path = "custom://bucket/dir/item.json"
    with mock.patch.object(fs, "_beam_file_systems", fake_beam_fs):
      self.assertTrue(fs.exists(remote_path))
      fake_beam_fs.exists.assert_called_once_with(remote_path)

      with fs.open_file(remote_path, "w") as f:
        f.write("test")
      fake_beam_fs.create.assert_called_once_with(remote_path)

      with fs.open_file(remote_path, "r") as f:
        self.assertEqual(f.read(), "remote bytes")
      fake_beam_fs.open.assert_called_once_with(remote_path)

  def test_remote_uri_mutation_delegates_to_beam_filesystems(self):
    fake_beam_fs = self._make_fake_beam_fs()
    remote_path = "custom://bucket/dir/item.json"
    remote_dir = "custom://bucket/dir"
    with mock.patch.object(fs, "_beam_file_systems", fake_beam_fs):
      fs.makedirs(remote_dir)
      fake_beam_fs.mkdirs.assert_called_once_with(remote_dir)

      fs.rename(remote_path, "custom://bucket/dir/item2.json")
      fake_beam_fs.rename.assert_called_once_with(
          [remote_path], ["custom://bucket/dir/item2.json"]
      )

      fs.remove(remote_path)
      fake_beam_fs.delete.assert_called_once_with([remote_path])

      # atomic_open_for_write writes directly without temp sibling rename.
      with fs.atomic_open_for_write(remote_path, "wb") as f:
        f.write(b"data")
      fake_beam_fs.rename.assert_called_once()

  def test_remote_uri_listing_delegates_to_beam_filesystems(self):
    fake_beam_fs = self._make_fake_beam_fs()
    remote_path = "custom://bucket/dir/item.json"
    remote_dir = "custom://bucket/dir"
    with mock.patch.object(fs, "_beam_file_systems", fake_beam_fs):
      self.assertTrue(fs.isdir(remote_dir))
      self.assertEqual(fs.listdir(remote_dir), ["item.json"])
      self.assertEqual(fs.glob("custom://bucket/dir/*.json"), [remote_path])
      self.assertFalse(os.path.exists(os.path.join(os.getcwd(), "custom:")))

  def test_registered_backend_serves_remote_paths(self):
    fake_backend = _fake_backend()
    fake_backend.exists.return_value = True
    fake_backend.isdir.return_value = True
    fake_backend.listdir.return_value = ["a.json", "b.json"]
    fake_backend.glob.return_value = ["gs://bucket/data/a.json"]
    fs.register_filesystem_backend(fake_backend)

    self.assertTrue(fs.exists("gs://bucket/data"))
    self.assertTrue(fs.isdir("gs://bucket/data"))
    self.assertEqual(fs.listdir("gs://bucket/data"), ["a.json", "b.json"])
    self.assertEqual(
        fs.glob("gs://bucket/data/*.json"), ["gs://bucket/data/a.json"]
    )
    fake_backend.exists.assert_called_once_with("gs://bucket/data")

  def test_registered_backend_serves_remote_makedirs(self):
    fake_backend = _fake_backend()
    fs.register_filesystem_backend(fake_backend)
    fs.makedirs("gs://bucket/out")
    fake_backend.makedirs.assert_called_once_with("gs://bucket/out")

  def test_error_follows_the_registered_backend(self):
    fs.register_filesystem_backend(_fake_backend())
    self.assertIs(fs.Error, _FakeStorageError)

    fs.register_filesystem_backend(None)
    self.assertIs(fs.Error, OSError)

  def test_version_tag_prefers_environment_variable(self):
    fs.register_version_tag_provider(lambda: "git-a1b2c3d")
    with mock.patch.dict(
        os.environ, {"CONTRAILS_PIPELINE_VERSION": "v1.2.3"}, clear=True
    ):
      self.assertEqual(fs.get_version_tag(), "v1.2.3")

  def test_version_tag_uses_registered_provider_when_env_unset(self):
    fs.register_version_tag_provider(lambda: "git-a1b2c3d")
    with mock.patch.dict(os.environ, {}, clear=True):
      self.assertEqual(fs.get_version_tag(), "git-a1b2c3d")

  @parameterized.named_parameters(
      ("os_error", OSError("no build info")),
      ("value_error", ValueError("bad version tag")),
      ("runtime_error", RuntimeError("no stamp")),
  )
  def test_version_tag_falls_back_when_the_provider_fails(
      self, error: BaseException
  ):
    self.enter_context(mock.patch.dict(os.environ, {}, clear=True))
    fs.register_version_tag_provider(
        functools.partial(_raise_from_provider, error)
    )
    self.addCleanup(logging.set_verbosity, logging.get_verbosity())
    logging.set_verbosity(logging.DEBUG)
    with self.assertLogs("absl", level="DEBUG") as logs:
      self.assertEqual(fs.get_version_tag(), "unversioned")
    self.assertIn("Version tag provider failed.", logs.output[0])

  def test_version_tag_propagates_unexpected_provider_errors(self):
    self.enter_context(mock.patch.dict(os.environ, {}, clear=True))
    fs.register_version_tag_provider(_buggy_provider)
    with self.assertRaisesRegex(TypeError, "provider bug"):
      fs.get_version_tag()

  def test_unregistered_backend_is_not_adopted_from_sys_modules(self):
    fake_driver = mock.NonCallableMock(spec_set=["Exists", "Error"])
    with mock.patch.dict(sys.modules, {"fake.storage.driver": fake_driver}):
      self.assertTrue(fs.exists(self.test_dir))
    fake_driver.Exists.assert_not_called()

  def test_resolve_resource_path_finds_package_files(self):
    resolved = fs.resolve_resource_path("missing/first.json", "common/fs.py")
    self.assertEqual(resolved[0], "missing/first.json")
    self.assertTrue(resolved[1].endswith(os.path.join("common", "fs.py")))
    self.assertTrue(os.path.exists(resolved[1]))
    self.assertEqual(resolved[-1], "common/fs.py")

  def test_resolve_resource_path_appends_existing_resolver_paths(self):
    resource = os.path.join(self.test_dir, "resource.geojson")
    with fs.open_file(resource, "w") as f:
      f.write("{}")
    missing = os.path.join(self.test_dir, "missing.geojson")
    requested_paths = []

    def resolver(relative_path: str) -> list[str]:
      requested_paths.append(relative_path)
      return [missing, resource]

    fs.register_resource_resolver(resolver)

    self.assertEqual(
        fs.resolve_resource_path("only/in/resolver.geojson"),
        [resource, "only/in/resolver.geojson"],
    )
    self.assertEqual(requested_paths, ["only/in/resolver.geojson"])

  def test_atomic_open_for_write_creates_and_replaces_file(self):
    file_path = os.path.join(self.test_dir, "atomic.txt")
    with fs.atomic_open_for_write(file_path, "w") as f:
      f.write("first version")
    with fs.open_file(file_path, "r") as f:
      self.assertEqual(f.read(), "first version")

    with fs.atomic_open_for_write(file_path, "w") as f:
      f.write("second version")
    with fs.open_file(file_path, "r") as f:
      self.assertEqual(f.read(), "second version")
    self.assertEqual(fs.listdir(self.test_dir), ["atomic.txt"])

  def test_atomic_open_for_write_preserves_existing_file_on_error(self):
    file_path = os.path.join(self.test_dir, "slice.npz")
    with fs.atomic_open_for_write(file_path, "wb") as f:
      f.write(b"primary-worker-complete-bytes")

    with self.assertRaisesRegex(RuntimeError, "backup worker cancelled"):
      with fs.atomic_open_for_write(file_path, "wb") as f:
        f.write(b"partial")
        raise RuntimeError("backup worker cancelled")

    with fs.open_file(file_path, "rb") as f:
      self.assertEqual(f.read(), b"primary-worker-complete-bytes")
    self.assertEqual(fs.listdir(self.test_dir), ["slice.npz"])

  @parameterized.parameters(
      ("r",),
      ("rb",),
      ("a",),
      ("ab",),
  )
  def test_atomic_open_for_write_rejects_read_modes(self, bad_mode: str):
    file_path = os.path.join(self.test_dir, "bad_mode.txt")
    with self.assertRaisesRegex(
        ValueError, r"atomic_open_for_write only supports 'w' or 'wb'"
    ):
      with fs.atomic_open_for_write(file_path, bad_mode):
        pass

  def test_atomic_open_for_write_uses_backend_rename_on_success(self):
    fake_backend = _fake_backend()
    fs.register_filesystem_backend(fake_backend)

    target = "gs://bucket/slice.npz"
    with fs.atomic_open_for_write(target, "wb") as f:
      f.write(b"payload")

    fake_backend.open.assert_called_once()
    tmp_path, mode = fake_backend.open.call_args[0]
    self.assertEqual(mode, "wb")
    self.assertTrue(os.path.basename(tmp_path).startswith(".slice.npz.tmp."))
    fake_backend.rename.assert_called_once_with(
        tmp_path, target, overwrite=True
    )
    fake_backend.remove.assert_not_called()

  def test_atomic_open_for_write_uses_backend_remove_on_error(self):
    fake_backend = _fake_backend()
    fs.register_filesystem_backend(fake_backend)

    target = "gs://bucket/slice.npz"
    with self.assertRaisesRegex(RuntimeError, "cancelled before flush"):
      with fs.atomic_open_for_write(target, "wb"):
        raise RuntimeError("cancelled before flush")

    failed_tmp_path, _ = fake_backend.open.call_args[0]
    fake_backend.rename.assert_not_called()
    fake_backend.remove.assert_called_once_with(failed_tmp_path)


if __name__ == "__main__":
  absltest.main()
