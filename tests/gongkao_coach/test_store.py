"""Persistence contract tests for the data-analysis study coach."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".agents/skills/gongkao-data-analysis-coach/scripts"
sys.path.insert(0, str(SCRIPTS))

from coachlib.store import StudyStore  # noqa: E402


KNOWN_IDS = {"growth.ratio", "abrx.base"}


def wrong_attempt(**overrides):
    payload = {
        "question": "亩产值与价格增速判断亩产趋势",
        "user_answer": "B",
        "correct_answer": "C",
        "is_correct": False,
        "duration_seconds": 85,
        "confidence": 4,
        "knowledge_ids": ["growth.ratio"],
        "errors": ["source_conflict"],
        "method_used": "照抄讲义",
        "recommended_method": "比较分子分母增速",
        "method_quality": "low",
        "source_ref": "第六讲 PDF p2 例21",
        "exact_method": {"formula": "ratio_growth"},
        "fast_method": {"route": "qualitative"},
    }
    payload.update(overrides)
    return payload


class StudyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "study.sqlite3"
        self.store = StudyStore(self.db, KNOWN_IDS)

    def test_attempt_round_trip_and_initial_review(self):
        receipt = self.store.record_attempt(wrong_attempt())

        self.assertGreater(receipt["attempt_id"], 0)
        saved = self.store.get_attempt(receipt["attempt_id"])
        self.assertEqual(saved["correct_answer"], "C")
        self.assertEqual(saved["knowledge_ids"], ["growth.ratio"])
        self.assertEqual(saved["errors"], ["source_conflict"])
        self.assertEqual(saved["exact_method"], {"formula": "ratio_growth"})
        self.assertIn("created_at", saved)
        expected_due = (date.today() + timedelta(days=1)).isoformat()
        self.assertEqual(receipt["next_review_date"], expected_due)
        self.assertEqual(
            len(self.store.due_reviews(on_date=receipt["next_review_date"])), 1
        )

    def test_unknown_knowledge_id_rolls_back_without_residue(self):
        with self.assertRaisesRegex(ValueError, "unknown knowledge"):
            self.store.record_attempt(
                wrong_attempt(knowledge_ids=["growth.ratio", "not.real"])
            )

        self.assertEqual(self.store.count_attempts(), 0)
        self.assertEqual(self.store.due_reviews(on_date="9999-12-31"), [])

    def test_low_quality_correct_attempt_also_enters_review(self):
        receipt = self.store.record_attempt(
            wrong_attempt(
                user_answer="C",
                is_correct=True,
                errors=[],
                confidence=3,
                method_quality="low",
            )
        )
        self.assertIsNotNone(receipt["review_id"])
        self.assertEqual(receipt["next_review_date"], (date.today() + timedelta(days=1)).isoformat())

    def test_source_conflict_enters_mistakes_even_when_answer_is_correct(self):
        receipt = self.store.record_attempt(
            wrong_attempt(
                user_answer="C",
                is_correct=True,
                confidence=3,
                errors=["source_conflict"],
                method_quality="acceptable",
            )
        )

        self.assertIsNotNone(receipt["review_id"])
        self.assertEqual(
            [item["id"] for item in self.store.list_mistakes()],
            [receipt["attempt_id"]],
        )

    def test_learning_metadata_and_high_priority_round_trip(self):
        receipt = self.store.record_attempt(
            wrong_attempt(
                difficulty="hard",
                mode="solve",
                parse_confidence=0.92,
                error_path="把讲义标答当成真值",
                correction_rule="比值趋势只比较分子、分母增速",
                is_transfer=True,
                target_duration_seconds=60,
            )
        )

        saved = self.store.get_attempt(receipt["attempt_id"])
        self.assertEqual(saved["difficulty"], "hard")
        self.assertEqual(saved["mode"], "solve")
        self.assertEqual(saved["parse_confidence"], 0.92)
        self.assertTrue(saved["is_transfer"])
        self.assertEqual(saved["priority"], "high")
        self.assertEqual(saved["correction_rule"], "比值趋势只比较分子、分母增速")

    def test_review_history_is_appended_and_intervals_follow_result(self):
        receipt = self.store.record_attempt(wrong_attempt())
        attempt_id = receipt["attempt_id"]

        first = self.store.record_review(attempt_id, "good", reviewed_on=receipt["next_review_date"])
        self.assertEqual(first["interval_days"], 3)
        second = self.store.record_review(attempt_id, "good", reviewed_on=first["next_review_date"])
        self.assertEqual(second["interval_days"], 7)
        third = self.store.record_review(attempt_id, "wrong", reviewed_on=second["next_review_date"])
        self.assertEqual(third["interval_days"], 1)
        fourth = self.store.record_review(attempt_id, "hard", reviewed_on=third["next_review_date"])
        self.assertEqual(fourth["interval_days"], 3)

        history = self.store.review_history(attempt_id)
        self.assertEqual(len(history), 5)  # initial schedule + four preserved outcomes
        self.assertEqual([row["result"] for row in history[:-1]], ["good", "good", "wrong", "hard"])
        self.assertEqual(history[-1]["status"], "scheduled")
        self.assertEqual([row["interval_days"] for row in history], [1, 3, 7, 1, 3])
        self.assertEqual([row["round"] for row in history], [1, 2, 3, 4, 5])

    def test_progress_is_derived_from_attempts_and_two_of_two_is_insufficient(self):
        for answer in ("A", "A"):
            self.store.record_attempt(
                {
                    "question": "基期量练习",
                    "user_answer": answer,
                    "correct_answer": "A",
                    "is_correct": True,
                    "duration_seconds": 40,
                    "confidence": 3,
                    "knowledge_ids": ["abrx.base"],
                    "errors": [],
                    "method_used": "增长率化分数",
                    "recommended_method": "增长率化分数",
                    "method_quality": "optimal",
                    "source_ref": "第三讲",
                    # Contradictory aggregate fields must have no effect.
                    "total": 999,
                    "correct": 0,
                    "recent_accuracy": 0,
                }
            )

        result = self.store.progress("abrx.base")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["correct"], 2)
        self.assertEqual(result["recent_accuracy"], 1.0)
        self.assertEqual(result["mastery"], "样本不足")
        self.assertLess(result["wilson_90"]["low"], 0.6)
        self.assertEqual(result["wilson_90"]["high"], 1.0)
        self.assertEqual(result["median_duration_seconds"], 40.0)

    def test_progress_exposes_six_dimensions_and_transfer_can_be_passed(self):
        for index in range(15):
            receipt = self.store.record_attempt(
                {
                    "question": f"比值增速迁移题 {index}",
                    "user_answer": "A",
                    "correct_answer": "A",
                    "is_correct": True,
                    "duration_seconds": 45,
                    "target_duration_seconds": 60,
                    "confidence": 4,
                    "knowledge_ids": ["growth.ratio"],
                    "errors": [],
                    "method_used": "先判方向再验算",
                    "recommended_method": "先判方向再验算",
                    "method_quality": "low" if index < 2 else "optimal",
                    "source_ref": "专项练习",
                    "is_transfer": index >= 12,
                }
            )
            if index < 2:
                self.store.record_review(
                    receipt["attempt_id"],
                    "good",
                    reviewed_on=receipt["next_review_date"],
                )

        result = self.store.progress("growth.ratio")
        self.assertEqual(result["mastery"], "稳定掌握")
        self.assertEqual(result["method_efficiency"]["optimal"], 13 / 15)
        self.assertEqual(result["transfer"]["total"], 3)
        self.assertEqual(result["transfer"]["accuracy"], 1.0)
        self.assertEqual(result["retention"]["passed"], 2)
        self.assertIn("accuracy_delta", result["stability"])

        overall = self.store.progress()
        self.assertEqual(overall["assessed_nodes"], 1)
        self.assertEqual(overall["total_nodes"], 2)
        self.assertEqual(overall["assessed_node_ratio"], 0.5)
        self.assertEqual(len(overall["nodes"]), 2)
        self.assertEqual(overall["mastery"], "覆盖不足")
        self.assertEqual(overall["attempt_aggregate_mastery"], "稳定掌握")

    def test_early_review_does_not_count_as_spaced_retention(self):
        receipt = self.store.record_attempt(wrong_attempt())
        self.store.record_review(
            receipt["attempt_id"],
            "good",
            reviewed_on=date.today(),
        )

        result = self.store.progress("growth.ratio")
        self.assertEqual(result["retention"]["passed"], 0)
        self.assertEqual(result["retention"]["early_completed"], 1)
        self.assertEqual(result["review_passes"], 0)

    def test_duplicate_same_day_review_is_rejected_without_advancing_twice(self):
        receipt = self.store.record_attempt(wrong_attempt())
        first = self.store.record_review(
            receipt["attempt_id"],
            "good",
            reviewed_on=receipt["next_review_date"],
        )

        with self.assertRaisesRegex(ValueError, "after the previous review"):
            self.store.record_review(
                receipt["attempt_id"],
                "good",
                reviewed_on=receipt["next_review_date"],
            )

        history = self.store.review_history(receipt["attempt_id"])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["interval_days"], first["interval_days"])

    def test_first_review_cannot_predate_attempt_schedule_start(self):
        receipt = self.store.record_attempt(wrong_attempt())

        with self.assertRaisesRegex(ValueError, "before the attempt"):
            self.store.record_review(
                receipt["attempt_id"],
                "good",
                reviewed_on="2000-01-01",
            )

        history = self.store.review_history(receipt["attempt_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "scheduled")

    def test_non_finite_numeric_inputs_are_rejected(self):
        for field, value in (
            ("duration_seconds", math.inf),
            ("duration_seconds", math.nan),
            ("parse_confidence", math.nan),
            ("target_duration_seconds", math.inf),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, "finite"):
                    self.store.record_attempt(wrong_attempt(**{field: value}))

    def test_future_schema_version_is_rejected_not_downgraded(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE schema_meta SET value='999' WHERE key='version'"
            )

        with self.assertRaisesRegex(ValueError, "newer schema version"):
            StudyStore(self.db, KNOWN_IDS)
        with sqlite3.connect(self.db) as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()[0]
        self.assertEqual(version, "999")

    def test_weighted_overall_progress_respects_node_weights(self):
        weighted_db = Path(self.temp.name) / "weighted.sqlite3"
        weighted = StudyStore(
            weighted_db,
            {"growth.ratio": 3.0, "abrx.base": 1.0},
        )
        weighted.record_attempt(
            wrong_attempt(
                user_answer="C",
                is_correct=True,
                errors=[],
                method_quality="optimal",
            )
        )

        overall = weighted.progress()
        self.assertEqual(overall["weighted_mastery_score"], 0.1875)

    def test_answer_only_attempt_is_marked_low_traceability(self):
        receipt = self.store.record_attempt(
            {
                "user_answer": "A",
                "correct_answer": "B",
                "is_correct": False,
                "knowledge_ids": ["abrx.base"],
                "errors": ["missing_question"],
                "method_quality": "acceptable",
            }
        )

        saved = self.store.get_attempt(receipt["attempt_id"])
        self.assertEqual(saved["traceability"], "low")
        self.assertIn("匿名作答", saved["question"])

    def test_export_is_atomic_json_and_contains_history(self):
        receipt = self.store.record_attempt(wrong_attempt())
        output = Path(self.temp.name) / "exports" / "study.json"

        result = self.store.export_json(output)

        self.assertEqual(result["attempt_count"], 1)
        parsed = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(parsed["attempts"][0]["id"], receipt["attempt_id"])
        self.assertEqual(len(parsed["reviews"]), 1)
        self.assertEqual(list(output.parent.glob("*.tmp")), [])

    def test_export_uses_one_consistent_database_snapshot(self):
        self.store.record_attempt(wrong_attempt(question="first"))
        output = Path(self.temp.name) / "snapshot.json"
        writer = StudyStore(self.db, KNOWN_IDS)
        original = StudyStore._attempt_from_row
        inserted = False

        def insert_during_export(connection, row):
            nonlocal inserted
            if not inserted:
                inserted = True
                writer.record_attempt(wrong_attempt(question="second"))
            return original(connection, row)

        StudyStore._attempt_from_row = staticmethod(insert_during_export)
        try:
            self.store.export_json(output)
        finally:
            StudyStore._attempt_from_row = staticmethod(original)

        parsed = json.loads(output.read_text(encoding="utf-8"))
        exported_attempt_ids = {item["id"] for item in parsed["attempts"]}
        self.assertEqual(len(exported_attempt_ids), 1)
        self.assertTrue(
            all(
                review["attempt_id"] in exported_attempt_ids
                for review in parsed["reviews"]
            )
        )


if __name__ == "__main__":
    unittest.main()
