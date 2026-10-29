resource "google_artifact_registry_repository" "regional_impact_explorer" {
  location      = local.region
  repository_id = "regional-impact-explorer"
  description   = "regional-impact-explorer docker images (Dataflow worker image for data_extraction)"
  format        = "DOCKER"

  # Every push to main tags a new image by commit SHA; keep the recent ones so
  # older jobs can be rerun, and drop the rest.
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 20
    }
  }
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "7776000s" # 90 days
    }
  }
}
