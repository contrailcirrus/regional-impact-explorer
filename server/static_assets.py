"""Static assets and boundary GeoJSON serving for the regional dashboard.

Serves the single-page application HTML (`ui/dist/index.html` when built with
Vite, falling back to the unbuilt `ui/index.html`, which needs a bundler to
run), Vite bundle assets under
`/assets/{asset_path:path}`, favicons, and the country and FIR boundary GeoJSON
published with the active dataset.
"""

from collections import abc
import functools
import gzip
import hashlib
import math
import mimetypes
import os
import types
from typing import Annotated, Any, Final, NamedTuple, TypeAlias

from absl import logging
import fastapi
import pydantic

from common import constants
from common import fs
from server import data_loader

_NO_REQUEST: Any = None
_STATIC_CACHE_CONTROL = "public, max-age=86400"
_BOUNDARY_CACHE_CONTROL = "public, no-cache"

HEADER_CACHE_CONTROL = "Cache-Control"
HEADER_CONTENT_ENCODING = "Content-Encoding"
HEADER_ETAG = "ETag"
HEADER_VARY = "Vary"
HEADER_ACCEPT_ENCODING = "Accept-Encoding"
HEADER_ACCEPT_ENCODING_LOWER = "accept-encoding"
HEADER_IF_NONE_MATCH_LOWER = "if-none-match"
ENCODING_GZIP = "gzip"
ENCODING_X_GZIP = "x-gzip"

MEDIA_TYPE_OCTET_STREAM = "application/octet-stream"
MEDIA_TYPE_HTML = "text/html"
MEDIA_TYPE_ICO = "image/x-icon"
MEDIA_TYPE_SVG = "image/svg+xml"
MEDIA_TYPE_JAVASCRIPT = "application/javascript"
MEDIA_TYPE_GEOJSON = "application/geo+json"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_DIR = os.path.dirname(_SCRIPT_DIR)
_UI_DIR = "ui"
_DIST_SUBDIR = "dist"
_ASSETS_SUBDIR = "assets"
_INDEX_HTML_FILENAME = "index.html"
_FAVICON_ICO_FILENAME = "favicon.ico"
_FAVICON_SVG_FILENAME = "favicon.svg"
_ASSET_NOT_FOUND_DETAIL = "Asset not found."
UI_DIST_DIR_ENV_VAR = "CONTRAILS_UI_DIST_DIR"
UI_DIST_DIR: str = os.environ.get(
    UI_DIST_DIR_ENV_VAR, os.path.join(_PACKAGE_DIR, _UI_DIR, _DIST_SUBDIR)
)

# Media types for Vite bundle extensions, pinned because `mimetypes` reads
# host configuration (`/etc/mime.types`, the Windows registry) and can map
# `.js` to `text/plain` or miss `.mjs`/`.wasm`/`.woff2` entirely; browsers
# refuse module scripts served with a non-JavaScript type.
_ASSET_MEDIA_TYPES: Final[abc.Mapping[str, str]] = types.MappingProxyType({
    ".css": "text/css",
    ".ico": MEDIA_TYPE_ICO,
    ".js": "text/javascript",
    ".json": "application/json",
    ".map": "application/json",
    ".mjs": "text/javascript",
    ".png": "image/png",
    ".svg": MEDIA_TYPE_SVG,
    ".wasm": "application/wasm",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
})


class StaticPayload(NamedTuple):
  """Pre-compressed static asset payload and strong SHA-256 ETag."""

  raw_bytes: bytes
  gzip_bytes: bytes
  etag: str


def _asset_media_type(path: str) -> str:
  """Returns the media type of a Vite bundle asset at `path`."""
  _, raw_extension = os.path.splitext(path)
  extension = raw_extension.lower()
  if (media_type := _ASSET_MEDIA_TYPES.get(extension)) is not None:
    return media_type
  guessed, _ = mimetypes.guess_type(path)
  return guessed or MEDIA_TYPE_OCTET_STREAM


def encode_static_payload(raw_bytes: bytes) -> StaticPayload:
  """Pre-compresses `raw_bytes` and computes its strong SHA-256 ETag."""
  gzip_bytes = gzip.compress(raw_bytes, compresslevel=6)
  etag = f'"{hashlib.sha256(raw_bytes).hexdigest()}"'
  return StaticPayload(raw_bytes=raw_bytes, gzip_bytes=gzip_bytes, etag=etag)


# Sized for a Vite build's hashed bundle files plus the boundary GeoJSON.
@functools.lru_cache(maxsize=64)
def load_static_payload_cached(path: str) -> StaticPayload | None:
  """Caches and returns a static asset payload.

  Args:
    path: Asset file path.

  Returns:
    The encoded payload, or `None` if the file does not exist.

  Raises:
    data_loader.ArtifactUnavailableError: If the file exists but cannot be
      read; raising keeps the failure out of the cache.
  """
  if not data_loader.safe_exists(path, strict=True):
    return None
  try:
    with fs.open_file(path, "rb") as asset_file:
      raw_bytes = asset_file.read()
  except (fs.Error, OSError) as e:
    logging.warning("Failed to read static asset %s: %s", path, e)
    raise data_loader.ArtifactUnavailableError(
        f"Could not read static asset {os.path.basename(path)!r}:"
        f" {type(e).__name__}"
    ) from e
  return encode_static_payload(raw_bytes)


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
  """Returns True when the `If-None-Match` header matches `etag`."""
  if not if_none_match:
    return False
  for raw_token in if_none_match.split(","):
    token = raw_token.strip()
    if token == "*":
      return True
    if token.startswith("W/"):
      token = token[2:].strip()
    if token == etag:
      return True
  return False


def _static_response_headers(
    *, cache_control: str, etag: str, gzip_encoded: bool
) -> dict[str, str]:
  """Returns the caching headers shared by every static asset response.

  Args:
    cache_control: Value of the `Cache-Control` header.
    etag: Strong ETag of the uncompressed payload.
    gzip_encoded: Whether the response body is gzip-compressed, which adds a
      `Content-Encoding: gzip` header.

  Returns:
    The response headers, in `Cache-Control`, `Content-Encoding`, `ETag`,
    `Vary` order.
  """
  headers = {HEADER_CACHE_CONTROL: cache_control}
  if gzip_encoded:
    headers[HEADER_CONTENT_ENCODING] = ENCODING_GZIP
  headers[HEADER_ETAG] = etag
  headers[HEADER_VARY] = HEADER_ACCEPT_ENCODING
  return headers


def _parse_qvalue(params: str) -> float:
  """Returns the `q` weight from an `Accept-Encoding` item's parameters."""
  for param in params.split(";"):
    name, _, value = param.partition("=")
    if name.strip() == "q":
      try:
        parsed = float(value.strip())
      except ValueError:
        return 1.0
      if math.isnan(parsed) or not 0.0 <= parsed <= 1.0:
        return 1.0
      return parsed
  return 1.0


def accepts_gzip_encoding(
    request: fastapi.Request | None, *, default_when_missing: bool = False
) -> bool:
  """Returns whether a gzip-encoded response is acceptable for `request`.

  Follows RFC 9110 section 12.5.3: an explicit `gzip` / `x-gzip` entry takes
  precedence over the `*` wildcard, and a weight of `q=0` marks a coding as
  not acceptable.

  Args:
    request: The incoming request, or None when called outside a request.
    default_when_missing: Value returned when `request` is None or omits the
      `Accept-Encoding` header.

  Returns:
    True when a gzip-encoded payload is acceptable, False otherwise.
  """
  if request is None:
    return default_when_missing
  accept_encoding = request.headers.get(HEADER_ACCEPT_ENCODING_LOWER)
  if accept_encoding is None:
    return default_when_missing
  gzip_q: float | None = None
  wildcard_q: float | None = None
  for item in accept_encoding.lower().split(","):
    coding, _, params = item.partition(";")
    coding = coding.strip()
    if coding in (ENCODING_GZIP, ENCODING_X_GZIP):
      quality = _parse_qvalue(params)
      gzip_q = quality if gzip_q is None else max(gzip_q, quality)
    elif coding == "*":
      quality = _parse_qvalue(params)
      wildcard_q = quality if wildcard_q is None else max(wildcard_q, quality)
  if gzip_q is not None:
    return gzip_q > 0.0
  return wildcard_q is not None and wildcard_q > 0.0


def build_static_response(
    payload: StaticPayload | tuple[bytes, bytes, str],
    *,
    media_type: str,
    request: fastapi.Request | None = None,
    cache_control: str = _STATIC_CACHE_CONTROL,
) -> fastapi.responses.Response:
  """Builds a cached HTTP response with ETag, Cache-Control, and pre-gzip."""
  raw_bytes, gzip_bytes, etag = payload
  if request is not None and _etag_matches(
      request.headers.get(HEADER_IF_NONE_MATCH_LOWER), etag
  ):
    return fastapi.Response(
        status_code=304,
        headers=_static_response_headers(
            cache_control=cache_control, etag=etag, gzip_encoded=False
        ),
    )

  gzip_encoded = accepts_gzip_encoding(request)
  return fastapi.Response(
      content=gzip_bytes if gzip_encoded else raw_bytes,
      media_type=media_type,
      headers=_static_response_headers(
          cache_control=cache_control, etag=etag, gzip_encoded=gzip_encoded
      ),
  )


@functools.lru_cache(maxsize=64)
def resolve_static_file(relative_path: str) -> str | None:
  """Resolves an asset path via `UI_DIST_DIR`, the cwd, package dir, and `fs`.

  Args:
    relative_path: Relative file path (e.g., `ui/index.html` or
      `ui/favicon.svg`).

  Returns:
    Resolved absolute file path if existing, otherwise None.
  """
  ui_prefix = f"{_UI_DIR}/"
  if relative_path.startswith(ui_prefix) and UI_DIST_DIR:
    dist_candidate = os.path.normpath(
        os.path.join(UI_DIST_DIR, relative_path[len(ui_prefix) :])
    )
    if data_loader.safe_exists(dist_candidate):
      return dist_candidate

  absolute_cwd = os.path.abspath(relative_path)
  if data_loader.safe_exists(absolute_cwd):
    return absolute_cwd

  real_package_dir = os.path.dirname(
      os.path.dirname(os.path.realpath(__file__))
  )
  package_roots = [_PACKAGE_DIR]
  if real_package_dir != _PACKAGE_DIR:
    package_roots.append(real_package_dir)

  for package_directory in package_roots:
    direct_package = os.path.normpath(
        os.path.join(package_directory, relative_path)
    )
    if data_loader.safe_exists(direct_package):
      return direct_package

  for resource_candidate in fs.resolve_resource_path(relative_path):
    if data_loader.safe_exists(resource_candidate):
      return resource_candidate

  return None


def serve_index() -> fastapi.responses.FileResponse:
  """Serves the main regional dashboard single-page application HTML."""
  relative_path = f"{_UI_DIR}/{_INDEX_HTML_FILENAME}"
  path = resolve_static_file(relative_path)
  if not path:
    raise fastapi.HTTPException(
        status_code=404, detail=f"{_INDEX_HTML_FILENAME} not found."
    )
  return fastapi.responses.FileResponse(path, media_type=MEDIA_TYPE_HTML)


def serve_favicon() -> fastapi.responses.FileResponse:
  """Serves favicon.ico icon file, falling back to SVG if unavailable."""
  relative_path = f"{_UI_DIR}/{_FAVICON_ICO_FILENAME}"
  path = resolve_static_file(relative_path)
  if not path:
    svg_relative_path = f"{_UI_DIR}/{_FAVICON_SVG_FILENAME}"
    svg_path = resolve_static_file(svg_relative_path)
    if svg_path:
      return fastapi.responses.FileResponse(svg_path, media_type=MEDIA_TYPE_SVG)
    raise fastapi.HTTPException(
        status_code=404, detail=f"{_FAVICON_ICO_FILENAME} not found."
    )
  return fastapi.responses.FileResponse(path, media_type=MEDIA_TYPE_ICO)


def serve_favicon_svg() -> fastapi.responses.FileResponse:
  """Serves vector favicon.svg icon file."""
  relative_path = f"{_UI_DIR}/{_FAVICON_SVG_FILENAME}"
  path = resolve_static_file(relative_path)
  if not path:
    raise fastapi.HTTPException(
        status_code=404, detail=f"{_FAVICON_SVG_FILENAME} not found."
    )
  return fastapi.responses.FileResponse(path, media_type=MEDIA_TYPE_SVG)


def serve_vite_asset(
    asset_path: str = fastapi.Path(
        ..., description="Relative path of a built asset under ui/dist/assets/."
    ),
    request: fastapi.Request = _NO_REQUEST,
) -> fastapi.responses.Response:
  """Serves a built Vite bundle asset from `UI_DIST_DIR/assets/`."""
  clean_relative_path = (asset_path or "").strip().lstrip("/")
  if not clean_relative_path or not UI_DIST_DIR:
    raise fastapi.HTTPException(status_code=404, detail=_ASSET_NOT_FOUND_DETAIL)

  # `realpath` resolves symlinks, so a link inside `assets/` that points
  # outside it is rejected by the containment check below.
  assets_root = os.path.realpath(os.path.join(UI_DIST_DIR, _ASSETS_SUBDIR))
  candidate = os.path.realpath(os.path.join(assets_root, clean_relative_path))
  try:
    common = os.path.commonpath([assets_root, candidate])
  except ValueError as e:
    raise fastapi.HTTPException(
        status_code=404, detail=_ASSET_NOT_FOUND_DETAIL
    ) from e
  if common != assets_root or candidate == assets_root:
    raise fastapi.HTTPException(status_code=404, detail=_ASSET_NOT_FOUND_DETAIL)

  payload = load_static_payload_cached(candidate)
  if payload is None:
    raise fastapi.HTTPException(status_code=404, detail=_ASSET_NOT_FOUND_DETAIL)

  return build_static_response(
      payload,
      # The requested name, not the resolved target, decides the media type.
      media_type=_asset_media_type(clean_relative_path),
      request=request,
  )


_BOUNDARY_YEAR_DESCRIPTION = (
    "Year whose published run to read the boundaries from, placed right after"
    " the resource name. Must be one of the years listed by /years."
)

BoundaryYearPath: TypeAlias = Annotated[
    int,
    fastapi.Path(description=_BOUNDARY_YEAR_DESCRIPTION),
    pydantic.BeforeValidator(data_loader.require_supported_year),
]


def _boundary_geojson_path(filename: str, year: int) -> str:
  """Path of a boundary file inside a year's published run.

  Every year's run publishes its own copy of the boundaries, so the caller
  names the year explicitly; there is no server-side default year.

  Args:
    filename: `constants.COUNTRIES_GEOJSON_FILENAME` or
      `constants.FIRS_GEOJSON_FILENAME`.
    year: A year listed by `data_loader.get_available_years`.

  Returns:
    The candidate path; it may not exist for runs published before the
    pipeline started writing boundaries.

  Raises:
    data_loader.DataValidationError: If the year has no published data.
  """
  dataset_dir = data_loader.resolve_dataset_dir(
      year, data_loader.active_dataset()
  )
  if dataset_dir is None:
    raise data_loader.DataValidationError(
        f"No published data for year: {year}"
    )
  return os.path.join(dataset_dir, filename)


def _serve_boundary_geojson(
    filename: str, year: int, request: fastapi.Request | None
) -> fastapi.responses.Response:
  """Serves a boundary GeoJSON published with the active dataset's year run."""
  path = _boundary_geojson_path(filename, year)
  payload = load_static_payload_cached(path)
  if payload is None:
    raise fastapi.HTTPException(
        status_code=404,
        detail=(
            f"{filename} not found in the active dataset run for year"
            f" '{year}'; re-derive the dataset to publish it."
        ),
    )
  return build_static_response(
      payload,
      media_type=MEDIA_TYPE_GEOJSON,
      request=request,
      # The year-scoped URL stays the same across dataset re-publications or
      # restarts, so the browser must revalidate; the ETag turns that into a
      # cheap 304.
      cache_control=_BOUNDARY_CACHE_CONTROL,
  )


def get_countries_geojson(
    year: BoundaryYearPath,
    request: fastapi.Request = _NO_REQUEST,
) -> fastapi.responses.Response:
  """Serves the country boundaries a year's run was computed on."""
  return _serve_boundary_geojson(
      constants.COUNTRIES_GEOJSON_FILENAME, year, request
  )


def get_firs_geojson(
    year: BoundaryYearPath,
    request: fastapi.Request = _NO_REQUEST,
) -> fastapi.responses.Response:
  """Serves the normalized FIR/UIR boundaries a year's run used."""
  return _serve_boundary_geojson(constants.FIRS_GEOJSON_FILENAME, year, request)


_CACHED_STATIC_FUNCTIONS = (
    resolve_static_file,
    load_static_payload_cached,
)
_static_cache_clear_callbacks: list[abc.Callable[[], None]] = []


def register_static_cache_clear_callback(
    callback: abc.Callable[[], None],
) -> None:
  """Registers a callback invoked whenever `clear_static_asset_caches` runs."""
  if callback not in _static_cache_clear_callbacks:
    _static_cache_clear_callbacks.append(callback)


def clear_static_asset_caches() -> None:
  """Empties all in-memory static asset path and payload caches."""
  for fn in _CACHED_STATIC_FUNCTIONS:
    fn.cache_clear()
  for callback in _static_cache_clear_callbacks:
    callback()


data_loader.register_cache_clear_callback(clear_static_asset_caches)
