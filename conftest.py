"""Pytest configuration for the Regional Contrails Dashboard test suite."""

from typing import Any

from absl import flags


def pytest_configure(config: Any) -> None:
  """Marks absl flags as parsed when tests run under pytest."""
  del config
  flags.FLAGS.mark_as_parsed()
