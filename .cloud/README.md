# .cloud

This directory contains configuration files used for
managing infrastructure and cloud services owned by this application.

## Terraform
Terraform is a powerful, highly-featured infrastructure-as-code framework.


> ⚠️ To avoid out-of-sync Terraform state management, avoid manually manipulating resources.
All changes/additions/deletions should be made by modifying the terraform config files,
and deployment via `terraform apply`.


Install the CLI on OSX: `brew install terraform`

Also:
- [Reference Docs for Terraform on GCP](https://cloud.google.com/docs/terraform)
- [Example terraform config files](https://github.com/terraform-google-modules/terraform-docs-samples/tree/main)

Example commands:
```bash
terraform init  # install dependencies & build a lockfile based on the *.tf config
```

```bash
terraform plan  # print out a "plan" (~ dry run) showing the anticipated resource creation
```

```bash
terraform apply  # deploy the resources defined in the lockfile
```
### State Management
Infrastructure state under Terraform's management is handled via the `gs://contrails-301217-infrastructure/terraform/state/regional-impact-explorer/default.tfstate` state file.
This file is updated when changes are made to the *.tf config files in this repo,
and updates are pushed to live resources via `terraform apply`.

## Resources

| File | Resources |
| --- | --- |
| `main.tf` | Provider, state backend, Dataflow API |
| `gcs.tf` | `contrails-301217-regional-impact-results` (published runs read by the server, plus hand-uploaded `reference/` inputs), `contrails-301217-regional-impact-intermediate` (reduce output for `derive_main` re-runs), `contrails-301217-regional-impact-dataflow-tmp` (Dataflow temp/staging, deleted after 7 days) |
| `artifact_registry.tf` | `regional-impact-explorer` Docker repository for the Dataflow worker image |
| `auth.tf` | `regional-impact-dataflow` worker service account and the IAM bindings for workers, launchers, CI and contrails-api (read-only on the results bucket) |
| `variables.tf` | Stubs for resources owned elsewhere: the flight-segment input bucket, the CI and contrails-api service accounts and the list of launchers |

The worker image is built from `data_extraction/Dockerfile` by
`.github/workflows/data-extraction-worker-image.yaml` on every push to `main`
that touches the pipeline, and tagged with the commit SHA and `latest`.

## Running the data_extraction pipeline on Dataflow

The launching environment must match the worker image: Python 3.12 and
`apache-beam[gcp]==2.77.0` (see `ARG`s in `data_extraction/Dockerfile`).

```bash
pip install -e ".[pipeline]" "apache-beam[gcp]==2.77.0"
gcloud auth application-default login

# One-off: upload reference data
python -m data_extraction.fetch_reference_data --output_dir=/tmp/ref
# ... plus firs.geojson (see data_extraction/README.md#fir-geojson)
gcloud storage cp /tmp/ref/* gs://contrails-301217-regional-impact-results/reference/

REF=gs://contrails-301217-regional-impact-results/reference
python -m data_extraction.extract_pipeline_main \
  --input_year=2024 \
  --dataset=my_dataset \
  --output_prefix=gs://contrails-301217-regional-impact-results \
  --intermediate_prefix=gs://contrails-301217-regional-impact-intermediate \
  --segments_path='gs://<segments-bucket>/<path>/2024/*.parquet' \
  --airports_path=$REF/airports.csv \
  --firs_path=$REF/firs.geojson \
  --country_geojson_path=$REF/countries.geojson \
  --population_path=$REF/population.csv \
  --ghg_emissions_path=$REF/ghg_emissions.json \
  --grid_width=3600 --grid_height=1800 \
  --min_lon=-180 --max_lon=180 --min_lat=-90 --max_lat=90 \
  --step_size_meters=10000 \
  -- --runner=DataflowRunner \
     --project=contrails-301217 \
     --region=us-east1 \
     --job_name=regional-impact-2024 \
     --service_account_email=regional-impact-dataflow@contrails-301217.iam.gserviceaccount.com \
     --temp_location=gs://contrails-301217-regional-impact-dataflow-tmp/temp \
     --staging_location=gs://contrails-301217-regional-impact-dataflow-tmp/staging \
     --sdk_container_image=us-east1-docker.pkg.dev/contrails-301217/regional-impact-explorer/data-extraction-worker:<git sha> \
     --sdk_location=container
```

Pin `--sdk_container_image` to a commit SHA rather than `latest` so a run is
reproducible and uses the same pipeline code as the checkout that launched it.
