# ------------------------------------
# Service account for Dataflow workers running the data_extraction pipeline
# ------------------------------------
resource "google_service_account" "data_extraction_sa" {
  account_id                   = "regional-impact-dataflow"
  create_ignore_already_exists = null
  description                  = "Dataflow worker service account for the regional-impact-explorer data_extraction pipeline"
  disabled                     = false
  display_name                 = "regional-impact-dataflow"
  project                      = local.project
  timeouts {
    create = null
  }
}

locals {
  # Built from account_id rather than the resource's email attribute so that
  # for_each keys below are known at plan time.
  data_extraction_sa_member = "serviceAccount:regional-impact-dataflow@${local.project}.iam.gserviceaccount.com"

  # Workers read reference data (results bucket, reference/) and write results,
  # intermediates and Dataflow temp files. Launchers need the same access: the driver validates inputs,
  # writes the boundary GeoJSONs into the new run directory and stages files
  # before submitting the job. objectAdmin (not objectCreator) because GCS
  # renames are copy + delete.
  pipeline_buckets = {
    results      = google_storage_bucket.results.name
    intermediate = google_storage_bucket.intermediate.name
    dataflow_tmp = google_storage_bucket.dataflow_tmp.name
  }
  pipeline_bucket_members = merge(
    { worker = local.data_extraction_sa_member },
    { for member in var.pipeline_launchers : member => member },
  )
  pipeline_bucket_bindings = {
    for pair in setproduct(keys(local.pipeline_buckets), keys(local.pipeline_bucket_members)) :
    "${pair[0]}/${pair[1]}" => {
      bucket = local.pipeline_buckets[pair[0]]
      member = local.pipeline_bucket_members[pair[1]]
    }
  }
  segments_bucket_members = var.segments_bucket == "" ? {} : local.pipeline_bucket_members
}

resource "google_project_iam_member" "data_extraction_sa_dataflow_worker" {
  project = local.project
  role    = "roles/dataflow.worker"
  member  = local.data_extraction_sa_member
  depends_on = [
    google_service_account.data_extraction_sa,
  ]
}

resource "google_storage_bucket_iam_member" "pipeline_bucket_object_admin" {
  for_each = local.pipeline_bucket_bindings
  bucket   = each.value.bucket
  role     = "roles/storage.objectAdmin"
  member   = each.value.member
  depends_on = [
    google_service_account.data_extraction_sa,
  ]
}

# Read-only access to the existing flight-segment Parquet bucket (see
# var.segments_bucket). The driver lists files there; workers read them.
resource "google_storage_bucket_iam_member" "segments_bucket_object_viewer" {
  for_each = local.segments_bucket_members
  bucket   = var.segments_bucket
  role     = "roles/storage.objectViewer"
  member   = each.value
  depends_on = [
    google_service_account.data_extraction_sa,
  ]
}

resource "google_artifact_registry_repository_iam_member" "data_extraction_sa_image_reader" {
  project    = local.project
  location   = google_artifact_registry_repository.regional_impact_explorer.location
  repository = google_artifact_registry_repository.regional_impact_explorer.name
  role       = "roles/artifactregistry.reader"
  member     = local.data_extraction_sa_member
  depends_on = [
    google_service_account.data_extraction_sa,
  ]
}

# ------------------------------------
# People/groups allowed to launch Dataflow jobs (see var.pipeline_launchers)
# ------------------------------------
resource "google_project_iam_member" "launcher_dataflow_developer" {
  for_each = toset(var.pipeline_launchers)
  project  = local.project
  role     = "roles/dataflow.developer"
  member   = each.value
}

# Lets launchers submit jobs with --service_account_email set to the pipeline
# service account.
resource "google_service_account_iam_member" "launcher_sa_user" {
  for_each           = toset(var.pipeline_launchers)
  service_account_id = google_service_account.data_extraction_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}

# ------------------------------------
# GitHub Actions (GCP_SERVICE_ACCOUNT secret) pushes the worker image
# ------------------------------------
resource "google_artifact_registry_repository_iam_member" "ci_image_writer" {
  count      = var.ci_service_account_email == "" ? 0 : 1
  project    = local.project
  location   = google_artifact_registry_repository.regional_impact_explorer.location
  repository = google_artifact_registry_repository.regional_impact_explorer.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${var.ci_service_account_email}"
}
