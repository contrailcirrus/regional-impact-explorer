EXPORT DATA OPTIONS (
  uri = 'gs://contrails-301217-sandbox-internal/flights-pipeline/regional-impact/segments/2026/*.parquet',
  format = 'PARQUET',
  compression = 'SNAPPY',
  overwrite = true
) AS
SELECT
  lon_start, lat_start, lon_end, lat_end,
  time_start,
  ef_mj,
  departure_airport_icao, arrival_airport_icao
FROM `contrails-301217.flights_pipeline_prod.inventory_2026Q2_run_sept2026_segments`
WHERE time_start >= '2026-01-01' AND time_start < '2026-01-10';
