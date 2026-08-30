"""Deterministic formulas and option-gap checks for data-analysis questions."""

from __future__ import annotations

import math
from numbers import Real
from typing import Sequence


def _finite_number(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _growth_rate(value: float, name: str) -> float:
    rate = _finite_number(value, name)
    if rate <= -1.0:
        raise ValueError(f"{name} must be greater than -1")
    return rate


def _nonzero(value: float, name: str) -> float:
    result = _finite_number(value, name)
    if result == 0.0:
        raise ValueError(f"{name} denominator must not be zero")
    return result


def base_period(current: float, rate: float) -> float:
    """Return the base-period value from current value and growth rate."""

    current_value = _finite_number(current, "current")
    valid_rate = _growth_rate(rate, "rate")
    return current_value / (1.0 + valid_rate)


def growth_amount(current: float, rate: float) -> float:
    """Return growth amount when current value and growth rate are known."""

    current_value = _finite_number(current, "current")
    valid_rate = _growth_rate(rate, "rate")
    return current_value * valid_rate / (1.0 + valid_rate)


def interval_growth(r1: float, r2: float) -> float:
    """Combine two consecutive growth rates, retaining the cross term."""

    first = _growth_rate(r1, "r1")
    second = _growth_rate(r2, "r2")
    return math.fsum((first, second, first * second))


def ratio_growth(num_rate: float, den_rate: float) -> float:
    """Return growth rate of a ratio from numerator and denominator rates."""

    numerator_rate = _growth_rate(num_rate, "num_rate")
    denominator_rate = _growth_rate(den_rate, "den_rate")
    return (numerator_rate - denominator_rate) / (1.0 + denominator_rate)


def product_growth(r1: float, r2: float) -> float:
    """Return growth rate of a product from its two component growth rates."""

    first = _growth_rate(r1, "r1")
    second = _growth_rate(r2, "r2")
    return math.fsum((first, second, first * second))


def current_share(part: float, whole: float) -> float:
    """Return current-period share."""

    part_value = _finite_number(part, "part")
    whole_value = _nonzero(whole, "whole")
    return part_value / whole_value


def base_share(
    part: float,
    whole: float,
    part_rate: float,
    whole_rate: float,
) -> float:
    """Return base-period share from current values and growth rates."""

    current = current_share(part, whole)
    valid_part_rate = _growth_rate(part_rate, "part_rate")
    valid_whole_rate = _growth_rate(whole_rate, "whole_rate")
    return current * (1.0 + valid_whole_rate) / (1.0 + valid_part_rate)


def share_change(
    part: float,
    whole: float,
    part_rate: float,
    whole_rate: float,
) -> float:
    """Return current share minus base share as a decimal share difference.

    For example, ``0.01`` means one percentage point; multiply the returned
    value by 100 when displaying the result in percentage points.
    """

    current = current_share(part, whole)
    valid_part_rate = _growth_rate(part_rate, "part_rate")
    valid_whole_rate = _growth_rate(whole_rate, "whole_rate")
    return (
        current
        * (valid_part_rate - valid_whole_rate)
        / (1.0 + valid_part_rate)
    )


def contribution_rate(part_delta: float, whole_delta: float) -> float:
    """Return a part's contribution to the whole's growth amount."""

    part_growth = _finite_number(part_delta, "part_delta")
    whole_growth = _nonzero(whole_delta, "whole_delta")
    return part_growth / whole_growth


_BIAS_ALIASES = {
    "two-sided": "two-sided",
    "unknown": "two-sided",
    "high": "high",
    "overestimate": "high",
    "偏大": "high",
    "low": "low",
    "underestimate": "low",
    "偏小": "low",
}


def assess_estimate_safety(
    estimate: float,
    absolute_error_bound: float,
    options: Sequence[float],
    bias: str = "two-sided",
) -> dict[str, object]:
    """Assess whether an approximation can safely select the nearest option.

    ``bias="high"`` means the estimate is known to be no smaller than the
    exact value; ``bias="low"`` means it is no greater than the exact value.
    The default uses a symmetric error interval.  Safety is strict: touching a
    midpoint between two options is unsafe because it no longer gives a unique
    choice.
    """

    estimate_value = _finite_number(estimate, "estimate")
    error = _finite_number(absolute_error_bound, "absolute_error_bound")
    if error < 0.0:
        raise ValueError("absolute_error_bound must be non-negative")
    if not isinstance(bias, str) or bias not in _BIAS_ALIASES:
        raise ValueError(
            "bias must be one of: two-sided, unknown, high, low, "
            "overestimate, underestimate, 偏大, 偏小"
        )
    normalized_bias = _BIAS_ALIASES[bias]

    if isinstance(options, (str, bytes)):
        raise ValueError("options must be a sequence of at least two numbers")
    try:
        option_values = [
            _finite_number(value, f"options[{index}]")
            for index, value in enumerate(options)
        ]
    except TypeError as error_detail:
        raise ValueError(
            "options must be a sequence of at least two numbers"
        ) from error_detail
    if len(option_values) < 2:
        raise ValueError("options must contain at least two values")
    if len(option_values) > 26:
        raise ValueError("options supports at most 26 labelled values")
    if len(set(option_values)) != len(option_values):
        raise ValueError("options must contain unique values")

    if normalized_bias == "high":
        exact_low, exact_high = estimate_value - error, estimate_value
    elif normalized_bias == "low":
        exact_low, exact_high = estimate_value, estimate_value + error
    else:
        exact_low = estimate_value - error
        exact_high = estimate_value + error

    distances = [abs(value - estimate_value) for value in option_values]
    smallest_distance = min(distances)
    tied_indices = [
        index
        for index, distance in enumerate(distances)
        if math.isclose(
            distance,
            smallest_distance,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
    ]
    common = {
        "estimate": estimate_value,
        "absolute_error_bound": error,
        "bias": normalized_bias,
        "plausible_exact_interval": [exact_low, exact_high],
    }
    if len(tied_indices) != 1:
        return {
            **common,
            "chosen_index": None,
            "chosen_label": None,
            "chosen_value": None,
            "nearest_competitor_index": None,
            "nearest_competitor_label": None,
            "nearest_competitor_value": None,
            "decision_boundary": estimate_value,
            "decision_margin": 0.0,
            "residual_margin": -error,
            "safe": False,
            "failure_reason": "估值与多个选项等距，无法唯一选择",
        }

    chosen_index = tied_indices[0]
    chosen_value = option_values[chosen_index]
    lower_candidates = [
        (value, index)
        for index, value in enumerate(option_values)
        if value < chosen_value
    ]
    upper_candidates = [
        (value, index)
        for index, value in enumerate(option_values)
        if value > chosen_value
    ]

    lower_boundary = None
    lower_index = None
    if lower_candidates:
        lower_value, lower_index = max(lower_candidates)
        lower_boundary = (lower_value + chosen_value) / 2.0

    upper_boundary = None
    upper_index = None
    if upper_candidates:
        upper_value, upper_index = min(upper_candidates)
        upper_boundary = (chosen_value + upper_value) / 2.0

    boundary_entries: list[tuple[float, float, int, str]] = []
    residual_entries: list[float] = []
    failures: list[str] = []
    chosen_label = chr(ord("A") + chosen_index)
    if lower_boundary is not None and lower_index is not None:
        boundary_entries.append(
            (
                estimate_value - lower_boundary,
                lower_boundary,
                lower_index,
                "lower",
            )
        )
        lower_residual = exact_low - lower_boundary
        residual_entries.append(lower_residual)
        if lower_residual <= 0.0:
            competitor_label = chr(ord("A") + lower_index)
            failures.append(
                f"误差区间跨越 {competitor_label}/{chosen_label} "
                f"的选择边界 {lower_boundary:g}"
            )
    if upper_boundary is not None and upper_index is not None:
        boundary_entries.append(
            (
                upper_boundary - estimate_value,
                upper_boundary,
                upper_index,
                "upper",
            )
        )
        upper_residual = upper_boundary - exact_high
        residual_entries.append(upper_residual)
        if upper_residual <= 0.0:
            competitor_label = chr(ord("A") + upper_index)
            failures.append(
                f"误差区间跨越 {chosen_label}/{competitor_label} "
                f"的选择边界 {upper_boundary:g}"
            )

    decision_margin, boundary, competitor_index, _ = min(boundary_entries)
    residual_margin = min(residual_entries)
    return {
        **common,
        "chosen_index": chosen_index,
        "chosen_label": chosen_label,
        "chosen_value": chosen_value,
        "nearest_competitor_index": competitor_index,
        "nearest_competitor_label": chr(ord("A") + competitor_index),
        "nearest_competitor_value": option_values[competitor_index],
        "decision_boundary": boundary,
        "decision_margin": decision_margin,
        "residual_margin": residual_margin,
        "safe": not failures,
        "failure_reason": "; ".join(failures) if failures else None,
    }
