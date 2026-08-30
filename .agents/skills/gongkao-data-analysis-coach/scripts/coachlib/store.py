"""Local SQLite persistence for attempts, mistakes, reviews, and progress."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import statistics
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .progress import classify_mastery, wilson_interval


GOOD_INTERVALS = (1, 3, 7, 14, 30)
METHOD_QUALITIES = {"optimal", "acceptable", "low"}
REVIEW_RESULTS = {"wrong", "hard", "good"}
SCHEMA_VERSION = 2


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_date(value: str | date | None, *, name: str) -> str:
    if value is None:
        return date.today().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD)") from error


def _json_dump(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("method metadata must be JSON serializable") from error


def _json_load(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _finite_number(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


class StudyStore:
    """A small transactional study database owned by the current workspace."""

    def __init__(
        self,
        database: str | Path,
        known_knowledge_ids: Iterable[str] | Mapping[str, float],
    ):
        self.database = Path(database).expanduser().resolve()
        if isinstance(known_knowledge_ids, Mapping):
            self.knowledge_weights = {
                str(item): _finite_number(weight, f"weight for {item}")
                for item, weight in known_knowledge_ids.items()
            }
        else:
            self.knowledge_weights = {
                str(item): 1.0 for item in known_knowledge_ids
            }
        if any(weight <= 0 for weight in self.knowledge_weights.values()):
            raise ValueError("knowledge weights must be positive")
        self.known_knowledge_ids = frozenset(self.knowledge_weights)
        if not self.known_knowledge_ids:
            raise ValueError("known knowledge ids must not be empty")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.initialization_receipt = self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> dict[str, Any]:
        statements = (
            "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            """CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                user_answer TEXT,
                correct_answer TEXT,
                is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
                duration_seconds REAL CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
                target_duration_seconds REAL NOT NULL DEFAULT 60 CHECK (target_duration_seconds > 0),
                confidence INTEGER CHECK (confidence IS NULL OR confidence BETWEEN 1 AND 5),
                difficulty TEXT,
                mode TEXT,
                parse_confidence REAL CHECK (parse_confidence IS NULL OR parse_confidence BETWEEN 0 AND 1),
                method_used TEXT,
                recommended_method TEXT,
                method_quality TEXT NOT NULL CHECK (method_quality IN ('optimal', 'acceptable', 'low')),
                error_path TEXT,
                correction_rule TEXT,
                source_ref TEXT,
                traceability TEXT NOT NULL DEFAULT 'high' CHECK (traceability IN ('high', 'low')),
                priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('normal', 'high')),
                is_transfer INTEGER NOT NULL DEFAULT 0 CHECK (is_transfer IN (0, 1)),
                exact_method_json TEXT,
                fast_method_json TEXT,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS attempt_tags (
                attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                knowledge_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (attempt_id, knowledge_id)
            )""",
            "CREATE INDEX IF NOT EXISTS attempt_tags_knowledge_idx ON attempt_tags(knowledge_id, attempt_id)",
            """CREATE TABLE IF NOT EXISTS attempt_errors (
                attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                error_code TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (attempt_id, error_code)
            )""",
            """CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                result TEXT CHECK (result IS NULL OR result IN ('wrong', 'hard', 'good')),
                status TEXT NOT NULL CHECK (status IN ('scheduled', 'completed')),
                due_date TEXT NOT NULL,
                reviewed_on TEXT,
                interval_days INTEGER NOT NULL CHECK (interval_days > 0),
                round INTEGER NOT NULL DEFAULT 1 CHECK (round > 0),
                interval_qualified INTEGER NOT NULL DEFAULT 0
                    CHECK (interval_qualified IN (0, 1)),
                created_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS reviews_due_idx ON reviews(status, due_date, attempt_id)",
            """CREATE UNIQUE INDEX IF NOT EXISTS reviews_one_scheduled_idx
               ON reviews(attempt_id) WHERE status='scheduled'""",
            """CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                knowledge_id TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                metadata_json TEXT
            )""",
        )
        attempt_migrations = {
            "target_duration_seconds": "REAL NOT NULL DEFAULT 60 CHECK (target_duration_seconds > 0)",
            "difficulty": "TEXT",
            "mode": "TEXT",
            "parse_confidence": "REAL CHECK (parse_confidence IS NULL OR parse_confidence BETWEEN 0 AND 1)",
            "error_path": "TEXT",
            "correction_rule": "TEXT",
            "traceability": "TEXT NOT NULL DEFAULT 'high' CHECK (traceability IN ('high', 'low'))",
            "priority": "TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('normal', 'high'))",
            "is_transfer": "INTEGER NOT NULL DEFAULT 0 CHECK (is_transfer IN (0, 1))",
        }
        review_migrations = {
            "round": "INTEGER NOT NULL DEFAULT 1 CHECK (round > 0)",
            "interval_qualified": (
                "INTEGER NOT NULL DEFAULT 0 "
                "CHECK (interval_qualified IN (0, 1))"
            ),
        }
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            meta_exists = connection.execute(
                """SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='schema_meta'"""
            ).fetchone()
            if meta_exists is not None:
                version_row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='version'"
                ).fetchone()
                if version_row is not None:
                    try:
                        existing_version = int(version_row[0])
                    except (TypeError, ValueError) as error:
                        raise ValueError("database schema version is invalid") from error
                    if existing_version > SCHEMA_VERSION:
                        raise ValueError(
                            "database uses newer schema version "
                            f"{existing_version}; this coach supports "
                            f"version {SCHEMA_VERSION}"
                        )
            for statement in statements:
                connection.execute(statement)
            attempt_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(attempts)")
            }
            for name, definition in attempt_migrations.items():
                if name not in attempt_columns:
                    connection.execute(
                        f"ALTER TABLE attempts ADD COLUMN {name} {definition}"
                    )
            review_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(reviews)")
            }
            for name, definition in review_migrations.items():
                if name not in review_columns:
                    connection.execute(
                        f"ALTER TABLE reviews ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "ok": True,
            "database": str(self.database),
            "object_id": f"schema-v{SCHEMA_VERSION}",
            "schema_version": SCHEMA_VERSION,
        }

    def _validate_knowledge_ids(self, raw_ids: Any) -> list[str]:
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("knowledge_ids must be a non-empty list")
        ids: list[str] = []
        for raw in raw_ids:
            if not isinstance(raw, str) or not raw:
                raise ValueError("knowledge_ids must contain non-empty strings")
            if raw not in self.known_knowledge_ids:
                available = ", ".join(sorted(self.known_knowledge_ids))
                raise ValueError(
                    f"unknown knowledge id: {raw}; available knowledge ids: {available}"
                )
            if raw not in ids:
                ids.append(raw)
        return ids

    @staticmethod
    def _validate_errors(raw_errors: Any) -> list[str]:
        if raw_errors is None:
            return []
        if not isinstance(raw_errors, list):
            raise ValueError("errors must be a list")
        errors: list[str] = []
        for raw in raw_errors:
            if not isinstance(raw, str) or not raw:
                raise ValueError("errors must contain non-empty strings")
            if raw not in errors:
                errors.append(raw)
        return errors

    def record_attempt(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("attempt payload must be a JSON object")
        knowledge_ids = self._validate_knowledge_ids(payload.get("knowledge_ids"))
        errors = self._validate_errors(payload.get("errors", []))
        question = payload.get("question")
        if question is None or (isinstance(question, str) and not question.strip()):
            question = "[匿名作答：未提供题目]"
            traceability = "low"
        elif not isinstance(question, str):
            raise ValueError("question must be a string when provided")
        else:
            question = question.strip()
            traceability = payload.get("traceability", "high")
        if traceability not in {"high", "low"}:
            raise ValueError("traceability must be high or low")
        if not isinstance(payload.get("is_correct"), bool):
            raise ValueError("is_correct must be a boolean")
        is_correct = payload["is_correct"]
        method_quality = payload.get("method_quality")
        if method_quality not in METHOD_QUALITIES:
            raise ValueError("method_quality must be optimal, acceptable, or low")

        duration = payload.get("duration_seconds")
        if duration is not None:
            duration = _finite_number(duration, "duration_seconds")
            if duration < 0:
                raise ValueError("duration_seconds must be a non-negative number")
        confidence = payload.get("confidence")
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, int) or not 1 <= confidence <= 5:
                raise ValueError("confidence must be an integer from 1 to 5")

        parse_confidence = payload.get("parse_confidence")
        if parse_confidence is not None:
            parse_confidence = _finite_number(
                parse_confidence, "parse_confidence"
            )
            if not 0 <= parse_confidence <= 1:
                raise ValueError("parse_confidence must be between 0 and 1")
        is_transfer = payload.get("is_transfer", False)
        if not isinstance(is_transfer, bool):
            raise ValueError("is_transfer must be a boolean")
        target_duration = payload.get("target_duration_seconds", 60)
        target_duration = _finite_number(
            target_duration, "target_duration_seconds"
        )
        if target_duration <= 0:
            raise ValueError("target_duration_seconds must be a positive number")
        difficulty = payload.get("difficulty")
        mode = payload.get("mode")
        for field_name, value in (("difficulty", difficulty), ("mode", mode)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string when provided")
        priority = (
            "high"
            if (not is_correct and confidence is not None and confidence >= 4)
            else "normal"
        )

        created_at = _utc_now()
        next_review_date: str | None = None
        review_id: int | None = None
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO attempts(
                    question, user_answer, correct_answer, is_correct,
                    duration_seconds, target_duration_seconds, confidence,
                    difficulty, mode, parse_confidence, method_used,
                    recommended_method, method_quality, error_path,
                    correction_rule, source_ref, traceability, priority,
                    is_transfer, exact_method_json, fast_method_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    question,
                    payload.get("user_answer"),
                    payload.get("correct_answer"),
                    int(is_correct),
                    duration,
                    target_duration,
                    confidence,
                    difficulty,
                    mode,
                    parse_confidence,
                    payload.get("method_used"),
                    payload.get("recommended_method"),
                    method_quality,
                    payload.get("error_path"),
                    payload.get("correction_rule"),
                    payload.get("source_ref"),
                    traceability,
                    priority,
                    int(is_transfer),
                    _json_dump(payload.get("exact_method")),
                    _json_dump(payload.get("fast_method")),
                    created_at,
                ),
            )
            attempt_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO attempt_tags(attempt_id, knowledge_id, position) VALUES (?, ?, ?)",
                [(attempt_id, item, index) for index, item in enumerate(knowledge_ids)],
            )
            connection.executemany(
                "INSERT INTO attempt_errors(attempt_id, error_code, position) VALUES (?, ?, ?)",
                [(attempt_id, item, index) for index, item in enumerate(errors)],
            )
            needs_review = (
                (not is_correct)
                or method_quality == "low"
                or "source_conflict" in errors
            )
            if needs_review:
                next_review_date = (date.today() + timedelta(days=1)).isoformat()
                review = connection.execute(
                    """
                    INSERT INTO reviews(
                        attempt_id, result, status, due_date, reviewed_on,
                        interval_days, round, created_at
                    ) VALUES (?, NULL, 'scheduled', ?, NULL, 1, 1, ?)
                    """,
                    (attempt_id, next_review_date, created_at),
                )
                review_id = int(review.lastrowid)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "ok": True,
            "database": str(self.database),
            "object_id": attempt_id,
            "attempt_id": attempt_id,
            "review_id": review_id,
            "next_review_date": next_review_date,
            "knowledge_ids": knowledge_ids,
            "priority": priority,
        }

    def count_attempts(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])

    @staticmethod
    def _attempt_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["is_correct"] = bool(result["is_correct"])
        result["is_transfer"] = bool(result["is_transfer"])
        result["exact_method"] = _json_load(result.pop("exact_method_json"))
        result["fast_method"] = _json_load(result.pop("fast_method_json"))
        attempt_id = result["id"]
        result["knowledge_ids"] = [
            item[0]
            for item in connection.execute(
                "SELECT knowledge_id FROM attempt_tags WHERE attempt_id=? ORDER BY position",
                (attempt_id,),
            ).fetchall()
        ]
        result["errors"] = [
            item[0]
            for item in connection.execute(
                "SELECT error_code FROM attempt_errors WHERE attempt_id=? ORDER BY position",
                (attempt_id,),
            ).fetchall()
        ]
        return result

    def get_attempt(self, attempt_id: int) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                raise ValueError(f"attempt not found: {attempt_id}")
            return self._attempt_from_row(connection, row)

    def list_mistakes(self, knowledge_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if knowledge_id is not None and knowledge_id not in self.known_knowledge_ids:
            raise ValueError(f"unknown knowledge id: {knowledge_id}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        query = """
            SELECT DISTINCT a.* FROM attempts a
            LEFT JOIN attempt_tags t ON t.attempt_id=a.id
            WHERE (
                a.is_correct=0 OR a.method_quality='low' OR
                EXISTS (
                    SELECT 1 FROM attempt_errors e
                    WHERE e.attempt_id=a.id AND e.error_code='source_conflict'
                )
            )
        """
        parameters: list[Any] = []
        if knowledge_id is not None:
            query += " AND t.knowledge_id=?"
            parameters.append(knowledge_id)
        query += " ORDER BY a.id DESC LIMIT ?"
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [self._attempt_from_row(connection, row) for row in rows]

    def due_reviews(self, on_date: str | date | None = None) -> list[dict[str, Any]]:
        target = _iso_date(on_date, name="on_date")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT r.*, a.question, a.correct_answer, a.priority,
                       a.error_path, a.correction_rule
                FROM reviews r JOIN attempts a ON a.id=r.attempt_id
                WHERE r.status='scheduled' AND r.due_date<=?
                ORDER BY r.due_date, r.id
                """,
                (target,),
            ).fetchall()
            return [dict(row) for row in rows]

    def review_history(self, attempt_id: int) -> list[dict[str, Any]]:
        self.get_attempt(attempt_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM reviews WHERE attempt_id=? ORDER BY id",
                (attempt_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def record_review(
        self,
        attempt_id: int,
        result: str,
        reviewed_on: str | date | None = None,
        expected_review_id: int | None = None,
    ) -> dict[str, Any]:
        if result not in REVIEW_RESULTS:
            raise ValueError("review result must be wrong, hard, or good")
        reviewed = _iso_date(reviewed_on, name="reviewed_on")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            scheduled = connection.execute(
                """
                SELECT * FROM reviews
                WHERE attempt_id=? AND status='scheduled'
                ORDER BY id DESC LIMIT 1
                """,
                (attempt_id,),
            ).fetchone()
            if scheduled is None:
                exists = connection.execute(
                    "SELECT 1 FROM attempts WHERE id=?", (attempt_id,)
                ).fetchone()
                if exists is None:
                    raise ValueError(f"attempt not found: {attempt_id}")
                raise ValueError(f"no scheduled review for attempt: {attempt_id}")
            if (
                expected_review_id is not None
                and int(scheduled["id"]) != expected_review_id
            ):
                raise ValueError(
                    "scheduled review changed; reload due reviews before submitting"
                )
            schedule_start = (
                date.fromisoformat(scheduled["due_date"])
                - timedelta(days=int(scheduled["interval_days"]))
            ).isoformat()
            if reviewed < schedule_start:
                raise ValueError(
                    "reviewed_on cannot be before the attempt or schedule start"
                )
            previous = connection.execute(
                """SELECT reviewed_on FROM reviews
                    WHERE attempt_id=? AND status='completed'
                    ORDER BY id DESC LIMIT 1""",
                (attempt_id,),
            ).fetchone()
            if (
                previous is not None
                and previous["reviewed_on"] is not None
                and reviewed <= previous["reviewed_on"]
            ):
                raise ValueError(
                    "reviewed_on must be after the previous review date"
                )
            current_interval = int(scheduled["interval_days"])
            if result == "wrong":
                next_interval = 1
            elif result == "hard":
                next_interval = 3
            else:
                next_interval = next(
                    (value for value in GOOD_INTERVALS if value > current_interval),
                    GOOD_INTERVALS[-1],
                )
            next_date = (date.fromisoformat(reviewed) + timedelta(days=next_interval)).isoformat()
            interval_qualified = reviewed >= scheduled["due_date"]
            updated = connection.execute(
                """
                UPDATE reviews
                SET result=?, status='completed', reviewed_on=?,
                    interval_qualified=?
                WHERE id=? AND status='scheduled'
                """,
                (result, reviewed, int(interval_qualified), scheduled["id"]),
            )
            if updated.rowcount != 1:
                raise ValueError(
                    "scheduled review changed; reload due reviews before submitting"
                )
            cursor = connection.execute(
                """
                INSERT INTO reviews(
                    attempt_id, result, status, due_date, reviewed_on,
                    interval_days, round, created_at
                ) VALUES (?, NULL, 'scheduled', ?, NULL, ?, ?, ?)
                """,
                (
                    attempt_id,
                    next_date,
                    next_interval,
                    int(scheduled["round"]) + 1,
                    _utc_now(),
                ),
            )
            next_review_id = int(cursor.lastrowid)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "ok": True,
            "database": str(self.database),
            "object_id": next_review_id,
            "attempt_id": attempt_id,
            "result": result,
            "interval_days": next_interval,
            "next_review_date": next_date,
            "review_id": next_review_id,
            "interval_qualified": interval_qualified,
        }

    def _node_progress(self, knowledge_id: str | None) -> dict[str, Any]:
        query = "SELECT DISTINCT a.* FROM attempts a"
        parameters: tuple[Any, ...] = ()
        if knowledge_id is not None:
            query += " JOIN attempt_tags t ON t.attempt_id=a.id WHERE t.knowledge_id=?"
            parameters = (knowledge_id,)
        query += " ORDER BY a.id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
            total = len(rows)
            correct = sum(int(row["is_correct"]) for row in rows)
            recent = rows[-10:]
            recent_accuracy = (
                sum(int(row["is_correct"]) for row in recent) / len(recent)
                if recent else 0.0
            )
            low_count = sum(row["method_quality"] == "low" for row in rows)
            method_low_rate = low_count / total if total else 0.0
            method_counts = {
                quality: sum(row["method_quality"] == quality for row in rows)
                for quality in sorted(METHOD_QUALITIES)
            }
            method_efficiency = {
                quality: count / total if total else None
                for quality, count in method_counts.items()
            }
            durations = [float(row["duration_seconds"]) for row in rows if row["duration_seconds"] is not None]
            recent_durations = [
                float(row["duration_seconds"])
                for row in recent
                if row["duration_seconds"] is not None
            ]
            median_duration = statistics.median(durations) if durations else None
            recent_median = statistics.median(recent_durations) if recent_durations else None
            targets = [float(row["target_duration_seconds"]) for row in rows]
            target_duration = statistics.median(targets) if targets else 60.0
            attempt_ids = [int(row["id"]) for row in rows]
            review_passes = 0
            completed_reviews = 0
            early_completed_reviews = 0
            error_counts: dict[str, int] = {}
            if attempt_ids:
                placeholders = ",".join("?" for _ in attempt_ids)
                review_row = connection.execute(
                    f"""SELECT
                        SUM(CASE WHEN interval_qualified=1 THEN 1 ELSE 0 END) AS completed,
                        SUM(CASE WHEN interval_qualified=0 THEN 1 ELSE 0 END) AS early_completed,
                        SUM(CASE WHEN interval_qualified=1 AND result IN ('hard', 'good')
                                 THEN 1 ELSE 0 END) AS passed
                        FROM reviews
                        WHERE status='completed'
                          AND attempt_id IN ({placeholders})""",
                    attempt_ids,
                ).fetchone()
                completed_reviews = int(review_row["completed"] or 0)
                early_completed_reviews = int(review_row["early_completed"] or 0)
                review_passes = int(review_row["passed"] or 0)
                error_counts = {
                    row["error_code"]: int(row["occurrences"])
                    for row in connection.execute(
                        f"""SELECT error_code, COUNT(*) AS occurrences
                            FROM attempt_errors
                            WHERE attempt_id IN ({placeholders})
                            GROUP BY error_code""",
                        attempt_ids,
                    )
                }
        low, high = wilson_interval(correct, total)
        accuracy = correct / total if total else None
        speed_met = recent_median is not None and recent_median <= target_duration
        transfer_rows = [row for row in rows if bool(row["is_transfer"])]
        transfer_total = len(transfer_rows)
        transfer_correct = sum(int(row["is_correct"]) for row in transfer_rows)
        transfer_accuracy = (
            transfer_correct / transfer_total if transfer_total else None
        )
        transfer_passed = (
            transfer_accuracy is not None and transfer_accuracy >= 0.80
        )
        total_error_occurrences = sum(error_counts.values())
        repeated_error_occurrences = sum(
            max(count - 1, 0) for count in error_counts.values()
        )
        repeated_error_rate = (
            repeated_error_occurrences / total_error_occurrences
            if total_error_occurrences else None
        )
        retention_rate = (
            review_passes / completed_reviews if completed_reviews else None
        )
        mastery = classify_mastery(
            total=total,
            correct=correct,
            recent_accuracy=recent_accuracy,
            method_low_rate=method_low_rate,
            speed_met=speed_met,
            transfer_passed=transfer_passed,
            review_passes=review_passes,
        )
        return {
            "knowledge_id": knowledge_id,
            "total": total,
            "correct": correct,
            "accuracy": accuracy,
            "recent_accuracy": recent_accuracy,
            "wilson_90": {"low": low, "high": high},
            "median_duration_seconds": median_duration,
            "recent_median_duration_seconds": recent_median,
            "target_duration_seconds": target_duration,
            "speed_met": speed_met,
            "method_low_rate": method_low_rate,
            "review_passes": review_passes,
            "transfer_passed": transfer_passed,
            "mastery": mastery,
            "correctness": {
                "correct": correct,
                "total": total,
                "accuracy": accuracy,
                "wilson_90": {"low": low, "high": high},
            },
            "speed": {
                "median_seconds": median_duration,
                "recent_median_seconds": recent_median,
                "target_seconds": target_duration,
                "recent_gap_seconds": (
                    recent_median - target_duration
                    if recent_median is not None else None
                ),
                "met": speed_met,
            },
            "method_efficiency": method_efficiency,
            "method_counts": method_counts,
            "stability": {
                "overall_accuracy": accuracy,
                "recent_accuracy": recent_accuracy if total else None,
                "accuracy_delta": (
                    recent_accuracy - accuracy if accuracy is not None else None
                ),
                "repeated_error_rate": repeated_error_rate,
                "error_occurrences": error_counts,
            },
            "transfer": {
                "total": transfer_total,
                "correct": transfer_correct,
                "accuracy": transfer_accuracy,
                "passed": transfer_passed,
            },
            "retention": {
                "completed": completed_reviews,
                "passed": review_passes,
                "pass_rate": retention_rate,
                "early_completed": early_completed_reviews,
            },
        }

    def progress(self, knowledge_id: str | None = None) -> dict[str, Any]:
        if knowledge_id is not None:
            if knowledge_id not in self.known_knowledge_ids:
                raise ValueError(f"unknown knowledge id: {knowledge_id}")
            return self._node_progress(knowledge_id)

        aggregate = self._node_progress(None)
        nodes = [
            self._node_progress(node_id)
            for node_id in sorted(self.known_knowledge_ids)
        ]
        assessed_nodes = sum(node["total"] > 0 for node in nodes)
        total_nodes = len(nodes)
        mastery_scores = {
            "未评估": 0.0,
            "样本不足": 0.25,
            "学习中": 0.5,
            "基本掌握": 0.75,
            "稳定掌握": 1.0,
        }
        total_weight = sum(self.knowledge_weights.values())
        weighted_score = sum(
            mastery_scores[node["mastery"]]
            * self.knowledge_weights[node["knowledge_id"]]
            for node in nodes
        ) / total_weight
        mastery_distribution = {
            state: sum(node["mastery"] == state for node in nodes)
            for state in mastery_scores
        }
        attempt_aggregate_mastery = aggregate["mastery"]
        if assessed_nodes == 0:
            overall_mastery = "未评估"
        elif assessed_nodes < total_nodes:
            overall_mastery = "覆盖不足"
        elif all(node["mastery"] == "稳定掌握" for node in nodes):
            overall_mastery = "稳定掌握"
        elif all(
            node["mastery"] in {"基本掌握", "稳定掌握"}
            for node in nodes
        ):
            overall_mastery = "基本掌握"
        elif any(node["mastery"] == "学习中" for node in nodes):
            overall_mastery = "学习中"
        else:
            overall_mastery = "样本不足"
        return {
            **aggregate,
            "scope": "overall",
            "mastery": overall_mastery,
            "attempt_aggregate_mastery": attempt_aggregate_mastery,
            "nodes": nodes,
            "assessed_nodes": assessed_nodes,
            "total_nodes": total_nodes,
            "assessed_node_ratio": assessed_nodes / total_nodes,
            "weighted_mastery_score": weighted_score,
            "mastery_distribution": mastery_distribution,
            "scope_warning": (
                None
                if assessed_nodes == total_nodes
                else "总体分数包含未评估节点，不能用已做题正确率代替整体掌握度"
            ),
        }

    def export_json(self, output: str | Path) -> dict[str, Any]:
        destination = Path(output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                attempt_rows = connection.execute(
                    "SELECT * FROM attempts ORDER BY id"
                ).fetchall()
                attempts = [
                    self._attempt_from_row(connection, row)
                    for row in attempt_rows
                ]
                reviews = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM reviews ORDER BY id"
                    )
                ]
                sessions = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM sessions ORDER BY id"
                    )
                ]
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        document = {
            "schema_version": 2,
            "exported_at": _utc_now(),
            "database": str(self.database),
            "attempts": attempts,
            "reviews": reviews,
            "sessions": sessions,
        }
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                json.dump(
                    document,
                    temporary,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, destination)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return {
            "ok": True,
            "database": str(self.database),
            "object_id": str(destination),
            "output": str(destination),
            "attempt_count": len(attempts),
            "review_count": len(reviews),
            "session_count": len(sessions),
        }
