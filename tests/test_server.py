from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ResourceError, ToolError

import obd_mcp.server as server_module
from obd_mcp.config import (
    AppConfig,
    ExtensionsConfig,
    ServerConfig,
    StorageConfig,
    VehicleConfig,
)
from obd_mcp.domain import (
    ConnectionState,
    DiagnosticIssue,
    DiagnosticTroubleCode,
    DTCState,
    ECURef,
    EcuSnapshot,
    IssueSeverity,
    IssueTimeline,
    SignalQuality,
    SignalReading,
    SignalSource,
    TimelineEvent,
    TimelineEventType,
    TransportProtocol,
    Vehicle,
    VehicleStatus,
)
from obd_mcp.drivers.registry import DriverRegistry
from obd_mcp.drivers.simulator import SimulatorDriver
from obd_mcp.errors import DriverError, VehicleNotFoundError
from obd_mcp.server import create_server

EXPECTED_TOOLS = {
    "obd_list_vehicles",
    "obd_get_vehicle_status",
    "obd_read_standard_pids",
    "obd_read_dtcs",
    "obd_read_ecu_snapshot",
    "obd_open_issue",
    "obd_get_issue_timeline",
}


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.vehicle = Vehicle(
            vehicle_id="demo",
            display_name="Demo",
            protocol=TransportProtocol.SIMULATED,
            connection_state=ConnectionState.CONNECTED,
        )
        self.status = VehicleStatus(
            vehicle=self.vehicle,
            ecus=(
                ECURef(
                    ecu_id="engine",
                    name="Engine ECU",
                    protocol=TransportProtocol.SIMULATED,
                ),
            ),
        )
        self.reading = SignalReading(
            vehicle_id="demo",
            ecu_id="engine",
            pid="010C",
            signal_id="engine_speed",
            name="Engine speed",
            value=800,
            unit="rpm",
            quality=SignalQuality.SYNTHETIC,
            source=SignalSource.SYNTHETIC,
        )
        self.dtc = DiagnosticTroubleCode(
            vehicle_id="demo",
            ecu_id="engine",
            code="P0300",
            state=DTCState.STORED,
        )
        self.snapshot = EcuSnapshot(
            vehicle_id="demo",
            ecu_id="engine",
            protocol=TransportProtocol.SIMULATED,
            signals=(self.reading,),
            dtcs=(self.dtc,),
        )
        self.issue = DiagnosticIssue(
            issue_id="issue-test",
            vehicle_id="demo",
            title="Check engine observation",
        )
        self.timeline = IssueTimeline(
            issue=self.issue,
            events=(
                TimelineEvent(
                    event_id="event-test",
                    issue_id="issue-test",
                    vehicle_id="demo",
                    event_type=TimelineEventType.OPENED,
                ),
            ),
        )

    async def list_vehicles(self) -> tuple[Vehicle, ...]:
        self.calls.append(("list", None))
        return (self.vehicle,)

    async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
        self.calls.append(("status", vehicle_id))
        return self.status

    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str] | None = None,
    ) -> tuple[SignalReading, ...]:
        self.calls.append(("pids", (vehicle_id, tuple(pids or ()))))
        return (self.reading,)

    async def read_dtcs(
        self,
        vehicle_id: str,
        ecu_id: str | None = None,
    ) -> tuple[DiagnosticTroubleCode, ...]:
        self.calls.append(("dtcs", (vehicle_id, ecu_id)))
        return (self.dtc,)

    async def read_ecu_snapshot(
        self,
        vehicle_id: str,
        ecu_id: str,
        *,
        profile_id: str | None = None,
    ) -> EcuSnapshot:
        self.calls.append(("snapshot", (vehicle_id, ecu_id, profile_id)))
        return self.snapshot

    async def open_issue(
        self,
        vehicle_id: str,
        title: str,
        description: str | None = None,
        *,
        severity: IssueSeverity = IssueSeverity.MEDIUM,
        dtc_codes: Sequence[str] = (),
    ) -> DiagnosticIssue:
        self.calls.append(
            (
                "open_issue",
                (vehicle_id, title, description, severity, tuple(dtc_codes)),
            )
        )
        return self.issue

    async def get_issue_timeline(self, issue_id: str) -> IssueTimeline:
        self.calls.append(("timeline", issue_id))
        return self.timeline


@pytest.mark.asyncio
async def test_server_exposes_only_the_seven_typed_tools() -> None:
    server = create_server(AppConfig(), service=FakeService())

    tools = {tool.name: tool for tool in await server.list_tools()}

    assert set(tools) == EXPECTED_TOOLS
    assert all(tool.outputSchema["type"] == "object" for tool in tools.values())
    pid_array_schema = tools["obd_read_standard_pids"].inputSchema["properties"]["pids"]["anyOf"][0]
    issue_properties = tools["obd_open_issue"].inputSchema["properties"]
    assert pid_array_schema["maxItems"] == 9
    assert issue_properties["title"]["maxLength"] == 256
    assert issue_properties["description"]["anyOf"][0]["maxLength"] == 8_192
    assert issue_properties["dtc_codes"]["anyOf"][0]["maxItems"] == 64
    assert "profile_id" not in tools["obd_read_ecu_snapshot"].inputSchema["properties"]
    for name, tool in tools.items():
        assert tool.annotations is not None
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
        if name == "obd_open_issue":
            assert tool.annotations.readOnlyHint is False
            assert tool.annotations.idempotentHint is False
        else:
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.idempotentHint is True


def test_server_explicitly_enables_dns_rebinding_protection_for_loopback_aliases() -> None:
    server = create_server(
        AppConfig(server=ServerConfig(host="127.0.0.2")),
        service=FakeService(),
    )

    security = server.settings.transport_security
    assert security is not None
    assert security.enable_dns_rebinding_protection is True
    assert "127.0.0.2:*" in security.allowed_hosts


@pytest.mark.asyncio
async def test_tool_calls_delegate_typed_intent_to_service() -> None:
    service = FakeService()
    server = create_server(AppConfig(), service=service)

    await server.call_tool(
        "obd_read_standard_pids",
        {"vehicle_id": "demo", "pids": ["010C"]},
    )
    await server.call_tool(
        "obd_open_issue",
        {
            "vehicle_id": "demo",
            "title": "Observe misfire",
            "severity": "high",
            "dtc_codes": ["P0300"],
        },
    )

    assert ("pids", ("demo", ("010C",))) in service.calls
    assert (
        "open_issue",
        (
            "demo",
            "Observe misfire",
            None,
            IssueSeverity.HIGH,
            ("P0300",),
        ),
    ) in service.calls


@pytest.mark.asyncio
async def test_server_publishes_read_only_safety_resource_and_prompt() -> None:
    server = create_server(AppConfig(), service=FakeService())

    resources = await server.list_resources()
    prompts = await server.list_prompts()

    assert [str(resource.uri) for resource in resources] == ["obd://safety/read-only"]
    assert [prompt.name for prompt in prompts] == ["obd_read_only_safety"]
    resource_contents = await server.read_resource("obd://safety/read-only")
    prompt_result = await server.get_prompt("obd_read_only_safety")
    assert "observation-only" in resource_contents[0].content
    assert "observation-only" in prompt_result.messages[0].content.text


@pytest.mark.asyncio
async def test_prompt_and_resource_errors_do_not_echo_raw_vin() -> None:
    raw_vin = "A" * 17
    server = create_server(AppConfig(), service=FakeService())

    with pytest.raises(ValueError, match="withheld") as prompt_error:
        await server.get_prompt(raw_vin)
    with pytest.raises(ResourceError, match="withheld") as resource_error:
        await server.read_resource(f"obd://{raw_vin}")

    assert raw_vin not in str(prompt_error.value)
    assert raw_vin not in str(resource_error.value)


@pytest.mark.asyncio
async def test_percent_encoded_vin_resource_is_withheld() -> None:
    raw_vin = "A" * 17
    encoded_vin = "".join(f"%{ord(character):02X}" for character in raw_vin)
    server = create_server(AppConfig(), service=FakeService())

    with pytest.raises(ResourceError, match="withheld") as resource_error:
        await server.read_resource(f"obd://unknown/{encoded_vin}")

    assert raw_vin not in str(resource_error.value)
    assert encoded_vin not in str(resource_error.value)


@pytest.mark.asyncio
async def test_prompt_and_resource_errors_bound_large_safe_inputs() -> None:
    huge = "x" * 2_000_000
    server = create_server(AppConfig(), service=FakeService())

    with pytest.raises(ValueError, match="withheld") as prompt_error:
        await server.get_prompt(huge)
    with pytest.raises(ResourceError) as resource_error:
        await server.read_resource(f"obd://{huge}")

    assert len(str(prompt_error.value)) < 100
    assert len(str(resource_error.value)) < 100


@pytest.mark.asyncio
async def test_remaining_read_tools_delegate_to_service() -> None:
    service = FakeService()
    server = create_server(AppConfig(), service=service)

    await server.call_tool("obd_list_vehicles", {})
    await server.call_tool("obd_get_vehicle_status", {"vehicle_id": "demo"})
    await server.call_tool(
        "obd_read_dtcs",
        {"vehicle_id": "demo", "ecu_id": "engine"},
    )
    await server.call_tool(
        "obd_read_ecu_snapshot",
        {"vehicle_id": "demo", "ecu_id": "engine"},
    )
    await server.call_tool("obd_get_issue_timeline", {"issue_id": "issue-test"})

    assert ("list", None) in service.calls
    assert ("status", "demo") in service.calls
    assert ("dtcs", ("demo", "engine")) in service.calls
    assert ("snapshot", ("demo", "engine", None)) in service.calls
    assert ("timeline", "issue-test") in service.calls


@pytest.mark.asyncio
async def test_expected_service_error_becomes_bounded_tool_error() -> None:
    class FailingService(FakeService):
        async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
            raise VehicleNotFoundError(f"vehicle not found: {vehicle_id}")

    server = create_server(AppConfig(), service=FailingService())

    with pytest.raises(ToolError, match="vehicle_not_found: diagnostic operation failed safely"):
        await server.call_tool("obd_get_vehicle_status", {"vehicle_id": "missing"})


@pytest.mark.asyncio
async def test_unexpected_and_sensitive_errors_are_bounded() -> None:
    vin = "A" * 17

    class UnexpectedService(FakeService):
        async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
            raise RuntimeError(f"private response {vin}")

    class SensitiveExpectedService(FakeService):
        async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
            raise DriverError(f"adapter exposed {vin}")

    for service in (UnexpectedService(), SensitiveExpectedService()):
        server = create_server(AppConfig(), service=service)
        with pytest.raises(ToolError) as exc_info:
            await server.call_tool("obd_get_vehicle_status", {"vehicle_id": "demo"})
        assert vin not in str(exc_info.value)
        assert "failed safely" in str(exc_info.value)


@pytest.mark.parametrize("container", ["text", "bytes", "set"])
@pytest.mark.asyncio
async def test_outbound_privacy_guard_catches_mutated_driver_metadata(
    container: str,
) -> None:
    service = FakeService()
    vin = "A" * 17
    leaked: object
    if container == "bytes":
        leaked = vin.encode()
    elif container == "set":
        leaked = {vin}
    else:
        leaked = f"late mutation {vin}"
    service.vehicle.metadata["note"] = leaked
    server = create_server(AppConfig(), service=service)

    with pytest.raises(ToolError, match="privacy_error") as exc_info:
        await server.call_tool("obd_list_vehicles", {})

    assert vin not in str(exc_info.value)


@pytest.mark.asyncio
async def test_final_tool_boundary_rejects_separator_formatted_vin() -> None:
    service = FakeService()
    separated_vin = "-".join(("AAAA", "AAAA", "AAAA", "AAAA", "A"))
    service.vehicle.metadata["note"] = f"VIN: {separated_vin}"
    server = create_server(AppConfig(), service=service)

    with pytest.raises(ToolError, match="privacy_error") as exc_info:
        await server.call_tool("obd_list_vehicles", {})

    assert separated_vin not in str(exc_info.value)


@pytest.mark.parametrize("separator", ["-", "_", " "])
@pytest.mark.asyncio
async def test_final_tool_boundary_rejects_unlabeled_delimited_vin(
    separator: str,
) -> None:
    service = FakeService()
    separated_vin = separator.join(("1AA", "AAAAA", "AAA", "AA4352"))
    service.vehicle.metadata["note"] = separated_vin
    server = create_server(AppConfig(), service=service)

    with pytest.raises(ToolError, match="privacy_error") as exc_info:
        await server.call_tool("obd_list_vehicles", {})

    assert separated_vin not in str(exc_info.value)


@pytest.mark.asyncio
async def test_final_tool_boundary_rejects_oversized_serialized_output() -> None:
    service = FakeService()
    service.vehicle.metadata["oversized"] = "x" * 1_048_577
    server = create_server(AppConfig(), service=service)

    with pytest.raises(ToolError, match="privacy_error"):
        await server.call_tool("obd_list_vehicles", {})


@pytest.mark.asyncio
async def test_final_fastmcp_result_size_includes_text_and_structured_copies() -> None:
    service = FakeService()
    service.vehicle.metadata["payload"] = "x" * 550_000
    server = create_server(AppConfig(), service=service)

    with pytest.raises(ToolError, match="output_safety_error"):
        await server.call_tool("obd_list_vehicles", {})


@pytest.mark.asyncio
async def test_sdk_argument_validation_never_echoes_rejected_input() -> None:
    vin = "".join(("1HG", "CM826", "33A004352"))
    server = create_server(AppConfig(), service=FakeService())

    with pytest.raises(ToolError, match="validation_error") as exc_info:
        await server.call_tool(
            "obd_get_vehicle_status",
            {"vehicle_id": vin},
        )

    assert vin not in str(exc_info.value)


@pytest.mark.asyncio
async def test_invalid_obd_dtc_first_digit_is_rejected_at_tool_boundary() -> None:
    server = create_server(AppConfig(), service=FakeService())

    with pytest.raises(ToolError, match="validation_error"):
        await server.call_tool(
            "obd_open_issue",
            {
                "vehicle_id": "demo",
                "title": "Invalid code",
                "dtc_codes": ["PA000"],
            },
        )


@pytest.mark.asyncio
async def test_expected_driver_errors_are_bounded_before_mcp() -> None:
    class HugeErrorService(FakeService):
        async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
            del vehicle_id
            raise DriverError("x" * 2_000_000)

    server = create_server(AppConfig(), service=HugeErrorService())

    with pytest.raises(ToolError) as exc_info:
        await server.call_tool("obd_get_vehicle_status", {"vehicle_id": "demo"})

    assert len(str(exc_info.value)) < 200
    assert "failed safely" in str(exc_info.value)


@pytest.mark.asyncio
async def test_short_driver_transport_text_is_not_exposed_to_mcp() -> None:
    raw_transport_text = "7E8 03 7F 22 31"

    class RawErrorService(FakeService):
        async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
            del vehicle_id
            raise DriverError(raw_transport_text)

    server = create_server(AppConfig(), service=RawErrorService())

    with pytest.raises(ToolError) as exc_info:
        await server.call_tool("obd_get_vehicle_status", {"vehicle_id": "demo"})

    assert raw_transport_text not in str(exc_info.value)
    assert "failed safely" in str(exc_info.value)


@pytest.mark.asyncio
async def test_sdk_and_application_logs_withhold_raw_vehicle_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    vin = "".join(("1HG", "CM826", "33A004352"))
    caplog.set_level(logging.DEBUG)
    server = create_server(AppConfig(), service=FakeService())
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)

    logging.getLogger("obd_mcp.test").warning("observed vehicle %s", vin)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            headers={"host": f"{vin}.invalid"},
            json={},
        )

    assert response.status_code in {400, 421}
    assert vin not in caplog.text
    assert "withheld" in caplog.text
    assert logging.getLogger("mcp").getEffectiveLevel() == logging.CRITICAL


@pytest.mark.asyncio
async def test_streamable_http_rejects_large_declared_and_chunked_bodies() -> None:
    server = create_server(AppConfig(), service=FakeService())
    transport = httpx.ASGITransport(app=server.streamable_http_app())
    oversized = b"x" * 1_048_577

    async def chunks() -> AsyncIterator[bytes]:
        yield oversized[:600_000]
        yield oversized[600_000:]

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8000",
    ) as client:
        declared = await client.post(
            "/mcp",
            content=oversized,
            headers={"content-type": "application/json"},
        )
        chunked = await client.post(
            "/mcp",
            content=chunks(),
            headers={"content-type": "application/json"},
        )

    assert declared.status_code == 413
    assert chunked.status_code == 413


@pytest.mark.asyncio
async def test_default_runtime_initializes_safe_simulator_and_local_store() -> None:
    config = AppConfig(storage=StorageConfig(path=Path(":memory:")))
    server = create_server(config)

    async with server.runtime_lifespan():
        await server.call_tool("obd_list_vehicles", {})
        result = await server.call_tool(
            "obd_get_vehicle_status",
            {"vehicle_id": "demo"},
        )

    assert result


def test_runtime_passes_explicit_third_party_driver_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[bool] = []

    def registry_factory(*, allow_third_party: bool = False) -> DriverRegistry:
        observed.append(allow_third_party)
        return DriverRegistry(allow_third_party=allow_third_party)

    monkeypatch.setattr(server_module, "DriverRegistry", registry_factory)
    config = AppConfig(
        extensions=ExtensionsConfig(allow_third_party_drivers=True),
        storage=StorageConfig(path=Path(":memory:")),
    )

    create_server(config)

    assert observed == [True]


@pytest.mark.asyncio
async def test_http_runner_disables_access_logging_and_late_log_reconfiguration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class FakeConfig:
        def __init__(self, app: object, **kwargs: object) -> None:
            observed["app"] = app
            observed.update(kwargs)

    class FakeUvicornServer:
        def __init__(self, config: FakeConfig) -> None:
            observed["config"] = config

        async def serve(self) -> None:
            observed["served"] = True

    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(Config=FakeConfig, Server=FakeUvicornServer),
    )
    server = create_server(AppConfig(), service=FakeService())

    await server.run_streamable_http_async()

    assert observed["access_log"] is False
    assert observed["log_config"] is None
    assert observed["served"] is True


@pytest.mark.asyncio
async def test_configured_fleet_rejects_driver_vehicle_identity_mismatch() -> None:
    driver = SimulatorDriver(
        vehicles=({"vehicle_id": "advertised-other", "display_name": "Other"},)
    )
    fleet = server_module._ConfiguredFleetDriver({"configured": driver})

    with pytest.raises(DriverError, match="assigned vehicle id"):
        await fleet.list_vehicles()


def test_server_core_rejects_duplicate_elm327_physical_endpoint(
    tmp_path: Path,
) -> None:
    port = tmp_path / "adapter"
    port.touch()
    config = AppConfig(
        vehicles=[
            VehicleConfig(
                id="hardware-one",
                driver="elm327",
                options={"port": str(port)},
            )
        ]
    )
    config.vehicles.append(
        VehicleConfig(
            id="hardware-two",
            driver="elm327",
            options={"port": str(port)},
        )
    )

    with pytest.raises(ValueError, match="same physical port"):
        create_server(config)


def test_server_core_revalidates_mutated_duplicate_vehicle_ids() -> None:
    config = AppConfig()
    config.vehicles.append(VehicleConfig(id="demo", name="Duplicate must not replace original"))

    with pytest.raises(ValueError, match="unique"):
        create_server(config)


def test_server_core_revalidates_mutated_vehicle_count() -> None:
    config = AppConfig()
    config.vehicles.extend(VehicleConfig(id=f"vehicle-{index}") for index in range(128))

    with pytest.raises(ValueError, match="at most 128 items"):
        create_server(config)


@pytest.mark.asyncio
async def test_configured_fleet_routes_each_vehicle_through_its_driver() -> None:
    config = AppConfig(
        storage=StorageConfig(path=Path(":memory:")),
        vehicles=[
            VehicleConfig(id="demo-one", name="Demo One"),
            VehicleConfig(id="demo-two", name="Demo Two"),
        ],
    )
    server = create_server(config)

    async with server._mcp_server.lifespan(server._mcp_server):
        await server.call_tool("obd_list_vehicles", {})
        await server.call_tool(
            "obd_get_vehicle_status",
            {"vehicle_id": "demo-two"},
        )
        await server.call_tool(
            "obd_read_standard_pids",
            {"vehicle_id": "demo-two", "pids": ["010C"]},
        )
        await server.call_tool(
            "obd_read_dtcs",
            {"vehicle_id": "demo-two", "ecu_id": "engine"},
        )
        await server.call_tool(
            "obd_read_ecu_snapshot",
            {"vehicle_id": "demo-two", "ecu_id": "engine"},
        )
        with pytest.raises(ToolError, match="vehicle_not_found"):
            await server.call_tool(
                "obd_get_vehicle_status",
                {"vehicle_id": "missing"},
            )
