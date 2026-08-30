"""Fail-closed knowledge-graph loading tests."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".agents/skills/gongkao-data-analysis-coach"
CLI = SKILL / "scripts/coach.py"
GRAPH = SKILL / "references/knowledge-graph.json"


def _load_coach_module():
    module_name = "gongkao_coach_cli_for_knowledge_loading_tests"
    scripts = str(CLI.parent)
    spec = importlib.util.spec_from_file_location(module_name, CLI)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import CLI module from {CLI}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, scripts)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(scripts)
    return module


COACH = _load_coach_module()


class KnowledgeWeightLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            dir=ROOT,
            prefix=".coach-knowledge-loader-test-",
        )
        self.addCleanup(self.temp.cleanup)
        self.temp_path = Path(self.temp.name)

    def graph_path(self, label: str, content: object | str) -> Path:
        path = self.temp_path / f"{label}.json"
        text = content if isinstance(content, str) else json.dumps(content)
        path.write_text(text, encoding="utf-8")
        return path

    def test_production_default_loads_the_complete_exact_weight_mapping(self):
        document = json.loads(GRAPH.read_text(encoding="utf-8"))
        expected = {
            node["id"]: float(node["weight"])
            for node in document["nodes"]
            if node.get("assessable", True)
        }

        loaded = COACH._knowledge_weights()

        self.assertEqual(loaded, expected)
        self.assertEqual(
            len(loaded),
            sum(node.get("assessable", True) for node in document["nodes"]),
        )
        self.assertTrue(
            all(node["parent"] is not None for node in document["nodes"] if node["id"] in loaded)
        )

    def test_assessable_is_explicit_and_a_root_cannot_become_recordable(self):
        document = json.loads(GRAPH.read_text(encoding="utf-8"))
        speed = next(node for node in document["nodes"] if node["id"] == "speed")
        del speed["assessable"]
        path = self.graph_path("root-missing-assessable", document)

        with self.assertRaisesRegex(ValueError, "assessable"):
            COACH._knowledge_weights(path)

    def test_parent_and_prerequisite_cycles_fail_closed(self):
        original = json.loads(GRAPH.read_text(encoding="utf-8"))
        cases = (
            ("parent", "speed.trailing-digit", "speed.trailing-digit"),
            ("prerequisites", "speed", ["trap.integrated"]),
        )
        for field, knowledge_id, value in cases:
            with self.subTest(field=field):
                document = json.loads(json.dumps(original))
                node = next(
                    item for item in document["nodes"]
                    if item["id"] == knowledge_id
                )
                node[field] = value
                path = self.graph_path(f"cycle-{field}", document)
                with self.assertRaisesRegex(ValueError, "cycle|itself"):
                    COACH._knowledge_weights(path)

    def test_node_ids_must_match_the_canonical_stable_registry(self):
        original = json.loads(GRAPH.read_text(encoding="utf-8"))
        for replacement in ("speed.bad id", "speed.renamed"):
            with self.subTest(replacement=replacement):
                document = json.loads(json.dumps(original))
                node = next(
                    item for item in document["nodes"]
                    if item["id"] == "speed.trailing-digit"
                )
                node["id"] = replacement
                path = self.graph_path("renamed-leaf", document)
                with self.assertRaisesRegex(ValueError, "id|stable"):
                    COACH._knowledge_weights(path)

    def test_canonical_node_semantics_cannot_be_modified(self):
        original = json.loads(GRAPH.read_text(encoding="utf-8"))
        mutations = (
            ("title", "基期量 A（被篡改）"),
            ("parent", "speed"),
            ("prerequisites", []),
            ("weight", 1.21),
            ("sources", [{"lesson": 3, "pages": [1, 2, 3, 4, 5, 6, 7, 8]}]),
        )

        for field, replacement in mutations:
            with self.subTest(field=field):
                document = json.loads(json.dumps(original))
                node = next(
                    item for item in document["nodes"]
                    if item["id"] == "abrx.base"
                )
                node[field] = replacement
                path = self.graph_path(f"modified-abrx-base-{field}", document)

                with self.assertRaisesRegex(ValueError, "canonical|digest"):
                    COACH._knowledge_weights(path)

    def test_positive_finite_weights_cannot_overflow_the_total(self):
        document = json.loads(GRAPH.read_text(encoding="utf-8"))
        for knowledge_id in ("speed.trailing-digit", "speed.high-order-sum"):
            node = next(
                item for item in document["nodes"]
                if item["id"] == knowledge_id
            )
            node["weight"] = 1e308
        path = self.graph_path("overflowing-total-weight", document)

        with self.assertRaisesRegex(ValueError, "total|overflow"):
            COACH._knowledge_weights(path)

    def test_explicit_path_rejects_every_malformed_graph_shape(self):
        missing = self.temp_path / "missing.json"
        cases = (
            ("missing file", missing, "does not exist"),
            ("bad JSON", self.graph_path("bad-json", "{"), "invalid JSON"),
            (
                "duplicate id",
                self.graph_path(
                    "duplicate-id",
                    {
                        "nodes": [
                            {"id": "growth.ratio", "weight": 1},
                            {"id": "growth.ratio", "weight": 2},
                        ]
                    },
                ),
                "duplicate",
            ),
            (
                "string weight",
                self.graph_path(
                    "string-weight",
                    {"nodes": [{"id": "growth.ratio", "weight": "1"}]},
                ),
                "weight",
            ),
            (
                "boolean weight",
                self.graph_path(
                    "boolean-weight",
                    {"nodes": [{"id": "growth.ratio", "weight": True}]},
                ),
                "weight",
            ),
            (
                "non-finite weight",
                self.graph_path(
                    "non-finite-weight",
                    '{"nodes":[{"id":"growth.ratio","weight":NaN}]}',
                ),
                "weight",
            ),
            (
                "zero weight",
                self.graph_path(
                    "zero-weight",
                    {"nodes": [{"id": "growth.ratio", "weight": 0}]},
                ),
                "positive",
            ),
            (
                "negative weight",
                self.graph_path(
                    "negative-weight",
                    {"nodes": [{"id": "growth.ratio", "weight": -0.1}]},
                ),
                "positive",
            ),
            (
                "missing id",
                self.graph_path(
                    "missing-id",
                    {
                        "nodes": [
                            {"id": "growth.ratio", "weight": 1},
                            {"weight": 2},
                        ]
                    },
                ),
                "missing id",
            ),
            (
                "missing weight",
                self.graph_path(
                    "missing-weight",
                    {
                        "nodes": [
                            {"id": "growth.ratio", "weight": 1},
                            {"id": "growth.product"},
                        ]
                    },
                ),
                "missing weight",
            ),
            (
                "nodes is object",
                self.graph_path(
                    "nodes-object",
                    {"nodes": {"id": "growth.ratio", "weight": 1}},
                ),
                "nodes must be a list",
            ),
            (
                "assessable is not boolean",
                self.graph_path(
                    "assessable-string",
                    {
                        "nodes": [
                            {
                                "id": "growth.ratio",
                                "weight": 1,
                                "assessable": "yes",
                            }
                        ]
                    },
                ),
                "assessable",
            ),
            (
                "no assessable nodes",
                self.graph_path(
                    "all-categories",
                    {
                        "nodes": [
                            {
                                "id": "growth",
                                "weight": 1,
                                "assessable": False,
                            }
                        ]
                    },
                ),
                "no assessable",
            ),
        )

        for label, path, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, message):
                    COACH._knowledge_weights(path)


class KnowledgeGraphCliBoundaryTests(unittest.TestCase):
    def copied_skill(self, graph_content: str | None):
        temporary = tempfile.TemporaryDirectory(
            dir=ROOT,
            prefix=".coach-knowledge-cli-test-",
        )
        self.addCleanup(temporary.cleanup)
        fixture_root = Path(temporary.name)
        fixture_skill = (
            fixture_root / ".agents/skills/gongkao-data-analysis-coach"
        )
        fixture_skill.parent.mkdir(parents=True)
        shutil.copytree(SKILL, fixture_skill)
        fixture_graph = fixture_skill / "references/knowledge-graph.json"
        if graph_content is None:
            fixture_graph.unlink()
        else:
            fixture_graph.write_text(graph_content, encoding="utf-8")
        return (
            fixture_root,
            fixture_skill / "scripts/coach.py",
            fixture_root / "state/study.sqlite3",
        )

    def run_cli(
        self,
        fixture_root: Path,
        fixture_cli: Path,
        database: Path,
        *arguments: str,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(fixture_cli),
                "--db",
                str(database),
                *arguments,
            ],
            cwd=fixture_root,
            text=True,
            input=input_text,
            capture_output=True,
            check=False,
        )

    def assert_graph_failure_without_database(
        self,
        graph_content: str | None,
        *arguments: str,
    ) -> None:
        fixture_root, fixture_cli, database = self.copied_skill(graph_content)

        process = self.run_cli(
            fixture_root,
            fixture_cli,
            database,
            *arguments,
        )

        self.assertEqual(process.returncode, 1, process.stdout)
        self.assertEqual(process.stdout, "")
        error = json.loads(process.stderr)
        self.assertFalse(error["ok"])
        self.assertIn("knowledge graph", error["error"])
        self.assertFalse(database.exists())

    def test_each_invalid_graph_fails_as_json_before_database_creation(self):
        cases = (
            ("missing", None),
            ("bad JSON", "{"),
            (
                "duplicate id",
                '{"nodes":[{"id":"growth.ratio","weight":1},'
                '{"id":"growth.ratio","weight":2}]}',
            ),
            (
                "invalid weight",
                '{"nodes":[{"id":"growth.ratio","weight":"heavy"}]}',
            ),
            (
                "non-positive weight",
                '{"nodes":[{"id":"growth.ratio","weight":0}]}',
            ),
            (
                "missing id",
                '{"nodes":[{"id":"growth.ratio","weight":1},'
                '{"weight":2}]}',
            ),
            (
                "missing weight",
                '{"nodes":[{"id":"growth.ratio","weight":1},'
                '{"id":"growth.product"}]}',
            ),
            (
                "nodes non-list",
                '{"nodes":{"id":"growth.ratio","weight":1}}',
            ),
        )

        for label, graph_content in cases:
            with self.subTest(label=label):
                self.assert_graph_failure_without_database(
                    graph_content,
                    "init",
                )

    def test_deleted_root_assessable_marker_cannot_persist_a_root_tag(self):
        document = json.loads(GRAPH.read_text(encoding="utf-8"))
        speed = next(node for node in document["nodes"] if node["id"] == "speed")
        del speed["assessable"]
        payload = json.dumps(
            {
                "question": "根节点不得作为作答标签",
                "user_answer": "A",
                "correct_answer": "A",
                "is_correct": True,
                "knowledge_ids": ["speed"],
                "errors": [],
                "method_quality": "optimal",
            },
            ensure_ascii=False,
        )

        self.assert_graph_failure_without_database(
            json.dumps(document, ensure_ascii=False),
            "record",
            "--json",
            payload,
        )

    def test_every_stateful_command_requires_the_graph_before_opening_database(self):
        record_payload = json.dumps(
            {
                "question": "图缺失时不得建库",
                "user_answer": "A",
                "correct_answer": "B",
                "is_correct": False,
                "knowledge_ids": ["growth.ratio"],
                "errors": ["formula_error"],
                "method_quality": "acceptable",
            },
            ensure_ascii=False,
        )
        commands = (
            ("init",),
            ("record", "--json", record_payload),
            ("attempt", "1"),
            ("mistakes",),
            ("due",),
            (
                "review",
                "--attempt-id",
                "1",
                "--review-id",
                "1",
                "--result",
                "good",
            ),
            ("progress",),
            ("export", "--output", "export.json"),
        )

        for command in commands:
            with self.subTest(command=command[0]):
                self.assert_graph_failure_without_database(None, *command)

    def test_stateless_commands_do_not_load_the_missing_graph_or_create_database(self):
        fixture_root, fixture_cli, database = self.copied_skill(None)
        cases = (
            (
                ("generate", "--knowledge-id", "abrx.base", "--count", "1"),
                None,
            ),
            (
                (
                    "calculate",
                    "--formula",
                    "current_share",
                    "--json-file",
                    "-",
                ),
                json.dumps({"part": 25, "whole": 100}),
            ),
            (
                ("check-estimate", "--json-file", "-"),
                json.dumps(
                    {
                        "estimate": 100,
                        "absolute_error_bound": 1,
                        "exact_value": 100,
                        "options": [90, 100, 110],
                        "bias": "two-sided",
                        "error_bound_derivation": "精确值用于回测",
                    },
                    ensure_ascii=False,
                ),
            ),
        )

        for command, input_text in cases:
            with self.subTest(command=command[0]):
                process = self.run_cli(
                    fixture_root,
                    fixture_cli,
                    database,
                    *command,
                    input_text=input_text,
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                result = json.loads(process.stdout)
                self.assertTrue(result["ok"])
                self.assertEqual(process.stderr, "")
                self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
