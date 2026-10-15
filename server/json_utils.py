"""Helpers for making decoded JSON payloads safe to serve.

This module has no web-framework dependencies so that both the data loaders
and the response models can share it.
"""

import math
from typing import Any


def sanitize_non_finite_floats(value: Any) -> Any:
  """Recursively replaces non-finite floats (NaN, ±Inf) with None.

  Python's `json` module decodes the non-standard `NaN`, `Infinity` and
  `-Infinity` literals into floats, but strict JSON encoders reject them on the
  way out. Mapping them to None serializes them as `null`.

  Args:
    value: A decoded JSON value: a scalar, or a dict or list nesting them.

  Returns:
    A copy of `value` in which every non-finite float is replaced with None.
    Dicts and lists are rebuilt; all other values are returned unchanged.
  """
  if isinstance(value, float):
    return value if math.isfinite(value) else None
  if isinstance(value, dict):
    return {
        key: sanitize_non_finite_floats(item) for key, item in value.items()
    }
  if isinstance(value, list):
    return [sanitize_non_finite_floats(item) for item in value]
  return value
