"""Command-line entry point and MCP transport selection."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from pydantic import ValidationError

from . import __version__
from .config import (
    AppConfig,
    ConfigError,
    Elm327DriverOptions,
    ServerConfig,
    SimulatorDriverOptions,
    load_config,
    validate_builtin_driver_options,
)
from .domain import contains_raw_vin, ensure_no_raw_vin
from .errors import OBDMCPError

_BUILTIN_DRIVERS = frozenset({"elm327", "simulator"})
_SENSITIVE_DIAGNOSTIC = "sensitive input withheld"


class _SafeArgumentParser(argparse.ArgumentParser):
    """Argument parser that never reflects a VIN-shaped input to stderr."""

    def error(self, message: str) -> NoReturn:
        super().error(_safe_diagnostic_message(message))


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser without importing hardware integrations."""

    parser = _SafeArgumentParser(
        prog="obd-mcp",
        description="Local-first, read-only vehicle diagnostics over MCP.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="TOML configuration file (the safe demo simulator is the default)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    config_after_command = _SafeArgumentParser(add_help=False)
    config_after_command.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="TOML configuration file",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "stdio",
        parents=[config_after_command],
        help="serve MCP over stdio (default)",
    )

    http = subparsers.add_parser(
        "http",
        parents=[config_after_command],
        help="serve MCP over loopback-only Streamable HTTP",
    )
    http.add_argument("--host", help="loopback host override")
    http.add_argument("--port", type=int, help="TCP port override")

    subparsers.add_parser(
        "check-config",
        parents=[config_after_command],
        help="validate configuration without connecting to a vehicle",
    )
    subparsers.add_parser("drivers", help="list available diagnostic drivers")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run a CLI command and return a process exit status."""

    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "stdio"

    try:
        if command == "drivers":
            return _list_drivers()

        config = load_config(args.config)
        if command == "check-config":
            return _check_config(config)
        if command == "http":
            config = _with_http_overrides(config, host=args.host, port=args.port)
            _validate_runtime_references(config)
            return run_http(config)
        _validate_runtime_references(config)
        return run_stdio(config)
    except ConfigError as exc:
        _diagnostic(f"configuration error: {exc}")
        return 2
    except OBDMCPError as exc:
        _diagnostic(f"{exc.code}: {exc.message}")
        return 2
    except ValueError as exc:
        _diagnostic(f"invalid argument: {exc}")
        return 2
    except KeyboardInterrupt:
        _diagnostic("interrupted")
        return 130


def run_stdio(config: AppConfig) -> int:
    """Run stdio without writing any diagnostics to protocol stdout."""

    from .server import create_server

    create_server(config).run(transport="stdio")
    return 0


def run_http(config: AppConfig) -> int:
    """Run loopback-only Streamable HTTP."""

    from .server import create_server

    _diagnostic(
        "serving MCP on "
        f"http://{_url_host(config.server.host)}:{config.server.port}"
        f"{config.server.http_path}"
    )
    create_server(config).run(transport="streamable-http")
    return 0


def _with_http_overrides(
    config: AppConfig,
    *,
    host: str | None,
    port: int | None,
) -> AppConfig:
    current = config.server
    server = ServerConfig(
        host=current.host if host is None else host,
        port=current.port if port is None else port,
        http_path=current.http_path,
        log_level=current.log_level,
    )
    return config.model_copy(update={"server": server})


def _check_config(config: AppConfig) -> int:
    _validate_runtime_references(config)
    summary: dict[str, Any] = {
        "status": "ok",
        "server": {
            "host": config.server.host,
            "port": config.server.port,
            "http_path": config.server.http_path,
        },
        "storage": {"path": _safe_summary_path(config.storage.path)},
        "privacy": {
            "allow_full_vin": config.privacy.allow_full_vin,
            "vin_suffix_length": config.privacy.vin_suffix_length,
        },
        "extensions": {
            "allow_third_party_drivers": config.extensions.allow_third_party_drivers,
        },
        "vehicles": [
            {
                "id": vehicle.id,
                "name": vehicle.name,
                "driver": vehicle.driver,
                "profile": (
                    _safe_summary_path(vehicle.profile) if vehicle.profile is not None else None
                ),
            }
            for vehicle in config.vehicles
        ],
    }
    ensure_no_raw_vin(summary)
    print(json.dumps(summary, sort_keys=True))
    return 0


def _safe_summary_path(path: Path) -> str:
    rendered = str(path)
    return "<sensitive path withheld>" if contains_raw_vin(rendered) else rendered


def _validate_runtime_references(config: AppConfig) -> None:
    from .drivers.registry import DriverRegistry
    from .drivers.simulator import SimulatorVehicle
    from .profiles import ProfileLoader, ProfileRegistry

    configured_drivers = {vehicle.driver for vehicle in config.vehicles}
    third_party_drivers = sorted(configured_drivers - _BUILTIN_DRIVERS)
    if third_party_drivers and not config.extensions.allow_third_party_drivers:
        raise ConfigError(
            "third-party diagnostic driver(s) require explicit "
            "extensions.allow_third_party_drivers=true: " + ", ".join(third_party_drivers)
        )

    available_drivers = set(
        DriverRegistry(allow_third_party=config.extensions.allow_third_party_drivers).names()
    )
    unknown_drivers = sorted(configured_drivers - available_drivers)
    if unknown_drivers:
        raise ConfigError("unknown diagnostic driver(s): " + ", ".join(unknown_drivers))

    elm_ports: dict[Path, str] = {}
    has_elm327 = False
    for vehicle in config.vehicles:
        options = validate_builtin_driver_options(vehicle.driver, vehicle.options)
        if isinstance(options, SimulatorDriverOptions):
            simulator_values = options.model_dump(exclude={"seed"}, exclude_unset=True)
            try:
                SimulatorVehicle.model_validate(
                    {
                        "vehicle_id": vehicle.id,
                        "display_name": vehicle.name,
                        **simulator_values,
                    }
                )
            except ValidationError as exc:
                raise ConfigError(
                    f"invalid simulator options for vehicle {vehicle.id!r}: "
                    f"{_validation_summary(exc)}"
                ) from exc
            except OBDMCPError as exc:
                raise ConfigError(
                    f"invalid simulator options for vehicle {vehicle.id!r}: {exc.message}"
                ) from exc
        elif isinstance(options, Elm327DriverOptions):
            has_elm327 = True
            physical_port = Path(options.port).expanduser().resolve(strict=False)
            if physical_port in elm_ports:
                raise ConfigError("multiple ELM327 vehicles resolve to the same physical port")
            elm_ports[physical_port] = vehicle.id

    if has_elm327 and importlib.util.find_spec("obdii") is None:
        raise ConfigError("ELM327 support requires the optional 'py-obdii' package")

    loader = ProfileLoader()
    registry = ProfileRegistry()
    loaded_paths: set[Path] = set()
    for vehicle in config.vehicles:
        if vehicle.profile is None or vehicle.profile in loaded_paths:
            continue
        registry.add(loader.load_path(vehicle.profile))
        loaded_paths.add(vehicle.profile)


def _validation_summary(error: ValidationError) -> str:
    failures: list[str] = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "options"
        failures.append(f"{location}: {item['msg']}")
    return "; ".join(failures)


def _list_drivers() -> int:
    from .drivers.registry import DriverRegistry

    for name in DriverRegistry().names():
        print(name)
    return 0


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _diagnostic(message: str) -> None:
    print(f"obd-mcp: {_safe_diagnostic_message(message)}", file=sys.stderr)


def _safe_diagnostic_message(message: str) -> str:
    return _SENSITIVE_DIAGNOSTIC if contains_raw_vin(message) else message
