"""Optional ELM327 driver backed by MIT-licensed ``py-obdii``.

The dependency is imported only when this driver first connects. Every query
uses a private source-controlled mapping to a vetted read command.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import math
from collections.abc import Callable, Sequence
from itertools import islice
from numbers import Integral, Real
from typing import Any, TypeVar

from ..domain import (
    ConnectionState,
    DataSource,
    DiagnosticTroubleCode,
    DTCState,
    ECURef,
    EcuSnapshot,
    SignalReading,
    TransportProtocol,
    Vehicle,
    VehicleStatus,
)
from ..errors import (
    DriverError,
    DriverUnavailableError,
    UnsupportedOperationError,
    VehicleNotFoundError,
)
from ..policy import STANDARD_PIDS, ReadOnlyPolicy
from ..profiles import ProfileReadDefinition
from .base import DiagnosticDriver

_MIN_BAUDRATE = 1_200
_MAX_BAUDRATE = 2_000_000
_MIN_TIMEOUT_SECONDS = 0.1
_MAX_TIMEOUT_SECONDS = 5.0
_MAX_DTC_CANDIDATES = 256
_DEFAULT_PROTOCOL = "iso_15765_4_can"
_PROTOCOL_NAMES = frozenset(
    {
        "iso_14230_4_kwp",
        "iso_14230_4_kwp_fast",
        "iso_15765_4_can",
        "iso_15765_4_can_b",
        "iso_15765_4_can_c",
        "iso_15765_4_can_d",
        "iso_9141_2",
        "sae_j1850_pwm",
        "sae_j1850_vpw",
    }
)
_T = TypeVar("_T")


def _bounded_baudrate(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"baudrate must be an integer between {_MIN_BAUDRATE} and {_MAX_BAUDRATE}")
    normalized = int(value)
    if not _MIN_BAUDRATE <= normalized <= _MAX_BAUDRATE:
        raise ValueError(f"baudrate must be an integer between {_MIN_BAUDRATE} and {_MAX_BAUDRATE}")
    return normalized


def _bounded_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(
            "timeout_seconds must be a finite number between "
            f"{_MIN_TIMEOUT_SECONDS} and {_MAX_TIMEOUT_SECONDS}"
        )
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(
            "timeout_seconds must be a finite number between "
            f"{_MIN_TIMEOUT_SECONDS} and {_MAX_TIMEOUT_SECONDS}"
        ) from exc
    if not (
        math.isfinite(normalized) and _MIN_TIMEOUT_SECONDS <= normalized <= _MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "timeout_seconds must be a finite number between "
            f"{_MIN_TIMEOUT_SECONDS} and {_MAX_TIMEOUT_SECONDS}"
        )
    return normalized


def _explicit_protocol(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("protocol must be an explicit supported py-obdii protocol name")
    normalized = value.strip().lower()
    if normalized not in _PROTOCOL_NAMES:
        raise ValueError("protocol must be an explicit supported py-obdii protocol name")
    return normalized


class _ElmConnectionError(DriverError):
    """A runtime connectivity failure that can be represented as status."""


class Elm327Driver(DiagnosticDriver):
    """Read-only adapter for a configured ELM327 endpoint."""

    def __init__(
        self,
        *,
        port: str,
        baudrate: int = 38_400,
        timeout_seconds: float = 5.0,
        protocol: str = _DEFAULT_PROTOCOL,
        vehicle_id: str = "elm327",
        display_name: str = "ELM327 Vehicle",
    ) -> None:
        if not isinstance(port, str) or not port.strip():
            raise ValueError("port is required")
        self._port = port.strip()
        self._baudrate = _bounded_baudrate(baudrate)
        self._timeout_seconds = _bounded_timeout(timeout_seconds)
        self._protocol = _explicit_protocol(protocol)
        self._vehicle_id = vehicle_id
        self._display_name = display_name
        self._policy = ReadOnlyPolicy()
        self._connection: Any | None = None
        self._commands: Any | None = None
        self._expected_protocol: Any | None = None
        self._connection_poisoned = False
        self._io_lock = asyncio.Lock()
        self._closed = False
        self._connection_state = ConnectionState.DISCONNECTED

    def _ensure_open(self) -> None:
        if self._closed:
            raise DriverError("ELM327 driver is closed")

    def _ensure_vehicle(self, vehicle_id: str) -> None:
        self._ensure_open()
        if vehicle_id != self._vehicle_id:
            raise VehicleNotFoundError(f"vehicle not found: {vehicle_id}")

    @staticmethod
    def _silence_dependency_logging() -> None:
        """Prevent the optional dependency from emitting diagnostic traffic."""

        null_handler = logging.NullHandler()
        package_logger = logging.getLogger("obdii")
        package_logger.handlers.clear()
        package_logger.addHandler(null_handler)
        package_logger.propagate = False
        package_logger.setLevel(logging.CRITICAL + 1)

        # The pinned dependency emits command and response traffic from this
        # child logger. Disable it explicitly in case an application configured
        # a child logger independently of the package logger.
        connection_logger = logging.getLogger("obdii.connection")
        connection_logger.handlers.clear()
        connection_logger.addHandler(logging.NullHandler())
        connection_logger.propagate = False
        connection_logger.setLevel(logging.CRITICAL + 1)
        connection_logger.disabled = True

    def _configured_vehicle(self, state: ConnectionState) -> Vehicle:
        return Vehicle(
            vehicle_id=self._vehicle_id,
            display_name=self._display_name,
            protocol=TransportProtocol.OBD2,
            connection_state=state,
        )

    async def _run_serialized_sync(
        self,
        operation: Callable[..., _T],
        *args: Any,
    ) -> _T:
        """Run blocking I/O without releasing the lock while its thread is active."""

        async with self._io_lock:
            worker = asyncio.create_task(asyncio.to_thread(operation, *args))
            cancellation: asyncio.CancelledError | None = None
            while True:
                try:
                    await asyncio.shield(worker)
                    break
                except asyncio.CancelledError as exc:
                    cancellation = exc
                    if worker.done():
                        break
                except Exception:
                    if cancellation is None:
                        raise
                    break

            if cancellation is not None:
                if not worker.cancelled():
                    worker.exception()
                raise cancellation
            return worker.result()

    def _cleanup_failed_connection(self, connection: Any, commands: Any) -> None:
        """Close a partially initialized connection, retaining it if close fails."""

        try:
            connection.close()
        except Exception:
            self._connection = connection
            self._commands = commands
            self._connection_poisoned = True
            self._connection_state = ConnectionState.DEGRADED

    def _connect_sync(self) -> None:
        self._ensure_open()
        if self._connection is not None and self._connection_poisoned:
            try:
                self._connection.close()
            except Exception as exc:
                self._connection_state = ConnectionState.DEGRADED
                raise DriverError("failed ELM327 connection remains quarantined") from exc
            self._connection = None
            self._commands = None
            self._expected_protocol = None
            self._connection_poisoned = False
            self._connection_state = ConnectionState.DISCONNECTED

        if self._connection is not None:
            is_connected = getattr(self._connection, "is_connected", None)
            try:
                connected = is_connected is None or bool(is_connected())
            except Exception:
                connected = False
            if connected:
                if (
                    self._expected_protocol is None
                    or getattr(self._connection, "protocol", None) != self._expected_protocol
                ):
                    self._connection_state = ConnectionState.DEGRADED
                    raise DriverError(
                        "ELM327 adapter protocol does not match the configured protocol"
                    )
                self._connection_state = ConnectionState.CONNECTED
                return
            try:
                self._connection.close()
            except Exception as exc:
                self._connection_state = ConnectionState.DEGRADED
                raise DriverError("failed to close stale ELM327 connection") from exc
            self._connection = None
            self._commands = None
            self._expected_protocol = None
            self._connection_poisoned = False
            self._connection_state = ConnectionState.DISCONNECTED

        try:
            obdii = importlib.import_module("obdii")
        except ImportError as exc:
            raise DriverUnavailableError(
                "ELM327 support requires the optional 'py-obdii' package"
            ) from exc
        self._silence_dependency_logging()

        connection: Any | None = None
        commands: Any = None
        try:
            connection_type = obdii.Connection
            commands = obdii.commands
            protocol = getattr(obdii.Protocol, self._protocol.upper())
            self._expected_protocol = protocol
            connection = connection_type(
                self._port,
                protocol=protocol,
                auto_connect=False,
                smart_query=False,
                early_return=False,
                log_handler=None,
                baudrate=self._baudrate,
                timeout=self._timeout_seconds,
                write_timeout=self._timeout_seconds,
            )
        except Exception as exc:
            self._connection_state = ConnectionState.DEGRADED
            raise DriverError("invalid ELM327 driver configuration") from exc

        try:
            connection.connect()
        except (AttributeError, TypeError, ValueError) as exc:
            self._cleanup_failed_connection(connection, commands)
            raise DriverError("invalid ELM327 driver configuration") from exc
        except Exception as exc:
            self._cleanup_failed_connection(connection, commands)
            if self._connection is None:
                self._connection_state = ConnectionState.DISCONNECTED
            raise _ElmConnectionError("failed to connect to the configured ELM327 adapter") from exc
        else:
            if getattr(connection, "protocol", None) != protocol:
                self._cleanup_failed_connection(connection, commands)
                if self._connection is None:
                    self._connection_state = ConnectionState.DISCONNECTED
                raise DriverError(
                    "ELM327 adapter negotiated a protocol different from the configured protocol"
                )
            self._connection = connection
            self._commands = commands
            self._connection_poisoned = False
            self._connection_state = ConnectionState.CONNECTED

    def _query_named_sync(
        self,
        command_name: str,
        *,
        expected_mode: int,
        expected_pid: int | str | None = None,
    ) -> Any:
        # command_name is supplied only by the private fixed mappings below.
        self._connect_sync()
        self._silence_dependency_logging()
        assert self._connection is not None
        assert self._commands is not None
        command = getattr(self._commands, command_name)
        actual_mode = getattr(command, "mode", None)
        if actual_mode is None:
            raise DriverError("optional driver command has no verifiable mode")
        actual_value = getattr(actual_mode, "value", actual_mode)
        if isinstance(actual_value, bool) or not isinstance(actual_value, Integral):
            raise DriverError("optional driver returned an invalid command mode")
        if int(actual_value) != expected_mode:
            raise DriverError("optional driver command failed the read-only mode check")
        if isinstance(expected_pid, int):
            actual_pid = getattr(command, "pid", None)
            if (
                isinstance(actual_pid, bool)
                or not isinstance(actual_pid, Integral)
                or int(actual_pid) != expected_pid
            ):
                raise DriverError("optional driver command failed the fixed PID check")
        elif isinstance(expected_pid, str) and getattr(command, "pid", None) != expected_pid:
            raise DriverError("optional driver command failed the fixed payload check")
        return self._connection.query(command)

    @staticmethod
    def _scalar(value: Any) -> bool | int | float | str | None:
        if value is None:
            return None
        magnitude = getattr(value, "magnitude", value)
        if isinstance(magnitude, bool):
            return magnitude
        if isinstance(magnitude, int):
            return magnitude
        if isinstance(magnitude, Real):
            return float(magnitude)
        if isinstance(magnitude, str):
            return magnitude
        if isinstance(magnitude, Sequence) and not isinstance(magnitude, (bytes, bytearray)):
            if len(magnitude) == 1:
                return Elm327Driver._scalar(magnitude[0])
            return ", ".join(str(item) for item in magnitude)
        return str(magnitude)

    def _read_pid_sync(self, vehicle_id: str, pid: str) -> SignalReading | None:
        definition = STANDARD_PIDS[pid]
        response = self._query_named_sync(
            definition.py_obdii_command,
            expected_mode=0x01,
            expected_pid=int(pid[2:], 16),
        )
        value = self._scalar(getattr(response, "value", None))
        if value is None:
            return None
        return SignalReading(
            vehicle_id=vehicle_id,
            ecu_id="engine",
            pid=pid,
            signal_id=definition.signal_id,
            name=definition.name,
            value=value,
            unit=definition.unit,
            source=DataSource.STANDARD,
        )

    def _read_dtcs_sync(
        self,
        vehicle_id: str,
        ecu_id: str | None,
    ) -> tuple[DiagnosticTroubleCode, ...]:
        response = self._query_named_sync(
            "GET_DTC",
            expected_mode=0x03,
            expected_pid="",
        )
        raw_value = getattr(response, "value", ())
        if raw_value is None:
            return ()
        if isinstance(raw_value, str):
            candidates: Sequence[Any] = (raw_value,)
        elif isinstance(raw_value, Sequence):
            candidates = raw_value
        else:
            candidates = (raw_value,)
        result: list[DiagnosticTroubleCode] = []
        for item in islice(candidates, _MAX_DTC_CANDIDATES):
            if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                if not item:
                    continue
                code = item[0]
            else:
                code = item
            normalized = str(code).strip().upper()
            try:
                result.append(
                    DiagnosticTroubleCode(
                        vehicle_id=vehicle_id,
                        ecu_id=ecu_id or "engine",
                        code=normalized,
                        state=DTCState.STORED,
                    )
                )
            except ValueError:
                # Malformed third-party responses are ignored rather than leaked.
                continue
        return tuple(result)

    async def list_vehicles(self) -> tuple[Vehicle, ...]:
        async with self._io_lock:
            self._ensure_open()
            state = self._connection_state
        return (self._configured_vehicle(state),)

    async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
        self._ensure_vehicle(vehicle_id)
        try:
            await self._run_serialized_sync(self._connect_sync)
        except _ElmConnectionError:
            state = self._connection_state
            if state not in (ConnectionState.DISCONNECTED, ConnectionState.DEGRADED):
                state = ConnectionState.DISCONNECTED
            return VehicleStatus(
                vehicle=self._configured_vehicle(state),
                ecus=(
                    ECURef(
                        ecu_id="engine",
                        name="Powertrain ECU",
                        protocol=TransportProtocol.OBD2,
                        available=False,
                    ),
                ),
                notes=("Configured ELM327 adapter is currently unreachable.",),
            )
        return VehicleStatus(
            vehicle=self._configured_vehicle(ConnectionState.CONNECTED),
            ecus=(ECURef(ecu_id="engine", name="Powertrain ECU", protocol=TransportProtocol.OBD2),),
        )

    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str],
    ) -> tuple[SignalReading, ...]:
        self._ensure_vehicle(vehicle_id)
        authorized = self._policy.authorize_standard_pids(pids)
        result: list[SignalReading] = []
        try:
            for pid in authorized:
                self._policy.authorize_obd_service(0x01, pid=pid)
                reading = await self._run_serialized_sync(
                    self._read_pid_sync,
                    vehicle_id,
                    pid,
                )
                if reading is not None:
                    result.append(reading)
            return tuple(result)
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError("ELM327 PID read failed") from exc

    async def read_dtcs(
        self,
        vehicle_id: str,
        ecu_id: str | None = None,
    ) -> tuple[DiagnosticTroubleCode, ...]:
        self._ensure_vehicle(vehicle_id)
        self._policy.authorize_obd_service(0x03)
        if ecu_id not in (None, "engine"):
            raise DriverError(f"ECU not found: {ecu_id}")
        try:
            return await self._run_serialized_sync(self._read_dtcs_sync, vehicle_id, ecu_id)
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError("ELM327 DTC read failed") from exc

    async def read_ecu_snapshot(
        self,
        vehicle_id: str,
        ecu_id: str,
        reads: Sequence[ProfileReadDefinition] = (),
        *,
        profile_id: str | None = None,
    ) -> EcuSnapshot:
        self._ensure_vehicle(vehicle_id)
        if ecu_id != "engine":
            raise DriverError(f"ECU not found: {ecu_id}")
        if reads:
            raise UnsupportedOperationError(
                "the py-obdii ELM327 driver does not issue profile-defined UDS reads"
            )
        for pid in STANDARD_PIDS:
            self._policy.authorize_obd_service(0x01, pid=pid)
        self._policy.authorize_obd_service(0x03)
        signals = await self.read_standard_pids(vehicle_id, tuple(STANDARD_PIDS))
        dtcs = await self.read_dtcs(vehicle_id, ecu_id)
        return EcuSnapshot(
            vehicle_id=vehicle_id,
            ecu_id=ecu_id,
            protocol=TransportProtocol.OBD2,
            signals=signals,
            dtcs=dtcs,
            profile_id=profile_id,
        )

    def _close_sync(self) -> None:
        if self._closed:
            return
        connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:
                self._connection_state = ConnectionState.DEGRADED
                raise DriverError("failed to close ELM327 connection") from exc
        self._connection = None
        self._commands = None
        self._expected_protocol = None
        self._connection_poisoned = False
        self._connection_state = ConnectionState.DISCONNECTED
        self._closed = True

    async def close(self) -> None:
        await self._run_serialized_sync(self._close_sync)
