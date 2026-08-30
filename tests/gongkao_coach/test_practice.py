"""Deterministic, independently verified practice generation tests."""

from __future__ import annotations

import copy
import json
import math
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
            "rough_error_bound",
            "option_margin",
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
                    verification = verify_question(question)
                    self.assertTrue(verification["ok"], verification)

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
                self.assertTrue(
                    any("exact_value" in error for error in result["errors"]),
                    result,
                )

    def test_numeric_questions_have_strict_error_margin(self) -> None:
        for node in NUMERIC_NODES:
            with self.subTest(node=node):
                for question in generate_set(node, count=8, seed=7):
                    self.assertGreater(
                        question["option_margin"],
                        question["rough_error_bound"],
                    )
                    verification = verify_question(question)
                    self.assertTrue(verification["margin_safe"], verification)
                    self.assertAlmostEqual(
                        verification["recomputed_option_margin"],
                        question["option_margin"],
                    )

    def test_qualitative_question_has_one_string_answer(self) -> None:
        question = generate_set("share.trend", count=1, seed=13)[0]

        self.assertTrue(all(isinstance(item, str) for item in question["options"]))
        self.assertEqual(len(set(question["options"])), 4)
        self.assertEqual(question["option_margin"], None)
        self.assertEqual(question["rough_error_bound"], 0.0)

    def test_unknown_node_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown knowledge"):
            generate_set("not.real", count=1, seed=1)

    def test_invalid_count_is_rejected(self) -> None:
        for count in (0, -1, 101, 1.5, True, "2"):
            with self.subTest(count=count):
                with self.assertRaisesRegex(ValueError, "count"):
                    generate_set("abrx.base", count=count, seed=1)

    def test_verifier_rejects_duplicate_correct_option_and_bad_margin(self) -> None:
        original = generate_set("share.current", count=1, seed=23)[0]
        duplicate = copy.deepcopy(original)
        duplicate["options"][0] = duplicate["correct_value"]
        duplicate["options"][1] = duplicate["correct_value"]
        duplicate["correct_index"] = 0
        duplicate["correct_label"] = "A"
        self.assertFalse(verify_question(duplicate)["ok"])

        bad_margin = copy.deepcopy(original)
        bad_margin["rough_error_bound"] = bad_margin["option_margin"]
        result = verify_question(bad_margin)
        self.assertFalse(result["ok"])
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
        with tempfile.TemporaryDirectory() as directory:
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
        self.assertEqual(len(payload["questions"]), 3)
        self.assertEqual(
            payload["questions"],
            generate_set("growth.ratio", count=3, seed=73),
        )
        self.assertFalse(database_created)


if __name__ == "__main__":
    unittest.main()
