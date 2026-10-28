"""Records what a published run was built from.

Every published run directory carries byte-for-byte copies of the reference
inputs it was derived with, plus a `provenance.json` naming where each came
from, its SHA-256, the code version, and the segment or intermediate source:

    runs/{publish_run_id}/
      provenance.json
      inputs/{name}/{basename}      e.g. inputs/airports/airports.csv

Copying (rather than only recording paths) keeps a run reproducible after the
source files are edited, moved, or deleted, and lets the reduce workers read
the copies instead of the originals, so a local reference path works on a
remote runner too.
"""

from collections.abc import Mapping
import dataclasses
import datetime
import hashlib
import os
import subprocess
from typing import Any

from absl import logging

from common import artifact_io
from common import fs

PROVENANCE_FILENAME = "provenance.json"
INPUTS_DIR = "inputs"
SCHEMA_VERSION = 1

STAGE_REDUCE = "reduce"
STAGE_DERIVE = "derive"

# Copied in bounded chunks: airports.csv alone is ~13 MB, and a FIR file at
# full ICAO resolution can be larger.
_COPY_CHUNK_BYTES = 8 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 5.0


@dataclasses.dataclass(frozen=True)
class ReferenceInput:
  """One reference input copied into a run directory.

  Attributes:
    name: Short input name, e.g. `airports`; also its directory under
      `inputs/`.
    source: Path or URI the input was read from.
    path: Absolute path of the copy inside the run directory.
    relative_path: `path` relative to the run directory, as recorded in
      `provenance.json`.
    sha256: Hex SHA-256 of the input's bytes.
    size_bytes: Size of the input in bytes.
  """

  name: str
  source: str
  path: str
  relative_path: str
  sha256: str
  size_bytes: int

  def to_dict(self) -> dict[str, Any]:
    """Serializes the record for `provenance.json` (without the run path)."""
    return {
        "source": self.source,
        "copy": self.relative_path,
        "sha256": self.sha256,
        "size_bytes": self.size_bytes,
    }


def _copy_and_hash(source: str, target: str) -> tuple[str, int]:
  """Copies `source` to `target` unchanged and returns its SHA-256 and size."""
  digest = hashlib.sha256()
  size = 0
  fs.ensure_parent_dir(target)
  with (
      fs.open_file(source, "rb") as src,
      fs.atomic_open_for_write(target, "wb") as dst,
  ):
    while chunk := src.read(_COPY_CHUNK_BYTES):
      digest.update(chunk)
      size += len(chunk)
      dst.write(chunk)
  return digest.hexdigest(), size


def copy_reference_inputs(
    run_dir: str, sources: Mapping[str, str]
) -> dict[str, ReferenceInput]:
  """Copies each named reference input into `run_dir/inputs/{name}/`.

  Every source is checked before anything is written, so a mistyped path fails
  without leaving a partial run directory behind.

  Args:
    run_dir: Published run directory to copy into.
    sources: Input name (e.g. `airports`) to source path. Empty paths are
      skipped, so optional baselines can be passed unconditionally.

  Returns:
    The copied inputs, keyed by name, in `sources` order.

  Raises:
    ValueError: If any non-empty source does not exist.
  """
  present = {name: path for name, path in sources.items() if path}
  missing = [
      f"{name}={path!r}"
      for name, path in present.items()
      if not fs.exists(path)
  ]
  if missing:
    raise ValueError(f"Reference inputs not found: {', '.join(missing)}")

  copied: dict[str, ReferenceInput] = {}
  for name, source in present.items():
    relative_path = os.path.join(
        INPUTS_DIR, name, os.path.basename(source.rstrip("/"))
    )
    target = os.path.join(run_dir, relative_path)
    sha256, size = _copy_and_hash(source, target)
    copied[name] = ReferenceInput(
        name=name,
        source=source,
        path=target,
        relative_path=relative_path,
        sha256=sha256,
        size_bytes=size,
    )
    logging.info(
        "Copied reference input %s (%d bytes, sha256 %s) from %s to %s",
        name,
        size,
        sha256,
        source,
        target,
    )
  return copied


def _run_git(*args: str) -> str | None:
  """Runs git in this checkout; returns stripped stdout, or None on failure."""
  try:
    result = subprocess.run(
        ["git", *args],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=True,
    )
  except (OSError, subprocess.SubprocessError):
    return None
  return result.stdout.strip()


def git_state() -> dict[str, Any] | None:
  """Returns the commit and dirty flag of the launching checkout, if any.

  Returns:
    `{"commit": <sha>, "dirty": <bool>}`, or None outside a git checkout (for
    example inside the worker image, which has no git).
  """
  commit = _run_git("rev-parse", "HEAD")
  if not commit:
    return None
  status = _run_git("status", "--porcelain", "--untracked-files=no")
  return {"commit": commit, "dirty": bool(status)}


def build_provenance(
    *,
    stage: str,
    dataset: str,
    year: int,
    run_id: str,
    publish_run_id: str,
    reference_inputs: Mapping[str, ReferenceInput],
    **details: Any,
) -> dict[str, Any]:
  """Assembles the `provenance.json` document for one published run.

  Args:
    stage: `STAGE_REDUCE` or `STAGE_DERIVE`.
    dataset: Dataset name.
    year: Dataset year.
    run_id: Intermediate run id the artifacts come from.
    publish_run_id: Published run directory name.
    reference_inputs: Copied inputs from `copy_reference_inputs`.
    **details: Stage-specific fields (segments, intermediate, runner, ...).

  Returns:
    The JSON-serializable provenance document.
  """
  return {
      "schema_version": SCHEMA_VERSION,
      "stage": stage,
      "dataset": dataset,
      "year": year,
      "run_id": run_id,
      "publish_run_id": publish_run_id,
      "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
      "pipeline_version": fs.get_version_tag(),
      "git": git_state(),
      "reference_inputs": {
          name: ref.to_dict() for name, ref in reference_inputs.items()
      },
      **details,
  }


def write_provenance(run_dir: str, provenance: Mapping[str, Any]) -> str:
  """Writes `provenance.json` into `run_dir` and returns its path."""
  path = os.path.join(run_dir, PROVENANCE_FILENAME)
  artifact_io.write_json_output(dict(provenance), path)
  return path
