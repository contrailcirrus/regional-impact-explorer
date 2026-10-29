# Published dashboard artifacts: {year}/{dataset}/latest.json and the immutable
# runs/{run_id}/ directories the server reads, with the bucket root as
# --output_prefix. Long-lived pipeline reference inputs (countries.geojson,
# firs.geojson, airports.csv, population.csv, ghg_emissions.json) are uploaded
# by hand under reference/; the server only lists four-digit year directories,
# so it ignores them.
resource "google_storage_bucket" "results" {
  name                        = "contrails-301217-regional-impact-results"
  project                     = data.google_project.project.project_id
  location                    = local.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  enable_object_retention     = false
  requester_pays              = false
  hierarchical_namespace {
    enabled = false
  }
  soft_delete_policy {
    retention_duration_seconds = 604800
  }
}

# Reduce-stage intermediates (--intermediate_prefix) for derive_main re-runs.
# Separate from the results so they get their own lifecycle and the server never
# needs access to them.
resource "google_storage_bucket" "intermediate" {
  name                        = "contrails-301217-regional-impact-intermediate"
  project                     = data.google_project.project.project_id
  location                    = local.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  enable_object_retention     = false
  requester_pays              = false
  hierarchical_namespace {
    enabled = false
  }
  soft_delete_policy {
    retention_duration_seconds = 604800
  }

  # Intermediates are only read again by derive_main re-runs, which become
  # rare once a run's metrics settle.
  lifecycle_rule {
    condition {
      age                   = 30
      matches_storage_class = ["STANDARD"]
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }
}

# Dataflow --temp_location and --staging_location. Nothing here outlives a job.
resource "google_storage_bucket" "dataflow_tmp" {
  name                        = "contrails-301217-regional-impact-dataflow-tmp"
  project                     = data.google_project.project.project_id
  location                    = local.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  enable_object_retention     = false
  requester_pays              = false
  hierarchical_namespace {
    enabled = false
  }
  soft_delete_policy {
    retention_duration_seconds = 0
  }

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }
}
