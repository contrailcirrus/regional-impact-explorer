"""Read-only `fs` backend that serves `gs://` paths with the GCS client.

Without a registered backend, `fs` reads remote URIs through Apache Beam, which
the API server does not install. Registering `GcsBackend` lets the server read
published artifacts from GCS with the much lighter storage client:

  fs.register_filesystem_backend(gcs_backend.GcsBackend())

It implements only what the server calls: `open` for reading, `exists` and
`listdir`. The pipeline writes through Beam and never registers it, so writes,
renames, deletes and globs raise `io.UnsupportedOperation`.

A registered backend serves every `fs` call, so local paths (and `file://`
URIs), such as the standalone server's frontend bundle, are read from local
disk. Other URI schemes are rejected.

Reads download the whole object, which suits the server's JSON and gzip
artifacts. Storage errors surface as `OSError`, and a missing object as
`FileNotFoundError`, so `fs` callers catch them as they would local errors.
"""

import builtins
from collections.abc import Iterable, Iterator
import contextlib
import io
import os
import re
import threading
from typing import Any, IO, NoReturn
import urllib.parse

from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions
from google.cloud import storage

_GCS_PREFIX = "gs://"
_FILE_PREFIX = "file://"
_URI_SCHEME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _is_gcs(path: str) -> bool:
  return path[: len(_GCS_PREFIX)].lower() == _GCS_PREFIX


def _local_path(path: str) -> str:
  """Returns the filesystem path for a local path or `file://` URI.

  Args:
    path: Local path or URI.

  Returns:
    The local filesystem path.

  Raises:
    ValueError: If `path` is a URI with a scheme other than `gs` or `file`.
  """
  if path[: len(_FILE_PREFIX)].lower() == _FILE_PREFIX:
    return urllib.parse.unquote(urllib.parse.urlparse(path).path)
  if _URI_SCHEME_PATTERN.match(path):
    raise ValueError(f"Unsupported filesystem scheme in path: {path!r}")
  return path


def _split(path: str) -> tuple[str, str]:
  """Splits `gs://bucket/key` into the bucket name and object key."""
  bucket, _, key = path[len(_GCS_PREFIX) :].partition("/")
  if not bucket:
    raise ValueError(f"GCS path has no bucket: {path!r}")
  return bucket, key


def _dir_prefix(key: str) -> str:
  """Returns the object-name prefix of the "directory" `key`."""
  key = key.rstrip("/")
  return f"{key}/" if key else ""


@contextlib.contextmanager
def _storage_errors(path: str) -> Iterator[None]:
  """Re-raises storage client errors for `path` as `OSError`s."""
  try:
    yield
  except api_exceptions.NotFound as e:
    raise FileNotFoundError(f"No such object: {path}") from e
  except (api_exceptions.GoogleAPIError, auth_exceptions.GoogleAuthError) as e:
    raise OSError(f"GCS request for {path} failed: {e}") from e


def _unsupported(*args: Any, **kwargs: Any) -> NoReturn:
  del args, kwargs  # Unused.
  raise io.UnsupportedOperation("GcsBackend only reads.")


class GcsBackend:
  """Reads `gs://` paths from GCS and every other path from local disk.

  Attributes:
    error_type: `OSError`: every storage failure is re-raised as one.
  """

  error_type: type[BaseException] = OSError

  # Required by `fs.FilesystemBackend`, but never called by the server.
  rename = _unsupported
  remove = _unsupported
  makedirs = _unsupported
  isdir = _unsupported
  glob = _unsupported

  def __init__(self, client: Any = None) -> None:
    """Initializes the backend.

    Args:
      client: A `google.cloud.storage.Client`, or None to create one with
        Application Default Credentials on first use.
    """
    self._client = client
    self._client_lock = threading.Lock()

  def _get_client(self) -> Any:
    with self._client_lock:
      if self._client is None:
        self._client = storage.Client()
      return self._client

  def open(self, path: str, mode: str) -> IO[Any]:
    """Opens `path` for reading, as text (`r`) or bytes (`rb`)."""
    if mode not in ("r", "rb"):
      raise io.UnsupportedOperation(f"GcsBackend only reads, not {mode!r}.")
    if not _is_gcs(path):
      if mode == "rb":
        return builtins.open(_local_path(path), mode)
      return builtins.open(_local_path(path), mode, encoding="utf-8")
    bucket, key = _split(path)
    with _storage_errors(path):
      data = self._get_client().bucket(bucket).blob(key).download_as_bytes()
    raw = io.BytesIO(data)
    return raw if mode == "rb" else io.TextIOWrapper(raw, encoding="utf-8")

  def exists(self, path: str) -> bool:
    """Returns whether `path` is an object or a "directory" holding any."""
    if not _is_gcs(path):
      return os.path.exists(_local_path(path))
    bucket, key = _split(path)
    client = self._get_client()
    with _storage_errors(path):
      is_object_path = key and not key.endswith("/")
      if is_object_path and client.bucket(bucket).blob(key).exists():
        return True
      blobs = client.list_blobs(bucket, prefix=_dir_prefix(key), max_results=1)
      return any(True for _ in blobs)

  def listdir(self, path: str) -> Iterable[str]:
    """Returns the names of the files and subdirectories directly in `path`.

    On GCS, a "directory" with no objects under it lists as empty rather than
    raising.

    Args:
      path: Directory path.

    Returns:
      The entry names, sorted.
    """
    if not _is_gcs(path):
      return os.listdir(_local_path(path))
    bucket, key = _split(path)
    prefix = _dir_prefix(key)
    with _storage_errors(path):
      blobs = self._get_client().list_blobs(
          bucket, prefix=prefix, delimiter="/"
      )
      # `prefixes` (the subdirectories) is filled in as the pages are read.
      names = {blob.name[len(prefix) :] for blob in blobs}
      names.update(sub[len(prefix) :].rstrip("/") for sub in blobs.prefixes)
    names.discard("")  # A zero-byte "directory" placeholder object.
    return sorted(names)
