# Shared Contrails Dashboard Utilities (`common/`)

Lightweight utilities and constants shared between the data extraction pipeline
(`data_extraction/`) and the FastAPI backend server (`server/`).

This package has no dependency on heavy data-processing libraries
(`apache-beam`, `numpy`, `pandas`, `pyarrow`, or `shapely`) at import time,
allowing the backend server to run in a minimal Python environment while sharing
the exact artifact layout, metric naming conventions, and filesystem abstraction
with the pipeline.

## Modules

| Module | Description |
| --- | --- |
| `constants.py` | FIR ID and country-list normalization helpers (`clean_fir_name`, `make_fir_id`, `split_iso2_codes`), GWP conversion multipliers (`GWP20_TONNES_PER_MJ`, `GWP50_TONNES_PER_MJ`, `GWP100_TONNES_PER_MJ`, `GWP_TONNES_PER_MJ`), `WORLD_AVG_GHG_PER_CAPITA_TONNES`, published artifact filenames, run-pointer schema constants, FIR scope prefixes, derived metric/rank field keys and `{gwp}` templates, intermediate key/metric/row type aliases (`CountryKey`, `CountryMetricsTuple`, `CountryRow`, `FirKey`, `FirMetricsTuple`, `FirRow`), shared row/bundle `TypedDict`s (`EncodedCountryRow`, `EncodedFirRow`, `TemporalBundleRow`), and canonical published column key sets (`PUBLISHED_COUNTRY_ROW_KEYS`, `PUBLISHED_FIR_ROW_KEYS`). |
| `fs.py` | Pluggable filesystem and resource abstraction (`Error`, `open_file`, `atomic_open_for_write`, `ensure_parent_dir`, `rename`, `remove`, `exists`, `makedirs`, `isdir`, `listdir`, `glob`, `resolve_resource_path`, `get_version_tag`) supporting local POSIX paths, custom backends and hooks registered via `register_filesystem_backend`, `register_resource_resolver`, and `register_version_tag_provider`, and lazy delegation to `apache_beam.io.filesystems.FileSystems` for remote URIs (`gs://`, `s3://`, `hdfs://`). |
| `artifact_io.py` | Run-id validation (`validate_run_id`), atomic JSON/binary/GeoJSON artifact writers (`write_json_output`, `write_binary_output`, `write_boundary_geojson`; JSON writes store non-finite floats as `0.0`), `latest.json` pointer resolution and publishing (`read_run_pointer`, `resolve_dataset_dir`, `write_run_pointer`), and published run directory path helpers (`year_dir_name`, `dataset_root`, `run_output_dir`, `require_new_run_dir`, `pointer_path`). |
