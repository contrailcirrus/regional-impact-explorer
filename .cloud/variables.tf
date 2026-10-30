# Stubs for resources that live outside this config. Fill in the defaults (or
# pass -var / a *.tfvars file) once the owning buckets and accounts are known.
# Empty values skip the corresponding IAM bindings.

variable "segments_bucket" {
  description = "Name (no gs:// prefix) of the existing bucket holding the flight-segment Parquet input. The worker service account and launchers get read access to it."
  type        = string
  default     = "contrails-301217-sandbox-internal"
}

variable "ci_service_account_email" {
  description = "Email of the service account behind the GCP_SERVICE_ACCOUNT GitHub secret, granted push access to the worker image repository."
  type        = string
  default     = "ci-service-account@contrails-301217.iam.gserviceaccount.com"
}

variable "pipeline_launchers" {
  description = "IAM members (e.g. \"group:contrails-eng@contrails.org\", \"user:someone@contrails.org\") allowed to launch Dataflow jobs as the pipeline service account."
  type        = list(string)
  default     = ["user:joffrey.peters@contrails.org",
                  "user:nick.masson@contrails.org"]
}

variable "api_service_account_email" {
  description = "Email of the service account contrails-api runs as on Cloud Run. It serves the dashboard API from the results bucket, so it gets read access to it."
  type        = string
  default     = "contrails-api-service-account@contrails-301217.iam.gserviceaccount.com"
}
