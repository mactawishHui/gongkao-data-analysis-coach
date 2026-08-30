#!/usr/bin/env python3
"""JSON-only command line interface for the local study coach."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import date
from decimal import Decimal
from numbers import Real
from pathlib import Path
from typing import Any

from coachlib import formulas
from coachlib.store import ERROR_CODES, StudyStore, paths_alias


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
RESOLVED_WORKSPACE_ROOT = WORKSPACE_ROOT.resolve()
DEFAULT_DATABASE = WORKSPACE_ROOT / ".gongkao-study" / "study.sqlite3"

FORMULA_SPECS = {
    "base_period": {
        "input_names": ("current", "rate"),
        "rate_fields": ("rate",),
        "result_semantics": "same unit as current; input rate is normalized as a decimal",
    },
    "growth_amount": {
        "input_names": ("current", "rate"),
        "rate_fields": ("rate",),
        "result_semantics": "same unit as current; input rate is normalized as a decimal",
    },
    "interval_growth": {
        "input_names": ("r1", "r2"),
        "rate_fields": ("r1", "r2"),
        "result_semantics": "decimal growth rate; multiply by 100 for percent",
    },
    "ratio_growth": {
        "input_names": ("num_rate", "den_rate"),
        "rate_fields": ("num_rate", "den_rate"),
        "result_semantics": "decimal growth rate; multiply by 100 for percent",
    },
    "product_growth": {
        "input_names": ("r1", "r2"),
        "rate_fields": ("r1", "r2"),
        "result_semantics": "decimal growth rate; multiply by 100 for percent",
    },
    "current_share": {
        "input_names": ("part", "whole"),
        "rate_fields": (),
        "result_semantics": "decimal share; multiply by 100 for percent",
    },
    "base_share": {
        "input_names": ("part", "whole", "part_rate", "whole_rate"),
        "rate_fields": ("part_rate", "whole_rate"),
        "result_semantics": "decimal share; multiply by 100 for percent",
    },
    "share_change": {
        "input_names": ("part", "whole", "part_rate", "whole_rate"),
        "rate_fields": ("part_rate", "whole_rate"),
        "result_semantics": (
            "decimal share change; multiply by 100 for percentage points"
        ),
    },
    "contribution_rate": {
        "input_names": ("part_delta", "whole_delta"),
        "rate_fields": (),
        "result_semantics": "decimal contribution ratio; multiply by 100 for percent",
    },
}

ESTIMATE_BIAS_ALIASES = {
    "two-sided": "two-sided",
    "unknown": "two-sided",
    "high": "high",
    "overestimate": "high",
    "偏大": "high",
    "low": "low",
    "underestimate": "low",
    "偏小": "low",
}

ESTIMATE_CHECK_KEYS = frozenset(
    {
        "estimate",
        "absolute_error_bound",
        "exact_value",
        "options",
        "bias",
        "error_bound_derivation",
    }
)
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
READ_ONLY_STORE_COMMANDS = frozenset(
    {"attempt", "mistakes", "due", "progress", "export"}
)
EXPECTED_KNOWLEDGE_ROOTS = frozenset(
    {"speed", "abrx", "growth", "share", "compare", "mixture", "average", "special", "trap"}
)
EXPECTED_KNOWLEDGE_NODE_COUNT = 50
EXPECTED_ASSESSABLE_NODE_COUNT = 41
EXPECTED_KNOWLEDGE_IDS = frozenset(
    {
        "speed",
        "abrx",
        "growth",
        "share",
        "compare",
        "mixture",
        "average",
        "special",
        "trap",
        "speed.trailing-digit",
        "speed.high-order-sum",
        "speed.round-benchmark",
        "speed.segmented-subtraction",
        "speed.fraction",
        "speed.decomposition",
        "speed.division",
        "speed.415",
        "speed.assumed-allocation",
        "abrx.general",
        "abrx.current",
        "abrx.base",
        "abrx.rate",
        "abrx.growth-amount",
        "growth.basic",
        "growth.interval",
        "growth.ratio",
        "growth.product",
        "share.current",
        "share.base",
        "share.multilevel",
        "share.trend",
        "share.change",
        "share.value-difference",
        "compare.fraction",
        "compare.increment",
        "compare.increment-rate",
        "compare.catch-up",
        "compare.find-then-calculate",
        "mixture.salt-water",
        "mixture.three-rates",
        "mixture.cross",
        "average.single",
        "average.multiple",
        "average.annual-increase",
        "average.annual-rate",
        "special.pull",
        "special.contribution",
        "special.inclusion-exclusion",
        "trap.time-scope",
        "trap.integrated",
    }
)
KNOWLEDGE_NODE_FIELDS = frozenset(
    {
        "id",
        "title",
        "parent",
        "assessable",
        "prerequisites",
        "weight",
        "sources",
        "signals",
        "formula_ids",
        "fast_methods",
        "error_codes",
    }
)
CANONICAL_KNOWLEDGE_MANIFEST_SHA256 = (
    "5ac46ffd0a2e09708867cbd93d3f8764268f9478b3dabc44b17b4dc8a46fcecc"
)


class HelpRequested(Exception):
    """Internal control flow for JSON-formatted argparse help."""

    def __init__(self, help_text: str):
        super().__init__("help requested")
        self.help_text = help_text


class JsonArgumentParser(argparse.ArgumentParser):
    def print_help(self, file=None) -> None:
        self._json_help_text = self.format_help()

    def exit(self, status=0, message=None) -> None:
        if status == 0:
            raise HelpRequested(
                getattr(self, "_json_help_text", self.format_help())
            )
        raise ValueError((message or "argument parsing failed").strip())

    def error(self, message: str) -> None:
        raise ValueError(message)


def _knowledge_weights(graph_path: str | Path | None = None) -> dict[str, float]:
    path = (
        SKILL_DIR / "references" / "knowledge-graph.json"
        if graph_path is None
        else Path(graph_path)
    )
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ValueError(f"knowledge graph does not exist: {path}") from error
    except OSError as error:
        raise ValueError(f"knowledge graph cannot be read: {path}: {error}") from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"knowledge graph has invalid JSON: {error.msg}"
        ) from error
    if not isinstance(document, dict):
        raise ValueError("knowledge graph document must be an object")
    if "nodes" not in document:
        raise ValueError("knowledge graph is missing nodes")
    nodes = document["nodes"]
    if not isinstance(nodes, list):
        raise ValueError("knowledge graph nodes must be a list")
    if not nodes:
        raise ValueError("knowledge graph contains no nodes")

    result: dict[str, float] = {}
    seen_ids: set[str] = set()
    nodes_by_id: dict[str, dict[str, Any]] = {}
    missing_assessable: list[str] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError(f"knowledge graph node[{index}] must be an object")
        if "id" not in node:
            raise ValueError(f"knowledge graph node[{index}] is missing id")
        knowledge_id = node["id"]
        if not isinstance(knowledge_id, str) or not knowledge_id.strip():
            raise ValueError(
                f"knowledge graph node[{index}] id must be a nonempty string"
            )
        if re.fullmatch(r"[a-z]+(?:[.-][a-z0-9]+)*", knowledge_id) is None:
            raise ValueError(
                f"knowledge graph id has invalid stable format: {knowledge_id}"
            )
        if knowledge_id in seen_ids:
            raise ValueError(f"knowledge graph contains duplicate id: {knowledge_id}")
        seen_ids.add(knowledge_id)
        if "weight" not in node:
            raise ValueError(
                f"knowledge graph node[{index}] is missing weight"
            )
        weight = node["weight"]
        if isinstance(weight, bool) or not isinstance(weight, Real):
            raise ValueError(
                f"knowledge graph weight for {knowledge_id} must be a finite number"
            )
        normalized_weight = float(weight)
        if not math.isfinite(normalized_weight):
            raise ValueError(
                f"knowledge graph weight for {knowledge_id} must be a finite number"
            )
        if normalized_weight <= 0.0:
            raise ValueError(
                f"knowledge graph weight for {knowledge_id} must be positive"
            )
        if "assessable" not in node:
            missing_assessable.append(knowledge_id)
            assessable = None
        else:
            assessable = node["assessable"]
            if not isinstance(assessable, bool):
                raise ValueError(
                    f"knowledge graph assessable for {knowledge_id} "
                    "must be an explicit boolean"
                )
        if assessable:
            result[knowledge_id] = normalized_weight
        nodes_by_id[knowledge_id] = node
    if missing_assessable:
        raise ValueError(
            "knowledge graph assessable must be explicit for: "
            + ", ".join(sorted(missing_assessable))
        )
    if not result:
        raise ValueError("knowledge graph contains no assessable nodes")
    try:
        total_weight = math.fsum(result.values())
    except OverflowError as error:
        raise ValueError("knowledge graph assessable weight total overflows") from error
    if not math.isfinite(total_weight):
        raise ValueError("knowledge graph assessable weight total must be finite")

    if document.get("version") != 1:
        raise ValueError("knowledge graph version must be exactly 1")
    if len(nodes) != EXPECTED_KNOWLEDGE_NODE_COUNT:
        raise ValueError(
            "knowledge graph must contain exactly "
            f"{EXPECTED_KNOWLEDGE_NODE_COUNT} nodes"
        )
    if seen_ids != EXPECTED_KNOWLEDGE_IDS:
        missing_ids = sorted(EXPECTED_KNOWLEDGE_IDS - seen_ids)
        unexpected_ids = sorted(seen_ids - EXPECTED_KNOWLEDGE_IDS)
        details = []
        if missing_ids:
            details.append("missing " + ", ".join(missing_ids))
        if unexpected_ids:
            details.append("unexpected " + ", ".join(unexpected_ids))
        raise ValueError(
            "knowledge graph stable id registry does not match: "
            + "; ".join(details)
        )
    if len(result) != EXPECTED_ASSESSABLE_NODE_COUNT:
        raise ValueError(
            "knowledge graph must contain exactly "
            f"{EXPECTED_ASSESSABLE_NODE_COUNT} assessable nodes"
        )

    dependencies: dict[str, list[str]] = {}
    roots: set[str] = set()
    for knowledge_id, node in nodes_by_id.items():
        missing_fields = KNOWLEDGE_NODE_FIELDS - set(node)
        if missing_fields:
            raise ValueError(
                f"knowledge graph node {knowledge_id} is missing fields: "
                + ", ".join(sorted(missing_fields))
            )
        title = node["title"]
        if not isinstance(title, str) or not title.strip():
            raise ValueError(
                f"knowledge graph title for {knowledge_id} must be nonempty"
            )
        parent = node["parent"]
        if parent is None:
            roots.add(knowledge_id)
        elif not isinstance(parent, str) or parent not in nodes_by_id:
            raise ValueError(
                f"knowledge graph parent for {knowledge_id} does not exist"
            )
        if parent == knowledge_id:
            raise ValueError(
                f"knowledge graph node {knowledge_id} cannot depend on itself"
            )

        prerequisites = node["prerequisites"]
        if not isinstance(prerequisites, list) or any(
            not isinstance(item, str) for item in prerequisites
        ):
            raise ValueError(
                f"knowledge graph prerequisites for {knowledge_id} must be a list of ids"
            )
        if len(prerequisites) != len(set(prerequisites)):
            raise ValueError(
                f"knowledge graph prerequisites for {knowledge_id} contain duplicates"
            )
        for prerequisite in prerequisites:
            if prerequisite == knowledge_id:
                raise ValueError(
                    f"knowledge graph node {knowledge_id} cannot depend on itself"
                )
            if prerequisite not in nodes_by_id:
                raise ValueError(
                    f"knowledge graph prerequisite {prerequisite} does not exist"
                )
        dependencies[knowledge_id] = [
            *([] if parent is None else [parent]),
            *prerequisites,
        ]

        for field in ("signals", "formula_ids", "fast_methods", "error_codes"):
            values = node[field]
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip()
                for value in values
            ):
                raise ValueError(
                    f"knowledge graph {field} for {knowledge_id} must be a list of strings"
                )
        if not node["signals"] or not node["fast_methods"] or not node["error_codes"]:
            raise ValueError(
                f"knowledge graph coaching metadata for {knowledge_id} must not be empty"
            )
        unknown_errors = set(node["error_codes"]) - ERROR_CODES
        if unknown_errors:
            raise ValueError(
                f"knowledge graph node {knowledge_id} has unknown error codes: "
                + ", ".join(sorted(unknown_errors))
            )
        sources = node["sources"]
        if not isinstance(sources, list) or not sources:
            raise ValueError(
                f"knowledge graph sources for {knowledge_id} must not be empty"
            )
        for source in sources:
            if (
                not isinstance(source, dict)
                or set(source) != {"lesson", "pages"}
                or isinstance(source["lesson"], bool)
                or not isinstance(source["lesson"], int)
                or not 1 <= source["lesson"] <= 10
                or not isinstance(source["pages"], list)
                or not source["pages"]
                or any(
                    isinstance(page, bool) or not isinstance(page, int) or page <= 0
                    for page in source["pages"]
                )
            ):
                raise ValueError(
                    f"knowledge graph source for {knowledge_id} is malformed"
                )

    if roots != EXPECTED_KNOWLEDGE_ROOTS:
        raise ValueError(
            "knowledge graph roots do not match the nine required branches"
        )
    for knowledge_id, node in nodes_by_id.items():
        is_root = knowledge_id in roots
        if node["assessable"] is is_root:
            raise ValueError(
                "knowledge graph roots must be non-assessable and child nodes "
                f"must be assessable: {knowledge_id}"
            )
        namespace = knowledge_id.split(".", 1)[0]
        if namespace not in EXPECTED_KNOWLEDGE_ROOTS:
            raise ValueError(
                f"knowledge graph namespace for {knowledge_id} is invalid"
            )

    visit_state: dict[str, int] = {}

    def visit(knowledge_id: str) -> None:
        if visit_state.get(knowledge_id) == 1:
            raise ValueError(
                f"knowledge graph contains a dependency cycle at {knowledge_id}"
            )
        if visit_state.get(knowledge_id) == 2:
            return
        visit_state[knowledge_id] = 1
        for dependency in dependencies[knowledge_id]:
            visit(dependency)
        visit_state[knowledge_id] = 2

    for knowledge_id in nodes_by_id:
        visit(knowledge_id)

    canonical_manifest = {
        "version": document["version"],
        "nodes": document["nodes"],
    }
    try:
        manifest_bytes = json.dumps(
            canonical_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(
            "knowledge graph canonical manifest cannot be serialized"
        ) from error
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_digest != CANONICAL_KNOWLEDGE_MANIFEST_SHA256:
        raise ValueError(
            "knowledge graph canonical manifest digest does not match"
        )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="公考资料分析教练本地状态工具")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("init")

    record = subcommands.add_parser("record")
    record_input = record.add_mutually_exclusive_group(required=True)
    record_input.add_argument("--json", dest="raw_json")
    record_input.add_argument("--json-file", type=Path)

    attempt = subcommands.add_parser("attempt")
    attempt.add_argument("attempt_id", type=int)

    mistakes = subcommands.add_parser("mistakes")
    mistakes.add_argument("--knowledge-id")
    mistakes.add_argument("--limit", type=int, default=50)

    due = subcommands.add_parser("due")
    due.add_argument("--date", dest="on_date")

    review = subcommands.add_parser("review")
    review.add_argument("--attempt-id", type=int, required=True)
    review.add_argument("--review-id", type=int, required=True)
    review.add_argument("--result", choices=("wrong", "hard", "good"), required=True)
    review.add_argument("--date", dest="reviewed_on")

    progress = subcommands.add_parser("progress")
    progress.add_argument("--knowledge-id")

    export = subcommands.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--force", action="store_true")

    generate = subcommands.add_parser("generate")
    generate.add_argument("--knowledge-id", required=True)
    generate.add_argument("--count", type=int, default=5)
    generate.add_argument("--seed", type=int, default=0)
    generate.add_argument(
        "--difficulty",
        choices=("easy", "medium", "hard"),
        default="medium",
    )

    subcommands.add_parser("practice-catalog")

    calculate = subcommands.add_parser("calculate")
    calculate.add_argument(
        "--formula",
        choices=tuple(FORMULA_SPECS),
        required=True,
    )
    calculate.add_argument("--json-file", type=Path, required=True)

    check_estimate = subcommands.add_parser("check-estimate")
    check_estimate.add_argument("--json-file", type=Path, required=True)
    return parser


def _response(database: Path, object_id: Any, **payload: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "database": str(database),
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


def _reject_future_date(value: str | None) -> None:
    """Keep simulation dates out of the user-facing CLI boundary."""

    if value is None:
        return
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("--date must be an ISO date (YYYY-MM-DD)") from error
    if parsed > date.today():
        raise ValueError("--date cannot be in the future")


def _json_file_text(source: Path, context: str) -> str:
    if source == Path("-"):
        return sys.stdin.read()
    try:
        return source.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(
            f"cannot read {context} JSON file {source}: {error}"
        ) from error


def _json_object(
    text: str,
    context: str,
    *,
    preserve_numbers: bool = False,
) -> dict[str, Any]:
    try:
        numeric_parsers = (
            {"parse_int": Decimal, "parse_float": Decimal}
            if preserve_numbers
            else {}
        )
        payload = json.loads(
            text,
            parse_constant=_reject_nonstandard_constant,
            **numeric_parsers,
        )
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid {context} JSON: {error.msg}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{context} JSON must be an object")
    return payload


def _json_file_object(
    source: Path,
    context: str,
    *,
    preserve_numbers: bool = False,
) -> dict[str, Any]:
    return _json_object(
        _json_file_text(source, context),
        context,
        preserve_numbers=preserve_numbers,
    )


def _require_exact_keys(
    payload: dict[str, Any],
    required: set[str] | frozenset[str],
    context: str,
) -> None:
    actual = set(payload)
    missing = sorted(required - actual)
    unexpected = sorted(actual - required)
    problems = []
    if missing:
        problems.append(f"missing keys: {', '.join(missing)}")
    if unexpected:
        problems.append(f"unexpected keys: {', '.join(unexpected)}")
    if problems:
        raise ValueError(f"{context} JSON has {'; '.join(problems)}")


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _exact_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    else:
        raise ValueError(f"{name} must be a finite number")
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite number")
    return result


def _decimal_to_finite_float(value: Decimal, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must fit in the finite numeric range")
    return result


def _calculate(formula_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    spec = FORMULA_SPECS[formula_name]
    input_names = spec["input_names"]
    rate_fields = spec["rate_fields"]
    required = set(input_names)
    if rate_fields:
        required.add("rate_unit")
    _require_exact_keys(payload, required, f"calculate {formula_name}")

    normalized = dict(payload)
    if rate_fields:
        unit = payload["rate_unit"]
        if not isinstance(unit, str) or unit not in {"decimal", "percent"}:
            raise ValueError("rate_unit must be exactly 'decimal' or 'percent'")
        divisor = 100.0 if unit == "percent" else 1.0
        for field in rate_fields:
            normalized[field] = _finite_number(payload[field], field) / divisor
        normalized["rate_unit"] = "decimal"

    formula = getattr(formulas, formula_name)
    arguments = {name: normalized[name] for name in input_names}
    result = formula(**arguments)
    return {
        "formula": formula_name,
        "normalized_inputs": normalized,
        "result": result,
        "result_semantics": spec["result_semantics"],
    }


def _check_estimate(payload: dict[str, Any]) -> dict[str, Any]:
    _require_exact_keys(payload, ESTIMATE_CHECK_KEYS, "check-estimate")
    derivation = payload["error_bound_derivation"]
    if not isinstance(derivation, str) or not derivation.strip():
        raise ValueError("error_bound_derivation must be a nonempty string")

    estimate_decimal = _exact_decimal(payload["estimate"], "estimate")
    error_bound_decimal = _exact_decimal(
        payload["absolute_error_bound"],
        "absolute_error_bound",
    )
    exact_value_decimal = _exact_decimal(payload["exact_value"], "exact_value")
    if error_bound_decimal < 0:
        raise ValueError("absolute_error_bound must be non-negative")
    bias = payload["bias"]
    if not isinstance(bias, str) or bias not in ESTIMATE_BIAS_ALIASES:
        raise ValueError(
            "bias must be one of: two-sided, unknown, high, low, "
            "overestimate, underestimate, 偏大, 偏小"
        )
    normalized_bias = ESTIMATE_BIAS_ALIASES[bias]

    actual_error_decimal = abs(estimate_decimal - exact_value_decimal)
    if actual_error_decimal > error_bound_decimal:
        raise ValueError(
            "exact_value is outside declared absolute_error_bound: "
            f"actual_error={actual_error_decimal}, bound={error_bound_decimal}"
        )
    if normalized_bias == "high" and exact_value_decimal > estimate_decimal:
        raise ValueError(
            "exact_value violates declared high bias direction: "
            "a high estimate must be no smaller than the exact value"
        )
    if normalized_bias == "low" and exact_value_decimal < estimate_decimal:
        raise ValueError(
            "exact_value violates declared low bias direction: "
            "a low estimate must be no greater than the exact value"
        )

    estimate = _decimal_to_finite_float(estimate_decimal, "estimate")
    error_bound = _decimal_to_finite_float(
        error_bound_decimal,
        "absolute_error_bound",
    )
    exact_value = _decimal_to_finite_float(exact_value_decimal, "exact_value")
    actual_error = _decimal_to_finite_float(actual_error_decimal, "actual_error")
    raw_options = payload["options"]
    if not isinstance(raw_options, list):
        raise ValueError("options must be a list of finite numbers")
    options = [
        _decimal_to_finite_float(
            _exact_decimal(value, f"options[{index}]"),
            f"options[{index}]",
        )
        for index, value in enumerate(raw_options)
    ]
    assessment = formulas.assess_estimate_safety(
        estimate,
        error_bound,
        options,
        normalized_bias,
    )
    return {
        **assessment,
        "exact_value": exact_value,
        "actual_error": actual_error,
        "error_bound_derivation": derivation,
        "bound_verified": True,
    }


def _workspace_path(value: str | Path, name: str) -> Path:
    resolved = Path(value).expanduser().resolve()
    try:
        resolved.relative_to(RESOLVED_WORKSPACE_ROOT)
    except ValueError as error:
        raise ValueError(
            f"{name} must stay inside workspace root "
            f"{RESOLVED_WORKSPACE_ROOT}: {resolved}"
        ) from error
    return resolved


def _database_from_argv(argv: list[str]) -> Path:
    candidate: str | Path = DEFAULT_DATABASE
    for index, token in enumerate(argv):
        if token == "--db" and index + 1 < len(argv):
            candidate = argv[index + 1]
            break
        if token.startswith("--db="):
            candidate = token.split("=", 1)[1]
            break
    return _workspace_path(candidate, "--db")


def _dispatch(arguments: argparse.Namespace) -> dict[str, Any]:
    arguments.db = _workspace_path(arguments.db, "--db")
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
            arguments.difficulty,
        )
        return _response(
            arguments.db,
            f"practice:{arguments.seed}",
            difficulty=arguments.difficulty,
            questions=questions,
        )
    if command == "practice-catalog":
        try:
            from coachlib.practice import practice_catalog
        except ImportError as error:
            raise RuntimeError("practice catalog is not implemented yet") from error
        templates = practice_catalog()
        return _response(
            arguments.db,
            "practice-catalog",
            templates=templates,
            count=len(templates),
        )
    if command == "calculate":
        payload = _json_file_object(arguments.json_file, "calculate")
        calculation = _calculate(arguments.formula, payload)
        return _response(
            arguments.db,
            f"calculation:{arguments.formula}",
            **calculation,
        )
    if command == "check-estimate":
        payload = _json_file_object(
            arguments.json_file,
            "check-estimate",
            preserve_numbers=True,
        )
        assessment = _check_estimate(payload)
        return _response(
            arguments.db,
            "estimate-check",
            **assessment,
        )
    if command == "export":
        arguments.output = _workspace_path(arguments.output, "export --output")
        active_database_artifacts = {
            arguments.db,
            *(Path(f"{arguments.db}{suffix}") for suffix in SQLITE_SIDECAR_SUFFIXES),
        }
        if any(
            paths_alias(arguments.output, artifact)
            for artifact in active_database_artifacts
        ):
            raise ValueError("export --output cannot be the active database path")

    knowledge_weights = _knowledge_weights()
    record_payload: dict[str, Any] | None = None
    if command == "record":
        if arguments.raw_json is not None:
            record_payload = _json_object(arguments.raw_json, "record")
        else:
            record_payload = _json_file_object(arguments.json_file, "record")
        StudyStore.validate_attempt_payload(record_payload, knowledge_weights)
    if command == "review":
        _reject_future_date(arguments.reviewed_on)
        if not arguments.db.is_file():
            raise FileNotFoundError(
                f"study database does not exist: {arguments.db}"
            )

    store = (
        StudyStore.open_readonly(
            arguments.db,
            knowledge_weights,
            path_already_resolved=True,
        )
        if command in READ_ONLY_STORE_COMMANDS
        else StudyStore(
            arguments.db,
            knowledge_weights,
            path_already_resolved=True,
        )
    )
    if command == "init":
        initialized = store.initialization_receipt
        if initialized is None:
            raise RuntimeError("database initialization did not return a receipt")
        return _response(
            arguments.db,
            initialized["object_id"],
            **_without_envelope(initialized),
        )
    if command == "record":
        if record_payload is None:
            raise RuntimeError("record payload preflight did not complete")
        receipt = store.record_attempt(record_payload)
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
        exported = store.export_json(
            arguments.output,
            overwrite=arguments.force,
            path_already_resolved=True,
        )
        return _response(
            arguments.db,
            exported["output"],
            **_without_envelope(exported),
        )
    raise ValueError(f"unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        arguments = _parser().parse_args(raw_argv)
        result = _dispatch(arguments)
        serialized = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
        )
    except HelpRequested as requested:
        try:
            help_database = _database_from_argv(raw_argv)
        except Exception as error:
            serialized_error = json.dumps(
                {"ok": False, "error": str(error), "type": type(error).__name__},
                ensure_ascii=False,
                allow_nan=False,
            )
            sys.stderr.write(serialized_error + "\n")
            return 1
        serialized_help = json.dumps(
            {
                "ok": True,
                "database": str(help_database),
                "object_id": "help",
                "help": requested.help_text,
            },
            ensure_ascii=False,
            allow_nan=False,
        )
        sys.stdout.write(serialized_help + "\n")
        return 0
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
