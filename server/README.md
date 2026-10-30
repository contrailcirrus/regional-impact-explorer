# Regional contrail dashboard server

FastAPI service that serves regional contrail climate-impact metrics, rankings,
multi-year time series, 24x12 hour-by-month temporal matrices, float16
log-scaled spatial grid buffers, run-scoped boundary GeoJSONs, and the dashboard
web UI.

## Overview

The server reads immutable dataset runs produced by `data_extraction/` using the
shared `common/` package (`common/constants.py`, `common/fs.py`, and
`common/artifact_io.py`), without depending on `data_extraction/` or heavy
pipeline libraries (`apache-beam`, `numpy`, `pandas`, `pyarrow`, `shapely`). For
each four-digit `{year}` directory under the configured data root, it resolves
`{data_dir}/{year}/{dataset}/latest.json` to locate the active `runs/{run_id}/`
directory.

*   **Run-scoped snapshot caching:** Tabular, spatial grid, and boundary caches
    are keyed on the resolved `runs/{run_id}/` path. The `latest.json` pointer
    is read once per `(year, dataset)` and memoized for the life of the
    process, and the list of available years is discovered once on first use.
    All artifacts served for a year therefore come from the same run snapshot.
    To serve a newly published run or a new year, restart the server.
*   **Startup preloading:** During application startup, a background thread pool
    warms the per-country, FIR, FIR index, country temporal, FIR temporal, grid
    availability, and initial total-impact grid metadata/binary caches across
    all discovered years so initial interactive requests avoid cold filesystem
    reads.

## Requirements

*   Python 3.11+
*   `fastapi>=0.100.0`, `pydantic>=2.0.0`, `uvicorn>=0.23.0`, `absl-py>=2.0.0`,
    `google-cloud-storage>=2.14.0`: the package's core dependencies, installed
    by `pip install -e .`. A `gs://` `REGIONAL_CONTRAILS_DATA_DIR` is read with
    the GCS client (`common/gcs_backend.py`, registered at startup by
    `data_loader.configure_from_env`) using Application Default Credentials;
    Apache Beam is not needed.

## Running the server

Run the server from the repository root (or from any directory after installing
with `pip install -e .`) with
`REGIONAL_CONTRAILS_DATASET` and `REGIONAL_CONTRAILS_DATA_DIR` set:

```sh
export REGIONAL_CONTRAILS_DATASET=my_dataset
export REGIONAL_CONTRAILS_DATA_DIR=/data/out
python -m server.server
```

The dashboard at `/` needs a production frontend build. Run `npm install` and
`npm run build` in `ui/` first, which writes `ui/dist/`. Without a build, `/`
serves the unbuilt `ui/index.html`, which does not render in a browser; because
static file resolution (`resolve_static_file` and `load_static_payload_cached`)
is memoized for the life of the process, if `/` is requested before `npm run
build` (or if `ui/dist/` is rebuilt while the server is running), restart the
server to pick up the new bundle. During frontend development use `npm run dev`
instead (see the `ui` README). The API routes work either way.

To serve a prebuilt Vite frontend bundle from a custom path (by default
`<package_root>/ui/dist` is used automatically when present):

```sh
export CONTRAILS_UI_DIST_DIR=/path/to/ui/dist
python -m server.server
```

## Mounting in another application

The API endpoints are on `server.router`, so another FastAPI application can
serve them under a prefix. `server.app` is only the standalone server around
it. A host application:

```python
from server import data_loader, server

# At startup: reads REGIONAL_CONTRAILS_DATASET (raising DatasetConfigError if
# it is unset) and sets up a gs:// REGIONAL_CONTRAILS_DATA_DIR to be read
# without Beam.
data_loader.configure_from_env()
app.include_router(server.router, prefix="/internal/regional")
server.register_exception_handlers(app)  # Routers cannot carry these.
```

`REGIONAL_CONTRAILS_DATA_DIR` is read when `data_loader` is imported, so set
both variables before the host imports it. The host also adds `X-Grid-Meta` to
the `expose_headers` of its CORS policy. Everything else in `server.app` stays
behind:

*   **CORS**: the host's own middleware applies instead.
*   **Compression middleware**: the router's routes (`server.GzipRoute`) gzip
    their own JSON responses over 1 kB for clients that accept gzip, so the
    host's other endpoints are unaffected.
*   **Startup preload**: endpoints load each year's data on first use, so a
    host instance that never serves the dashboard never loads it.
*   **The dashboard's own files** (`/`, `/index.html`, favicons, `/assets/*`):
    the frontend is deployed separately.

Like the standalone server, a host caches which run `latest.json` names for
the life of the process, so a newly published run is served after a restart
or redeploy.

## Configuration

The server is configured via environment variables. `<package_root>` is the
parent directory of `server/` (the repository root in a source checkout).

| Variable | Default | Description |
| --- | --- | --- |
| `REGIONAL_CONTRAILS_DATASET` | *(required)* | Dataset name to serve (`{dataset}` directory under `{data_dir}/{year}/`). Up to 64 ASCII letters, digits, `-`, or `_`. |
| `REGIONAL_CONTRAILS_DATA_DIR` | `<package_root>/data` | Root directory containing published `{year}/{dataset}/` directories. |
| `CONTRAILS_UI_DIST_DIR` | `<package_root>/ui/dist` | Directory containing built frontend assets (`index.html`, `favicon.svg`, `assets/*`). If a file is missing there (or `CONTRAILS_UI_DIST_DIR=""`), it is looked up under `ui/` in the working directory, then `<package_root>`, and finally via `fs.resolve_resource_path`; the unbuilt `ui/index.html` found that way does not render, and `/assets/*` is only served from `CONTRAILS_UI_DIST_DIR` (returning 404 when `CONTRAILS_UI_DIST_DIR=""`). Assets must be regular files inside the directory; symlinks that resolve outside `assets/` are not served. |
| `CONTRAILS_CORS_ORIGIN_REGEX` | `http://127\.0\.0\.1:\d+\|http://localhost:\d+` | Regular expression matching allowed CORS origins. Cross-origin requests are limited to `GET`/`HEAD` with CORS-safelisted request headers and no credentials (cookies); the `X-Grid-Meta` response header is exposed to matching origins. |
| `HOST` | `0.0.0.0` | Bind address when running `python -m server.server`. |
| `PORT` | `9090` | TCP port when running `python -m server.server`. |

## Dataset directory layout

Each published year and dataset under `REGIONAL_CONTRAILS_DATA_DIR` must follow
the layout written by `data_extraction/`:

```
{data_dir}/{year}/{dataset}/
├── latest.json                                              # {"schema_version": 1, "run_id": "<run_id>", ...}
└── runs/{run_id}/
    ├── country_fir_density_df_export_per_country.json       # required to advertise {year} in /years
    ├── fir_density_df_export_per_country.json               # per-FIR annual summary rows
    ├── country_temporal_density_df_export_per_country.json  # optional per-country 24x12 temporal bundles
    ├── fir_temporal_density_df_export_per_country.json      # optional per-FIR 24x12 temporal bundles
    ├── countries.geojson                                    # country boundaries attributed in this run
    ├── firs.geojson                                         # FIR boundaries attributed in this run
    ├── grid_meta.json                                       # shared grid extent, LODs, and scale maxima
    └── {grid_prefix}[_lod0|_lod1].bin.gz                    # gzip-compressed float16 signed-log10 grids
```

## API endpoints

Every year-scoped endpoint places the four-digit `{year}` right after the
resource name. All endpoints are `GET`-only; other methods return `405`.
Interactive OpenAPI documentation is served at `/docs`, `/redoc` and
`/openapi.json`.

| Method & Path | Description |
| --- | --- |
| `GET /years` | `{"years": [...]}`: the four-digit years available for `REGIONAL_CONTRAILS_DATASET`, ascending. |
| `GET /countries/{year}` | List of all country metric rows for `{year}`. |
| `GET /country/{year}/{iso}` | Country metrics and aggregated FIR summary fields (`fir_total_*`, `fir_dom_*`, `fir_int_*`) for `{iso}` (ISO 3166-1 alpha-2). Individual FIR rows are served by `GET /firs/{year}`. |
| `GET /firs/{year}` | List of all FIR metric rows for `{year}`, with attributed country names and ISO codes. |
| `GET /rankings/{year}/{metric}` | Ranked countries or FIRs for `{metric}` (`?gwp=gwp20\|gwp50\|gwp100`). |
| `GET /series/{metric}` | Multi-year time series for `{metric}` across all available years (`?iso=` or `?fir_id=`, `?gwp=gwp20\|gwp50\|gwp100`). If both are given, `fir_id` selects the FIR row (with no fallback to `iso`; returns 404 if `fir_id` matches no FIR), `iso` only labels `iso_a2` on the response, and `country_name` is `null` for FIR series. Individual unreadable years appear as points with `value: null`, but if all available years are unreadable or all points have `value: null`, returns 404. |
| `GET /grid/{year}/availability` | Availability flags for total, monthly, and hourly grid families for `{year}`. |
| `GET /grid/{year}/{prefix}/meta` | Spatial metadata for `{prefix}`: `grid_meta.json` without `lods`, with the chosen level's dimensions merged in and `grid_name` (`"global_grid"` in `grid_meta.json`) replaced by `{prefix}_lod{lod}` or `{prefix}`. `?lod=0` is 4x downsampled and `?lod=1` 2x; any other integer, or no `lod`, is full resolution. If the requested level was not published, the full-resolution metadata is returned; if `grid_meta.json` is missing or unreadable, returns 404 even when `{prefix}.bin.gz` exists. |
| `GET /grid/{year}/{prefix}/binary` | `float16` grid payload (`application/octet-stream`; `?lod` as for `/meta`, with the same full-resolution fallback) and compact JSON metadata in the `X-Grid-Meta` header, which is omitted if the run has no `grid_meta.json`. The body is sent with `Content-Encoding: gzip` when the request accepts gzip or has no `Accept-Encoding` header, and decompressed for `Accept-Encoding: identity`. The decoded body is `width * height` `float16` values, row-major from `min_lat`/`min_lon` (see the `data_extraction` README for the encoding). |
| `GET /temporal/{year}` | 24-hour by 12-month temporal impact and flight-activity matrices for one entity: `?iso=` for a country, `?fir_id=` for a FIR (both: FIR with country fallback). Neither returns 400; an `iso` that is not a country of `{year}` returns 404. There is no global aggregate. |
| `GET /countries/{year}.json` | Country boundary `FeatureCollection` (`application/geo+json`) for `{year}`. |
| `GET /firs/{year}.json` | FIR boundary `FeatureCollection` (`application/geo+json`) for `{year}`. |
| `GET /`, `GET /assets/{asset_path}` | Dashboard HTML entrypoint and static frontend assets (`/index.html`, `/favicon.ico`, `/favicon.svg`, `/assets/*`). |

Grid `{prefix}` values are `total_global_{kind}_grid`,
`monthly_global_{kind}_grid_{01..12}` and `hourly_global_{kind}_grid_{00..23}`,
where `{kind}` is `impact` or `density`. `GET /grid/{year}/availability` lists
which families exist for a year.

### Ranking and time-series metrics

`GET /rankings/{year}/{metric}` and `GET /series/{metric}` accept a canonical
metric ID (case-insensitive, with leading/trailing whitespace stripped). Both
endpoints accept `?gwp=gwp20|gwp50|gwp100` (default `gwp50`), which substitutes
the selected GWP horizon into `{gwp}` in the underlying column template. On
`/rankings/{year}/{metric}`, the response field `title` gets a ` (GWP50)`-style
suffix for GWP-dependent metrics and a ` (FIRs)` suffix for FIR rankings, and
`isFirRanking` is `true` for FIR rankings and `null` otherwise; on
`/series/{metric}`, the title field is named `metric_title` (with the `
(GWP50)`-style suffix when GWP-dependent, without ` (FIRs)`).

In rankings, `items[].rank` is the 1-based position among deduplicated entries
with a non-null value, sorted by `value` descending, or `null` when `value` is
`null`. The pipeline's precomputed rank column listed below breaks equal-value
ties, and remaining ties (as well as `null`-value entries) are ordered by
`(name, iso_a2)` for countries and `(name, fir_id)` for FIRs. Computed
`RankingsResponse` objects are memoized in `_rankings_response_cache` per
`(run_dir, year, dataset, metric, gwp)`.

Some territories without an ISO code share `iso_a2 = "-99"`. `/countries` and
`/rankings` list them separately, but `/country`, `/series` and `/temporal`
cannot address them individually: `/country` and `/series` use the first such
row, and `/temporal` sums all of them.

#### Country metrics (`COUNTRY_METRICS`)

Used for country rankings (`GET /rankings/{year}/{metric}`) and country
year-over-year series (`GET /series/{metric}?iso={iso}`). When querying a FIR
time series (`GET /series/{metric}?fir_id={fir_id}`), country metrics with unit
`tonnes_co2e` or `milliwatts_per_square_metre` are also accepted and
automatically projected onto the corresponding `FIR_ROW_METRICS` definition via
`project_to_fir_row_metric`.

| Canonical metric (`{metric}`) | Title | Unit | Primary column | Tie-break rank column | Secondary column (`secondary_unit`) |
| --- | --- | --- | --- | --- | --- |
| `mean_rf_mw_per_sq_m` | Contrail Mean Radiative Forcing | `milliwatts_per_square_metre` | `mean_rf_mw_per_sq_m` | `mean_rf_mw_per_sq_m_rank` | — |
| `co2e_gwp_tonnes` | Contrail Impact Absolute | `tonnes_co2e` | `co2e_{gwp}_tonnes` | `co2e_{gwp}_tonnes_rank` | — |
| `co2e_gwp_tonnes_per_capita` | Contrail Impact Per Capita | `tonnes_co2e_per_capita` | `co2e_{gwp}_tonnes_per_capita` | `co2e_{gwp}_tonnes_per_capita_rank` | `co2e_{gwp}_per_capita_vs_world_avg` (`ratio_of_world_avg_ghg_per_capita`) |
| `co2e_gwp_vs_national_ghg` | vs. National GHG Emissions | `ratio_of_national_ghg` | `co2e_{gwp}_vs_national_ghg` | `co2e_{gwp}_vs_national_ghg_rank` | `ghg_mtco2e` (`megatonnes_co2e`) |
| `outgoing_flight_co2e_gwp_tonnes` | Outgoing Flights Contrail Impact | `tonnes_co2e` | `outgoing_flight_co2e_{gwp}_tonnes` | `outgoing_flight_co2e_{gwp}_tonnes_rank` | — |
| `outgoing_flight_co2e_gwp_tonnes_per_capita` | Outgoing Impact Per Capita | `tonnes_co2e_per_capita` | `outgoing_flight_co2e_{gwp}_tonnes_per_capita` | `outgoing_flight_co2e_{gwp}_tonnes_per_capita_rank` | `outgoing_flight_co2e_{gwp}_per_capita_vs_world_avg` (`ratio_of_world_avg_ghg_per_capita`) |
| `outgoing_flight_co2e_gwp_vs_national_ghg` | Outgoing vs. National GHG | `ratio_of_national_ghg` | `outgoing_flight_co2e_{gwp}_vs_national_ghg` | `outgoing_flight_co2e_{gwp}_vs_national_ghg_rank` | `ghg_mtco2e` (`megatonnes_co2e`) |
| `fir_total_co2e_gwp_tonnes` | Controlled FIRs Contrail Impact | `tonnes_co2e` | `fir_total_co2e_{gwp}_tonnes` | `fir_total_co2e_{gwp}_tonnes_rank` | — |
| `fir_total_mean_rf_mw_per_sq_m` | Controlled FIRs Mean Radiative Forcing | `milliwatts_per_square_metre` | `fir_total_mean_rf_mw_per_sq_m` | `fir_total_mean_rf_mw_per_sq_m_rank` | — |
| `fir_total_co2e_gwp_tonnes_per_capita` | Controlled FIRs Impact Per Capita | `tonnes_co2e_per_capita` | `fir_total_co2e_{gwp}_tonnes_per_capita` | `fir_total_co2e_{gwp}_tonnes_per_capita_rank` | `fir_total_co2e_{gwp}_per_capita_vs_world_avg` (`ratio_of_world_avg_ghg_per_capita`) |
| `fir_total_co2e_gwp_vs_national_ghg` | Controlled FIRs vs. National GHG | `ratio_of_national_ghg` | `fir_total_co2e_{gwp}_vs_national_ghg` | `fir_total_co2e_{gwp}_vs_national_ghg_rank` | `ghg_mtco2e` (`megatonnes_co2e`) |

#### FIR row metrics (`FIR_ROW_METRICS`)

Used for individual FIR rankings (`GET /rankings/{year}/{metric}`) and FIR
year-over-year series (`GET /series/{metric}?fir_id={fir_id}`). Requesting a
`FIR_ROW_METRICS` slug on `/series/{metric}` without `?fir_id=` returns `400`.

| Canonical metric (`{metric}`) | Title | Unit | Primary column | Tie-break rank column | Secondary column (`secondary_unit`) |
| --- | --- | --- | --- | --- | --- |
| `fir_individual_co2e_gwp_tonnes` | FIR Contrail Impact | `tonnes_co2e` | `co2e_{gwp}_tonnes` | `co2e_{gwp}_tonnes_rank` | `area_sq_km` (`square_kilometres`) |
| `fir_individual_mean_rf_mw_per_sq_m` | FIR Mean Radiative Forcing | `milliwatts_per_square_metre` | `mean_rf_mw_per_sq_m` | `mean_rf_mw_per_sq_m_rank` | `area_sq_km` (`square_kilometres`) |

### Error responses

| Status | Meaning | When returned |
| --- | --- | --- |
| `400 Bad Request` | Invalid request parameter or unpublished year (`DataValidationError`, `HTTPException`) | Non-4-digit or unpublished `{year}` (not in `/years`; a year whose `latest.json` is absent, undecodable, structurally invalid, or has an unsupported `schema_version` at year discovery counts as unpublished) on any year-scoped endpoint (`/countries/{year}`, `/country/{year}/{iso}`, `/firs/{year}`, `/rankings/{year}/{metric}`, `/temporal/{year}`, `/countries/{year}.json`, `/firs/{year}.json`, `/grid/{year}/availability`, `/grid/{year}/{prefix}/meta`, `/grid/{year}/{prefix}/binary`); malformed or non-ASCII `iso` or grid `prefix`; a `fir_id` longer than 128 characters on `/series/{metric}` (other malformed `fir_id` values match no FIR and return 404); `/series/{metric}` or `/temporal/{year}` called with neither `iso` nor `fir_id`; or `/series/{metric}` called with a `FIR_ROW_METRICS` slug without `fir_id`. |
| `404 Not Found` | Resource, metric, or route not found (`UnknownMetricError`, `HTTPException`) | Unrecognized `{metric}` slug on `/rankings/{year}/{metric}` or `/series/{metric}`; country-only metric (`tonnes_co2e_per_capita` or `ratio_of_national_ghg`) requested with `fir_id` on `/series/{metric}`; unknown country `{iso}` on `/country/{year}/{iso}`, `/temporal/{year}`, or `/series/{metric}`; unknown `{fir_id}` on `/temporal/{year}` (without `iso` fallback) or `/series/{metric}`; entity has only null values across all available years on `/series/{metric}`; missing grid `{prefix}` on `/grid/{year}/{prefix}/meta` or `/grid/{year}/{prefix}/binary`, or missing/unreadable `grid_meta.json` on `/grid/{year}/{prefix}/meta`; missing boundary GeoJSON file for a published year on `/countries/{year}.json` or `/firs/{year}.json`; or an unmatched URL path. |
| `422 Unprocessable Entity` | Unsupported GWP horizon, missing metric column, or query schema validation failure (`UnsupportedGwpError`, `MissingColumnError`, FastAPI validation) | Unsupported `?gwp=` value (must be `gwp20`, `gwp50`, or `gwp100`); requested metric/GWP column absent from the dataset rows on `/rankings/{year}/{metric}` or `/series/{metric}`; or FastAPI query parameter validation failure (such as a non-integer `?lod=` query value or an `iso`/`fir_id` query string exceeding `max_length` on `/temporal/{year}`). |
| `503 Service Unavailable` | Dataset unconfigured or published pointer/artifact unreadable or corrupt (`DatasetConfigError`, `ArtifactUnavailableError`) | `REGIONAL_CONTRAILS_DATASET` is not configured or invalid (only reachable when the app is served without its lifespan handler; `python -m server.server` and the lifespan refuse to start unconfigured); a listed year's `country_fir_density_df_export_per_country.json` is unreadable (`OSError`) or invalid JSON (`JSONDecodeError`), `fir_density_df_export_per_country.json` is missing, unreadable, or invalid JSON, or either annual export is not a JSON list of objects; a published row or temporal matrix fails Pydantic schema or `fir_to_country_iso2s` validation; a grid `.bin.gz` payload fails gzip decompression when `Accept-Encoding: identity` is requested; or `resolve_dataset_dir` encounters a structurally invalid `latest.json` (not a JSON object, a non-integer `schema_version`, or a missing or unsafe `run_id`) after `/years` was already cached. |

## Tests

`server_test.py` runs unit and integration tests against the synthetic dataset
generated in-process by `testdata_fixture.py`.
