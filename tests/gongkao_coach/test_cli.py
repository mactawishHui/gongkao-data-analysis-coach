"""Black-box JSON CLI tests."""

from __future__ import annotations

import json
import math
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / ".agents/skills/gongkao-data-analysis-coach/scripts/coach.py"


class CoachCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "study.sqlite3"

    def run_cli(self, *args, expect=0):
        process = subprocess.run(
            [sys.executable, str(CLI), "--db", str(self.db), *map(str, args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(process.returncode, expect, process.stderr)
        return process

    def parse_success(self, process):
        # Exactly one JSON object: no banners, tracebacks, or trailing log lines.
        payload = json.loads(process.stdout)
        self.assertIsInstance(payload, dict)
        self.assertTrue(payload["ok"])
        self.assertEqual(Path(payload["database"]), self.db.resolve())
        self.assertIn("object_id", payload)
        self.assertEqual(process.stderr, "")
        return payload

    def test_init_record_read_list_progress_review_and_export(self):
        self.parse_success(self.run_cli("init"))
        raw = json.dumps(
            {
                "question": "亩产值趋势",
                "user_answer": "B",
                "correct_answer": "C",
                "is_correct": False,
                "duration_seconds": 85,
                "confidence": 5,
                "knowledge_ids": ["growth.ratio"],
                "errors": ["source_conflict"],
                "method_used": "照抄讲义",
                "recommended_method": "比较分子分母增速",
                "method_quality": "low",
                "source_ref": "第六讲 PDF p2 例21",
            },
            ensure_ascii=False,
        )
        recorded = self.parse_success(self.run_cli("record", "--json", raw))
        attempt_id = recorded["attempt_id"]

        fetched = self.parse_success(self.run_cli("attempt", attempt_id))
        self.assertEqual(fetched["attempt"]["correct_answer"], "C")
        mistakes = self.parse_success(
            self.run_cli("mistakes", "--knowledge-id", "growth.ratio", "--limit", 10)
        )
        self.assertEqual(len(mistakes["attempts"]), 1)
        due = self.parse_success(self.run_cli("due", "--date", recorded["next_review_date"]))
        self.assertEqual(len(due["reviews"]), 1)
        reviewed = self.parse_success(
            self.run_cli("review", "--attempt-id", attempt_id, "--result", "good")
        )
        self.assertEqual(reviewed["interval_days"], 3)
        progress = self.parse_success(
            self.run_cli("progress", "--knowledge-id", "growth.ratio")
        )
        self.assertEqual(progress["progress"]["total"], 1)

        output = Path(self.temp.name) / "export.json"
        exported = self.parse_success(self.run_cli("export", "--output", output))
        self.assertTrue(output.exists())
        self.assertEqual(exported["attempt_count"], 1)
        json.loads(output.read_text(encoding="utf-8"))

    def test_unknown_id_is_nonzero_json_error_on_stderr_and_no_stdout(self):
        raw = json.dumps(
            {
                "question": "bad tag",
                "user_answer": "A",
                "correct_answer": "B",
                "is_correct": False,
                "knowledge_ids": ["not.real"],
                "errors": [],
                "method_quality": "acceptable",
            }
        )
        process = self.run_cli("record", "--json", raw, expect=1)
        self.assertEqual(process.stdout, "")
        error = json.loads(process.stderr)
        self.assertFalse(error["ok"])
        self.assertIn("unknown knowledge", error["error"])

    def test_non_standard_nan_json_is_rejected(self):
        raw = (
            '{"question":"bad number","user_answer":"A",'
            '"correct_answer":"B","is_correct":false,'
            '"duration_seconds":NaN,"knowledge_ids":["abrx.base"],'
            '"errors":[],"method_quality":"acceptable"}'
        )
        process = self.run_cli("record", "--json", raw, expect=1)
        self.assertEqual(process.stdout, "")
        error = json.loads(process.stderr)
        self.assertFalse(error["ok"])
        self.assertIn("non-standard numeric constant", error["error"])

    def test_legacy_infinity_yields_clean_json_error_without_partial_stdout(self):
        self.run_cli("init")
        raw = json.dumps(
            {
                "question": "legacy value",
                "user_answer": "A",
                "correct_answer": "A",
                "is_correct": True,
                "duration_seconds": 30,
                "knowledge_ids": ["abrx.base"],
                "errors": [],
                "method_quality": "optimal",
            }
        )
        self.run_cli("record", "--json", raw)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE attempts SET duration_seconds=?",
                (math.inf,),
            )

        process = self.run_cli(
            "progress", "--knowledge-id", "abrx.base", expect=1
        )
        self.assertEqual(process.stdout, "")
        error = json.loads(process.stderr)
        self.assertFalse(error["ok"])
        self.assertIn("Out of range", error["error"])

    def test_generate_interface_is_present_before_generator_lands(self):
        process = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--db",
                str(self.db),
                "generate",
                "--knowledge-id",
                "abrx.base",
                "--count",
                "1",
                "--seed",
                "7",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode == 0:
            result = self.parse_success(process)
            self.assertEqual(len(result["questions"]), 1)
        else:
            self.assertEqual(process.stdout, "")
            error = json.loads(process.stderr)
            self.assertFalse(error["ok"])
            self.assertIn("generator", error["error"].lower())

    def test_default_database_is_workspace_local_not_inside_skill(self):
        process = subprocess.run(
            [sys.executable, str(CLI), "init"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        payload = json.loads(process.stdout)
        expected = (ROOT / ".gongkao-study/study.sqlite3").resolve()
        self.assertEqual(Path(payload["database"]), expected)
        self.assertNotIn(".agents/skills", payload["database"])


if __name__ == "__main__":
    unittest.main()
