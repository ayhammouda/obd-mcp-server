from __future__ import annotations

import asyncio
import sqlite3
import stat
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from obd_mcp import storage as storage_module
from obd_mcp.config import load_config
from obd_mcp.domain import IssueSeverity, IssueStatus, TimelineEventType
from obd_mcp.errors import IssueNotFoundError, ServiceClosedError, StorageError
from obd_mcp.storage import SQLiteIssueStore


@pytest.mark.asyncio
async def test_issue_lifecycle_persists_an_ordered_timeline_and_private_modes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private" / "issues.sqlite3"
    store = SQLiteIssueStore(database)
    await store.initialize()

    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700

    issue = await store.open_issue(
        "sim-vehicle-1",
        "Investigate synthetic misfire",
        "Observation only",
        severity=IssueSeverity.HIGH,
        dtc_codes=("p0300", "P0300"),
    )
    assert issue.status is IssueStatus.OPEN
    assert issue.dtc_codes == ("P0300",)

    note = await store.append_event(
        issue.issue_id,
        TimelineEventType.NOTE,
        message="Qualified technician review recommended",
        details={"source": "synthetic"},
    )
    closed = await store.close_issue(issue.issue_id)
    closed_again = await store.close_issue(issue.issue_id)
    timeline = await store.get_timeline(issue.issue_id)

    assert note.event_type is TimelineEventType.NOTE
    assert closed.status is IssueStatus.CLOSED
    assert closed_again.closed_at == closed.closed_at
    assert [event.event_type for event in timeline.events] == [
        TimelineEventType.OPENED,
        TimelineEventType.NOTE,
        TimelineEventType.CLOSED,
    ]

    await store.close()
    reopened = SQLiteIssueStore(database)
    persisted = await reopened.get_timeline(issue.issue_id)
    assert persisted.issue.status is IssueStatus.CLOSED
    assert len(persisted.events) == 3
    await reopened.close()


@pytest.mark.asyncio
async def test_storage_rejects_missing_issues_and_sensitive_timeline_metadata(
    tmp_path: Path,
) -> None:
    store = SQLiteIssueStore(tmp_path / "issues.sqlite3")

    with pytest.raises(IssueNotFoundError):
        await store.get_timeline("missing")

    issue = await store.open_issue("sim", "Issue")
    with pytest.raises(ValidationError, match="VIN"):
        await store.append_event(
            issue.issue_id,
            TimelineEventType.NOTE,
            details={"vin": "A" * 17},
        )
    with pytest.raises(ValidationError, match="VIN"):
        await store.append_event(
            issue.issue_id,
            TimelineEventType.NOTE,
            details={"serial": "1" + "A" * 16},
        )
    await store.close()

    with pytest.raises(ServiceClosedError):
        await store.get_issue(issue.issue_id)


@pytest.mark.asyncio
async def test_storage_refuses_symlink_database_paths(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch(mode=0o600)
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)

    with pytest.raises(StorageError, match="non-symlink"):
        await SQLiteIssueStore(link).initialize()


@pytest.mark.asyncio
async def test_config_loading_preserves_storage_symlink_for_store_validation(
    tmp_path: Path,
) -> None:
    actual_parent = tmp_path / "actual"
    actual_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        "[storage]\npath = 'linked/issues.sqlite3'\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert "linked" in config.storage.path.parts
    with pytest.raises(StorageError, match=r"parent.*symlink"):
        await SQLiteIssueStore(config.storage.path).initialize()
    assert not (actual_parent / "issues.sqlite3").exists()


@pytest.mark.asyncio
async def test_storage_refuses_symlinked_and_writable_parent_directories(
    tmp_path: Path,
) -> None:
    actual_parent = tmp_path / "actual"
    actual_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)

    with pytest.raises(StorageError, match=r"parent.*symlink"):
        await SQLiteIssueStore(linked_parent / "issues.sqlite3").initialize()

    nested_parent = linked_parent / "must-not-be-created"
    with pytest.raises(StorageError, match=r"parent.*symlink"):
        await SQLiteIssueStore(nested_parent / "issues.sqlite3").initialize()
    assert not (actual_parent / nested_parent.name).exists()

    writable_parent = tmp_path / "writable"
    writable_parent.mkdir(mode=0o700)
    writable_parent.chmod(0o770)
    with pytest.raises(StorageError, match="group/world-writable"):
        await SQLiteIssueStore(writable_parent / "issues.sqlite3").initialize()
    assert stat.S_IMODE(writable_parent.stat().st_mode) == 0o770


@pytest.mark.asyncio
async def test_storage_preserves_existing_safe_parent_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "shared-readable"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    store = SQLiteIssueStore(parent / "issues.sqlite3")

    await store.initialize()

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    await store.close()


@pytest.mark.asyncio
async def test_storage_rejects_database_inode_swap_during_sqlite_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "issues.sqlite3"
    displaced = tmp_path / "displaced.sqlite3"
    original_connect = sqlite3.connect
    opened_connections: list[sqlite3.Connection] = []

    def swapping_connect(
        database_name: str,
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        Path(database_name).replace(displaced)
        Path(database_name).touch(mode=0o600)
        connection = original_connect(database_name, *args, **kwargs)
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(storage_module.sqlite3, "connect", swapping_connect)

    with pytest.raises(StorageError, match="file changed"):
        await SQLiteIssueStore(database).initialize()

    assert displaced.stat().st_ino != database.stat().st_ino
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened_connections[0].execute("SELECT 1")


@pytest.mark.asyncio
async def test_storage_migrates_schema_zero_to_one_and_rejects_newer_versions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "version-zero.sqlite3"
    zero_connection = sqlite3.connect(database)
    try:
        assert zero_connection.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        zero_connection.close()

    store = SQLiteIssueStore(database)
    await store.initialize()
    await store.close()

    migrated_connection = sqlite3.connect(database)
    try:
        assert migrated_connection.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        migrated_connection.close()

    future_database = tmp_path / "future.sqlite3"
    future_connection = sqlite3.connect(future_database)
    try:
        future_connection.execute("PRAGMA user_version = 2")
    finally:
        future_connection.close()

    with pytest.raises(StorageError, match=r"schema version 2.*supported version 1"):
        await SQLiteIssueStore(future_database).initialize()

    unchanged_connection = sqlite3.connect(future_database)
    try:
        assert unchanged_connection.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        unchanged_connection.close()


@pytest.mark.asyncio
async def test_append_event_only_accepts_notes(tmp_path: Path) -> None:
    store = SQLiteIssueStore(tmp_path / "issues.sqlite3")
    issue = await store.open_issue("sim", "Issue")

    for event_type in (
        TimelineEventType.OPENED,
        TimelineEventType.STATUS_CHANGED,
        TimelineEventType.CLOSED,
    ):
        with pytest.raises(StorageError, match="only note events"):
            await store.append_event(issue.issue_id, event_type)

    timeline = await store.get_timeline(issue.issue_id)
    assert [event.event_type for event in timeline.events] == [TimelineEventType.OPENED]
    await store.close()


@pytest.mark.asyncio
async def test_cancelled_initialization_is_drained_before_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteIssueStore(tmp_path / "issues.sqlite3")
    initialization_started = threading.Event()
    release_initialization = threading.Event()
    original_initialize = store._initialize_sync

    def blocked_initialize() -> tuple[
        sqlite3.Connection,
        tuple[tuple[int, int], tuple[int, int]] | None,
    ]:
        initialization_started.set()
        if not release_initialization.wait(timeout=5):
            raise AssertionError("timed out waiting to release initialization")
        return original_initialize()

    monkeypatch.setattr(store, "_initialize_sync", blocked_initialize)
    initialize_task = asyncio.create_task(store.initialize())
    assert await asyncio.to_thread(initialization_started.wait, 2)

    initialize_task.cancel()
    await asyncio.sleep(0)
    close_task = asyncio.create_task(store.close())
    await asyncio.sleep(0.05)
    try:
        assert not initialize_task.done()
        assert not close_task.done()
    finally:
        release_initialization.set()

    with pytest.raises(asyncio.CancelledError):
        await initialize_task
    await close_task
    with pytest.raises(ServiceClosedError):
        await store.initialize()


@pytest.mark.asyncio
async def test_cancelled_operation_is_drained_before_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteIssueStore(tmp_path / "issues.sqlite3")
    issue = await store.open_issue("sim", "Issue")
    operation_started = threading.Event()
    release_operation = threading.Event()
    original_get_issue = store._get_issue_sync

    def blocked_get_issue(issue_id: str) -> object:
        operation_started.set()
        if not release_operation.wait(timeout=5):
            raise AssertionError("timed out waiting to release operation")
        return original_get_issue(issue_id)

    monkeypatch.setattr(store, "_get_issue_sync", blocked_get_issue)
    operation_task = asyncio.create_task(store.get_issue(issue.issue_id))
    assert await asyncio.to_thread(operation_started.wait, 2)

    operation_task.cancel()
    await asyncio.sleep(0)
    close_task = asyncio.create_task(store.close())
    await asyncio.sleep(0.05)
    try:
        assert not operation_task.done()
        assert not close_task.done()
    finally:
        release_operation.set()

    with pytest.raises(asyncio.CancelledError):
        await operation_task
    await close_task


@pytest.mark.asyncio
async def test_cancelled_close_is_drained_and_finishes_closed_state() -> None:
    store = SQLiteIssueStore(":memory:")
    await store.initialize()
    connection = store._connection
    assert connection is not None
    close_started = threading.Event()
    release_close = threading.Event()

    class BlockingClose:
        def close(self) -> None:
            close_started.set()
            if not release_close.wait(timeout=5):
                raise AssertionError("timed out waiting to release close")
            connection.close()

    store._connection = BlockingClose()  # type: ignore[assignment]
    close_task = asyncio.create_task(store.close())
    assert await asyncio.to_thread(close_started.wait, 2)

    close_task.cancel()
    await asyncio.sleep(0)
    close_task.cancel()
    initialize_task = asyncio.create_task(store.initialize())
    await asyncio.sleep(0.05)
    try:
        assert not close_task.done()
        assert not initialize_task.done()
    finally:
        release_close.set()

    with pytest.raises(asyncio.CancelledError):
        await close_task
    with pytest.raises(ServiceClosedError):
        await initialize_task


@pytest.mark.asyncio
async def test_failed_close_can_be_retried() -> None:
    store = SQLiteIssueStore(":memory:")
    await store.initialize()
    connection = store._connection
    assert connection is not None

    class FlakyClose:
        def __init__(self) -> None:
            self.calls = 0

        def close(self) -> None:
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("synthetic close failure")
            connection.close()

    flaky = FlakyClose()
    store._connection = flaky  # type: ignore[assignment]

    with pytest.raises(StorageError, match="failed to close"):
        await store.close()
    await store.close()
    await store.close()

    assert flaky.calls == 2


@pytest.mark.asyncio
async def test_in_memory_store_is_supported_for_ephemeral_tests() -> None:
    store = SQLiteIssueStore(":memory:")
    issue = await store.open_issue("sim", "Ephemeral")

    assert (await store.get_issue(issue.issue_id)).title == "Ephemeral"
    assert store.path is None
    await store.close()
