"""Deterministic, self-verifying practice generation for first-release nodes."""

from __future__ import annotations

import json
import hashlib
import math
import random
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any

from .formulas import (
    base_period,
    base_share,
    contribution_rate,
    current_share,
    growth_amount,
    interval_growth,
    ratio_growth,
)


TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "references"
    / "practice-templates.json"
)
LETTERS = "ABCD"
DIFFICULTIES = ("easy", "medium", "hard")
OPTION_GAP_MULTIPLIERS = {
    "easy": 1.5,
    "medium": 1.0,
    "hard": 0.5,
}
RATE_SIGNAL_GAP_MULTIPLIERS = {
    "easy": 2.0,
    "medium": 1.0,
    "hard": 0.5,
}

DATA_KEYS = {
    "abrx.base": {"current", "rate"},
    "abrx.growth-amount": {"current", "rate"},
    "growth.interval": {"first_rate", "second_rate"},
    "growth.ratio": {"numerator_rate", "denominator_rate"},
    "share.current": {"part", "whole"},
    "share.base": {"part", "whole", "part_rate", "whole_rate"},
    "share.trend": {"part_rate", "whole_rate"},
    "average.single": {"total", "count"},
    "average.annual-increase": {"start", "end", "years"},
    "special.contribution": {"part_delta", "whole_delta"},
}

PROMPT_NUMBER_FORMATS = {
    "abrx.base": {"current": "g", "rate": ".0%"},
    "abrx.growth-amount": {"current": "g", "rate": ".0%"},
    "growth.interval": {"first_rate": ".1%", "second_rate": ".1%"},
    "growth.ratio": {
        "numerator_rate": ".1%",
        "denominator_rate": ".1%",
    },
    "share.current": {"part": "g", "whole": "g"},
    "share.base": {
        "part": "g",
        "whole": "g",
        "part_rate": ".0%",
        "whole_rate": ".0%",
    },
    "share.trend": {"part_rate": ".2%", "whole_rate": ".2%"},
    "average.single": {"total": "", "count": ""},
    "average.annual-increase": {"start": "", "end": "", "years": ""},
    "special.contribution": {"part_delta": "g", "whole_delta": "g"},
}

QUICK_HINT_REQUIRED_EVIDENCE = (
    "estimate",
    "bias",
    "error_bound_derivation",
)
QUICK_HINT_SAFETY_REASON = (
    "quick_hint safety is not verified because no actual estimate, bias, "
    "or error_bound_derivation is provided"
)
ROUGH_ERROR_BOUND_SEMANTICS = (
    "legacy alias of declared_display_error_budget; not a measured "
    "quick_hint error bound"
)
OPTION_MARGIN_SEMANTICS = (
    "legacy alias of option_boundary_margin, the exact formula result's "
    "distance to the nearest option boundary"
)
VERIFICATION_SUMMARY_FIELDS = (
    "ok",
    "deterministic_formula_truth_verified",
    "unique_correct_option_verified",
    "display_rounding_verified",
    "declared_budget_margin_verified",
    "quick_hint_safety_verified",
    "actual_display_rounding_error",
    "declared_display_error_budget",
    "recomputed_option_boundary_margin",
    "quick_hint_safety_reason",
)


def _validate_difficulty(value: Any) -> str:
    if not isinstance(value, str) or value not in DIFFICULTIES:
        raise ValueError(
            "difficulty must be one of: easy, medium, hard"
        )
    return value


def _difficulty_policy(kind: str) -> dict[str, Any]:
    if kind == "qualitative":
        return {
            "basis": "rate_signal_gap",
            "multipliers": dict(RATE_SIGNAL_GAP_MULTIPLIERS),
        }
    return {
        "basis": "option_spacing",
        "multipliers": dict(OPTION_GAP_MULTIPLIERS),
    }


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _prompt_visible_decimal(
    node_id: str,
    field: str,
    value: int | float,
) -> Decimal:
    rendered = format(value, PROMPT_NUMBER_FORMATS[node_id][field])
    if rendered.endswith("%"):
        return Decimal(rendered[:-1]) / Decimal(100)
    return Decimal(rendered)


def _prompt_visible_data(
    node_id: str,
    data: dict[str, Any],
) -> dict[str, Decimal]:
    return {
        field: _prompt_visible_decimal(node_id, field, data[field])
        for field in DATA_KEYS[node_id]
    }


def _canonicalize_prompt_data(
    node_id: str,
    data: dict[str, Any],
) -> dict[str, int | float]:
    """Reduce sampled data to exactly the values visible in the prompt."""

    return {
        field: (
            int(_prompt_visible_decimal(node_id, field, value))
            if isinstance(value, int) and not isinstance(value, bool)
            else float(_prompt_visible_decimal(node_id, field, value))
        )
        for field, value in data.items()
    }


def _generation_decimal_result(
    node_id: str,
    data: dict[str, Any],
) -> Decimal:
    """Compute the numeric answer from exactly what the prompt displays."""

    values = _prompt_visible_data(node_id, data)
    one = Decimal(1)
    hundred = Decimal(100)
    with localcontext() as context:
        context.prec = 50
        if node_id == "abrx.base":
            return values["current"] / (one + values["rate"])
        if node_id == "abrx.growth-amount":
            return (
                values["current"]
                * values["rate"]
                / (one + values["rate"])
            )
        if node_id == "growth.interval":
            first = values["first_rate"]
            second = values["second_rate"]
            return hundred * (first + second + first * second)
        if node_id == "growth.ratio":
            return (
                hundred
                * (values["numerator_rate"] - values["denominator_rate"])
                / (one + values["denominator_rate"])
            )
        if node_id == "share.current":
            return hundred * values["part"] / values["whole"]
        if node_id == "share.base":
            return (
                hundred
                * values["part"]
                / values["whole"]
                * (one + values["whole_rate"])
                / (one + values["part_rate"])
            )
        if node_id == "average.single":
            return values["total"] / values["count"]
        if node_id == "average.annual-increase":
            return (
                (values["end"] - values["start"])
                / values["years"]
            )
        if node_id == "special.contribution":
            return hundred * values["part_delta"] / values["whole_delta"]
    raise ValueError(f"knowledge id is not numeric: {node_id}")


def _independent_decimal_result(
    node_id: str,
    data: dict[str, Any],
) -> Decimal:
    """Independently recalculate a numeric prompt without generator formulas."""

    visible = _prompt_visible_data(node_id, data)
    one = Decimal("1")
    hundred = Decimal("100")
    with localcontext() as context:
        context.prec = 50
        if node_id == "abrx.base":
            result = visible["current"] / (one + visible["rate"])
        elif node_id == "abrx.growth-amount":
            base = visible["current"] / (one + visible["rate"])
            result = visible["current"] - base
        elif node_id == "growth.interval":
            result = hundred * (
                (one + visible["first_rate"])
                * (one + visible["second_rate"])
                - one
            )
        elif node_id == "growth.ratio":
            result = hundred * (
                (one + visible["numerator_rate"])
                / (one + visible["denominator_rate"])
                - one
            )
        elif node_id == "share.current":
            result = hundred * visible["part"] / visible["whole"]
        elif node_id == "share.base":
            base_part = visible["part"] / (one + visible["part_rate"])
            base_whole = visible["whole"] / (one + visible["whole_rate"])
            result = hundred * base_part / base_whole
        elif node_id == "average.single":
            result = visible["total"] / visible["count"]
        elif node_id == "average.annual-increase":
            result = (
                visible["end"] - visible["start"]
            ) / visible["years"]
        elif node_id == "special.contribution":
            result = (
                hundred
                * visible["part_delta"]
                / visible["whole_delta"]
            )
        else:
            raise ValueError(f"knowledge id is not numeric: {node_id}")
    return result


def _round_half_up(value: Decimal, precision: int) -> float:
    quantum = Decimal(1).scaleb(-precision)
    with localcontext() as context:
        context.prec = max(50, value.adjusted() + precision + 5)
        rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    result = float(rounded)
    if not math.isfinite(result):
        raise OverflowError("rounded display value is outside the finite range")
    return result


def _validate_data_domain(node_id: str, data: dict[str, Any]) -> list[str]:
    if any(not _is_finite_number(value) for value in data.values()):
        return ["data domain requires finite numeric values"]

    errors: list[str] = []
    rate_fields = {
        "abrx.base": ("rate",),
        "abrx.growth-amount": ("rate",),
        "growth.interval": ("first_rate", "second_rate"),
        "growth.ratio": ("numerator_rate", "denominator_rate"),
        "share.base": ("part_rate", "whole_rate"),
        "share.trend": ("part_rate", "whole_rate"),
    }.get(node_id, ())
    for field in rate_fields:
        if not -1.0 < float(data[field]) <= 10.0:
            errors.append(f"data domain requires -100% < {field} <= 1000%")

    for field, value in data.items():
        visible_value = _prompt_visible_decimal(node_id, field, value)
        numeric_value = Decimal(str(value))
        if numeric_value != visible_value:
            errors.append(
                f"data domain requires prompt-visible precision for {field}"
            )

    if node_id in {"abrx.base", "abrx.growth-amount"}:
        if float(data["current"]) <= 0:
            errors.append("data domain requires current > 0")
    elif node_id in {"share.current", "share.base"}:
        part = float(data["part"])
        whole = float(data["whole"])
        if whole <= 0 or part < 0 or part > whole:
            errors.append("data domain requires 0 <= part <= whole and whole > 0")
        if node_id == "share.base" and not errors:
            base_part = part / (1.0 + float(data["part_rate"]))
            base_whole = whole / (1.0 + float(data["whole_rate"]))
            if base_part > base_whole + 1e-12:
                errors.append(
                    "data domain requires base-period part <= base-period whole"
                )
    elif node_id == "average.single":
        count = data["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            errors.append("data domain requires count to be a positive integer")
        if float(data["total"]) < 0:
            errors.append("data domain requires total >= 0")
    elif node_id == "average.annual-increase":
        years = data["years"]
        if isinstance(years, bool) or not isinstance(years, int) or years <= 0:
            errors.append("data domain requires years to be a positive integer")
        if float(data["start"]) < 0 or float(data["end"]) < 0:
            errors.append("data domain requires start and end >= 0")
    elif node_id == "special.contribution":
        if float(data["whole_delta"]) == 0:
            errors.append("data domain requires whole_delta != 0")
    elif node_id == "share.trend":
        if math.isclose(
            float(data["part_rate"]),
            float(data["whole_rate"]),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            errors.append(
                "data domain requires a nonzero rate gap for difficulty control"
            )
    return errors


def _load_templates() -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        templates = document["templates"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"practice templates cannot be loaded: {error}") from error
    result: dict[str, dict[str, Any]] = {}
    for template in templates:
        node_id = template.get("knowledge_id")
        if not isinstance(node_id, str) or not node_id:
            raise RuntimeError("practice template has an invalid knowledge_id")
        if node_id in result:
            raise RuntimeError(f"duplicate practice template: {node_id}")
        result[node_id] = template
    return result


def _recompute(formula_id: str, data: dict[str, Any]) -> float | str:
    if formula_id == "base_period":
        return base_period(data["current"], data["rate"])
    if formula_id == "growth_amount":
        return growth_amount(data["current"], data["rate"])
    if formula_id == "interval_growth_percentage":
        return 100.0 * interval_growth(data["first_rate"], data["second_rate"])
    if formula_id == "ratio_growth_percentage":
        return 100.0 * ratio_growth(
            data["numerator_rate"], data["denominator_rate"]
        )
    if formula_id == "current_share_percentage":
        return 100.0 * current_share(data["part"], data["whole"])
    if formula_id == "base_share_percentage":
        return 100.0 * base_share(
            data["part"],
            data["whole"],
            data["part_rate"],
            data["whole_rate"],
        )
    if formula_id == "share_trend_direction":
        difference = data["part_rate"] - data["whole_rate"]
        if difference > 0:
            return "上升"
        if difference < 0:
            return "下降"
        return "不变"
    if formula_id == "average_single":
        return data["total"] / data["count"]
    if formula_id == "average_annual_increase":
        return (data["end"] - data["start"]) / data["years"]
    if formula_id == "contribution_rate_percentage":
        return 100.0 * contribution_rate(
            data["part_delta"], data["whole_delta"]
        )
    raise ValueError(f"unknown formula id: {formula_id}")


def _numeric_margin(exact: float, options: list[float], index: int) -> float:
    chosen = options[index]
    lower = [value for value in options if value < chosen]
    upper = [value for value in options if value > chosen]
    margins: list[float] = []
    if lower:
        margins.append(exact - (max(lower) + chosen) / 2.0)
    if upper:
        margins.append((chosen + min(upper)) / 2.0 - exact)
    if not margins:
        raise ValueError("numeric question needs a competing option")
    return min(margins)


def _render_prompt(node_id: str, data: dict[str, Any]) -> str:
    if node_id == "abrx.base":
        return (
            f"某指标现期为 {data['current']:g} 万，同比增长 "
            f"{data['rate']:.0%}。基期约为多少万？"
        )
    if node_id == "abrx.growth-amount":
        return (
            f"某指标现期为 {data['current']:g}，同比增长 "
            f"{data['rate']:.0%}。增长量约为多少？"
        )
    if node_id == "growth.interval":
        return (
            f"某指标连续两年增速分别为 {data['first_rate']:.1%}、"
            f"{data['second_rate']:.1%}，两年累计增速约为多少？"
        )
    if node_id == "growth.ratio":
        return (
            f"某比值的分子增长 {data['numerator_rate']:.1%}、分母增长 "
            f"{data['denominator_rate']:.1%}，该比值增速约为多少？"
        )
    if node_id == "share.current":
        return (
            f"总体为 {data['whole']:g}，其中部分为 {data['part']:g}，"
            "部分占总体约多少？"
        )
    if node_id == "share.base":
        return (
            f"现期部分为 {data['part']:g}、总体为 {data['whole']:g}，"
            f"增速分别为 {data['part_rate']:.0%}、"
            f"{data['whole_rate']:.0%}。基期比重约为多少？"
        )
    if node_id == "share.trend":
        return (
            f"部分增长 {data['part_rate']:.2%}，总体增长 "
            f"{data['whole_rate']:.2%}，现期比重较基期如何变化？"
        )
    if node_id == "average.single":
        return (
            f"总量为 {data['total']}，共有 {data['count']} 份，"
            "平均每份约为多少？"
        )
    if node_id == "average.annual-increase":
        return (
            f"某指标从 {data['start']} 增至 {data['end']}，跨 "
            f"{data['years']} 个年度间隔，年均增加约多少？"
        )
    if node_id == "special.contribution":
        return (
            f"总体增量为 {data['whole_delta']:g}，其中某部分增量为 "
            f"{data['part_delta']:g}，其增量贡献率约为多少？"
        )
    raise ValueError(f"unknown knowledge id: {node_id}")


def _semantic_id(node_id: str, data: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"knowledge_id": node_id, "data": data},
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _verification_scope(kind: str | None) -> dict[str, Any]:
    verified = [
        "deterministic_formula_truth",
        "unique_correct_option",
        "difficulty_configuration",
    ]
    not_applicable: list[str] = []
    numeric_checks = [
        "display_rounding",
        "declared_budget_vs_option_boundary",
    ]
    if kind == "qualitative":
        not_applicable.extend(numeric_checks)
    else:
        verified.extend(numeric_checks)
    return {
        "verified": verified,
        "not_verified": ["quick_hint_safety"],
        "not_applicable": not_applicable,
        "quick_hint_evidence_required": list(QUICK_HINT_REQUIRED_EVIDENCE),
    }


def practice_catalog() -> list[dict[str, Any]]:
    """Return the deterministic template catalog without opening study state."""

    return [
        {
            "knowledge_id": template["knowledge_id"],
            "title": template["title"],
            "kind": template["kind"],
            "unit": template.get("unit"),
            "formula_id": template["formula_id"],
            "supported_difficulties": list(DIFFICULTIES),
            "difficulty_policy": _difficulty_policy(template["kind"]),
            "verification_scope": _verification_scope(template["kind"]),
            "generation_scope": template.get("generation_scope"),
            "quick_hint_safety_verified": False,
            "quick_hint_safety_reason": QUICK_HINT_SAFETY_REASON,
        }
        for template in _load_templates().values()
    ]


def _verification_result(
    errors: list[str],
    scope: dict[str, Any],
    *,
    formula_truth_verified: bool = False,
    unique_correct_option_verified: bool = False,
    display_rounding_verified: bool | None = False,
    declared_budget_margin_verified: bool | None = False,
    actual_display_rounding_error: float | None = None,
    declared_display_error_budget: float | None = None,
    recomputed_option_boundary_margin: float | None = None,
) -> dict[str, Any]:
    return {
        "ok": not errors,
        "errors": errors,
        "verification_scope": scope,
        "deterministic_formula_truth_verified": formula_truth_verified,
        "unique_correct_option_verified": unique_correct_option_verified,
        "display_rounding_verified": display_rounding_verified,
        "declared_budget_margin_verified": declared_budget_margin_verified,
        "quick_hint_safety_verified": False,
        "quick_hint_safety_reason": QUICK_HINT_SAFETY_REASON,
        "actual_display_rounding_error": actual_display_rounding_error,
        "declared_display_error_budget": declared_display_error_budget,
        "recomputed_option_boundary_margin": recomputed_option_boundary_margin,
        # Backward-compatible aliases.  Their semantics are deliberately
        # narrower than quick-hint safety.
        "margin_safe": declared_budget_margin_verified is True,
        "margin_safe_semantics": (
            "legacy alias of declared_budget_margin_verified; it does not "
            "verify quick_hint safety"
        ),
        "recomputed_option_margin": recomputed_option_boundary_margin,
    }


def _verification_summary(verification: dict[str, Any]) -> dict[str, Any]:
    return {
        field: verification[field]
        for field in VERIFICATION_SUMMARY_FIELDS
    }


def _strict_equal(left: Any, right: Any) -> bool:
    """Compare generated provenance without Python's bool/int aliasing."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _strict_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def verify_question(question: dict[str, Any]) -> dict[str, Any]:
    """Independently verify deterministic answer construction.

    This verifier does not execute or measure the human ``quick_hint``.  The
    fixed template budget is only checked against display rounding and option
    boundaries, so quick-hint safety remains explicitly unverified.
    """

    errors: list[str] = []
    scope = _verification_scope(None)
    formula_truth_verified = False
    unique_correct_option_verified = False
    display_rounding_verified: bool | None = False
    declared_budget_margin_verified: bool | None = False
    actual_rounding_error: float | None = None
    recomputed_margin: float | None = None
    declared_budget: float | None = None
    try:
        knowledge_id = question["knowledge_id"]
        prompt = question["prompt"]
        options = question["options"]
        correct_index = question["correct_index"]
        correct_label = question["correct_label"]
        correct_value = question["correct_value"]
        exact_value = question["exact_value"]
        formula_id = question["formula_id"]
        data = question["data"]
        declared_budget = question["declared_display_error_budget"]
        legacy_rough_error = question["rough_error_bound"]
        stored_margin = question["option_boundary_margin"]
        legacy_margin = question["option_margin"]
        quick_hint_safety = question["quick_hint_safety_verified"]
        stored_scope = question["verification_scope"]
        rough_semantics = question["rough_error_bound_semantics"]
        margin_semantics = question["option_margin_semantics"]
        difficulty = question["difficulty"]
        difficulty_basis = question["difficulty_basis"]
        difficulty_multiplier = question["difficulty_multiplier"]
        effective_option_gap = question["effective_option_gap"]
        question_seed = question["seed"]
    except (KeyError, TypeError) as error:
        return _verification_result(
            [f"missing or malformed field: {error}"],
            scope,
        )

    try:
        normalized_difficulty = _validate_difficulty(difficulty)
    except ValueError as error:
        errors.append(str(error))
        normalized_difficulty = None

    try:
        template = _load_templates()[knowledge_id]
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        errors.append(f"knowledge template cannot be resolved: {error}")
        template = None
    if template is not None:
        scope = _verification_scope(template.get("kind"))
        difficulty_policy = _difficulty_policy(template["kind"])
        if normalized_difficulty is None:
            expected_difficulty_multiplier = None
        else:
            expected_difficulty_multiplier = difficulty_policy[
                "multipliers"
            ][normalized_difficulty]
        expected_effective_option_gap = (
            float(template["option_gap"]) * expected_difficulty_multiplier
            if template["kind"] == "numeric"
            and expected_difficulty_multiplier is not None
            else None
        )
        expected_declared_budget = template.get(
            "declared_display_error_budget"
        )
        expected_bindings = {
            "formula_id": template["formula_id"],
            "template_title": template["title"],
            "quick_hint": template["quick_hint"],
            "unit": template.get("unit"),
            "precision": template.get("precision"),
            "declared_display_error_budget": expected_declared_budget,
            "rough_error_bound": template.get("rough_error_bound"),
            "quick_hint_safety_verified": False,
            "rough_error_bound_semantics": ROUGH_ERROR_BOUND_SEMANTICS,
            "option_margin_semantics": OPTION_MARGIN_SEMANTICS,
            "difficulty": normalized_difficulty,
            "difficulty_basis": difficulty_policy["basis"],
            "difficulty_multiplier": expected_difficulty_multiplier,
            "effective_option_gap": expected_effective_option_gap,
        }
        bindings = {
            "formula_id": formula_id,
            "template_title": question.get("template_title"),
            "quick_hint": question.get("quick_hint"),
            "unit": question.get("unit"),
            "precision": question.get("precision"),
            "declared_display_error_budget": declared_budget,
            "rough_error_bound": legacy_rough_error,
            "quick_hint_safety_verified": quick_hint_safety,
            "rough_error_bound_semantics": rough_semantics,
            "option_margin_semantics": margin_semantics,
            "difficulty": difficulty,
            "difficulty_basis": difficulty_basis,
            "difficulty_multiplier": difficulty_multiplier,
            "effective_option_gap": effective_option_gap,
        }
        for field, value in bindings.items():
            if value != expected_bindings[field]:
                errors.append(f"{field} does not match knowledge template")
        if isinstance(question_seed, bool) or not isinstance(question_seed, int):
            errors.append("seed must be an integer")
        elif normalized_difficulty is not None:
            try:
                expected_question = _build_question(
                    template,
                    knowledge_id,
                    question_seed,
                    normalized_difficulty,
                )
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                errors.append(
                    f"seed and difficulty question cannot be reconstructed: {error}"
                )
            else:
                for field, expected_value in expected_question.items():
                    if not _strict_equal(question.get(field), expected_value):
                        errors.append(
                            f"{field} does not match the declared seed and difficulty"
                        )
        if not isinstance(data, dict) or set(data) != DATA_KEYS.get(
            knowledge_id, set()
        ):
            errors.append("data fields do not match knowledge template")
        else:
            errors.extend(_validate_data_domain(knowledge_id, data))
            try:
                expected_prompt = _render_prompt(knowledge_id, data)
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                errors.append(f"prompt cannot be rendered from data: {error}")
            else:
                if prompt != expected_prompt:
                    errors.append("prompt does not match structured data")
            try:
                expected_semantic_id = _semantic_id(knowledge_id, data)
            except (TypeError, ValueError) as error:
                errors.append(f"semantic_id cannot be computed: {error}")
            else:
                if question.get("semantic_id") != expected_semantic_id:
                    errors.append("semantic_id does not match structured data")

    if stored_scope != scope:
        claimed_verified = (
            stored_scope.get("verified", [])
            if isinstance(stored_scope, dict)
            else []
        )
        if (
            isinstance(claimed_verified, (list, tuple, set))
            and "quick_hint_safety" in claimed_verified
        ):
            errors.append(
                "verification_scope cannot claim quick_hint_safety without "
                "estimate, bias, and error_bound_derivation"
            )
        else:
            errors.append("verification_scope does not match verifier scope")
    if quick_hint_safety is not False:
        errors.append(
            "quick_hint_safety_verified must be false without actual estimate, "
            "bias, and error_bound_derivation"
        )
    stored_verification = question.get("verification")
    if stored_verification is not None:
        if not isinstance(stored_verification, dict):
            errors.append("stored verification summary must be an object")
        elif stored_verification.get("quick_hint_safety_verified") is not False:
            errors.append(
                "stored verification cannot claim quick_hint safety without "
                "estimate, bias, and error_bound_derivation"
            )

    if not isinstance(options, list) or len(options) != 4:
        errors.append("options must contain exactly four values")
        return _verification_result(
            errors,
            scope,
            declared_display_error_budget=declared_budget,
        )
    if question.get("option_labels") != LETTERS:
        errors.append("option_labels must be ABCD")
    options_unique = not any(
        options[left] == options[right]
        for left in range(4)
        for right in range(left + 1, 4)
    )
    if not options_unique:
        errors.append("options must be unique")
    if (
        isinstance(correct_index, bool)
        or not isinstance(correct_index, int)
        or not 0 <= correct_index < 4
    ):
        errors.append("correct_index must be from 0 to 3")
        return _verification_result(
            errors,
            scope,
            declared_display_error_budget=declared_budget,
        )
    correct_binding = True
    if correct_label != LETTERS[correct_index]:
        errors.append("correct_label does not match correct_index")
        correct_binding = False
    if options[correct_index] != correct_value:
        errors.append("correct_value does not match the indexed option")
        correct_binding = False
    if options.count(correct_value) != 1:
        errors.append("correct_value must occur exactly once in options")
        correct_binding = False

    decimal_recomputed: Decimal | None = None
    try:
        if template is not None and template.get("kind") == "numeric":
            decimal_recomputed = _independent_decimal_result(
                knowledge_id,
                data,
            )
            recomputed: float | str | None = float(decimal_recomputed)
        else:
            recomputed = _recompute(formula_id, data)
    except (ArithmeticError, KeyError, TypeError, ValueError) as error:
        errors.append(f"formula recalculation failed: {error}")
        recomputed = None

    if isinstance(recomputed, str):
        display_rounding_verified = None
        declared_budget_margin_verified = None
        valid_qualitative_options = all(
            isinstance(value, str) for value in options
        ) and set(options) == {"上升", "下降", "不变", "无法判断"}
        if not all(isinstance(value, str) for value in options):
            errors.append("qualitative options must all be strings")
        elif not valid_qualitative_options:
            errors.append("qualitative options do not match the template")
        formula_truth_verified = exact_value == recomputed
        if not formula_truth_verified:
            errors.append("exact_value does not match formula recalculation")
        if correct_value != recomputed:
            errors.append("correct_value does not match qualitative result")
        unique_correct_option_verified = (
            valid_qualitative_options
            and options_unique
            and correct_binding
            and correct_value == recomputed
        )
        if stored_margin is not None or legacy_margin is not None:
            errors.append("qualitative option boundary margins must be null")
        if declared_budget is not None:
            errors.append("qualitative declared display budget must be null")
        if legacy_rough_error is not None:
            errors.append("qualitative rough_error_bound alias must be null")
    elif _is_finite_number(recomputed):
        formula_truth_verified = (
            _is_finite_number(exact_value)
            and float(exact_value) == float(recomputed)
        )
        if not formula_truth_verified:
            errors.append("exact_value does not match formula recalculation")

        expected_display: float | None = None
        if (
            template is not None
            and template.get("kind") == "numeric"
            and decimal_recomputed is not None
        ):
            try:
                expected_display = _round_half_up(
                    decimal_recomputed,
                    int(template["precision"]),
                )
            except (ArithmeticError, OverflowError, TypeError, ValueError) as error:
                errors.append(f"display rounding failed: {error}")
                display_rounding_verified = False
            else:
                actual_rounding_error = float(
                    abs(decimal_recomputed - Decimal(str(expected_display)))
                )
                display_rounding_verified = correct_value == expected_display
                if not display_rounding_verified:
                    errors.append("correct_value does not use template precision")

        if any(not _is_finite_number(value) for value in options):
            errors.append("numeric options must all be finite numbers")
        else:
            numeric_options = [float(value) for value in options]
            if (
                template is not None
                and template.get("kind") == "numeric"
                and normalized_difficulty is not None
            ):
                expected_gap = (
                    float(template["option_gap"])
                    * OPTION_GAP_MULTIPLIERS[normalized_difficulty]
                )
                ordered_options = sorted(numeric_options)
                observed_gaps = [
                    ordered_options[index + 1] - ordered_options[index]
                    for index in range(3)
                ]
                if any(
                    not math.isclose(
                        observed_gap,
                        expected_gap,
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    )
                    for observed_gap in observed_gaps
                ):
                    errors.append(
                        "numeric option spacing does not match difficulty"
                    )
            distances = [
                abs(value - float(recomputed)) for value in numeric_options
            ]
            minimum = min(distances)
            nearest = [
                index
                for index, distance in enumerate(distances)
                if math.isclose(
                    distance,
                    minimum,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
            ]
            unique_correct_option_verified = (
                nearest == [correct_index]
                and options_unique
                and correct_binding
            )
            if not unique_correct_option_verified:
                errors.append(
                    "formula result does not select one unique correct option"
                )
            else:
                try:
                    recomputed_margin = _numeric_margin(
                        float(recomputed),
                        numeric_options,
                        correct_index,
                    )
                except (ArithmeticError, TypeError, ValueError) as error:
                    errors.append(f"option boundary margin cannot be computed: {error}")
                else:
                    if (
                        not _is_finite_number(stored_margin)
                        or not math.isclose(
                            float(stored_margin),
                            recomputed_margin,
                            rel_tol=1e-10,
                            abs_tol=1e-10,
                        )
                    ):
                        errors.append(
                            "option_boundary_margin does not match recomputed boundary"
                        )
                    if legacy_margin != stored_margin:
                        errors.append(
                            "option_margin legacy alias does not match "
                            "option_boundary_margin"
                        )

        budget_valid = (
            _is_finite_number(declared_budget)
            and float(declared_budget) >= 0.0
        )
        if not budget_valid:
            errors.append(
                "declared_display_error_budget must be a finite "
                "non-negative number"
            )
        elif legacy_rough_error != declared_budget:
            errors.append(
                "rough_error_bound legacy alias does not match "
                "declared_display_error_budget"
            )
        if budget_valid and actual_rounding_error is not None:
            rounding_within_budget = (
                actual_rounding_error <= float(declared_budget)
            )
            if not rounding_within_budget:
                errors.append(
                    "display rounding error exceeds declared display budget"
                )
            if recomputed_margin is not None:
                budget_within_boundary = (
                    float(declared_budget) < recomputed_margin
                )
                declared_budget_margin_verified = (
                    display_rounding_verified is True
                    and rounding_within_budget
                    and budget_within_boundary
                )
                if not budget_within_boundary:
                    errors.append(
                        "declared display budget reaches or crosses an "
                        "option boundary"
                    )
    else:
        errors.append("formula did not return a finite result")

    preliminary = _verification_result(
        errors,
        scope,
        formula_truth_verified=formula_truth_verified,
        unique_correct_option_verified=unique_correct_option_verified,
        display_rounding_verified=display_rounding_verified,
        declared_budget_margin_verified=declared_budget_margin_verified,
        actual_display_rounding_error=actual_rounding_error,
        declared_display_error_budget=declared_budget,
        recomputed_option_boundary_margin=recomputed_margin,
    )
    if isinstance(stored_verification, dict):
        expected_summary = _verification_summary(preliminary)
        for field, expected in expected_summary.items():
            if stored_verification.get(field) != expected:
                errors.append(
                    f"stored verification {field} does not match independent "
                    "recalculation"
                )
    return _verification_result(
        errors,
        scope,
        formula_truth_verified=formula_truth_verified,
        unique_correct_option_verified=unique_correct_option_verified,
        display_rounding_verified=display_rounding_verified,
        declared_budget_margin_verified=declared_budget_margin_verified,
        actual_display_rounding_error=actual_rounding_error,
        declared_display_error_budget=declared_budget,
        recomputed_option_boundary_margin=recomputed_margin,
    )


def _sample_data(
    node_id: str,
    rng: random.Random,
    difficulty: str,
) -> tuple[dict[str, Any], str]:
    if node_id == "abrx.base":
        base = rng.randrange(50, 501) * 10
        rate = rng.choice((0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25))
        current = base * (1.0 + rate)
        return {"current": current, "rate": rate}, (
            f"某指标现期为 {current:g} 万，同比增长 {rate:.0%}。基期约为多少万？"
        )
    if node_id == "abrx.growth-amount":
        base = rng.randrange(100, 401) * 500
        rate = rng.choice((0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15))
        current = base * (1.0 + rate)
        return {"current": current, "rate": rate}, (
            f"某指标现期为 {current:g}，同比增长 {rate:.0%}。增长量约为多少？"
        )
    if node_id == "growth.interval":
        first = rng.randrange(-100, 201) / 1000.0
        second = rng.randrange(-80, 181) / 1000.0
        return {"first_rate": first, "second_rate": second}, (
            f"某指标连续两年增速分别为 {first:.1%}、{second:.1%}，"
            "两年累计增速约为多少？"
        )
    if node_id == "growth.ratio":
        numerator = rng.randrange(-80, 221) / 1000.0
        denominator = rng.randrange(-60, 181) / 1000.0
        return {
            "numerator_rate": numerator,
            "denominator_rate": denominator,
        }, (
            f"某比值的分子增长 {numerator:.1%}、分母增长 "
            f"{denominator:.1%}，该比值增速约为多少？"
        )
    if node_id == "share.current":
        whole = rng.randrange(20, 201) * 100
        share = rng.choice((0.12, 0.16, 0.20, 0.25, 0.30, 0.40, 0.50))
        part = whole * share
        return {"part": part, "whole": whole}, (
            f"总体为 {whole:g}，其中部分为 {part:g}，部分占总体约多少？"
        )
    if node_id == "share.base":
        base_whole = rng.randrange(20, 101) * 100
        share = rng.choice((0.12, 0.18, 0.20, 0.25, 0.32, 0.40))
        part_rate = rng.choice((-0.03, 0.04, 0.08, 0.12, 0.16))
        whole_rate = rng.choice((-0.02, 0.03, 0.06, 0.10, 0.14))
        base_part = base_whole * share
        whole = base_whole * (1.0 + whole_rate)
        part = base_part * (1.0 + part_rate)
        return {
            "part": part,
            "whole": whole,
            "part_rate": part_rate,
            "whole_rate": whole_rate,
        }, (
            f"现期部分为 {part:g}、总体为 {whole:g}，增速分别为 {part_rate:.0%}、{whole_rate:.0%}。基期比重约为多少？"
        )
    if node_id == "share.trend":
        whole_rate = rng.randrange(-80, 181) / 1000.0
        relation = rng.choice((-1, 1))
        base_signal_gap = rng.randrange(10, 81) / 1000.0
        part_rate = (
            whole_rate
            + relation
            * base_signal_gap
            * RATE_SIGNAL_GAP_MULTIPLIERS[difficulty]
        )
        return {"part_rate": part_rate, "whole_rate": whole_rate}, (
            f"部分增长 {part_rate:.2%}，总体增长 {whole_rate:.2%}，"
            "现期比重较基期如何变化？"
        )
    if node_id == "average.single":
        count = rng.randrange(4, 41)
        average = rng.randrange(20, 501)
        total = count * average
        return {"total": total, "count": count}, (
            f"总量为 {total}，共有 {count} 份，平均每份约为多少？"
        )
    if node_id == "average.annual-increase":
        years = rng.randrange(3, 9)
        start = rng.randrange(20, 201) * 100
        increase = rng.randrange(10, 101) * 10
        end = start + years * increase
        return {"start": start, "end": end, "years": years}, (
            f"某指标从 {start} 增至 {end}，跨 {years} 个年度间隔，年均增加约多少？"
        )
    if node_id == "special.contribution":
        whole_delta = rng.randrange(20, 201) * 100
        share = rng.choice((0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60))
        part_delta = whole_delta * share
        return {"part_delta": part_delta, "whole_delta": whole_delta}, (
            f"总体增量为 {whole_delta:g}，其中某部分增量为 {part_delta:g}，其增量贡献率约为多少？"
        )
    raise ValueError(f"unknown knowledge id: {node_id}")


def _build_question(
    template: dict[str, Any],
    node_id: str,
    question_seed: int,
    difficulty: str,
) -> dict[str, Any]:
    normalized_difficulty = _validate_difficulty(difficulty)
    rng = random.Random(question_seed)
    data, _ = _sample_data(node_id, rng, normalized_difficulty)
    data = _canonicalize_prompt_data(node_id, data)
    prompt = _render_prompt(node_id, data)
    difficulty_policy = _difficulty_policy(template["kind"])
    difficulty_multiplier = difficulty_policy["multipliers"][
        normalized_difficulty
    ]
    effective_option_gap: float | None = None
    if template["kind"] == "qualitative":
        exact: float | str = _recompute(template["formula_id"], data)
        options: list[Any] = ["上升", "下降", "不变", "无法判断"]
        rng.shuffle(options)
        correct_value = exact
        margin = None
    else:
        precision = int(template["precision"])
        decimal_exact = _generation_decimal_result(node_id, data)
        exact = float(decimal_exact)
        correct_value = _round_half_up(decimal_exact, precision)
        effective_option_gap = (
            float(template["option_gap"]) * difficulty_multiplier
        )
        decimal_gap = Decimal(str(effective_option_gap))
        numeric_rank = rng.randrange(4)
        options = [
            _round_half_up(
                Decimal(str(correct_value))
                + Decimal(rank - numeric_rank) * decimal_gap,
                precision,
            )
            for rank in range(4)
        ]
        if len(set(options)) != 4:
            raise ValueError("generated numeric options are not unique")
        rng.shuffle(options)
        correct_index = options.index(correct_value)
        margin = _numeric_margin(float(exact), options, correct_index)
    correct_index = options.index(correct_value)
    declared_budget = template.get("declared_display_error_budget")
    question = {
        "knowledge_id": node_id,
        "semantic_id": _semantic_id(node_id, data),
        "template_title": template["title"],
        "prompt": prompt,
        "data": data,
        "options": options,
        "option_labels": LETTERS,
        "correct_index": correct_index,
        "correct_label": LETTERS[correct_index],
        "correct_value": correct_value,
        "exact_value": exact,
        "quick_hint": template["quick_hint"],
        "formula_id": template["formula_id"],
        "seed": question_seed,
        "unit": template.get("unit"),
        "precision": template.get("precision"),
        "declared_display_error_budget": declared_budget,
        "option_boundary_margin": margin,
        "quick_hint_safety_verified": False,
        "verification_scope": _verification_scope(template.get("kind")),
        "difficulty": normalized_difficulty,
        "difficulty_basis": difficulty_policy["basis"],
        "difficulty_multiplier": difficulty_multiplier,
        "effective_option_gap": effective_option_gap,
        "rough_error_bound_semantics": ROUGH_ERROR_BOUND_SEMANTICS,
        "option_margin_semantics": OPTION_MARGIN_SEMANTICS,
        # Compatibility aliases; neither one measures quick_hint error.
        "rough_error_bound": template.get("rough_error_bound"),
        "option_margin": margin,
    }
    return question


def generate_set(
    knowledge_id: str,
    count: int = 5,
    seed: int = 0,
    difficulty: str = "medium",
) -> list[dict[str, Any]]:
    """Generate a deterministic set, resampling until every item verifies."""

    templates = _load_templates()
    if knowledge_id not in templates:
        raise ValueError(f"unknown knowledge id: {knowledge_id}")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise ValueError("count must be an integer from 1 to 100")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    normalized_difficulty = _validate_difficulty(difficulty)

    master = random.Random(f"gongkao-practice-v1:{knowledge_id}:{seed}")
    questions: list[dict[str, Any]] = []
    seen_semantic_ids: set[str] = set()
    for _ in range(count):
        for _attempt in range(100):
            question_seed = master.getrandbits(63)
            try:
                question = _build_question(
                    templates[knowledge_id],
                    knowledge_id,
                    question_seed,
                    normalized_difficulty,
                )
            except (ArithmeticError, TypeError, ValueError):
                continue
            if question["semantic_id"] in seen_semantic_ids:
                continue
            verification = verify_question(question)
            if verification["ok"]:
                question["verification"] = _verification_summary(verification)
                questions.append(question)
                seen_semantic_ids.add(question["semantic_id"])
                break
        else:
            raise RuntimeError(
                "could not generate a deterministically verified question "
                f"for {knowledge_id}"
            )
    return questions
