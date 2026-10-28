output "data_extraction_service_account" {
  description = "Pass as --service_account_email when launching the pipeline."
  value       = google_service_account.data_extraction_sa.email
}

output "worker_image" {
  description = "Pass as --sdk_container_image (with a :<git sha> or :latest tag)."
  value       = "${local.region}-docker.pkg.dev/${local.project}/${google_artifact_registry_repository.regional_impact_explorer.repository_id}/data-extraction-worker"
}

output "results_bucket" {
  value = google_storage_bucket.results.name
}

output "intermediate_bucket" {
  value = google_storage_bucket.intermediate.name
}

output "dataflow_tmp_bucket" {
  value = google_storage_bucket.dataflow_tmp.name
}
