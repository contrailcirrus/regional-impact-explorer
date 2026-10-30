"""Flags shared by the `reduce` and `derive` binaries.

Both stages need to know which year and dataset they are working on, where to
publish, and where the geometry and baseline inputs live. Declaring those
twice would let the two binaries drift on defaults, and would make any test
that imports both raise `DuplicateFlagError`.

Stage-specific flags stay in their own binary: parquet input and grid geometry
belong to `reduce`. `derive` defines no flags of its own; run selection
(`--run_id`, `--publish_run_id`, `--intermediate_prefix`) is shared.
"""

import posixpath
import re

from absl import flags

INPUT_YEAR = flags.DEFINE_string(
    "input_year",
    "",
    "Required. Four-digit calendar year of the input data: the directory "
    "name under {output_prefix}/ that artifacts are published to. The "
    "server lists it once that year's artifacts are present.",
)
DATASET = flags.DEFINE_string(
    "dataset",
    "",
    "Required. Dataset identifier: the directory name under "
    "{output_prefix}/{year}/ that artifacts are published to. Up to 64 "
    "letters, digits, '-' or '_'. Start the server with "
    "REGIONAL_CONTRAILS_DATASET set to the same value.",
)

_MAX_DATASET_LENGTH = 64
# Same allowlist as the server. ASCII only: str.isalnum() would also accept
# non-ASCII letters and digits, which the server rejects.
_DATASET_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def validate_input_year(year: int | str) -> int:
  """Validates a four-digit calendar year.

  Args:
    year: Calendar year as an integer or string.

  Returns:
    The validated four-digit year as an integer.

  Raises:
    ValueError: If year is empty or not a four-digit numeric string.
  """
  cleaned_year = str(year).strip()
  if not cleaned_year:
    raise ValueError(
        "--input_year is required: it names the year directory the run is "
        "published to and the calendar the metrics are computed for."
    )
  if len(cleaned_year) != 4 or not (
      cleaned_year.isascii() and cleaned_year.isdigit()
  ):
    raise ValueError(
        f"--input_year={cleaned_year!r} is invalid: use a four-digit year."
    )
  return int(cleaned_year)


def require_input_year() -> int:
  """Returns the validated `--input_year` value.

  Returns:
    The four-digit calendar year as an integer.

  Raises:
    ValueError: If `--input_year` is unset, empty, or invalid.
  """
  return validate_input_year(INPUT_YEAR.value or "")


def validate_dataset(dataset: str) -> str:
  """Validates a dataset identifier string.

  Args:
    dataset: Dataset identifier string to validate.

  Returns:
    The validated dataset identifier string.

  Raises:
    ValueError: If the dataset is empty, exceeds maximum length, or contains
      invalid characters.
  """
  cleaned_dataset = dataset.strip()
  if not cleaned_dataset:
    raise ValueError(
        "--dataset is required: it names the directory the run is published "
        "to, and the server must be started with REGIONAL_CONTRAILS_DATASET "
        "set to the same value."
    )
  if len(
      cleaned_dataset
  ) > _MAX_DATASET_LENGTH or not _DATASET_PATTERN.fullmatch(cleaned_dataset):
    raise ValueError(
        f"--dataset={cleaned_dataset!r} is invalid: use at most"
        f" {_MAX_DATASET_LENGTH} ASCII letters, digits, '-' or '_'."
    )
  return cleaned_dataset


def require_dataset() -> str:
  """Returns the validated `--dataset` value.

  Returns:
    The validated dataset identifier string.

  Raises:
    ValueError: If `--dataset` is unset, empty, or invalid.
  """
  return validate_dataset(DATASET.value or "")


def require_path(flag_name: str) -> str:
  """Returns the stripped value of a required path flag.

  Path flags default to empty so that no run silently reads or writes a
  location it did not name. Checking them up front turns a missing value into
  an immediate error instead of a failure after the slow geometry load, or, for
  `--output_prefix`, a write to a path relative to the working directory.

  Args:
    flag_name: Name of a registered string flag, without the leading `--`.

  Returns:
    The non-empty stripped path string from the flag.

  Raises:
    ValueError: If the flag is unset or blank.
  """
  value = (flags.FLAGS[flag_name].value or "").strip()
  if not value:
    raise ValueError(f"--{flag_name} is required.")
  return value


def _normalize_prefix(prefix: str) -> str:
  """Normalizes a path or URI prefix for equality checks."""
  return posixpath.normpath(prefix.strip().rstrip("/") or "/")


def require_distinct_prefixes(
    *, intermediate_root: str, output_root: str
) -> None:
  """Rejects an intermediate root that equals the published output root.

  The intermediate and the published artifacts share the
  `{root}/{year}/{dataset}/runs/{run_id}/` layout, so with equal roots the
  intermediate manifest, aggregates and grid slices would land inside the
  published run directory that the server reads.

  Args:
    intermediate_root: Value of `--intermediate_prefix`; empty means disabled.
    output_root: Value of `--output_prefix`.

  Raises:
    ValueError: If both roots name the same location after normalization.
  """
  if not intermediate_root.strip():
    return
  if _normalize_prefix(intermediate_root) == _normalize_prefix(output_root):
    raise ValueError(
        "--intermediate_prefix and --output_prefix must name different roots;"
        f" both resolve to {_normalize_prefix(output_root)!r}."
    )


OUTPUT_PREFIX = flags.DEFINE_string(
    "output_prefix",
    "",
    "Target root where published JSON and binary grid files are written.",
)

# Intermediate (reduce -> derive) handoff.
#
# Off by default on the reduce side: a normal run pays nothing. When set, the
# expensive half of the pipeline persists its aggregates and raw grid slices,
# so derivation changes can be republished in minutes instead of re-reading
# every segment Parquet file.
INTERMEDIATE_PREFIX = flags.DEFINE_string(
    "intermediate_prefix",
    "",
    "Root for the reduce-stage intermediate, laid out as "
    "{prefix}/{year}/{dataset}/runs/{run_id}/. Empty disables it.",
)

# One identity per invocation, shared by the intermediate and the published
# output, so a published dataset can always be traced back to the aggregates
# it came from. Reduce writes this run; derive reads it.
RUN_ID = flags.DEFINE_string(
    "run_id",
    "",
    "Run identifier. Reduce defaults to a fresh UTC-stamped id; derive "
    "requires an explicit one so a re-derivation is reproducible.",
)

# One budget for both stages: reduce refuses to finish a run that lost more
# shards than this, and derive refuses to republish an intermediate whose
# manifest records more. A lossy run is thus never published by accident from
# either side.
MAX_FAILED_SHARDS = flags.DEFINE_integer(
    "max_failed_shards",
    0,
    "Number of shards that may fail to be read before the run is declared a"
    " failure. Defaults to 0 because a dropped shard silently shrinks every"
    " downstream metric. Set to -1 to disable the check. derive applies the"
    " same budget to the count recorded in the intermediate manifest.",
)

# Publish layout. Both binaries publish into
# {output_prefix}/{year}/{dataset}/runs/{publish_run_id}/ and flip
# latest.json only once every artifact has landed, so readers never see a
# partial run.
PUBLISH_RUN_ID = flags.DEFINE_string(
    "publish_run_id",
    "",
    "Run id to publish under. Defaults to --run_id for reduce (so a dataset "
    "traces back to its aggregates) and to a fresh id for derive (so a "
    "re-derivation never overwrites the run it read from).",
)

# Geometry and baseline inputs. These are derive-time inputs: changing a
# boundary file changes the published areas without changing a single flight
# segment, which is exactly why they are not baked into the intermediate.
FIRS_PATH = flags.DEFINE_string(
    "firs_path",
    "",
    "Required. Path to FIR/UIR boundary GeoJSON. The normalized features are "
    "published as firs.geojson next to the run's metrics.",
)
EXPECTED_FIR_COUNT = flags.DEFINE_integer(
    "expected_fir_count",
    0,
    "If positive, the exact number of FIR features --firs_path must "
    "contain. Checked with the other FIR integrity checks before any flight "
    "data is read. 0 disables the count check.",
)
COUNTRY_GEOJSON_PATH = flags.DEFINE_string(
    "country_geojson_path",
    "",
    "Required. Path to country boundary GeoJSON with `iso_a2` and `name` "
    "properties (e.g. Natural Earth admin-0). Published with normalized "
    "lowercase `name`, `iso_a2`, and `iso_a3` properties as countries.geojson "
    "next to the run's metrics.",
)
POPULATION_PATH = flags.DEFINE_string(
    "population_path",
    "",
    "Path to country population CSV with columns `Country Code` (ISO 3166-1 "
    "alpha-3), `Year`, `Value`. The latest year <= --input_year is used. "
    "At least one of this, --ghg_emissions_path or "
    "--country_stats_path is required.",
)
GHG_EMISSIONS_PATH = flags.DEFINE_string(
    "ghg_emissions_path",
    "",
    "Path to Climate Watch historical emissions JSON (records with "
    "`iso_code3`; `Total excluding LULUCF` / `All GHG` / `Climate Watch`, in "
    "Mt CO2e). The latest year <= --input_year is used.",
)
COUNTRY_STATS_PATH = flags.DEFINE_string(
    "country_stats_path",
    "",
    'Optional JSON `{ISO2: {"population": ..., "ghg_mtco2e": ...}}` '
    "applied after the population and GHG files, per field. Use it for "
    "codes outside ISO 3166-1 (e.g. XK) or to correct individual values.",
)


def validate_baseline_source(
    population_path: str = "",
    ghg_emissions_path: str = "",
    country_stats_path: str = "",
) -> None:
  """Validates that at least one country baseline source is configured.

  Args:
    population_path: Optional path to country population CSV.
    ghg_emissions_path: Optional path to Climate Watch GHG emissions JSON.
    country_stats_path: Optional path to country stats override JSON.

  Raises:
    ValueError: If none of the baseline source paths are provided.
  """
  if not any(
      path.strip()
      for path in (population_path, ghg_emissions_path, country_stats_path)
  ):
    raise ValueError(
        "No country baseline source configured; pass at least one of "
        "--population_path, --ghg_emissions_path, or --country_stats_path."
    )


def require_baseline_source() -> None:
  """Checks that at least one country baseline flag is set.

  Raises:
    ValueError: If none of the country baseline flags are set.
  """
  validate_baseline_source(
      population_path=POPULATION_PATH.value or "",
      ghg_emissions_path=GHG_EMISSIONS_PATH.value or "",
      country_stats_path=COUNTRY_STATS_PATH.value or "",
  )
