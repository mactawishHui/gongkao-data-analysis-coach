#!/usr/bin/env python3
"""JSON-only command line interface for the local study coach."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from coachlib.store import StudyStore


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATABASE = WORKSPACE_ROOT / ".gongkao-study" / "study.sqlite3"

# Used until (and as a recovery fallback if) the knowledge graph cannot load.
FALLBACK_KNOWLEDGE_IDS = {
    "speed.tail-digits",
    "speed.high-place-addition",
    "speed.integer-baseline",
    "speed.segment-subtraction",
    "speed.common-fractions",
    "speed.split-multiplication",
    "speed.truncated-division",
    "speed.estimate-safety",
    "abrx.base",
    "abrx.current",
    "abrx.growth-amount",
    "abrx.growth-rate",
    "growth.general",
    "growth.interval",
    "growth.ratio",
    "growth.product",
    "share.current",
    "share.base",
    "share.trend",
    "share.change",
    "share.value-change",
    "compare.ratio",
    "compare.increment",
    "compare.order",
    "mixture.qualitative",
    "mixture.cross",
    "average.single",
    "average.compare",
    "average.growth",
    "average.annual-increase",
    "special.pull",
    "special.contribution",
    "special.inclusion-exclusion",
    "trap.period",
    "trap.unit",
    "trap.wording",
    "trap.source-conflict",
}


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _knowledge_weights() -> dict[str, float]:
    graph_path = SKILL_DIR / "references" / "knowledge-graph.json"
    if not graph_path.exists():
        return {node_id: 1.0 for node_id in FALLBACK_KNOWLEDGE_IDS}
    try:
        document = json.loads(graph_path.read_text(encoding="utf-8"))
        nodes = document["nodes"] if isinstance(document, dict) else document
        result = {
            node["id"]: node.get("weight", 1.0)
            for node in nodes
            if isinstance(node, dict) and isinstance(node.get("id"), str)
        }
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"knowledge graph cannot be loaded: {error}") from error
    if not result:
        raise ValueError("knowledge graph contains no nodes")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="公考资料分析教练本地状态工具")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("init")

    record = subcommands.add_parser("record")
    record.add_argument("--json", required=True, dest="raw_json")

    attempt = subcommands.add_parser("attempt")
    attempt.add_argument("attempt_id", type=int)

    mistakes = subcommands.add_parser("mistakes")
    mistakes.add_argument("--knowledge-id")
    mistakes.add_argument("--limit", type=int, default=50)

    due = subcommands.add_parser("due")
    due.add_argument("--date", dest="on_date")

    review = subcommands.add_parser("review")
    review.add_argument("--attempt-id", type=int, required=True)
    review.add_argument("--review-id", type=int)
    review.add_argument("--result", choices=("wrong", "hard", "good"), required=True)
    review.add_argument("--date", dest="reviewed_on")

    progress = subcommands.add_parser("progress")
    progress.add_argument("--knowledge-id")

    export = subcommands.add_parser("export")
    export.add_argument("--output", type=Path, required=True)

    generate = subcommands.add_parser("generate")
    generate.add_argument("--knowledge-id", required=True)
    generate.add_argument("--count", type=int, default=5)
    generate.add_argument("--seed", type=int, default=0)
    return parser


def _response(database: Path, object_id: Any, **payload: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "database": str(database.expanduser().resolve()),
        "object_id": object_id,
        **payload,
    }


def _without_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"ok", "database", "object_id"}
    }


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard numeric constant is not allowed: {value}")


def _dispatch(arguments: argparse.Namespace) -> dict[str, Any]:
    command = arguments.command
    if command == "generate":
        try:
            from coachlib.practice import generate_set
        except ImportError as error:
            raise RuntimeError("practice generator is not implemented yet") from error
        questions = generate_set(
            arguments.knowledge_id,
            arguments.count,
            arguments.seed,
        )
        return _response(
            arguments.db,
            f"practice:{arguments.seed}",
            questions=questions,
        )

    store = StudyStore(arguments.db, _knowledge_weights())
    if command == "init":
        initialized = store.initialization_receipt
        return _response(arguments.db, "schema-v2", **_without_envelope(initialized))
    if command == "record":
        try:
            payload = json.loads(
                arguments.raw_json,
                parse_constant=_reject_nonstandard_constant,
            )
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid record JSON: {error.msg}") from error
        receipt = store.record_attempt(payload)
        return _response(
            arguments.db,
            receipt["attempt_id"],
            **_without_envelope(receipt),
        )
    if command == "attempt":
        attempt = store.get_attempt(arguments.attempt_id)
        return _response(arguments.db, arguments.attempt_id, attempt=attempt)
    if command == "mistakes":
        attempts = store.list_mistakes(arguments.knowledge_id, arguments.limit)
        return _response(arguments.db, "mistakes", attempts=attempts, count=len(attempts))
    if command == "due":
        reviews = store.due_reviews(arguments.on_date)
        return _response(arguments.db, "due", reviews=reviews, count=len(reviews))
    if command == "review":
        receipt = store.record_review(
            arguments.attempt_id,
            arguments.result,
            arguments.reviewed_on,
            arguments.review_id,
        )
        return _response(
            arguments.db,
            receipt["review_id"],
            **_without_envelope(receipt),
        )
    if command == "progress":
        progress = store.progress(arguments.knowledge_id)
        return _response(arguments.db, arguments.knowledge_id or "overall", progress=progress)
    if command == "export":
        exported = store.export_json(arguments.output)
        return _response(
            arguments.db,
            exported["output"],
            **_without_envelope(exported),
        )
    raise ValueError(f"unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        result = _dispatch(arguments)
        serialized = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
        )
    except Exception as error:  # The CLI contract converts all expected failures to JSON.
        serialized_error = json.dumps(
            {"ok": False, "error": str(error), "type": type(error).__name__},
            ensure_ascii=False,
            allow_nan=False,
        )
        sys.stderr.write(serialized_error + "\n")
        return 1
    sys.stdout.write(serialized + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
