"""Local SQLite persistence for attempts, mistakes, reviews, and progress."""

from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import secrets
import sqlite3
import stat
import statistics
import sys
import unicodedata
from collections.abc import Iterable, Mapping
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .progress import classify_mastery, wilson_interval


GOOD_INTERVALS = (1, 3, 7, 14, 30)
METHOD_QUALITIES = {"optimal", "acceptable", "low"}
REVIEW_RESULTS = {"wrong", "hard", "good"}
ERROR_CODES = frozenset(
    {
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
)
ATTEMPT_PAYLOAD_FIELDS = frozenset(
    {
        "question",
        "user_answer",
        "correct_answer",
        "is_correct",
        "duration_seconds",
        "target_duration_seconds",
        "confidence",
        "knowledge_ids",
        "errors",
        "method_used",
        "recommended_method",
        "method_quality",
        "error_path",
        "correction_rule",
        "source_ref",
        "traceability",
        "difficulty",
        "mode",
        "parse_confidence",
        "is_transfer",
        "exact_method",
        "fast_method",
    }
)
SCHEMA_VERSION = 3
DATABASE_OWNER_KEY = "owner"
DATABASE_OWNER = "gongkao-data-analysis-coach"
DATABASE_INSTANCE_KEY = "instance_id"
MIGRATED_FROM_VERSION_KEY = "migrated_from_version"
DEFAULT_TARGET_DURATION_SECONDS = 45.0
PROGRESS_MIN_PARSE_CONFIDENCE = 0.90
TARGET_DURATION_SOURCES = frozenset({"default", "explicit", "legacy_unknown"})
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
READONLY_SNAPSHOT_ATTEMPTS = 8
SQLITE_DATABASE_HEADER = b"SQLite format 3\x00"
SQLITE_WAL_FORMAT_VERSION = 3_007_000
SQLITE_WAL_MAGIC_LITTLE_ENDIAN = 0x377F0682
SQLITE_WAL_MAGIC_BIG_ENDIAN = 0x377F0683
MAX_READONLY_IMAGE_BYTES = 512 * 1024 * 1024
READONLY_SIDECAR_SUFFIXES = ("-wal", "-journal")


def _system_rename_noreplace(
    source_name: str,
    destination_name: str,
    *,
    directory_fd: int,
) -> None:
    """Atomically rename one entry without replacing an existing target."""

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function_name = "renameatx_np"
        flag = 0x00000004  # RENAME_EXCL from macOS <sys/stdio.h>.
    elif sys.platform.startswith("linux"):
        function_name = "renameat2"
        flag = 0x00000001  # RENAME_NOREPLACE from Linux <stdio.h>.
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace rename is unavailable on this platform",
        )
    try:
        rename_function = getattr(libc, function_name)
    except AttributeError as error:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace rename is unavailable on this platform",
        ) from error
    rename_function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename_function.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = rename_function(
        directory_fd,
        os.fsencode(source_name),
        directory_fd,
        os.fsencode(destination_name),
        flag,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno() or errno.EIO
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            destination_name,
        )
    raise OSError(
        error_number,
        os.strerror(error_number),
        destination_name,
    )


def _rename_noreplace(
    source_name: str,
    destination_name: str,
    *,
    directory_fd: int,
) -> None:
    """Patchable boundary for an atomic same-directory no-replace rename."""

    _system_rename_noreplace(
        source_name,
        destination_name,
        directory_fd=directory_fd,
    )


_TARGET_DURATION_COLUMN = (
    f"REAL NOT NULL DEFAULT {DEFAULT_TARGET_DURATION_SECONDS:g} "
    "CHECK (target_duration_seconds > 0)"
)
_TARGET_DURATION_SOURCE_COLUMN = (
    "TEXT NOT NULL DEFAULT 'default' "
    "CHECK (target_duration_source IN "
    "('default', 'explicit', 'legacy_unknown'))"
)
_V2_TABLE_COLUMNS = {
    "schema_meta": frozenset({"key", "value"}),
    "attempts": frozenset(
        {
            "id",
            "question",
            "user_answer",
            "correct_answer",
            "is_correct",
            "duration_seconds",
            "target_duration_seconds",
            "confidence",
            "difficulty",
            "mode",
            "parse_confidence",
            "method_used",
            "recommended_method",
            "method_quality",
            "error_path",
            "correction_rule",
            "source_ref",
            "traceability",
            "priority",
            "is_transfer",
            "exact_method_json",
            "fast_method_json",
            "created_at",
        }
    ),
    "attempt_tags": frozenset({"attempt_id", "knowledge_id", "position"}),
    "attempt_errors": frozenset({"attempt_id", "error_code", "position"}),
    "reviews": frozenset(
        {
            "id",
            "attempt_id",
            "result",
            "status",
            "due_date",
            "reviewed_on",
            "interval_days",
            "round",
            "interval_qualified",
            "created_at",
        }
    ),
    "sessions": frozenset(
        {"id", "mode", "knowledge_id", "started_at", "ended_at", "metadata_json"}
    ),
}
_V3_TABLE_COLUMNS = {
    **_V2_TABLE_COLUMNS,
    "attempts": _V2_TABLE_COLUMNS["attempts"] | {"target_duration_source"},
}
_COACH_TABLES = frozenset(_V2_TABLE_COLUMNS)
_EXPECTED_PRIMARY_KEYS = {
    "schema_meta": {"key": 1},
    "attempts": {"id": 1},
    "attempt_tags": {"attempt_id": 1, "knowledge_id": 2},
    "attempt_errors": {"attempt_id": 1, "error_code": 2},
    "reviews": {"id": 1},
    "sessions": {"id": 1},
}
_EXPECTED_EXPLICIT_INDEXES = {
    "attempt_tags_knowledge_idx": (
        "attempt_tags",
        False,
        False,
        ("knowledge_id", "attempt_id"),
    ),
    "reviews_due_idx": (
        "reviews",
        False,
        False,
        ("status", "due_date", "attempt_id"),
    ),
    "reviews_one_scheduled_idx": (
        "reviews",
        True,
        True,
        ("attempt_id",),
    ),
}
_EXPECTED_INDEX_SQL = {
    "attempt_tags_knowledge_idx": (
        "createindexattempt_tags_knowledge_idx"
        "onattempt_tags(knowledge_id,attempt_id)"
    ),
    "reviews_due_idx": (
        "createindexreviews_due_idx"
        "onreviews(status,due_date,attempt_id)"
    ),
    "reviews_one_scheduled_idx": (
        "createuniqueindexreviews_one_scheduled_idx"
        "onreviews(attempt_id)wherestatus='scheduled'"
    ),
}
_EXPECTED_FOREIGN_KEYS = {
    "schema_meta": (),
    "attempts": (),
    "attempt_tags": (("attempts", "attempt_id", "id", "CASCADE"),),
    "attempt_errors": (("attempts", "attempt_id", "id", "CASCADE"),),
    "reviews": (("attempts", "attempt_id", "id", "CASCADE"),),
    "sessions": (),
}
_INTEGER_COLUMNS = frozenset(
    {
        "id",
        "is_correct",
        "confidence",
        "is_transfer",
        "attempt_id",
        "position",
        "interval_days",
        "round",
        "interval_qualified",
    }
)
_REAL_COLUMNS = frozenset(
    {
        "duration_seconds",
        "target_duration_seconds",
        "parse_confidence",
    }
)
_NOT_NULL_COLUMNS = {
    "schema_meta": frozenset({"value"}),
    "attempts": frozenset(
        {
            "question",
            "is_correct",
            "target_duration_seconds",
            "target_duration_source",
            "method_quality",
            "traceability",
            "priority",
            "is_transfer",
            "created_at",
        }
    ),
    "attempt_tags": frozenset({"attempt_id", "knowledge_id", "position"}),
    "attempt_errors": frozenset({"attempt_id", "error_code", "position"}),
    "reviews": frozenset(
        {
            "attempt_id",
            "status",
            "due_date",
            "interval_days",
            "round",
            "interval_qualified",
            "created_at",
        }
    ),
    "sessions": frozenset({"mode", "started_at"}),
}
_COLUMN_DEFAULTS = {
    ("attempts", "target_duration_source"): "'default'",
    ("attempts", "traceability"): "'high'",
    ("attempts", "priority"): "'normal'",
    ("attempts", "is_transfer"): "0",
    ("reviews", "round"): "1",
    ("reviews", "interval_qualified"): "0",
}
_REQUIRED_TABLE_SQL = {
    "schema_meta": ("keytextprimarykey", "valuetextnotnull"),
    "attempts": (
        "idintegerprimarykeyautoincrement",
        "check(is_correctin(0,1))",
        "check(duration_secondsisnullorduration_seconds>=0)",
        "check(target_duration_seconds>0)",
        "check(confidenceisnullorconfidencebetween1and5)",
        "check(parse_confidenceisnullorparse_confidencebetween0and1)",
        "check(method_qualityin('optimal','acceptable','low'))",
        "check(traceabilityin('high','low'))",
        "check(priorityin('normal','high'))",
        "check(is_transferin(0,1))",
    ),
    "attempt_tags": (
        "referencesattempts(id)ondeletecascade",
        "primarykey(attempt_id,knowledge_id)",
    ),
    "attempt_errors": (
        "referencesattempts(id)ondeletecascade",
        "primarykey(attempt_id,error_code)",
    ),
    "reviews": (
        "idintegerprimarykeyautoincrement",
        "referencesattempts(id)ondeletecascade",
        "check(resultisnullorresultin('wrong','hard','good'))",
        "check(statusin('scheduled','completed'))",
        "check(interval_days>0)",
        "check(round>0)",
        "check(interval_qualifiedin(0,1))",
    ),
    "sessions": ("idintegerprimarykeyautoincrement",),
}
_ATTEMPTS_SQL_BEFORE_TARGET = (
    "createtableattempts(idintegerprimarykeyautoincrement,questiontextnotnull,"
    "user_answertext,correct_answertext,is_correctintegernotnull"
    "check(is_correctin(0,1)),duration_secondsreal"
    "check(duration_secondsisnullorduration_seconds>=0),"
)
_ATTEMPTS_TARGET_SQL = (
    "target_duration_secondsrealnotnulldefault{default}"
    "check(target_duration_seconds>0)"
)
_ATTEMPTS_SOURCE_SQL = (
    ",target_duration_sourcetextnotnulldefault'default'"
    "check(target_duration_sourcein('default','explicit','legacy_unknown'))"
)
_ATTEMPTS_SQL_AFTER_TARGET = (
    ",confidenceintegercheck(confidenceisnullorconfidencebetween1and5),"
    "difficultytext,modetext,parse_confidencereal"
    "check(parse_confidenceisnullorparse_confidencebetween0and1),"
    "method_usedtext,recommended_methodtext,method_qualitytextnotnull"
    "check(method_qualityin('optimal','acceptable','low')),error_pathtext,"
    "correction_ruletext,source_reftext,traceabilitytextnotnulldefault'high'"
    "check(traceabilityin('high','low')),prioritytextnotnulldefault'normal'"
    "check(priorityin('normal','high')),is_transferintegernotnulldefault0"
    "check(is_transferin(0,1)),exact_method_jsontext,fast_method_jsontext,"
    "created_attextnotnull"
)
_CANONICAL_TABLE_SQL = {
    "schema_meta": "createtableschema_meta(keytextprimarykey,valuetextnotnull)",
    "attempt_tags": (
        "createtableattempt_tags(attempt_idintegernotnull"
        "referencesattempts(id)ondeletecascade,knowledge_idtextnotnull,"
        "positionintegernotnull,primarykey(attempt_id,knowledge_id))"
    ),
    "attempt_errors": (
        "createtableattempt_errors(attempt_idintegernotnull"
        "referencesattempts(id)ondeletecascade,error_codetextnotnull,"
        "positionintegernotnull,primarykey(attempt_id,error_code))"
    ),
    "reviews": (
        "createtablereviews(idintegerprimarykeyautoincrement,"
        "attempt_idintegernotnullreferencesattempts(id)ondeletecascade,"
        "resulttextcheck(resultisnullorresultin('wrong','hard','good')),"
        "statustextnotnullcheck(statusin('scheduled','completed')),"
        "due_datetextnotnull,reviewed_ontext,interval_daysintegernotnull"
        "check(interval_days>0),roundintegernotnulldefault1check(round>0),"
        "interval_qualifiedintegernotnulldefault0"
        "check(interval_qualifiedin(0,1)),created_attextnotnull)"
    ),
    "sessions": (
        "createtablesessions(idintegerprimarykeyautoincrement,modetextnotnull,"
        "knowledge_idtext,started_attextnotnull,ended_attext,metadata_jsontext)"
    ),
}


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


def _compact_schema_sql(sql: str) -> str:
    """Normalize insignificant SQL formatting without changing literals.

    SQLite keywords and unquoted identifiers are case-insensitive, while text
    inside quoted literals is data.  A plain ``lower``/whitespace pass would
    therefore make semantically different CHECK constraints compare equal.
    """

    compact: list[str] = []
    delimiter: str | None = None
    index = 0
    while index < len(sql):
        character = sql[index]
        if delimiter is not None:
            compact.append(character)
            closing = "]" if delimiter == "[" else delimiter
            if character == closing:
                if (
                    delimiter != "["
                    and index + 1 < len(sql)
                    and sql[index + 1] == delimiter
                ):
                    compact.append(sql[index + 1])
                    index += 1
                else:
                    delimiter = None
        elif character in {"'", '"', "`", "["}:
            delimiter = character
            compact.append(character)
        elif not character.isspace():
            compact.append(character.lower())
        index += 1
    if delimiter is not None:
        raise ValueError("incompatible coach database: unterminated schema quote")
    return "".join(compact)


def _valid_database_instance(value: str | None) -> bool:
    return bool(
        value
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _answer(value: Any, name: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    cleaned = value.strip()
    return cleaned, cleaned.casefold()


def _absolute_lexical_path(value: str | Path) -> Path:
    """Make a path absolute without re-following symlinks after CLI validation."""

    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _wal_checksum(
    payload: bytes | bytearray | memoryview,
    byteorder: str,
    seed: tuple[int, int] = (0, 0),
) -> tuple[int, int]:
    """Return SQLite's rolling two-word WAL checksum."""

    if len(payload) % 8:
        raise sqlite3.DatabaseError("invalid SQLite WAL checksum input length")
    first, second = seed
    view = memoryview(payload)
    for offset in range(0, len(view), 8):
        word_1 = int.from_bytes(view[offset : offset + 4], byteorder)
        word_2 = int.from_bytes(view[offset + 4 : offset + 8], byteorder)
        first = (first + word_1 + second) & 0xFFFFFFFF
        second = (second + word_2 + first) & 0xFFFFFFFF
    return first, second


def _sqlite_page_size(
    database_image: bytes | bytearray | memoryview,
) -> int:
    if (
        len(database_image) < 100
        or database_image[:16] != SQLITE_DATABASE_HEADER
    ):
        raise sqlite3.DatabaseError("invalid SQLite database header")
    encoded = int.from_bytes(database_image[16:18], "big")
    page_size = 65_536 if encoded == 1 else encoded
    if (
        page_size < 512
        or page_size > 65_536
        or page_size & (page_size - 1)
    ):
        raise sqlite3.DatabaseError("invalid SQLite database page size")
    return page_size


def _secure_mkdir_parents(directory: str | Path) -> Path:
    """Create missing directories from a no-follow, identity-checked ancestor."""

    target = _absolute_lexical_path(directory)
    missing: list[str] = []
    ancestor = target
    while True:
        try:
            ancestor_status = ancestor.stat(follow_symlinks=False)
            break
        except FileNotFoundError:
            if ancestor.parent == ancestor:
                raise ValueError("no existing ancestor for directory creation")
            missing.append(ancestor.name)
            ancestor = ancestor.parent
    if not stat.S_ISDIR(ancestor_status.st_mode):
        raise ValueError("directory parent ancestor is not a real directory")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(ancestor, directory_flags)
    try:
        opened_status = os.fstat(directory_fd)
        current_status = ancestor.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened_status.st_mode)
            or (opened_status.st_dev, opened_status.st_ino)
            != (current_status.st_dev, current_status.st_ino)
            or ancestor.resolve(strict=True) != ancestor
        ):
            raise ValueError("directory ancestor changed during secure creation")

        for component in reversed(missing):
            try:
                os.mkdir(component, mode=0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            child_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            try:
                child_status = os.fstat(child_fd)
                if not stat.S_ISDIR(child_status.st_mode):
                    raise ValueError(
                        "directory component is not a real directory"
                    )
            except BaseException:
                os.close(child_fd)
                raise
            previous_fd = directory_fd
            directory_fd = child_fd
            os.close(previous_fd)
    finally:
        os.close(directory_fd)
    return target


def paths_alias(left: str | Path, right: str | Path) -> bool:
    """Return whether two paths can address the same filesystem entry.

    ``Path.resolve`` alone does not canonicalize case or Unicode composition on
    common macOS filesystems.  Existing hard links are caught by ``samefile``;
    normalized basename comparison also protects SQLite sidecar names that do
    not exist yet.
    """

    left_path = Path(left).expanduser()
    right_path = Path(right).expanduser()
    try:
        if os.path.samefile(left_path, right_path):
            return True
    except OSError:
        pass

    try:
        parents_match = os.path.samefile(
            left_path.parent,
            right_path.parent,
        )
    except OSError:
        parents_match = (
            left_path.parent.resolve() == right_path.parent.resolve()
        )
    if not parents_match:
        return False

    def filename_key(name: str) -> str:
        return unicodedata.normalize("NFC", name).casefold()

    return filename_key(left_path.name) == filename_key(right_path.name)


class StudyStore:
    """A small transactional study database owned by the current workspace."""

    def __init__(
        self,
        database: str | Path,
        known_knowledge_ids: Iterable[str] | Mapping[str, float],
        *,
        initialize: bool = True,
        read_only: bool = False,
        path_already_resolved: bool = False,
    ):
        if not all(
            isinstance(value, bool)
            for value in (initialize, read_only, path_already_resolved)
        ):
            raise ValueError(
                "initialize, read_only, and path_already_resolved must be booleans"
            )
        self.database = (
            _absolute_lexical_path(database)
            if path_already_resolved
            else Path(database).expanduser().resolve()
        )
        if initialize and read_only:
            raise ValueError("read-only stores cannot initialize the database")
        if not initialize and not read_only:
            raise ValueError("store must initialize or use read-only mode")
        self._read_only = read_only
        self._readonly_artifacts: dict[str, bytes] | None = None
        self._database_parent_identity: tuple[int, int] | None = None
        self._database_identity: tuple[int, int] | None = None
        self._database_instance_id: str | None = None
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
        try:
            total_weight = math.fsum(self.knowledge_weights.values())
        except OverflowError as error:
            raise ValueError("knowledge weight total overflows") from error
        if not math.isfinite(total_weight):
            raise ValueError("knowledge weight total must be finite")
        self.known_knowledge_ids = frozenset(self.knowledge_weights)
        if not self.known_knowledge_ids:
            raise ValueError("known knowledge ids must not be empty")
        self.initialization_receipt: dict[str, Any] | None = None
        if initialize:
            _secure_mkdir_parents(self.database.parent)
            self._bind_database_parent()
            self.initialization_receipt = self.initialize()
            self._database_instance_id = self.initialization_receipt[
                "database_instance_id"
            ]
            self._database_identity = self._current_database_identity()
        elif read_only:
            self._bind_database_parent()
            readonly_identity = self._current_database_identity()
            self._readonly_artifacts = self._capture_database_artifacts(
                readonly_identity
            )
            if self._current_database_identity() != readonly_identity:
                raise ValueError(
                    "study database identity changed during read-only capture"
                )
            self._database_identity = readonly_identity
            self._validate_readonly_database()

    def _bind_database_parent(self) -> None:
        """Pin the canonical parent directory used for every database action."""

        parent = self.database.parent
        try:
            status = parent.stat(follow_symlinks=False)
            resolved = parent.resolve(strict=True)
        except OSError as error:
            raise ValueError("study database parent cannot be verified") from error
        if not stat.S_ISDIR(status.st_mode) or resolved != parent:
            raise ValueError(
                "study database parent must be a canonical real directory"
            )
        self._database_parent_identity = (
            int(status.st_dev),
            int(status.st_ino),
        )

    def _validate_database_parent_binding(self) -> None:
        expected = self._database_parent_identity
        if expected is None:
            raise ValueError("study database parent binding is unavailable")
        parent = self.database.parent
        try:
            status = parent.stat(follow_symlinks=False)
            resolved = parent.resolve(strict=True)
        except OSError as error:
            raise ValueError(
                "study database parent was replaced after validation"
            ) from error
        current = int(status.st_dev), int(status.st_ino)
        if (
            not stat.S_ISDIR(status.st_mode)
            or resolved != parent
            or current != expected
        ):
            raise ValueError(
                "study database parent was replaced after validation; "
                "parent binding does not match"
            )

    def _open_bound_parent_directory(self) -> int:
        self._validate_database_parent_binding()
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(self.database.parent, directory_flags)
        except OSError as error:
            raise ValueError(
                "study database parent directory cannot be opened safely"
            ) from error
        try:
            status = os.fstat(directory_fd)
        except Exception:
            os.close(directory_fd)
            raise
        identity = int(status.st_dev), int(status.st_ino)
        if identity != self._database_parent_identity:
            os.close(directory_fd)
            raise ValueError("study database parent directory binding changed")
        return directory_fd

    def _create_new_database(
        self,
        statements: Iterable[str],
    ) -> tuple[tuple[int, int], str]:
        """Build a complete image, then publish it with an exclusive rename."""

        database_instance_id = secrets.token_hex(16)
        memory = sqlite3.connect(":memory:")
        try:
            memory.execute("PRAGMA foreign_keys = ON")
            for statement in statements:
                memory.execute(statement)
            memory.executemany(
                "INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                (
                    (DATABASE_OWNER_KEY, DATABASE_OWNER),
                    (DATABASE_INSTANCE_KEY, database_instance_id),
                    ("version", str(SCHEMA_VERSION)),
                ),
            )
            memory.commit()
            if self._inspect_existing_database(memory) != SCHEMA_VERSION:
                raise ValueError("new in-memory coach database is invalid")
            if not hasattr(memory, "serialize"):
                raise RuntimeError(
                    "this Python SQLite build cannot serialize a new database"
                )
            database_image = memory.serialize()
        finally:
            memory.close()

        directory_fd = self._open_bound_parent_directory()
        staging_name: str | None = None
        staging_fd: int | None = None
        try:
            for suffix in SQLITE_SIDECAR_SUFFIXES:
                try:
                    os.stat(
                        f"{self.database.name}{suffix}",
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                raise ValueError(
                    "study database sidecar exists before secure creation"
                )

            for _ in range(32):
                candidate = (
                    f".gongkao-init-{secrets.token_hex(16)}.sqlite3"
                )
                try:
                    staging_fd = os.open(
                        candidate,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=directory_fd,
                    )
                except FileExistsError:
                    continue
                staging_name = candidate
                break
            else:
                raise FileExistsError(
                    "could not allocate a secure database staging file"
                )

            try:
                created = os.fstat(staging_fd)
                if (
                    not stat.S_ISREG(created.st_mode)
                    or created.st_nlink != 1
                ):
                    raise ValueError(
                        "staged study database is not a private regular file"
                    )
                created_identity = int(created.st_dev), int(created.st_ino)
                remaining = memoryview(database_image)
                while remaining:
                    written = os.write(staging_fd, remaining)
                    if written <= 0:
                        raise OSError("new study database write made no progress")
                    remaining = remaining[written:]
                os.fsync(staging_fd)

                staged_after_write = os.fstat(staging_fd)
                staged_path = os.stat(
                    staging_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(staged_after_write.st_mode)
                    or staged_after_write.st_nlink != 1
                    or staged_after_write.st_size != len(database_image)
                    or (
                        int(staged_after_write.st_dev),
                        int(staged_after_write.st_ino),
                    )
                    != created_identity
                    or (
                        int(staged_path.st_dev),
                        int(staged_path.st_ino),
                    )
                    != created_identity
                ):
                    raise ValueError(
                        "staged study database changed before publication"
                    )

                for suffix in SQLITE_SIDECAR_SUFFIXES:
                    try:
                        os.stat(
                            f"{self.database.name}{suffix}",
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        continue
                    raise ValueError(
                        "study database sidecar appeared during secure creation"
                    )
                try:
                    os.stat(
                        self.database.name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError(
                        "study database appeared during secure creation"
                    )

                try:
                    _rename_noreplace(
                        staging_name,
                        self.database.name,
                        directory_fd=directory_fd,
                    )
                except FileExistsError as error:
                    raise ValueError(
                        "study database appeared during secure publication"
                    ) from error
                published = os.stat(
                    self.database.name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (
                    (int(published.st_dev), int(published.st_ino))
                    != created_identity
                ):
                    raise ValueError(
                        "study database publication identity does not match"
                    )
                try:
                    os.stat(
                        staging_name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError(
                        "staged study database name survived publication"
                    )
                if os.fstat(staging_fd).st_nlink != 1:
                    raise ValueError(
                        "published study database has unexpected link count"
                    )
                for suffix in SQLITE_SIDECAR_SUFFIXES:
                    try:
                        os.stat(
                            f"{self.database.name}{suffix}",
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        continue
                    raise ValueError(
                        "study database sidecar appeared during publication"
                    )
                os.fsync(directory_fd)
            finally:
                os.close(staging_fd)
                staging_fd = None
        finally:
            # Before publication, a failed random staging file is deliberately
            # left in place: deleting a pathname after an adversarial swap is
            # not inode-conditional.  A successful rename consumes that name.
            os.close(directory_fd)
        self._validate_database_parent_binding()
        if self._current_database_identity() != created_identity:
            raise ValueError("study database changed after secure creation")
        return created_identity, database_instance_id

    @classmethod
    def open_readonly(
        cls,
        database: str | Path,
        known_knowledge_ids: Iterable[str] | Mapping[str, float],
        *,
        path_already_resolved: bool = False,
    ) -> StudyStore:
        """Open a current owned database without creating, migrating, or journaling."""

        if not isinstance(path_already_resolved, bool):
            raise ValueError("path_already_resolved must be a boolean")
        resolved = (
            _absolute_lexical_path(database)
            if path_already_resolved
            else Path(database).expanduser().resolve()
        )
        if not resolved.is_file():
            raise FileNotFoundError(f"study database does not exist: {resolved}")
        return cls(
            resolved,
            known_knowledge_ids,
            initialize=False,
            read_only=True,
            path_already_resolved=True,
        )

    def _connect(
        self,
        *,
        readonly_artifacts: Mapping[str, bytes] | None = None,
    ) -> sqlite3.Connection:
        self._validate_database_parent_binding()
        if readonly_artifacts is None and self.database.is_symlink():
            raise ValueError("study database path must not be a symbolic link")
        if readonly_artifacts is not None:
            connection = sqlite3.connect(":memory:", timeout=10)
        elif self._read_only:
            raise ValueError(
                "read-only connections require private database artifacts"
            )
        else:
            self._validate_writable_sidecars()
            if self._database_identity is not None:
                current_identity = self._current_database_identity()
                if current_identity != self._database_identity:
                    raise ValueError(
                        "study database path was replaced after validation; "
                        "database identity does not match"
                    )
                if self._database_instance_id is not None:
                    self._validate_expected_instance_before_writable_open(
                        current_identity
                    )
            connection = sqlite3.connect(
                f"{self.database.as_uri()}?mode=rw",
                uri=True,
                timeout=10,
            )
        try:
            if readonly_artifacts is None:
                self._validate_writable_sidecars()
            if readonly_artifacts is not None:
                database_image = self._materialize_readonly_image(
                    readonly_artifacts
                )
                if database_image:
                    if not hasattr(connection, "deserialize"):
                        raise RuntimeError(
                            "this Python SQLite build cannot deserialize "
                            "read-only snapshots"
                        )
                    connection.deserialize(database_image)
                connection.execute("PRAGMA query_only = ON")
            self._validate_database_parent_binding()
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            if readonly_artifacts is None:
                journal_mode = connection.execute(
                    "PRAGMA journal_mode = WAL"
                ).fetchone()
                if (
                    journal_mode is None
                    or str(journal_mode[0]).casefold() != "wal"
                ):
                    raise sqlite3.OperationalError(
                        "study database could not enter WAL mode"
                    )
                # Force SQLite to open and retain its WAL/SHM state before the
                # connection escapes this validated boundary.  A later rename
                # of a sidecar pathname then cannot redirect this connection's
                # writes to the replacement inode.
                connection.execute(
                    "SELECT name FROM sqlite_schema LIMIT 1"
                ).fetchone()
                self._validate_writable_sidecars()
            self._validate_database_parent_binding()
        except BaseException:
            try:
                connection.close()
            except BaseException:
                pass
            raise
        return connection

    def _current_database_identity(self) -> tuple[int, int]:
        self._validate_database_parent_binding()
        try:
            status = self.database.stat(follow_symlinks=False)
        except OSError as error:
            raise ValueError(
                "study database identity cannot be read"
            ) from error
        if not stat.S_ISREG(status.st_mode):
            raise ValueError("study database path is not a regular file")
        if status.st_nlink != 1:
            raise ValueError(
                "study database must have exactly one hard link; "
                "link count is unsafe for SQLite sidecar locking"
            )
        return int(status.st_dev), int(status.st_ino)

    def _validate_writable_sidecars(self) -> None:
        """Reject sidecars that SQLite could mutate through an unsafe name."""

        parent_fd = self._open_bound_parent_directory()
        open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        open_flags |= getattr(os, "O_NONBLOCK", 0)
        open_flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            for _ in range(READONLY_SNAPSHOT_ATTEMPTS):
                unstable = False
                for suffix in SQLITE_SIDECAR_SUFFIXES:
                    artifact_name = f"{self.database.name}{suffix}"
                    try:
                        path_before = os.stat(
                            artifact_name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(path_before.st_mode):
                        raise ValueError(
                            "study database sidecar must be a regular file: "
                            f"{artifact_name}"
                        )
                    if path_before.st_nlink > 1:
                        raise ValueError(
                            f"study database sidecar must have exactly one "
                            f"hard link: {artifact_name}"
                        )
                    if path_before.st_nlink == 0:
                        unstable = True
                        break
                    try:
                        artifact_fd = os.open(
                            artifact_name,
                            open_flags,
                            dir_fd=parent_fd,
                        )
                    except FileNotFoundError:
                        unstable = True
                        break
                    except OSError as error:
                        raise ValueError(
                            "study database sidecar cannot be opened safely: "
                            f"{artifact_name}"
                        ) from error
                    try:
                        opened = os.fstat(artifact_fd)
                        if not stat.S_ISREG(opened.st_mode):
                            raise ValueError(
                                "study database sidecar must be a regular "
                                f"file: {artifact_name}"
                            )
                        if opened.st_nlink > 1:
                            raise ValueError(
                                "study database sidecar must have exactly one "
                                f"hard link: {artifact_name}"
                            )
                        if opened.st_nlink == 0:
                            unstable = True
                            break
                        try:
                            path_after = os.stat(
                                artifact_name,
                                dir_fd=parent_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            unstable = True
                            break
                        if not stat.S_ISREG(path_after.st_mode):
                            raise ValueError(
                                "study database sidecar must be a regular "
                                f"file: {artifact_name}"
                            )
                        if path_after.st_nlink > 1:
                            raise ValueError(
                                "study database sidecar must have exactly one "
                                f"hard link: {artifact_name}"
                            )
                        if path_after.st_nlink == 0:
                            unstable = True
                            break
                        expected_identity = (
                            int(path_before.st_dev),
                            int(path_before.st_ino),
                        )
                        if (
                            (int(opened.st_dev), int(opened.st_ino))
                            != expected_identity
                            or (
                                int(path_after.st_dev),
                                int(path_after.st_ino),
                            )
                            != expected_identity
                        ):
                            unstable = True
                            break
                    finally:
                        os.close(artifact_fd)
                if not unstable:
                    break
            else:
                raise sqlite3.OperationalError(
                    "study database sidecars are busy; could not validate a "
                    "stable pathname binding"
                )
        finally:
            os.close(parent_fd)
        self._validate_database_parent_binding()

    def _validate_expected_instance_before_writable_open(
        self,
        expected_identity: tuple[int, int],
    ) -> None:
        """Validate the bound instance without letting SQLite touch sidecars."""

        artifacts = self._capture_database_artifacts(expected_identity)
        connection = self._connect(readonly_artifacts=artifacts)
        try:
            version = self._inspect_existing_database(connection)
            instance_id = self._metadata(connection).get(DATABASE_INSTANCE_KEY)
        finally:
            connection.close()
        if (
            version != SCHEMA_VERSION
            or instance_id != self._database_instance_id
        ):
            raise ValueError(
                "study database was replaced after validation; "
                "database instance marker does not match"
            )
        if self._current_database_identity() != expected_identity:
            raise ValueError(
                "study database path was replaced during instance validation"
            )

    def _read_database_artifacts(
        self,
        expected_identity: tuple[int, int],
    ) -> dict[str, bytes] | None:
        """Read one no-follow candidate snapshot through the bound parent."""

        parent_fd = self._open_bound_parent_directory()
        artifacts: dict[str, bytes] = {}
        captured_size = 0
        open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            for suffix in ("", *READONLY_SIDECAR_SUFFIXES):
                artifact_name = f"{self.database.name}{suffix}"
                try:
                    artifact_fd = os.open(
                        artifact_name,
                        open_flags,
                        dir_fd=parent_fd,
                    )
                except FileNotFoundError:
                    if suffix == "":
                        raise ValueError(
                            "study database disappeared during capture"
                        )
                    continue
                except OSError as error:
                    raise ValueError(
                        f"study database artifact cannot be opened safely: "
                        f"{artifact_name}"
                    ) from error
                try:
                    before = os.fstat(artifact_fd)
                    identity = int(before.st_dev), int(before.st_ino)
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError(
                            "study database artifacts must be single-link "
                            "regular files"
                        )
                    if before.st_nlink > 1:
                        raise ValueError(
                            "study database artifacts must be single-link "
                            "regular files"
                        )
                    if before.st_nlink == 0:
                        if suffix == "":
                            raise ValueError(
                                "study database main file lost its pathname"
                            )
                        return None
                    if suffix == "" and identity != expected_identity:
                        raise ValueError(
                            "study database identity changed during capture"
                        )
                    if suffix == "-journal" and before.st_size:
                        raise sqlite3.DatabaseError(
                            "active SQLite rollback journals cannot be read safely"
                        )
                    if (
                        before.st_size < 0
                        or before.st_size
                        > MAX_READONLY_IMAGE_BYTES - captured_size
                    ):
                        raise sqlite3.DatabaseError(
                            "read-only SQLite artifacts exceed the 512 MiB "
                            "safety limit"
                        )
                    chunks: list[bytes] = []
                    artifact_size = 0
                    while True:
                        chunk = os.read(artifact_fd, 1024 * 1024)
                        if not chunk:
                            break
                        artifact_size += len(chunk)
                        if (
                            artifact_size
                            > MAX_READONLY_IMAGE_BYTES - captured_size
                        ):
                            raise sqlite3.DatabaseError(
                                "read-only SQLite artifacts exceed the 512 MiB "
                                "safety limit"
                            )
                        chunks.append(chunk)
                    after = os.fstat(artifact_fd)
                    if after.st_nlink > 1:
                        raise ValueError(
                            "study database artifacts must be single-link "
                            "regular files"
                        )
                    if after.st_nlink == 0:
                        if suffix == "":
                            raise ValueError(
                                "study database main file lost its pathname"
                            )
                        return None
                    after_identity = int(after.st_dev), int(after.st_ino)
                    payload = b"".join(chunks)
                    if (
                        after_identity != identity
                        or after.st_size != before.st_size
                        or after.st_mtime_ns != before.st_mtime_ns
                        or len(payload) != after.st_size
                    ):
                        return None
                    artifacts[suffix] = payload
                    captured_size += len(payload)
                finally:
                    os.close(artifact_fd)
        finally:
            os.close(parent_fd)
        return artifacts

    @staticmethod
    def _materialize_readonly_image(
        artifacts: Mapping[str, bytes],
    ) -> bytes:
        """Merge the last valid WAL commit into one in-memory DB image."""

        main = artifacts.get("")
        if not isinstance(main, bytes):
            raise sqlite3.DatabaseError("read-only snapshot has no main database")
        journal = artifacts.get("-journal")
        if journal is not None and not isinstance(journal, bytes):
            raise sqlite3.DatabaseError(
                "read-only rollback journal artifact must contain bytes"
            )
        if journal:
            raise sqlite3.DatabaseError(
                "active SQLite rollback journals cannot be read safely"
            )
        wal = artifacts.get("-wal")
        if wal is not None and not isinstance(wal, bytes):
            raise sqlite3.DatabaseError(
                "read-only WAL artifact must contain bytes"
            )
        if len(main) + len(wal or b"") > MAX_READONLY_IMAGE_BYTES:
            raise sqlite3.DatabaseError(
                "read-only SQLite artifacts exceed the 512 MiB safety limit"
            )
        if not main:
            return b""
        page_size = _sqlite_page_size(main)
        if len(main) < page_size or len(main) % page_size:
            raise sqlite3.DatabaseError(
                "SQLite database size is not page aligned"
            )
        header_versions = bytes(main[18:20])
        if header_versions not in {b"\x01\x01", b"\x02\x02"}:
            raise sqlite3.DatabaseError(
                "invalid SQLite database read/write versions"
            )
        image = bytearray(main)
        if wal:
            if len(wal) < 32:
                raise sqlite3.DatabaseError("invalid SQLite WAL header")
            if header_versions != b"\x02\x02":
                raise sqlite3.DatabaseError(
                    "SQLite WAL requires WAL-mode database header versions"
                )
            magic = int.from_bytes(wal[0:4], "big")
            if magic == SQLITE_WAL_MAGIC_LITTLE_ENDIAN:
                checksum_order = "little"
            elif magic == SQLITE_WAL_MAGIC_BIG_ENDIAN:
                checksum_order = "big"
            else:
                raise sqlite3.DatabaseError("invalid SQLite WAL magic")
            if int.from_bytes(wal[4:8], "big") != SQLITE_WAL_FORMAT_VERSION:
                raise sqlite3.DatabaseError("unsupported SQLite WAL version")
            if int.from_bytes(wal[8:12], "big") != page_size:
                raise sqlite3.DatabaseError("SQLite WAL page size mismatch")

            checksum = _wal_checksum(wal[:24], checksum_order)
            stored_header_checksum = (
                int.from_bytes(wal[24:28], "big"),
                int.from_bytes(wal[28:32], "big"),
            )
            if checksum != stored_header_checksum:
                raise sqlite3.DatabaseError("invalid SQLite WAL header checksum")

            salt = wal[16:24]
            frame_size = 24 + page_size
            offset = 32
            wal_view = memoryview(wal)
            valid_frames: list[tuple[int, int]] = []
            committed_frame_count = 0
            committed_page_count = 0
            while offset + frame_size <= len(wal):
                frame_header = wal[offset : offset + 24]
                page_offset = offset + 24
                page = wal_view[page_offset : offset + frame_size]
                page_number = int.from_bytes(frame_header[0:4], "big")
                if (
                    page_number == 0
                    or page_number >= 0xFFFFFFFF
                    or frame_header[8:16] != salt
                ):
                    break
                candidate_checksum = _wal_checksum(
                    frame_header[:8],
                    checksum_order,
                    checksum,
                )
                candidate_checksum = _wal_checksum(
                    page,
                    checksum_order,
                    candidate_checksum,
                )
                stored_frame_checksum = (
                    int.from_bytes(frame_header[16:20], "big"),
                    int.from_bytes(frame_header[20:24], "big"),
                )
                if candidate_checksum != stored_frame_checksum:
                    break
                checksum = candidate_checksum
                valid_frames.append((page_number, page_offset))
                database_pages = int.from_bytes(frame_header[4:8], "big")
                if database_pages:
                    committed_frame_count = len(valid_frames)
                    committed_page_count = database_pages
                offset += frame_size

            if committed_frame_count:
                target_size = committed_page_count * page_size
                committed_frames = valid_frames[:committed_frame_count]
                maximum_page = max(
                    page_number for page_number, _ in committed_frames
                )
                main_pages = len(main) // page_size
                if (
                    committed_page_count >= 0xFFFFFFFF
                    or target_size < page_size
                    or committed_page_count > max(main_pages, maximum_page)
                ):
                    raise sqlite3.DatabaseError("invalid SQLite WAL commit size")
                # A valid growth transaction must carry at least the bytes by
                # which it extends the main image.  This also prevents a tiny,
                # forged WAL from requesting an unbounded allocation.
                if target_size > len(main) + len(wal):
                    raise sqlite3.DatabaseError(
                        "SQLite WAL commit size exceeds captured data"
                    )
                if target_size > MAX_READONLY_IMAGE_BYTES:
                    raise sqlite3.DatabaseError(
                        "read-only SQLite image exceeds the 512 MiB safety limit"
                    )
                if target_size > len(image):
                    image.extend(b"\x00" * (target_size - len(image)))
                else:
                    del image[target_size:]
                for page_number, page_offset in committed_frames:
                    if page_number > committed_page_count:
                        continue
                    start = (page_number - 1) * page_size
                    image[start : start + page_size] = wal_view[
                        page_offset : page_offset + page_size
                    ]

        final_page_size = _sqlite_page_size(image)
        if final_page_size != page_size:
            raise sqlite3.DatabaseError(
                "SQLite WAL changed the database page size"
            )
        final_versions = bytes(image[18:20])
        if final_versions not in {b"\x01\x01", b"\x02\x02"}:
            raise sqlite3.DatabaseError(
                "invalid materialized SQLite header versions"
            )
        if final_versions == b"\x02\x02":
            # The WAL has already been rolled forward.  Mark the detached image
            # as a single-file rollback database so deserialize never looks for
            # a filesystem sidecar.
            image[18:20] = b"\x01\x01"
        return bytes(image)

    @contextmanager
    def _readonly_snapshot(self):
        """Yield stable bytes that will be deserialized without filesystem IO."""

        artifacts = self._readonly_artifacts
        if artifacts is None:
            artifacts = self._capture_database_artifacts()
        self._validate_database_parent_binding()
        try:
            yield artifacts
        finally:
            self._validate_database_parent_binding()

    def _capture_database_artifacts(
        self,
        expected_identity: tuple[int, int] | None = None,
    ) -> dict[str, bytes]:
        """Capture two identical reads so one object has one trusted snapshot."""

        if expected_identity is None:
            expected_identity = self._current_database_identity()
        artifacts: dict[str, bytes] | None = None
        previous = self._read_database_artifacts(expected_identity)
        for _ in range(READONLY_SNAPSHOT_ATTEMPTS):
            current = self._read_database_artifacts(expected_identity)
            if previous is not None and current is not None and current == previous:
                artifacts = current
                break
            previous = current
        if artifacts is None:
            raise sqlite3.OperationalError(
                "study database is busy; could not capture a stable read-only snapshot"
            )
        return artifacts

    @contextmanager
    def _connection(self):
        if self._read_only:
            with self._readonly_snapshot() as artifacts:
                connection = self._connect(readonly_artifacts=artifacts)
                try:
                    yield connection
                finally:
                    connection.close()
        else:
            connection = self._connect()
            try:
                yield connection
            finally:
                connection.close()

    @staticmethod
    def _user_tables(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
            )
        }

    @staticmethod
    def _table_columns(
        connection: sqlite3.Connection,
        table: str,
    ) -> frozenset[str]:
        return frozenset(
            str(row[1])
            for row in connection.execute(f"PRAGMA table_xinfo({table})")
        )

    @staticmethod
    def _primary_key_shape(
        connection: sqlite3.Connection,
        table: str,
    ) -> dict[str, int]:
        return {
            str(row[1]): int(row[5])
            for row in connection.execute(f"PRAGMA table_xinfo({table})")
            if int(row[5]) > 0
        }

    @staticmethod
    def _normalize_default(value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        while normalized.startswith("(") and normalized.endswith(")"):
            normalized = normalized[1:-1].strip()
        return normalized

    def _validate_schema_shape(
        self,
        connection: sqlite3.Connection,
        version: int,
        expected_columns: Mapping[str, frozenset[str]],
    ) -> None:
        metadata = self._metadata(connection)
        attempt_rows = connection.execute("PRAGMA table_xinfo(attempts)").fetchall()
        attempt_names = [str(row[1]) for row in attempt_rows]
        target_default = next(
            self._normalize_default(row[4])
            for row in attempt_rows
            if row[1] == "target_duration_seconds"
        )
        if version == 2:
            if set(metadata) != {"version"}:
                raise ValueError(
                    "incompatible coach database: v2 metadata keys do not match"
                )
            if target_default not in {"45", "60"}:
                raise ValueError(
                    "incompatible coach database: v2 target duration default"
                )
            expected_attempts_sql = (
                _ATTEMPTS_SQL_BEFORE_TARGET
                + _ATTEMPTS_TARGET_SQL.format(default=target_default)
                + _ATTEMPTS_SQL_AFTER_TARGET
                + ")"
            )
        else:
            database_instance = metadata.get(DATABASE_INSTANCE_KEY)
            if not _valid_database_instance(database_instance):
                raise ValueError(
                    "incompatible coach database: database instance marker"
                )
            source_position = attempt_names.index("target_duration_source")
            target_position = attempt_names.index("target_duration_seconds")
            if source_position == target_position + 1:
                if target_default != "45":
                    raise ValueError(
                        "incompatible coach database: fresh v3 target default"
                    )
                if set(metadata) != {
                    "version",
                    DATABASE_OWNER_KEY,
                    DATABASE_INSTANCE_KEY,
                }:
                    raise ValueError(
                        "incompatible coach database: fresh v3 metadata keys"
                    )
                expected_attempts_sql = (
                    _ATTEMPTS_SQL_BEFORE_TARGET
                    + _ATTEMPTS_TARGET_SQL.format(default="45")
                    + _ATTEMPTS_SOURCE_SQL
                    + _ATTEMPTS_SQL_AFTER_TARGET
                    + ")"
                )
            elif source_position == len(attempt_names) - 1:
                if (
                    target_default not in {"45", "60"}
                    or metadata.get(MIGRATED_FROM_VERSION_KEY) != "2"
                    or set(metadata)
                    != {
                        "version",
                        DATABASE_OWNER_KEY,
                        DATABASE_INSTANCE_KEY,
                        MIGRATED_FROM_VERSION_KEY,
                    }
                ):
                    raise ValueError(
                        "incompatible coach database: migrated v3 metadata/default"
                    )
                expected_attempts_sql = (
                    _ATTEMPTS_SQL_BEFORE_TARGET
                    + _ATTEMPTS_TARGET_SQL.format(default=target_default)
                    + _ATTEMPTS_SQL_AFTER_TARGET
                    + _ATTEMPTS_SOURCE_SQL
                    + ")"
                )
            else:
                raise ValueError(
                    "incompatible coach database: target duration column order"
                )

        for table, expected in expected_columns.items():
            rows = connection.execute(f"PRAGMA table_xinfo({table})").fetchall()
            if any(len(row) > 6 and int(row[6]) != 0 for row in rows):
                raise ValueError(
                    f"incompatible coach database: {table} has hidden/generated columns"
                )
            for row in rows:
                column = str(row[1])
                expected_type = (
                    "INTEGER"
                    if column in _INTEGER_COLUMNS
                    else "REAL"
                    if column in _REAL_COLUMNS
                    else "TEXT"
                )
                if str(row[2]).upper() != expected_type:
                    raise ValueError(
                        f"incompatible coach database: {table}.{column} type"
                    )
                expected_not_null = column in _NOT_NULL_COLUMNS[table]
                if bool(row[3]) != expected_not_null:
                    raise ValueError(
                        f"incompatible coach database: {table}.{column} nullability"
                    )
                actual_default = self._normalize_default(row[4])
                if column == "target_duration_seconds":
                    if actual_default not in {"45", "60"}:
                        raise ValueError(
                            "incompatible coach database: target duration default"
                        )
                else:
                    expected_default = _COLUMN_DEFAULTS.get((table, column))
                    if actual_default != expected_default:
                        raise ValueError(
                            f"incompatible coach database: {table}.{column} default"
                        )
            if self._primary_key_shape(connection, table) != _EXPECTED_PRIMARY_KEYS[
                table
            ]:
                raise ValueError(
                    f"incompatible coach database: {table} primary key"
                )

            sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            compact_sql = (
                _compact_schema_sql(str(sql_row[0]))
                if sql_row is not None and sql_row[0] is not None
                else ""
            )
            expected_sql = (
                expected_attempts_sql
                if table == "attempts"
                else _CANONICAL_TABLE_SQL[table]
            )
            if compact_sql != expected_sql:
                raise ValueError(
                    f"incompatible coach database: {table} definition"
                )

        schema_objects = connection.execute(
            """SELECT type, name, tbl_name, sql FROM sqlite_master
               WHERE type IN ('view', 'trigger')
                  OR (type='index' AND sql IS NOT NULL)"""
        ).fetchall()
        forbidden = [
            str(row[1])
            for row in schema_objects
            if row[0] in {"view", "trigger"}
        ]
        if forbidden:
            raise ValueError(
                "incompatible coach database: unexpected trigger/view schema object(s): "
                + ", ".join(sorted(forbidden))
            )
        index_rows = {
            str(row[1]): row
            for row in schema_objects
            if row[0] == "index"
        }
        if set(index_rows) != set(_EXPECTED_EXPLICIT_INDEXES):
            raise ValueError(
                "incompatible coach database: explicit index schema objects do not match"
            )
        for table in _COACH_TABLES:
            if any(
                str(row[3]) == "u"
                for row in connection.execute(f"PRAGMA index_list({table})")
            ):
                raise ValueError(
                    f"incompatible coach database: {table} has unexpected unique index"
                )
        for name, (table, unique, partial, columns) in (
            _EXPECTED_EXPLICIT_INDEXES.items()
        ):
            index_list = {
                str(row[1]): row
                for row in connection.execute(f"PRAGMA index_list({table})")
            }
            row = index_list.get(name)
            if (
                row is None
                or bool(row[2]) != unique
                or bool(row[4]) != partial
            ):
                raise ValueError(
                    f"incompatible coach database: index {name} attributes"
                )
            actual_columns = tuple(
                str(item[2])
                for item in connection.execute(f"PRAGMA index_info({name})")
            )
            if actual_columns != columns:
                raise ValueError(
                    f"incompatible coach database: index {name} columns"
                )
        for name, expected_sql in _EXPECTED_INDEX_SQL.items():
            actual_sql = _compact_schema_sql(str(index_rows[name][3]))
            if actual_sql != expected_sql:
                raise ValueError(
                    f"incompatible coach database: index {name} definition"
                )

        for table, expected in _EXPECTED_FOREIGN_KEYS.items():
            actual = tuple(
                sorted(
                    (
                        str(row[2]),
                        str(row[3]),
                        str(row[4]),
                        str(row[6]).upper(),
                    )
                    for row in connection.execute(
                        f"PRAGMA foreign_key_list({table})"
                    )
                )
            )
            if actual != tuple(sorted(expected)):
                raise ValueError(
                    f"incompatible coach database: {table} foreign keys"
                )

        quick_check = [
            str(row[0]) for row in connection.execute("PRAGMA quick_check")
        ]
        if quick_check != ["ok"]:
            raise ValueError("incompatible coach database: integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("incompatible coach database: foreign key check failed")

    @staticmethod
    def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT key, value FROM schema_meta")
        }

    @staticmethod
    def _legacy_target_default(connection: sqlite3.Connection) -> float:
        for row in connection.execute("PRAGMA table_xinfo(attempts)"):
            if row[1] != "target_duration_seconds":
                continue
            try:
                result = float(row[4])
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "incompatible coach database: target duration default is invalid"
                ) from error
            if result not in {45.0, 60.0}:
                raise ValueError(
                    "incompatible coach database: unsupported target duration default"
                )
            return result
        raise ValueError(
            "incompatible coach database: attempts is missing target duration"
        )

    def _inspect_existing_database(
        self,
        connection: sqlite3.Connection,
    ) -> int | None:
        tables = self._user_tables(connection)
        if not tables:
            return None
        if "schema_meta" not in tables:
            raise ValueError(
                "not a coach database: existing SQLite file has no owner metadata"
            )
        if self._table_columns(connection, "schema_meta") != _V2_TABLE_COLUMNS[
            "schema_meta"
        ]:
            raise ValueError(
                "incompatible coach database: schema_meta columns do not match"
            )
        if self._primary_key_shape(connection, "schema_meta") != (
            _EXPECTED_PRIMARY_KEYS["schema_meta"]
        ):
            raise ValueError(
                "incompatible coach database: schema_meta primary key"
            )
        metadata = self._metadata(connection)
        version_text = metadata.get("version")
        if version_text is None:
            raise ValueError("incompatible coach database: schema version is missing")
        try:
            version = int(version_text)
        except (TypeError, ValueError) as error:
            raise ValueError("database schema version is invalid") from error

        owner = metadata.get(DATABASE_OWNER_KEY)
        if owner is not None and owner != DATABASE_OWNER:
            raise ValueError(
                "database owner marker does not match the study coach"
            )
        if version > SCHEMA_VERSION:
            raise ValueError(
                "database uses newer schema version "
                f"{version}; this coach supports version {SCHEMA_VERSION}"
            )

        unexpected_tables = tables - _COACH_TABLES
        if unexpected_tables:
            raise ValueError(
                "not a coach database: unexpected table(s): "
                + ", ".join(sorted(unexpected_tables))
            )
        missing_tables = _COACH_TABLES - tables
        if missing_tables:
            raise ValueError(
                "incompatible coach database: missing table(s): "
                + ", ".join(sorted(missing_tables))
            )

        if version == 2:
            expected_columns = _V2_TABLE_COLUMNS
        elif version == SCHEMA_VERSION:
            if owner is None:
                raise ValueError("database owner marker is missing")
            expected_columns = _V3_TABLE_COLUMNS
        else:
            raise ValueError(
                f"unsupported coach database schema version: {version}"
            )
        for table, expected in expected_columns.items():
            actual = self._table_columns(connection, table)
            if actual != expected:
                missing = sorted(expected - actual)
                unexpected = sorted(actual - expected)
                detail = []
                if missing:
                    detail.append("missing " + ", ".join(missing))
                if unexpected:
                    detail.append("unexpected " + ", ".join(unexpected))
                raise ValueError(
                    f"incompatible coach database: {table} columns "
                    + "; ".join(detail)
                )
        self._validate_schema_shape(connection, version, expected_columns)
        if version == 2:
            self._legacy_target_default(connection)
        return version

    def _validate_readonly_database(self) -> None:
        with self._connection() as connection:
            version = self._inspect_existing_database(connection)
            database_instance = (
                self._metadata(connection).get(DATABASE_INSTANCE_KEY)
                if version == SCHEMA_VERSION
                else None
            )
        if version is None:
            raise ValueError("incompatible coach database: database is empty")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"database schema version {version} requires migration; "
                "run init to migrate before opening read-only"
            )
        if not _valid_database_instance(database_instance):
            raise ValueError("database instance marker is invalid")
        self._database_instance_id = database_instance

    def _validate_writable_connection(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        if self._inspect_existing_database(connection) != SCHEMA_VERSION:
            raise ValueError("writable study database is not current schema")
        database_instance = self._metadata(connection).get(DATABASE_INSTANCE_KEY)
        if (
            self._database_instance_id is None
            or database_instance != self._database_instance_id
        ):
            raise ValueError(
                "study database was replaced after validation; "
                "database instance marker does not match"
            )

    def _validate_active_database_binding(
        self,
        expected_identity: tuple[int, int],
        expected_instance: str,
    ) -> None:
        """Verify that a committed write still belongs to the active path.

        SQLite keeps writing to an opened inode even if its directory entry is
        renamed.  Rechecking both the path identity and a private read-only
        snapshot prevents a successful receipt from being returned for a
        database that is no longer at the configured path.
        """

        self._validate_writable_sidecars()
        if self._current_database_identity() != expected_identity:
            raise ValueError(
                "study database path was replaced during the write; "
                "active database identity does not match"
            )
        try:
            with self._readonly_snapshot() as artifacts:
                connection = self._connect(readonly_artifacts=artifacts)
                try:
                    version = self._inspect_existing_database(connection)
                    active_instance = self._metadata(connection).get(
                        DATABASE_INSTANCE_KEY
                    )
                finally:
                    connection.close()
        except (OSError, sqlite3.Error, ValueError) as error:
            raise ValueError(
                "active study database binding could not be verified"
            ) from error
        if version != SCHEMA_VERSION or active_instance != expected_instance:
            raise ValueError(
                "study database path was replaced during the write; "
                "active database instance does not match"
            )
        if self._current_database_identity() != expected_identity:
            raise ValueError(
                "study database path was replaced during binding verification"
            )
        self._validate_writable_sidecars()

    def _validate_active_path_identity(
        self,
        expected_identity: tuple[int, int],
    ) -> None:
        self._validate_writable_sidecars()
        if self._current_database_identity() != expected_identity:
            raise ValueError(
                "study database path was replaced before commit; "
                "active database identity does not match"
            )

    def initialize(self) -> dict[str, Any]:
        if self._read_only:
            raise ValueError("read-only stores cannot initialize the database")
        self._validate_writable_sidecars()
        statements = (
            "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            f"""CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                user_answer TEXT,
                correct_answer TEXT,
                is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
                duration_seconds REAL CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
                target_duration_seconds {_TARGET_DURATION_COLUMN},
                target_duration_source {_TARGET_DURATION_SOURCE_COLUMN},
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
        self._validate_database_parent_binding()
        source_existed = os.path.lexists(self.database)
        preflight_version: int | None = None
        preflight_instance: str | None = None
        preflight_identity: tuple[int, int] | None = None
        if not source_existed:
            (
                preflight_identity,
                preflight_instance,
            ) = self._create_new_database(statements)
            preflight_version = SCHEMA_VERSION
        else:
            preflight_identity = self._current_database_identity()
            # Inspect a private byte snapshot first.  Opening an untrusted
            # active-WAL database directly can update its -shm read marks even
            # when schema validation subsequently rejects it.
            with self._readonly_snapshot() as artifacts:
                preflight = self._connect(readonly_artifacts=artifacts)
                try:
                    preflight_version = self._inspect_existing_database(
                        preflight
                    )
                    if preflight_version == SCHEMA_VERSION:
                        preflight_instance = self._metadata(preflight).get(
                            DATABASE_INSTANCE_KEY
                        )
                finally:
                    preflight.close()
            if self._current_database_identity() != preflight_identity:
                raise ValueError(
                    "database identity changed after preflight inspection"
                )
            if preflight_version is None:
                raise ValueError(
                    "incompatible coach database: existing empty or foreign "
                    "database is not owned by this coach"
                )

        self._database_identity = preflight_identity
        connection = self._connect()
        write_target_identity = self._current_database_identity()
        try:
            if (
                preflight_identity is not None
                and write_target_identity != preflight_identity
            ):
                raise ValueError(
                    "database identity changed before initialization"
                )
            existing_version = self._inspect_existing_database(connection)
            if existing_version != preflight_version:
                raise ValueError("database schema changed after preflight inspection")
            existing_instance = (
                self._metadata(connection).get(DATABASE_INSTANCE_KEY)
                if existing_version == SCHEMA_VERSION
                else None
            )
            if existing_version == SCHEMA_VERSION and (
                existing_instance != preflight_instance
            ):
                raise ValueError(
                    "database instance changed after preflight inspection"
                )
            database_instance_id = (
                existing_instance
                if existing_version == SCHEMA_VERSION
                else secrets.token_hex(16)
            )
            legacy_default = (
                self._legacy_target_default(connection)
                if existing_version == 2
                else None
            )
            connection.execute("BEGIN IMMEDIATE")
            if self._inspect_existing_database(connection) != existing_version:
                raise ValueError("database schema changed during initialization")
            for statement in statements:
                connection.execute(statement)
            if existing_version == 2:
                connection.execute(
                    "ALTER TABLE attempts ADD COLUMN "
                    f"target_duration_source {_TARGET_DURATION_SOURCE_COLUMN}"
                )
                if legacy_default == 60.0:
                    matching_source = "legacy_unknown"
                else:
                    matching_source = "default"
                connection.execute(
                    """UPDATE attempts
                       SET target_duration_source = CASE
                           WHEN target_duration_seconds = ? THEN ?
                           ELSE 'explicit'
                       END""",
                    (legacy_default, matching_source),
                )
            metadata_updates = [
                (DATABASE_OWNER_KEY, DATABASE_OWNER),
                (DATABASE_INSTANCE_KEY, database_instance_id),
                ("version", str(SCHEMA_VERSION)),
            ]
            if existing_version == 2:
                metadata_updates.append((MIGRATED_FROM_VERSION_KEY, "2"))
            for key, value in metadata_updates:
                connection.execute(
                    """INSERT INTO schema_meta(key, value) VALUES(?, ?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (key, value),
                )
            for table, expected in _V3_TABLE_COLUMNS.items():
                if self._table_columns(connection, table) != expected:
                    raise ValueError(
                        f"incompatible coach database after migration: {table}"
                    )
            self._validate_schema_shape(
                connection,
                SCHEMA_VERSION,
                _V3_TABLE_COLUMNS,
            )
            self._validate_active_path_identity(write_target_identity)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._validate_active_database_binding(
            write_target_identity,
            database_instance_id,
        )
        return {
            "ok": True,
            "database": str(self.database),
            "object_id": f"schema-v{SCHEMA_VERSION}",
            "schema_version": SCHEMA_VERSION,
            "database_instance_id": database_instance_id,
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
            if raw not in ERROR_CODES:
                available = ", ".join(sorted(ERROR_CODES))
                raise ValueError(
                    f"unknown error code: {raw}; available error codes: {available}"
                )
            if raw not in errors:
                errors.append(raw)
        return errors

    @classmethod
    def validate_attempt_payload(
        cls,
        payload: dict[str, Any],
        known_knowledge_ids: Iterable[str] | Mapping[str, float],
    ) -> dict[str, Any]:
        """Validate and normalize a record payload without filesystem access."""

        raw_ids = (
            known_knowledge_ids.keys()
            if isinstance(known_knowledge_ids, Mapping)
            else known_knowledge_ids
        )
        validator = cls.__new__(cls)
        validator.known_knowledge_ids = frozenset(str(item) for item in raw_ids)
        if not validator.known_knowledge_ids:
            raise ValueError("known knowledge ids must not be empty")
        return validator.record_attempt(payload, _validate_only=True)

    def record_attempt(
        self,
        payload: dict[str, Any],
        *,
        _validate_only: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(_validate_only, bool):
            raise ValueError("_validate_only must be a boolean")
        if not isinstance(payload, dict):
            raise ValueError("attempt payload must be a JSON object")
        unknown_fields = sorted(set(payload) - ATTEMPT_PAYLOAD_FIELDS)
        if unknown_fields:
            raise ValueError(
                "unknown attempt field(s): " + ", ".join(unknown_fields)
            )
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
        user_answer, normalized_user_answer = _answer(
            payload.get("user_answer"), "user_answer"
        )
        correct_answer, normalized_correct_answer = _answer(
            payload.get("correct_answer"), "correct_answer"
        )
        answers_match = normalized_user_answer == normalized_correct_answer
        if is_correct != answers_match:
            raise ValueError("is_correct contradicts normalized answers")
        if not is_correct and not errors:
            raise ValueError(
                "wrong attempt requires at least one root-cause error code"
            )
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
        target_duration_is_explicit = "target_duration_seconds" in payload
        target_duration = payload.get(
            "target_duration_seconds", DEFAULT_TARGET_DURATION_SECONDS
        )
        target_duration = _finite_number(
            target_duration, "target_duration_seconds"
        )
        if target_duration <= 0:
            raise ValueError("target_duration_seconds must be a positive number")
        target_duration_source = (
            "explicit" if target_duration_is_explicit else "default"
        )
        difficulty = payload.get("difficulty")
        mode = payload.get("mode")
        for field_name, value in (("difficulty", difficulty), ("mode", mode)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string when provided")
        optional_text = {}
        for field_name in (
            "method_used",
            "recommended_method",
            "error_path",
            "correction_rule",
            "source_ref",
        ):
            value = payload.get(field_name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string when provided")
            optional_text[field_name] = value
        exact_method_json = _json_dump(payload.get("exact_method"))
        fast_method_json = _json_dump(payload.get("fast_method"))
        priority = (
            "high"
            if (not is_correct and confidence is not None and confidence >= 4)
            else "normal"
        )

        if _validate_only:
            return {
                "ok": True,
                "knowledge_ids": knowledge_ids,
                "errors": errors,
                "priority": priority,
            }

        created_at = _utc_now()
        next_review_date: str | None = None
        review_id: int | None = None
        if (
            self._database_identity is None
            or self._database_instance_id is None
        ):
            raise ValueError("writable study database binding is unavailable")
        connection = self._connect()
        try:
            self._validate_writable_connection(connection)
            connection.execute("BEGIN IMMEDIATE")
            self._validate_writable_connection(connection)
            cursor = connection.execute(
                """
                INSERT INTO attempts(
                    question, user_answer, correct_answer, is_correct,
                    duration_seconds, target_duration_seconds,
                    target_duration_source, confidence, difficulty, mode,
                    parse_confidence, method_used, recommended_method,
                    method_quality, error_path, correction_rule, source_ref,
                    traceability, priority, is_transfer, exact_method_json,
                    fast_method_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    question,
                    user_answer,
                    correct_answer,
                    int(is_correct),
                    duration,
                    target_duration,
                    target_duration_source,
                    confidence,
                    difficulty,
                    mode,
                    parse_confidence,
                    optional_text["method_used"],
                    optional_text["recommended_method"],
                    method_quality,
                    optional_text["error_path"],
                    optional_text["correction_rule"],
                    optional_text["source_ref"],
                    traceability,
                    priority,
                    int(is_transfer),
                    exact_method_json,
                    fast_method_json,
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
            self._validate_active_path_identity(self._database_identity)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._validate_active_database_binding(
            self._database_identity,
            self._database_instance_id,
        )
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
        target_source = result["target_duration_source"]
        if target_source not in TARGET_DURATION_SOURCES:
            raise ValueError(
                f"invalid target duration source in attempt {result['id']}: "
                f"{target_source}"
            )
        result["effective_target_duration_seconds"] = (
            DEFAULT_TARGET_DURATION_SECONDS
            if target_source in {"default", "legacy_unknown"}
            else float(result["target_duration_seconds"])
        )
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
                SELECT r.id AS review_id, r.attempt_id, r.result, r.status,
                       r.due_date, r.reviewed_on, r.interval_days, r.round,
                       r.interval_qualified, r.created_at,
                       a.question, a.priority
                FROM reviews r JOIN attempts a ON a.id=r.attempt_id
                WHERE r.status='scheduled' AND r.due_date<=?
                ORDER BY r.due_date, r.id
                """,
                (target,),
            ).fetchall()
            reviews = []
            for row in rows:
                review = dict(row)
                attempt_id = review["attempt_id"]
                review["knowledge_ids"] = [
                    item[0]
                    for item in connection.execute(
                        """SELECT knowledge_id FROM attempt_tags
                            WHERE attempt_id=? ORDER BY position""",
                        (attempt_id,),
                    ).fetchall()
                ]
                review["errors"] = [
                    item[0]
                    for item in connection.execute(
                        """SELECT error_code FROM attempt_errors
                            WHERE attempt_id=? ORDER BY position""",
                        (attempt_id,),
                    ).fetchall()
                ]
                reviews.append(review)
            return reviews

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
        if (
            self._database_identity is None
            or self._database_instance_id is None
        ):
            raise ValueError("writable study database binding is unavailable")
        connection = self._connect()
        try:
            self._validate_writable_connection(connection)
            connection.execute("BEGIN IMMEDIATE")
            self._validate_writable_connection(connection)
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
            self._validate_active_path_identity(self._database_identity)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._validate_active_database_binding(
            self._database_identity,
            self._database_instance_id,
        )
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

    def _node_progress(
        self,
        knowledge_id: str | None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        query = "SELECT DISTINCT a.* FROM attempts a"
        conditions = [
            "a.traceability='high'",
            "(a.parse_confidence IS NULL OR a.parse_confidence>=?)",
            """NOT EXISTS (
                SELECT 1 FROM attempt_errors evidence_error
                WHERE evidence_error.attempt_id=a.id
                  AND evidence_error.error_code IN ('missing_question', 'parse_uncertain')
            )""",
        ]
        parameters: list[Any] = [PROGRESS_MIN_PARSE_CONFIDENCE]
        if knowledge_id is not None:
            query += " JOIN attempt_tags t ON t.attempt_id=a.id"
            conditions.extend(["t.knowledge_id=?", "t.position=0"])
            parameters.append(knowledge_id)
        query += " WHERE " + " AND ".join(conditions) + " ORDER BY a.id"
        connection_context = (
            self._connection()
            if connection is None
            else nullcontext(connection)
        )
        with connection_context as connection:
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
            target_source_counts = {
                source: sum(row["target_duration_source"] == source for row in rows)
                for source in sorted(TARGET_DURATION_SOURCES)
                if any(row["target_duration_source"] == source for row in rows)
            }
            targets = [
                (
                    DEFAULT_TARGET_DURATION_SECONDS
                    if row["target_duration_source"]
                    in {"default", "legacy_unknown"}
                    else float(row["target_duration_seconds"])
                )
                for row in rows
            ]
            target_duration = (
                statistics.median(targets)
                if targets
                else DEFAULT_TARGET_DURATION_SECONDS
            )
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
            "target_duration_source_counts": target_source_counts,
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
                "target_source_counts": target_source_counts,
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

    def _progress_from_connection(
        self,
        knowledge_id: str | None,
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        if knowledge_id is not None:
            if knowledge_id not in self.known_knowledge_ids:
                raise ValueError(f"unknown knowledge id: {knowledge_id}")
            return self._node_progress(
                knowledge_id,
                connection=connection,
            )

        aggregate = self._node_progress(None, connection=connection)
        nodes = [
            self._node_progress(node_id, connection=connection)
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

    def progress(self, knowledge_id: str | None = None) -> dict[str, Any]:
        if (
            knowledge_id is not None
            and knowledge_id not in self.known_knowledge_ids
        ):
            raise ValueError(f"unknown knowledge id: {knowledge_id}")
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                return self._progress_from_connection(
                    knowledge_id,
                    connection,
                )
            finally:
                connection.rollback()

    def export_json(
        self,
        output: str | Path,
        overwrite: bool = False,
        *,
        path_already_resolved: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(overwrite, bool) or not isinstance(
            path_already_resolved, bool
        ):
            raise ValueError(
                "overwrite and path_already_resolved must be booleans"
            )
        destination = (
            _absolute_lexical_path(output)
            if path_already_resolved
            else Path(output).expanduser().resolve()
        )
        active_database_artifacts = {
            self.database,
            *(
                Path(f"{self.database}{suffix}")
                for suffix in SQLITE_SIDECAR_SUFFIXES
            ),
        }
        if any(
            paths_alias(destination, artifact)
            for artifact in active_database_artifacts
        ):
            raise ValueError("export output cannot be the active database path")
        _secure_mkdir_parents(destination.parent)
        if destination.parent.resolve() != destination.parent:
            raise ValueError(
                "export destination parent is not the validated canonical directory"
            )
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
            "schema_version": SCHEMA_VERSION,
            "exported_at": _utc_now(),
            "database": str(self.database),
            "attempts": attempts,
            "reviews": reviews,
            "sessions": sessions,
        }
        temporary_name: str | None = None
        temporary_fd: int | None = None
        directory_fd: int | None = None
        try:
            if destination.parent.resolve() != destination.parent:
                raise ValueError("export destination parent changed after validation")
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
            directory_fd = os.open(destination.parent, directory_flags)
            opened_status = os.fstat(directory_fd)
            current_status = destination.parent.stat()
            opened_identity = (opened_status.st_dev, opened_status.st_ino)
            if opened_identity != (current_status.st_dev, current_status.st_ino):
                raise ValueError("export destination parent identity changed")

            if not overwrite:
                try:
                    os.stat(
                        destination.name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError(
                        f"export destination already exists: {destination}"
                    )

            for _ in range(32):
                candidate = (
                    f".{destination.name}.{secrets.token_hex(8)}.tmp"
                )
                try:
                    temporary_fd = os.open(
                        candidate,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=directory_fd,
                    )
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
            else:
                raise FileExistsError("could not allocate an export temp file")

            created = os.fstat(temporary_fd)
            if not stat.S_ISREG(created.st_mode) or created.st_nlink != 1:
                raise ValueError("temporary export is not a private regular file")
            created_identity = int(created.st_dev), int(created.st_ino)

            with os.fdopen(
                os.dup(temporary_fd),
                mode="w",
                encoding="utf-8",
            ) as temporary:
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

            temporary_status = os.fstat(temporary_fd)
            temporary_path_status = os.stat(
                temporary_name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(temporary_status.st_mode)
                or temporary_status.st_nlink != 1
                or (
                    int(temporary_status.st_dev),
                    int(temporary_status.st_ino),
                )
                != created_identity
                or (
                    int(temporary_path_status.st_dev),
                    int(temporary_path_status.st_ino),
                )
                != created_identity
            ):
                raise ValueError("temporary export identity changed before commit")

            commit_parent_status = destination.parent.stat()
            if (
                destination.parent.resolve() != destination.parent
                or (
                    commit_parent_status.st_dev,
                    commit_parent_status.st_ino,
                )
                != opened_identity
            ):
                raise ValueError("export destination parent changed before commit")
            if overwrite:
                os.replace(
                    temporary_name,
                    destination.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                temporary_name = None
            else:
                try:
                    _rename_noreplace(
                        temporary_name,
                        destination.name,
                        directory_fd=directory_fd,
                    )
                except FileExistsError as error:
                    raise FileExistsError(
                        f"export destination already exists: {destination}"
                    ) from error
                temporary_name = None
            published_status = os.stat(
                destination.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            open_status = os.fstat(temporary_fd)
            if (
                not stat.S_ISREG(published_status.st_mode)
                or (
                    int(published_status.st_dev),
                    int(published_status.st_ino),
                )
                != created_identity
                or (
                    int(open_status.st_dev),
                    int(open_status.st_ino),
                )
                != created_identity
            ):
                raise ValueError("export publication identity does not match")
            os.fsync(directory_fd)
            try:
                committed_parent_status = destination.parent.stat(
                    follow_symlinks=False
                )
                committed_parent_resolved = destination.parent.resolve(
                    strict=True
                )
            except OSError as error:
                raise ValueError(
                    "export destination parent changed during commit"
                ) from error
            if (
                not stat.S_ISDIR(committed_parent_status.st_mode)
                or committed_parent_resolved != destination.parent
                or (
                    committed_parent_status.st_dev,
                    committed_parent_status.st_ino,
                )
                != opened_identity
            ):
                raise ValueError(
                    "export destination parent changed during commit"
                )
        finally:
            # A failed pathname is retained instead of unlinked: another
            # process may have replaced that random name after validation.
            if temporary_fd is not None:
                os.close(temporary_fd)
            if directory_fd is not None:
                os.close(directory_fd)
        return {
            "ok": True,
            "database": str(self.database),
            "object_id": str(destination),
            "output": str(destination),
            "attempt_count": len(attempts),
            "review_count": len(reviews),
            "session_count": len(sessions),
        }
