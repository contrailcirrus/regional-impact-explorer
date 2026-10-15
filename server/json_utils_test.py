"""Unit tests for json_utils."""


from absl import flags
from absl.testing import absltest
from absl.testing import parameterized

from server import json_utils


def setUpModule() -> None:
  flags.FLAGS.mark_as_parsed()


class SanitizeNonFiniteFloatsTest(parameterized.TestCase):

  @parameterized.named_parameters(
      ("nan", float("nan")),
      ("pos_inf", float("inf")),
      ("neg_inf", float("-inf")),
  )
  def test_non_finite_floats_become_none(self, value):
    self.assertIsNone(json_utils.sanitize_non_finite_floats(value))

  @parameterized.parameters(0.0, -1.5, 1e308, 0, 7, True, "NaN", None)
  def test_finite_and_non_float_scalars_are_unchanged(self, value):
    self.assertIs(json_utils.sanitize_non_finite_floats(value), value)

  def test_nested_containers_are_sanitized(self):
    payload = {
        "a": float("nan"),
        "b": [1.0, float("inf"), {"c": float("-inf"), "d": "x"}],
        "e": {"f": [float("nan")], "g": 2},
    }

    self.assertEqual(
        json_utils.sanitize_non_finite_floats(payload),
        {
            "a": None,
            "b": [1.0, None, {"c": None, "d": "x"}],
            "e": {"f": [None], "g": 2},
        },
    )

  def test_input_is_not_mutated(self):
    row = {"value": float("nan"), "items": [float("inf")]}

    sanitized = json_utils.sanitize_non_finite_floats(row)

    self.assertIsNot(sanitized, row)
    self.assertIsNot(sanitized["items"], row["items"])
    self.assertNotEqual(row["value"], row["value"])  # Still NaN.
    self.assertEqual(row["items"], [float("inf")])


if __name__ == "__main__":
  absltest.main()
