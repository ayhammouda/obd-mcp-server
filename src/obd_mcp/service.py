"""Concurrency-safe orchestration and result validation for diagnostics."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from .domain import (
    DataSource,
    DiagnosticIssue,
    DiagnosticTroubleCode,
    DTCState,
    ECURef,
    EcuSnapshot,
    IssueSeverity,
    IssueTimeline,
    SignalReading,
    TransportProtocol,
    Vehicle,
    VehicleStatus,
    ensure_safe_public_value,
)
from .drivers.base import DiagnosticDriver
from .errors import (
    DriverError,
    OBDMCPError,
    ServiceClosedError,
    UnsupportedOperationError,
    VehicleNotFoundError,
)
from .policy import STANDARD_PIDS, ReadOnlyPolicy
from .profiles import (
    DecoderDataType,
    DiagnosticProfile,
    ProfileReadDefinition,
    ProfileRegistry,
)
from .storage import SQLiteIssueStore

T = TypeVar("T")

MAX_VEHICLES = 128
MAX_ECUS_PER_VEHICLE = 128
MAX_DTCS_PER_READ = 256
MAX_SNAPSHOT_SIGNALS = 512
MAX_CACHE_ENTRIES = 512


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    value: Any


class DiagnosticService:
    """Read-only application service with fail-closed driver validation."""

    def __init__(
        self,
        driver: DiagnosticDriver,
        store: SQLiteIssueStore,
        *,
        policy: ReadOnlyPolicy | None = None,
        profiles: ProfileRegistry | None = None,
        vehicle_profiles: Mapping[str, str] | None = None,
        cache_ttl_seconds: float = 2.0,
        operation_timeout_seconds: float = 30.0,
        max_cache_entries: int = MAX_CACHE_ENTRIES,
    ) -> None:
        if not math.isfinite(cache_ttl_seconds) or cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds must not be negative")
        if (
            not math.isfinite(operation_timeout_seconds)
            or not 0.1 <= operation_timeout_seconds <= 60.0
        ):
            raise ValueError("operation_timeout_seconds must be between 0.1 and 60")
        if not 1 <= max_cache_entries <= 4_096:
            raise ValueError("max_cache_entries must be between 1 and 4096")

        self._driver = driver
        self._store = store
        self._policy = policy or ReadOnlyPolicy()
        self._profiles = profiles or ProfileRegistry()
        self._vehicle_profiles = dict(vehicle_profiles or {})
        self._cache_ttl_seconds = float(cache_ttl_seconds)
        self._operation_timeout_seconds = float(operation_timeout_seconds)
        self._max_cache_entries = max_cache_entries
        self._cache: dict[tuple[Any, ...], _CacheEntry] = {}
        self._cache_lock = asyncio.Lock()
        self._vehicle_locks: dict[str, asyncio.Lock] = {}
        self._list_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._state_condition = asyncio.Condition()
        self._state = "open"
        self._active_operations = 0
        self._draining_driver_futures: set[asyncio.Future[Any]] = set()
        self._draining_driver_fences: dict[str, asyncio.Future[Any]] = {}

    @asynccontextmanager
    async def _operation_guard(self) -> AsyncIterator[None]:
        async with self._state_condition:
            if self._state != "open":
                raise ServiceClosedError("diagnostic service is closing or closed")
            self._active_operations += 1
        try:
            yield
        finally:
            async with self._state_condition:
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._state_condition.notify_all()

    def _vehicle_lock(self, vehicle_id: str) -> asyncio.Lock:
        lock = self._vehicle_locks.get(vehicle_id)
        if lock is not None:
            return lock

        if len(self._vehicle_locks) >= MAX_VEHICLES:
            for existing_id, candidate in tuple(self._vehicle_locks.items()):
                if not candidate.locked():
                    self._vehicle_locks.pop(existing_id, None)
                if len(self._vehicle_locks) < MAX_VEHICLES:
                    break
        if len(self._vehicle_locks) >= MAX_VEHICLES:
            raise DriverError("configured vehicle concurrency limit reached")

        lock = asyncio.Lock()
        self._vehicle_locks[vehicle_id] = lock
        return lock

    def _purge_cache_locked(self, now: float) -> None:
        expired = [key for key, entry in self._cache.items() if entry.expires_at <= now]
        for key in expired:
            self._cache.pop(key, None)

    async def _cache_get(self, key: tuple[Any, ...]) -> Any | None:
        if self._cache_ttl_seconds == 0:
            return None
        async with self._cache_lock:
            self._purge_cache_locked(time.monotonic())
            entry = self._cache.get(key)
            if entry is None:
                return None
            self._validate_privacy(entry.value)
            return entry.value

    async def _cache_put(self, key: tuple[Any, ...], value: T) -> T:
        if self._cache_ttl_seconds == 0:
            return value
        async with self._cache_lock:
            now = time.monotonic()
            self._purge_cache_locked(now)
            while len(self._cache) >= self._max_cache_entries:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = _CacheEntry(
                expires_at=now + self._cache_ttl_seconds,
                value=value,
            )
        return value

    async def _cached_call(
        self,
        key: tuple[Any, ...],
        lock: asyncio.Lock,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        cached = await self._cache_get(key)
        if cached is not None:
            return cast(T, cached)
        async with lock:
            cached = await self._cache_get(key)
            if cached is not None:
                return cast(T, cached)
            value = await operation()
            self._validate_privacy(value)
            return await self._cache_put(key, value)

    async def _driver_call(
        self,
        operation: Awaitable[T],
        *,
        fence_key: str,
    ) -> T:
        draining = self._draining_driver_fences.get(fence_key)
        if draining is not None and not draining.done():
            if hasattr(operation, "close"):
                operation.close()
            raise DriverError("previous diagnostic driver operation is still draining")
        future = asyncio.ensure_future(operation)
        try:
            done, _pending = await asyncio.wait(
                (future,),
                timeout=self._operation_timeout_seconds,
            )
        except asyncio.CancelledError:
            future.cancel()
            self._track_draining_driver_future(future, fence_key=fence_key)
            raise
        if future in done:
            return future.result()

        future.cancel()
        self._track_draining_driver_future(future, fence_key=fence_key)
        raise DriverError("diagnostic driver operation timed out")

    def _track_draining_driver_future(
        self,
        future: asyncio.Future[Any],
        *,
        fence_key: str,
    ) -> None:
        self._draining_driver_futures.add(future)
        self._draining_driver_fences[fence_key] = future

        def consume(completed: asyncio.Future[Any]) -> None:
            self._draining_driver_futures.discard(completed)
            if self._draining_driver_fences.get(fence_key) is completed:
                self._draining_driver_fences.pop(fence_key, None)
            if not completed.cancelled():
                completed.exception()

        future.add_done_callback(consume)

    async def _close_driver_after_draining(self) -> None:
        pending = tuple(self._draining_driver_futures)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            self._draining_driver_futures.difference_update(pending)
        await self._driver.close()

    @staticmethod
    def _validate_privacy(value: Any) -> None:
        try:
            ensure_safe_public_value(value)
        except ValueError as exc:
            raise DriverError("diagnostic result failed public-output safety validation") from exc

    @staticmethod
    def _bounded_tuple(
        value: object,
        *,
        label: str,
        limit: int,
    ) -> tuple[Any, ...]:
        if not isinstance(value, tuple):
            raise DriverError(f"driver {label} result must be a tuple")
        if len(value) > limit:
            raise DriverError(f"driver {label} result exceeds the {limit}-item limit")
        return value

    async def _load_vehicles(self) -> tuple[Vehicle, ...]:
        raw = await self._driver_call(
            self._driver.list_vehicles(),
            fence_key="fleet",
        )
        values = self._bounded_tuple(raw, label="vehicle list", limit=MAX_VEHICLES)
        if not all(isinstance(value, Vehicle) for value in values):
            raise DriverError("driver vehicle list contains an invalid model")
        vehicles = cast(tuple[Vehicle, ...], values)
        ids = [vehicle.vehicle_id for vehicle in vehicles]
        if len(ids) != len(set(ids)):
            raise DriverError("driver returned duplicate vehicle ids")
        self._validate_privacy(vehicles)
        return vehicles

    async def _list_vehicles(self) -> tuple[Vehicle, ...]:
        return await self._cached_call(("vehicles",), self._list_lock, self._load_vehicles)

    async def list_vehicles(self) -> tuple[Vehicle, ...]:
        async with self._operation_guard():
            return await self._list_vehicles()

    async def _ensure_vehicle(self, vehicle_id: str) -> None:
        if vehicle_id not in {vehicle.vehicle_id for vehicle in await self._list_vehicles()}:
            raise VehicleNotFoundError(f"vehicle not found: {vehicle_id}")

    async def _load_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
        status = await self._driver_call(
            self._driver.get_vehicle_status(vehicle_id),
            fence_key=vehicle_id,
        )
        if not isinstance(status, VehicleStatus):
            raise DriverError("driver vehicle status contains an invalid model")
        if status.vehicle.vehicle_id != vehicle_id:
            raise DriverError("driver status vehicle does not match the request")
        if len(status.ecus) > MAX_ECUS_PER_VEHICLE:
            raise DriverError("driver status exceeds the ECU limit")
        ecu_ids = [ecu.ecu_id for ecu in status.ecus]
        if len(ecu_ids) != len(set(ecu_ids)):
            raise DriverError("driver status contains duplicate ECU ids")
        self._validate_privacy(status)
        return status

    async def _get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
        await self._ensure_vehicle(vehicle_id)
        return await self._cached_call(
            ("status", vehicle_id),
            self._vehicle_lock(vehicle_id),
            lambda: self._load_vehicle_status(vehicle_id),
        )

    async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
        async with self._operation_guard():
            return await self._get_vehicle_status(vehicle_id)

    async def _ensure_ecu(self, vehicle_id: str, ecu_id: str) -> None:
        status = await self._get_vehicle_status(vehicle_id)
        if ecu_id not in {ecu.ecu_id for ecu in status.ecus}:
            raise DriverError(f"ECU not found: {ecu_id}")

    def _validate_standard_readings(
        self,
        value: object,
        *,
        vehicle_id: str,
        authorized: tuple[str, ...],
        known_ecus: set[str],
    ) -> tuple[SignalReading, ...]:
        values = self._bounded_tuple(
            value,
            label="standard PID",
            limit=len(authorized),
        )
        if not all(isinstance(item, SignalReading) for item in values):
            raise DriverError("driver PID result contains an invalid model")
        readings = cast(tuple[SignalReading, ...], values)
        seen: set[str] = set()
        for reading in readings:
            if reading.vehicle_id != vehicle_id:
                raise DriverError("driver PID vehicle does not match the request")
            if reading.ecu_id is not None and reading.ecu_id not in known_ecus:
                raise DriverError("driver PID references an unknown ECU")
            if reading.pid not in authorized:
                raise DriverError("driver returned an unrequested standard PID")
            if reading.pid in seen:
                raise DriverError("driver returned a duplicate standard PID")
            assert reading.pid is not None
            self._validate_standard_signal_semantics(reading)
            seen.add(reading.pid)
        self._validate_privacy(readings)
        return readings

    @staticmethod
    def _validate_standard_signal_semantics(reading: SignalReading) -> None:
        assert reading.pid is not None
        definition = STANDARD_PIDS[reading.pid]
        if reading.signal_id != definition.signal_id:
            raise DriverError("driver PID signal id does not match the core definition")
        if reading.name != definition.name or reading.unit != definition.unit:
            raise DriverError("driver PID label or unit does not match the core definition")
        if (
            isinstance(reading.value, bool)
            or not isinstance(reading.value, (int, float))
            or not definition.minimum <= float(reading.value) <= definition.maximum
        ):
            raise DriverError("driver PID value is outside the canonical numeric range")
        if reading.source not in {DataSource.STANDARD, DataSource.SYNTHETIC}:
            raise DriverError("driver PID source is not valid for a standard observation")

    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str] | None = None,
    ) -> tuple[SignalReading, ...]:
        async with self._operation_guard():
            authorized = self._policy.authorize_standard_pids(pids)
            for pid in authorized:
                self._policy.authorize_obd_service(0x01, pid=pid)
            status = await self._get_vehicle_status(vehicle_id)
            self._require_standard_protocol_scope(status.ecus, ecu_id=None)
            known_ecus = {ecu.ecu_id for ecu in status.ecus}

            async def read() -> tuple[SignalReading, ...]:
                raw = await self._driver_call(
                    self._driver.read_standard_pids(vehicle_id, authorized),
                    fence_key=vehicle_id,
                )
                return self._validate_standard_readings(
                    raw,
                    vehicle_id=vehicle_id,
                    authorized=authorized,
                    known_ecus=known_ecus,
                )

            return await self._cached_call(
                ("pids", vehicle_id, authorized),
                self._vehicle_lock(vehicle_id),
                read,
            )

    def _validate_dtcs(
        self,
        value: object,
        *,
        vehicle_id: str,
        requested_ecu_id: str | None,
        known_ecus: set[str],
        code_family: str | None = None,
    ) -> tuple[DiagnosticTroubleCode, ...]:
        values = self._bounded_tuple(value, label="DTC", limit=MAX_DTCS_PER_READ)
        if not all(isinstance(item, DiagnosticTroubleCode) for item in values):
            raise DriverError("driver DTC result contains an invalid model")
        dtcs = cast(tuple[DiagnosticTroubleCode, ...], values)
        seen: set[tuple[str | None, str, str]] = set()
        for dtc in dtcs:
            if dtc.vehicle_id != vehicle_id:
                raise DriverError("driver DTC vehicle does not match the request")
            if requested_ecu_id is not None and dtc.ecu_id != requested_ecu_id:
                raise DriverError("driver DTC ECU does not match the request")
            if dtc.ecu_id is not None and dtc.ecu_id not in known_ecus:
                raise DriverError("driver DTC references an unknown ECU")
            if code_family == "obd":
                if len(dtc.code) != 5 or dtc.state is not DTCState.STORED:
                    raise DriverError("Mode 03 DTC result must be a stored five-character OBD code")
                if dtc.source not in {DataSource.STANDARD, DataSource.SYNTHETIC}:
                    raise DriverError("Mode 03 DTC source is invalid")
            elif code_family == "uds" and len(dtc.code) != 6:
                raise DriverError("UDS 0x19 DTC result must be a six-hex code")
            identity = (dtc.ecu_id, dtc.code, dtc.state.value)
            if identity in seen:
                raise DriverError("driver returned a duplicate DTC")
            seen.add(identity)
        self._validate_privacy(dtcs)
        return dtcs

    async def read_dtcs(
        self,
        vehicle_id: str,
        ecu_id: str | None = None,
    ) -> tuple[DiagnosticTroubleCode, ...]:
        async with self._operation_guard():
            self._policy.authorize_obd_service(0x03)
            status = await self._get_vehicle_status(vehicle_id)
            ecu_by_id = {ecu.ecu_id: ecu for ecu in status.ecus}
            known_ecus = set(ecu_by_id)
            if ecu_id is not None and ecu_id not in known_ecus:
                raise DriverError(f"ECU not found: {ecu_id}")
            self._require_standard_protocol_scope(status.ecus, ecu_id=ecu_id)

            async def read() -> tuple[DiagnosticTroubleCode, ...]:
                raw = await self._driver_call(
                    self._driver.read_dtcs(vehicle_id, ecu_id),
                    fence_key=vehicle_id,
                )
                return self._validate_dtcs(
                    raw,
                    vehicle_id=vehicle_id,
                    requested_ecu_id=ecu_id,
                    known_ecus=known_ecus,
                    code_family="obd",
                )

            return await self._cached_call(
                ("dtcs", vehicle_id, ecu_id),
                self._vehicle_lock(vehicle_id),
                read,
            )

    @staticmethod
    def _require_standard_protocol_scope(
        ecus: Sequence[ECURef],
        *,
        ecu_id: str | None,
    ) -> None:
        allowed = {TransportProtocol.OBD2, TransportProtocol.SIMULATED}
        if ecu_id is not None:
            target = next(ecu for ecu in ecus if ecu.ecu_id == ecu_id)
            if target.protocol not in allowed:
                raise UnsupportedOperationError(
                    "standard OBD reads require an OBD2 or simulated ECU"
                )
            return
        if not ecus or any(ecu.protocol not in allowed for ecu in ecus):
            raise UnsupportedOperationError(
                "unscoped standard OBD reads require only OBD2 or simulated ECUs"
            )

    def _profile_context(
        self,
        vehicle_id: str,
        ecu_id: str,
        profile_id: str | None,
    ) -> tuple[tuple[ProfileReadDefinition, ...], DiagnosticProfile | None]:
        bound_profile_id = self._vehicle_profiles.get(vehicle_id)
        if profile_id is not None and profile_id != bound_profile_id:
            raise UnsupportedOperationError("profile is not bound to the requested vehicle")
        selected_profile_id = bound_profile_id if profile_id is None else profile_id
        if selected_profile_id is None:
            return (), None

        profile = self._profiles.get(selected_profile_id)
        if profile.selector.protocol is not TransportProtocol.UDS:
            raise UnsupportedOperationError("profile protocol is not supported by schema version 1")
        if profile.selector.ecu_ids and ecu_id not in profile.selector.ecu_ids:
            raise UnsupportedOperationError(
                f"profile {selected_profile_id!r} is not declared for ECU {ecu_id!r}"
            )
        reads = tuple(read for read in profile.reads if read.ecu_id == ecu_id)
        if not reads:
            raise UnsupportedOperationError(
                f"profile {selected_profile_id!r} has no read definitions for ECU {ecu_id!r}"
            )
        for read in reads:
            self._policy.authorize_uds_service(read.service_id)
        return reads, profile

    def _validate_snapshot(
        self,
        value: object,
        *,
        vehicle_id: str,
        ecu_id: str,
        reads: tuple[ProfileReadDefinition, ...],
        profile: DiagnosticProfile | None,
        known_ecus: set[str],
        target_protocol: TransportProtocol,
    ) -> EcuSnapshot:
        if not isinstance(value, EcuSnapshot):
            raise DriverError("driver ECU snapshot contains an invalid model")
        snapshot = value
        if snapshot.vehicle_id != vehicle_id or snapshot.ecu_id != ecu_id:
            raise DriverError("driver ECU snapshot does not match the request")
        if snapshot.ecu_id not in known_ecus:
            raise DriverError("driver ECU snapshot references an unknown ECU")
        if len(snapshot.signals) > MAX_SNAPSHOT_SIGNALS:
            raise DriverError("driver ECU snapshot exceeds the signal limit")
        if len(snapshot.dtcs) > MAX_DTCS_PER_READ:
            raise DriverError("driver ECU snapshot exceeds the DTC limit")

        expected_profile_id = profile.profile_id if profile is not None else None
        if snapshot.profile_id not in (None, expected_profile_id):
            raise DriverError("driver ECU snapshot returned the wrong profile id")

        if profile is None:
            allowed_signal_ids = {definition.signal_id for definition in STANDARD_PIDS.values()}
            allow_dtcs = True
        else:
            allowed_signal_ids = {
                read.signal_id
                for read in reads
                if read.service_id == 0x22 and read.signal_id is not None
            }
            allow_dtcs = any(read.service_id == 0x19 for read in reads)
        if snapshot.protocol is not target_protocol:
            raise DriverError("driver ECU snapshot protocol does not match the request")

        seen_signals: set[str] = set()
        profile_reads_by_signal = {
            read.signal_id: read
            for read in reads
            if read.service_id == 0x22 and read.signal_id is not None
        }
        for signal in snapshot.signals:
            if signal.vehicle_id != vehicle_id or signal.ecu_id != ecu_id:
                raise DriverError("driver snapshot signal does not match the request")
            if signal.signal_id not in allowed_signal_ids:
                raise DriverError("driver snapshot returned an unrequested signal")
            if profile is None:
                if signal.pid not in STANDARD_PIDS:
                    raise DriverError("driver snapshot standard signal has no allowlisted PID")
                assert signal.pid is not None
                self._validate_standard_signal_semantics(signal)
            elif signal.pid is not None:
                raise DriverError("driver profile snapshot returned a standard PID")
            else:
                profile_definition = profile_reads_by_signal[signal.signal_id]
                expected_unit = (
                    profile_definition.decoder.unit
                    if profile_definition.decoder is not None
                    else ""
                )
                if signal.name != profile_definition.name or signal.unit != expected_unit:
                    raise DriverError(
                        "driver profile signal label or unit does not match its definition"
                    )
                assert profile_definition.decoder is not None
                numeric_types = {
                    DecoderDataType.UINT8,
                    DecoderDataType.INT8,
                    DecoderDataType.UINT16,
                    DecoderDataType.INT16,
                    DecoderDataType.UINT32,
                    DecoderDataType.INT32,
                }
                if profile_definition.decoder.data_type in numeric_types:
                    if isinstance(signal.value, bool) or not isinstance(
                        signal.value,
                        (int, float),
                    ):
                        raise DriverError("driver profile signal type does not match its decoder")
                    raw_bounds = {
                        DecoderDataType.UINT8: (0, 2**8 - 1),
                        DecoderDataType.INT8: (-(2**7), 2**7 - 1),
                        DecoderDataType.UINT16: (0, 2**16 - 1),
                        DecoderDataType.INT16: (-(2**15), 2**15 - 1),
                        DecoderDataType.UINT32: (0, 2**32 - 1),
                        DecoderDataType.INT32: (-(2**31), 2**31 - 1),
                    }
                    raw_min, raw_max = raw_bounds[profile_definition.decoder.data_type]
                    endpoint_a = (
                        raw_min * profile_definition.decoder.scale
                        + profile_definition.decoder.value_offset
                    )
                    endpoint_b = (
                        raw_max * profile_definition.decoder.scale
                        + profile_definition.decoder.value_offset
                    )
                    if (
                        not min(endpoint_a, endpoint_b)
                        <= float(signal.value)
                        <= max(
                            endpoint_a,
                            endpoint_b,
                        )
                    ):
                        raise DriverError(
                            "driver profile signal value is outside its decoder range"
                        )
                elif not isinstance(signal.value, str):
                    raise DriverError("driver profile signal type does not match its decoder")
            if signal.signal_id in seen_signals:
                raise DriverError("driver snapshot returned a duplicate signal")
            seen_signals.add(signal.signal_id)

        if snapshot.dtcs and not allow_dtcs:
            raise DriverError("driver snapshot returned unrequested DTC data")
        self._validate_dtcs(
            snapshot.dtcs,
            vehicle_id=vehicle_id,
            requested_ecu_id=ecu_id,
            known_ecus=known_ecus,
            code_family="obd" if profile is None else "uds",
        )
        self._validate_privacy(snapshot)
        return snapshot

    async def _read_snapshot(
        self,
        vehicle_id: str,
        ecu_id: str,
        reads: tuple[ProfileReadDefinition, ...],
        profile: DiagnosticProfile | None,
        known_ecus: set[str],
        target_protocol: TransportProtocol,
    ) -> EcuSnapshot:
        profile_id = profile.profile_id if profile is not None else None
        raw = await self._driver_call(
            self._driver.read_ecu_snapshot(
                vehicle_id,
                ecu_id,
                reads,
                profile_id=profile_id,
            ),
            fence_key=vehicle_id,
        )
        snapshot = self._validate_snapshot(
            raw,
            vehicle_id=vehicle_id,
            ecu_id=ecu_id,
            reads=reads,
            profile=profile,
            known_ecus=known_ecus,
            target_protocol=target_protocol,
        )
        if profile is None:
            return snapshot.model_copy(
                update={
                    "profile_id": None,
                    "profile_source": None,
                    "profile_confidence": None,
                }
            )

        source = profile.provenance.source
        confidence = profile.provenance.confidence
        enriched = snapshot.model_copy(
            update={
                "profile_id": profile.profile_id,
                "profile_source": source,
                "profile_confidence": confidence,
            }
        )
        self._validate_privacy(enriched)
        return enriched

    async def read_ecu_snapshot(
        self,
        vehicle_id: str,
        ecu_id: str,
        *,
        profile_id: str | None = None,
    ) -> EcuSnapshot:
        async with self._operation_guard():
            reads, profile = self._profile_context(vehicle_id, ecu_id, profile_id)
            if profile is None:
                for pid in STANDARD_PIDS:
                    self._policy.authorize_obd_service(0x01, pid=pid)
                self._policy.authorize_obd_service(0x03)
            status = await self._get_vehicle_status(vehicle_id)
            ecu_by_id = {ecu.ecu_id: ecu for ecu in status.ecus}
            known_ecus = set(ecu_by_id)
            target_ecu = ecu_by_id.get(ecu_id)
            if target_ecu is None:
                raise DriverError(f"ECU not found: {ecu_id}")
            if profile is None:
                self._require_standard_protocol_scope((target_ecu,), ecu_id=ecu_id)
            elif target_ecu.protocol not in {
                TransportProtocol.UDS,
                TransportProtocol.SIMULATED,
            }:
                raise UnsupportedOperationError("profile reads require a UDS or simulated ECU")
            selected_profile_id = profile.profile_id if profile is not None else None
            return await self._cached_call(
                ("snapshot", vehicle_id, ecu_id, selected_profile_id),
                self._vehicle_lock(vehicle_id),
                lambda: self._read_snapshot(
                    vehicle_id,
                    ecu_id,
                    reads,
                    profile,
                    known_ecus,
                    target_ecu.protocol,
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
    ) -> DiagnosticIssue:
        async with self._operation_guard():
            # Local notes remain available even when a configured adapter is offline.
            await self._ensure_vehicle(vehicle_id)
            issue = await self._store.open_issue(
                vehicle_id,
                title,
                description,
                severity=severity,
                dtc_codes=dtc_codes,
            )
            self._validate_privacy(issue)
            return issue

    async def get_issue_timeline(self, issue_id: str) -> IssueTimeline:
        async with self._operation_guard():
            timeline = await self._store.get_timeline(issue_id)
            self._validate_privacy(timeline)
            return timeline

    async def close_issue(
        self,
        issue_id: str,
        *,
        message: str = "Issue closed",
    ) -> DiagnosticIssue:
        async with self._operation_guard():
            issue = await self._store.close_issue(issue_id, message=message)
            self._validate_privacy(issue)
            return issue

    async def close(self) -> None:
        async with self._close_lock:
            async with self._state_condition:
                if self._state == "closed":
                    return
                self._state = "closing"
                await self._state_condition.wait_for(lambda: self._active_operations == 0)

            task = asyncio.gather(
                self._close_driver_after_draining(),
                self._store.close(),
                return_exceptions=True,
            )
            cancelled = False
            try:
                results = await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                results = await task

            errors = [result for result in results if isinstance(result, BaseException)]
            async with self._state_condition:
                self._state = "close_failed" if errors else "closed"
                self._state_condition.notify_all()

            if cancelled:
                raise asyncio.CancelledError
            if errors:
                first = errors[0]
                if isinstance(first, OBDMCPError):
                    raise first
                raise OBDMCPError("failed to close diagnostic service resources") from first
