"""Filesystem and environment abstraction layer.

Provides a standard-library filesystem backend by default for open-source
execution, while allowing launchers to register a custom backend (such as a
remote-storage client) and version-tag provider at process startup.
"""

import builtins
from collections.abc import Callable, Iterable, Iterator
import contextlib
import glob as glob_mod
import importlib
import io
import os
import pathlib
import re
import threading
from typing import Any, IO, Protocol, TypeAlias
import urllib.parse
import uuid

from absl import logging

_UNSET = object()
_beam_file_systems: Any = _UNSET


def _get_beam_file_systems() -> Any:
  """Returns the Beam FileSystems class, or None if apache-beam is missing."""
  global _beam_file_systems
  if _beam_file_systems is _UNSET:
    try:
      _beam_file_systems = importlib.import_module(
          "apache_beam.io.filesystems"
      ).FileSystems
    except ImportError:
      _beam_file_systems = None
  return _beam_file_systems


def _require_beam_file_systems() -> Any:
  """Returns the Beam FileSystems class, raising RuntimeError if absent."""
  bfs = _get_beam_file_systems()
  if bfs is None:
    raise RuntimeError("Apache Beam FileSystems is unavailable.")
  return bfs


class FilesystemBackend(Protocol):
  """Filesystem operations for paths the local disk can't serve.

  Implementations route every `fs` call once registered with
  `register_filesystem_backend`, so they must also handle local paths.

  Attributes:
    error_type: Exception type the backend raises for storage errors. It is
      exposed as `fs.Error` while the backend is registered.
  """

  error_type: type[BaseException]

  def open(self, path: str, mode: str) -> IO[Any]:
    """Opens `path` with the given I/O `mode`."""
    ...

  def rename(self, src: str, dst: str, overwrite: bool) -> None:
    """Atomically renames `src` to `dst`."""
    ...

  def remove(self, path: str) -> None:
    """Removes the file at `path`."""
    ...

  def exists(self, path: str) -> bool:
    """Returns whether `path` exists."""
    ...

  def makedirs(self, path: str) -> None:
    """Recursively creates the directory `path` if absent."""
    ...

  def isdir(self, path: str) -> bool:
    """Returns whether `path` is an existing directory."""
    ...

  def listdir(self, path: str) -> Iterable[str]:
    """Returns the basenames of entries in directory `path`."""
    ...

  def glob(self, pattern: str) -> Iterable[str]:
    """Returns paths matching the glob `pattern`."""
    ...


# Returns extra local paths for a relative resource path; see
# `register_resource_resolver`.
ResourceResolver: TypeAlias = Callable[[str], Iterable[str]]

_backend: FilesystemBackend | None = None
_version_tag_provider: Callable[[], str] | None = None
_resource_resolver: ResourceResolver | None = None

# Exposed as `fs.Error` so callers can write `except (OSError, fs.Error, ...):`.
# Set to the registered backend's `error_type`, or `OSError` without one.
Error: type[BaseException] = OSError

# RFC 3986 URI scheme pattern: begins with a letter, followed by letters,
# digits, '+', '.', or '-'.
_URI_SCHEME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _get_scheme(path: str) -> str | None:
  """Returns the lowercased URI scheme in `path` (e.g. 'gs', 'file'), or None.

  The path is matched as given: surrounding whitespace is not stripped, just
  as Beam's own scheme detection does not strip it, so a padded URI is
  treated as a plain local path by every branch.

  Args:
    path: Local path or URI.

  Returns:
    The scheme without `://`, or None for a plain path.
  """
  match = _URI_SCHEME_PATTERN.match(path)
  if match is None:
    return None
  return match.group(0).removesuffix("://").lower()


def _is_remote_scheme(path: str) -> bool:
  """Returns True if `path` specifies a non-local URI scheme."""
  scheme = _get_scheme(path)
  return scheme is not None and scheme != "file"


def _normalize_local_path(path: str) -> str:
  """Converts 'file://' URIs to filesystem paths, leaving others unchanged.

  The scheme is matched case-insensitively via `_get_scheme`, so every path
  routed to the local filesystem as a `file` URI is also converted here.

  Args:
    path: Local path or URI.

  Returns:
    The local filesystem path for a `file` URI, otherwise `path` unchanged.
  """
  if _get_scheme(path) == "file":
    parsed = urllib.parse.urlparse(path)
    return urllib.parse.unquote(parsed.path)
  return path


def _get_beam_filesystem(path: str) -> Any:
  """Returns the Apache Beam FileSystem for `path`, or raises ValueError."""
  bfs = _get_beam_file_systems()
  if bfs is None:
    raise ValueError(
        f"Unsupported filesystem scheme in path: {path!r}. "
        "apache-beam is required to access remote filesystem schemes."
    )
  try:
    return bfs.get_filesystem(path)
  except ValueError as e:
    raise ValueError(f"Unsupported filesystem scheme in path: {path!r}") from e


def _get_backend() -> FilesystemBackend | None:
  """Returns the registered filesystem backend, or None for the local disk."""
  return _backend


def register_filesystem_backend(backend: FilesystemBackend | None) -> None:
  """Routes all `fs` operations through `backend`, or the local disk if None.

  Args:
    backend: Backend to use, or None to restore the standard library.
  """
  global _backend, Error
  _backend = backend
  Error = backend.error_type if backend is not None else OSError


def register_resource_resolver(resolver: ResourceResolver | None) -> None:
  """Registers a hook that finds resources outside the package directory.

  `resolve_resource_path` always searches the directories containing this
  package. Launchers that ship resources elsewhere, for example inside a
  zipped binary, register a resolver that maps a relative resource path to
  extra local candidate paths. Pass None to remove the resolver.

  Args:
    resolver: Callable returning candidate local paths for a relative path, or
      None.
  """
  global _resource_resolver
  _resource_resolver = resolver


def register_version_tag_provider(
    provider: Callable[[], str] | None,
) -> None:
  """Registers a callable returning the build/version tag (e.g. commit SHA).

  Args:
    provider: Callable returning the build or version tag string, or None to
      clear the registered provider.
  """
  global _version_tag_provider
  _version_tag_provider = provider


def get_version_tag() -> str:
  """Returns the build version tag from the environment or registered provider.

  A provider that fails with `OSError`, `ValueError` or `RuntimeError` is
  logged at debug level and treated as having no tag; other exceptions
  propagate so that bugs in the provider are not hidden.
  """
  env_tag = (
      os.environ.get("CONTRAILS_PIPELINE_VERSION", "").strip()
      or os.environ.get("PIPELINE_VERSION_TAG", "").strip()
  )
  if env_tag:
    return env_tag
  if _version_tag_provider is not None:
    try:
      tag = _version_tag_provider()
    except (OSError, ValueError, RuntimeError):
      logging.debug("Version tag provider failed.", exc_info=True)
    else:
      if tag:
        return tag
  return "unversioned"


def open_file(path: str, mode: str = "r") -> IO[Any]:
  """Opens a file for reading or writing (`r`, `w`, `rb`, `wb`)."""
  backend = _get_backend()
  if backend is not None:
    return backend.open(path, mode)
  if _is_remote_scheme(path):
    _get_beam_filesystem(path)
    bfs = _require_beam_file_systems()
    if "w" in mode:
      raw = bfs.create(path)
      if "b" in mode:
        return raw
      return io.TextIOWrapper(raw, encoding="utf-8")
    if "r" in mode:
      raw = bfs.open(path)
      if "b" in mode:
        return raw
      return io.TextIOWrapper(raw, encoding="utf-8")
    raise ValueError(f"Unsupported open mode: {mode!r}")

  local_path = _normalize_local_path(path)
  if "b" in mode:
    return builtins.open(local_path, mode)
  return builtins.open(local_path, mode, encoding="utf-8")


def rename(src: str, dst: str, overwrite: bool = True) -> None:
  """Atomically renames `src` to `dst`, overwriting `dst` if `overwrite`."""
  backend = _get_backend()
  if backend is not None:
    backend.rename(src, dst, overwrite=overwrite)
    return
  if _is_remote_scheme(src) or _is_remote_scheme(dst):
    _get_beam_filesystem(src)
    _get_beam_filesystem(dst)
    bfs = _require_beam_file_systems()
    if not overwrite and bfs.exists(dst):
      raise FileExistsError(dst)
    bfs.rename([src], [dst])
    return
  local_src = _normalize_local_path(src)
  local_dst = _normalize_local_path(dst)
  if overwrite:
    os.replace(local_src, local_dst)
    return
  if os.path.exists(local_dst):
    raise FileExistsError(local_dst)
  os.rename(local_src, local_dst)


def remove(path: str) -> None:
  """Removes the file at `path`."""
  backend = _get_backend()
  if backend is not None:
    backend.remove(path)
    return
  if _is_remote_scheme(path):
    _get_beam_filesystem(path)
    bfs = _require_beam_file_systems()
    bfs.delete([path])
    return
  os.remove(_normalize_local_path(path))


@contextlib.contextmanager
def atomic_open_for_write(path: str, mode: str = "w") -> Iterator[IO[Any]]:
  """Writes to `path` atomically.

  For local files and registered backends, writes to a unique sibling
  temporary file and atomically replaces `path` on close. For remote object
  stores without a registered backend, writes directly via the underlying
  channel whose object PUT is already atomic, avoiding expensive and
  non-atomic copy+delete renames.

  Args:
    path: Final destination path.
    mode: Write mode (`"w"` or `"wb"`).

  Yields:
    Writable file handle backed by a temporary sibling file or direct channel.
  """
  if mode not in ("w", "wb"):
    raise ValueError(
        f"atomic_open_for_write only supports 'w' or 'wb', got {mode!r}"
    )
  backend = _get_backend()
  if backend is None and _is_remote_scheme(path):
    _get_beam_filesystem(path)
    with open_file(path, mode) as f:
      yield f
    return

  dirname, basename = os.path.split(path)
  tmp_name = (
      f".{basename}.tmp.{os.getpid()}.{threading.get_ident()}"
      f".{uuid.uuid4().hex}"
  )
  tmp_path = os.path.join(dirname, tmp_name) if dirname else tmp_name
  try:
    with open_file(tmp_path, mode) as f:
      yield f
    rename(tmp_path, path, overwrite=True)
  except BaseException:
    try:
      remove(tmp_path)
    except (OSError, Error):
      pass
    raise


def exists(path: str) -> bool:
  """Returns True if `path` exists."""
  backend = _get_backend()
  if backend is not None:
    return bool(backend.exists(path))
  if _is_remote_scheme(path):
    _get_beam_filesystem(path)
    bfs = _require_beam_file_systems()
    return bool(bfs.exists(path))
  return os.path.exists(_normalize_local_path(path))


def makedirs(path: str) -> None:
  """Recursively creates `path` if it does not already exist."""
  backend = _get_backend()
  if backend is not None:
    backend.makedirs(path)
    return
  if _is_remote_scheme(path):
    _get_beam_filesystem(path)
    bfs = _require_beam_file_systems()
    bfs.mkdirs(path)
    return
  os.makedirs(_normalize_local_path(path), exist_ok=True)


def ensure_parent_dir(path: str) -> None:
  """Ensures the parent directory of `path` exists.

  Args:
    path: File path whose parent directory should exist.
  """
  dirname = os.path.dirname(path)
  if dirname and not exists(dirname):
    try:
      makedirs(dirname)
    except (OSError, Error):
      # A concurrent writer may have created the directory first; anything
      # else is a genuine failure.
      if not isdir(dirname):
        raise


def isdir(path: str) -> bool:
  """Returns True if `path` is an existing directory."""
  backend = _get_backend()
  if backend is not None:
    return bool(backend.isdir(path))
  if _is_remote_scheme(path):
    _get_beam_filesystem(path)
    bfs = _require_beam_file_systems()
    pattern = path.rstrip("/") + "/*"
    match_results = bfs.match([pattern], limits=[1])
    return bool(match_results and match_results[0].metadata_list)
  return os.path.isdir(_normalize_local_path(path))


def listdir(path: str) -> list[str]:
  """Lists entries inside directory `path`."""
  backend = _get_backend()
  if backend is not None:
    return list(backend.listdir(path))
  if _is_remote_scheme(path):
    _get_beam_filesystem(path)
    bfs = _require_beam_file_systems()
    prefix = path.rstrip("/") + "/"
    pattern = prefix + "*"
    match_results = bfs.match([pattern])
    if not match_results or not match_results[0].metadata_list:
      return []
    entries = set()
    for meta in match_results[0].metadata_list:
      rel = meta.path
      if rel.startswith(prefix):
        rel = rel[len(prefix) :]
      entry = rel.split("/")[0]
      if entry:
        entries.add(entry)
    return sorted(entries)
  return os.listdir(_normalize_local_path(path))


def glob(pattern: str) -> list[str]:
  """Returns paths matching `pattern`."""
  backend = _get_backend()
  if backend is not None:
    return list(backend.glob(pattern))
  if _is_remote_scheme(pattern):
    _get_beam_filesystem(pattern)
    bfs = _require_beam_file_systems()
    match_results = bfs.match([pattern])
    if match_results and match_results[0].metadata_list:
      return sorted(
          metadata.path for metadata in match_results[0].metadata_list
      )
    return []
  return sorted(glob_mod.glob(_normalize_local_path(pattern)))


def resolve_resource_path(*relative_candidates: str) -> list[str]:
  """Returns local candidate paths for resources bundled with the package.

  For each relative path, this lists the existing matches under the package
  directory and its ancestors, then any existing paths from the registered
  resource resolver, and finally the relative path itself as a fallback.

  Args:
    *relative_candidates: Relative resource paths, in order of preference.

  Returns:
    Candidate paths without duplicates. Existing paths come before the
    unresolved relative fallbacks for each candidate.
  """
  resolved: list[str] = []
  seen: set[str] = set()

  def add(path: str) -> None:
    if path not in seen:
      seen.add(path)
      resolved.append(path)

  # Keep the non-dereferenced path too, so symlinked layouts still resolve
  # resources that sit next to the symlink rather than its target.
  search_roots: list[pathlib.Path] = []
  here_abs = pathlib.Path(os.path.abspath(__file__)).parent
  search_roots.extend([here_abs, *here_abs.parents])
  here_real = pathlib.Path(__file__).resolve().parent
  if here_real != here_abs:
    search_roots.extend([here_real, *here_real.parents])

  for relative_path in relative_candidates:
    if not relative_path:
      continue
    for root in search_roots:
      candidate = str(root / relative_path)
      if os.path.exists(candidate):
        add(candidate)
    resolver_candidates = (
        _resource_resolver(relative_path)
        if _resource_resolver is not None
        else ()
    )
    for candidate in resolver_candidates:
      if os.path.exists(candidate):
        add(candidate)
    add(relative_path)

  return resolved
