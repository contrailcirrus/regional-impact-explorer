# Regional Contrail &amp; Energy Forcing Dashboard UI

TypeScript + React single-page application for regional aviation contrail impact
analysis.

## Build and module resolution

The frontend is built with Vite and TypeScript (`index.html` &rarr; `dist/`):

| Stage | Entry / Output | Served by |
| --- | --- | --- |
| Development | `index.html` | `npm run dev` (Vite dev server on port `3000`) |
| Production | `dist/index.html` + `dist/assets/*` | `server/static_assets.py` (FastAPI), or a static file server that also proxies the API routes listed in `vite.config.ts` to the backend on the same origin |

### Bare specifier aliases

The source files import `'material-ui'` as a bare specifier, which is mapped to
`@material-ui/core` via configuration:

- `tsconfig.json` &rarr; `compilerOptions.paths` (satisfies `tsc`)
-   `vite.config.ts` &rarr; `resolve.alias` (satisfies the Vite dev server and
    Rollup bundler)

In the standalone `npm test` run, `test_browser_globals.ts` registers a
`node:module` hook (`registerHooks`, requiring **Node.js 22.15+**) that
short-circuits both `'material-ui'` and `'@material-ui/core'` (as well as
`'react'`) to an in-memory ESM stub (`contrails-test-shim:material-ui`)
exporting string tag names for a fixed list of components (`Box`,
`CircularProgress`, `IconButton`, `MenuItem`, `Select`, `Typography`). Importing
any additional `@material-ui/core` component requires adding its named export to
`materialUiStub` and `MATERIAL_UI_ESM_SHIM_SOURCE` in `test_browser_globals.ts`.
The Vite alias is an anchored regex on purpose, so it cannot prefix-match other
`material-ui*` specifiers.

### Runtime library loading

d3, React and Material-UI are bundled from npm by Vite. The sources still read
`const d3Lib = d3 || window.d3`, but the imported `d3` module is always
defined, so the `window.d3` branch is never taken in this build.

Country and FIR boundaries are not bundled. The dashboard always draws the
boundaries of the most recent published year: it fetches
`/countries/<year>.json` and `/firs/<year>.json` for that year once per page
load (the server serves the GeoJSON files the pipeline published with that
year's run of the active dataset) and reuses them for every year, so switching
years does not re-download them. Runs derived with different boundary files are
therefore drawn with the most recent year's boundaries.

MapLibre GL JS (JavaScript and CSS) is loaded from the pinned unpkg CDN tags
in the HTML entry document and accessed via `window.maplibregl`, so the browser
needs network access to unpkg.com. It is deliberately not an npm dependency:
the sources never import it, and both builds use the same CDN file. To upgrade
MapLibre, change the version and the integrity hashes in `index.html`.

## Development

```bash
npm install
npm run dev
```

The Vite dev server runs on `http://localhost:3000` and proxies backend routes
to the FastAPI server on `http://localhost:9090` (configurable via the
`CONTRAILS_BACKEND_ORIGIN` environment variable). The proxy table in
`vite.config.ts` lists every route the frontend calls; add to it when you add an
endpoint, or the dev server will 404 while production works.

To develop against contrails-api, which serves the API under
`/internal/regional` (on port 8080 with `make serve`), also set
`CONTRAILS_BACKEND_PATH_PREFIX`:

```bash
CONTRAILS_BACKEND_ORIGIN=http://localhost:8080 \
CONTRAILS_BACKEND_PATH_PREFIX=/internal/regional \
npm run dev
```

## Production build

```bash
npm run build
```

Emits `dist/index.html` and hashed bundles under `dist/assets/`. When
`index.html` exists in `ui/dist/` (or in the directory named by
`CONTRAILS_UI_DIST_DIR`), the FastAPI server (`python -m server.server`) serves
the production build at `/` and `/assets/*`. The build is required for the
FastAPI server's `/` to render; the frontend fetches every API route from its
own origin.

## Testing and type-checking

```bash
npm run typecheck
npm test
```

`npm run typecheck` runs `tsc --noEmit` across all application and `*_test.ts`
files with strict unused-local and unused-parameter checks enabled. `npm test`
executes the Jasmine unit test suite (`*_test.ts`) and requires **Node.js
22.15+** for `module.registerHooks`.

## Module overview

| File | Responsibility |
| --- | --- |
| `app.tsx` | Browser entry point mounting `RegionalDashboard` onto `#root`. |
| `RegionalDashboard.tsx` | Top-level stateful dashboard component managing the MapLibre GL map, WebGL2/Canvas raster overlays, year/country/FIR selection, and GWP horizon, and hosting the metrics panel. |
| `MapControlsOverlay.tsx` | Map overlay controls, playback timeline scrubber, boundary legend, status/loading banners, and vertical colorbar legend. |
| `RegionalFilterDrawer.tsx` | Filter bar controls for selecting the active year, searchable country/region, and GWP horizon (`FilterState`). |
| `ContrailMetricsPanel.tsx` | Side panel displaying selected country or FIR climate impact metrics, airspace attribution breakdowns, outgoing flight emissions, and ranking badges, embedding `TemporalImpactHeatmap`, and managing `CountryRankingsPopup` and `YoYSeriesPopup`. |
| `metrics_panel_widgets.tsx` | Reusable hero cards, metric rows, FIR chip selector, `ModalShell`, SVG icons, and rank/YoY badges shared by `ContrailMetricsPanel` and both popups. |
| `CountryRankingsPopup.tsx` | Modal leaderboard comparing countries and FIRs across absolute, density, per-capita, and national-GHG-ratio metrics. |
| `YoYSeriesPopup.tsx` | Multi-year time-series chart modal rendering year-over-year trends and deltas fetched from `GET /series/{metric}`. |
| `TemporalImpactHeatmap.tsx` | Interactive 24-hour by 12-month heatmap component visualizing diurnal and seasonal daily CO2e impact (`tCO2e/day`), flight distance (`km/day`), and contrail avoidance opportunity index. |
| `temporal_heatmap_model.ts` | Pure data model for normalizing `/temporal/{year}` payloads into 24×12 cells, computing percentile ranks and sigmoid impact/congestion scores, building highlight boundary edges, and generating hover/footnote copy. |
| `opportunity_scale.ts` | Sigmoid (logistic) congestion and impact transforms, percentile ranking, RdYlGn/OrRd palettes and interpolation, hover badge status colours, and slider-track gradients behind the avoidance opportunity heatmap. |
| `CountryMetadata.ts` | Backend fetchers (`/years`, `/countries`, `/firs`, `/country`, `/rankings`, `/series`), bounded caches, payload validators and normalizers, boundary-feature resolvers, and hero/YoY metric aggregation for countries and FIRs. |
| `dashboard_geo_utils.ts` | Pure MapLibre layer/source helpers, LRU grid cache and in-flight request deduplication, gzip grid decoding (`DecompressionStream`), GeoJSON boundary loaders, antimeridian-safe centroids, FIR feature matching, initial-country resolution, and year-gate helpers. |
| `geo_raster_utils.ts` | IEEE 754 `float16` lookup table (`FLOAT16_TO_FLOAT32_LUT`), WebGL2 (`SharedGridGpuRenderer`) and 2D Canvas (`renderGridOnCpu`) raster renderers, Web Mercator latitude projection, and colorbar legend tick helpers. |
| `metric_formatters.ts` | Client-side number, percentage, CO2e mass and radiative forcing density formatters (`Intl.NumberFormat`), unit labels, YoY change and rank-label helpers. |

## Data architecture

- **Backend is the single source of truth.** FIR and country reference data,
  boundaries and the list of available years are served by the backend in
  [`server/`](../server/README.md), not bundled into the frontend.
- **Years are always explicit.** The dashboard fetches `/years` before it
  renders anything else and shows a loading, error or "no data published" state
  until at least one year is known. The year dropdown then starts on the latest
  year, and every data request names its year; neither the frontend nor the
  server has a fallback year. Year-over-year comparisons use the previous
  calendar year only when `/years` lists it.
- **Large datasets stay server-side.** Full 24&times;12 temporal impact matrices
  are fetched on demand rather than shipped in the bundle.
-   **Client-side localization.** Summary and ranking metrics, units, YoY
    deltas, and rank labels are formatted on the client with `Intl.NumberFormat`
    (`metric_formatters.ts`).
