"""Black-box JSON CLI tests."""

from __future__ import annotations

import json
import math
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / ".agents/skills/gongkao-data-analysis-coach/scripts/coach.py"
SKILL = ROOT / ".agents/skills/gongkao-data-analysis-coach"


class CoachCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            dir=ROOT,
            prefix=".coach-cli-test-",
        )
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "study.sqlite3"

    def run_cli(self, *args, expect=0, input_text=None, database=None):
        selected_database = self.db if database is None else Path(database)
        process = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--db",
                str(selected_database),
                *map(str, args),
            ],
            cwd=ROOT,
            text=True,
            input=input_text,
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
        due = self.parse_success(self.run_cli("due"))
        self.assertEqual(due["reviews"], [])
        reviewed = self.parse_success(
            self.run_cli(
                "review",
                "--attempt-id",
                attempt_id,
                "--review-id",
                recorded["review_id"],
                "--result",
                "good",
            )
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

    def test_cli_rejects_database_paths_that_resolve_outside_workspace(self):
        outside = tempfile.TemporaryDirectory(
            dir=ROOT.parent,
            prefix=".coach-outside-",
        )
        self.addCleanup(outside.cleanup)
        outside_dir = Path(outside.name)
        link = Path(self.temp.name) / "outside-link"
        link.symlink_to(outside_dir, target_is_directory=True)
        traversal = (
            Path(self.temp.name)
            / ".."
            / ".."
            / outside_dir.name
            / "traversal.sqlite3"
        )
        cases = {
            "absolute": outside_dir / "absolute.sqlite3",
            "dotdot": traversal,
            "symlink": link / "symlink.sqlite3",
        }

        for label, database in cases.items():
            with self.subTest(label=label):
                process = self.run_cli(
                    "init",
                    database=database,
                    expect=1,
                )
                self.assertEqual(process.stdout, "")
                error = json.loads(process.stderr)
                self.assertIn("workspace", error["error"])

    def test_cli_rejects_export_paths_that_resolve_outside_workspace(self):
        self.parse_success(self.run_cli("init"))
        outside = tempfile.TemporaryDirectory(
            dir=ROOT.parent,
            prefix=".coach-export-outside-",
        )
        self.addCleanup(outside.cleanup)
        outside_dir = Path(outside.name)
        link = Path(self.temp.name) / "export-link"
        link.symlink_to(outside_dir, target_is_directory=True)
        traversal = (
            Path(self.temp.name)
            / ".."
            / ".."
            / outside_dir.name
            / "traversal.json"
        )
        cases = {
            "absolute": outside_dir / "absolute.json",
            "dotdot": traversal,
            "symlink": link / "symlink.json",
        }

        for label, output in cases.items():
            with self.subTest(label=label):
                process = self.run_cli(
                    "export",
                    "--output",
                    output,
                    expect=1,
                )
                self.assertEqual(process.stdout, "")
                error = json.loads(process.stderr)
                self.assertIn("workspace", error["error"])

    def test_cli_export_requires_force_to_replace_existing_destination(self):
        self.parse_success(self.run_cli("init"))
        output = Path(self.temp.name) / "existing.json"
        self.parse_success(self.run_cli("export", "--output", output))
        output.write_text("sentinel", encoding="utf-8")

        refused = self.run_cli(
            "export",
            "--output",
            output,
            expect=1,
        )
        self.assertEqual(refused.stdout, "")
        self.assertIn("exists", json.loads(refused.stderr)["error"])
        self.assertEqual(output.read_text(encoding="utf-8"), "sentinel")

        forced = self.parse_success(
            self.run_cli("export", "--output", output, "--force")
        )
        self.assertEqual(forced["output"], str(output.resolve()))
        self.assertIsInstance(json.loads(output.read_text(encoding="utf-8")), dict)

    def test_cli_export_never_overwrites_the_active_database(self):
        for force in (False, True):
            with self.subTest(force=force):
                database = Path(self.temp.name) / f"active-{force}.sqlite3"
                initialized = self.run_cli("init", database=database)
                self.assertEqual(initialized.returncode, 0, initialized.stderr)
                arguments = ["export", "--output", database]
                if force:
                    arguments.append("--force")

                process = self.run_cli(
                    *arguments,
                    database=database,
                    expect=1,
                )

                self.assertEqual(process.stdout, "")
                error = json.loads(process.stderr)
                self.assertIn("active database", error["error"])

    def test_cli_export_rejects_case_alias_of_active_database(self):
        database = Path(self.temp.name) / "Study.sqlite3"
        initialized = self.run_cli("init", database=database)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        alias = database.with_name("study.sqlite3")
        try:
            aliases_same_file = database.samefile(alias)
        except OSError:
            aliases_same_file = False
        if not aliases_same_file:
            self.skipTest("test filesystem is case-sensitive")
        before = database.read_bytes()

        process = self.run_cli(
            "export",
            "--output",
            alias,
            "--force",
            database=database,
            expect=1,
        )

        self.assertEqual(process.stdout, "")
        self.assertIn("active database", json.loads(process.stderr)["error"])
        self.assertEqual(database.read_bytes(), before)
        with sqlite3.connect(database) as connection:
            owner = connection.execute(
                "SELECT value FROM schema_meta WHERE key='owner'"
            ).fetchone()[0]
        self.assertEqual(owner, "gongkao-data-analysis-coach")

    def test_cli_export_never_overwrites_sqlite_sidecars(self):
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                database = Path(self.temp.name) / f"sidecar{suffix}.sqlite3"
                self.run_cli("init", database=database)
                sidecar = Path(f"{database}{suffix}")
                sidecar.write_text("sentinel", encoding="utf-8")

                process = self.run_cli(
                    "export",
                    "--output",
                    sidecar,
                    "--force",
                    database=database,
                    expect=1,
                )

                self.assertEqual(process.stdout, "")
                self.assertIn("active database", json.loads(process.stderr)["error"])
                self.assertEqual(sidecar.read_text(encoding="utf-8"), "sentinel")

    def test_review_requires_the_current_review_id(self):
        raw = json.dumps(
            {
                "question": "并发复习令牌",
                "user_answer": "B",
                "correct_answer": "C",
                "is_correct": False,
                "knowledge_ids": ["growth.ratio"],
                "errors": ["formula_error"],
                "method_quality": "acceptable",
            },
            ensure_ascii=False,
        )
        recorded = self.parse_success(self.run_cli("record", "--json", raw))

        process = self.run_cli(
            "review",
            "--attempt-id",
            recorded["attempt_id"],
            "--result",
            "good",
            expect=1,
        )

        self.assertEqual(process.stdout, "")
        error = json.loads(process.stderr)
        self.assertFalse(error["ok"])
        self.assertIn("review-id", error["error"])

    def test_review_rejects_a_stale_review_id(self):
        raw = json.dumps(
            {
                "question": "过期复习令牌",
                "user_answer": "B",
                "correct_answer": "C",
                "is_correct": False,
                "knowledge_ids": ["growth.ratio"],
                "errors": ["formula_error"],
                "method_quality": "acceptable",
            },
            ensure_ascii=False,
        )
        recorded = self.parse_success(self.run_cli("record", "--json", raw))

        process = self.run_cli(
            "review",
            "--attempt-id",
            recorded["attempt_id"],
            "--review-id",
            recorded["review_id"] + 1,
            "--result",
            "good",
            expect=1,
        )

        self.assertEqual(process.stdout, "")
        error = json.loads(process.stderr)
        self.assertFalse(error["ok"])
        self.assertIn("scheduled review changed", error["error"])

    def test_future_due_is_a_read_only_projection_but_future_review_is_rejected(self):
        raw = json.dumps(
            {
                "question": "未来到期查询只是投影",
                "user_answer": "B",
                "correct_answer": "C",
                "is_correct": False,
                "knowledge_ids": ["growth.ratio"],
                "errors": ["formula_error"],
                "method_quality": "acceptable",
            },
            ensure_ascii=False,
        )
        recorded = self.parse_success(self.run_cli("record", "--json", raw))
        future = (date.today() + timedelta(days=1)).isoformat()

        due = self.parse_success(self.run_cli("due", "--date", future))
        self.assertEqual(due["count"], 1)
        self.assertEqual(due["reviews"][0]["attempt_id"], recorded["attempt_id"])
        self.assertNotIn("correct_answer", due["reviews"][0])

        process = self.run_cli(
            "review",
            "--attempt-id",
            recorded["attempt_id"],
            "--review-id",
            recorded["review_id"],
            "--result",
            "good",
            "--date",
            future,
            expect=1,
        )
        self.assertEqual(process.stdout, "")
        error = json.loads(process.stderr)
        self.assertFalse(error["ok"])
        self.assertIn("future", error["error"])

    def test_read_commands_do_not_rewrite_or_change_journal_mode(self):
        self.run_cli("init")
        with sqlite3.connect(self.db) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
        before_bytes = self.db.read_bytes()
        before_digest = hashlib.sha256(before_bytes).hexdigest()
        before_mtime = self.db.stat().st_mtime_ns

        result = self.parse_success(self.run_cli("due", "--date", "2099-01-01"))

        self.assertEqual(result["count"], 0)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before_digest)
        self.assertEqual(self.db.stat().st_mtime_ns, before_mtime)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_read_command_does_not_create_a_missing_database(self):
        process = self.run_cli("progress", expect=1)

        self.assertEqual(process.stdout, "")
        self.assertFalse(self.db.exists())
        self.assertIn("does not exist", json.loads(process.stderr)["error"].lower())

    def test_invalid_record_payload_never_creates_target_database(self):
        payloads = ("{", "{}", '{"question":"q"}')
        for index, raw in enumerate(payloads):
            with self.subTest(raw=raw):
                database = Path(self.temp.name) / f"invalid-{index}.sqlite3"
                process = self.run_cli(
                    "record",
                    "--json",
                    raw,
                    database=database,
                    expect=1,
                )

                self.assertEqual(process.stdout, "")
                self.assertFalse(database.exists())

    def test_malformed_record_does_not_rejournal_existing_database(self):
        self.run_cli("init")
        with sqlite3.connect(self.db) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
        before = self.db.read_bytes()

        process = self.run_cli("record", "--json", "{", expect=1)

        self.assertEqual(process.stdout, "")
        self.assertEqual(self.db.read_bytes(), before)
        with sqlite3.connect(self.db) as connection:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "delete")

    def test_rejected_review_never_creates_a_missing_database(self):
        cases = (
            ("2099-01-01", "future"),
            (None, "does not exist"),
        )
        for index, (reviewed_on, expected_error) in enumerate(cases):
            with self.subTest(reviewed_on=reviewed_on):
                database = Path(self.temp.name) / f"missing-review-{index}.sqlite3"
                arguments = [
                    "review",
                    "--attempt-id",
                    "1",
                    "--review-id",
                    "1",
                    "--result",
                    "good",
                ]
                if reviewed_on is not None:
                    arguments.extend(("--date", reviewed_on))
                process = self.run_cli(
                    *arguments,
                    database=database,
                    expect=1,
                )

                self.assertFalse(database.exists())
                self.assertIn(expected_error, json.loads(process.stderr)["error"])

    def test_calculate_supports_all_nine_formulas_without_creating_database(self):
        cases = (
            (
                "base_period",
                {"current": 120, "rate": 20, "rate_unit": "percent"},
                100.0,
                "same unit as current",
            ),
            (
                "growth_amount",
                {"current": 120, "rate": 20, "rate_unit": "percent"},
                20.0,
                "same unit as current",
            ),
            (
                "interval_growth",
                {"r1": 10, "r2": 20, "rate_unit": "percent"},
                0.32,
                "decimal",
            ),
            (
                "ratio_growth",
                {
                    "num_rate": 8.9,
                    "den_rate": 8.7,
                    "rate_unit": "percent",
                },
                (0.089 - 0.087) / 1.087,
                "decimal",
            ),
            (
                "product_growth",
                {"r1": 10, "r2": 20, "rate_unit": "percent"},
                0.32,
                "decimal",
            ),
            (
                "current_share",
                {"part": 30, "whole": 120},
                0.25,
                "decimal",
            ),
            (
                "base_share",
                {
                    "part": 30,
                    "whole": 120,
                    "part_rate": 20,
                    "whole_rate": 10,
                    "rate_unit": "percent",
                },
                0.25 * 1.1 / 1.2,
                "decimal",
            ),
            (
                "share_change",
                {
                    "part": 30,
                    "whole": 120,
                    "part_rate": 20,
                    "whole_rate": 10,
                    "rate_unit": "percent",
                },
                0.25 * 0.1 / 1.2,
                "decimal",
            ),
            (
                "contribution_rate",
                {"part_delta": 30, "whole_delta": 120},
                0.25,
                "decimal",
            ),
        )

        self.assertFalse(self.db.exists())
        for formula, inputs, expected, semantics_fragment in cases:
            with self.subTest(formula=formula):
                result = self.parse_success(
                    self.run_cli(
                        "calculate",
                        "--formula",
                        formula,
                        "--json-file",
                        "-",
                        input_text=json.dumps(inputs),
                    )
                )
                self.assertEqual(result["formula"], formula)
                self.assertTrue(
                    math.isclose(result["result"], expected, rel_tol=1e-12)
                )
                self.assertIn(semantics_fragment, result["result_semantics"])
                self.assertIsInstance(result["normalized_inputs"], dict)
        self.assertFalse(self.db.exists())

    def test_calculate_percent_and_decimal_rate_units_are_equivalent(self):
        decimal_source = Path(self.temp.name) / "decimal-rate.json"
        decimal_source.write_text(
            json.dumps(
                {"current": 420, "rate": 0.05, "rate_unit": "decimal"}
            ),
            encoding="utf-8",
        )

        percent = self.parse_success(
            self.run_cli(
                "calculate",
                "--formula",
                "base_period",
                "--json-file",
                "-",
                input_text=json.dumps(
                    {"current": 420, "rate": 5, "rate_unit": "percent"}
                ),
            )
        )
        decimal = self.parse_success(
            self.run_cli(
                "calculate",
                "--formula",
                "base_period",
                "--json-file",
                decimal_source,
            )
        )

        self.assertEqual(percent["result"], decimal["result"])
        self.assertEqual(percent["normalized_inputs"]["rate"], 0.05)
        self.assertEqual(decimal["normalized_inputs"]["rate"], 0.05)
        self.assertEqual(percent["normalized_inputs"]["rate_unit"], "decimal")
        self.assertFalse(self.db.exists())

    def test_calculate_rejects_missing_unknown_units_and_nonexact_keys(self):
        cases = (
            (
                "missing unit",
                "base_period",
                {"current": 100, "rate": 5},
                "missing",
            ),
            (
                "unknown unit",
                "base_period",
                {"current": 100, "rate": 5, "rate_unit": "percentage"},
                "rate_unit",
            ),
            (
                "missing formula key",
                "base_period",
                {"rate": 5, "rate_unit": "percent"},
                "missing",
            ),
            (
                "extra key",
                "current_share",
                {"part": 20, "whole": 100, "rate_unit": "percent"},
                "unexpected",
            ),
        )

        for label, formula, inputs, expected_error in cases:
            with self.subTest(label=label):
                process = self.run_cli(
                    "calculate",
                    "--formula",
                    formula,
                    "--json-file",
                    "-",
                    input_text=json.dumps(inputs),
                    expect=1,
                )
                self.assertEqual(process.stdout, "")
                error = json.loads(process.stderr)
                self.assertIn(expected_error, error["error"])
        self.assertFalse(self.db.exists())

    def test_check_estimate_verifies_bound_and_returns_safety_details_without_db(self):
        marker = Path(self.temp.name) / "must-not-exist-estimate"
        derivation = f"截位误差不超过 1；$(touch {marker}) 仅为普通文本"
        payload = {
            "estimate": 100,
            "absolute_error_bound": 1,
            "exact_value": 99.5,
            "options": [90, 100, 110],
            "bias": "high",
            "error_bound_derivation": derivation,
        }

        result = self.parse_success(
            self.run_cli(
                "check-estimate",
                "--json-file",
                "-",
                input_text=json.dumps(payload, ensure_ascii=False),
            )
        )

        self.assertTrue(result["bound_verified"])
        self.assertEqual(result["actual_error"], 0.5)
        self.assertEqual(result["exact_value"], 99.5)
        self.assertEqual(result["error_bound_derivation"], derivation)
        self.assertEqual(result["bias"], "high")
        self.assertTrue(result["safe"])
        self.assertFalse(marker.exists())
        self.assertFalse(self.db.exists())

    def test_check_estimate_rejects_understated_and_wrong_direction_bounds(self):
        base = {
            "estimate": 100,
            "absolute_error_bound": 2,
            "exact_value": 99,
            "options": [90, 100, 110],
            "bias": "high",
            "error_bound_derivation": "严格误差界",
        }
        cases = (
            (
                "understated",
                {**base, "absolute_error_bound": 0.5},
                "outside declared",
            ),
            (
                "wrong direction",
                {**base, "exact_value": 101},
                "bias direction",
            ),
        )

        for label, payload, expected_error in cases:
            with self.subTest(label=label):
                process = self.run_cli(
                    "check-estimate",
                    "--json-file",
                    "-",
                    input_text=json.dumps(payload, ensure_ascii=False),
                    expect=1,
                )
                self.assertEqual(process.stdout, "")
                self.assertIn(
                    expected_error,
                    json.loads(process.stderr)["error"],
                )
        self.assertFalse(self.db.exists())

    def test_check_estimate_does_not_collapse_large_json_numbers_to_float(self):
        payloads = (
            """{"estimate":10000000000000000,
            "absolute_error_bound":0,
            "exact_value":10000000000000001,
            "options":[10000000000000000,10000000000000100],
            "bias":"low","error_bound_derivation":"integer boundary"}""",
            """{"estimate":10000000000000000.0,
            "absolute_error_bound":0.0,
            "exact_value":10000000000000001.0,
            "options":[10000000000000000.0,10000000000000100.0],
            "bias":"low","error_bound_derivation":"decimal boundary"}""",
        )

        for raw_payload in payloads:
            with self.subTest(payload=raw_payload):
                process = self.run_cli(
                    "check-estimate",
                    "--json-file",
                    "-",
                    input_text=raw_payload,
                    expect=1,
                )
                self.assertEqual(process.stdout, "")
                self.assertIn(
                    "outside declared",
                    json.loads(process.stderr)["error"],
                )
        self.assertFalse(self.db.exists())

    def test_check_estimate_requires_exact_keys_and_nonempty_derivation(self):
        base = {
            "estimate": 100,
            "absolute_error_bound": 1,
            "exact_value": 100,
            "options": [90, 100, 110],
            "bias": "two-sided",
            "error_bound_derivation": "误差界推导",
        }
        cases = (
            ("blank derivation", {**base, "error_bound_derivation": "  "}, "nonempty"),
            ("missing key", {key: value for key, value in base.items() if key != "bias"}, "missing"),
            ("extra key", {**base, "note": "not allowed"}, "unexpected"),
        )

        for label, payload, expected_error in cases:
            with self.subTest(label=label):
                process = self.run_cli(
                    "check-estimate",
                    "--json-file",
                    "-",
                    input_text=json.dumps(payload, ensure_ascii=False),
                    expect=1,
                )
                self.assertEqual(process.stdout, "")
                self.assertIn(
                    expected_error,
                    json.loads(process.stderr)["error"],
                )
        self.assertFalse(self.db.exists())

    def test_unknown_id_is_nonzero_json_error_on_stderr_and_no_stdout(self):
        raw = json.dumps(
            {
                "question": "bad tag",
                "user_answer": "A",
                "correct_answer": "B",
                "is_correct": False,
                "knowledge_ids": ["not.real"],
                "errors": ["formula_error"],
                "method_quality": "acceptable",
            }
        )
        process = self.run_cli("record", "--json", raw, expect=1)
        self.assertEqual(process.stdout, "")
        error = json.loads(process.stderr)
        self.assertFalse(error["ok"])
        self.assertIn("unknown knowledge", error["error"])

    def test_record_json_file_path_round_trips_quotes_and_newlines(self):
        question = '材料写道："增长率为 5.8%"。\n第二行保留原始换行。'
        payload = {
            "question": question,
            "user_answer": "C",
            "correct_answer": "C",
            "is_correct": True,
            "knowledge_ids": ["abrx.growth-amount"],
            "errors": [],
            "method_quality": "optimal",
        }
        source = Path(self.temp.name) / "attempt.json"
        source.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        recorded = self.parse_success(
            self.run_cli("record", "--json-file", source)
        )
        fetched = self.parse_success(
            self.run_cli("attempt", recorded["attempt_id"])
        )

        self.assertEqual(fetched["attempt"]["question"], question)

    def test_record_json_stdin_treats_shell_metacharacters_as_plain_data(self):
        marker = Path(self.temp.name) / "must-not-exist"
        question = (
            f'引号 " 和换行\n$(touch {marker}); `whoami`; '
            "$HOME && a|b > output < input"
        )
        raw = json.dumps(
            {
                "question": question,
                "user_answer": "C",
                "correct_answer": "C",
                "is_correct": True,
                "knowledge_ids": ["abrx.growth-amount"],
                "errors": [],
                "method_quality": "optimal",
            },
            ensure_ascii=False,
        )

        recorded = self.parse_success(
            self.run_cli(
                "record",
                "--json-file",
                "-",
                input_text=raw,
            )
        )
        fetched = self.parse_success(
            self.run_cli("attempt", recorded["attempt_id"])
        )

        self.assertEqual(fetched["attempt"]["question"], question)
        self.assertFalse(marker.exists())

    def test_record_json_and_json_file_are_mutually_exclusive(self):
        raw = json.dumps(
            {
                "question": "互斥输入",
                "user_answer": "A",
                "correct_answer": "A",
                "is_correct": True,
                "knowledge_ids": ["abrx.base"],
                "errors": [],
                "method_quality": "optimal",
            }
        )
        source = Path(self.temp.name) / "attempt.json"
        source.write_text(raw, encoding="utf-8")

        process = self.run_cli(
            "record",
            "--json",
            raw,
            "--json-file",
            source,
            expect=1,
        )

        self.assertEqual(process.stdout, "")
        error = json.loads(process.stderr)
        self.assertFalse(error["ok"])
        self.assertIn("--json-file", error["error"])

    def test_non_standard_nan_json_is_rejected(self):
        raw = (
            '{"question":"bad number","user_answer":"A",'
            '"correct_answer":"B","is_correct":false,'
            '"duration_seconds":NaN,"knowledge_ids":["abrx.base"],'
            '"errors":["formula_error"],"method_quality":"acceptable"}'
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
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary_workspace:
            workspace = Path(temporary_workspace)
            copied_skill = workspace / ".agents/skills/gongkao-data-analysis-coach"
            copied_skill.parent.mkdir(parents=True)
            shutil.copytree(
                SKILL,
                copied_skill,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            copied_cli = copied_skill / "scripts/coach.py"
            process = subprocess.run(
                [sys.executable, str(copied_cli), "init"],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            payload = json.loads(process.stdout)
            expected = (workspace / ".gongkao-study/study.sqlite3").resolve()
            self.assertEqual(Path(payload["database"]), expected)
            self.assertNotIn(".agents/skills", payload["database"])

    def test_root_and_subcommand_help_are_single_json_success_documents(self):
        for arguments in (("--help",), ("record", "--help")):
            with self.subTest(arguments=arguments):
                process = subprocess.run(
                    [sys.executable, str(CLI), *arguments],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(process.returncode, 0, process.stderr)
                payload = json.loads(process.stdout)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["object_id"], "help")
                self.assertIn("usage:", payload["help"])
                self.assertEqual(process.stderr, "")

    def test_help_reports_the_custom_validated_database_path(self):
        custom = Path(self.temp.name) / "custom-help.sqlite3"
        for arguments in (("--help",), ("record", "--help")):
            with self.subTest(arguments=arguments):
                process = subprocess.run(
                    [
                        sys.executable,
                        str(CLI),
                        "--db",
                        str(custom),
                        *arguments,
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(process.returncode, 0, process.stderr)
                payload = json.loads(process.stdout)
                self.assertEqual(Path(payload["database"]), custom.resolve())
                self.assertFalse(custom.exists())


if __name__ == "__main__":
    unittest.main()
