"""Persistence contract tests for the data-analysis study coach."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".agents/skills/gongkao-data-analysis-coach/scripts"
sys.path.insert(0, str(SCRIPTS))

from coachlib.store import (  # noqa: E402
    ERROR_CODES,
    StudyStore,
    _secure_mkdir_parents,
)


KNOWN_IDS = {"growth.ratio", "abrx.base"}
EXPECTED_ERROR_CODES = {
    "wrong_period",
    "wrong_scope",
    "wrong_indicator",
    "wrong_unit",
    "wrong_wording",
    "wrong_denominator",
    "reversed_rates",
    "missing_cross_term",
    "sign_error",
    "formula_error",
    "arithmetic_error",
    "precision_error",
    "unsafe_estimate",
    "method_mismatch",
    "source_conflict",
    "missing_question",
    "parse_uncertain",
}


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


def create_v2_database(path: Path, target_default: int = 60) -> None:
    """Create a complete ownerless v2 coach database."""

    if target_default not in {45, 60}:
        raise ValueError("test helper supports only known v2 defaults")

    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES('version', '2');
            CREATE TABLE attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                user_answer TEXT,
                correct_answer TEXT,
                is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
                duration_seconds REAL CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
                target_duration_seconds REAL NOT NULL DEFAULT {target_default}
                    CHECK (target_duration_seconds > 0),
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
                traceability TEXT NOT NULL DEFAULT 'high'
                    CHECK (traceability IN ('high', 'low')),
                priority TEXT NOT NULL DEFAULT 'normal'
                    CHECK (priority IN ('normal', 'high')),
                is_transfer INTEGER NOT NULL DEFAULT 0
                    CHECK (is_transfer IN (0, 1)),
                exact_method_json TEXT,
                fast_method_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE attempt_tags (
                attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                knowledge_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (attempt_id, knowledge_id)
            );
            CREATE INDEX attempt_tags_knowledge_idx
                ON attempt_tags(knowledge_id, attempt_id);
            CREATE TABLE attempt_errors (
                attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                error_code TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (attempt_id, error_code)
            );
            CREATE TABLE reviews (
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
            );
            CREATE INDEX reviews_due_idx
                ON reviews(status, due_date, attempt_id);
            CREATE UNIQUE INDEX reviews_one_scheduled_idx
                ON reviews(attempt_id) WHERE status='scheduled';
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                knowledge_id TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                metadata_json TEXT
            );
            """
        )
        for index in range(15):
            cursor = connection.execute(
                """
                INSERT INTO attempts(
                    question, user_answer, correct_answer, is_correct,
                    duration_seconds, method_quality, traceability,
                    is_transfer, created_at
                ) VALUES (?, 'A', 'A', 1, 50, 'optimal', 'high', ?, ?)
                """,
                (f"旧版默认限时题 {index}", int(index >= 12), "2026-08-30T00:00:00+00:00"),
            )
            connection.execute(
                """INSERT INTO attempt_tags(attempt_id, knowledge_id, position)
                    VALUES (?, 'growth.ratio', 0)""",
                (cursor.lastrowid,),
            )


def move_database_artifacts(source: Path, destination: Path) -> None:
    """Move a live SQLite database and any currently named sidecars."""

    source.replace(destination)
    for suffix in ("-wal", "-shm", "-journal"):
        source_sidecar = Path(f"{source}{suffix}")
        if source_sidecar.exists():
            source_sidecar.replace(Path(f"{destination}{suffix}"))


class StudyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "study.sqlite3"
        self.store = StudyStore(self.db, KNOWN_IDS)

    def test_error_code_registry_is_exact_and_importable(self):
        self.assertEqual(ERROR_CODES, frozenset(EXPECTED_ERROR_CODES))

    def test_mapping_weights_with_an_overflowing_total_are_rejected_before_io(self):
        database = Path(self.temp.name) / "overflow-weight" / "study.sqlite3"

        with self.assertRaisesRegex(ValueError, "total|overflow"):
            StudyStore(
                database,
                {"growth.ratio": 1e308, "abrx.base": 1e308},
            )

        self.assertFalse(database.parent.exists())

    def test_typo_error_code_is_rejected_without_partial_record(self):
        with self.assertRaisesRegex(ValueError, "unknown error code"):
            self.store.record_attempt(
                wrong_attempt(errors=["source_conflct"])
            )

        self.assertEqual(self.store.count_attempts(), 0)

    def test_user_and_correct_answers_are_required_nonempty_strings(self):
        cases = (
            ("missing user", {"user_answer": None}),
            ("blank user", {"user_answer": "  "}),
            ("missing correct", {"correct_answer": None}),
            ("blank correct", {"correct_answer": "\n"}),
            ("numeric user", {"user_answer": 3}),
        )

        for label, overrides in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "answer.*non-empty string"):
                    self.store.record_attempt(wrong_attempt(**overrides))
        self.assertEqual(self.store.count_attempts(), 0)

    def test_is_correct_must_match_normalized_answers(self):
        cases = (
            wrong_attempt(
                user_answer=" c ",
                correct_answer="C",
                is_correct=False,
                errors=["formula_error"],
            ),
            wrong_attempt(
                user_answer="b",
                correct_answer=" C ",
                is_correct=True,
                errors=[],
            ),
        )

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "is_correct contradicts"):
                    self.store.record_attempt(payload)
        self.assertEqual(self.store.count_attempts(), 0)

    def test_wrong_attempt_requires_a_root_cause_code(self):
        with self.assertRaisesRegex(ValueError, "wrong attempt.*error code"):
            self.store.record_attempt(wrong_attempt(errors=[]))

        self.assertEqual(self.store.count_attempts(), 0)

    def test_unknown_top_level_field_is_rejected_without_partial_record(self):
        with self.assertRaisesRegex(ValueError, "unknown attempt field"):
            self.store.record_attempt(
                wrong_attempt(typo_duration_seconds=85)
            )

        self.assertEqual(self.store.count_attempts(), 0)

    def test_attempt_payload_can_be_fully_validated_without_opening_a_database(self):
        result = StudyStore.validate_attempt_payload(wrong_attempt(), KNOWN_IDS)

        self.assertTrue(result["ok"])
        self.assertEqual(result["knowledge_ids"], ["growth.ratio"])
        with self.assertRaisesRegex(ValueError, "unknown knowledge"):
            StudyStore.validate_attempt_payload(
                wrong_attempt(knowledge_ids=["not.real"]),
                KNOWN_IDS,
            )

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

    def test_due_review_dto_hides_answer_and_includes_review_context(self):
        receipt = self.store.record_attempt(
            wrong_attempt(
                error_path="我选B，正确答案其实是C",
                correction_rule="正确答案C；分子率大则上升",
            )
        )

        review = self.store.due_reviews(receipt["next_review_date"])[0]

        self.assertEqual(
            set(review),
            {
                "review_id",
                "attempt_id",
                "result",
                "status",
                "due_date",
                "reviewed_on",
                "interval_days",
                "round",
                "interval_qualified",
                "created_at",
                "question",
                "priority",
                "knowledge_ids",
                "errors",
            },
        )
        self.assertNotIn("correct_answer", review)
        self.assertEqual(review["knowledge_ids"], ["growth.ratio"])
        self.assertEqual(review["errors"], ["source_conflict"])
        serialized = json.dumps(review, ensure_ascii=False)
        self.assertNotIn("正确答案", serialized)
        self.assertNotIn("其实是C", serialized)

    def test_existing_empty_or_zero_byte_database_is_never_claimed(self):
        zero_byte = Path(self.temp.name) / "foreign-zero.sqlite3"
        zero_byte.write_bytes(b"")
        empty_sqlite = Path(self.temp.name) / "foreign-empty.sqlite3"
        with sqlite3.connect(empty_sqlite) as connection:
            connection.execute("VACUUM")

        for path in (zero_byte, empty_sqlite):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "empty|foreign|owned"):
                    StudyStore(path, KNOWN_IDS)
                self.assertEqual(path.read_bytes(), before)

    def test_failed_new_database_connect_does_not_poison_the_retry_path(self):
        database = Path(self.temp.name) / "transient-failure.sqlite3"
        with mock.patch(
            "coachlib.store.sqlite3.connect",
            side_effect=sqlite3.OperationalError("injected connect failure"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected"):
                StudyStore(database, KNOWN_IDS)

        self.assertFalse(database.exists())
        recovered = StudyStore(database, KNOWN_IDS)
        self.assertEqual(recovered.initialization_receipt["schema_version"], 3)

    def test_late_foreign_sidecar_is_preserved_during_secure_creation(self):
        database = Path(self.temp.name) / "late-sidecar.sqlite3"
        journal = Path(f"{database}-journal")
        real_fsync = os.fsync
        injected = False

        def inject_sidecar_after_staging_write(descriptor):
            nonlocal injected
            result = real_fsync(descriptor)
            if not injected:
                journal.write_text("FOREIGN-SIDECAR", encoding="utf-8")
                injected = True
            return result

        with mock.patch(
            "coachlib.store.os.fsync",
            side_effect=inject_sidecar_after_staging_write,
        ):
            with self.assertRaisesRegex(ValueError, "sidecar appeared"):
                StudyStore(database, KNOWN_IDS)

        self.assertTrue(injected)
        self.assertEqual(journal.read_text(encoding="utf-8"), "FOREIGN-SIDECAR")
        self.assertFalse(database.exists())

    def test_partial_staging_write_never_publishes_a_database(self):
        database = Path(self.temp.name) / "partial-write.sqlite3"
        real_write = os.write
        write_calls = 0

        def fail_after_partial_write(descriptor, payload):
            nonlocal write_calls
            write_calls += 1
            if write_calls == 1:
                return real_write(descriptor, payload[:100])
            raise OSError("injected staging write failure")

        with mock.patch(
            "coachlib.store.os.write",
            side_effect=fail_after_partial_write,
        ):
            with self.assertRaisesRegex(OSError, "staging write"):
                StudyStore(database, KNOWN_IDS)

        self.assertFalse(database.exists())
        recovered = StudyStore(database, KNOWN_IDS)
        self.assertEqual(recovered.initialization_receipt["schema_version"], 3)

    def test_atomic_publication_never_overwrites_a_late_destination(self):
        database = Path(self.temp.name) / "late-destination.sqlite3"
        foreign_bytes = b"FOREIGN-DATABASE-MUST-SURVIVE"

        def inject_destination_before_rename(
            source_name,
            destination_name,
            *,
            directory_fd,
        ):
            descriptor = os.open(
                destination_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.write(descriptor, foreign_bytes)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            from coachlib import store as store_module

            return store_module._system_rename_noreplace(
                source_name,
                destination_name,
                directory_fd=directory_fd,
            )

        with mock.patch(
            "coachlib.store._rename_noreplace",
            side_effect=inject_destination_before_rename,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "appeared during secure publication",
            ):
                StudyStore(database, KNOWN_IDS)

        self.assertEqual(database.read_bytes(), foreign_bytes)

    def test_database_with_a_second_hardlink_is_never_reopened(self):
        alias = Path(self.temp.name) / "database-alias.sqlite3"
        os.link(self.db, alias)

        with self.assertRaisesRegex(ValueError, "hard link|link count"):
            StudyStore(self.db, KNOWN_IDS)
        with self.assertRaisesRegex(ValueError, "hard link|link count"):
            StudyStore.open_readonly(self.db, KNOWN_IDS)
        with self.assertRaisesRegex(ValueError, "hard link|link count"):
            self.store.count_attempts()

    def test_writable_open_never_mutates_linked_or_symlinked_sidecars(self):
        for suffix in ("-wal", "-shm", "-journal"):
            for link_kind in ("hardlink", "symlink"):
                with self.subTest(suffix=suffix, link_kind=link_kind):
                    database = (
                        Path(self.temp.name)
                        / f"unsafe-sidecar-{suffix[1:]}-{link_kind}.sqlite3"
                    )
                    StudyStore(database, KNOWN_IDS)
                    sidecar = Path(f"{database}{suffix}")
                    sidecar.unlink(missing_ok=True)
                    victim = database.with_name(
                        f"victim-{suffix[1:]}-{link_kind}.bin"
                    )
                    victim_bytes = b"V" * 65536
                    victim.write_bytes(victim_bytes)
                    if link_kind == "hardlink":
                        os.link(victim, sidecar)
                    else:
                        sidecar.symlink_to(victim)

                    with self.assertRaisesRegex(
                        ValueError,
                        "sidecar|hard link|symbolic|regular",
                    ):
                        StudyStore(database, KNOWN_IDS)

                    self.assertEqual(victim.read_bytes(), victim_bytes)

    def test_sidecar_swap_after_connect_never_mutates_the_victim(self):
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                database = (
                    Path(self.temp.name)
                    / f"post-connect-swap-{suffix[1:]}.sqlite3"
                )
                store = StudyStore(database, KNOWN_IDS)
                victim = database.with_name(f"post-connect-victim{suffix}")
                victim_bytes = b"V" * 65536
                victim.write_bytes(victim_bytes)
                original_connect = StudyStore._connect
                injected = False

                def connect_then_swap(instance, *args, **kwargs):
                    nonlocal injected
                    connection = original_connect(instance, *args, **kwargs)
                    if not injected and kwargs.get("readonly_artifacts") is None:
                        sidecar = Path(f"{database}{suffix}")
                        sidecar.unlink(missing_ok=True)
                        os.link(victim, sidecar)
                        injected = True
                    return connection

                with mock.patch.object(
                    StudyStore,
                    "_connect",
                    new=connect_then_swap,
                ):
                    try:
                        store.record_attempt(wrong_attempt())
                    except (ValueError, sqlite3.Error):
                        pass

                self.assertTrue(injected)
                self.assertEqual(victim.read_bytes(), victim_bytes)

    def test_hardlink_added_at_publication_is_rejected_and_not_reopened(self):
        database = Path(self.temp.name) / "raced-hardlink.sqlite3"
        alias_name = "attacker-database-alias.sqlite3"
        from coachlib import store as store_module

        def add_alias_then_publish(
            source_name,
            destination_name,
            *,
            directory_fd,
        ):
            os.link(
                source_name,
                alias_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            return store_module._system_rename_noreplace(
                source_name,
                destination_name,
                directory_fd=directory_fd,
            )

        with mock.patch(
            "coachlib.store._rename_noreplace",
            side_effect=add_alias_then_publish,
        ):
            with self.assertRaisesRegex(ValueError, "link count"):
                StudyStore(database, KNOWN_IDS)

        self.assertTrue(database.exists())
        self.assertTrue((database.parent / alias_name).exists())
        with self.assertRaisesRegex(ValueError, "hard link|link count"):
            StudyStore(database, KNOWN_IDS)

    def test_sidecar_added_during_publication_causes_initialization_failure(self):
        database = Path(self.temp.name) / "raced-sidecar.sqlite3"
        from coachlib import store as store_module

        def publish_then_add_sidecar(
            source_name,
            destination_name,
            *,
            directory_fd,
        ):
            store_module._system_rename_noreplace(
                source_name,
                destination_name,
                directory_fd=directory_fd,
            )
            descriptor = os.open(
                f"{destination_name}-wal",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.write(descriptor, b"FOREIGN-WAL")
            finally:
                os.close(descriptor)

        with mock.patch(
            "coachlib.store._rename_noreplace",
            side_effect=publish_then_add_sidecar,
        ):
            with self.assertRaisesRegex(ValueError, "sidecar.*publication"):
                StudyStore(database, KNOWN_IDS)

        self.assertEqual(Path(f"{database}-wal").read_bytes(), b"FOREIGN-WAL")

    def test_parent_symlink_swap_before_connect_cannot_create_outside_database(self):
        parent = Path(self.temp.name) / "bound-parent"
        parent.mkdir()
        database = parent / "study.sqlite3"
        resolved_database = database.resolve()
        original_parent = Path(self.temp.name) / "bound-parent-original"
        outside = Path(self.temp.name) / "outside-target"
        outside.mkdir()
        original_connect = StudyStore._connect
        swapped = False

        def swap_parent_before_connect(store, *args, **kwargs):
            nonlocal swapped
            if store.database == resolved_database and not swapped:
                parent.rename(original_parent)
                parent.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_connect(store, *args, **kwargs)

        with mock.patch.object(
            StudyStore,
            "_connect",
            new=swap_parent_before_connect,
        ):
            with self.assertRaisesRegex(ValueError, "parent|path|replaced|binding"):
                StudyStore(database, KNOWN_IDS)

        self.assertTrue(swapped)
        self.assertFalse((outside / database.name).exists())

    def test_bound_parent_fd_is_closed_when_identity_read_fails(self):
        opened = []
        real_open = os.open

        def track_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        with mock.patch("coachlib.store.os.open", side_effect=track_open), mock.patch(
            "coachlib.store.os.fstat",
            side_effect=OSError("injected fstat failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected"):
                self.store._open_bound_parent_directory()

        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])

    def test_secure_parent_creation_closes_child_fd_when_fstat_fails(self):
        target = Path(self.temp.name).resolve() / "new-parent" / "child"
        opened = []
        fstat_calls = 0
        real_open = os.open
        real_fstat = os.fstat

        def track_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        def fail_child_fstat(descriptor):
            nonlocal fstat_calls
            fstat_calls += 1
            if fstat_calls == 2:
                raise OSError("injected child fstat failure")
            return real_fstat(descriptor)

        with mock.patch(
            "coachlib.store.os.open",
            side_effect=track_open,
        ), mock.patch(
            "coachlib.store.os.fstat",
            side_effect=fail_child_fstat,
        ):
            with self.assertRaisesRegex(OSError, "child fstat"):
                _secure_mkdir_parents(target)

        self.assertEqual(len(opened), 2)
        for descriptor in opened:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_readonly_connection_is_closed_when_post_setup_pragma_fails(self):
        class FailingConnection:
            def __init__(self):
                self.closed = False
                self.execute_calls = 0
                self.row_factory = None

            def execute(self, _statement):
                self.execute_calls += 1
                if self.execute_calls == 2:
                    raise RuntimeError("injected foreign_keys failure")
                return self

            def close(self):
                self.closed = True

        connection = FailingConnection()
        with mock.patch(
            "coachlib.store.sqlite3.connect",
            return_value=connection,
        ):
            with self.assertRaisesRegex(RuntimeError, "foreign_keys"):
                self.store._connect(readonly_artifacts={"": b""})

        self.assertTrue(connection.closed)

    def test_parent_swap_after_validation_cannot_make_sqlite_create_outside(self):
        parent = Path(self.temp.name) / "validated-parent"
        parent.mkdir()
        database = parent / "study.sqlite3"
        resolved_database = database.resolve()
        original_parent = Path(self.temp.name) / "validated-parent-original"
        outside = Path(self.temp.name) / "validated-outside"
        outside.mkdir()
        original_validate = StudyStore._validate_database_parent_binding
        swapped = False

        def validate_then_swap(store):
            nonlocal swapped
            original_validate(store)
            if store.database == resolved_database and not swapped:
                parent.rename(original_parent)
                parent.symlink_to(outside, target_is_directory=True)
                swapped = True

        with mock.patch.object(
            StudyStore,
            "_validate_database_parent_binding",
            new=validate_then_swap,
        ):
            with self.assertRaises((ValueError, sqlite3.Error)):
                StudyStore(database, KNOWN_IDS)

        self.assertTrue(swapped)
        self.assertFalse((outside / database.name).exists())

    def test_late_parent_symlink_cannot_create_directories_outside(self):
        inside = Path(self.temp.name) / "inside-root"
        inside.mkdir()
        late_link = inside / "late-link"
        database = late_link / "created-parent" / "study.sqlite3"
        outside = Path(self.temp.name) / "outside-directory-target"
        outside.mkdir()
        late_link.symlink_to(outside, target_is_directory=True)

        with self.assertRaises((ValueError, OSError)):
            StudyStore(
                database,
                KNOWN_IDS,
                path_already_resolved=True,
            )

        self.assertFalse((outside / "created-parent").exists())
        self.assertFalse((outside / "created-parent" / database.name).exists())

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
        self.assertEqual(saved["target_duration_seconds"], 60.0)
        self.assertEqual(saved["target_duration_source"], "explicit")
        self.assertEqual(saved["effective_target_duration_seconds"], 60.0)
        self.assertEqual(saved["priority"], "high")
        self.assertEqual(saved["correction_rule"], "比值趋势只比较分子、分母增速")

    def test_new_schema_and_omitted_attempt_target_default_to_45_seconds(self):
        receipt = self.store.record_attempt(wrong_attempt())

        saved = self.store.get_attempt(receipt["attempt_id"])
        with sqlite3.connect(self.db) as connection:
            columns = {
                row[1]: row
                for row in connection.execute("PRAGMA table_info(attempts)")
            }

        self.assertEqual(saved["target_duration_seconds"], 45.0)
        self.assertEqual(saved["target_duration_source"], "default")
        self.assertEqual(saved["effective_target_duration_seconds"], 45.0)
        self.assertEqual(float(columns["target_duration_seconds"][4]), 45.0)

    def test_v2_default_60_targets_migrate_conservatively_to_effective_45(self):
        legacy_db = Path(self.temp.name) / "legacy-v2.sqlite3"
        create_v2_database(legacy_db)

        migrated = StudyStore(legacy_db, KNOWN_IDS)
        saved = migrated.get_attempt(1)
        result = migrated.progress("growth.ratio")

        self.assertEqual(migrated.initialization_receipt["schema_version"], 3)
        self.assertEqual(saved["target_duration_seconds"], 60.0)
        self.assertEqual(saved["target_duration_source"], "legacy_unknown")
        self.assertEqual(saved["effective_target_duration_seconds"], 45.0)
        self.assertEqual(result["target_duration_seconds"], 45.0)
        self.assertEqual(result["speed"]["target_source_counts"], {"legacy_unknown": 15})
        self.assertFalse(result["speed_met"])

    def test_migrated_v3_default_insert_still_uses_effective_45_seconds(self):
        legacy_db = Path(self.temp.name) / "legacy-default-insert.sqlite3"
        create_v2_database(legacy_db, target_default=60)
        migrated = StudyStore(legacy_db, KNOWN_IDS)
        with sqlite3.connect(legacy_db) as connection:
            cursor = connection.execute(
                """INSERT INTO attempts(
                    question, user_answer, correct_answer, is_correct,
                    duration_seconds, method_quality, created_at
                ) VALUES ('direct default', 'A', 'A', 1, 50,
                          'optimal', '2026-08-30T00:00:00+00:00')"""
            )
            attempt_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO attempt_tags(attempt_id, knowledge_id, position)
                    VALUES (?, 'growth.ratio', 0)""",
                (attempt_id,),
            )

        saved = migrated.get_attempt(attempt_id)
        result = migrated.progress("growth.ratio")

        self.assertEqual(saved["target_duration_seconds"], 60.0)
        self.assertEqual(saved["target_duration_source"], "default")
        self.assertEqual(saved["effective_target_duration_seconds"], 45.0)
        self.assertEqual(result["target_duration_seconds"], 45.0)

    def test_v2_nondefault_targets_remain_explicit_during_migration(self):
        legacy_db = Path(self.temp.name) / "legacy-v2-explicit.sqlite3"
        create_v2_database(legacy_db)
        with sqlite3.connect(legacy_db) as connection:
            connection.execute(
                "UPDATE attempts SET target_duration_seconds=55 WHERE id=1"
            )

        migrated = StudyStore(legacy_db, KNOWN_IDS)

        explicit = migrated.get_attempt(1)
        unknown = migrated.get_attempt(2)
        self.assertEqual(explicit["target_duration_source"], "explicit")
        self.assertEqual(explicit["effective_target_duration_seconds"], 55.0)
        self.assertEqual(unknown["target_duration_source"], "legacy_unknown")
        self.assertEqual(unknown["effective_target_duration_seconds"], 45.0)

    def test_v2_45_default_is_distinguished_from_explicit_target(self):
        legacy_db = Path(self.temp.name) / "legacy-v2-default-45.sqlite3"
        create_v2_database(legacy_db, target_default=45)
        with sqlite3.connect(legacy_db) as connection:
            connection.execute(
                "UPDATE attempts SET target_duration_seconds=60 WHERE id=1"
            )

        migrated = StudyStore(legacy_db, KNOWN_IDS)

        explicit = migrated.get_attempt(1)
        defaulted = migrated.get_attempt(2)
        self.assertEqual(explicit["target_duration_source"], "explicit")
        self.assertEqual(explicit["effective_target_duration_seconds"], 60.0)
        self.assertEqual(defaulted["target_duration_source"], "default")
        self.assertEqual(defaulted["effective_target_duration_seconds"], 45.0)

    def test_new_database_has_explicit_owner_marker_and_schema_v3(self):
        with sqlite3.connect(self.db) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM schema_meta"))

        self.assertEqual(metadata["owner"], "gongkao-data-analysis-coach")
        self.assertEqual(metadata["version"], "3")
        self.assertRegex(metadata["instance_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(
            list(self.db.parent.glob(".gongkao-init-*.sqlite3")),
            [],
        )
        self.assertEqual(os.stat(self.db).st_nlink, 1)
        self.assertNotIn(
            "initialization_alias",
            self.store.initialization_receipt,
        )

    def test_schema_comparison_preserves_quoted_literal_case_and_spaces(self):
        cases = (
            ("attempts", "'optimal'", "'OPTIMAL'"),
            ("attempts", "'optimal'", "'opti mal'"),
            ("reviews_one_scheduled_idx", "'scheduled'", "'SCHEDULED'"),
        )
        for index, (object_name, original, replacement) in enumerate(cases):
            with self.subTest(object_name=object_name, replacement=replacement):
                database = Path(self.temp.name) / f"quoted-{index}.sqlite3"
                StudyStore(database, KNOWN_IDS)
                with sqlite3.connect(database) as connection:
                    schema_version = int(
                        connection.execute("PRAGMA schema_version").fetchone()[0]
                    )
                    connection.execute("PRAGMA writable_schema=ON")
                    connection.execute(
                        """UPDATE sqlite_master SET sql=replace(sql, ?, ?)
                           WHERE name=?""",
                        (original, replacement, object_name),
                    )
                    connection.execute("PRAGMA writable_schema=OFF")
                    connection.execute(f"PRAGMA schema_version={schema_version + 1}")

                with self.assertRaisesRegex(ValueError, "definition"):
                    StudyStore.open_readonly(database, KNOWN_IDS)

    def test_unrelated_sqlite_database_is_rejected_without_modification(self):
        unrelated = Path(self.temp.name) / "application.sqlite3"
        with sqlite3.connect(unrelated) as connection:
            connection.execute(
                "CREATE TABLE customer_orders(id INTEGER PRIMARY KEY, note TEXT)"
            )
            connection.execute(
                "INSERT INTO customer_orders(note) VALUES ('must survive')"
            )
            journal_before = connection.execute("PRAGMA journal_mode").fetchone()[0]
        bytes_before = unrelated.read_bytes()

        with self.assertRaisesRegex(ValueError, "not a coach database"):
            StudyStore(unrelated, KNOWN_IDS)

        self.assertEqual(unrelated.read_bytes(), bytes_before)
        with sqlite3.connect(unrelated) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            journal_after = connection.execute("PRAGMA journal_mode").fetchone()[0]
            note = connection.execute("SELECT note FROM customer_orders").fetchone()[0]
        self.assertEqual(tables, {"customer_orders"})
        self.assertEqual(note, "must survive")
        self.assertEqual(journal_after, journal_before)

    def test_unrelated_active_wal_database_rejection_preserves_all_bytes(self):
        unrelated = Path(self.temp.name) / "active-application.sqlite3"
        connection = sqlite3.connect(unrelated)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE customer_orders(id INTEGER PRIMARY KEY, note TEXT)"
            )
            connection.execute(
                "INSERT INTO customer_orders(note) VALUES ('must survive')"
            )
            connection.commit()
            artifacts = tuple(
                path
                for path in (
                    unrelated,
                    Path(f"{unrelated}-wal"),
                    Path(f"{unrelated}-shm"),
                )
                if path.exists()
            )
            self.assertEqual(len(artifacts), 3)
            before = {artifact.name: artifact.read_bytes() for artifact in artifacts}

            with self.assertRaisesRegex(ValueError, "not a coach database"):
                StudyStore(unrelated, KNOWN_IDS)

            after = {artifact.name: artifact.read_bytes() for artifact in artifacts}
            self.assertEqual(after, before)
        finally:
            connection.close()

    def test_damaged_legacy_shape_is_rejected_without_schema_stamp(self):
        damaged = Path(self.temp.name) / "damaged.sqlite3"
        with sqlite3.connect(damaged) as connection:
            connection.executescript(
                """
                CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO schema_meta(key, value) VALUES('version', '2');
                CREATE TABLE attempts(id INTEGER PRIMARY KEY);
                """
            )
        bytes_before = damaged.read_bytes()

        with self.assertRaisesRegex(ValueError, "incompatible coach database"):
            StudyStore(damaged, KNOWN_IDS)

        self.assertEqual(damaged.read_bytes(), bytes_before)
        with sqlite3.connect(damaged) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()[0]
        self.assertEqual(tables, {"schema_meta", "attempts"})
        self.assertEqual(version, "2")

    def test_v2_without_metadata_primary_key_is_rejected_before_mutation(self):
        damaged = Path(self.temp.name) / "v2-no-meta-pk.sqlite3"
        create_v2_database(damaged)
        with sqlite3.connect(damaged) as connection:
            connection.executescript(
                """
                ALTER TABLE schema_meta RENAME TO old_schema_meta;
                CREATE TABLE schema_meta(key TEXT, value TEXT NOT NULL);
                INSERT INTO schema_meta SELECT key, value FROM old_schema_meta;
                DROP TABLE old_schema_meta;
                """
            )
            journal_before = connection.execute("PRAGMA journal_mode").fetchone()[0]
        before = damaged.read_bytes()

        with self.assertRaisesRegex(ValueError, "schema|primary key"):
            StudyStore(damaged, KNOWN_IDS)

        self.assertEqual(damaged.read_bytes(), before)
        with sqlite3.connect(damaged) as connection:
            journal_after = connection.execute("PRAGMA journal_mode").fetchone()[0]
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()[0]
        self.assertEqual(journal_after, journal_before)
        self.assertEqual(version, "2")

    def test_v2_with_trigger_is_rejected_without_executing_trigger(self):
        damaged = Path(self.temp.name) / "v2-trigger.sqlite3"
        create_v2_database(damaged)
        with sqlite3.connect(damaged) as connection:
            connection.execute(
                """CREATE TRIGGER erase_on_claim AFTER INSERT ON schema_meta
                    WHEN NEW.key='owner' BEGIN DELETE FROM attempts; END"""
            )
        before = damaged.read_bytes()

        with self.assertRaisesRegex(ValueError, "trigger|schema object"):
            StudyStore(damaged, KNOWN_IDS)

        self.assertEqual(damaged.read_bytes(), before)
        with sqlite3.connect(damaged) as connection:
            count = connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        self.assertEqual(count, 15)

    def test_v3_missing_required_index_is_rejected_readonly(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("DROP INDEX reviews_one_scheduled_idx")

        with self.assertRaisesRegex(ValueError, "index|schema object"):
            StudyStore.open_readonly(self.db, KNOWN_IDS)

    def test_v3_rejects_broadened_partial_unique_index_predicate(self):
        with sqlite3.connect(self.db) as connection:
            connection.executescript(
                """
                DROP INDEX reviews_one_scheduled_idx;
                CREATE UNIQUE INDEX reviews_one_scheduled_idx
                    ON reviews(attempt_id)
                    WHERE status='scheduled' OR status='completed';
                """
            )

        with self.assertRaisesRegex(ValueError, "index.*definition"):
            StudyStore.open_readonly(self.db, KNOWN_IDS)

    def test_wrong_owner_marker_is_rejected_without_repairing_it(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE schema_meta SET value='another-application' WHERE key='owner'"
            )

        with self.assertRaisesRegex(ValueError, "owner marker"):
            StudyStore(self.db, KNOWN_IDS)

    def test_validated_writable_store_rejects_database_path_replacement(self):
        replacement = Path(self.temp.name) / "replacement.sqlite3"
        StudyStore(replacement, KNOWN_IDS)
        with sqlite3.connect(replacement) as connection:
            connection.execute(
                "UPDATE schema_meta SET value='another-application' "
                "WHERE key='owner'"
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{self.db}{suffix}").unlink(missing_ok=True)
        replacement.replace(self.db)
        before = self.db.read_bytes()

        with self.assertRaisesRegex(ValueError, "replaced|identity"):
            self.store.record_attempt(wrong_attempt())

        self.assertEqual(self.db.read_bytes(), before)
        with sqlite3.connect(self.db) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM schema_meta"))
            count = connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        self.assertEqual(metadata["owner"], "another-application")
        self.assertEqual(count, 0)

        with sqlite3.connect(self.db) as connection:
            owner = connection.execute(
                "SELECT value FROM schema_meta WHERE key='owner'"
            ).fetchone()[0]
        self.assertEqual(owner, "another-application")

    def test_instance_marker_rejects_replacement_by_another_valid_coach_database(self):
        original_instance = self.store.initialization_receipt["database_instance_id"]
        replacement = Path(self.temp.name) / "valid-replacement.sqlite3"
        replacement_store = StudyStore(replacement, KNOWN_IDS)
        self.assertNotEqual(
            original_instance,
            replacement_store.initialization_receipt["database_instance_id"],
        )
        with sqlite3.connect(replacement) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{self.db}{suffix}").unlink(missing_ok=True)
        replacement.replace(self.db)

        # Simulate the narrow stat-to-connect race by making the path identity
        # check observe the replacement as if it were the validated inode.
        self.store._database_identity = self.store._current_database_identity()
        before = self.db.read_bytes()

        with self.assertRaisesRegex(ValueError, "instance|replaced"):
            self.store.record_attempt(wrong_attempt())

        self.assertEqual(self.db.read_bytes(), before)
        with sqlite3.connect(self.db) as connection:
            count = connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        self.assertEqual(count, 0)

    def test_record_never_reports_success_if_path_changes_after_transaction_check(self):
        replacement = Path(self.temp.name) / "record-window-replacement.sqlite3"
        StudyStore(replacement, KNOWN_IDS)
        with sqlite3.connect(replacement) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
        backup = Path(self.temp.name) / "record-window-backup.sqlite3"
        original_validate = self.store._validate_writable_connection
        calls = 0

        def swap_after_second_validation(connection):
            nonlocal calls
            original_validate(connection)
            calls += 1
            if calls == 2:
                move_database_artifacts(self.db, backup)
                move_database_artifacts(replacement, self.db)

        with mock.patch.object(
            self.store,
            "_validate_writable_connection",
            side_effect=swap_after_second_validation,
        ):
            with self.assertRaisesRegex(ValueError, "replaced|binding|identity"):
                self.store.record_attempt(wrong_attempt())

        with sqlite3.connect(self.db) as connection:
            active_count = connection.execute(
                "SELECT COUNT(*) FROM attempts"
            ).fetchone()[0]
        with sqlite3.connect(backup) as connection:
            backup_count = connection.execute(
                "SELECT COUNT(*) FROM attempts"
            ).fetchone()[0]
        self.assertEqual(active_count, 0)
        self.assertEqual(backup_count, 0)

    def test_initialize_never_reports_success_if_path_changes_after_final_check(self):
        replacement = Path(self.temp.name) / "init-window-replacement.sqlite3"
        StudyStore(replacement, KNOWN_IDS)
        for database in (self.db, replacement):
            with sqlite3.connect(database) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("PRAGMA journal_mode=DELETE")
        backup = Path(self.temp.name) / "init-window-backup.sqlite3"
        original_inspect = StudyStore._inspect_existing_database
        calls = 0

        def swap_after_third_inspection(store, connection):
            nonlocal calls
            result = original_inspect(store, connection)
            if store.database == self.db.resolve():
                calls += 1
                if calls == 3:
                    move_database_artifacts(self.db, backup)
                    move_database_artifacts(replacement, self.db)
            return result

        with mock.patch.object(
            StudyStore,
            "_inspect_existing_database",
            new=swap_after_third_inspection,
        ):
            with self.assertRaisesRegex(ValueError, "replaced|binding|identity"):
                StudyStore(self.db, KNOWN_IDS)

        with sqlite3.connect(self.db) as connection:
            active_instance = connection.execute(
                "SELECT value FROM schema_meta WHERE key='instance_id'"
            ).fetchone()[0]
        with sqlite3.connect(backup) as connection:
            backup_instance = connection.execute(
                "SELECT value FROM schema_meta WHERE key='instance_id'"
            ).fetchone()[0]
        self.assertNotEqual(active_instance, backup_instance)

    def test_empty_progress_uses_45_second_target(self):
        result = self.store.progress("abrx.base")

        self.assertEqual(result["target_duration_seconds"], 45.0)
        self.assertEqual(result["speed"]["target_seconds"], 45.0)

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

    def test_only_primary_tag_counts_as_node_mastery_evidence(self):
        receipt = self.store.record_attempt(
            wrong_attempt(knowledge_ids=["growth.ratio", "abrx.base"])
        )

        primary = self.store.progress("growth.ratio")
        secondary = self.store.progress("abrx.base")
        overall = self.store.progress()

        self.assertEqual(primary["total"], 1)
        self.assertEqual(primary["correct"], 0)
        self.assertEqual(secondary["total"], 0)
        self.assertEqual(secondary["mastery"], "未评估")
        self.assertEqual(overall["assessed_nodes"], 1)
        self.assertEqual(overall["assessed_node_ratio"], 0.5)
        self.assertEqual(
            self.store.list_mistakes("abrx.base")[0]["id"],
            receipt["attempt_id"],
        )

    def test_unreliable_attempts_remain_searchable_but_do_not_count_as_progress(self):
        unreliable_receipts = [
            self.store.record_attempt(
                wrong_attempt(
                    question="题干完整但来源无法追溯",
                    traceability="low",
                    duration_seconds=5,
                    is_transfer=True,
                    errors=["source_conflict"],
                )
            ),
            self.store.record_attempt(
                wrong_attempt(
                    question="OCR 置信度不足的题目",
                    traceability="high",
                    parse_confidence=0.89,
                    duration_seconds=10,
                    is_transfer=True,
                    errors=["source_conflict"],
                )
            ),
            self.store.record_attempt(
                wrong_attempt(
                    question="显式标记解析不确定的题目",
                    traceability="high",
                    parse_confidence=0.95,
                    duration_seconds=15,
                    is_transfer=True,
                    errors=["parse_uncertain"],
                )
            ),
            self.store.record_attempt(
                wrong_attempt(
                    question="可读取但缺失关键题干的记录",
                    traceability="high",
                    parse_confidence=0.95,
                    duration_seconds=20,
                    is_transfer=True,
                    errors=["missing_question"],
                )
            ),
        ]
        reliable_receipt = self.store.record_attempt(
            wrong_attempt(
                question="可追溯且解析可靠的题目",
                user_answer="C",
                correct_answer="C",
                is_correct=True,
                confidence=4,
                errors=[],
                method_quality="optimal",
                traceability="high",
                parse_confidence=0.90,
            )
        )

        saved_ids = {
            self.store.get_attempt(receipt["attempt_id"])["id"]
            for receipt in unreliable_receipts
        }
        mistake_ids = {
            attempt["id"] for attempt in self.store.list_mistakes("growth.ratio")
        }
        due_ids = {
            review["attempt_id"]
            for review in self.store.due_reviews(
                on_date=(date.today() + timedelta(days=1)).isoformat()
            )
        }
        node = self.store.progress("growth.ratio")
        overall = self.store.progress()

        self.assertEqual(
            saved_ids,
            {receipt["attempt_id"] for receipt in unreliable_receipts},
        )
        self.assertEqual(mistake_ids, saved_ids)
        self.assertEqual(due_ids, saved_ids)
        self.assertNotIn(reliable_receipt["attempt_id"], mistake_ids)
        self.assertEqual(node["total"], 1)
        self.assertEqual(node["correct"], 1)
        self.assertEqual(node["speed"]["median_seconds"], 85.0)
        self.assertEqual(node["transfer"]["total"], 0)
        self.assertEqual(overall["total"], 1)
        self.assertEqual(overall["correct"], 1)
        self.assertEqual(overall["assessed_nodes"], 1)
        self.assertEqual(overall["assessed_node_ratio"], 0.5)

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
            connection.commit()
            journal_before = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]

        with self.assertRaisesRegex(ValueError, "newer schema version"):
            StudyStore(self.db, KNOWN_IDS)
        with sqlite3.connect(self.db) as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()[0]
            journal_after = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(version, "999")
        self.assertEqual(journal_after, journal_before)

    def test_readonly_open_does_not_create_a_missing_database_or_parent(self):
        missing = Path(self.temp.name) / "missing-parent" / "study.sqlite3"

        with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
            StudyStore.open_readonly(missing, KNOWN_IDS)

        self.assertFalse(missing.parent.exists())

    def test_readonly_open_rejects_a_database_symlink_inserted_after_validation(self):
        outside = Path(self.temp.name) / "outside-valid.sqlite3"
        StudyStore(outside, KNOWN_IDS)
        alias_parent = Path(self.temp.name) / "readonly-alias-parent"
        alias_parent.mkdir()
        alias = alias_parent / "study.sqlite3"
        alias.symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "regular file|symbolic"):
            StudyStore.open_readonly(
                alias_parent.resolve() / alias.name,
                KNOWN_IDS,
                path_already_resolved=True,
            )

    def test_uninitialized_writable_store_mode_is_rejected(self):
        missing = Path(self.temp.name) / "unvalidated" / "study.sqlite3"

        with self.assertRaisesRegex(ValueError, "initialize or use read-only"):
            StudyStore(missing, KNOWN_IDS, initialize=False)

        self.assertFalse(missing.parent.exists())

    def test_readonly_open_and_queries_leave_database_bytes_unchanged(self):
        receipt = self.store.record_attempt(wrong_attempt())
        before = {
            item.name: item.read_bytes()
            for item in self.db.parent.iterdir()
            if item.is_file()
        }

        readonly = StudyStore.open_readonly(self.db, KNOWN_IDS)
        saved = readonly.get_attempt(receipt["attempt_id"])
        progress = readonly.progress("growth.ratio")
        due = readonly.due_reviews(receipt["next_review_date"])

        after = {
            item.name: item.read_bytes()
            for item in self.db.parent.iterdir()
            if item.is_file()
        }
        self.assertIsNone(readonly.initialization_receipt)
        self.assertEqual(saved["id"], receipt["attempt_id"])
        self.assertEqual(progress["total"], 1)
        self.assertEqual(due[0]["attempt_id"], receipt["attempt_id"])
        self.assertEqual(after, before)

    def test_private_read_snapshots_are_deserialized_without_temp_files(self):
        receipt = self.store.record_attempt(wrong_attempt())
        original_connect = StudyStore._connect
        observed_artifact_sets = []
        before = set(self.db.parent.iterdir())

        def track_snapshot(store, *, readonly_artifacts=None):
            if readonly_artifacts is not None:
                observed_artifact_sets.append(frozenset(readonly_artifacts))
            return original_connect(
                store,
                readonly_artifacts=readonly_artifacts,
            )

        with mock.patch.object(
            StudyStore,
            "_connect",
            new=track_snapshot,
        ):
            readonly = StudyStore.open_readonly(self.db, KNOWN_IDS)
            self.assertEqual(
                readonly.get_attempt(receipt["attempt_id"])["id"],
                receipt["attempt_id"],
            )

        self.assertTrue(observed_artifact_sets)
        self.assertTrue(all("" in names for names in observed_artifact_sets))
        self.assertTrue(
            all("-shm" not in names for names in observed_artifact_sets)
        )
        self.assertEqual(set(self.db.parent.iterdir()), before)
        self.assertEqual(
            list(self.db.parent.glob(".gongkao-readonly-*")),
            [],
        )

    def test_readonly_snapshot_path_cannot_be_redirected_to_another_database(self):
        original = self.store.record_attempt(
            wrong_attempt(question="ORIGINAL", duration_seconds=12)
        )
        outside = Path(self.temp.name) / "outside.sqlite3"
        attacker = StudyStore(outside, KNOWN_IDS)
        attacker.record_attempt(
            wrong_attempt(question="ATTACKER", duration_seconds=99)
        )
        sqlite_connect = sqlite3.connect

        def redirect_named_snapshot(database, *args, **kwargs):
            if ".gongkao-readonly-" in str(database):
                return sqlite_connect(outside)
            return sqlite_connect(database, *args, **kwargs)

        with mock.patch(
            "coachlib.store.sqlite3.connect",
            side_effect=redirect_named_snapshot,
        ):
            readonly = StudyStore.open_readonly(self.db, KNOWN_IDS)
            saved = readonly.get_attempt(original["attempt_id"])

        self.assertEqual(saved["question"], "ORIGINAL")
        self.assertEqual(saved["duration_seconds"], 12.0)

    def test_readonly_open_requires_explicit_migration_for_v2(self):
        legacy = Path(self.temp.name) / "readonly-v2.sqlite3"
        create_v2_database(legacy)
        bytes_before = legacy.read_bytes()

        with self.assertRaisesRegex(ValueError, "run init to migrate"):
            StudyStore.open_readonly(legacy, KNOWN_IDS)

        self.assertEqual(legacy.read_bytes(), bytes_before)

    def test_readonly_open_reads_an_active_wal_snapshot_without_database_writes(self):
        receipt = self.store.record_attempt(
            wrong_attempt(duration_seconds=12)
        )
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "UPDATE attempts SET duration_seconds=37 WHERE id=?",
                (receipt["attempt_id"],),
            )
            connection.commit()
            wal = Path(f"{self.db}-wal")
            shm = Path(f"{self.db}-shm")
            self.assertTrue(wal.exists())
            self.assertTrue(shm.exists())
            artifacts = (self.db, wal, shm)
            before = {artifact.name: artifact.read_bytes() for artifact in artifacts}

            readonly = StudyStore.open_readonly(self.db, KNOWN_IDS)
            saved = readonly.get_attempt(receipt["attempt_id"])

            self.assertEqual(saved["duration_seconds"], 37.0)
            after = {artifact.name: artifact.read_bytes() for artifact in artifacts}
            self.assertEqual(after, before)
        finally:
            connection.close()

    def test_writable_reopen_accepts_safe_single_link_active_wal_sidecars(self):
        receipt = self.store.record_attempt(wrong_attempt(duration_seconds=12))
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "UPDATE attempts SET duration_seconds=37 WHERE id=?",
                (receipt["attempt_id"],),
            )
            connection.commit()
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self.db}{suffix}")
                self.assertTrue(sidecar.exists())
                self.assertEqual(os.stat(sidecar).st_nlink, 1)

            reopened = StudyStore(self.db, KNOWN_IDS)

            self.assertEqual(
                reopened.get_attempt(receipt["attempt_id"])["duration_seconds"],
                37.0,
            )
        finally:
            connection.close()

    def test_wal_materialization_uses_only_the_last_valid_commit(self):
        anchor = sqlite3.connect(self.db)
        try:
            anchor.execute("PRAGMA journal_mode=WAL")
            self.store.record_attempt(wrong_attempt(question="first commit"))
            anchor.execute("BEGIN")
            self.assertEqual(
                anchor.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
                1,
            )
            first = self.store._capture_database_artifacts(
                self.store._current_database_identity()
            )
            self.store.record_attempt(wrong_attempt(question="second commit"))
            second = self.store._capture_database_artifacts(
                self.store._current_database_identity()
            )
            self.assertGreater(len(second["-wal"]), len(first["-wal"]))

            def questions(artifacts):
                image = StudyStore._materialize_readonly_image(artifacts)
                memory = sqlite3.connect(":memory:")
                try:
                    memory.deserialize(image)
                    return [
                        row[0]
                        for row in memory.execute(
                            "SELECT question FROM attempts ORDER BY id"
                        )
                    ]
                finally:
                    memory.close()

            self.assertEqual(
                questions(second),
                ["first commit", "second commit"],
            )
            incomplete = dict(second)
            incomplete["-wal"] += b"incomplete"
            self.assertEqual(
                questions(incomplete),
                ["first commit", "second commit"],
            )

            corrupt_frame = dict(second)
            corrupt_wal = bytearray(corrupt_frame["-wal"])
            corrupt_wal[len(first["-wal"]) + 16] ^= 1
            corrupt_frame["-wal"] = bytes(corrupt_wal)
            self.assertEqual(questions(corrupt_frame), ["first commit"])

            corrupt_header = dict(second)
            corrupt_wal = bytearray(corrupt_header["-wal"])
            corrupt_wal[24] ^= 1
            corrupt_header["-wal"] = bytes(corrupt_wal)
            with self.assertRaisesRegex(sqlite3.DatabaseError, "checksum"):
                StudyStore._materialize_readonly_image(corrupt_header)

            hot_journal = {"": second[""], "-journal": b"not-empty"}
            with self.assertRaisesRegex(sqlite3.DatabaseError, "rollback journal"):
                StudyStore._materialize_readonly_image(hot_journal)
        finally:
            anchor.rollback()
            anchor.close()

    def test_materialization_never_repairs_invalid_database_header_versions(self):
        main = bytearray(self.db.read_bytes())
        self.assertIn(bytes(main[18:20]), {b"\x01\x01", b"\x02\x02"})

        for versions in (b"cc", b"\x01\x02", b"\x02\x01"):
            with self.subTest(versions=versions):
                malformed = bytearray(main)
                malformed[18:20] = versions
                with self.assertRaisesRegex(
                    sqlite3.DatabaseError,
                    "version|header",
                ):
                    StudyStore._materialize_readonly_image(
                        {"": bytes(malformed)}
                    )

    def test_readonly_size_limit_applies_before_capture_and_without_a_wal(self):
        main = self.db.read_bytes()
        with mock.patch(
            "coachlib.store.MAX_READONLY_IMAGE_BYTES",
            len(main) - 1,
        ):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "safety limit"):
                StudyStore._materialize_readonly_image({"": main})

        with mock.patch(
            "coachlib.store.MAX_READONLY_IMAGE_BYTES",
            1,
        ), mock.patch(
            "coachlib.store.os.read",
            side_effect=AssertionError("oversized artifact was read"),
        ):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "safety limit"):
                self.store._capture_database_artifacts(
                    self.store._current_database_identity()
                )

    def test_readonly_object_keeps_its_validated_snapshot_until_reopened(self):
        receipt = self.store.record_attempt(
            wrong_attempt(duration_seconds=12)
        )
        readonly = StudyStore.open_readonly(self.db, KNOWN_IDS)
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "UPDATE attempts SET duration_seconds=37 WHERE id=?",
                (receipt["attempt_id"],),
            )
            connection.commit()

            saved = readonly.get_attempt(receipt["attempt_id"])
            reopened = StudyStore.open_readonly(self.db, KNOWN_IDS)
            refreshed = reopened.get_attempt(receipt["attempt_id"])

            self.assertEqual(saved["duration_seconds"], 12.0)
            self.assertEqual(refreshed["duration_seconds"], 37.0)
        finally:
            connection.close()

    def test_readonly_object_never_queries_a_replaced_unvalidated_database(self):
        receipt = self.store.record_attempt(
            wrong_attempt(duration_seconds=12)
        )
        readonly = StudyStore.open_readonly(self.db, KNOWN_IDS)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE schema_meta SET value='another-application' "
                "WHERE key='owner'"
            )
            connection.execute(
                "UPDATE attempts SET duration_seconds=99 WHERE id=?",
                (receipt["attempt_id"],),
            )

        saved = readonly.get_attempt(receipt["attempt_id"])

        self.assertEqual(saved["duration_seconds"], 12.0)
        with self.assertRaisesRegex(ValueError, "owner marker"):
            StudyStore.open_readonly(self.db, KNOWN_IDS)

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

    def test_overall_progress_uses_one_consistent_database_snapshot(self):
        writer = StudyStore(self.db, KNOWN_IDS)
        original = self.store._node_progress
        inserted = False

        def interleaved(knowledge_id, connection=None):
            nonlocal inserted
            if connection is None:
                result = original(knowledge_id)
            else:
                result = original(knowledge_id, connection=connection)
            if knowledge_id is None and not inserted:
                inserted = True
                writer.record_attempt(wrong_attempt())
            return result

        self.store._node_progress = interleaved

        report = self.store.progress()

        self.assertEqual(report["total"], 0)
        self.assertTrue(all(node["total"] == 0 for node in report["nodes"]))
        self.assertEqual(writer.count_attempts(), 1)

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
        self.assertEqual(parsed["schema_version"], 3)
        self.assertEqual(parsed["attempts"][0]["id"], receipt["attempt_id"])
        self.assertEqual(parsed["attempts"][0]["target_duration_source"], "default")
        self.assertEqual(
            parsed["attempts"][0]["effective_target_duration_seconds"],
            45.0,
        )
        self.assertEqual(len(parsed["reviews"]), 1)
        self.assertEqual(list(output.parent.glob("*.tmp")), [])

    def test_export_refuses_database_and_sqlite_sidecars_even_with_overwrite(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                database = Path(self.temp.name) / f"protected-{suffix or 'main'}.sqlite3"
                store = StudyStore(database, KNOWN_IDS)
                destination = Path(f"{database}{suffix}")
                if suffix:
                    destination.write_text("sentinel", encoding="utf-8")
                before = destination.read_bytes()

                with self.assertRaisesRegex(ValueError, "active database"):
                    store.export_json(destination, overwrite=True)

                self.assertEqual(destination.read_bytes(), before)

    def test_export_refuses_case_and_unicode_aliases_of_protected_paths(self):
        alias = self.db.with_name(self.db.name.upper())
        try:
            aliases_same_file = self.db.samefile(alias)
        except OSError:
            aliases_same_file = False
        if aliases_same_file:
            before = self.db.read_bytes()
            with self.assertRaisesRegex(ValueError, "active database"):
                self.store.export_json(alias, overwrite=True)
            self.assertEqual(self.db.read_bytes(), before)

        unicode_db = Path(self.temp.name) / "café.sqlite3"
        unicode_store = StudyStore(unicode_db, KNOWN_IDS)
        decomposed_alias = unicode_db.with_name("cafe\u0301.sqlite3")
        before = unicode_db.read_bytes()
        with self.assertRaisesRegex(ValueError, "active database"):
            unicode_store.export_json(decomposed_alias, overwrite=True)
        self.assertEqual(unicode_db.read_bytes(), before)

    def test_export_rejects_parent_symlink_swap_before_commit(self):
        self.store.record_attempt(wrong_attempt())
        export_parent = Path(self.temp.name) / "export-parent"
        export_parent.mkdir()
        relocated = Path(self.temp.name) / "relocated-parent"
        outside = Path(self.temp.name) / "outside-target"
        outside.mkdir()
        destination = export_parent / "export.json"
        original = self.store._attempt_from_row
        swapped = False

        def swap_parent(connection, row):
            nonlocal swapped
            if not swapped:
                swapped = True
                export_parent.rename(relocated)
                export_parent.symlink_to(outside, target_is_directory=True)
            return original(connection, row)

        self.store._attempt_from_row = swap_parent

        with self.assertRaisesRegex(ValueError, "parent changed"):
            self.store.export_json(destination, overwrite=True)

        self.assertFalse((outside / destination.name).exists())
        self.assertFalse((relocated / destination.name).exists())

    def test_export_never_reports_success_if_parent_moves_during_commit(self):
        self.store.record_attempt(wrong_attempt())
        for overwrite in (False, True):
            with self.subTest(overwrite=overwrite):
                export_parent = Path(self.temp.name) / f"commit-parent-{overwrite}"
                export_parent.mkdir()
                relocated = Path(self.temp.name) / f"commit-relocated-{overwrite}"
                destination = export_parent / "export.json"
                if overwrite:
                    destination.write_text("old export", encoding="utf-8")
                if overwrite:
                    real_operation = os.replace
                    patch_target = "coachlib.store.os.replace"
                else:
                    from coachlib import store as store_module

                    real_operation = store_module._system_rename_noreplace
                    patch_target = "coachlib.store._rename_noreplace"
                swapped = False

                def swap_parent_then_commit(*args, **kwargs):
                    nonlocal swapped
                    if not swapped:
                        export_parent.rename(relocated)
                        export_parent.mkdir()
                        swapped = True
                    return real_operation(*args, **kwargs)

                with mock.patch(
                    patch_target,
                    side_effect=swap_parent_then_commit,
                ):
                    with self.assertRaisesRegex(ValueError, "parent.*commit"):
                        self.store.export_json(
                            destination,
                            overwrite=overwrite,
                        )

                self.assertTrue(swapped)
                self.assertFalse(destination.exists())

    def test_export_source_swap_never_reports_success(self):
        self.store.record_attempt(wrong_attempt())
        for overwrite in (False, True):
            with self.subTest(overwrite=overwrite):
                export_parent = Path(self.temp.name) / f"source-swap-{overwrite}"
                export_parent.mkdir()
                destination = export_parent / "export.json"
                if overwrite:
                    destination.write_text("OLD", encoding="utf-8")
                victim = export_parent / "victim.txt"
                victim_bytes = b"VICTIM-MUST-NOT-BE-A-SUCCESSFUL-EXPORT"
                victim.write_bytes(victim_bytes)
                saved_name = "attacker-saved-real-export"

                if overwrite:
                    real_operation = os.replace
                    patch_target = "coachlib.store.os.replace"
                else:
                    from coachlib import store as store_module

                    real_operation = store_module._system_rename_noreplace
                    patch_target = "coachlib.store._rename_noreplace"

                def swap_source_then_publish(
                    source_name,
                    destination_name,
                    *args,
                    **kwargs,
                ):
                    directory_fd = kwargs.get("directory_fd")
                    if directory_fd is None:
                        directory_fd = kwargs["src_dir_fd"]
                    os.rename(
                        source_name,
                        saved_name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                    os.rename(
                        victim.name,
                        source_name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                    return real_operation(
                        source_name,
                        destination_name,
                        *args,
                        **kwargs,
                    )

                with mock.patch(
                    patch_target,
                    side_effect=swap_source_then_publish,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "export.*identity|identity.*export",
                    ):
                        self.store.export_json(
                            destination,
                            overwrite=overwrite,
                        )

                self.assertEqual(destination.read_bytes(), victim_bytes)
                self.assertTrue(
                    json.loads(
                        (export_parent / saved_name).read_text(encoding="utf-8")
                    )
                )

    def test_export_failure_does_not_unlink_a_swapped_temporary_path(self):
        self.store.record_attempt(wrong_attempt())
        export_parent = Path(self.temp.name) / "failed-source-swap"
        export_parent.mkdir()
        destination = export_parent / "export.json"
        victim = export_parent / "victim.txt"
        victim_bytes = b"VICTIM-PATH-MUST-SURVIVE"
        victim.write_bytes(victim_bytes)
        swapped_temporary: str | None = None

        def swap_source_then_fail(
            source_name,
            destination_name,
            *,
            directory_fd,
        ):
            nonlocal swapped_temporary
            saved_name = "attacker-saved-real-export"
            os.rename(
                source_name,
                saved_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.rename(
                victim.name,
                source_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            swapped_temporary = source_name
            raise OSError("injected publication failure")

        with mock.patch(
            "coachlib.store._rename_noreplace",
            side_effect=swap_source_then_fail,
        ):
            with self.assertRaisesRegex(OSError, "publication failure"):
                self.store.export_json(destination)

        self.assertIsNotNone(swapped_temporary)
        self.assertEqual(
            (export_parent / swapped_temporary).read_bytes(),
            victim_bytes,
        )

    def test_export_refuses_to_replace_existing_destination_by_default(self):
        self.store.record_attempt(wrong_attempt())
        output = Path(self.temp.name) / "existing.json"
        output.write_text("keep me", encoding="utf-8")

        with self.assertRaisesRegex(FileExistsError, "exists"):
            self.store.export_json(output)

        self.assertEqual(output.read_text(encoding="utf-8"), "keep me")
        self.assertEqual(list(output.parent.glob("*.tmp")), [])

    def test_export_does_not_refollow_a_parent_symlink_after_cli_validation(self):
        self.store.record_attempt(wrong_attempt())
        late_parent = Path(self.temp.name) / "late-export-parent"
        destination = late_parent / "export.json"
        outside = Path(self.temp.name) / "outside-export-target"
        outside.mkdir()
        late_parent.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "parent|destination|canonical"):
            self.store.export_json(
                destination,
                path_already_resolved=True,
            )

        self.assertFalse((outside / destination.name).exists())

    def test_export_can_replace_existing_destination_when_explicit(self):
        self.store.record_attempt(wrong_attempt())
        output = Path(self.temp.name) / "existing.json"
        output.write_text("replace me", encoding="utf-8")

        receipt = self.store.export_json(output, overwrite=True)

        self.assertEqual(receipt["output"], str(output.resolve()))
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8"))["attempts"][0]["id"],
            1,
        )

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
