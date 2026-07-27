from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest

from obd_mcp.domain import (
    DataSource,
    DTCState,
    EcuSnapshot,
    IssueSeverity,
    TimelineEventType,
    TransportProtocol,
    VehicleStatus,
)
from obd_mcp.drivers.simulator import SimulatorDriver
from obd_mcp.errors import (
    DriverError,
    PolicyDeniedError,
    ServiceClosedError,
    UnsupportedOperationError,
)
from obd_mcp.policy import ReadOnlyPolicy
from obd_mcp.profiles import DiagnosticProfile, ProfileRegistry
from obd_mcp.service import DiagnosticService
from obd_mcp.storage import SQLiteIssueStore


class CountingSimulator(SimulatorDriver):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.pid_calls = 0

    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str],
    ) -> tuple:
        self.pid_calls += 1
        await asyncio.sleep(0.01)
        return await super().read_standard_pids(vehicle_id, pids)


class ConcurrencySimulator(SimulatorDriver):
    def __init__(self) -> None:
        super().__init__(
            vehicles=(
                {"vehicle_id": "sim-1", "display_name": "One"},
                {"vehicle_id": "sim-2", "display_name": "Two"},
            )
        )
        self.active = 0
        self.max_active = 0

    async def _enter(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.03)

    def _exit(self) -> None:
        self.active -= 1

    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str],
    ) -> tuple:
        await self._enter()
        try:
            return await super().read_standard_pids(vehicle_id, pids)
        finally:
            self._exit()

    async def read_ecu_snapshot(
        self,
        vehicle_id: str,
        ecu_id: str,
        reads: Sequence = (),
        *,
        profile_id: str | None = None,
    ) -> EcuSnapshot:
        await self._enter()
        try:
            return EcuSnapshot(
                vehicle_id=vehicle_id,
                ecu_id=ecu_id,
                protocol=TransportProtocol.SIMULATED,
                profile_id=profile_id,
            )
        finally:
            self._exit()


class WrongPidSimulator(SimulatorDriver):
    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str],
    ) -> tuple:
        readings = await super().read_standard_pids(vehicle_id, pids)
        return (readings[0].model_copy(update={"vehicle_id": "wrong-vehicle"}),)


class SlowSimulator(SimulatorDriver):
    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str],
    ) -> tuple:
        await asyncio.sleep(1)
        return await super().read_standard_pids(vehicle_id, pids)


class SlowCancellationSimulator(SimulatorDriver):
    def __init__(self) -> None:
        super().__init__()
        self.drained = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str],
    ) -> tuple:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.25)
            self.drained.set()
            raise
        finally:
            self.active -= 1
        return await super().read_standard_pids(vehicle_id, pids)


class PerVehicleSlowCancellationSimulator(SimulatorDriver):
    def __init__(self) -> None:
        super().__init__(
            vehicles=(
                {"vehicle_id": "sim-1", "display_name": "One"},
                {"vehicle_id": "sim-2", "display_name": "Two"},
            )
        )
        self.drained = asyncio.Event()

    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str],
    ) -> tuple:
        if vehicle_id == "sim-2":
            return await super().read_standard_pids(vehicle_id, pids)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.25)
            self.drained.set()
            raise
        return await super().read_standard_pids(vehicle_id, pids)


class MutatingPidSimulator(SimulatorDriver):
    def __init__(self, updates: dict[str, object]) -> None:
        super().__init__()
        self.updates = updates

    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str],
    ) -> tuple:
        readings = await super().read_standard_pids(vehicle_id, pids)
        return (readings[0].model_copy(update=self.updates),)


class MutatingSnapshotSimulator(SimulatorDriver):
    def __init__(
        self,
        *,
        signal_updates: dict[str, object] | None = None,
        snapshot_updates: dict[str, object] | None = None,
    ) -> None:
        super().__init__()
        self.signal_updates = signal_updates or {}
        self.snapshot_updates = snapshot_updates or {}

    async def read_ecu_snapshot(
        self,
        vehicle_id: str,
        ecu_id: str,
        reads: Sequence = (),
        *,
        profile_id: str | None = None,
    ) -> EcuSnapshot:
        snapshot = await super().read_ecu_snapshot(
            vehicle_id,
            ecu_id,
            reads,
            profile_id=profile_id,
        )
        if snapshot.signals and self.signal_updates:
            snapshot = snapshot.model_copy(
                update={
                    "signals": (
                        snapshot.signals[0].model_copy(update=self.signal_updates),
                        *snapshot.signals[1:],
                    )
                }
            )
        return snapshot.model_copy(update=self.snapshot_updates)


class MutatingDtcSimulator(SimulatorDriver):
    def __init__(self, updates: dict[str, object]) -> None:
        super().__init__()
        self.updates = updates

    async def read_dtcs(self, vehicle_id: str, ecu_id: str | None = None) -> tuple:
        dtcs = await super().read_dtcs(vehicle_id, ecu_id)
        return (dtcs[0].model_copy(update=self.updates),)


class ProtocolStatusSimulator(SimulatorDriver):
    def __init__(self, protocol: TransportProtocol) -> None:
        super().__init__()
        self.protocol = protocol
        self.pid_calls = 0
        self.dtc_calls = 0
        self.snapshot_calls = 0

    async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
        status = await super().get_vehicle_status(vehicle_id)
        return status.model_copy(
            update={
                "ecus": tuple(
                    ecu.model_copy(update={"protocol": self.protocol}) for ecu in status.ecus
                )
            }
        )

    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str],
    ) -> tuple:
        self.pid_calls += 1
        return await super().read_standard_pids(vehicle_id, pids)

    async def read_dtcs(self, vehicle_id: str, ecu_id: str | None = None) -> tuple:
        self.dtc_calls += 1
        return await super().read_dtcs(vehicle_id, ecu_id)

    async def read_ecu_snapshot(
        self,
        vehicle_id: str,
        ecu_id: str,
        reads: Sequence = (),
        *,
        profile_id: str | None = None,
    ) -> EcuSnapshot:
        self.snapshot_calls += 1
        return await super().read_ecu_snapshot(
            vehicle_id,
            ecu_id,
            reads,
            profile_id=profile_id,
        )


class CountingVehicleIoSimulator(SimulatorDriver):
    def __init__(self) -> None:
        super().__init__()
        self.dtc_calls = 0
        self.snapshot_calls = 0

    async def read_dtcs(self, vehicle_id: str, ecu_id: str | None = None) -> tuple:
        self.dtc_calls += 1
        return await super().read_dtcs(vehicle_id, ecu_id)

    async def read_ecu_snapshot(
        self,
        vehicle_id: str,
        ecu_id: str,
        reads: Sequence = (),
        *,
        profile_id: str | None = None,
    ) -> EcuSnapshot:
        self.snapshot_calls += 1
        return await super().read_ecu_snapshot(
            vehicle_id,
            ecu_id,
            reads,
            profile_id=profile_id,
        )


class DenyMode03Policy(ReadOnlyPolicy):
    def authorize_obd_service(self, service_id: int, *, pid: str | int | None = None) -> None:
        if service_id == 0x03:
            raise PolicyDeniedError("Mode 03 denied for test")
        super().authorize_obd_service(service_id, pid=pid)


class OfflineStatusSimulator(SimulatorDriver):
    async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
        raise DriverError("adapter is offline")


class FlakyCloseSimulator(SimulatorDriver):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise DriverError("transient close failure")
        await super().close()


def enhanced_profile(source: str) -> DiagnosticProfile:
    return DiagnosticProfile.model_validate(
        {
            "schema_version": 1,
            "profile_id": f"test.{source}",
            "name": f"{source} profile",
            "version": "1",
            "provenance": {
                "source": source,
                "origin": "Unit-test profile",
                "license": "LicenseRef-Test",
                "redistribution_allowed": source != "licensed-oem",
                "confidence": 0.61,
            },
            "selector": {"protocol": "uds", "ecu_ids": ["engine"]},
            "reads": [
                {
                    "name": "Enhanced counter",
                    "ecu_id": "engine",
                    "service": "0x22",
                    "identifier": "0xF40D",
                    "signal_id": "enhanced_counter",
                    "decoder": {
                        "data_type": "uint16",
                        "byte_length": 2,
                        "unit": "count",
                    },
                },
                {
                    "name": "Stored DTCs",
                    "ecu_id": "engine",
                    "service": "0x19",
                    "identifier": "0x02",
                },
            ],
        }
    )


@pytest.mark.asyncio
async def test_cache_collapses_concurrent_reads_and_expires(tmp_path: Path) -> None:
    driver = CountingSimulator()
    service = DiagnosticService(
        driver,
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
        cache_ttl_seconds=0.02,
    )

    await asyncio.gather(
        service.read_standard_pids("sim-vehicle-1", ("010C",)),
        service.read_standard_pids("sim-vehicle-1", ("010C",)),
    )
    assert driver.pid_calls == 1

    await asyncio.sleep(0.03)
    await service.read_standard_pids("sim-vehicle-1", ("010C",))
    assert driver.pid_calls == 2
    await service.close()


@pytest.mark.asyncio
async def test_vehicle_lock_is_master_and_distinct_vehicles_can_progress(
    tmp_path: Path,
) -> None:
    driver = ConcurrencySimulator()
    service = DiagnosticService(
        driver,
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
        cache_ttl_seconds=0,
    )

    await asyncio.gather(
        service.read_standard_pids("sim-1", ("010C",)),
        service.read_ecu_snapshot("sim-1", "engine"),
    )
    assert driver.max_active == 1

    driver.max_active = 0
    await asyncio.gather(
        service.read_standard_pids("sim-1", ("010C",)),
        service.read_standard_pids("sim-2", ("010C",)),
    )
    assert driver.max_active == 2
    await service.close()


@pytest.mark.parametrize("source", ["community", "licensed-oem"])
@pytest.mark.asyncio
async def test_enhanced_snapshots_retain_exact_profile_provenance(
    source: str,
    tmp_path: Path,
) -> None:
    profile = enhanced_profile(source)
    service = DiagnosticService(
        SimulatorDriver(),
        SQLiteIssueStore(tmp_path / f"{source}.sqlite3"),
        profiles=ProfileRegistry((profile,)),
        vehicle_profiles={"sim-vehicle-1": profile.profile_id},
    )

    snapshot = await service.read_ecu_snapshot(
        "sim-vehicle-1",
        "engine",
    )
    expected = DataSource(source)

    assert snapshot.profile_id == profile.profile_id
    assert snapshot.profile_source is expected
    assert snapshot.profile_confidence == 0.61
    assert snapshot.signals
    assert snapshot.dtcs
    assert all(reading.source is DataSource.SYNTHETIC for reading in snapshot.signals)
    assert all(reading.confidence == 1.0 for reading in snapshot.signals)
    assert all(dtc.source is DataSource.SYNTHETIC for dtc in snapshot.dtcs)
    assert all(dtc.confidence == 1.0 for dtc in snapshot.dtcs)
    await service.close()


@pytest.mark.asyncio
async def test_service_issue_workflow_and_shutdown(tmp_path: Path) -> None:
    store = SQLiteIssueStore(tmp_path / "issues.sqlite3")
    service = DiagnosticService(
        SimulatorDriver(),
        store,
    )

    issue = await service.open_issue(
        "sim-vehicle-1",
        "Synthetic observation",
        severity=IssueSeverity.LOW,
        dtc_codes=("P0300",),
    )
    await store.append_event(
        issue.issue_id,
        TimelineEventType.NOTE,
        message="Observation recorded",
    )
    timeline = await service.get_issue_timeline(issue.issue_id)
    assert timeline.issue.issue_id == issue.issue_id
    assert len(timeline.events) == 2

    await service.close_issue(issue.issue_id)
    await service.close()
    await service.close()
    with pytest.raises(ServiceClosedError):
        await service.list_vehicles()


@pytest.mark.asyncio
async def test_service_policy_blocks_unlisted_pid_before_driver(tmp_path: Path) -> None:
    driver = CountingSimulator()
    service = DiagnosticService(driver, SQLiteIssueStore(tmp_path / "issues.sqlite3"))

    with pytest.raises(PolicyDeniedError):
        await service.read_standard_pids("sim-vehicle-1", ("0902",))
    assert driver.pid_calls == 0
    await service.close()


@pytest.mark.asyncio
async def test_driver_results_are_correlated_before_they_are_cached(
    tmp_path: Path,
) -> None:
    service = DiagnosticService(
        WrongPidSimulator(),
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
    )

    with pytest.raises(DriverError, match="does not match"):
        await service.read_standard_pids("sim-vehicle-1", ("010C",))

    await service.close()


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"name": "Misleading speed"}, "label or unit"),
        ({"unit": "mph"}, "label or unit"),
        ({"ecu_id": "missing"}, "unknown ECU"),
        ({"source": DataSource.COMMUNITY}, "source"),
        ({"value": "not-a-number"}, "numeric range"),
        ({"value": 20_000}, "numeric range"),
    ],
)
@pytest.mark.asyncio
async def test_standard_pid_results_must_match_canonical_definition(
    updates: dict[str, object],
    message: str,
    tmp_path: Path,
) -> None:
    service = DiagnosticService(
        MutatingPidSimulator(updates),
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
    )

    with pytest.raises(DriverError, match=message):
        await service.read_standard_pids("sim-vehicle-1", ("010C",))

    await service.close()


@pytest.mark.asyncio
async def test_snapshot_signals_must_match_canonical_standard_and_profile_definitions(
    tmp_path: Path,
) -> None:
    standard_service = DiagnosticService(
        MutatingSnapshotSimulator(signal_updates={"unit": "wrong"}),
        SQLiteIssueStore(tmp_path / "standard.sqlite3"),
    )
    with pytest.raises(DriverError, match="label or unit"):
        await standard_service.read_ecu_snapshot("sim-vehicle-1", "engine")
    await standard_service.close()

    profile = enhanced_profile("community")
    profile_service = DiagnosticService(
        MutatingSnapshotSimulator(signal_updates={"name": "Wrong profile label"}),
        SQLiteIssueStore(tmp_path / "profile.sqlite3"),
        profiles=ProfileRegistry((profile,)),
        vehicle_profiles={"sim-vehicle-1": profile.profile_id},
    )
    with pytest.raises(DriverError, match="profile signal label or unit"):
        await profile_service.read_ecu_snapshot("sim-vehicle-1", "engine")
    await profile_service.close()


@pytest.mark.parametrize("value", ["not-a-number", 20_000])
@pytest.mark.asyncio
async def test_unprofiled_snapshot_enforces_standard_pid_value_semantics(
    value: object,
    tmp_path: Path,
) -> None:
    service = DiagnosticService(
        MutatingSnapshotSimulator(signal_updates={"value": value}),
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
    )

    with pytest.raises(DriverError, match="numeric range"):
        await service.read_ecu_snapshot("sim-vehicle-1", "engine")

    await service.close()


@pytest.mark.asyncio
async def test_profile_snapshot_enforces_decoder_numeric_range(
    tmp_path: Path,
) -> None:
    profile = enhanced_profile("community")
    service = DiagnosticService(
        MutatingSnapshotSimulator(signal_updates={"value": 1e300}),
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
        profiles=ProfileRegistry((profile,)),
        vehicle_profiles={"sim-vehicle-1": profile.profile_id},
    )

    with pytest.raises(DriverError, match="decoder range"):
        await service.read_ecu_snapshot("sim-vehicle-1", "engine")

    await service.close()


@pytest.mark.asyncio
async def test_unprofiled_snapshot_clears_driver_supplied_profile_provenance(
    tmp_path: Path,
) -> None:
    service = DiagnosticService(
        MutatingSnapshotSimulator(
            snapshot_updates={
                "profile_id": None,
                "profile_source": DataSource.COMMUNITY,
                "profile_confidence": 0.25,
            }
        ),
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
    )

    snapshot = await service.read_ecu_snapshot("sim-vehicle-1", "engine")

    assert snapshot.profile_id is None
    assert snapshot.profile_source is None
    assert snapshot.profile_confidence is None
    await service.close()


@pytest.mark.asyncio
async def test_snapshot_protocol_must_equal_target_ecu_protocol(
    tmp_path: Path,
) -> None:
    service = DiagnosticService(
        MutatingSnapshotSimulator(
            snapshot_updates={"protocol": TransportProtocol.OBD2},
        ),
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
    )

    with pytest.raises(DriverError, match="protocol does not match"):
        await service.read_ecu_snapshot("sim-vehicle-1", "engine")

    await service.close()


@pytest.mark.asyncio
async def test_mode03_policy_denial_occurs_before_dtc_or_snapshot_driver_io(
    tmp_path: Path,
) -> None:
    dtc_driver = CountingVehicleIoSimulator()
    dtc_service = DiagnosticService(
        dtc_driver,
        SQLiteIssueStore(tmp_path / "dtc.sqlite3"),
        policy=DenyMode03Policy(),
    )
    with pytest.raises(PolicyDeniedError, match="Mode 03"):
        await dtc_service.read_dtcs("sim-vehicle-1", "engine")
    assert dtc_driver.dtc_calls == 0
    await dtc_service.close()

    snapshot_driver = CountingVehicleIoSimulator()
    snapshot_service = DiagnosticService(
        snapshot_driver,
        SQLiteIssueStore(tmp_path / "snapshot.sqlite3"),
        policy=DenyMode03Policy(),
    )
    with pytest.raises(PolicyDeniedError, match="Mode 03"):
        await snapshot_service.read_ecu_snapshot("sim-vehicle-1", "engine")
    assert snapshot_driver.snapshot_calls == 0
    await snapshot_service.close()


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"code": "123456"}, "five-character OBD"),
        ({"state": DTCState.PENDING}, "stored five-character OBD"),
        ({"source": DataSource.COMMUNITY}, "source"),
    ],
)
@pytest.mark.asyncio
async def test_mode03_results_enforce_obd_code_state_and_source(
    updates: dict[str, object],
    message: str,
    tmp_path: Path,
) -> None:
    service = DiagnosticService(
        MutatingDtcSimulator(updates),
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
    )

    with pytest.raises(DriverError, match=message):
        await service.read_dtcs("sim-vehicle-1", "engine")

    await service.close()


@pytest.mark.parametrize(
    ("operation", "protocol"),
    [
        ("pids", TransportProtocol.UDS),
        ("dtcs", TransportProtocol.KWP2000),
        ("snapshot", TransportProtocol.UDS),
    ],
)
@pytest.mark.asyncio
async def test_standard_reads_reject_non_obd_protocol_before_vehicle_io(
    operation: str,
    protocol: TransportProtocol,
    tmp_path: Path,
) -> None:
    driver = ProtocolStatusSimulator(protocol)
    service = DiagnosticService(
        driver,
        SQLiteIssueStore(tmp_path / f"{operation}.sqlite3"),
    )

    async def invoke() -> None:
        if operation == "pids":
            await service.read_standard_pids("sim-vehicle-1", ("010C",))
        elif operation == "dtcs":
            await service.read_dtcs("sim-vehicle-1", "engine")
        else:
            await service.read_ecu_snapshot("sim-vehicle-1", "engine")

    with pytest.raises(UnsupportedOperationError, match="OBD"):
        await invoke()

    assert driver.pid_calls == 0
    assert driver.dtc_calls == 0
    assert driver.snapshot_calls == 0
    await service.close()


@pytest.mark.asyncio
async def test_profile_reads_reject_non_uds_target_before_snapshot_io(
    tmp_path: Path,
) -> None:
    profile = enhanced_profile("community")
    driver = ProtocolStatusSimulator(TransportProtocol.OBD2)
    service = DiagnosticService(
        driver,
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
        profiles=ProfileRegistry((profile,)),
        vehicle_profiles={"sim-vehicle-1": profile.profile_id},
    )

    with pytest.raises(UnsupportedOperationError, match="UDS"):
        await service.read_ecu_snapshot("sim-vehicle-1", "engine")

    assert driver.snapshot_calls == 0
    await service.close()


@pytest.mark.asyncio
async def test_invalid_vehicle_ids_do_not_grow_lock_or_cache_maps(tmp_path: Path) -> None:
    service = DiagnosticService(
        SimulatorDriver(),
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
        max_cache_entries=2,
    )

    for index in range(100):
        with pytest.raises(DriverError, match="vehicle not found"):
            await service.get_vehicle_status(f"missing-{index}")

    assert service._vehicle_locks == {}
    assert len(service._cache) <= 2
    await service.close()


@pytest.mark.asyncio
async def test_core_operation_timeout_is_independent_of_driver_behavior(
    tmp_path: Path,
) -> None:
    service = DiagnosticService(
        SlowSimulator(),
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
        operation_timeout_seconds=0.1,
    )

    with pytest.raises(DriverError, match="timed out"):
        await service.read_standard_pids("sim-vehicle-1", ("010C",))

    await service.close()


@pytest.mark.asyncio
async def test_core_timeout_returns_while_cancelled_driver_cleanup_drains(
    tmp_path: Path,
) -> None:
    driver = SlowCancellationSimulator()
    service = DiagnosticService(
        driver,
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
        operation_timeout_seconds=0.1,
    )
    started = asyncio.get_running_loop().time()

    with pytest.raises(DriverError, match="timed out"):
        await service.read_standard_pids("sim-vehicle-1", ("010C",))

    assert asyncio.get_running_loop().time() - started < 0.2
    assert driver.drained.is_set() is False
    with pytest.raises(DriverError, match="still draining"):
        await service.read_standard_pids("sim-vehicle-1", ("010C",))
    assert driver.max_active == 1
    await service.close()
    assert driver.drained.is_set() is True


@pytest.mark.asyncio
async def test_draining_vehicle_fence_does_not_block_another_vehicle(
    tmp_path: Path,
) -> None:
    driver = PerVehicleSlowCancellationSimulator()
    service = DiagnosticService(
        driver,
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
        operation_timeout_seconds=0.1,
    )

    with pytest.raises(DriverError, match="timed out"):
        await service.read_standard_pids("sim-1", ("010C",))

    readings = await service.read_standard_pids("sim-2", ("010C",))
    assert readings
    await service.close()
    assert driver.drained.is_set() is True


@pytest.mark.asyncio
async def test_profiles_cannot_cross_their_configured_vehicle_binding(
    tmp_path: Path,
) -> None:
    profile = enhanced_profile("community")
    driver = SimulatorDriver(
        vehicles=(
            {"vehicle_id": "sim-1", "display_name": "One"},
            {"vehicle_id": "sim-2", "display_name": "Two"},
        )
    )
    service = DiagnosticService(
        driver,
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
        profiles=ProfileRegistry((profile,)),
        vehicle_profiles={"sim-1": profile.profile_id},
    )

    with pytest.raises(UnsupportedOperationError, match="not bound"):
        await service.read_ecu_snapshot(
            "sim-2",
            "engine",
            profile_id=profile.profile_id,
        )

    unprofiled = await service.read_ecu_snapshot("sim-2", "engine")
    assert unprofiled.profile_id is None
    await service.close()


@pytest.mark.asyncio
async def test_local_issues_can_be_recorded_while_adapter_status_is_offline(
    tmp_path: Path,
) -> None:
    service = DiagnosticService(
        OfflineStatusSimulator(),
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
    )

    issue = await service.open_issue("sim-vehicle-1", "Offline observation")
    assert issue.vehicle_id == "sim-vehicle-1"
    with pytest.raises(DriverError, match="offline"):
        await service.get_vehicle_status("sim-vehicle-1")

    await service.close()


@pytest.mark.asyncio
async def test_failed_service_close_is_retryable_and_operations_stay_blocked(
    tmp_path: Path,
) -> None:
    driver = FlakyCloseSimulator()
    service = DiagnosticService(
        driver,
        SQLiteIssueStore(tmp_path / "issues.sqlite3"),
    )

    with pytest.raises(DriverError, match="close failure"):
        await service.close()
    with pytest.raises(ServiceClosedError):
        await service.list_vehicles()

    await service.close()
    assert driver.close_calls == 2
