# Regional Contrails Dashboard

The Regional Contrails Dashboard analyzes and visualizes per-country and per-FIR
(Flight Information Region) aviation contrail climate impact. It converts
flight-segment contrail energy forcing into $\text{CO}_2\text{e}$ across GWP20,
GWP50, and GWP100 horizons, computes radiative forcing per area, per-capita
impacts, and ratios against national greenhouse-gas emissions, and renders
global spatial density/impact heatmaps alongside $24 \times 12$ (hour $\times$
month) temporal contrail avoidance opportunity matrices.

## Architecture

The project is organized into four packages that communicate through published
filesystem artifacts:

```
flight-segment Parquet ──► data_extraction/ ──► {output_prefix}/{year}/{dataset}/latest.json ──► server/ ──► ui/
  + reference inputs       (Beam + derive)       + runs/{run_id}/ artifacts                   (FastAPI)   (React)
                                 │                                                                │
                                 └──────────────────────────► common/ ◄───────────────────────────┘
                                                    (constants, fs, artifact_io)
```

| Component | Directory | Description |
| --- | --- | --- |
| Shared utilities & constants | [`common/`](./common/README.md) | Shared GWP constants, metric/artifact naming contracts, atomic `latest.json` run-pointer helpers (`artifact_io.py`), and pluggable filesystem I/O (`fs.py`). Free of heavy pipeline dependencies so `server/` stays lightweight. |
| Data extraction pipeline | [`data_extraction/`](./data_extraction/README.md) | Apache Beam `reduce` stage that attributes flight segments to countries, FIRs, and global raster grids, plus a Beam-free `derive` stage that computes derived climate metrics; together they publish immutable run directories. |
| Backend API server | [`server/`](./server/README.md) | FastAPI application that reads published run directories for a configured dataset (`CONTRAILS_DATASET`) under `CONTRAILS_DATA_DIR` and serves country/FIR summaries, rankings, multi-year series, temporal matrices, raster grids, and static UI assets. |
| Frontend UI | [`ui/`](./ui/README.md) | React and TypeScript single-page application featuring a MapLibre globe/map with a WebGL2 float16 raster heatmap layer, country and FIR metric panels, rankings, year-over-year trajectories, and interactive temporal avoidance opportunity heatmaps. |

For details on the contrail energy-forcing to $\text{CO}_2\text{e}$ conversions,
radiative forcing formulas, spatial vs. departure-airport attribution,
`signed_log10_ref_scaled` float16 grid encoding, and JSON artifact schemas, see
[Scientific methodology & metric derivations](./data_extraction/README.md#scientific-methodology--metric-derivations)
in `data_extraction/README.md`.

> [!IMPORTANT]
> The pipeline expects **non-effective** contrail energy forcing (no
> $\text{ERF/RF}$ ratio applied) and applies $\text{ERF/RF} = 0.42$ itself
> when converting to $\text{CO}_2\text{e}$. If your segment data is already
> effective energy forcing, divide it by 0.42 first; otherwise all
> $\text{CO}_2\text{e}$ values are 58% too low. See
> [Energy forcing input contract](./data_extraction/README.md#energy-forcing-input-contract).

## Getting Started

**Prerequisites:** **Python 3.11+** (for the data extraction pipeline and
FastAPI backend server) and **Node.js 22.15+** with `npm` (for building,
serving, and testing the React + Vite frontend; `npm test` uses
`module.registerHooks`, introduced in Node.js 22.15.0).

### 1. Install Python Dependencies

From the repository root, install the full package (the API server, the
Apache Beam extraction pipeline and the test dependencies) in a virtual
environment:

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e ".[pipeline,dev]"
```

`pip install -e .` installs only what the FastAPI backend server needs to serve
already-published data, locally or from `gs://`. The extraction pipeline's
dependencies, Apache Beam among them, are in the `pipeline` extra.

### 2. Data Extraction Pipeline

See [`data_extraction/README.md`](./data_extraction/README.md) for input file
schemas, CLI flags, and output directory layout. No data is included in this
repository: you supply the flight-segment Parquet files and a FIR boundary
GeoJSON yourself. First, download the public reference datasets
(`countries.geojson`, `population.csv`, `ghg_emissions.json`, and
`airports.csv`). Then provide `firs.geojson` in the
[FIR GeoJSON](./data_extraction/README.md#fir-geojson) format: if you have the
ICAO 2015 FIR shapefile release (`ICAOFIRFULallDensify.shp` / `.dbf`),
`preprocess_icao_fir_2015` converts it; for a quick smoke test without the ICAO
release, pass `--firs_path=data_extraction/testdata/global_firs.geojson` (a
synthetic 7-region fixture). Finally, run the pipeline locally with Apache
Beam's `DirectRunner` (include `--intermediate_prefix=/path/to/intermediate` if
you want to save reduced aggregates for later `derive_main` runs without
re-running Beam):

```sh
python -m data_extraction.fetch_reference_data --output_dir=/path/to/ref

# Optional: only if converting the ICAO 2015 FIR shapefile release
python -m data_extraction.preprocess_icao_fir_2015 \
  /path/to/ICAO_FIR_2015/ICAOFIRFULallDensify.shp \
  /path/to/ICAO_FIR_2015/ICAOFIRFULallDensify.dbf \
  /path/to/ref/firs.geojson

python -m data_extraction.extract_pipeline_main \
  --input_year=2024 \
  --dataset=my_dataset \
  --output_prefix=/path/to/data \
  --intermediate_prefix=/path/to/intermediate \
  --segments_path='/path/to/segments/2024/*.parquet' \
  --airports_path=/path/to/ref/airports.csv \
  --firs_path=/path/to/ref/firs.geojson \
  --country_geojson_path=/path/to/ref/countries.geojson \
  --population_path=/path/to/ref/population.csv \
  --ghg_emissions_path=/path/to/ref/ghg_emissions.json \
  --grid_width=3600 --grid_height=1800 \
  --min_lon=-180 --max_lon=180 --min_lat=-90 --max_lat=90 \
  --step_size_meters=10000 \
  -- --runner=DirectRunner
```

### 3. Backend Server

Point the FastAPI server at the published data root and dataset name. The API
works right away; the dashboard at `/` additionally needs the production UI
build from step 4 (`npm run build`):

```sh
export CONTRAILS_DATA_DIR=/path/to/data
export CONTRAILS_DATASET=my_dataset
export PORT=9090
python -m server.server
```

### 4. Frontend UI

See [`ui/README.md`](./ui/README.md) for frontend build and development server
instructions. Either build the UI so the backend serves it at `/`:

```sh
cd ui
npm install
npm run build
```

or run the Vite development server on port 3000, which proxies API calls to the
backend on port 9090:

```sh
cd ui
npm install
npm run dev
```

### 5. Running Tests

From the repository root (with `.venv` activated after `pip install -e
".[pipeline,dev]"`), run the Python test suite across `common/`, `data_extraction/`, and
`server/`:

```sh
pytest
```

Individual test modules can also be run via `unittest` from the repository root
(for example, `python -m unittest common.fs_test`).

To run the frontend TypeScript unit test suite:

```sh
cd ui
npm test
```

## License

This project is licensed under the [Apache License, Version 2.0](./LICENSE).
