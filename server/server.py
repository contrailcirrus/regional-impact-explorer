"""REST API server for regional contrails country and FIR impact metrics.

Provides endpoints to look up country and FIR metrics, rankings by metric,
multi-year time series, spatial grid buffers, boundary GeoJSONs, and temporal
matrices.

The data endpoints live on `router`, so another FastAPI application can mount
them under a prefix. A host application must also call
`register_exception_handlers` (routers cannot carry exception handlers), expose
the `X-Grid-Meta` header in its CORS policy, configure the dataset through the
`CONTRAILS_DATA_DIR` and `CONTRAILS_DATASET` environment variables, and call
`data_loader.configure_storage` at startup so a `gs://` data root is readable
without Apache Beam.

`app` is the standalone server: it mounts `router` at the root, adds CORS, gzip
and the dashboard's static assets, and preloads the dataset at startup.
"""

import asyncio
from collections import abc
import contextlib
import dataclasses
import functools
import gzip
import os
import threading
import types
from typing import Annotated, Any, NamedTuple, TypeAlias, TypeVar, TypedDict
import zlib

from absl import app as absl_app
from absl import logging
import fastapi
from fastapi import responses
from fastapi.middleware import cors
from fastapi.middleware import gzip as gzip_middleware
import pydantic
import uvicorn

from common import constants
from common import fs
from server import data_loader
from server import metrics
from server import models
from server import static_assets

_YEAR_PATH_DESCRIPTION = (
    "Dataset year to query, placed right after the resource name. Must be one"
    " of the years listed by /years."
)
SupportedYearPath: TypeAlias = Annotated[
    int,
    fastapi.Path(description=_YEAR_PATH_DESCRIPTION),
    pydantic.BeforeValidator(data_loader.require_supported_year),
]
FormattedYearPath: TypeAlias = Annotated[
    int,
    fastapi.Path(description=_YEAR_PATH_DESCRIPTION),
    pydantic.BeforeValidator(data_loader.require_year_format),
]
CORS_ORIGIN_REGEX_ENV_VAR = "CONTRAILS_CORS_ORIGIN_REGEX"
_PORT_ENV_VAR = "PORT"
_HOST_ENV_VAR = "HOST"
_DEFAULT_PORT = 9090
_DEFAULT_HOST = "0.0.0.0"
_HEADER_X_GRID_META = "X-Grid-Meta"
_CACHE_CONTROL_ONE_HOUR = "public, max-age=3600"
_KEY_DETAIL = "detail"
_KEY_COUNTRIES = "countries"
_KEY_COUNTRY_NAME = "country_name"
_KEY_VALUE = "value"
_KEY_SECONDARY_VALUE = "secondary_value"
_KEY_RANK = "rank"

# Strong references to in-flight background tasks. asyncio only holds weak
# references to running tasks, so without this set the preload task can be
# garbage-collected mid-execution.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


async def _safe_preload() -> None:
  """Warms the dataset caches in a worker thread, logging instead of raising.

  Preloading is a pure optimization: every endpoint lazily loads what it
  needs. A failure here must never prevent the server from starting.
  """
  try:
    await asyncio.to_thread(data_loader.preload_all_data)
  except (
      data_loader.ArtifactUnavailableError,
      data_loader.DatasetConfigError,
      data_loader.DataValidationError,
      fs.Error,
      OSError,
      RuntimeError,
  ) as e:
    logging.exception(
        "Background dataset preloading encountered an error: %s", e
    )


@contextlib.asynccontextmanager
async def lifespan(
    app_instance: fastapi.FastAPI,
) -> abc.AsyncIterator[None]:
  """Sets up storage, checks the dataset config, then preloads in background.

  Only the standalone `app` runs this. A host application that mounts `router`
  skips the preload, and every endpoint loads what it needs on first use.

  Args:
    app_instance: The FastAPI application being started. Unused.

  Yields:
    None, once the background preload has been scheduled.

  Raises:
    data_loader.DatasetConfigError: If no dataset is configured and
      `CONTRAILS_DATASET` is unset or invalid. Raising here aborts startup,
      which also covers `uvicorn server.server:app` launches that bypass
      `main`.
  """
  del app_instance  # Unused.
  data_loader.configure_storage()
  dataset = data_loader.ensure_dataset_configured()
  logging.info("Serving dataset %r from %s", dataset, data_loader.DATA_DIR)

  task = asyncio.create_task(_safe_preload())
  _BACKGROUND_TASKS.add(task)
  task.add_done_callback(_BACKGROUND_TASKS.discard)
  yield


DEFAULT_CORS_ORIGIN_REGEX = r"http://127\.0\.0\.1:\d+|http://localhost:\d+"

app = fastapi.FastAPI(
    title="Regional Contrails Dashboard API",
    description=(
        "Provides access to per-country and per-FIR aviation contrail impact"
        " metrics, spatial grids, and boundary GeoJSONs."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    cors.CORSMiddleware,
    allow_origin_regex=os.environ.get(
        CORS_ORIGIN_REGEX_ENV_VAR, DEFAULT_CORS_ORIGIN_REGEX
    ),
    # The API is read-only and the UI is served from the same origin, so
    # cross-origin callers get no cookies and only safe methods.
    allow_credentials=False,
    allow_methods=["GET", "HEAD"],
    expose_headers=[_HEADER_X_GRID_META],
)


def _gzip_middleware_kwargs() -> abc.Mapping[str, Any]:
  """Returns immutable keyword arguments for `GZipMiddleware`."""
  kwdefaults = gzip_middleware.GZipMiddleware.__init__.__kwdefaults__ or {}
  kwargs: dict[str, Any] = {
      "minimum_size": 1000,
      "compresslevel": 5,
  }
  if (exclude_types := kwdefaults.get("exclude_content_types")) is not None:
    kwargs["exclude_content_types"] = (
        *exclude_types,
        static_assets.MEDIA_TYPE_OCTET_STREAM,
    )
  return types.MappingProxyType(kwargs)


_GZIP_MIDDLEWARE_KWARGS: abc.Mapping[str, Any] = _gzip_middleware_kwargs()
app.add_middleware(gzip_middleware.GZipMiddleware, **_GZIP_MIDDLEWARE_KWARGS)


def configure_cors_origin_regex(origin_regex: str) -> None:
  """Reconfigures the CORS allow_origin_regex before the first request."""
  for middleware in app.user_middleware:
    if middleware.cls is cors.CORSMiddleware:
      middleware.kwargs["allow_origin_regex"] = origin_regex
  app.middleware_stack = None


# The domain layers (data_loader, metrics) raise framework-free exceptions so
# they stay reusable from CLIs and notebooks. This is the single place that
# knows how those failures map onto HTTP status codes.
_DOMAIN_ERROR_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (data_loader.DataValidationError, 400),
    (metrics.UnknownMetricError, 404),
    (metrics.UnsupportedGwpError, 422),
    (metrics.MissingColumnError, 422),
    # Only reachable if the app is served without going through `lifespan`
    # or `main`, which both refuse to start unconfigured.
    (data_loader.DatasetConfigError, 503),
    # A published year whose required artifact cannot be read.
    (data_loader.ArtifactUnavailableError, 503),
)


def _domain_error_response(
    request: fastapi.Request,
    exc: Exception,
    *,
    status_code: int,
) -> responses.JSONResponse:
  """Renders a domain exception as a JSON error body with `status_code`.

  Args:
    request: The incoming request. Unused.
    exc: The domain exception being handled.
    status_code: HTTP status code mapped to the exception type.

  Returns:
    A JSON response of the form `{"detail": str(exc)}`.
  """
  del request  # Unused.
  return responses.JSONResponse(
      status_code=status_code,
      content={_KEY_DETAIL: str(exc)},
  )


def register_exception_handlers(
    application: fastapi.FastAPI,
) -> None:
  """Maps each domain exception type onto its HTTP status code.

  Call this on any application that includes `router`.

  Args:
    application: The FastAPI application to register handlers on.
  """
  for error_type, status_code in _DOMAIN_ERROR_STATUS:
    application.add_exception_handler(
        error_type,
        functools.partial(_domain_error_response, status_code=status_code),
    )


register_exception_handlers(app)

# The data endpoints. `app` includes them below, after they are all defined.
router = fastapi.APIRouter()


_ModelT = TypeVar("_ModelT", bound=pydantic.BaseModel)


def _malformed_row_error(
    row: abc.Mapping[str, Any], *, artifact: str, fields: abc.Iterable[str]
) -> data_loader.ArtifactUnavailableError:
  """Logs and returns the error for a published row that breaks the schema.

  The message names the row identity and the invalid fields but never the
  offending values.

  Args:
    row: The row as loaded from the artifact.
    artifact: Human-readable artifact name for the error message.
    fields: Names of the invalid fields.

  Returns:
    The error, for the caller to raise.
  """
  identity = (
      row.get(constants.KEY_FIR_ID)
      or row.get(constants.KEY_ISO_A2)
      or "<unknown>"
  )
  message = (
      f"Malformed {artifact} row '{identity}': invalid field(s)"
      f" {', '.join(fields)}."
  )
  logging.error(message)
  return data_loader.ArtifactUnavailableError(message)


def _validate_artifact_row(
    model: type[_ModelT],
    row: abc.Mapping[str, Any],
    *,
    artifact: str,
) -> _ModelT:
  """Validates one published artifact row against its response model.

  A row that does not match the model means the published run is malformed,
  not that the request is wrong, so the failure surfaces as
  `data_loader.ArtifactUnavailableError` (503) rather than an unmapped 500.

  Args:
    model: The Pydantic response model for the row.
    row: The row as loaded from the artifact.
    artifact: Human-readable artifact name for the error message.

  Returns:
    The validated model instance.

  Raises:
    data_loader.ArtifactUnavailableError: If the row does not match the model.
      The message names the row identity and the invalid fields but never the
      offending values.
  """
  try:
    return model.model_validate(row)
  except pydantic.ValidationError as e:
    fields = sorted(
        {".".join(str(part) for part in error["loc"]) for error in e.errors()}
    )
    raise _malformed_row_error(row, artifact=artifact, fields=fields) from e


def _controlling_country_iso2s(
    row: abc.Mapping[str, Any], *, artifact: str
) -> list[str]:
  """Returns the ISO alpha-2 codes of the countries controlling a FIR row.

  Args:
    row: A FIR row as loaded from the artifact.
    artifact: Human-readable artifact name for the error message.

  Returns:
    The row's `fir_to_country_iso2s` codes (primary country first), or an
    empty list when the row has none.

  Raises:
    data_loader.ArtifactUnavailableError: If `fir_to_country_iso2s` is not a
      list of strings, e.g. the comma-joined string of an older run.
  """
  codes = row.get(constants.KEY_FIR_TO_COUNTRY_ISO2S)
  if codes is None:
    return []
  # Cached rows are frozen, so a published JSON list arrives as a tuple.
  if isinstance(codes, (list, tuple)) and all(
      isinstance(code, str) for code in codes
  ):
    return list(codes)
  raise _malformed_row_error(
      row, artifact=artifact, fields=[constants.KEY_FIR_TO_COUNTRY_ISO2S]
  )


@router.get("/years", response_model=models.AvailableYearsResponse)
def get_available_years() -> models.AvailableYearsResponse:
  """Returns the years with published data, sorted ascending.

  The server has no default year. Clients choose one of these years and pass
  it on every year-scoped request.
  """
  return models.AvailableYearsResponse(years=data_loader.get_available_years())


# Boundary GeoJSON routes. Starlette matches routes in registration order and
# `/countries/{year}` / `/firs/{year}` would otherwise also capture
# `2025.json`, so these must be registered before the list routes below.
router.add_api_route(
    "/countries/{year}.json",
    static_assets.get_countries_geojson,
    methods=["GET"],
    response_class=fastapi.responses.Response,
)
router.add_api_route(
    "/firs/{year}.json",
    static_assets.get_firs_geojson,
    methods=["GET"],
    response_class=fastapi.responses.Response,
)


@router.get("/country/{year}/{iso}", response_model=models.CountryMetricsResponse)
def get_country_by_year_and_iso(
    year: SupportedYearPath,
    iso: str = fastapi.Path(..., description="ISO alpha-2 country code."),
) -> models.CountryMetricsResponse:
  """Returns metric values for a country identified by year and ISO code.

  Args:
    year: Dataset year to query.
    iso: 2-letter ISO alpha-2 country code, case-insensitive. Other path-safe
      alphanumeric tokens (such as alpha-3 or numeric codes) are not translated
      and return 404, whereas identifiers containing spaces or other disallowed
      characters raise `DataValidationError` (HTTP 400).

  Returns:
    The country's metrics for the requested year.

  Raises:
    data_loader.DataValidationError: If `year` or `iso` is malformed, or if
      `year` has no published data (mapped to HTTP 400).
    data_loader.ArtifactUnavailableError: If the year's country export is
      unreadable or malformed (mapped to HTTP 503).
    fastapi.HTTPException: 404 if no country matches.
  """
  iso_key = data_loader.resolve_iso_code(iso)
  _, index = data_loader.get_country_data_for_year(year)
  if (country_row := index.get(iso_key)) is not None:
    return _validate_artifact_row(
        models.CountryMetricsResponse,
        dict(country_row),
        artifact=constants.COUNTRY_EXPORT_FILENAME,
    )

  logging.info(
      "Country query for ISO '%s' year '%s' yielded no matches.",
      iso_key,
      year,
  )
  raise fastapi.HTTPException(
      status_code=404,
      detail=(
          f"Country with ISO identifier '{iso_key}' not found for year"
          f" '{year}'."
      ),
  )


class _CachedCountriesResponse(NamedTuple):
  """Cached `/countries/{year}` response paired with backing rows sequence."""

  rows: Any
  response: list[models.CountryMetricsResponse]


class _CachedFirsResponse(NamedTuple):
  """Cached `/firs/{year}` response with backing rows and country index."""

  rows: Any
  country_index: Any
  response: list[models.FirMetricsResponse]


class _CachedRankingsResponse(NamedTuple):
  """Cached `/rankings/{year}/{metric}` response paired with backing inputs."""

  rows: Any
  country_index: Any
  response: models.RankingsResponse


class RankingEntryDict(TypedDict, total=False):
  """Intermediate dictionary for sorting and formatting a ranking entry."""

  name: str
  iso_a2: str
  fir_id: str
  fir_icao: str
  firname: str
  type: str | None
  country_name: str
  countries: list[models.FirManagingCountry]
  value: float | None
  secondary_value: float | None
  rank: int | None


class _ParsedRankingEntry(NamedTuple):
  """Deduplication key and intermediate ranking entry dictionary."""

  dedup_key: Any
  entry: RankingEntryDict


_RESPONSE_CACHE_LOCK = threading.Lock()
_countries_response_cache: dict[
    tuple[str | None, int, str], _CachedCountriesResponse
] = {}
_firs_response_cache: dict[tuple[str | None, int, str], _CachedFirsResponse] = (
    {}
)
_rankings_response_cache: dict[
    tuple[str | None, int, str, str, str], _CachedRankingsResponse
] = {}


def _clear_endpoint_response_caches() -> None:
  """Clears cached country, FIR, and rankings endpoint response objects."""
  with _RESPONSE_CACHE_LOCK:
    _countries_response_cache.clear()
    _firs_response_cache.clear()
    _rankings_response_cache.clear()


data_loader.register_cache_clear_callback(_clear_endpoint_response_caches)


@router.get(
    "/countries/{year}", response_model=list[models.CountryMetricsResponse]
)
def list_countries(
    year: SupportedYearPath,
) -> list[models.CountryMetricsResponse]:
  """Lists every country row for a year.

  Args:
    year: Dataset year to query.

  Returns:
    All country rows for the year.
  """
  dataset_clean = data_loader.active_dataset()
  rows, _ = data_loader.get_country_data_for_year(year, dataset_clean)
  cache_key = (
      data_loader.resolve_dataset_dir(year, dataset_clean),
      year,
      dataset_clean,
  )
  with _RESPONSE_CACHE_LOCK:
    cached = _countries_response_cache.get(cache_key)
  if cached is not None and cached.rows is rows:
    return cached.response
  response = [
      _validate_artifact_row(
          models.CountryMetricsResponse,
          dict(country_row),
          artifact=constants.COUNTRY_EXPORT_FILENAME,
      )
      for country_row in rows
  ]
  with _RESPONSE_CACHE_LOCK:
    _countries_response_cache[cache_key] = _CachedCountriesResponse(
        rows=rows, response=response
    )
  return response


@router.get("/firs/{year}", response_model=list[models.FirMetricsResponse])
def list_firs(
    year: SupportedYearPath,
) -> list[models.FirMetricsResponse]:
  """Lists every FIR row for a year, with its controlling countries.

  Args:
    year: Dataset year to query.

  Returns:
    All FIR rows for the year. Each row gets a `countries` list of
    `{iso_a2, name}` for the codes in `fir_to_country_iso2s`.
  """
  dataset_clean = data_loader.active_dataset()
  rows = data_loader.get_fir_data_for_year(year, dataset_clean)
  _, country_index = data_loader.get_country_data_for_year(
      year, dataset_clean
  )
  cache_key = (
      data_loader.resolve_dataset_dir(year, dataset_clean),
      year,
      dataset_clean,
  )
  with _RESPONSE_CACHE_LOCK:
    cached = _firs_response_cache.get(cache_key)
  if (
      cached is not None
      and cached.rows is rows
      and cached.country_index is country_index
  ):
    return cached.response

  result: list[models.FirMetricsResponse] = []
  for row in rows:
    country_isos = _controlling_country_iso2s(
        row, artifact=constants.FIR_EXPORT_FILENAME
    )
    enriched_row = dict(row)
    enriched_row[_KEY_COUNTRIES] = _managing_countries(
        country_index, country_isos
    )
    result.append(
        _validate_artifact_row(
            models.FirMetricsResponse,
            enriched_row,
            artifact=constants.FIR_EXPORT_FILENAME,
        )
    )
  with _RESPONSE_CACHE_LOCK:
    _firs_response_cache[cache_key] = _CachedFirsResponse(
        rows=rows, country_index=country_index, response=result
    )
  return result


def _ranking_sort_key(
    entry: RankingEntryDict | abc.Mapping[str, Any],
    tiebreak_fields: abc.Sequence[str],
) -> tuple[int, float, float, tuple[str, ...]]:
  """Orders ranking entries best-first, with missing values last.

  Entries with a value sort by value descending, then by the pipeline's
  precomputed rank (which already breaks ties deterministically), then by
  `tiebreak_fields`. Entries without a value (e.g. per-area metrics for an
  entity with no area) sort after all valued entries, by `tiebreak_fields`.

  Args:
    entry: Ranking entry with `value` and `rank` keys.
    tiebreak_fields: Entry keys used, in order, as the final tie-breaker.

  Returns:
    A sort key tuple.
  """
  ids = tuple(str(entry.get(field) or "") for field in tiebreak_fields)
  value = entry.get(_KEY_VALUE)
  if value is None:
    return (1, 0.0, 0.0, ids)
  rank = entry.get(_KEY_RANK)
  return (0, -value, float(rank) if rank else float("inf"), ids)


def _display_rank(
    index: int, entry: RankingEntryDict | abc.Mapping[str, Any]
) -> int | None:
  """Returns the 1-based rank of a sorted entry, or None if it has no value.

  Valued entries sort before missing ones (see `_ranking_sort_key`), so the
  position index is also the rank among valued entries.

  Args:
    index: 0-based position of the entry in the sorted list.
    entry: Ranking entry with a `value` key.
  """
  return index + 1 if entry.get(_KEY_VALUE) is not None else None


def _build_single_fir_ranking_entry(
    row: abc.Mapping[str, Any],
    country_index: abc.Mapping[str, abc.Mapping[str, Any]],
    *,
    field: str,
    rank_field: str | None,
    secondary_field: str | None,
) -> _ParsedRankingEntry | None:
  """Builds a deduplication key and ranking entry dict from one FIR row.

  Args:
    row: Raw FIR summary row dictionary.
    country_index: Mapping from uppercase ISO-2 code to country summary row.
    field: Resolved primary metric column name.
    rank_field: Resolved precomputed rank column name, or `None`.
    secondary_field: Resolved secondary metric column name, or `None`.

  Returns:
    A `_ParsedRankingEntry(dedup_key, entry)`, or `None` if the row lacks FIR
    identity.
  """
  row_fir_id = str(row.get(constants.KEY_FIR_ID) or "").strip().upper()
  icao = str(row.get(constants.KEY_FIR_ICAO) or "").strip().upper()
  fir_name = str(row.get(constants.KEY_FIRNAME) or "").strip()
  if not row_fir_id and not icao and not fir_name:
    return None
  dedup_key = row_fir_id or f"{icao}_{fir_name.upper()}"

  country_isos = _controlling_country_iso2s(
      row, artifact=constants.FIR_EXPORT_FILENAME
  )
  primary_iso, *_ = country_isos if country_isos else ("",)
  metric_values = _coerce_ranking_metric_values(
      row,
      field=field,
      rank_field=rank_field,
      secondary_field=secondary_field,
  )
  entry: RankingEntryDict = {
      "name": fir_name or icao or "Unknown FIR",
      "iso_a2": primary_iso,
      "fir_id": row_fir_id,
      "fir_icao": icao,
      "firname": fir_name,
      "type": row.get(constants.KEY_TYPE),
      "country_name": _lookup_country_name(country_index, primary_iso),
      "countries": _managing_countries(country_index, country_isos),
      "value": metric_values.value,
      "secondary_value": metric_values.secondary_value,
      "rank": metric_values.rank,
  }
  return _ParsedRankingEntry(dedup_key=dedup_key, entry=entry)


def _managing_countries(
    country_index: abc.Mapping[str, abc.Mapping[str, Any]],
    country_isos: abc.Iterable[str],
) -> list[models.FirManagingCountry]:
  """Returns `FirManagingCountry` items for the countries managing a FIR."""
  return [
      models.FirManagingCountry(
          iso_a2=country_iso,
          name=_lookup_country_name(country_index, country_iso),
      )
      for country_iso in country_isos
  ]


def _lookup_country_name(
    country_index: abc.Mapping[str, abc.Mapping[str, Any]], iso: str
) -> str:
  """Returns a country's display name, falling back to its ISO-2 code.

  Args:
    country_index: Mapping from uppercase ISO-2 code to country summary row.
    iso: Uppercase ISO-2 code to look up.
  """
  country_row = country_index.get(iso, {})
  return str(country_row.get(constants.KEY_NAME) or iso)


class _RankingMetricValues(NamedTuple):
  """Coerced metric values of one ranking row."""

  value: float | None
  secondary_value: float | None
  rank: int | None


def _coerce_ranking_metric_values(
    row: abc.Mapping[str, Any],
    *,
    field: str,
    rank_field: str | None,
    secondary_field: str | None,
) -> _RankingMetricValues:
  """Reads the primary, secondary, and precomputed-rank values of a row.

  Args:
    row: Raw country or FIR summary row dictionary.
    field: Resolved primary metric column name.
    rank_field: Resolved precomputed rank column name, or `None`.
    secondary_field: Resolved secondary metric column name, or `None`.

  Returns:
    The coerced values. `rank` is None when there is no rank column, no
    primary value, or the precomputed rank is not positive.
  """
  metric_value = metrics.coerce_float(row.get(field))
  precomputed_rank = (
      metrics.coerce_float(row.get(rank_field))
      if rank_field and metric_value is not None
      else None
  )
  return _RankingMetricValues(
      value=metric_value,
      secondary_value=(
          metrics.coerce_float(row.get(secondary_field))
          if secondary_field
          else None
      ),
      rank=(
          int(precomputed_rank)
          if precomputed_rank is not None and precomputed_rank > 0
          else None
      ),
  )


def _build_fir_rankings_response(
    *,
    year: int,
    gwp_norm: str,
    fir_spec: metrics.MetricSpec,
) -> models.RankingsResponse:
  """Builds or returns the cached `RankingsResponse` for a FIR-level metric."""
  dataset_clean = data_loader.active_dataset()
  fir_rows = data_loader.get_fir_data_for_year(year, dataset_clean)
  _, country_index = data_loader.get_country_data_for_year(year, dataset_clean)
  cache_key = (
      data_loader.resolve_dataset_dir(year, dataset_clean),
      year,
      dataset_clean,
      fir_spec.id,
      gwp_norm,
  )
  with _RESPONSE_CACHE_LOCK:
    cached_fir = _rankings_response_cache.get(cache_key)
  if (
      cached_fir is not None
      and cached_fir.rows is fir_rows
      and cached_fir.country_index is country_index
  ):
    return cached_fir.response

  ranking_fields = _resolve_ranking_fields(
      fir_spec, fir_rows, gwp_norm=gwp_norm, year=year
  )
  fir_items = _build_fir_ranking_items(
      fir_rows, country_index=country_index, ranking_fields=ranking_fields
  )
  fir_response = models.RankingsResponse(
      metric=fir_spec.id,
      title=f"{fir_spec.resolved_title(gwp_norm)} (FIRs)",
      year=year,
      gwp=gwp_norm,
      unit=fir_spec.unit,
      secondary_unit=fir_spec.secondary_unit,
      isFirRanking=True,
      items=fir_items,
  )
  with _RESPONSE_CACHE_LOCK:
    _rankings_response_cache[cache_key] = _CachedRankingsResponse(
        rows=fir_rows,
        country_index=country_index,
        response=fir_response,
    )
  return fir_response


def _build_single_country_ranking_entry(
    row: abc.Mapping[str, Any],
    *,
    field: str,
    rank_field: str | None,
    secondary_field: str | None,
) -> _ParsedRankingEntry | None:
  """Builds a deduplication key and ranking entry dict from one country row.

  Args:
    row: Raw country summary row dictionary.
    field: Resolved primary metric column name.
    rank_field: Resolved precomputed rank column name, or `None`.
    secondary_field: Resolved secondary metric column name, or `None`.

  Returns:
    A `_ParsedRankingEntry(dedup_key, entry)`, or `None` if `name` or `iso_a2`
    is missing.
  """
  iso_code = row.get(constants.KEY_ISO_A2)
  country_name = row.get(constants.KEY_NAME)
  if not country_name or not iso_code:
    return None
  iso_clean = str(iso_code).strip().upper()
  entity_key = (
      (iso_clean, str(country_name).strip().upper())
      if iso_clean.startswith("-") or iso_clean == constants.UNASSIGNED_ISO
      else iso_clean
  )
  metric_values = _coerce_ranking_metric_values(
      row,
      field=field,
      rank_field=rank_field,
      secondary_field=secondary_field,
  )
  entry: RankingEntryDict = {
      "name": str(country_name),
      "iso_a2": iso_clean,
      "value": metric_values.value,
      "secondary_value": metric_values.secondary_value,
      "rank": metric_values.rank,
  }
  return _ParsedRankingEntry(dedup_key=entity_key, entry=entry)


class _RankingFields(NamedTuple):
  """Resolved dataset columns backing a ranking metric."""

  field: str
  rank_field: str | None
  secondary_field: str | None


def _resolve_ranking_fields(
    spec: metrics.MetricSpec,
    rows: abc.Sequence[abc.Mapping[str, Any]],
    *,
    gwp_norm: str,
    year: int,
) -> _RankingFields:
  """Resolves a ranking metric's columns and checks the primary one exists.

  Args:
    spec: Metric spec to rank by.
    rows: Dataset rows the ranking is built from.
    gwp_norm: Normalized GWP horizon.
    year: Dataset year, used in the error message.

  Returns:
    The resolved primary, precomputed rank, and secondary column names.

  Raises:
    metrics.MissingColumnError: If the dataset lacks the primary column.
  """
  field = spec.resolve(spec.field, gwp_norm)
  metrics.require_column(
      rows,
      field,
      metric=spec.id,
      gwp=gwp_norm,
      year=year,
      dataset=data_loader.active_dataset(),
  )
  return _RankingFields(
      field=field,
      rank_field=spec.resolve(spec.rank_field, gwp_norm),
      secondary_field=spec.resolve(spec.secondary_field, gwp_norm),
  )


def _dedupe_ranking_entries(
    parsed_entries: abc.Iterable[_ParsedRankingEntry | None],
) -> list[RankingEntryDict]:
  """Drops unparseable rows and keeps the first entry per deduplication key."""
  seen_keys: set[Any] = set()
  entries: list[RankingEntryDict] = []
  for parsed in parsed_entries:
    if parsed is None:
      continue
    if parsed.dedup_key in seen_keys:
      continue
    seen_keys.add(parsed.dedup_key)
    entries.append(parsed.entry)
  return entries


def _build_fir_ranking_items(
    fir_rows: abc.Iterable[abc.Mapping[str, Any]],
    *,
    country_index: abc.Mapping[str, abc.Mapping[str, Any]],
    ranking_fields: _RankingFields,
) -> list[models.RankingItem]:
  """Parses, deduplicates, and ranks FIR rows best-first.

  Args:
    fir_rows: Raw FIR summary rows.
    country_index: Mapping from uppercase ISO-2 code to country summary row.
    ranking_fields: Resolved columns backing the ranked metric.

  Returns:
    One ranked item per distinct FIR.
  """
  fir_entries = _dedupe_ranking_entries(
      _build_single_fir_ranking_entry(
          row,
          country_index,
          field=ranking_fields.field,
          rank_field=ranking_fields.rank_field,
          secondary_field=ranking_fields.secondary_field,
      )
      for row in fir_rows
  )
  fir_entries.sort(
      key=lambda entry: _ranking_sort_key(
          entry, (constants.KEY_NAME, constants.KEY_FIR_ID)
      )
  )
  return [
      _fir_ranking_item(_display_rank(index, entry), entry)
      for index, entry in enumerate(fir_entries)
  ]


def _fir_ranking_item(
    rank: int | None, entry: RankingEntryDict
) -> models.RankingItem:
  """Converts a FIR ranking entry into a `RankingItem` with display rank."""
  return models.RankingItem(
      rank=rank,
      name=entry["name"],
      iso_a2=entry["iso_a2"],
      fir_id=entry["fir_id"],
      fir_icao=entry["fir_icao"],
      firname=entry["firname"],
      type=entry["type"],
      country_name=entry["country_name"],
      countries=entry["countries"],
      value=entry["value"],
      secondary_value=entry["secondary_value"],
  )


def _build_country_ranking_items(
    rows: abc.Iterable[abc.Mapping[str, Any]],
    *,
    ranking_fields: _RankingFields,
) -> list[models.RankingItem]:
  """Parses, deduplicates, and ranks country rows best-first.

  Args:
    rows: Raw country summary rows.
    ranking_fields: Resolved columns backing the ranked metric.

  Returns:
    One ranked item per distinct country entity.
  """
  entries = _dedupe_ranking_entries(
      _build_single_country_ranking_entry(
          row,
          field=ranking_fields.field,
          rank_field=ranking_fields.rank_field,
          secondary_field=ranking_fields.secondary_field,
      )
      for row in rows
  )
  entries.sort(
      key=lambda entry: _ranking_sort_key(
          entry, (constants.KEY_NAME, constants.KEY_ISO_A2)
      )
  )
  return [
      models.RankingItem(
          rank=_display_rank(index, entry),
          name=entry["name"],
          iso_a2=entry["iso_a2"],
          value=entry["value"],
          secondary_value=entry["secondary_value"],
      )
      for index, entry in enumerate(entries)
  ]


def _build_country_rankings_response(
    *,
    year: int,
    spec: metrics.MetricSpec,
    gwp_norm: str,
) -> models.RankingsResponse:
  """Builds or returns the cached `RankingsResponse` for a country metric."""
  dataset_clean = data_loader.active_dataset()
  rows, _ = data_loader.get_country_data_for_year(year, dataset_clean)
  cache_key = (
      data_loader.resolve_dataset_dir(year, dataset_clean),
      year,
      dataset_clean,
      spec.id,
      gwp_norm,
  )
  with _RESPONSE_CACHE_LOCK:
    cached_country = _rankings_response_cache.get(cache_key)
  if cached_country is not None and cached_country.rows is rows:
    return cached_country.response

  ranking_fields = _resolve_ranking_fields(
      spec, rows, gwp_norm=gwp_norm, year=year
  )
  items = _build_country_ranking_items(rows, ranking_fields=ranking_fields)
  country_response = models.RankingsResponse(
      metric=spec.id,
      title=spec.resolved_title(gwp_norm),
      year=year,
      gwp=gwp_norm,
      unit=spec.unit,
      secondary_unit=spec.secondary_unit,
      items=items,
  )
  with _RESPONSE_CACHE_LOCK:
    _rankings_response_cache[cache_key] = _CachedRankingsResponse(
        rows=rows, country_index=None, response=country_response
    )
  return country_response


@router.get("/rankings/{year}/{metric}", response_model=models.RankingsResponse)
def get_country_rankings(
    year: SupportedYearPath,
    metric: str = fastapi.Path(..., description="Metric ID."),
    gwp: str = fastapi.Query(
        constants.GWP50, description="GWP choice (gwp20, gwp50, gwp100)."
    ),
) -> models.RankingsResponse:
  """Returns all countries or FIRs ranked by the specified metric, best first.

  Args:
    year: Dataset year to query.
    metric: Metric ID to rank by.
    gwp: Global warming potential horizon (gwp20, gwp50, or gwp100).

  Returns:
    The ranked items along with the metric's title and unit metadata.

  Raises:
    data_loader.DataValidationError: If the year is malformed or has no
      published data.
    data_loader.ArtifactUnavailableError: If a required annual export is
      unreadable or malformed.
    metrics.UnknownMetricError: If the metric ID is not recognized.
    metrics.UnsupportedGwpError: If the horizon is not supported.
    metrics.MissingColumnError: If the dataset lacks the backing column.
  """
  gwp_norm = metrics.normalize_gwp(gwp)
  spec = metrics.lookup_metric(metrics.RANKING_METRIC_INDEX, metric)
  if spec.id in metrics.FIR_ROW_METRIC_INDEX:
    return _build_fir_rankings_response(
        year=year,
        gwp_norm=gwp_norm,
        fir_spec=spec,
    )
  return _build_country_rankings_response(
      year=year,
      spec=spec,
      gwp_norm=gwp_norm,
  )


@dataclasses.dataclass
class _TimeSeriesAccumulation:
  """Collected multi-year series points and resolved entity labels."""

  # One point per available year; `value` is None where the entity is missing.
  points: list[models.TimeSeriesPoint]
  # True if any matched row contains the requested metric column.
  column_seen: bool
  # True if the entity was found in at least one year's data.
  entity_seen: bool
  # Country ISO2 code, given or inferred from a FIR's country mapping.
  resolved_iso: str | None
  # Country display name from the first matching country row.
  resolved_country_name: str | None
  # Canonical upper-case FIR ID, given or taken from the first matching row.
  resolved_fir_id: str | None
  # Upper-case FIR ICAO code from the first matching FIR row.
  resolved_fir_icao: str | None
  # FIR display name from the first matching FIR row.
  resolved_firname: str | None


def _fetch_time_series_row(
    series_year: int,
    *,
    use_fir_rows: bool,
    iso: str | None,
    fir_id: str | None,
) -> abc.Mapping[str, Any]:
  """Looks up the country or FIR row for one year of a time series.

  Args:
    series_year: Dataset year to look up.
    use_fir_rows: True to look up a FIR row by `fir_id`, False to look up a
      country row by `iso`.
    iso: Resolved country ISO-2 code, or None.
    fir_id: Canonical FIR ID, or None.

  Returns:
    The matching row, or an empty mapping if the entity is absent from that
    year.

  Raises:
    data_loader.ArtifactUnavailableError: If the year's data cannot be loaded.
  """
  if use_fir_rows:
    row = data_loader.get_fir_row_by_identifier(series_year, fir_id=fir_id)
    return row if row is not None else {}
  _, iso_index = data_loader.get_country_data_for_year(series_year)
  row = iso_index.get(iso) if iso is not None else None
  return row if row is not None else {}


def _record_fir_row_labels(
    accumulation: _TimeSeriesAccumulation, match_row: abc.Mapping[str, Any]
) -> None:
  """Fills unset FIR identity labels in `accumulation` from a matched row."""
  if not accumulation.resolved_fir_icao and (
      fir_icao := match_row.get(constants.KEY_FIR_ICAO)
  ):
    accumulation.resolved_fir_icao = str(fir_icao).strip().upper()
  if not accumulation.resolved_firname and (
      firname := match_row.get(constants.KEY_FIRNAME)
  ):
    accumulation.resolved_firname = str(firname).strip()
  if not accumulation.resolved_iso:
    fir_isos = _controlling_country_iso2s(
        match_row, artifact=constants.FIR_EXPORT_FILENAME
    )
    # The FIR's primary controlling country.
    accumulation.resolved_iso = next(iter(fir_isos), None)


def _record_entity_labels(
    accumulation: _TimeSeriesAccumulation,
    match_row: abc.Mapping[str, Any],
    *,
    use_fir_rows: bool,
) -> None:
  """Fills unset entity identity labels in `accumulation` from `match_row`."""
  if use_fir_rows and match_row:
    _record_fir_row_labels(accumulation, match_row)
  elif match_row and not accumulation.resolved_country_name:
    accumulation.resolved_country_name = match_row.get(constants.KEY_NAME)


def _collect_time_series_points(
    *,
    available_years: abc.Sequence[int],
    field: str,
    use_fir_rows: bool,
    resolved_iso: str | None,
    resolved_fir_id: str | None,
) -> _TimeSeriesAccumulation:
  """Collects per-year metric values and resolves entity metadata."""
  accumulation = _TimeSeriesAccumulation(
      points=[],
      column_seen=False,
      entity_seen=False,
      resolved_iso=resolved_iso,
      resolved_country_name=None,
      resolved_fir_id=resolved_fir_id,
      resolved_fir_icao=None,
      resolved_firname=None,
  )
  for series_year in available_years:
    try:
      match_row = _fetch_time_series_row(
          series_year,
          use_fir_rows=use_fir_rows,
          iso=accumulation.resolved_iso,
          fir_id=accumulation.resolved_fir_id,
      )
    except data_loader.ArtifactUnavailableError as e:
      # One unreadable year must not hide the others: it becomes a null point.
      logging.warning(
          "Could not load time series row for year %s: %s", series_year, e
      )
      accumulation.points.append(
          models.TimeSeriesPoint(year=series_year, value=None)
      )
      continue

    _record_entity_labels(accumulation, match_row, use_fir_rows=use_fir_rows)
    _record_series_point(
        accumulation, series_year=series_year, match_row=match_row, field=field
    )
  return accumulation


def _record_series_point(
    accumulation: _TimeSeriesAccumulation,
    *,
    series_year: int,
    match_row: abc.Mapping[str, Any],
    field: str,
) -> None:
  """Appends one year's point and updates the entity/column seen flags.

  Args:
    accumulation: Series accumulation to update in place.
    series_year: Dataset year of the point.
    match_row: Matching row mapping for that year, or empty if absent.
    field: Resolved metric column name.
  """
  if match_row:
    accumulation.entity_seen = True
    if field in match_row:
      accumulation.column_seen = True
  value = metrics.coerce_float(match_row.get(field)) if match_row else None
  accumulation.points.append(
      models.TimeSeriesPoint(year=series_year, value=value)
  )


def _validate_time_series_result(
    accumulation: _TimeSeriesAccumulation,
    *,
    spec: metrics.MetricSpec,
    gwp_norm: str,
    field: str,
) -> None:
  """Raises HTTPException (404 or 422) if time-series results are missing."""
  target = accumulation.resolved_fir_id or accumulation.resolved_iso
  if not accumulation.entity_seen:
    raise fastapi.HTTPException(
        status_code=404, detail=f"No time-series data found for '{target}'."
    )
  if not accumulation.column_seen:
    raise fastapi.HTTPException(
        status_code=422,
        detail=(
            f"Metric {spec.id!r} at {gwp_norm} is backed by column {field!r}, "
            f"which dataset {data_loader.active_dataset()!r} does not provide "
            "for any supported year. No substitute horizon is served. Re-run "
            "the extraction pipeline for this dataset, or request a horizon it "
            "does provide."
        ),
    )
  if all(point.value is None for point in accumulation.points):
    raise fastapi.HTTPException(
        status_code=404, detail=f"No time-series data found for '{target}'."
    )


def _resolve_time_series_spec(
    metric: str, *, fir_selected: bool
) -> metrics.MetricSpec:
  """Resolves the metric spec backing a `/series` request.

  Args:
    metric: Metric ID as requested.
    fir_selected: True if the request addresses a FIR, in which case the metric
      is projected onto the equivalent per-FIR column.

  Returns:
    The FIR-row or country metric spec.

  Raises:
    fastapi.HTTPException: 400 if a FIR-only metric is requested without a FIR.
    metrics.UnknownMetricError: If the metric ID is not recognized.
  """
  metric_clean = (metric or "").lower().strip()
  if metric_clean in metrics.FIR_ROW_METRIC_INDEX and not fir_selected:
    raise fastapi.HTTPException(
        status_code=400,
        detail=f"Metric {metric_clean!r} requires '{constants.KEY_FIR_ID}'.",
    )
  if fir_selected:
    return metrics.project_to_fir_row_metric(metric_clean)
  return metrics.lookup_metric(metrics.COUNTRY_METRIC_INDEX, metric_clean)


def _build_time_series_response(
    accumulation: _TimeSeriesAccumulation,
    *,
    spec: metrics.MetricSpec,
    gwp_norm: str,
) -> models.TimeSeriesResponse:
  """Assembles the `/series` response from validated series points."""
  return models.TimeSeriesResponse(
      metric=spec.id,
      metric_title=spec.resolved_title(gwp_norm),
      unit=spec.unit,
      iso_a2=accumulation.resolved_iso,
      country_name=accumulation.resolved_country_name,
      fir_id=accumulation.resolved_fir_id,
      fir_icao=accumulation.resolved_fir_icao,
      firname=accumulation.resolved_firname,
      gwp=gwp_norm,
      points=accumulation.points,
  )


@router.get("/series/{metric}", response_model=models.TimeSeriesResponse)
def get_time_series(
    metric: str = fastapi.Path(..., description="Metric ID."),
    *,
    iso: str | None = fastapi.Query(None, description="Country ISO code."),
    fir_id: str | None = fastapi.Query(None, description="Canonical FIR ID."),
    gwp: str = fastapi.Query(
        constants.GWP50, description="GWP choice (gwp20, gwp50, gwp100)."
    ),
) -> models.TimeSeriesResponse:
  """Returns the multi-year series for a metric and a country or FIR.

  Args:
    metric: Metric ID. When `fir_id` is supplied, any metric with unit
      `tonnes_co2e` or `milliwatts_per_square_metre` is projected onto the
      equivalent per-FIR column.
    iso: Optional country ISO identifier.
    fir_id: Optional canonical FIR ID.
    gwp: Global warming potential horizon (gwp20, gwp50, or gwp100).

  Returns:
    One point per available year, with a null value for years the entity is
    missing from.

  Raises:
    fastapi.HTTPException: 400 if no entity selector is supplied or a FIR-row
      metric is requested without `fir_id`; 404 if the entity is never seen or
      all points are null; 422 if the backing column is absent across all
      available years.
    data_loader.DataValidationError: If `iso` is malformed or `fir_id` is
      longer than 128 characters.
    metrics.UnknownMetricError: If the metric ID is not recognized.
    metrics.UnsupportedGwpError: If the horizon is not supported.
  """
  resolved_iso: str | None = data_loader.resolve_iso_code(iso) if iso else None
  resolved_fir_id: str | None = (
      data_loader.validate_fir_id(fir_id) if fir_id else None
  )
  if not resolved_iso and not resolved_fir_id:
    raise fastapi.HTTPException(
        status_code=400,
        detail=(
            f"Must provide at least one of 'iso' or '{constants.KEY_FIR_ID}'."
        ),
    )

  gwp_norm = metrics.normalize_gwp(gwp)
  spec = _resolve_time_series_spec(metric, fir_selected=bool(resolved_fir_id))
  field = spec.resolve(spec.field, gwp_norm)

  accumulation = _collect_time_series_points(
      available_years=data_loader.get_available_years(),
      field=field,
      use_fir_rows=bool(resolved_fir_id),
      resolved_iso=resolved_iso,
      resolved_fir_id=resolved_fir_id,
  )
  _validate_time_series_result(
      accumulation,
      spec=spec,
      gwp_norm=gwp_norm,
      field=field,
  )
  return _build_time_series_response(accumulation, spec=spec, gwp_norm=gwp_norm)


@router.get("/grid/{year}/{prefix}/meta")
def get_grid_meta(
    year: FormattedYearPath,
    prefix: str,
    lod: int | None = fastapi.Query(
        None,
        description=(
            "Optional level of detail: 0 (4x downsampled) or 1 (2x"
            " downsampled); any other value or none serves full"
            " resolution."
        ),
    ),
) -> fastapi.responses.JSONResponse:
  """Returns the spatial metadata header for a year and grid prefix.

  Args:
    year: Dataset year to query.
    prefix: Grid artifact prefix, for example "total_global_impact_grid".
    lod: Optional level of detail. Levels 0 and 1 are served from downsampled
      artifacts; any other value falls back to full resolution.

  Returns:
    The metadata header, cacheable for an hour.

  Raises:
    fastapi.HTTPException: 404 if no artifact matches.
    data_loader.DataValidationError: If the year or prefix is malformed.
  """
  prefix_clean = prefix.strip()
  data = data_loader.get_grid_meta(
      year=year,
      prefix=prefix_clean,
      lod=lod,
  )
  if data is None:
    raise fastapi.HTTPException(
        status_code=404,
        detail=(
            f"Grid metadata for prefix '{prefix_clean}' and year '{year}'"
            " not found."
        ),
    )
  return fastapi.responses.JSONResponse(
      content=data,
      headers={static_assets.HEADER_CACHE_CONTROL: _CACHE_CONTROL_ONE_HOUR},
  )


class _EncodedGridPayload(NamedTuple):
  """Grid response body and headers after content-encoding negotiation."""

  content: bytes
  headers: dict[str, str]


def _encode_grid_payload(
    payload: bytes, request: fastapi.Request | None
) -> _EncodedGridPayload:
  """Picks gzip or identity encoding for a gzipped grid payload.

  Args:
    payload: Gzip-compressed grid buffer.
    request: The incoming request, consulted for its Accept-Encoding header.

  Returns:
    The payload as-is with `Content-Encoding: gzip` when the client accepts
    gzip or sends no `Accept-Encoding` header, otherwise the decompressed
    payload. Headers always carry the cache policy and `Vary: Accept-Encoding`.

  Raises:
    data_loader.ArtifactUnavailableError: If `payload` cannot be decompressed
      when identity encoding is requested.
  """
  headers = {
      static_assets.HEADER_CACHE_CONTROL: _CACHE_CONTROL_ONE_HOUR,
      static_assets.HEADER_VARY: static_assets.HEADER_ACCEPT_ENCODING,
  }
  if static_assets.accepts_gzip_encoding(request, default_when_missing=True):
    headers[static_assets.HEADER_CONTENT_ENCODING] = static_assets.ENCODING_GZIP
  else:
    try:
      payload = gzip.decompress(payload)
    except (OSError, EOFError, zlib.error) as e:
      logging.exception("Failed to decompress grid binary payload: %s", e)
      raise data_loader.ArtifactUnavailableError(
          "Failed to decompress grid binary payload."
      ) from e
  return _EncodedGridPayload(content=payload, headers=headers)


@router.get("/grid/{year}/{prefix}/binary")
def get_grid_binary(
    year: FormattedYearPath,
    *,
    prefix: str,
    request: fastapi.Request,
    lod: int | None = fastapi.Query(
        None,
        description=(
            "Optional level of detail: 0 (4x downsampled) or 1 (2x"
            " downsampled); any other value or none serves full"
            " resolution."
        ),
    ),
) -> fastapi.responses.Response:
  """Returns the raw grid buffer for a year and grid prefix.

  The payload is gzipped when the request accepts gzip or sends no
  Accept-Encoding header, in which case the Content-Encoding header is set
  accordingly. The X-Grid-Meta header is omitted when the run has no grid
  metadata.

  Args:
    year: Dataset year to query.
    prefix: Grid artifact prefix, for example "total_global_impact_grid".
    request: Incoming request, read only for its Accept-Encoding header.
    lod: Optional level of detail. Levels 0 and 1 are served from downsampled
      artifacts; any other value falls back to full resolution.

  Returns:
    The binary buffer as an octet stream, cacheable for an hour.

  Raises:
    fastapi.HTTPException: 404 if no artifact matches.
    data_loader.DataValidationError: If the year or prefix is malformed.
    data_loader.ArtifactUnavailableError: If the stored payload fails gzip
      decompression for an `Accept-Encoding: identity` request.
  """
  prefix_clean = prefix.strip()
  payload = data_loader.get_grid_binary_payload(
      year=year,
      prefix=prefix_clean,
      lod=lod,
  )
  if payload is None:
    raise fastapi.HTTPException(
        status_code=404,
        detail=(
            f"Grid binary buffer for prefix '{prefix_clean}' and year"
            f" '{year}' not found."
        ),
    )
  encoded = _encode_grid_payload(payload, request)
  meta_json = data_loader.get_grid_meta_json(
      year=year,
      prefix=prefix_clean,
      lod=lod,
  )
  if meta_json is not None:
    encoded.headers[_HEADER_X_GRID_META] = meta_json
  return fastapi.responses.Response(
      content=encoded.content,
      media_type=static_assets.MEDIA_TYPE_OCTET_STREAM,
      headers=encoded.headers,
  )


@router.get(
    "/grid/{year}/availability",
    response_model=models.GridAvailabilityResponse,
)
def get_grid_availability(
    year: FormattedYearPath,
) -> models.GridAvailabilityResponse:
  """Reports which total, monthly, and hourly grids exist for a year.

  Args:
    year: Dataset year to query.

  Returns:
    The availability flags for each grid family.
  """
  availability = data_loader.get_grid_availability(year=year)
  return models.GridAvailabilityResponse.model_validate(availability)


# Core 24x12 matrices required on every published temporal bundle row.
_REQUIRED_TEMPORAL_MATRIX_FIELDS = (
    constants.KEY_CO2E_GWP50_TONNES,
    constants.KEY_SEGMENT_COUNT,
    constants.KEY_CONTRAIL_SEGMENT_COUNT,
)

_TEMPORAL_COUNT_MATRIX_FIELDS = (
    constants.KEY_SEGMENT_COUNT,
    constants.KEY_CONTRAIL_SEGMENT_COUNT,
)

_TEMPORAL_FLOAT_MATRIX_FIELDS = (
    constants.KEY_CO2E_GWP20_TONNES,
    constants.KEY_CO2E_GWP50_TONNES,
    constants.KEY_CO2E_GWP100_TONNES,
    constants.KEY_TOTAL_FLIGHT_DISTANCE_KM,
    constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM,
)

# All 24x12 (hours x months) matrices supported by TemporalMatrixResponse;
# see `metric_derivations._make_empty_temporal_bundle`.
_TEMPORAL_MATRIX_FIELDS = (
    *_TEMPORAL_FLOAT_MATRIX_FIELDS,
    *_TEMPORAL_COUNT_MATRIX_FIELDS,
)


class _SummedTemporalMatrices(NamedTuple):
  """Summed 24x12 float and integer count matrices for a temporal entity."""

  float_matrices: dict[str, list[list[float]]]
  count_matrices: dict[str, list[list[int]]]


def _safe_temporal_count(value: Any) -> int:
  """Coerces a temporal count cell to `int`, defaulting invalid values to 0."""
  if isinstance(value, bool):
    return 0
  if isinstance(value, int):
    return value
  return round(data_loader.safe_rank_metric(value))


def _add_temporal_float_matrix(
    dest: list[list[float]], matrix: abc.Sequence[Any]
) -> None:
  """Adds the leading 24x12 float cells of `matrix` into `dest`, in place."""
  for hour_idx, hour_values in enumerate(matrix[:24]):
    for month_idx, value in enumerate(hour_values[:12]):
      dest[hour_idx][month_idx] += data_loader.safe_rank_metric(value)


def _add_temporal_count_matrix(
    dest: list[list[int]], matrix: abc.Sequence[Any]
) -> None:
  """Adds the leading 24x12 integer cells of `matrix` into `dest`, in place."""
  for hour_idx, hour_values in enumerate(matrix[:24]):
    for month_idx, value in enumerate(hour_values[:12]):
      dest[hour_idx][month_idx] += _safe_temporal_count(value)


def _is_valid_temporal_matrix(matrix: Any) -> bool:
  """Returns whether `matrix` has at least 24 12-month sequence rows."""
  if not isinstance(matrix, (list, tuple)) or len(matrix) < 24:
    return False
  return all(
      isinstance(row, (list, tuple)) and len(row) >= 12 for row in matrix[:24]
  )


def _validate_temporal_row_matrices(
    row: abc.Mapping[str, Any], *, artifact: str
) -> None:
  """Raises `ArtifactUnavailableError` if a temporal row's matrices are bad."""
  invalid = [
      field
      for field in _REQUIRED_TEMPORAL_MATRIX_FIELDS
      if not _is_valid_temporal_matrix(row.get(field))
  ]
  invalid.extend(
      field
      for field in _TEMPORAL_MATRIX_FIELDS
      if field not in _REQUIRED_TEMPORAL_MATRIX_FIELDS
      and row.get(field) is not None
      and not _is_valid_temporal_matrix(row.get(field))
  )
  if invalid:
    raise _malformed_row_error(row, artifact=artifact, fields=invalid)


def _sum_temporal_matrices(
    rows: abc.Iterable[abc.Mapping[str, Any]],
    *,
    artifact: str = constants.COUNTRY_TEMPORAL_EXPORT_FILENAME,
) -> _SummedTemporalMatrices:
  """Sums the 24x12 matrices of temporal bundle rows, per field."""
  float_sums = {
      field: [[0.0] * 12 for _ in range(24)]
      for field in _TEMPORAL_FLOAT_MATRIX_FIELDS
  }
  count_sums = {
      field: [[0] * 12 for _ in range(24)]
      for field in _TEMPORAL_COUNT_MATRIX_FIELDS
  }
  for row in rows:
    _validate_temporal_row_matrices(row, artifact=artifact)
    for field, dest in float_sums.items():
      matrix = row.get(field)
      if isinstance(matrix, (list, tuple)):
        _add_temporal_float_matrix(dest, matrix)
    for field, dest in count_sums.items():
      matrix = row.get(field)
      if isinstance(matrix, (list, tuple)):
        _add_temporal_count_matrix(dest, matrix)
  return _SummedTemporalMatrices(
      float_matrices=float_sums,
      count_matrices=count_sums,
  )


def _build_temporal_matrix_response(
    matrices: _SummedTemporalMatrices,
    *,
    year: int,
    iso_a2: str,
    name: str,
    is_fir: bool,
    fir_id: str | None = None,
    fir_icao: str | None = None,
    fir_name: str | None = None,
) -> models.TemporalMatrixResponse:
  """Builds a `TemporalMatrixResponse` from summed 24x12 matrices.

  Args:
    matrices: Summed hour-by-month matrices from `_sum_temporal_matrices`.
    year: Dataset year.
    iso_a2: Resolved ISO-2 country code.
    name: Display title for the entity.
    is_fir: True when the matrix represents an FIR rather than a country.
    fir_id: Canonical FIR ID when `is_fir` is True, or `None`.
    fir_icao: FIR ICAO code when `is_fir` is True, or `None`.
    fir_name: Cleaned FIR name when `is_fir` is True, or `None`.

  Returns:
    A populated `models.TemporalMatrixResponse`.
  """
  float_matrices = matrices.float_matrices
  count_matrices = matrices.count_matrices
  return models.TemporalMatrixResponse(
      year=year,
      iso_a2=iso_a2,
      name=name,
      fir_id=fir_id,
      fir_icao=fir_icao,
      fir_name=fir_name,
      is_fir=is_fir,
      months=[f"{year}{month_idx:02d}" for month_idx in range(1, 13)],
      hours=[f"{hour_idx:02d}:00" for hour_idx in range(24)],
      co2e_gwp20_tonnes=float_matrices[constants.KEY_CO2E_GWP20_TONNES],
      co2e_gwp50_tonnes=float_matrices[constants.KEY_CO2E_GWP50_TONNES],
      co2e_gwp100_tonnes=float_matrices[constants.KEY_CO2E_GWP100_TONNES],
      segment_count=count_matrices[constants.KEY_SEGMENT_COUNT],
      contrail_segment_count=count_matrices[
          constants.KEY_CONTRAIL_SEGMENT_COUNT
      ],
      total_flight_distance_km=float_matrices[
          constants.KEY_TOTAL_FLIGHT_DISTANCE_KM
      ],
      contrail_flight_distance_km=float_matrices[
          constants.KEY_CONTRAIL_FLIGHT_DISTANCE_KM
      ],
  )


class _MatchedFirTemporalRows(NamedTuple):
  """Matching FIR temporal rows and first-row metadata."""

  rows: list[abc.Mapping[str, Any]]
  fir_id: str | None
  fir_name: str | None
  fir_icao: str | None
  fir_iso: str | None


def _extract_matching_fir_temporal_rows(
    fir_raw_data: abc.Sequence[abc.Mapping[str, Any]],
    fir_id_clean: str,
) -> _MatchedFirTemporalRows:
  """Filters FIR temporal rows matching a canonical FIR ID.

  Args:
    fir_raw_data: All FIR temporal rows for the year and dataset.
    fir_id_clean: Canonical FIR identifier (uppercased).

  Returns:
    A `_MatchedFirTemporalRows` instance drawn from the first matching row.
    All fields are empty or None when nothing matches.
  """
  fir_id_upper = fir_id_clean.strip().upper()
  matched_fir_id = None
  matched_fir_name = None
  matched_fir_icao = None
  matched_fir_iso = None
  matching_fir_rows: list[abc.Mapping[str, Any]] = []

  for row in fir_raw_data:
    row_fir_id = str(row.get(constants.KEY_FIR_ID) or "").strip().upper()
    if row_fir_id != fir_id_upper:
      continue

    row_icao = str(row.get(constants.KEY_FIR_ICAO) or "").strip().upper()
    row_name = str(row.get(constants.KEY_FIRNAME) or "").strip()
    if matched_fir_id is None:
      matched_fir_id = row_fir_id
      matched_fir_name = row_name
      matched_fir_icao = row_icao
      fir_isos = _controlling_country_iso2s(
          row, artifact=constants.FIR_TEMPORAL_EXPORT_FILENAME
      )
      matched_fir_iso = next(iter(fir_isos), None)

    matching_fir_rows.append(row)

  return _MatchedFirTemporalRows(
      rows=matching_fir_rows,
      fir_id=matched_fir_id,
      fir_name=matched_fir_name,
      fir_icao=matched_fir_icao,
      fir_iso=matched_fir_iso,
  )


class _MatchedCountryTemporalRows(NamedTuple):
  """Matching country temporal rows and the first row's display name."""

  rows: list[abc.Mapping[str, Any]]
  country_name: str | None


def _extract_matching_country_temporal_rows(
    raw_data: abc.Sequence[abc.Mapping[str, Any]],
    iso_clean: str,
) -> _MatchedCountryTemporalRows:
  """Filters country temporal rows matching an ISO-2 country code.

  Args:
    raw_data: All country temporal rows for the year and dataset.
    iso_clean: Uppercased ISO alpha-2 code.

  Returns:
    A `_MatchedCountryTemporalRows` instance. `country_name` is None when
    nothing matched.
  """
  matching_rows = [
      row
      for row in raw_data
      if str(row.get(constants.KEY_ISO_A2) or "").strip().upper() == iso_clean
  ]
  matched_name = None
  if matching_rows:
    first_row, *_ = matching_rows
    matched_name = str(first_row.get(constants.KEY_NAME) or "").strip()
  return _MatchedCountryTemporalRows(
      rows=matching_rows, country_name=matched_name
  )


def _try_build_fir_temporal_response(
    *,
    year: int,
    dataset_clean: str,
    fir_id_clean: str,
    iso_clean: str | None,
    cache_key: data_loader.TemporalCacheKey,
) -> models.TemporalMatrixResponse | None:
  """Returns FIR temporal response when `fir_id_clean` matches, or `None`."""
  fir_raw_data = data_loader.get_fir_temporal_raw_data(year, dataset_clean)
  matched_fir = _extract_matching_fir_temporal_rows(fir_raw_data, fir_id_clean)
  if not matched_fir.rows:
    return None
  temporal_response = _build_temporal_matrix_response(
      _sum_temporal_matrices(
          matched_fir.rows,
          artifact=constants.FIR_TEMPORAL_EXPORT_FILENAME,
      ),
      year=year,
      iso_a2=matched_fir.fir_iso or iso_clean or "",
      name=_fir_temporal_display_name(matched_fir, fir_id_clean),
      is_fir=True,
      fir_id=matched_fir.fir_id or fir_id_clean,
      fir_icao=matched_fir.fir_icao or "",
      fir_name=matched_fir.fir_name or "",
  )
  data_loader.cache_temporal_matrix(cache_key, temporal_response)
  return temporal_response


def _fir_temporal_display_name(
    matched_fir: _MatchedFirTemporalRows, fir_id_clean: str
) -> str:
  """Returns the display title for a matched FIR temporal response.

  Args:
    matched_fir: Matching FIR temporal rows and first-row metadata.
    fir_id_clean: Canonical FIR ID requested, used as the last fallback.

  Returns:
    "FIR <name> (<icao>)" when both are known, else whichever of the name,
    ICAO code, or requested ID is available first.
  """
  if matched_fir.fir_name and matched_fir.fir_icao:
    return f"FIR {matched_fir.fir_name} ({matched_fir.fir_icao})"
  return matched_fir.fir_name or matched_fir.fir_icao or fir_id_clean


def _find_known_country_row(
    *, year: int, dataset_clean: str, iso_clean: str
) -> abc.Mapping[str, Any]:
  """Returns the year's country export row for an ISO code.

  Args:
    year: Supported dataset year.
    dataset_clean: Active dataset name.
    iso_clean: Uppercased ISO alpha-2 code.

  Returns:
    The first per-country export row whose `iso_a2` is `iso_clean`.

  Raises:
    fastapi.HTTPException: 404 if no per-country row carries `iso_clean`.
  """
  _, iso_index = data_loader.get_country_data_for_year(year, dataset_clean)
  country_row = iso_index.get(iso_clean)
  if country_row is None:
    raise fastapi.HTTPException(
        status_code=404,
        detail=(
            f"Country with ISO identifier '{iso_clean}' not found for year"
            f" '{year}'."
        ),
    )
  return country_row


def _build_country_temporal_response(
    *,
    run_dir: str | None,
    year: int,
    dataset_clean: str,
    iso_clean: str,
) -> models.TemporalMatrixResponse:
  """Builds and caches the temporal matrix response for one country.

  Args:
    run_dir: Resolved run directory for `(year, dataset_clean)`.
    year: Supported dataset year.
    dataset_clean: Active dataset name.
    iso_clean: Uppercased ISO alpha-2 code.

  Returns:
    The country's summed hour-by-month matrices. A known country without
    temporal rows gets all-zero matrices, titled with its export name.

  Raises:
    fastapi.HTTPException: 404 if `iso_clean` is not a country of the year.
  """
  country_cache_key = (run_dir, year, dataset_clean, iso_clean, None)
  cached_country = data_loader.get_cached_temporal_matrix(country_cache_key)
  if isinstance(cached_country, models.TemporalMatrixResponse):
    return cached_country

  country_row = _find_known_country_row(
      year=year, dataset_clean=dataset_clean, iso_clean=iso_clean
  )
  raw_data = data_loader.get_temporal_raw_data(year, dataset_clean)
  matched_country = _extract_matching_country_temporal_rows(raw_data, iso_clean)
  export_name = str(country_row.get(constants.KEY_NAME) or "").strip()
  temporal_response = _build_temporal_matrix_response(
      _sum_temporal_matrices(
          matched_country.rows,
          artifact=constants.COUNTRY_TEMPORAL_EXPORT_FILENAME,
      ),
      year=year,
      iso_a2=iso_clean,
      name=matched_country.country_name or export_name or iso_clean,
      is_fir=False,
  )
  data_loader.cache_temporal_matrix(country_cache_key, temporal_response)
  return temporal_response


def _normalize_temporal_iso(iso: str | None) -> str | None:
  """Resolves the optional `/temporal` ISO filter.

  Args:
    iso: Raw `iso` query parameter.

  Returns:
    The canonical ISO-2 code, or None if `iso` is omitted or blank.

  Raises:
    data_loader.DataValidationError: If the ISO code contains disallowed
      characters or exceeds 64 characters.
  """
  if iso is None or not iso.strip():
    return None
  return data_loader.resolve_iso_code(iso)


def _compute_temporal_response(
    *,
    run_dir: str | None,
    year: int,
    dataset_clean: str,
    iso_clean: str | None,
    fir_id_clean: str | None,
) -> models.TemporalMatrixResponse:
  """Builds an uncached temporal response, trying the FIR filter first.

  Args:
    run_dir: Resolved run directory for `(year, dataset_clean)`.
    year: Supported dataset year.
    dataset_clean: Active dataset name.
    iso_clean: Resolved ISO-2 code, or None if only `fir_id` was given.
    fir_id_clean: Validated canonical FIR ID, or None if only `iso` was given.

  Returns:
    The FIR temporal response if a FIR was requested and matched, else the
    country response.

  Raises:
    fastapi.HTTPException: 404 if a FIR-only query matches no FIR, or if the
      country is not part of the year's country export.
  """
  if fir_id_clean:
    fir_response = _try_build_fir_temporal_response(
        year=year,
        dataset_clean=dataset_clean,
        fir_id_clean=fir_id_clean,
        iso_clean=iso_clean,
        cache_key=(
            run_dir,
            year,
            dataset_clean,
            iso_clean,
            fir_id_clean,
        ),
    )
    if fir_response is not None:
      return fir_response

  if iso_clean is None:
    raise fastapi.HTTPException(
        status_code=404,
        detail=f"FIR '{fir_id_clean}' not found for year '{year}'.",
    )
  return _build_country_temporal_response(
      run_dir=run_dir,
      year=year,
      dataset_clean=dataset_clean,
      iso_clean=iso_clean,
  )


@router.get("/temporal/{year}", response_model=models.TemporalMatrixResponse)
def get_temporal_metrics(
    year: SupportedYearPath,
    iso: str | None = fastapi.Query(
        None,
        max_length=64,
        description=(
            "ISO alpha-2 code of the country to aggregate. Required unless"
            " `fir_id` is given."
        ),
    ),
    fir_id: str | None = fastapi.Query(
        None,
        max_length=128,
        description=(
            "Canonical FIR ID. If provided and data is available, returns FIR"
            " temporal analysis. Required unless `iso` is given."
        ),
    ),
) -> models.TemporalMatrixResponse:
  """Returns the 24x12 temporal matrix of impact and segment counts.

  Every request needs `iso`, `fir_id`, or both (FIR with country fallback).
  There is no aggregate over all countries. Results are memoized per (run_dir,
  year, dataset, iso, fir_id). A FIR filter is attempted first; when no FIR
  temporal row matches `fir_id`, the request falls back to country aggregation
  if `iso` is also provided, or raises 404 otherwise.

  Args:
    year: Dataset year to query.
    iso: Country ISO alpha-2 code, case-insensitive. Must name a country of the
      year's country export.
    fir_id: Canonical FIR ID.

  Returns:
    The hour-by-month matrices along with the resolved entity labels.

  Raises:
    data_loader.DataValidationError: If the year or entity identifier is
      malformed or unsupported, or if neither `iso` nor `fir_id` is given
      (mapped to HTTP 400).
    data_loader.ArtifactUnavailableError: If a required temporal or country
      export is unreadable or malformed (mapped to HTTP 503).
    fastapi.HTTPException: 404 if a FIR-only query matches no FIR, or if
      `iso` names no country of the year.
  """
  dataset_clean = data_loader.active_dataset()

  fir_id_clean = data_loader.validate_fir_id(fir_id)
  iso_clean = _normalize_temporal_iso(iso)
  if iso_clean is None and fir_id_clean is None:
    raise data_loader.DataValidationError(
        "Temporal queries require an 'iso' or 'fir_id' parameter."
    )

  run_dir = data_loader.resolve_dataset_dir(year, dataset_clean)
  cache_key = (run_dir, year, dataset_clean, iso_clean, fir_id_clean)
  cached = data_loader.get_cached_temporal_matrix(cache_key)
  if cached is not None:
    return cached

  return _compute_temporal_response(
      run_dir=run_dir,
      year=year,
      dataset_clean=dataset_clean,
      iso_clean=iso_clean,
      fir_id_clean=fir_id_clean,
  )


# Data routes come first, so the static asset routes below cannot shadow them.
app.include_router(router)


# Static assets routes
app.add_api_route(
    "/",
    static_assets.serve_index,
    methods=["GET"],
    response_class=fastapi.responses.FileResponse,
)
app.add_api_route(
    "/index.html",
    static_assets.serve_index,
    methods=["GET"],
    response_class=fastapi.responses.FileResponse,
)
app.add_api_route(
    "/favicon.ico",
    static_assets.serve_favicon,
    methods=["GET"],
    response_class=fastapi.responses.FileResponse,
)
app.add_api_route(
    "/favicon.svg",
    static_assets.serve_favicon_svg,
    methods=["GET"],
    response_class=fastapi.responses.FileResponse,
)
app.add_api_route(
    "/assets/{asset_path:path}",
    static_assets.serve_vite_asset,
    methods=["GET"],
    response_class=fastapi.responses.Response,
)
# The boundary GeoJSON routes (`/countries/{year}.json`, `/firs/{year}.json`)
# are registered near the top of this module, ahead of `/countries/{year}` and
# `/firs/{year}`.


def main(argv: abc.Sequence[str]) -> None:
  """Entry point for running the API server binary."""
  del argv  # Unused.
  try:
    data_loader.ensure_dataset_configured()
  except data_loader.DatasetConfigError as e:
    # UsageError prints the message and exits non-zero without a traceback.
    raise absl_app.UsageError(str(e)) from e
  port = int(os.environ.get(_PORT_ENV_VAR, _DEFAULT_PORT))
  host = os.environ.get(_HOST_ENV_VAR, _DEFAULT_HOST)
  uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
  absl_app.run(main)
