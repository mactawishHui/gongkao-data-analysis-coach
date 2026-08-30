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

from coachlib.formulas import (  # noqa: E402
    assess_estimate_safety,
    base_period,
    base_share,
    contribution_rate,
    current_share,
    growth_amount,
    interval_growth,
    product_growth,
    ratio_growth,
    share_change,
)


class FormulaTests(unittest.TestCase):
    def test_base_period_from_current_and_rate(self) -> None:
        self.assertAlmostEqual(base_period(120.0, 0.20), 100.0)

    def test_growth_amount_uses_current_period_identity(self) -> None:
        result = growth_amount(72414.0, 0.058)

        self.assertEqual(round(result, 1), 3969.8)
        self.assertEqual(round(result), 3970)

    def test_interval_growth_keeps_cross_term(self) -> None:
        self.assertAlmostEqual(interval_growth(0.10, 0.20), 0.32)

    def test_ratio_growth_preserves_small_positive_direction(self) -> None:
        self.assertGreater(ratio_growth(0.089, 0.087), 0.0)
        self.assertAlmostEqual(
            ratio_growth(0.089, 0.087),
            (0.089 - 0.087) / 1.087,
        )

    def test_product_growth_keeps_cross_term(self) -> None:
        self.assertAlmostEqual(product_growth(0.10, -0.20), -0.12)

    def test_complete_decline_is_valid_when_the_formula_remains_defined(self) -> None:
        self.assertEqual(interval_growth(0.10, -1.0), -1.0)
        self.assertEqual(ratio_growth(-1.0, 0.10), -1.0)
        self.assertEqual(product_growth(-1.0, 0.10), -1.0)
        self.assertEqual(product_growth(0.10, -1.0), -1.0)

    def test_share_formulas(self) -> None:
        self.assertAlmostEqual(current_share(30.0, 120.0), 0.25)
        self.assertAlmostEqual(
            base_share(30.0, 120.0, part_rate=0.20, whole_rate=0.10),
            0.25 * 1.10 / 1.20,
        )
        self.assertAlmostEqual(
            share_change(30.0, 120.0, part_rate=0.20, whole_rate=0.10),
            0.25 * 0.10 / 1.20,
        )

    def test_contribution_rate(self) -> None:
        self.assertAlmostEqual(contribution_rate(30.0, 120.0), 0.25)
        self.assertLess(
            contribution_rate(-3.0, -10.0),
            contribution_rate(-7.0, -10.0),
        )

    def test_zero_denominators_raise_clear_value_error(self) -> None:
        cases = (
            ("base_period", lambda: base_period(10.0, -1.0)),
            ("growth_amount", lambda: growth_amount(10.0, -1.0)),
            ("ratio_growth", lambda: ratio_growth(0.1, -1.0)),
            ("current_share", lambda: current_share(1.0, 0.0)),
            (
                "base_share whole",
                lambda: base_share(1.0, 0.0, 0.1, 0.1),
            ),
            (
                "base_share part rate",
                lambda: base_share(1.0, 2.0, -1.0, 0.1),
            ),
            (
                "share_change whole",
                lambda: share_change(1.0, 0.0, 0.1, 0.1),
            ),
            (
                "share_change part rate",
                lambda: share_change(1.0, 2.0, -1.0, 0.1),
            ),
            (
                "contribution_rate",
                lambda: contribution_rate(1.0, 0.0),
            ),
        )

        for label, operation in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "zero|greater than -1"):
                    operation()

    def test_rates_at_or_below_negative_one_are_rejected(self) -> None:
        cases = (
            lambda: base_period(10.0, -1.01),
            lambda: growth_amount(10.0, -2.0),
            lambda: interval_growth(-1.0, 0.1),
            lambda: ratio_growth(0.1, -1.0),
            lambda: interval_growth(0.1, -1.01),
            lambda: ratio_growth(-1.01, 0.1),
            lambda: product_growth(-1.01, 0.1),
            lambda: product_growth(0.1, -1.01),
            lambda: base_share(1.0, 2.0, 0.1, -1.0),
            lambda: share_change(1.0, 2.0, 0.1, -1.0),
        )

        for operation in cases:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "greater than -1|at least -1"):
                    operation()

    def test_non_finite_inputs_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            base_period(math.inf, 0.1)
        with self.assertRaisesRegex(ValueError, "finite"):
            current_share(1.0, math.nan)

    def test_ratio_first_order_avoids_recoverable_overflow(self) -> None:
        amount = growth_amount(1e308, 10.0)
        base_ratio = base_share(1e308, 1.0, 100.0, 10.0)
        share_delta = share_change(1e308, 1.0, 100.0, 10.0)

        self.assertTrue(math.isfinite(amount))
        self.assertTrue(math.isfinite(base_ratio))
        self.assertTrue(math.isfinite(share_delta))
        self.assertAlmostEqual(amount / 1e307, 100.0 / 11.0)
        self.assertAlmostEqual(base_ratio / 1e307, 110.0 / 101.0)
        self.assertAlmostEqual(share_delta / 1e307, 900.0 / 101.0)

    def test_share_formulas_avoid_recoverable_three_factor_overflow(self) -> None:
        part_rate = -0.9999999999999999
        base_ratio = base_share(1.0, 1e308, part_rate, 1e308)
        share_delta = share_change(1.0, 1e308, part_rate, 1e308)

        expected_scale = 1.0 / (1.0 + part_rate)
        self.assertTrue(math.isfinite(base_ratio))
        self.assertTrue(math.isfinite(share_delta))
        self.assertAlmostEqual(base_ratio / expected_scale, 1.0, places=14)
        self.assertAlmostEqual(share_delta / expected_scale, -1.0, places=14)

    def test_combined_growth_avoids_catastrophic_cancellation(self) -> None:
        almost_minus_one = math.nextafter(-1.0, math.inf)
        expected = 1.1102230246251566e292

        for operation in (interval_growth, product_growth):
            with self.subTest(operation=operation.__name__):
                result = operation(almost_minus_one, 1e308)
                self.assertAlmostEqual(result / expected, 1.0, places=14)

    def test_combined_growth_preserves_subnormal_and_tiny_rates(self) -> None:
        for tiny in (1e-80, math.ulp(0.0)):
            for operation in (interval_growth, product_growth):
                with self.subTest(tiny=tiny, operation=operation.__name__):
                    self.assertEqual(operation(tiny, 0.0), tiny)


class EstimateSafetyTests(unittest.TestCase):
    def test_wide_option_gap_is_safe_and_reports_margin(self) -> None:
        result = assess_estimate_safety(
            estimate=3970.0,
            absolute_error_bound=10.0,
            options=[3500.0, 3800.0, 3970.0, 4300.0],
            bias="two-sided",
        )

        self.assertTrue(result["safe"])
        self.assertEqual(result["chosen_label"], "C")
        self.assertEqual(result["chosen_value"], 3970.0)
        self.assertEqual(result["nearest_competitor_label"], "B")
        self.assertAlmostEqual(result["decision_margin"], 85.0)
        self.assertAlmostEqual(result["residual_margin"], 75.0)
        self.assertEqual(result["plausible_exact_interval"], [3960.0, 3980.0])
        self.assertIsNone(result["failure_reason"])

    def test_bias_direction_changes_the_plausible_exact_interval(self) -> None:
        overestimate = assess_estimate_safety(
            estimate=3955.0,
            absolute_error_bound=10.0,
            options=[3900.0, 4000.0],
            bias="high",
        )
        underestimate = assess_estimate_safety(
            estimate=3955.0,
            absolute_error_bound=10.0,
            options=[3900.0, 4000.0],
            bias="low",
        )

        self.assertEqual(overestimate["plausible_exact_interval"], [3945.0, 3955.0])
        self.assertFalse(overestimate["safe"])
        self.assertIn("跨越", overestimate["failure_reason"])
        self.assertEqual(underestimate["plausible_exact_interval"], [3955.0, 3965.0])
        self.assertTrue(underestimate["safe"])

    def test_one_sided_margin_uses_only_the_reachable_boundary(self) -> None:
        result = assess_estimate_safety(
            estimate=96.0,
            absolute_error_bound=10.0,
            options=[90.0, 100.0, 120.0],
            bias="low",
        )

        self.assertTrue(result["safe"])
        self.assertEqual(result["chosen_label"], "B")
        self.assertEqual(result["nearest_competitor_label"], "C")
        self.assertEqual(result["decision_boundary"], 110.0)
        self.assertEqual(result["decision_margin"], 14.0)
        self.assertEqual(result["residual_margin"], 4.0)

    def test_one_sided_error_moving_away_from_all_options_is_unbounded_safe(self) -> None:
        result = assess_estimate_safety(
            estimate=121.0,
            absolute_error_bound=1000.0,
            options=[90.0, 100.0, 120.0],
            bias="low",
        )

        self.assertTrue(result["safe"])
        self.assertEqual(result["chosen_label"], "C")
        self.assertIsNone(result["nearest_competitor_label"])
        self.assertIsNone(result["decision_boundary"])
        self.assertIsNone(result["decision_margin"])
        self.assertIsNone(result["residual_margin"])

    def test_dense_options_are_declared_unsafe(self) -> None:
        result = assess_estimate_safety(
            estimate=100.0,
            absolute_error_bound=3.0,
            options=[94.0, 99.0, 101.0, 106.0],
        )

        self.assertFalse(result["safe"])
        self.assertEqual(result["decision_margin"], 0.0)
        self.assertIsNotNone(result["failure_reason"])

    def test_error_interval_touching_option_midpoint_is_unsafe(self) -> None:
        result = assess_estimate_safety(
            estimate=100.0,
            absolute_error_bound=5.0,
            options=[90.0, 100.0, 120.0, 140.0],
            bias="high",
        )

        self.assertEqual(result["plausible_exact_interval"], [95.0, 100.0])
        self.assertEqual(result["decision_boundary"], 95.0)
        self.assertEqual(result["residual_margin"], 0.0)
        self.assertFalse(result["safe"])
        self.assertIn("跨越", result["failure_reason"])

    def test_decimal_roundoff_at_option_midpoint_is_conservatively_unsafe(self) -> None:
        result = assess_estimate_safety(
            estimate=0.21,
            absolute_error_bound=0.09,
            options=[0.2, 0.4],
            bias="low",
        )

        self.assertAlmostEqual(result["decision_boundary"], 0.3)
        self.assertEqual(result["residual_margin"], 0.0)

    def test_large_finite_option_midpoint_does_not_overflow(self) -> None:
        result = assess_estimate_safety(
            estimate=1e308,
            absolute_error_bound=2e307,
            options=[1e308, 1.7e308],
            bias="low",
        )

        self.assertTrue(result["safe"], result)
        self.assertTrue(math.isfinite(result["decision_boundary"]))
        self.assertAlmostEqual(result["decision_boundary"] / 1e308, 1.35)
        self.assertGreater(result["residual_margin"], 0.0)

    def test_tie_detection_uses_the_actual_float_values(self) -> None:
        close = assess_estimate_safety(0.0, 0.0, [0.0, 1e-13])
        large = assess_estimate_safety(
            1e12 + 0.1,
            0.0,
            [0.0, 2e12],
        )

        self.assertEqual(close["chosen_index"], 0)
        self.assertEqual(large["chosen_index"], 1)

    def test_nonfinite_derived_error_interval_is_rejected_explicitly(self) -> None:
        with self.assertRaisesRegex(OverflowError, "plausible exact interval"):
            assess_estimate_safety(
                1e308,
                1e308,
                [0.0, 1e308],
                bias="low",
            )

    def test_cancellation_roundoff_at_negative_midpoint_is_unsafe(self) -> None:
        result = assess_estimate_safety(
            estimate=-0.60445,
            absolute_error_bound=0.54945,
            options=[-0.61, 0.5],
            bias="low",
        )

        self.assertAlmostEqual(result["decision_boundary"], -0.055)
        self.assertEqual(result["residual_margin"], 0.0)
        self.assertFalse(result["safe"])
        self.assertIn("跨越", result["failure_reason"])

    def test_invalid_estimate_safety_inputs_are_rejected(self) -> None:
        cases = (
            lambda: assess_estimate_safety(10.0, -0.1, [9.0, 10.0]),
            lambda: assess_estimate_safety(10.0, 0.1, [10.0]),
            lambda: assess_estimate_safety(10.0, 0.1, [10.0, 10.0]),
            lambda: assess_estimate_safety(math.nan, 0.1, [9.0, 10.0]),
            lambda: assess_estimate_safety(10.0, 0.1, [9.0, math.inf]),
            lambda: assess_estimate_safety(
                10.0, 0.1, [9.0, 10.0], bias="sometimes"
            ),
        )

        for operation in cases:
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    operation()


if __name__ == "__main__":
    unittest.main()
