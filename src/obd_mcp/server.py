"""Thin FastMCP facade over the read-only diagnostic service."""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import AsyncIterator, Awaitable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, TypeVar
from urllib.parse import unquote

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ResourceError, ToolError
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ContentBlock, GetPromptResult, ToolAnnotations
from pydantic import AnyUrl, Field, StringConstraints, ValidationError
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import (
    AppConfig,
    VehicleConfig,
    load_config,
    revalidate_app_config,
)
from .domain import (
    DiagnosticIssue,
    DiagnosticTroubleCode,
    DomainModel,
    EcuSnapshot,
    IssueSeverity,
    IssueTimeline,
    LongText,
    PidCode,
    SafeId,
    ShortText,
    SignalReading,
    Vehicle,
    VehicleStatus,
    contains_raw_vin,
    ensure_no_raw_vin,
    ensure_safe_public_value,
)
from .drivers.base import DiagnosticDriver
from .drivers.registry import DriverRegistry
from .errors import DriverError, OBDMCPError, VehicleNotFoundError
from .policy import STANDARD_PIDS
from .profiles import ProfileLoader, ProfileReadDefinition, ProfileRegistry

_SAFETY_GUIDANCE = """\
This server is an observation-only diagnostic gateway.

- Vehicle-facing tools expose only fixed, normalized read capabilities.
- Raw frames, arbitrary service identifiers, adapter commands, DTC clearing,
  resets, coding, flashing, actuator tests, security access, and all other
  vehicle writes are unavailable.
- obd_open_issue writes only to the local SQLite issue database.
- Results are observations, not a determination that a vehicle is safe to drive.
- Full VINs are never returned; only the core's redacted identity may appear.
"""

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_LOCAL_ISSUE_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
_MAX_HTTP_BODY_BYTES = 1_048_576
_HTTP_SESSION_IDLE_TIMEOUT_SECONDS = 300.0
_MAX_ERROR_MESSAGE_CHARS = 512

T = TypeVar("T")
DtcCodeInput = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=5,
        max_length=6,
        pattern=r"^(?:[PBCU][0-3][0-9A-F]{3}|[0-9A-F]{6})$",
        to_upper=True,
    ),
]
PidListInput = Annotated[list[PidCode], Field(max_length=len(STANDARD_PIDS))]
DtcCodeListInput = Annotated[list[DtcCodeInput], Field(max_length=64)]


class DiagnosticServiceProtocol(Protocol):
    async def list_vehicles(self) -> Sequence[Vehicle]: ...

    async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus: ...

    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str] | None = None,
    ) -> Sequence[SignalReading]: ...

    async def read_dtcs(
        self,
        vehicle_id: str,
        ecu_id: str | None = None,
    ) -> Sequence[DiagnosticTroubleCode]: ...

    async def read_ecu_snapshot(
        self,
        vehicle_id: str,
        ecu_id: str,
    ) -> EcuSnapshot: ...

    async def open_issue(
        self,
        vehicle_id: str,
        title: str,
        description: str | None = None,
        *,
        severity: IssueSeverity = IssueSeverity.MEDIUM,
        dtc_codes: Sequence[str] = (),
    ) -> DiagnosticIssue: ...

    async def get_issue_timeline(self, issue_id: str) -> IssueTimeline: ...


class VehicleListResult(DomainModel):
    """Structured output for vehicle discovery."""

    vehicles: tuple[Vehicle, ...]


class StandardPidResult(DomainModel):
    """Structured output for fixed Mode 01 observations."""

    vehicle_id: SafeId
    readings: tuple[SignalReading, ...]


class DTCReadResult(DomainModel):
    """Structured output for read-only trouble-code observations."""

    vehicle_id: SafeId
    ecu_id: SafeId | None = None
    dtcs: tuple[DiagnosticTroubleCode, ...]


class AwaitableFactory(Protocol):
    def __call__(self) -> Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class _Runtime:
    service: DiagnosticServiceProtocol
    initialize: AwaitableFactory | None = None
    close: AwaitableFactory | None = None


def create_server(
    config: AppConfig | None = None,
    *,
    service: DiagnosticServiceProtocol | None = None,
) -> FastMCP[Any]:
    """Create the MCP server, optionally with an injected service for tests."""

    active_config = revalidate_app_config(config or load_config())
    runtime = _Runtime(service=service) if service is not None else _build_runtime(active_config)
    mcp = _BoundedFastMCP(
        name="obd-mcp",
        instructions=_SAFETY_GUIDANCE,
        log_level=active_config.server.log_level,
        host=active_config.server.host,
        port=active_config.server.port,
        streamable_http_path=active_config.server.http_path,
        json_response=True,
        stateless_http=False,
        transport_security=_transport_security(active_config.server.host),
    )
    mcp.bind_runtime(runtime)
    _configure_safe_logging()

    @mcp.tool(
        title="List vehicles",
        description="List configured vehicles using normalized, VIN-safe identities.",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def obd_list_vehicles() -> VehicleListResult:
        vehicles = await _invoke(runtime.service.list_vehicles())
        return VehicleListResult(vehicles=tuple(vehicles))

    @mcp.tool(
        title="Get vehicle status",
        description=(
            "Read connection and ECU status. This is an observation, not a "
            "roadworthiness determination."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def obd_get_vehicle_status(vehicle_id: SafeId) -> VehicleStatus:
        return await _invoke(runtime.service.get_vehicle_status(vehicle_id))

    @mcp.tool(
        title="Read standard PIDs",
        description=(
            "Read only fixed, centrally allowlisted Mode 01 PIDs; arbitrary "
            "commands and service identifiers are not accepted."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def obd_read_standard_pids(
        vehicle_id: SafeId,
        pids: PidListInput | None = None,
    ) -> StandardPidResult:
        readings = await _invoke(runtime.service.read_standard_pids(vehicle_id, pids))
        return StandardPidResult(vehicle_id=vehicle_id, readings=tuple(readings))

    @mcp.tool(
        title="Read diagnostic trouble codes",
        description="Read DTC observations without clearing or changing vehicle state.",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def obd_read_dtcs(
        vehicle_id: SafeId,
        ecu_id: SafeId | None = None,
    ) -> DTCReadResult:
        dtcs = await _invoke(runtime.service.read_dtcs(vehicle_id, ecu_id))
        return DTCReadResult(vehicle_id=vehicle_id, ecu_id=ecu_id, dtcs=tuple(dtcs))

    @mcp.tool(
        title="Read ECU snapshot",
        description=(
            "Read an ECU snapshot using only identifiers in a validated, "
            "source-labeled read-only profile."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def obd_read_ecu_snapshot(
        vehicle_id: SafeId,
        ecu_id: SafeId,
    ) -> EcuSnapshot:
        return await _invoke(
            runtime.service.read_ecu_snapshot(
                vehicle_id,
                ecu_id,
            )
        )

    @mcp.tool(
        title="Open local diagnostic issue",
        description=(
            "Create an issue only in the local SQLite database. This never sends "
            "a vehicle command or changes vehicle state."
        ),
        annotations=_LOCAL_ISSUE_WRITE,
        structured_output=True,
    )
    async def obd_open_issue(
        vehicle_id: SafeId,
        title: ShortText,
        description: LongText | None = None,
        severity: Literal["info", "low", "medium", "high", "critical"] = "medium",
        dtc_codes: DtcCodeListInput | None = None,
    ) -> DiagnosticIssue:
        return await _invoke(
            runtime.service.open_issue(
                vehicle_id,
                title,
                description,
                severity=IssueSeverity(severity),
                dtc_codes=dtc_codes or (),
            )
        )

    @mcp.tool(
        title="Get local issue timeline",
        description="Read a diagnostic issue timeline from the local SQLite database.",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def obd_get_issue_timeline(issue_id: SafeId) -> IssueTimeline:
        return await _invoke(runtime.service.get_issue_timeline(issue_id))

    @mcp.resource(
        "obd://safety/read-only",
        name="OBD MCP read-only safety contract",
        description="Safety and interpretation guidance for every OBD MCP result.",
        mime_type="text/markdown",
    )
    def obd_read_only_safety_resource() -> str:
        return _SAFETY_GUIDANCE

    @mcp.prompt(
        name="obd_read_only_safety",
        title="Interpret OBD observations safely",
        description="Apply the server's read-only and non-roadworthiness constraints.",
    )
    def obd_read_only_safety_prompt() -> str:
        return _SAFETY_GUIDANCE

    return mcp


def _transport_security(host: str) -> TransportSecuritySettings:
    """Keep DNS-rebinding checks explicit for every validated loopback IP."""

    authority_host = f"[{host}]" if ":" in host else host
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[authority_host, f"{authority_host}:*"],
        allowed_origins=[f"http://{authority_host}", f"http://{authority_host}:*"],
    )


class _RequestBodyLimitMiddleware:
    """Bound HTTP request bodies before Starlette or MCP parses JSON."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                await PlainTextResponse("Invalid Content-Length", status_code=400)(
                    scope, receive, send
                )
                return
            if declared_length < 0 or declared_length > self._max_bytes:
                await PlainTextResponse("Request body too large", status_code=413)(
                    scope, receive, send
                )
                return

        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            total += len(message.get("body", b""))
            if total > self._max_bytes:
                await PlainTextResponse("Request body too large", status_code=413)(
                    scope, receive, send
                )
                return
            if not message.get("more_body", False):
                break

        iterator = iter(messages)

        async def replay() -> Message:
            return next(iterator, {"type": "http.disconnect"})

        await self._app(scope, replay, send)


class _BoundedFastMCP(FastMCP[Any]):
    def bind_runtime(self, runtime: _Runtime) -> None:
        self._runtime = runtime
        self._runtime_lock = asyncio.Lock()
        self._runtime_scopes = 0
        self._runtime_finished = False

    @asynccontextmanager
    async def runtime_lifespan(self) -> AsyncIterator[None]:
        runtime = self._runtime
        if runtime.initialize is None and runtime.close is None:
            yield
            return

        async with self._runtime_lock:
            if self._runtime_finished:
                raise RuntimeError("server runtime cannot be restarted after shutdown")
            if self._runtime_scopes == 0 and runtime.initialize is not None:
                await runtime.initialize()
            self._runtime_scopes += 1

        try:
            yield
        finally:
            async with self._runtime_lock:
                self._runtime_scopes -= 1
                if self._runtime_scopes == 0:
                    try:
                        if runtime.close is not None:
                            await runtime.close()
                    finally:
                        self._runtime_finished = True

    async def run_stdio_async(self) -> None:
        async with self.runtime_lifespan():
            await super().run_stdio_async()

    async def run_streamable_http_async(self) -> None:
        """Run HTTP without request-target access logs that can expose identifiers."""

        import uvicorn

        config = uvicorn.Config(
            self.streamable_http_app(),
            host=self.settings.host,
            port=self.settings.port,
            log_level=self.settings.log_level.lower(),
            access_log=False,
            log_config=None,
        )
        await uvicorn.Server(config).serve()

    def streamable_http_app(self) -> Starlette:
        app = super().streamable_http_app()
        self.session_manager.session_idle_timeout = _HTTP_SESSION_IDLE_TIMEOUT_SECONDS
        sdk_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def process_lifespan(
            starlette_app: Starlette,
        ) -> AsyncIterator[Mapping[str, Any]]:
            async with (
                self.runtime_lifespan(),
                sdk_lifespan(starlette_app) as state,
            ):
                yield {} if state is None else state

        app.router.lifespan_context = process_lifespan
        app.add_middleware(
            _RequestBodyLimitMiddleware,
            max_bytes=_MAX_HTTP_BODY_BYTES,
        )
        return app

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        try:
            result = await super().call_tool(name, arguments)
        except ToolError as exc:
            message = str(exc)
            if (
                _has_validation_cause(exc)
                or contains_raw_vin(message)
                or len(message) > _MAX_ERROR_MESSAGE_CHARS
            ):
                raise ToolError(
                    "validation_error: tool request or result failed safe validation"
                ) from None
            raise
        try:
            ensure_safe_public_value(result)
        except ValueError:
            raise ToolError("output_safety_error: tool result was withheld") from None
        return result

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> GetPromptResult:
        try:
            if len(name) > 128:
                raise ValueError("prompt name is too long")
            ensure_no_raw_vin(name)
            ensure_safe_public_value(arguments, max_bytes=16_384)
        except ValueError:
            raise ValueError("sensitive prompt input withheld") from None
        try:
            return await super().get_prompt(name, arguments)
        except Exception as exc:
            if contains_raw_vin(str(exc)) or len(str(exc)) > _MAX_ERROR_MESSAGE_CHARS:
                raise ValueError("prompt request failed safely") from None
            raise

    async def read_resource(
        self,
        uri: AnyUrl | str,
    ) -> Iterable[ReadResourceContents]:
        rendered_uri = str(uri)
        try:
            if len(rendered_uri) > 512:
                raise ValueError("resource URI is too long")
            ensure_no_raw_vin(rendered_uri)
            decoded_uri = rendered_uri
            for _ in range(4):
                next_uri = unquote(decoded_uri, errors="strict")
                if next_uri == decoded_uri:
                    break
                decoded_uri = next_uri
                ensure_no_raw_vin(decoded_uri)
        except ValueError:
            raise ResourceError("sensitive resource input withheld") from None
        try:
            return await super().read_resource(uri)
        except ResourceError:
            # SDK messages include the caller-provided URI. Keep the public
            # failure constant so encoded or otherwise transformed identifiers
            # cannot be reflected by an unknown-resource path.
            raise ResourceError("resource request failed safely") from None


class _VinRedactingLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            if record.exc_info is not None:
                rendered += "".join(traceback.format_exception(*record.exc_info))
        except Exception:
            rendered = "unrenderable log record"
        if contains_raw_vin(rendered):
            record.msg = "log message withheld because it contained vehicle identity data"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def _configure_safe_logging() -> None:
    """Suppress SDK payload logging and redact VINs from existing handlers."""

    redacting_filter = _VinRedactingLogFilter()
    mcp_logger = logging.getLogger("mcp")
    mcp_logger.setLevel(logging.CRITICAL)
    mcp_logger.addFilter(redacting_filter)
    for logger_name in (
        "mcp.server.lowlevel.server",
        "mcp.server.transport_security",
    ):
        logging.getLogger(logger_name).addFilter(redacting_filter)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redacting_filter)


def _has_validation_cause(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ValidationError):
            return True
        current = current.__cause__ or current.__context__
    return False


async def _invoke(operation: Awaitable[T]) -> T:
    try:
        result = await operation
    except OBDMCPError as exc:
        if isinstance(exc, DriverError):
            message = "diagnostic operation failed safely"
        else:
            message = (
                "diagnostic operation failed safely"
                if contains_raw_vin(exc.message) or len(exc.message) > _MAX_ERROR_MESSAGE_CHARS
                else exc.message
            )
        raise ToolError(f"{exc.code}: {message}") from None
    except Exception:
        raise ToolError("internal_error: diagnostic operation failed safely") from None

    try:
        ensure_safe_public_value(result)
    except ValueError:
        raise ToolError("privacy_error: diagnostic result was withheld") from None
    return result


def _build_runtime(config: AppConfig) -> _Runtime:
    from .service import DiagnosticService
    from .storage import SQLiteIssueStore

    registry = DriverRegistry(
        allow_third_party=config.extensions.allow_third_party_drivers,
    )
    configured_drivers = {
        vehicle.id: _create_driver(registry, vehicle) for vehicle in config.vehicles
    }
    driver: DiagnosticDriver
    if len(configured_drivers) == 1:
        driver = next(iter(configured_drivers.values()))
    else:
        driver = _ConfiguredFleetDriver(configured_drivers)

    profiles = ProfileRegistry()
    profile_loader = ProfileLoader()
    profile_ids: dict[str, str] = {}
    loaded_paths: dict[str, str] = {}
    for vehicle in config.vehicles:
        if vehicle.profile is None:
            continue
        path_key = str(vehicle.profile)
        profile_id = loaded_paths.get(path_key)
        if profile_id is None:
            profile = profile_loader.load_path(vehicle.profile)
            profiles.add(profile)
            profile_id = profile.profile_id
            loaded_paths[path_key] = profile_id
        profile_ids[vehicle.id] = profile_id

    store = SQLiteIssueStore(config.storage.path)
    diagnostic_service = DiagnosticService(
        driver,
        store,
        profiles=profiles,
        vehicle_profiles=profile_ids,
    )
    return _Runtime(
        service=diagnostic_service,
        initialize=store.initialize,
        close=diagnostic_service.close,
    )


def _create_driver(registry: DriverRegistry, vehicle: VehicleConfig) -> DiagnosticDriver:
    options = dict(vehicle.options)
    if vehicle.driver == "simulator":
        seed = options.pop("seed", 0)
        simulated_vehicle: dict[str, Any] = {
            "vehicle_id": vehicle.id,
            "display_name": vehicle.name,
            **options,
        }
        return registry.create(
            "simulator",
            seed=seed,
            vehicles=[simulated_vehicle],
        )

    options["vehicle_id"] = vehicle.id
    options["display_name"] = vehicle.name
    return registry.create(vehicle.driver, **options)


class _ConfiguredFleetDriver(DiagnosticDriver):
    """Route configured vehicle IDs to their independently constructed drivers."""

    def __init__(self, drivers: Mapping[str, DiagnosticDriver]) -> None:
        self._drivers = dict(drivers)

    async def list_vehicles(self) -> tuple[Vehicle, ...]:
        groups = await asyncio.gather(
            *(driver.list_vehicles() for driver in self._drivers.values())
        )
        vehicles: list[Vehicle] = []
        for configured_id, group in zip(self._drivers, groups, strict=True):
            if (
                not isinstance(group, tuple)
                or len(group) != 1
                or not isinstance(group[0], Vehicle)
                or group[0].vehicle_id != configured_id
            ):
                raise DriverError(
                    "configured driver must advertise exactly its assigned vehicle id"
                )
            vehicles.append(group[0])
        return tuple(vehicles)

    async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
        return await self._driver(vehicle_id).get_vehicle_status(vehicle_id)

    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str],
    ) -> tuple[SignalReading, ...]:
        return await self._driver(vehicle_id).read_standard_pids(vehicle_id, pids)

    async def read_dtcs(
        self,
        vehicle_id: str,
        ecu_id: str | None = None,
    ) -> tuple[DiagnosticTroubleCode, ...]:
        return await self._driver(vehicle_id).read_dtcs(vehicle_id, ecu_id)

    async def read_ecu_snapshot(
        self,
        vehicle_id: str,
        ecu_id: str,
        reads: Sequence[ProfileReadDefinition] = (),
        *,
        profile_id: str | None = None,
    ) -> EcuSnapshot:
        return await self._driver(vehicle_id).read_ecu_snapshot(
            vehicle_id,
            ecu_id,
            reads,
            profile_id=profile_id,
        )

    async def close(self) -> None:
        results = await asyncio.gather(
            *(driver.close() for driver in self._drivers.values()),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            first = errors[0]
            if isinstance(first, OBDMCPError):
                raise first
            raise OBDMCPError("failed to close one or more configured drivers") from first

    def _driver(self, vehicle_id: str) -> DiagnosticDriver:
        try:
            return self._drivers[vehicle_id]
        except KeyError as exc:
            raise VehicleNotFoundError(f"vehicle not found: {vehicle_id}") from exc
