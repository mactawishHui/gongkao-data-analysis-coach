"""Deterministic, independently verified practice generation tests."""

from __future__ import annotations

import copy
import inspect
import json
import math
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / ".agents/skills/gongkao-data-analysis-coach"
SCRIPTS_DIR = SKILL_DIR / "scripts"
CLI = SCRIPTS_DIR / "coach.py"
TEMPLATES = SKILL_DIR / "references/practice-templates.json"
sys.path.insert(0, str(SCRIPTS_DIR))

from coachlib.formulas import (  # noqa: E402
    base_period,
    base_share,
    contribution_rate,
    current_share,
    growth_amount,
    interval_growth,
    ratio_growth,
)
from coachlib.practice import generate_set, verify_question  # noqa: E402
from coachlib import practice as practice_module  # noqa: E402


NUMERIC_NODES = (
    "abrx.base",
    "abrx.growth-amount",
    "growth.interval",
    "growth.ratio",
    "share.current",
    "share.base",
    "average.single",
    "average.annual-increase",
    "special.contribution",
)
QUALITATIVE_NODES = ("share.trend",)
SUPPORTED_NODES = NUMERIC_NODES + QUALITATIVE_NODES


def independently_recompute(question):
    data = question["data"]
    formula_id = question["formula_id"]
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
    raise AssertionError(f"unexpected formula: {formula_id}")


class PracticeGenerationTests(unittest.TestCase):
    def test_templates_cover_all_first_release_nodes(self) -> None:
        document = json.loads(TEMPLATES.read_text(encoding="utf-8"))
        ids = {item["knowledge_id"] for item in document["templates"]}

        self.assertEqual(ids, set(SUPPORTED_NODES))

    def test_growth_amount_quick_hint_uses_current_period_symbol(self) -> None:
        document = json.loads(TEMPLATES.read_text(encoding="utf-8"))
        template = next(
            item
            for item in document["templates"]
            if item["knowledge_id"] == "abrx.growth-amount"
        )

        self.assertIn("X=B×r/(1+r)", template["quick_hint"])
        self.assertNotIn("X=A×r/(1+r)", template["quick_hint"])

    def test_templates_call_the_fixed_budget_a_display_budget_not_hint_evidence(self) -> None:
        document = json.loads(TEMPLATES.read_text(encoding="utf-8"))

        for phrase in ("确定性公式真值", "唯一正确项", "展示舍入"):
            self.assertIn(phrase, document["description"])
        self.assertIn("声明的展示误差预算", document["description"])
        self.assertIn("不是 quick_hint", document["description"])
        for template in document["templates"]:
            with self.subTest(node=template["knowledge_id"]):
                self.assertIn("declared_display_error_budget", template)
                if template["kind"] == "numeric":
                    self.assertEqual(
                        template["rough_error_bound"],
                        template["declared_display_error_budget"],
                    )
                else:
                    self.assertIsNone(
                        template["declared_display_error_budget"]
                    )
                    self.assertIsNone(template["rough_error_bound"])
                self.assertIs(template["quick_hint_safety_verified"], False)

    def test_all_ten_nodes_generate_complete_verified_questions(self) -> None:
        required = {
            "prompt",
            "data",
            "options",
            "correct_index",
            "correct_label",
            "correct_value",
            "exact_value",
            "quick_hint",
            "formula_id",
            "seed",
            "declared_display_error_budget",
            "option_boundary_margin",
            "rough_error_bound",
            "option_margin",
            "rough_error_bound_semantics",
            "option_margin_semantics",
            "quick_hint_safety_verified",
            "verification_scope",
            "verification",
            "difficulty",
            "difficulty_basis",
            "difficulty_multiplier",
            "effective_option_gap",
        }

        for node in SUPPORTED_NODES:
            with self.subTest(node=node):
                questions = generate_set(node, count=4, seed=20260830)
                self.assertEqual(len(questions), 4)
                for question in questions:
                    self.assertTrue(required.issubset(question))
                    self.assertEqual(question["knowledge_id"], node)
                    self.assertEqual(len(question["options"]), 4)
                    self.assertIn(question["correct_label"], "ABCD")
                    self.assertEqual(
                        question["correct_label"],
                        "ABCD"[question["correct_index"]],
                    )
                    self.assertEqual(
                        question["options"].count(question["correct_value"]),
                        1,
                    )
                    self.assertFalse(question["quick_hint_safety_verified"])
                    self.assertIn(
                        "not a measured quick_hint error",
                        question["rough_error_bound_semantics"],
                    )
                    self.assertIn(
                        "option boundary",
                        question["option_margin_semantics"],
                    )
                    self.assertIn(
                        "deterministic_formula_truth",
                        question["verification_scope"]["verified"],
                    )
                    self.assertIn(
                        "unique_correct_option",
                        question["verification_scope"]["verified"],
                    )
                    self.assertIn(
                        "difficulty_configuration",
                        question["verification_scope"]["verified"],
                    )
                    self.assertIn(
                        "quick_hint_safety",
                        question["verification_scope"]["not_verified"],
                    )
                    summary = question["verification"]
                    self.assertTrue(summary["deterministic_formula_truth_verified"])
                    self.assertTrue(summary["unique_correct_option_verified"])
                    self.assertFalse(summary["quick_hint_safety_verified"])
                    verification = verify_question(question)
                    self.assertTrue(verification["ok"], verification)
                    self.assertEqual(
                        verification["verification_scope"],
                        question["verification_scope"],
                    )

    def test_generation_is_completely_reproducible(self) -> None:
        for node in SUPPORTED_NODES:
            with self.subTest(node=node):
                first = generate_set(node, count=5, seed=42)
                second = generate_set(node, count=5, seed=42)
                self.assertEqual(first, second)

    def test_numeric_correct_value_uses_all_four_numeric_ranks(self) -> None:
        for node in NUMERIC_NODES:
            with self.subTest(node=node):
                ranks = {
                    sorted(question["options"]).index(question["correct_value"])
                    for seed in range(80)
                    for question in generate_set(node, count=1, seed=seed)
                }
                self.assertEqual(ranks, {0, 1, 2, 3})

    def test_each_generated_set_has_unique_semantic_questions(self) -> None:
        for node in SUPPORTED_NODES:
            with self.subTest(node=node):
                questions = generate_set(node, count=100, seed=5)
                signatures = {
                    json.dumps(question["data"], sort_keys=True)
                    for question in questions
                }
                self.assertEqual(len(signatures), 100)

    def test_verifier_independently_recalculates_each_formula(self) -> None:
        for node in SUPPORTED_NODES:
            with self.subTest(node=node):
                question = generate_set(node, count=1, seed=19)[0]
                recomputed = independently_recompute(question)
                if isinstance(recomputed, str):
                    self.assertEqual(recomputed, question["exact_value"])
                else:
                    self.assertTrue(
                        math.isclose(
                            recomputed,
                            question["exact_value"],
                            rel_tol=1e-9,
                            abs_tol=1e-8,
                        )
                    )

                tampered = copy.deepcopy(question)
                if isinstance(recomputed, str):
                    tampered["exact_value"] = "无法判断"
                else:
                    tampered["exact_value"] += 1.0
                result = verify_question(tampered)
                self.assertFalse(result["ok"])
                self.assertFalse(
                    result["deterministic_formula_truth_verified"],
                    result,
                )
                self.assertTrue(
                    any("exact_value" in error for error in result["errors"]),
                    result,
                )

    def test_numeric_questions_verify_rounding_and_declared_budget_not_quick_hint(self) -> None:
        for node in NUMERIC_NODES:
            with self.subTest(node=node):
                for question in generate_set(node, count=8, seed=7):
                    self.assertIn("option_boundary_margin", question)
                    self.assertIn("declared_display_error_budget", question)
                    self.assertGreater(
                        question["option_boundary_margin"],
                        question["declared_display_error_budget"],
                    )
                    verification = verify_question(question)
                    self.assertTrue(
                        verification["display_rounding_verified"],
                        verification,
                    )
                    self.assertTrue(
                        verification["declared_budget_margin_verified"],
                        verification,
                    )
                    self.assertLessEqual(
                        verification["actual_display_rounding_error"],
                        question["declared_display_error_budget"],
                    )
                    self.assertFalse(
                        verification["quick_hint_safety_verified"],
                        verification,
                    )
                    self.assertAlmostEqual(
                        verification["recomputed_option_boundary_margin"],
                        question["option_boundary_margin"],
                    )
                    # Backward-compatible names describe the same display budget,
                    # never a measured error bound for the human quick hint.
                    self.assertEqual(
                        question["rough_error_bound"],
                        question["declared_display_error_budget"],
                    )
                    self.assertEqual(
                        question["option_margin"],
                        question["option_boundary_margin"],
                    )
                    self.assertEqual(
                        verification["margin_safe"],
                        verification["declared_budget_margin_verified"],
                    )

    def test_halfway_decimal_results_use_conventional_half_up_rounding(self) -> None:
        interval = generate_set("growth.interval", count=1, seed=2)[0]
        ratio = generate_set("growth.ratio", count=1, seed=433)[0]

        self.assertEqual(interval["prompt"], (
            "某指标连续两年增速分别为 -9.4%、2.5%，两年累计增速约为多少？"
        ))
        self.assertEqual(interval["correct_value"], -7.14)
        self.assertTrue(interval["verification"]["display_rounding_verified"])
        self.assertEqual(ratio["prompt"], (
            "某比值的分子增长 -4.1%、分母增长 12.0%，该比值增速约为多少？"
        ))
        self.assertEqual(ratio["correct_value"], -14.38)
        self.assertTrue(ratio["verification"]["display_rounding_verified"])

    def test_verifier_rejects_binary_float_halfway_rounding_answer(self) -> None:
        question = generate_set("growth.interval", count=1, seed=2)[0]
        question.pop("verification")
        wrong_value = -7.13
        offset = wrong_value - question["correct_value"]
        question["options"] = [
            round(float(value) + offset, 2)
            for value in question["options"]
        ]
        question["correct_value"] = wrong_value
        question["option_boundary_margin"] = practice_module._numeric_margin(
            float(question["exact_value"]),
            question["options"],
            question["correct_index"],
        )
        question["option_margin"] = question["option_boundary_margin"]

        result = verify_question(question)

        self.assertFalse(result["ok"], result)
        self.assertFalse(result["display_rounding_verified"], result)
        self.assertTrue(
            any("precision" in error or "rounding" in error for error in result["errors"]),
            result,
        )

    def test_verifier_rejects_any_stored_claim_that_quick_hint_is_verified(self) -> None:
        original = generate_set("growth.ratio", count=1, seed=67)[0]
        self.assertIn("verification", original)
        self.assertIn("verification_scope", original)
        cases = []

        top_level = copy.deepcopy(original)
        top_level["quick_hint_safety_verified"] = True
        cases.append(("top level", top_level))

        nested = copy.deepcopy(original)
        nested["verification"]["quick_hint_safety_verified"] = True
        cases.append(("verification summary", nested))

        scope = copy.deepcopy(original)
        scope["verification_scope"]["verified"].append("quick_hint_safety")
        cases.append(("verification scope", scope))

        for label, question in cases:
            with self.subTest(label=label):
                result = verify_question(question)
                self.assertFalse(result["ok"], result)
                self.assertFalse(result["quick_hint_safety_verified"])
                self.assertTrue(
                    any("quick_hint" in error for error in result["errors"]),
                    result,
                )

    def test_qualitative_question_has_one_string_answer(self) -> None:
        question = generate_set("share.trend", count=1, seed=13)[0]

        self.assertTrue(all(isinstance(item, str) for item in question["options"]))
        self.assertEqual(len(set(question["options"])), 4)
        self.assertEqual(question["option_boundary_margin"], None)
        self.assertEqual(question["option_margin"], None)
        self.assertEqual(question["declared_display_error_budget"], None)
        self.assertEqual(question["rough_error_bound"], None)
        self.assertIsNone(question["verification"]["display_rounding_verified"])
        self.assertIsNone(
            question["verification"]["declared_budget_margin_verified"]
        )
        self.assertFalse(question["quick_hint_safety_verified"])

    def test_unknown_node_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown knowledge"):
            generate_set("not.real", count=1, seed=1)

    def test_invalid_count_is_rejected(self) -> None:
        for count in (0, -1, 101, 1.5, True, "2"):
            with self.subTest(count=count):
                with self.assertRaisesRegex(ValueError, "count"):
                    generate_set("abrx.base", count=count, seed=1)

    def test_difficulty_is_reproducible_and_controls_numeric_option_density(self) -> None:
        self.assertIn("difficulty", inspect.signature(generate_set).parameters)
        generated = {}
        for difficulty in ("easy", "medium", "hard"):
            with self.subTest(difficulty=difficulty):
                first = generate_set(
                    "growth.ratio",
                    count=3,
                    seed=88,
                    difficulty=difficulty,
                )
                second = generate_set(
                    "growth.ratio",
                    count=3,
                    seed=88,
                    difficulty=difficulty,
                )
                self.assertEqual(first, second)
                self.assertTrue(
                    all(item["difficulty"] == difficulty for item in first)
                )
                self.assertTrue(
                    all(verify_question(item)["ok"] for item in first)
                )
                self.assertTrue(
                    all(
                        item["quick_hint_safety_verified"] is False
                        for item in first
                    )
                )
                generated[difficulty] = first[0]

        self.assertEqual(
            generated["easy"]["data"],
            generated["medium"]["data"],
        )
        self.assertEqual(
            generated["medium"]["data"],
            generated["hard"]["data"],
        )
        gaps = {
            difficulty: question["effective_option_gap"]
            for difficulty, question in generated.items()
        }
        self.assertGreater(gaps["easy"], gaps["medium"])
        self.assertGreater(gaps["medium"], gaps["hard"])
        for difficulty, question in generated.items():
            ordered = sorted(question["options"])
            observed = [
                ordered[index + 1] - ordered[index]
                for index in range(3)
            ]
            self.assertTrue(
                all(
                    math.isclose(value, gaps[difficulty], abs_tol=1e-12)
                    for value in observed
                )
            )

    def test_every_qualitative_question_has_a_difficulty_controlled_gap(self) -> None:
        self.assertIn("difficulty", inspect.signature(generate_set).parameters)
        for seed in range(200):
            generated = {
                difficulty: generate_set(
                    "share.trend",
                    count=1,
                    seed=seed,
                    difficulty=difficulty,
                )[0]
                for difficulty in ("easy", "medium", "hard")
            }
            signal_gaps = {
                difficulty: abs(
                    question["data"]["part_rate"]
                    - question["data"]["whole_rate"]
                )
                for difficulty, question in generated.items()
            }
            self.assertGreater(
                signal_gaps["easy"],
                signal_gaps["medium"],
                seed,
            )
            self.assertGreater(
                signal_gaps["medium"],
                signal_gaps["hard"],
                seed,
            )
            self.assertEqual(
                {
                    question["exact_value"]
                    for question in generated.values()
                },
                {generated["medium"]["exact_value"]},
            )
            self.assertIn(
                generated["medium"]["exact_value"],
                {"上升", "下降"},
            )
            self.assertTrue(
                all(
                    verify_question(question)["ok"]
                    for question in generated.values()
                )
            )

    def test_verifier_rejects_zero_gap_qualitative_questions(self) -> None:
        question = generate_set(
            "share.trend",
            count=1,
            seed=12,
            difficulty="medium",
        )[0]
        question["data"]["part_rate"] = question["data"]["whole_rate"]
        question["prompt"] = practice_module._render_prompt(
            "share.trend",
            question["data"],
        )
        question["semantic_id"] = practice_module._semantic_id(
            "share.trend",
            question["data"],
        )
        question["exact_value"] = "不变"
        question["correct_value"] = "不变"
        question["correct_index"] = question["options"].index("不变")
        question["correct_label"] = "ABCD"[question["correct_index"]]

        result = verify_question(question)

        self.assertFalse(result["ok"])
        self.assertTrue(
            any("nonzero rate gap" in error for error in result["errors"]),
            result,
        )

    def test_verifier_binds_qualitative_gap_to_seed_and_difficulty(self) -> None:
        question = generate_set(
            "share.trend",
            count=1,
            seed=1,
            difficulty="hard",
        )[0]
        question.pop("verification")
        question["data"] = {"part_rate": 0.90, "whole_rate": 0.0}
        question["prompt"] = practice_module._render_prompt(
            "share.trend",
            question["data"],
        )
        question["semantic_id"] = practice_module._semantic_id(
            "share.trend",
            question["data"],
        )
        question["exact_value"] = "上升"
        question["correct_value"] = "上升"
        question["correct_index"] = question["options"].index("上升")
        question["correct_label"] = "ABCD"[question["correct_index"]]

        result = verify_question(question)

        self.assertFalse(result["ok"], result)
        self.assertTrue(
            any("seed and difficulty" in error for error in result["errors"]),
            result,
        )

    def test_qualitative_prompt_never_rounds_distinct_rates_to_the_same_display(self) -> None:
        for difficulty in ("easy", "medium", "hard"):
            for seed in range(200):
                data, sampled_prompt = practice_module._sample_data(
                    "share.trend",
                    random.Random(seed),
                    difficulty,
                )
                rendered_prompt = practice_module._render_prompt(
                    "share.trend",
                    data,
                )
                self.assertEqual(sampled_prompt, rendered_prompt)
                part_display = f"{data['part_rate']:.2%}"
                whole_display = f"{data['whole_rate']:.2%}"
                self.assertIn(part_display, rendered_prompt)
                self.assertIn(whole_display, rendered_prompt)
                self.assertAlmostEqual(
                    float(part_display.removesuffix("%")) / 100.0,
                    data["part_rate"],
                )
                self.assertAlmostEqual(
                    float(whole_display.removesuffix("%")) / 100.0,
                    data["whole_rate"],
                )
                if data["part_rate"] != data["whole_rate"]:
                    self.assertNotEqual(
                        part_display,
                        whole_display,
                        (difficulty, seed, data, rendered_prompt),
                    )

    def test_numeric_rate_prompts_expose_the_exact_structured_rates(self) -> None:
        fields = {
            "growth.interval": ("first_rate", "second_rate"),
            "growth.ratio": ("numerator_rate", "denominator_rate"),
        }
        for node, rate_fields in fields.items():
            for difficulty in ("easy", "medium", "hard"):
                for seed in range(200):
                    data, sampled_prompt = practice_module._sample_data(
                        node,
                        random.Random(seed),
                        difficulty,
                    )
                    rendered_prompt = practice_module._render_prompt(node, data)
                    self.assertEqual(sampled_prompt, rendered_prompt)
                    for field in rate_fields:
                        displayed = f"{data[field]:.1%}"
                        self.assertIn(displayed, rendered_prompt)
                        parsed_rate = float(displayed.removesuffix("%")) / 100.0
                        self.assertTrue(
                            math.isclose(
                                parsed_rate,
                                data[field],
                                rel_tol=0.0,
                                abs_tol=1e-12,
                            ),
                            (
                                node,
                                difficulty,
                                seed,
                                field,
                                data,
                                rendered_prompt,
                            ),
                        )

    def test_verifier_rejects_hidden_numeric_rate_precision(self) -> None:
        cases = (
            ("growth.interval", "first_rate"),
            ("growth.ratio", "numerator_rate"),
        )
        for node, field in cases:
            with self.subTest(node=node):
                question = generate_set(
                    node,
                    count=1,
                    seed=55,
                    difficulty="hard",
                )[0]
                question["data"][field] += 0.0004
                question["prompt"] = practice_module._render_prompt(
                    node,
                    question["data"],
                )
                question["semantic_id"] = practice_module._semantic_id(
                    node,
                    question["data"],
                )

                result = verify_question(question)

                self.assertFalse(result["ok"])
                self.assertTrue(
                    any(
                        "prompt-visible precision" in error
                        for error in result["errors"]
                    ),
                    result,
                )

    def test_verifier_rejects_hidden_precision_in_g_formatted_values(self) -> None:
        question = generate_set("abrx.base", count=1, seed=31)[0]
        question.pop("verification")
        question["data"] = {
            "current": 1_000_000_499_999.0,
            "rate": 0.10,
        }
        question["prompt"] = practice_module._render_prompt(
            "abrx.base",
            question["data"],
        )
        question["semantic_id"] = practice_module._semantic_id(
            "abrx.base",
            question["data"],
        )
        visible_exact = 1_000_000_000_000.0 / 1.10
        correct_value = 909_090_909_091.0
        question["exact_value"] = visible_exact
        question["correct_value"] = correct_value
        question["options"] = [
            correct_value,
            correct_value + 12.0,
            correct_value + 24.0,
            correct_value + 36.0,
        ]
        question["correct_index"] = 0
        question["correct_label"] = "A"
        question["option_boundary_margin"] = practice_module._numeric_margin(
            visible_exact,
            question["options"],
            0,
        )
        question["option_margin"] = question["option_boundary_margin"]

        self.assertEqual(
            question["prompt"],
            "某指标现期为 1e+12 万，同比增长 10%。基期约为多少万？",
        )
        result = verify_question(question)

        self.assertFalse(result["ok"], result)
        self.assertTrue(
            any(
                "prompt-visible precision for current" in error
                for error in result["errors"]
            ),
            result,
        )

    def test_verifier_rejects_hidden_g_precision_even_within_sixteen_ulps(self) -> None:
        question = generate_set("abrx.base", count=1, seed=31)[0]
        question.pop("verification")
        question["data"] = {
            "current": 1_000_000_000_000_002.0,
            "rate": 0.10,
        }
        question["prompt"] = practice_module._render_prompt(
            "abrx.base",
            question["data"],
        )
        question["semantic_id"] = practice_module._semantic_id(
            "abrx.base",
            question["data"],
        )
        visible_exact = 1_000_000_000_000_000.0 / 1.10
        correct_value = 909_090_909_090_909.0
        question["exact_value"] = visible_exact
        question["correct_value"] = correct_value
        question["options"] = [
            correct_value,
            correct_value + 12.0,
            correct_value + 24.0,
            correct_value + 36.0,
        ]
        question["correct_index"] = 0
        question["correct_label"] = "A"
        question["option_boundary_margin"] = practice_module._numeric_margin(
            visible_exact,
            question["options"],
            0,
        )
        question["option_margin"] = question["option_boundary_margin"]

        self.assertEqual(
            question["prompt"],
            "某指标现期为 1e+15 万，同比增长 10%。基期约为多少万？",
        )
        result = verify_question(question)

        self.assertFalse(result["ok"], result)
        self.assertTrue(
            any(
                "prompt-visible precision for current" in error
                for error in result["errors"]
            ),
            result,
        )

    def test_verifier_rejects_large_exact_value_drift_hidden_by_relative_tolerance(self) -> None:
        question = generate_set("abrx.base", count=1, seed=31)[0]
        question.pop("verification")
        question["data"] = {
            "current": 1_000_000_000_000_000.0,
            "rate": 0.10,
        }
        question["prompt"] = practice_module._render_prompt(
            "abrx.base",
            question["data"],
        )
        question["semantic_id"] = practice_module._semantic_id(
            "abrx.base",
            question["data"],
        )
        decimal_truth = practice_module._independent_decimal_result(
            "abrx.base",
            question["data"],
        )
        float_truth = float(decimal_truth)
        correct_value = practice_module._round_half_up(
            decimal_truth,
            int(question["precision"]),
        )
        gap = question["effective_option_gap"]
        question["options"] = [
            correct_value,
            correct_value + gap,
            correct_value + 2 * gap,
            correct_value + 3 * gap,
        ]
        question["correct_value"] = correct_value
        question["correct_index"] = 0
        question["correct_label"] = "A"
        question["option_boundary_margin"] = practice_module._numeric_margin(
            float_truth,
            question["options"],
            0,
        )
        question["option_margin"] = question["option_boundary_margin"]
        question["exact_value"] = float_truth + 500_000.0

        result = verify_question(question)

        self.assertFalse(result["ok"], result)
        self.assertFalse(result["deterministic_formula_truth_verified"])
        self.assertTrue(
            any(
                "exact_value does not match" in error
                for error in result["errors"]
            ),
            result,
        )

    def test_invalid_difficulty_is_rejected_and_tampering_is_detected(self) -> None:
        self.assertIn("difficulty", inspect.signature(generate_set).parameters)
        for difficulty in ("expert", "", None, 1, True):
            with self.subTest(difficulty=difficulty):
                with self.assertRaisesRegex(ValueError, "difficulty"):
                    generate_set(
                        "abrx.base",
                        count=1,
                        seed=1,
                        difficulty=difficulty,
                    )

        question = generate_set(
            "abrx.base",
            count=1,
            seed=22,
            difficulty="hard",
        )[0]
        question["difficulty"] = "easy"
        result = verify_question(question)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("difficulty" in error for error in result["errors"]),
            result,
        )

    def test_verifier_binds_options_and_all_types_to_seed(self) -> None:
        template = practice_module._load_templates()["abrx.base"]
        question = practice_module._build_question(
            template,
            "abrx.base",
            6526425922124793017,
            "medium",
        )
        self.assertTrue(verify_question(question)["ok"])

        changed_seed = copy.deepcopy(question)
        changed_seed["seed"] = 8132
        result = verify_question(changed_seed)
        self.assertFalse(result["ok"], result)
        self.assertTrue(
            any("seed" in error or "options" in error for error in result["errors"]),
            result,
        )

        for field, replacement in (
            ("difficulty_multiplier", True),
            ("precision", False),
        ):
            with self.subTest(field=field):
                wrong_type = copy.deepcopy(question)
                wrong_type[field] = replacement
                result = verify_question(wrong_type)
                self.assertFalse(result["ok"], result)
                self.assertTrue(any(field in error for error in result["errors"]), result)

    def test_malformed_verification_scope_returns_structured_failure(self) -> None:
        question = generate_set("abrx.base", count=1, seed=22)[0]
        question["verification_scope"] = {"verified": None}

        result = verify_question(question)

        self.assertFalse(result["ok"])
        self.assertTrue(
            any("verification_scope" in error for error in result["errors"]),
            result,
        )

    def test_extreme_numeric_payloads_return_structured_failure(self) -> None:
        for current in (1e100, 10**1000):
            with self.subTest(current_type=type(current).__name__):
                question = generate_set("abrx.base", count=1, seed=22)[0]
                question.pop("verification")
                question["data"] = {"current": current, "rate": 0.1}

                result = verify_question(question)

                self.assertFalse(result["ok"])
                self.assertTrue(result["errors"], result)

    def test_practice_catalog_is_machine_readable_and_discloses_scope(self) -> None:
        self.assertTrue(hasattr(practice_module, "practice_catalog"))

        catalog = practice_module.practice_catalog()

        self.assertEqual(len(catalog), 10)
        self.assertEqual(
            {entry["knowledge_id"] for entry in catalog},
            set(SUPPORTED_NODES),
        )
        for entry in catalog:
            with self.subTest(node=entry["knowledge_id"]):
                self.assertTrue(entry["title"])
                self.assertIn("unit", entry)
                self.assertEqual(
                    entry["supported_difficulties"],
                    ["easy", "medium", "hard"],
                )
                self.assertIn(
                    entry["difficulty_policy"]["basis"],
                    {"option_spacing", "rate_signal_gap"},
                )
                self.assertEqual(
                    set(entry["difficulty_policy"]["multipliers"]),
                    {"easy", "medium", "hard"},
                )
                self.assertIn(
                    "quick_hint_safety",
                    entry["verification_scope"]["not_verified"],
                )
                self.assertIs(entry["quick_hint_safety_verified"], False)

        trend_template = next(
            template
            for template in json.loads(
                TEMPLATES.read_text(encoding="utf-8")
            )["templates"]
            if template["knowledge_id"] == "share.trend"
        )
        trend_catalog = next(
            entry
            for entry in catalog
            if entry["knowledge_id"] == "share.trend"
        )
        expected_scope = {
            "generated_correct_values": ["上升", "下降"],
            "distractor_only_values": ["不变", "无法判断"],
            "description": "只生成部分与整体增速不相等的明确升降题；不变仍作为干扰项。",
        }
        self.assertEqual(trend_template.get("generation_scope"), expected_scope)
        self.assertEqual(trend_catalog.get("generation_scope"), expected_scope)

    def test_verifier_rejects_duplicate_correct_option_and_bad_margin(self) -> None:
        original = generate_set("share.current", count=1, seed=23)[0]
        duplicate = copy.deepcopy(original)
        duplicate["options"][0] = duplicate["correct_value"]
        duplicate["options"][1] = duplicate["correct_value"]
        duplicate["correct_index"] = 0
        duplicate["correct_label"] = "A"
        self.assertFalse(verify_question(duplicate)["ok"])

        bad_margin = copy.deepcopy(original)
        bad_margin["declared_display_error_budget"] = bad_margin[
            "option_boundary_margin"
        ]
        bad_margin["rough_error_bound"] = bad_margin[
            "declared_display_error_budget"
        ]
        result = verify_question(bad_margin)
        self.assertFalse(result["ok"])
        self.assertFalse(result["declared_budget_margin_verified"])
        self.assertFalse(result["margin_safe"])

    def test_verifier_binds_question_to_knowledge_template_and_prompt(self) -> None:
        original = generate_set("abrx.base", count=1, seed=31)[0]
        wrong_formula = copy.deepcopy(original)
        wrong_formula["formula_id"] = "average_single"
        wrong_formula["data"] = {"total": 100, "count": 4}
        wrong_formula["exact_value"] = 25.0
        wrong_formula["options"] = [15.0, 25.0, 35.0, 45.0]
        wrong_formula["correct_index"] = 1
        wrong_formula["correct_label"] = "B"
        wrong_formula["correct_value"] = 25.0
        self.assertFalse(verify_question(wrong_formula)["ok"])

        wrong_prompt = copy.deepcopy(original)
        wrong_prompt["prompt"] = "这不是由结构化数据生成的题干"
        result = verify_question(wrong_prompt)
        self.assertFalse(result["ok"])
        self.assertTrue(any("prompt" in item for item in result["errors"]))

        qualitative = generate_set("share.trend", count=1, seed=9)[0]
        qualitative["options"][0] = 123
        result = verify_question(qualitative)
        self.assertFalse(result["ok"])

    def test_malformed_unhashable_options_return_structured_failure(self) -> None:
        question = generate_set("share.current", count=1, seed=4)[0]
        question["options"][0] = {"bad": "option"}

        result = verify_question(question)
        self.assertFalse(result["ok"])
        self.assertIsInstance(result["errors"], list)

    def test_verifier_rejects_invalid_exam_data_domains(self) -> None:
        cases = (
            ("average.single", {"total": -10, "count": -2}),
            (
                "average.annual-increase",
                {"start": 1000, "end": 600, "years": -4},
            ),
        )
        for node, bad_data in cases:
            with self.subTest(node=node):
                question = generate_set(node, count=1, seed=14)[0]
                question["data"] = bad_data
                question["prompt"] = practice_module._render_prompt(node, bad_data)
                question["semantic_id"] = practice_module._semantic_id(
                    node, bad_data
                )
                recomputed = independently_recompute(question)
                question["exact_value"] = recomputed
                precision = question["precision"]
                question["correct_value"] = round(recomputed, precision)
                question["options"] = [
                    question["correct_value"] - 30,
                    question["correct_value"],
                    question["correct_value"] + 30,
                    question["correct_value"] + 60,
                ]
                question["correct_index"] = 1
                question["correct_label"] = "B"
                question["option_boundary_margin"] = 15.0
                question["option_margin"] = 15.0

                result = verify_question(question)
                self.assertFalse(result["ok"])
                self.assertTrue(
                    any("data domain" in item for item in result["errors"]),
                    result,
                )

        base_share_question = generate_set("share.base", count=1, seed=21)[0]
        bad_data = {
            "part": 90.0,
            "whole": 100.0,
            "part_rate": 0.0,
            "whole_rate": 0.20,
        }
        base_share_question["data"] = bad_data
        base_share_question["prompt"] = practice_module._render_prompt(
            "share.base", bad_data
        )
        base_share_question["semantic_id"] = practice_module._semantic_id(
            "share.base", bad_data
        )
        exact = independently_recompute(base_share_question)
        base_share_question["exact_value"] = exact
        base_share_question["correct_value"] = round(exact, 0)
        base_share_question["options"] = [104.0, 108.0, 112.0, 116.0]
        base_share_question["correct_index"] = 1
        base_share_question["correct_label"] = "B"
        base_share_question["option_boundary_margin"] = 2.0
        base_share_question["option_margin"] = 2.0
        result = verify_question(base_share_question)
        self.assertFalse(result["ok"])
        self.assertTrue(any("base-period" in item for item in result["errors"]))

    def test_option_labels_are_fixed_to_abcd(self) -> None:
        question = generate_set("abrx.base", count=1, seed=44)[0]
        question["option_labels"] = "WXYZ"

        result = verify_question(question)
        self.assertFalse(result["ok"])
        self.assertTrue(any("option_labels" in item for item in result["errors"]))

    def test_degenerate_and_huge_options_return_structured_failure(self) -> None:
        original = generate_set("share.current", count=1, seed=18)[0]
        for options in (
            [10.0, 10.0, 10.0, 10.0],
            [10**10000, 20.0, 30.0, 40.0],
        ):
            with self.subTest(kind=type(options[0]).__name__):
                question = copy.deepcopy(original)
                question["options"] = options
                question["correct_index"] = 0
                question["correct_label"] = "A"
                question["correct_value"] = options[0]
                result = verify_question(question)
                self.assertFalse(result["ok"])
                self.assertIsInstance(result["errors"], list)

    def test_cli_generate_returns_one_json_object(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=ROOT,
            prefix=".coach-practice-test-",
        ) as directory:
            database = Path(directory) / "study.sqlite3"
            process = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--db",
                    str(database),
                    "generate",
                    "--knowledge-id",
                    "growth.ratio",
                    "--count",
                    "3",
                    "--seed",
                    "73",
                    "--difficulty",
                    "hard",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            database_created = database.exists()

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        payload = json.loads(process.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["difficulty"], "hard")
        self.assertEqual(len(payload["questions"]), 3)
        self.assertEqual(
            payload["questions"],
            generate_set(
                "growth.ratio",
                count=3,
                seed=73,
                difficulty="hard",
            ),
        )
        self.assertTrue(
            all(question["difficulty"] == "hard" for question in payload["questions"])
        )
        self.assertFalse(database_created)

    def test_cli_rejects_invalid_difficulty_as_json(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=ROOT,
            prefix=".coach-practice-test-",
        ) as directory:
            database = Path(directory) / "study.sqlite3"
            process = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--db",
                    str(database),
                    "generate",
                    "--knowledge-id",
                    "growth.ratio",
                    "--difficulty",
                    "expert",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            database_created = database.exists()

        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout, "")
        error = json.loads(process.stderr)
        self.assertFalse(error["ok"])
        self.assertIn("difficulty", error["error"])
        self.assertFalse(database_created)

    def test_cli_practice_catalog_lists_ten_templates_without_database(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=ROOT,
            prefix=".coach-practice-test-",
        ) as directory:
            database = Path(directory) / "study.sqlite3"
            process = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--db",
                    str(database),
                    "practice-catalog",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            database_created = database.exists()

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        payload = json.loads(process.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 10)
        self.assertEqual(len(payload["templates"]), 10)
        self.assertTrue(
            all(
                item["quick_hint_safety_verified"] is False
                for item in payload["templates"]
            )
        )
        self.assertFalse(database_created)


if __name__ == "__main__":
    unittest.main()
