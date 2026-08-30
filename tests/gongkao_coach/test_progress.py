from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "gongkao-data-analysis-coach"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

from coachlib.progress import classify_mastery, wilson_interval  # noqa: E402


class WilsonIntervalTests(unittest.TestCase):
    def test_two_perfect_answers_do_not_look_like_certain_mastery(self) -> None:
        low, high = wilson_interval(2, 2)

        self.assertLess(low, 0.6)
        self.assertEqual(high, 1.0)

    def test_empty_sample_returns_full_uncertainty_interval(self) -> None:
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))

    def test_interval_is_bounded_and_ordered(self) -> None:
        low, high = wilson_interval(73, 100)

        self.assertGreaterEqual(low, 0.0)
        self.assertLessEqual(high, 1.0)
        self.assertLessEqual(low, 0.73)
        self.assertGreaterEqual(high, 0.73)

    def test_invalid_wilson_arguments_are_rejected(self) -> None:
        cases = (
            lambda: wilson_interval(-1, 2),
            lambda: wilson_interval(3, 2),
            lambda: wilson_interval(1, -2),
            lambda: wilson_interval(1.5, 2),
            lambda: wilson_interval(1, 2.5),
            lambda: wilson_interval(1, 2, z=0.0),
            lambda: wilson_interval(1, 2, z=math.inf),
        )

        for operation in cases:
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    operation()


class MasteryClassificationTests(unittest.TestCase):
    def test_zero_attempts_are_unassessed(self) -> None:
        result = classify_mastery(
            total=0,
            correct=0,
            recent_accuracy=0.0,
            method_low_rate=0.0,
            speed_met=False,
            transfer_passed=False,
            review_passes=0,
        )

        self.assertEqual(result, "未评估")

    def test_two_perfect_answers_remain_insufficient(self) -> None:
        result = classify_mastery(
            total=2,
            correct=2,
            recent_accuracy=1.0,
            method_low_rate=0.0,
            speed_met=True,
            transfer_passed=False,
            review_passes=0,
        )

        self.assertEqual(result, "样本不足")

    def test_five_attempts_are_still_learning(self) -> None:
        result = classify_mastery(
            total=5,
            correct=5,
            recent_accuracy=1.0,
            method_low_rate=0.0,
            speed_met=True,
            transfer_passed=True,
            review_passes=2,
        )

        self.assertEqual(result, "学习中")

    def test_basic_mastery_requires_all_explicit_thresholds(self) -> None:
        passing = classify_mastery(
            total=8,
            correct=7,
            recent_accuracy=0.80,
            method_low_rate=0.30,
            speed_met=True,
            transfer_passed=False,
            review_passes=1,
        )
        no_review = classify_mastery(
            total=8,
            correct=7,
            recent_accuracy=0.80,
            method_low_rate=0.30,
            speed_met=True,
            transfer_passed=False,
            review_passes=0,
        )

        self.assertEqual(passing, "基本掌握")
        self.assertEqual(no_review, "学习中")

    def test_stable_mastery_requires_speed_transfer_and_two_reviews(self) -> None:
        passing = classify_mastery(
            total=15,
            correct=14,
            recent_accuracy=0.90,
            method_low_rate=0.15,
            speed_met=True,
            transfer_passed=True,
            review_passes=2,
        )
        too_slow = classify_mastery(
            total=15,
            correct=14,
            recent_accuracy=0.90,
            method_low_rate=0.15,
            speed_met=False,
            transfer_passed=True,
            review_passes=2,
        )

        self.assertEqual(passing, "稳定掌握")
        self.assertEqual(too_slow, "基本掌握")

    def test_invalid_mastery_inputs_are_rejected(self) -> None:
        valid = {
            "total": 8,
            "correct": 7,
            "recent_accuracy": 0.8,
            "method_low_rate": 0.2,
            "speed_met": True,
            "transfer_passed": True,
            "review_passes": 1,
        }
        invalid_overrides = (
            {"total": -1},
            {"correct": -1},
            {"correct": 9},
            {"recent_accuracy": -0.01},
            {"recent_accuracy": 1.01},
            {"method_low_rate": -0.01},
            {"method_low_rate": 1.01},
            {"review_passes": -1},
            {"speed_met": 1},
            {"transfer_passed": "yes"},
        )

        for override in invalid_overrides:
            arguments = valid | override
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    classify_mastery(**arguments)


if __name__ == "__main__":
    unittest.main()
