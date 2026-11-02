# Dataflow worker image for the data_extraction Beam pipeline.
#
# Build from the repository root (the build context needs common/ and
# pyproject.toml as well as data_extraction/):
#
#   docker build -f data_extraction/Dockerfile .
#
# Dataflow requires the worker SDK to match the launching environment, so the
# machine that runs `python -m data_extraction.extract_pipeline_main` must use
# the same Python minor version and `apache-beam[gcp]==BEAM_VERSION`.
ARG PYTHON_VERSION=3.12
ARG BEAM_VERSION=2.77.0

FROM apache/beam_python${PYTHON_VERSION}_sdk:${BEAM_VERSION}

# ARGs declared before FROM are not visible after it unless redeclared.
ARG BEAM_VERSION

WORKDIR /opt/regional-impact-explorer

# The package installs common/ but not data_extraction/ (see pyproject.toml),
# so the SDK harness imports the pipeline code from here.
ENV PYTHONPATH=/opt/regional-impact-explorer

COPY pyproject.toml README.md ./
COPY common/ common/
COPY data_extraction/ data_extraction/

# Pinning apache-beam keeps pip from replacing the SDK the base image's boot
# entrypoint was built against. The import check fails the build, rather than
# the first Dataflow job, if a dependency is missing.
RUN pip install --no-cache-dir ".[pipeline]" "apache-beam[gcp]==${BEAM_VERSION}" \
    && cd / \
    && python -c "import data_extraction.extract_pipeline_main"

# The base image's ENTRYPOINT (/opt/apache/beam/boot) is left unchanged:
# Dataflow starts the SDK harness through it.
