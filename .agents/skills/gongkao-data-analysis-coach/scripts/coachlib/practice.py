"""Deterministic, self-verifying practice generation for first-release nodes."""

from __future__ import annotations

import json
import hashlib
import math
import random
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


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


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
            f"部分增长 {data['part_rate']:.1%}，总体增长 "
            f"{data['whole_rate']:.1%}，现期比重较基期如何变化？"
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


def verify_question(question: dict[str, Any]) -> dict[str, Any]:
    """Independently recalculate a generated question and its option safety."""

    errors: list[str] = []
    margin_safe = False
    recomputed_margin: float | None = None
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
        rough_error = question["rough_error_bound"]
        stored_margin = question["option_margin"]
    except (KeyError, TypeError) as error:
        return {
            "ok": False,
            "errors": [f"missing or malformed field: {error}"],
            "margin_safe": False,
            "recomputed_option_margin": None,
        }

    try:
        template = _load_templates()[knowledge_id]
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        errors.append(f"knowledge template cannot be resolved: {error}")
        template = None
    if template is not None:
        bindings = {
            "formula_id": formula_id,
            "template_title": question.get("template_title"),
            "quick_hint": question.get("quick_hint"),
            "unit": question.get("unit"),
            "precision": question.get("precision"),
            "rough_error_bound": rough_error,
        }
        expected_bindings = {
            "formula_id": template["formula_id"],
            "template_title": template["title"],
            "quick_hint": template["quick_hint"],
            "unit": template.get("unit"),
            "precision": template.get("precision"),
            "rough_error_bound": float(template["rough_error_bound"]),
        }
        for field, value in bindings.items():
            if value != expected_bindings[field]:
                errors.append(f"{field} does not match knowledge template")
        if not isinstance(data, dict) or set(data) != DATA_KEYS.get(
            knowledge_id, set()
        ):
            errors.append("data fields do not match knowledge template")
        else:
            errors.extend(_validate_data_domain(knowledge_id, data))
            try:
                expected_prompt = _render_prompt(knowledge_id, data)
            except (KeyError, TypeError, ValueError) as error:
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

    if not isinstance(options, list) or len(options) != 4:
        errors.append("options must contain exactly four values")
        return {
            "ok": False,
            "errors": errors,
            "margin_safe": False,
            "recomputed_option_margin": None,
        }
    if question.get("option_labels") != LETTERS:
        errors.append("option_labels must be ABCD")
    if any(
        options[left] == options[right]
        for left in range(4)
        for right in range(left + 1, 4)
    ):
        errors.append("options must be unique")
    if (
        isinstance(correct_index, bool)
        or not isinstance(correct_index, int)
        or not 0 <= correct_index < 4
    ):
        errors.append("correct_index must be from 0 to 3")
        return {
            "ok": False,
            "errors": errors,
            "margin_safe": False,
            "recomputed_option_margin": None,
        }
    if correct_label != LETTERS[correct_index]:
        errors.append("correct_label does not match correct_index")
    if options[correct_index] != correct_value:
        errors.append("correct_value does not match the indexed option")
    if options.count(correct_value) != 1:
        errors.append("correct_value must occur exactly once in options")

    try:
        recomputed = _recompute(formula_id, data)
    except (ArithmeticError, KeyError, TypeError, ValueError) as error:
        errors.append(f"formula recalculation failed: {error}")
        recomputed = None

    if isinstance(recomputed, str):
        if not all(isinstance(value, str) for value in options):
            errors.append("qualitative options must all be strings")
        elif set(options) != {"上升", "下降", "不变", "无法判断"}:
            errors.append("qualitative options do not match the template")
        if exact_value != recomputed:
            errors.append("exact_value does not match formula recalculation")
        if correct_value != recomputed:
            errors.append("correct_value does not match qualitative result")
        if stored_margin is not None:
            errors.append("qualitative option_margin must be null")
        if rough_error != 0.0:
            errors.append("qualitative rough_error_bound must be zero")
        margin_safe = not any("option" in error for error in errors)
    elif _is_finite_number(recomputed):
        if (
            not _is_finite_number(exact_value)
            or not math.isclose(
                float(exact_value),
                float(recomputed),
                rel_tol=1e-9,
                abs_tol=1e-8,
            )
        ):
            errors.append("exact_value does not match formula recalculation")
        if template is not None and template["kind"] == "numeric":
            expected_display = round(float(recomputed), int(template["precision"]))
            if correct_value != expected_display:
                errors.append("correct_value does not use template precision")
        if any(not _is_finite_number(value) for value in options):
            errors.append("numeric options must all be finite numbers")
        else:
            distances = [abs(float(value) - float(recomputed)) for value in options]
            minimum = min(distances)
            nearest = [
                index
                for index, distance in enumerate(distances)
                if math.isclose(distance, minimum, rel_tol=1e-12, abs_tol=1e-12)
            ]
            if nearest != [correct_index]:
                errors.append("formula result does not select one unique correct option")
            else:
                try:
                    recomputed_margin = _numeric_margin(
                        float(recomputed),
                        [float(value) for value in options],
                        correct_index,
                    )
                except (ArithmeticError, TypeError, ValueError) as error:
                    errors.append(f"option margin cannot be computed: {error}")
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
                            "option_margin does not match recomputed boundary"
                        )
                    if (
                        not _is_finite_number(rough_error)
                        or float(rough_error) < 0
                    ):
                        errors.append(
                            "rough_error_bound must be a finite non-negative number"
                        )
                    else:
                        margin_safe = recomputed_margin > float(rough_error)
                        if not margin_safe:
                            errors.append(
                                "rough_error_bound reaches or crosses an option boundary"
                            )
    else:
        errors.append("formula did not return a finite result")

    return {
        "ok": not errors,
        "errors": errors,
        "margin_safe": margin_safe,
        "recomputed_option_margin": recomputed_margin,
    }


def _sample_data(node_id: str, rng: random.Random) -> tuple[dict[str, Any], str]:
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
            f"某指标连续两年增速分别为 {first:.0%}、{second:.0%}，两年累计增速约为多少？"
        )
    if node_id == "growth.ratio":
        numerator = rng.randrange(-80, 221) / 1000.0
        denominator = rng.randrange(-60, 181) / 1000.0
        return {
            "numerator_rate": numerator,
            "denominator_rate": denominator,
        }, (
            f"某比值的分子增长 {numerator:.0%}、分母增长 {denominator:.0%}，该比值增速约为多少？"
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
        relation = rng.choice((-1, 0, 1))
        part_rate = whole_rate + relation * rng.randrange(10, 81) / 1000.0
        return {"part_rate": part_rate, "whole_rate": whole_rate}, (
            f"部分增长 {part_rate:.0%}，总体增长 {whole_rate:.0%}，现期比重较基期如何变化？"
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
) -> dict[str, Any]:
    rng = random.Random(question_seed)
    data, _ = _sample_data(node_id, rng)
    prompt = _render_prompt(node_id, data)
    exact = _recompute(template["formula_id"], data)
    if template["kind"] == "qualitative":
        options: list[Any] = ["上升", "下降", "不变", "无法判断"]
        rng.shuffle(options)
        correct_value = exact
        margin = None
    else:
        precision = int(template["precision"])
        correct_value = round(float(exact), precision)
        gap = float(template["option_gap"])
        numeric_rank = rng.randrange(4)
        options = [
            round(correct_value + (rank - numeric_rank) * gap, precision)
            for rank in range(4)
        ]
        if len(set(options)) != 4:
            raise ValueError("generated numeric options are not unique")
        rng.shuffle(options)
        correct_index = options.index(correct_value)
        margin = _numeric_margin(float(exact), options, correct_index)
    correct_index = options.index(correct_value)
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
        "rough_error_bound": float(template["rough_error_bound"]),
        "option_margin": margin,
    }
    return question


def generate_set(
    knowledge_id: str,
    count: int = 5,
    seed: int = 0,
) -> list[dict[str, Any]]:
    """Generate a deterministic set, resampling until every item verifies."""

    templates = _load_templates()
    if knowledge_id not in templates:
        raise ValueError(f"unknown knowledge id: {knowledge_id}")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise ValueError("count must be an integer from 1 to 100")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")

    master = random.Random(f"gongkao-practice-v1:{knowledge_id}:{seed}")
    questions: list[dict[str, Any]] = []
    seen_semantic_ids: set[str] = set()
    for _ in range(count):
        for _attempt in range(100):
            question_seed = master.getrandbits(63)
            try:
                question = _build_question(
                    templates[knowledge_id], knowledge_id, question_seed
                )
            except (ArithmeticError, TypeError, ValueError):
                continue
            if question["semantic_id"] in seen_semantic_ids:
                continue
            verification = verify_question(question)
            if verification["ok"]:
                questions.append(question)
                seen_semantic_ids.add(question["semantic_id"])
                break
        else:
            raise RuntimeError(
                f"could not generate a safe question for {knowledge_id}"
            )
    return questions
