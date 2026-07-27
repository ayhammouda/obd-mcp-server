"""SQLite persistence for diagnostic issues and their audit timeline."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from .domain import (
    DiagnosticIssue,
    IssueSeverity,
    IssueStatus,
    IssueTimeline,
    TimelineEvent,
    TimelineEventType,
    utc_now,
)
from .errors import IssueNotFoundError, ServiceClosedError, StorageError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    dtc_codes_json TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS timeline_events (
    event_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id) ON DELETE CASCADE,
    vehicle_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS timeline_events_issue_time
    ON timeline_events(issue_id, occurred_at, event_id);
"""
_SCHEMA_VERSION = 1
T = TypeVar("T")
_FileIdentity = tuple[int, int]
_DatabaseIdentity = tuple[_FileIdentity, _FileIdentity]


class SQLiteIssueStore:
    """Small async facade over a private-permission SQLite database."""

    def __init__(self, path: str | Path) -> None:
        self._memory = str(path) == ":memory:"
        expanded = Path(path).expanduser()
        if not expanded.is_absolute():
            expanded = Path.cwd() / expanded
        # Do not resolve here: preserving the final path component is required
        # so _prepare_database_file can reject symlinks before SQLite opens it.
        self._path = None if self._memory else expanded
        self._connection: sqlite3.Connection | None = None
        self._operation_lock = asyncio.Lock()
        self._database_identity: _DatabaseIdentity | None = None
        self._closed = False

    @property
    def path(self) -> Path | None:
        return self._path

    def _is_initialized(self) -> bool:
        return self._connection is not None

    async def initialize(self) -> None:
        async with self._operation_lock:
            await self._initialize_locked()

    async def _initialize_locked(self) -> None:
        if self._closed:
            raise ServiceClosedError("issue store is closed")
        if self._is_initialized():
            return

        async def initialize_in_background() -> None:
            connection, identity = await asyncio.to_thread(self._initialize_sync)
            self._connection = connection
            self._database_identity = identity

        task = asyncio.create_task(initialize_in_background())
        try:
            await self._drain_task_on_cancellation(task)
        except StorageError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise StorageError("failed to initialize issue database") from exc

    def _initialize_sync(self) -> tuple[sqlite3.Connection, _DatabaseIdentity | None]:
        if self._memory:
            identity = None
            database = ":memory:"
        else:
            assert self._path is not None
            identity = self._prepare_database_file()
            database = os.fspath(self._path)
        connection = sqlite3.connect(database, check_same_thread=False, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            if identity is not None:
                self._verify_database_identity(identity)
            version_row = connection.execute("PRAGMA user_version").fetchone()
            version = int(version_row[0])
            if version > _SCHEMA_VERSION:
                raise StorageError(
                    f"issue database schema version {version} is newer than supported "
                    f"version {_SCHEMA_VERSION}"
                )
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            migration = f"PRAGMA user_version = {_SCHEMA_VERSION};\n" if version == 0 else ""
            connection.executescript(f"BEGIN IMMEDIATE;\n{_SCHEMA}\n{migration}COMMIT;")
            self._harden_permissions(identity)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()
            raise
        return connection, identity

    @staticmethod
    def _identity(file_stat: os.stat_result) -> _FileIdentity:
        return file_stat.st_dev, file_stat.st_ino

    def _reject_symlinked_parent_components(self) -> None:
        assert self._path is not None
        parent = self._path.parent
        for candidate in (parent, *parent.parents):
            try:
                candidate_stat = candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise StorageError("failed to inspect issue database parent directory") from exc
            if stat.S_ISLNK(candidate_stat.st_mode):
                raise StorageError(
                    "issue database parent must be a directory with no symlink components"
                )

    def _inspect_parent_directory(self) -> _FileIdentity:
        assert self._path is not None
        parent = self._path.parent
        self._reject_symlinked_parent_components()
        try:
            parent_stat = parent.lstat()
        except OSError as exc:
            raise StorageError("failed to inspect issue database parent directory") from exc
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise StorageError(
                "issue database parent must be a directory with no symlink components"
            )
        if parent_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise StorageError("issue database parent must not be group/world-writable")
        return self._identity(parent_stat)

    def _prepare_parent_directory(self) -> _FileIdentity:
        assert self._path is not None
        self._reject_symlinked_parent_components()
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError("failed to create issue database parent directory") from exc
        return self._inspect_parent_directory()

    def _prepare_database_file(self) -> _DatabaseIdentity:
        assert self._path is not None
        parent_identity = self._prepare_parent_directory()
        try:
            existing_stat = self._path.lstat()
        except FileNotFoundError:
            existing_stat = None
        except OSError as exc:
            raise StorageError("failed to inspect issue database path") from exc
        if existing_stat is not None and (
            stat.S_ISLNK(existing_stat.st_mode) or not stat.S_ISREG(existing_stat.st_mode)
        ):
            raise StorageError("issue database path must be a regular, non-symlink file")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise StorageError("failed to securely open issue database file") from exc
        try:
            descriptor_stat = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_stat.st_mode):
                raise StorageError("issue database path must be a regular, non-symlink file")
            try:
                path_stat = self._path.lstat()
            except OSError as exc:
                raise StorageError("issue database file changed while it was being opened") from exc
            database_identity = self._identity(descriptor_stat)
            if (
                stat.S_ISLNK(path_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or self._identity(path_stat) != database_identity
            ):
                raise StorageError("issue database file changed while it was being opened")
            if self._inspect_parent_directory() != parent_identity:
                raise StorageError("issue database parent changed while it was being opened")
            try:
                os.fchmod(descriptor, 0o600)
            except OSError as exc:
                raise StorageError("failed to restrict issue database file permissions") from exc
        finally:
            os.close(descriptor)
        identity = (parent_identity, database_identity)
        self._verify_database_identity(identity)
        return identity

    def _verify_database_identity(self, identity: _DatabaseIdentity) -> None:
        assert self._path is not None
        expected_parent, expected_database = identity
        if self._inspect_parent_directory() != expected_parent:
            raise StorageError("issue database parent changed while the database was in use")
        try:
            database_stat = self._path.lstat()
        except OSError as exc:
            raise StorageError("issue database file changed while the database was in use") from exc
        if (
            stat.S_ISLNK(database_stat.st_mode)
            or not stat.S_ISREG(database_stat.st_mode)
            or self._identity(database_stat) != expected_database
        ):
            raise StorageError("issue database file changed while the database was in use")

    def _harden_file(
        self,
        candidate: Path,
        *,
        expected_identity: _FileIdentity | None = None,
    ) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(candidate, flags)
        except FileNotFoundError as exc:
            if expected_identity is not None:
                raise StorageError(
                    f"{candidate.name} disappeared while the database was in use"
                ) from exc
            return
        except OSError as exc:
            raise StorageError(f"failed to securely open {candidate.name}") from exc
        try:
            descriptor_stat = os.fstat(descriptor)
            try:
                path_stat = candidate.lstat()
            except OSError as exc:
                raise StorageError(
                    f"{candidate.name} changed while permissions were restricted"
                ) from exc
            descriptor_identity = self._identity(descriptor_stat)
            if (
                not stat.S_ISREG(descriptor_stat.st_mode)
                or stat.S_ISLNK(path_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or self._identity(path_stat) != descriptor_identity
                or (expected_identity is not None and descriptor_identity != expected_identity)
            ):
                raise StorageError(f"{candidate.name} changed while permissions were restricted")
            try:
                os.fchmod(descriptor, 0o600)
            except OSError as exc:
                raise StorageError(f"failed to restrict permissions for {candidate.name}") from exc
        finally:
            os.close(descriptor)

    def _harden_permissions(self, identity: _DatabaseIdentity | None = None) -> None:
        if self._path is None:
            return
        effective_identity = identity or self._database_identity
        if effective_identity is not None:
            self._verify_database_identity(effective_identity)
        database_identity = effective_identity[1] if effective_identity is not None else None
        for candidate in (
            self._path,
            Path(f"{self._path}-journal"),
            Path(f"{self._path}-wal"),
            Path(f"{self._path}-shm"),
        ):
            expected_identity = database_identity if candidate == self._path else None
            self._harden_file(candidate, expected_identity=expected_identity)
        if effective_identity is not None:
            self._verify_database_identity(effective_identity)

    @staticmethod
    async def _drain_task_on_cancellation(task: asyncio.Task[T]) -> T:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise cancellation

    async def _run(self, operation: Callable[..., T], *args: Any) -> T:
        async with self._operation_lock:
            await self._initialize_locked()

            def execute_sync() -> T:
                result = operation(*args)
                self._harden_permissions()
                return result

            task = asyncio.create_task(asyncio.to_thread(execute_sync))
            try:
                return await self._drain_task_on_cancellation(task)
            except (IssueNotFoundError, StorageError):
                raise
            except sqlite3.Error as exc:
                raise StorageError("issue database operation failed") from exc

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StorageError("issue database is not initialized")
        return self._connection

    @staticmethod
    def _issue_from_row(row: sqlite3.Row) -> DiagnosticIssue:
        return DiagnosticIssue(
            issue_id=row["issue_id"],
            vehicle_id=row["vehicle_id"],
            title=row["title"],
            description=row["description"],
            severity=row["severity"],
            status=row["status"],
            dtc_codes=tuple(json.loads(row["dtc_codes_json"])),
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> TimelineEvent:
        return TimelineEvent(
            event_id=row["event_id"],
            issue_id=row["issue_id"],
            vehicle_id=row["vehicle_id"],
            event_type=row["event_type"],
            occurred_at=row["occurred_at"],
            message=row["message"],
            details=json.loads(row["details_json"]),
        )

    def _insert_event_sync(self, event: TimelineEvent) -> None:
        connection = self._require_connection()
        connection.execute(
            """
            INSERT INTO timeline_events (
                event_id, issue_id, vehicle_id, event_type,
                occurred_at, message, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.issue_id,
                event.vehicle_id,
                event.event_type.value,
                event.occurred_at.isoformat(),
                event.message,
                json.dumps(event.details, separators=(",", ":"), sort_keys=True),
            ),
        )

    async def open_issue(
        self,
        vehicle_id: str,
        title: str,
        description: str | None = None,
        *,
        severity: IssueSeverity = IssueSeverity.MEDIUM,
        dtc_codes: Sequence[str] = (),
        issue_id: str | None = None,
    ) -> DiagnosticIssue:
        issue = DiagnosticIssue(
            issue_id=issue_id or f"issue-{uuid.uuid4().hex}",
            vehicle_id=vehicle_id,
            title=title,
            description=description or "",
            severity=severity,
            dtc_codes=tuple(dtc_codes),
        )
        return await self._run(self._open_issue_sync, issue)

    def _open_issue_sync(self, issue: DiagnosticIssue) -> DiagnosticIssue:
        connection = self._require_connection()
        event = TimelineEvent(
            event_id=f"event-{uuid.uuid4().hex}",
            issue_id=issue.issue_id,
            vehicle_id=issue.vehicle_id,
            event_type=TimelineEventType.OPENED,
            occurred_at=issue.opened_at,
            message=f"Issue opened: {issue.title}",
            details={"severity": issue.severity.value, "dtc_codes": list(issue.dtc_codes)},
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO issues (
                    issue_id, vehicle_id, title, description, severity, status,
                    dtc_codes_json, opened_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    issue.issue_id,
                    issue.vehicle_id,
                    issue.title,
                    issue.description,
                    issue.severity.value,
                    issue.status.value,
                    json.dumps(issue.dtc_codes, separators=(",", ":")),
                    issue.opened_at.isoformat(),
                    None,
                ),
            )
            self._insert_event_sync(event)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        return issue

    async def get_issue(self, issue_id: str) -> DiagnosticIssue:
        return await self._run(self._get_issue_sync, issue_id)

    def _get_issue_sync(self, issue_id: str) -> DiagnosticIssue:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM issues WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        if row is None:
            raise IssueNotFoundError(f"issue not found: {issue_id}")
        return self._issue_from_row(row)

    async def append_event(
        self,
        issue_id: str,
        event_type: TimelineEventType,
        *,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> TimelineEvent:
        if event_type is not TimelineEventType.NOTE:
            raise StorageError("only note events may be appended directly")
        return await self._run(
            self._append_event_sync,
            issue_id,
            event_type,
            message,
            details or {},
        )

    def _append_event_sync(
        self,
        issue_id: str,
        event_type: TimelineEventType,
        message: str,
        details: dict[str, Any],
    ) -> TimelineEvent:
        issue = self._get_issue_sync(issue_id)
        event = TimelineEvent(
            event_id=f"event-{uuid.uuid4().hex}",
            issue_id=issue_id,
            vehicle_id=issue.vehicle_id,
            event_type=event_type,
            message=message,
            details=details,
        )
        self._insert_event_sync(event)
        return event

    async def get_timeline(self, issue_id: str) -> IssueTimeline:
        return await self._run(self._get_timeline_sync, issue_id)

    def _get_timeline_sync(self, issue_id: str) -> IssueTimeline:
        issue = self._get_issue_sync(issue_id)
        connection = self._require_connection()
        rows = connection.execute(
            """
            SELECT * FROM timeline_events
            WHERE issue_id = ?
            ORDER BY occurred_at ASC, event_id ASC
            """,
            (issue_id,),
        ).fetchall()
        return IssueTimeline(issue=issue, events=tuple(self._event_from_row(row) for row in rows))

    async def close_issue(self, issue_id: str, *, message: str = "Issue closed") -> DiagnosticIssue:
        return await self._run(self._close_issue_sync, issue_id, message)

    def _close_issue_sync(self, issue_id: str, message: str) -> DiagnosticIssue:
        issue = self._get_issue_sync(issue_id)
        if issue.status == IssueStatus.CLOSED:
            return issue
        closed_at = utc_now()
        closed = issue.model_copy(update={"status": IssueStatus.CLOSED, "closed_at": closed_at})
        event = TimelineEvent(
            event_id=f"event-{uuid.uuid4().hex}",
            issue_id=issue_id,
            vehicle_id=issue.vehicle_id,
            event_type=TimelineEventType.CLOSED,
            occurred_at=closed_at,
            message=message,
            details={"status": IssueStatus.CLOSED.value},
        )
        connection = self._require_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE issues SET status = ?, closed_at = ? WHERE issue_id = ?",
                (IssueStatus.CLOSED.value, closed_at.isoformat(), issue_id),
            )
            self._insert_event_sync(event)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        return closed

    async def close(self) -> None:
        async with self._operation_lock:
            if self._closed:
                return
            connection = self._connection

            async def close_in_background() -> None:
                if connection is not None:
                    try:
                        await asyncio.to_thread(connection.close)
                    except sqlite3.Error as exc:
                        raise StorageError("failed to close issue database") from exc
                self._harden_permissions()
                self._connection = None
                self._database_identity = None
                self._closed = True

            task = asyncio.create_task(close_in_background())
            try:
                await self._drain_task_on_cancellation(task)
            except StorageError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise StorageError("failed to close issue database") from exc
