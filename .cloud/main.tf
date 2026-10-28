provider "google" {
  project = "contrails-301217"
  region  = "us-east1"
  zone    = "us-east1-b"
}

terraform {
  backend "gcs" {
    bucket = "contrails-301217-infrastructure"
    prefix = "terraform/state/regional-impact-explorer"
  }
}

data "google_project" "project" {}

locals {
  project = "contrails-301217"
  region  = "us-east1"
}

# Already enabled in the project if other Dataflow jobs run there; managing it
# here is harmless because destroying this config leaves the API enabled.
resource "google_project_service" "dataflow" {
  project            = local.project
  service            = "dataflow.googleapis.com"
  disable_on_destroy = false
}
