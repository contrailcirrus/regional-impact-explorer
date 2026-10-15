"""Unit tests for the root pytest configuration."""

from absl import flags
from absl.testing import absltest
from absl.testing import flagsaver

import conftest


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


class ConftestTest(absltest.TestCase):

  def test_pytest_configure_marks_unparsed_flags(self):
    saved_flags = flagsaver.save_flag_values()
    self.addCleanup(flagsaver.restore_flag_values, saved_flags)
    self.addCleanup(flags.FLAGS.mark_as_parsed)
    flags.FLAGS.unparse_flags()
    self.assertFalse(flags.FLAGS.is_parsed())
    conftest.pytest_configure(None)
    self.assertTrue(flags.FLAGS.is_parsed())


if __name__ == "__main__":
  absltest.main()
