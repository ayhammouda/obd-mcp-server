from __future__ import annotations

import asyncio
import importlib
import logging
import threading
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from obd_mcp.domain import ConnectionState, DataSource, SignalQuality
from obd_mcp.drivers import (
    DiagnosticDriver,
    DriverRegistry,
    Elm327Driver,
    SimulatorDriver,
    SimulatorVehicle,
)
from obd_mcp.drivers import registry as registry_module
from obd_mcp.errors import (
    DriverError,
    DriverUnavailableError,
    PolicyDeniedError,
    UnsupportedOperationError,
    VehicleNotFoundError,
)
from obd_mcp.profiles import ProfileReadDefinition


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"dtcs": {"engine": ["NOT-A-DTC"]}}, "canonical uppercase OBD"),
        ({"dtcs": {"ghost": ["P0300"]}}, "unknown ECU"),
        ({"dtcs": {"engine": ["P0300", "P0300"]}}, "unique"),
        ({"pid_values": {"010C": 1e100}}, "between"),
        ({"pid_values": {"010C": 10**10_000}}, "between"),
        ({"pid_values": {"010C": True}}, "finite numbers"),
        (
            {
                "ecus": [
                    {"ecu_id": "engine", "name": "One", "protocol": "simulated"},
                    {"ecu_id": "engine", "name": "Two", "protocol": "simulated"},
                ]
            },
            "unique",
        ),
        (
            {
                "ecus": [
                    {"ecu_id": "engine", "name": "Engine", "protocol": "obd2"},
                ]
            },
            "simulated protocol",
        ),
        (
            {
                "ecus": [
                    {"ecu_id": "body", "name": "Body", "protocol": "simulated"},
                ],
                "dtcs": {},
            },
            "requires an engine ECU",
        ),
        ({"dtcs": {"engine": ["P0300"] * 257}}, "more than 256"),
    ],
)
def test_simulator_vehicle_rejects_deterministic_runtime_failures(
    options: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        SimulatorVehicle.model_validate(
            {
                "vehicle_id": "sim",
                "display_name": "Simulator",
                **options,
            }
        )


@pytest.mark.parametrize("seed", [True, 1.5, 2**63])
def test_simulator_driver_rejects_invalid_or_unbounded_seed(seed: object) -> None:
    with pytest.raises(ValueError, match="signed 64-bit integer"):
        SimulatorDriver(seed=seed)  # type: ignore[arg-type]


def test_simulator_driver_rejects_huge_integer_seed_without_stringifying_it() -> None:
    seed = pow(10, 10_000)

    with pytest.raises(ValueError, match="signed 64-bit integer"):
        SimulatorDriver(seed=seed)


@pytest.mark.asyncio
async def test_simulator_is_deterministic_and_never_returns_full_vin() -> None:
    configured = SimulatorVehicle(
        vehicle_id="sim",
        display_name="Simulator",
        vin="A" * 17,
    )
    first = SimulatorDriver(seed=42, vehicles=(configured,))
    second = SimulatorDriver(seed=42, vehicles=(configured,))

    first_values = await first.read_standard_pids("sim", ("010C", "0142"))
    second_values = await second.read_standard_pids("sim", ("010C", "0142"))
    vehicle = (await first.list_vehicles())[0]

    assert [reading.value for reading in first_values] == [
        reading.value for reading in second_values
    ]
    assert all(reading.source is DataSource.SYNTHETIC for reading in first_values)
    assert all(reading.quality is SignalQuality.SYNTHETIC for reading in first_values)
    serialized = vehicle.model_dump_json()
    assert "A" * 17 not in serialized
    assert vehicle.vin is not None
    assert vehicle.vin.redacted.endswith("AAAA")


@pytest.mark.asyncio
async def test_simulator_rechecks_pid_policy_and_reads_normalized_snapshot() -> None:
    driver = SimulatorDriver()
    vehicle_id = (await driver.list_vehicles())[0].vehicle_id

    with pytest.raises(PolicyDeniedError):
        await driver.read_standard_pids(vehicle_id, ("0902",))

    snapshot = await driver.read_ecu_snapshot(vehicle_id, "engine")
    assert snapshot.signals
    assert snapshot.dtcs
    assert all(reading.source is DataSource.SYNTHETIC for reading in snapshot.signals)


@pytest.mark.asyncio
async def test_optional_elm327_dependency_is_imported_only_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def missing_import(name: str) -> Any:
        calls.append(name)
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", missing_import)
    driver = Elm327Driver(port="/dev/not-opened")
    assert calls == []

    vehicles = await driver.list_vehicles()
    assert calls == []
    assert vehicles[0].connection_state is ConnectionState.DISCONNECTED

    with pytest.raises(DriverUnavailableError, match="py-obdii"):
        await driver.get_vehicle_status("elm327")
    assert calls == ["obdii"]


class _FakeCommand:
    def __init__(self, mode: int, pid: object | None = None) -> None:
        self.mode = mode
        self.pid = ("" if mode == 0x03 else 0x0C) if pid is None else pid


class _FakeConnection:
    instances: ClassVar[list[_FakeConnection]] = []
    dtc_value: ClassVar[Any] = ["P0300"]
    pid_value: ClassVar[Any] = 803

    def __init__(self, port: str, **kwargs: Any) -> None:
        self.port = port
        self.kwargs = kwargs
        self.protocol = kwargs.get("protocol")
        self.connected = False
        self.closed = False
        self.queries: list[_FakeCommand] = []
        self.instances.append(self)

    def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected and not self.closed

    def query(self, command: _FakeCommand) -> SimpleNamespace:
        self.queries.append(command)
        if command.mode == 0x03:
            return SimpleNamespace(value=self.dtc_value)
        return SimpleNamespace(value=self.pid_value, units="rpm")

    def close(self) -> None:
        self.closed = True


_FAKE_PROTOCOL = object()


def _fake_obdii_module(
    connection: type[Any],
    commands: SimpleNamespace,
) -> SimpleNamespace:
    return SimpleNamespace(
        Connection=connection,
        Protocol=SimpleNamespace(ISO_15765_4_CAN=_FAKE_PROTOCOL),
        commands=commands,
    )


@pytest.mark.asyncio
async def test_elm327_lists_configured_vehicle_without_touching_hardware_and_degrades_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnreachableConnection(_FakeConnection):
        def connect(self) -> None:
            raise OSError("adapter unplugged")

    commands = SimpleNamespace(
        ENGINE_SPEED=_FakeCommand(0x01),
        GET_DTC=_FakeCommand(0x03),
    )
    module = _fake_obdii_module(UnreachableConnection, commands)
    imports: list[str] = []

    def import_obdii(name: str) -> Any:
        imports.append(name)
        return module

    monkeypatch.setattr(importlib, "import_module", import_obdii)
    _FakeConnection.instances.clear()
    driver = Elm327Driver(port="/dev/fake")

    vehicles = await driver.list_vehicles()
    assert imports == []
    assert _FakeConnection.instances == []
    assert vehicles[0].connection_state is ConnectionState.DISCONNECTED

    status = await driver.get_vehicle_status("elm327")
    assert imports == ["obdii"]
    assert status.vehicle.connection_state is ConnectionState.DISCONNECTED
    assert status.ecus[0].available is False
    assert status.notes == ("Configured ELM327 adapter is currently unreachable.",)
    await driver.close()


@pytest.mark.asyncio
async def test_elm327_status_does_not_hide_driver_configuration_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidConfigurationConnection:
        def __init__(self, _port: str, **_kwargs: Any) -> None:
            raise ValueError("unsupported serial configuration")

    module = _fake_obdii_module(
        InvalidConfigurationConnection,
        SimpleNamespace(GET_DTC=_FakeCommand(0x03)),
    )
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    driver = Elm327Driver(port="/dev/fake")

    with pytest.raises(DriverError, match="invalid ELM327 driver configuration"):
        await driver.get_vehicle_status("elm327")
    await driver.close()


@pytest.mark.asyncio
async def test_elm327_uses_only_private_vetted_read_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = SimpleNamespace(
        ENGINE_SPEED=_FakeCommand(0x01),
        GET_DTC=_FakeCommand(0x03),
    )
    module = _fake_obdii_module(_FakeConnection, commands)
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    _FakeConnection.dtc_value = ["P0300"]
    _FakeConnection.pid_value = 803
    driver = Elm327Driver(port="/dev/fake", timeout_seconds=1)

    readings = await driver.read_standard_pids("elm327", ("010C",))
    dtcs = await driver.read_dtcs("elm327")
    connection = _FakeConnection.instances[0]

    assert readings[0].value == 803
    assert readings[0].source is DataSource.STANDARD
    assert dtcs[0].code == "P0300"
    assert [command.mode for command in connection.queries] == [0x01, 0x03]
    assert connection.kwargs["auto_connect"] is False
    assert connection.kwargs["protocol"] is _FAKE_PROTOCOL
    assert not hasattr(driver, "query")
    assert not hasattr(driver, "raw_command")
    assert (await driver.get_vehicle_status("elm327")).ecus[0].ecu_id == "engine"
    with pytest.raises(VehicleNotFoundError):
        await driver.read_dtcs("other")

    read = ProfileReadDefinition.model_validate(
        {
            "name": "Enhanced",
            "ecu_id": "engine",
            "service": "0x22",
            "identifier": "0xF40D",
            "signal_id": "enhanced",
            "decoder": {"data_type": "uint16", "byte_length": 2},
        }
    )
    with pytest.raises(UnsupportedOperationError):
        await driver.read_ecu_snapshot("elm327", "engine", (read,))

    await driver.close()
    await driver.close()
    assert connection.closed is True


@pytest.mark.asyncio
async def test_elm327_mode03_policy_denial_occurs_before_adapter_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DenyMode03:
        def authorize_obd_service(
            self,
            service_id: int,
            *,
            pid: str | int | None = None,
        ) -> None:
            del pid
            if service_id == 0x03:
                raise PolicyDeniedError("Mode 03 denied for test")

    module = _fake_obdii_module(
        _FakeConnection,
        SimpleNamespace(GET_DTC=_FakeCommand(0x03)),
    )
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    driver = Elm327Driver(port="/dev/fake")
    driver._policy = DenyMode03()  # type: ignore[assignment]

    with pytest.raises(PolicyDeniedError, match="Mode 03"):
        await driver.read_dtcs("elm327")

    assert _FakeConnection.instances == []


@pytest.mark.asyncio
async def test_elm327_rejects_protocol_negotiation_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MismatchedProtocolConnection(_FakeConnection):
        def connect(self) -> None:
            super().connect()
            self.protocol = object()

    module = _fake_obdii_module(
        MismatchedProtocolConnection,
        SimpleNamespace(ENGINE_SPEED=_FakeCommand(0x01)),
    )
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    driver = Elm327Driver(port="/dev/fake")

    with pytest.raises(DriverError, match="different from the configured protocol"):
        await driver.read_standard_pids("elm327", ("010C",))

    assert _FakeConnection.instances[0].closed is True
    await driver.close()


@pytest.mark.asyncio
async def test_elm327_mismatched_retained_connection_cannot_be_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetainedMismatchConnection(_FakeConnection):
        query_calls = 0

        def connect(self) -> None:
            super().connect()
            self.protocol = object()

        def query(self, command: _FakeCommand) -> SimpleNamespace:
            type(self).query_calls += 1
            return super().query(command)

        def close(self) -> None:
            raise OSError("close failed")

    module = _fake_obdii_module(
        RetainedMismatchConnection,
        SimpleNamespace(ENGINE_SPEED=_FakeCommand(0x01)),
    )
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    RetainedMismatchConnection.query_calls = 0
    driver = Elm327Driver(port="/dev/fake")

    with pytest.raises(DriverError, match="different from the configured protocol"):
        await driver.read_standard_pids("elm327", ("010C",))
    with pytest.raises(DriverError, match="remains quarantined"):
        await driver.read_standard_pids("elm327", ("010C",))

    assert RetainedMismatchConnection.query_calls == 0


@pytest.mark.asyncio
async def test_elm327_failed_connect_retained_handle_is_quarantined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedConnectRetainedConnection(_FakeConnection):
        query_calls = 0

        def connect(self) -> None:
            self.connected = True
            raise OSError("initialization failed")

        def query(self, command: _FakeCommand) -> SimpleNamespace:
            type(self).query_calls += 1
            return super().query(command)

        def close(self) -> None:
            raise OSError("close failed")

    module = _fake_obdii_module(
        FailedConnectRetainedConnection,
        SimpleNamespace(ENGINE_SPEED=_FakeCommand(0x01)),
    )
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    FailedConnectRetainedConnection.query_calls = 0
    driver = Elm327Driver(port="/dev/fake")

    with pytest.raises(DriverError, match="failed to connect"):
        await driver.read_standard_pids("elm327", ("010C",))
    with pytest.raises(DriverError, match="remains quarantined"):
        await driver.read_standard_pids("elm327", ("010C",))

    assert FailedConnectRetainedConnection.query_calls == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"port": ""}, "port"),
        ({"port": "/dev/fake", "baudrate": 0}, "baudrate"),
        ({"port": "/dev/fake", "baudrate": 1_199}, "baudrate"),
        ({"port": "/dev/fake", "baudrate": 2_000_001}, "baudrate"),
        ({"port": "/dev/fake", "baudrate": float("nan")}, "baudrate"),
        ({"port": "/dev/fake", "baudrate": float("inf")}, "baudrate"),
        ({"port": "/dev/fake", "baudrate": 10**1_000}, "baudrate"),
        ({"port": "/dev/fake", "timeout_seconds": 0}, "timeout_seconds"),
        ({"port": "/dev/fake", "timeout_seconds": 0.09}, "timeout_seconds"),
        ({"port": "/dev/fake", "timeout_seconds": 5.01}, "timeout_seconds"),
        ({"port": "/dev/fake", "timeout_seconds": float("nan")}, "timeout_seconds"),
        ({"port": "/dev/fake", "timeout_seconds": float("inf")}, "timeout_seconds"),
        ({"port": "/dev/fake", "timeout_seconds": 10**1_000}, "timeout_seconds"),
        ({"port": "/dev/fake", "protocol": "auto"}, "protocol"),
        ({"port": "/dev/fake", "protocol": "sae_j1939_can"}, "protocol"),
    ],
)
def test_elm327_rejects_invalid_connection_settings(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        Elm327Driver(**kwargs)


@pytest.mark.parametrize(
    ("baudrate", "timeout_seconds"),
    [
        (1_200, 0.1),
        (2_000_000, 5.0),
    ],
)
def test_elm327_accepts_documented_connection_setting_boundaries(
    baudrate: int,
    timeout_seconds: float,
) -> None:
    Elm327Driver(
        port="/dev/fake",
        baudrate=baudrate,
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.asyncio
async def test_elm327_rejects_wrong_mode_and_malformed_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = SimpleNamespace(
        ENGINE_SPEED=_FakeCommand(0x02),
        GET_DTC=_FakeCommand(0x03),
    )
    module = _fake_obdii_module(_FakeConnection, commands)
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    _FakeConnection.dtc_value = [123, "not-a-code", "P0300"]
    driver = Elm327Driver(port="/dev/fake")

    with pytest.raises(DriverError, match="mode check"):
        await driver.read_standard_pids("elm327", ("010C",))
    assert [dtc.code for dtc in await driver.read_dtcs("elm327")] == ["P0300"]
    with pytest.raises(DriverError, match="ECU not found"):
        await driver.read_dtcs("elm327", "body")
    await driver.close()


@pytest.mark.asyncio
async def test_elm327_rejects_allowlisted_name_mapped_to_the_wrong_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = SimpleNamespace(ENGINE_SPEED=_FakeCommand(0x01, pid=0x0D))
    module = _fake_obdii_module(_FakeConnection, commands)
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    driver = Elm327Driver(port="/dev/fake")

    with pytest.raises(DriverError, match="fixed PID check"):
        await driver.read_standard_pids("elm327", ("010C",))

    assert _FakeConnection.instances[0].queries == []
    await driver.close()


@pytest.mark.asyncio
async def test_elm327_rejects_mode03_command_with_unexpected_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = SimpleNamespace(GET_DTC=_FakeCommand(0x03, pid=0x0C))
    module = _fake_obdii_module(_FakeConnection, commands)
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    driver = Elm327Driver(port="/dev/fake")

    with pytest.raises(DriverError, match="fixed payload check"):
        await driver.read_dtcs("elm327")

    assert _FakeConnection.instances[0].queries == []
    await driver.close()


@pytest.mark.asyncio
async def test_elm327_caps_third_party_dtc_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = SimpleNamespace(GET_DTC=_FakeCommand(0x03))
    module = _fake_obdii_module(_FakeConnection, commands)
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    _FakeConnection.dtc_value = ["P0300"] * 300
    driver = Elm327Driver(port="/dev/fake")

    dtcs = await driver.read_dtcs("elm327")

    assert len(dtcs) == 256
    await driver.close()


@pytest.mark.asyncio
async def test_elm327_closes_stale_connection_before_replacing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = SimpleNamespace(ENGINE_SPEED=_FakeCommand(0x01))
    module = _fake_obdii_module(_FakeConnection, commands)
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    driver = Elm327Driver(port="/dev/fake")

    await driver.read_standard_pids("elm327", ("010C",))
    stale = _FakeConnection.instances[0]
    stale.connected = False

    await driver.read_standard_pids("elm327", ("010C",))

    assert stale.closed is True
    assert len(_FakeConnection.instances) == 2
    assert _FakeConnection.instances[1].queries
    await driver.close()


@pytest.mark.asyncio
async def test_elm327_rejects_commands_without_a_verifiable_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = SimpleNamespace(
        ENGINE_SPEED=SimpleNamespace(),
        GET_DTC=_FakeCommand(0x03),
    )
    module = _fake_obdii_module(_FakeConnection, commands)
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    driver = Elm327Driver(port="/dev/fake")

    with pytest.raises(DriverError, match="verifiable mode"):
        await driver.read_standard_pids("elm327", ("010C",))
    await driver.close()


@pytest.mark.parametrize("mode", [True, 1.0, "1"])
@pytest.mark.asyncio
async def test_elm327_requires_an_integral_non_boolean_command_mode(
    monkeypatch: pytest.MonkeyPatch,
    mode: Any,
) -> None:
    commands = SimpleNamespace(ENGINE_SPEED=SimpleNamespace(mode=mode))
    module = _fake_obdii_module(_FakeConnection, commands)
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    driver = Elm327Driver(port="/dev/fake")

    with pytest.raises(DriverError, match="invalid command mode"):
        await driver.read_standard_pids("elm327", ("010C",))
    await driver.close()


@pytest.mark.asyncio
async def test_elm327_silences_dependency_traffic_when_application_logging_is_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class LoggingConnection(_FakeConnection):
        def query(self, command: _FakeCommand) -> SimpleNamespace:
            dependency_logger = logging.getLogger("obdii.connection")
            dependency_logger.debug("sensitive-command-token")
            response = super().query(command)
            dependency_logger.debug("sensitive-response-token")
            return response

    commands = SimpleNamespace(ENGINE_SPEED=_FakeCommand(0x01))
    module = _fake_obdii_module(LoggingConnection, commands)
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    driver = Elm327Driver(port="/dev/fake")
    await driver.get_vehicle_status("elm327")

    package_logger = logging.getLogger("obdii")
    package_logger.disabled = False
    package_logger.handlers.clear()
    package_logger.propagate = True
    package_logger.setLevel(logging.NOTSET)
    dependency_logger = logging.getLogger("obdii.connection")
    dependency_logger.disabled = False
    dependency_logger.handlers.clear()
    dependency_logger.propagate = True
    dependency_logger.setLevel(logging.NOTSET)
    caplog.set_level(logging.DEBUG)

    await driver.read_standard_pids("elm327", ("010C",))

    assert "sensitive-command-token" not in caplog.text
    assert "sensitive-response-token" not in caplog.text
    await driver.close()


@pytest.mark.asyncio
async def test_elm327_cancellation_keeps_io_serialized_until_worker_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingConnection(_FakeConnection):
        started = threading.Event()
        release = threading.Event()
        counter_lock = threading.Lock()
        active_queries = 0
        max_active_queries = 0
        query_calls = 0

        def query(self, command: _FakeCommand) -> SimpleNamespace:
            connection_type = type(self)
            with connection_type.counter_lock:
                connection_type.active_queries += 1
                connection_type.query_calls += 1
                connection_type.max_active_queries = max(
                    connection_type.max_active_queries,
                    connection_type.active_queries,
                )
            connection_type.started.set()
            try:
                if not connection_type.release.wait(timeout=2):
                    raise TimeoutError("test query did not receive release signal")
                return super().query(command)
            finally:
                with connection_type.counter_lock:
                    connection_type.active_queries -= 1

    commands = SimpleNamespace(
        ENGINE_SPEED=_FakeCommand(0x01),
        VEHICLE_SPEED=_FakeCommand(0x01, pid=0x0D),
    )
    module = _fake_obdii_module(BlockingConnection, commands)
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    driver = Elm327Driver(port="/dev/fake")

    first = asyncio.create_task(driver.read_standard_pids("elm327", ("010C", "010D")))
    assert await asyncio.to_thread(BlockingConnection.started.wait, 1.0)
    first.cancel()
    second = asyncio.create_task(driver.read_standard_pids("elm327", ("010C",)))

    await asyncio.sleep(0.05)
    assert first.done() is False
    assert second.done() is False
    assert BlockingConnection.query_calls == 1
    assert BlockingConnection.max_active_queries == 1

    BlockingConnection.release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await second)[0].value == _FakeConnection.pid_value
    assert BlockingConnection.query_calls == 2
    assert BlockingConnection.max_active_queries == 1
    await driver.close()


@pytest.mark.asyncio
async def test_elm327_cancelled_close_finishes_sync_close_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingCloseConnection(_FakeConnection):
        close_started = threading.Event()
        close_release = threading.Event()

        def close(self) -> None:
            self.close_started.set()
            if not self.close_release.wait(timeout=2):
                raise TimeoutError("test close did not receive release signal")
            super().close()

    commands = SimpleNamespace(ENGINE_SPEED=_FakeCommand(0x01))
    module = _fake_obdii_module(BlockingCloseConnection, commands)
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    driver = Elm327Driver(port="/dev/fake")
    await driver.read_standard_pids("elm327", ("010C",))

    close_task = asyncio.create_task(driver.close())
    assert await asyncio.to_thread(BlockingCloseConnection.close_started.wait, 1.0)
    close_task.cancel()
    await asyncio.sleep(0.05)
    assert close_task.done() is False

    BlockingCloseConnection.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert _FakeConnection.instances[0].closed is True
    with pytest.raises(DriverError, match="closed"):
        await driver.read_standard_pids("elm327", ("010C",))
    await driver.close()


@pytest.mark.asyncio
async def test_elm327_failed_close_retains_handle_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetryableCloseConnection(_FakeConnection):
        close_attempts = 0

        def close(self) -> None:
            type(self).close_attempts += 1
            if type(self).close_attempts == 1:
                raise OSError("temporary close failure")
            super().close()

    commands = SimpleNamespace(ENGINE_SPEED=_FakeCommand(0x01))
    module = _fake_obdii_module(RetryableCloseConnection, commands)
    monkeypatch.setattr(importlib, "import_module", lambda _name: module)
    _FakeConnection.instances.clear()
    driver = Elm327Driver(port="/dev/fake")
    await driver.read_standard_pids("elm327", ("010C",))

    with pytest.raises(DriverError, match="failed to close"):
        await driver.close()
    assert (await driver.read_standard_pids("elm327", ("010C",)))[0].value == 803

    await driver.close()
    await driver.close()
    assert RetryableCloseConnection.close_attempts == 2
    assert _FakeConnection.instances[0].closed is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (7, 7),
        (2.5, 2.5),
        ("ready", "ready"),
        ([4], 4),
        ([1, 2], "1, 2"),
    ],
)
def test_elm327_scalar_normalization(value: Any, expected: Any) -> None:
    assert Elm327Driver._scalar(value) == expected


class _FakeEntryPoints(list[Any]):
    def select(self, *, group: str) -> _FakeEntryPoints:
        assert group == "obd_mcp.drivers"
        return self


class _FakeEntryPoint:
    name = "plugin-sim"

    @staticmethod
    def load() -> type[SimulatorDriver]:
        return SimulatorDriver


def test_registry_exposes_builtins_and_entry_point_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        registry_module.metadata,
        "entry_points",
        lambda: _FakeEntryPoints([_FakeEntryPoint()]),
    )
    default_registry = DriverRegistry()
    assert default_registry.names() == ("elm327", "simulator")
    with pytest.raises(DriverUnavailableError):
        default_registry.create("plugin-sim")

    registry = DriverRegistry(allow_third_party=True)

    assert registry.names() == ("elm327", "plugin-sim", "simulator")
    assert isinstance(registry.create("plugin-sim"), DiagnosticDriver)
    with pytest.raises(DriverUnavailableError):
        registry.create("missing")


def test_registry_rejects_builtin_override_and_non_driver_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = SimpleNamespace(name="simulator", load=lambda: SimulatorDriver)
    monkeypatch.setattr(
        registry_module.metadata,
        "entry_points",
        lambda: _FakeEntryPoints([override]),
    )
    with pytest.raises(DriverError, match="override"):
        DriverRegistry(allow_third_party=True).names()

    bad = SimpleNamespace(name="bad", load=lambda: (lambda: object()))
    monkeypatch.setattr(
        registry_module.metadata,
        "entry_points",
        lambda: _FakeEntryPoints([bad]),
    )
    with pytest.raises(DriverUnavailableError, match="did not return"):
        DriverRegistry(allow_third_party=True).create("bad")
