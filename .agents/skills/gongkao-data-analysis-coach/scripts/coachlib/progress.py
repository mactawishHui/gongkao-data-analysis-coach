"""Evidence-aware confidence intervals and mastery classification."""

from __future__ import annotations

import math
from numbers import Real


WILSON_90_Z = 1.6448536269514722


def _count(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _proportion(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a number between 0 and 1")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{name} must be a number between 0 and 1")
    return result


def wilson_interval(
    successes: int,
    total: int,
    z: float = WILSON_90_Z,
) -> tuple[float, float]:
    """Return a Wilson score interval; an empty sample has full uncertainty."""

    valid_successes = _count(successes, "successes")
    valid_total = _count(total, "total")
    if valid_successes > valid_total:
        raise ValueError("successes must not exceed total")
    if isinstance(z, bool) or not isinstance(z, Real):
        raise ValueError("z must be a finite positive number")
    valid_z = float(z)
    if not math.isfinite(valid_z) or valid_z <= 0.0:
        raise ValueError("z must be a finite positive number")
    if valid_total == 0:
        return 0.0, 1.0

    observed = valid_successes / valid_total
    z_squared = valid_z * valid_z
    denominator = 1.0 + z_squared / valid_total
    center = (observed + z_squared / (2.0 * valid_total)) / denominator
    half_width = (
        valid_z
        * math.sqrt(
            observed * (1.0 - observed) / valid_total
            + z_squared / (4.0 * valid_total * valid_total)
        )
        / denominator
    )
    low = max(0.0, center - half_width)
    high = min(1.0, center + half_width)
    if valid_successes == 0:
        low = 0.0
    if valid_successes == valid_total:
        high = 1.0
    return low, high


def classify_mastery(
    *,
    total: int,
    correct: int,
    recent_accuracy: float,
    method_low_rate: float,
    speed_met: bool,
    transfer_passed: bool,
    review_passes: int,
) -> str:
    """Classify a knowledge node using the design's evidence thresholds.

    ``correct`` is used to validate consistency with ``total``.  Mastery
    thresholds use the caller-provided ``recent_accuracy``; the database layer
    must derive these inputs consistently from its authoritative attempts and
    configured recent window.
    """

    valid_total = _count(total, "total")
    valid_correct = _count(correct, "correct")
    if valid_correct > valid_total:
        raise ValueError("correct must not exceed total")
    valid_recent_accuracy = _proportion(recent_accuracy, "recent_accuracy")
    valid_method_low_rate = _proportion(method_low_rate, "method_low_rate")
    valid_review_passes = _count(review_passes, "review_passes")
    if not isinstance(speed_met, bool):
        raise ValueError("speed_met must be a boolean")
    if not isinstance(transfer_passed, bool):
        raise ValueError("transfer_passed must be a boolean")

    if valid_total == 0:
        return "未评估"
    if valid_total <= 4:
        return "样本不足"

    stable = (
        valid_total >= 15
        and valid_recent_accuracy >= 0.90
        and speed_met
        and valid_method_low_rate <= 0.15
        and transfer_passed
        and valid_review_passes >= 2
    )
    if stable:
        return "稳定掌握"

    basic = (
        valid_total >= 8
        and valid_recent_accuracy >= 0.80
        and valid_method_low_rate <= 0.30
        and valid_review_passes >= 1
    )
    if basic:
        return "基本掌握"
    return "学习中"
