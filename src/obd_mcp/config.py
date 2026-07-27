"""Strict, local-only configuration for the OBD MCP server."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_data_path
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from .domain import contains_raw_vin

DEFAULT_DATABASE_PATH = (
    user_data_path(
        appname="obd-mcp-server",
        appauthor=False,
    )
    / "issues.sqlite3"
)

_SECRET_KEY_PARTS = {
    "apikey",
    "credential",
    "credentials",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "token",
}


class ConfigError(ValueError):
    """Raised when a configuration file cannot be loaded safely."""


def validate_loopback_host(value: str) -> str:
    """Return a normalized host when it is unambiguously loopback-only."""

    host = value.strip()
    if host.lower() == "localhost":
        # Resolve the conventional name ourselves so a modified hosts file
        # cannot make a configuration described as local bind elsewhere.
        return "127.0.0.1"

    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(
            "HTTP host must be localhost or a loopback IP address; "
            "remote HTTP requires authentication, which is not implemented"
        ) from exc

    if not address.is_loopback:
        raise ValueError(
            "HTTP host must be loopback-only; remote HTTP requires authentication, "
            "which is not implemented"
        )
    return str(address)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerConfig(_StrictModel):
    """Transport settings. HTTP is deliberately limited to the local machine."""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    http_path: str = "/mcp"
    log_level: Literal["INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("host")
    @classmethod
    def _host_must_be_loopback(cls, value: str) -> str:
        return validate_loopback_host(value)

    @field_validator("http_path")
    @classmethod
    def _http_path_must_be_absolute(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("http_path must start with exactly one '/'")
        return value


class StorageConfig(_StrictModel):
    """Local SQLite persistence settings."""

    path: Path = DEFAULT_DATABASE_PATH


class PrivacyConfig(_StrictModel):
    """Controls the only VIN representation the facade may return."""

    allow_full_vin: Literal[False] = False
    vin_suffix_length: Literal[4] = 4


class ExtensionsConfig(_StrictModel):
    """Explicit opt-in for executable third-party driver extensions."""

    allow_third_party_drivers: StrictBool = False


class SimulatorDriverOptions(_StrictModel):
    """Strict options accepted by the built-in deterministic simulator."""

    seed: StrictInt = Field(default=0, ge=-(2**63), le=2**63 - 1)
    ecus: tuple[dict[str, JsonValue], ...] = ()
    dtcs: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    pid_values: dict[str, StrictInt | StrictFloat] = Field(default_factory=dict)


class Elm327DriverOptions(_StrictModel):
    """Strict, bounded connection options for the built-in ELM327 driver."""

    port: str = Field(min_length=1, max_length=1_024)
    baudrate: StrictInt = Field(default=38_400, ge=1_200, le=2_000_000)
    timeout_seconds: float = Field(
        default=5.0,
        ge=0.1,
        le=5.0,
        allow_inf_nan=False,
    )
    protocol: Literal[
        "iso_14230_4_kwp",
        "iso_14230_4_kwp_fast",
        "iso_15765_4_can",
        "iso_15765_4_can_b",
        "iso_15765_4_can_c",
        "iso_15765_4_can_d",
        "iso_9141_2",
        "sae_j1850_pwm",
        "sae_j1850_vpw",
    ] = "iso_15765_4_can"

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _timeout_must_be_numeric_not_boolean(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("timeout_seconds must be a finite number")
        return value

    @field_validator("port")
    @classmethod
    def _port_must_be_explicit(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("port must be an explicitly selected endpoint")
        if value != normalized:
            raise ValueError("port must not contain surrounding whitespace")
        return normalized


BuiltinDriverOptions = SimulatorDriverOptions | Elm327DriverOptions


def validate_builtin_driver_options(
    driver: str,
    options: Mapping[str, JsonValue],
) -> BuiltinDriverOptions | None:
    """Validate built-in options without importing or connecting to a driver."""

    if driver == "simulator":
        return SimulatorDriverOptions.model_validate(options)
    if driver == "elm327":
        return Elm327DriverOptions.model_validate(options)
    return None


class VehicleConfig(_StrictModel):
    """A configured vehicle and the driver used to reach it."""

    id: str = Field(
        default="demo",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    name: str = Field(default="Demo Vehicle", min_length=1, max_length=120)
    driver: str = Field(
        default="simulator",
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    profile: Path | None = None
    options: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("options")
    @classmethod
    def _options_must_not_contain_sensitive_values(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        _reject_sensitive_config(value)
        return value

    @model_validator(mode="after")
    def _builtin_options_must_match_driver_schema(self) -> VehicleConfig:
        try:
            validate_builtin_driver_options(self.driver, self.options)
        except ValidationError as exc:
            raise ValueError(f"invalid {self.driver} options: {_validation_summary(exc)}") from exc
        return self


class AppConfig(_StrictModel):
    """Top-level TOML configuration."""

    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    extensions: ExtensionsConfig = Field(default_factory=ExtensionsConfig)
    vehicles: list[VehicleConfig] = Field(
        default_factory=lambda: [VehicleConfig()],
        min_length=1,
        max_length=128,
    )

    @model_validator(mode="before")
    @classmethod
    def _raw_config_must_not_contain_vins(cls, value: Any) -> Any:
        _reject_raw_vin_config(value)
        return value

    @model_validator(mode="after")
    def _vehicle_ids_must_be_unique(self) -> AppConfig:
        ids = [vehicle.id for vehicle in self.vehicles]
        if len(ids) != len(set(ids)):
            raise ValueError("vehicle ids must be unique")
        validate_distinct_physical_endpoints(self.vehicles)
        return self


def validate_distinct_physical_endpoints(
    vehicles: Sequence[VehicleConfig],
) -> None:
    """Reject multiple drivers aimed at one physical adapter endpoint."""

    elm_ports: set[Path] = set()
    for vehicle in vehicles:
        options = validate_builtin_driver_options(vehicle.driver, vehicle.options)
        if not isinstance(options, Elm327DriverOptions):
            continue
        physical_port = Path(options.port).expanduser().resolve(strict=False)
        if physical_port in elm_ports:
            raise ValueError("multiple ELM327 vehicles resolve to the same physical port")
        elm_ports.add(physical_port)


def revalidate_app_config(config: AppConfig) -> AppConfig:
    """Rebuild a config so shallow post-validation mutations cannot reach runtime."""

    try:
        raw = config.model_dump(mode="python", warnings="error")
        return AppConfig.model_validate(raw)
    except (TypeError, ValueError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            detail = _validation_summary(exc)
        else:
            detail = "configuration could not be safely reconstructed"
        raise ConfigError(f"invalid runtime configuration: {detail}") from None


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load a TOML file, or return the deterministic demo configuration."""

    if path is None:
        return AppConfig()

    config_path = Path(path).expanduser().absolute()
    try:
        with config_path.open("rb") as handle:
            document_bytes = handle.read()
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file does not exist: {config_path}") from exc
    except PermissionError as exc:
        raise ConfigError(f"configuration file is not readable: {config_path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read configuration file {config_path}: {exc}") from exc

    try:
        document = document_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"configuration file is not valid UTF-8: {config_path}") from exc
    if contains_raw_vin(document):
        raise ConfigError("configuration contains VIN-shaped data")

    try:
        raw = tomllib.loads(document)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    try:
        config = AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(
            f"invalid configuration in {config_path}: {_validation_summary(exc)}"
        ) from exc

    return _resolve_config_paths(config, config_path.parent)


def _resolve_config_paths(config: AppConfig, base_dir: Path) -> AppConfig:
    storage_path = _absolute_path_without_resolving(config.storage.path, base_dir)
    vehicles: list[VehicleConfig] = []

    for vehicle in config.vehicles:
        profile = None
        if vehicle.profile is not None:
            profile = _resolve_path(vehicle.profile, base_dir)
            if not profile.is_file():
                raise ConfigError(f"profile for vehicle {vehicle.id!r} does not exist: {profile}")
        vehicles.append(vehicle.model_copy(update={"profile": profile}))

    return config.model_copy(
        update={
            "storage": config.storage.model_copy(update={"path": storage_path}),
            "vehicles": vehicles,
        }
    )


def _resolve_path(path: Path, base_dir: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = base_dir / expanded
    return expanded.resolve()


def _absolute_path_without_resolving(path: Path, base_dir: Path) -> Path:
    """Make a path absolute while preserving symlink components for security checks."""

    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = base_dir / expanded
    return expanded.absolute()


def _reject_sensitive_config(value: JsonValue | Mapping[str, Any] | Sequence[Any]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if contains_raw_vin(key_text):
                raise ValueError("VIN-shaped data is not accepted in configuration")
            if _is_sensitive_key(key_text):
                raise ValueError("sensitive options are not accepted in configuration")
            _reject_sensitive_config(item)
        return

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_sensitive_config(item)
        return

    if isinstance(value, str) and contains_raw_vin(value):
        raise ValueError("VIN-shaped data is not accepted in configuration")


def _reject_raw_vin_config(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if contains_raw_vin(str(key)):
                raise ValueError("raw configuration must not contain VIN-shaped data")
            _reject_raw_vin_config(item)
        return

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_raw_vin_config(item)
        return

    if isinstance(value, str) and contains_raw_vin(value):
        raise ValueError("raw configuration must not contain VIN-shaped data")


def _is_sensitive_key(key: str) -> bool:
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower()
    ordered_parts = [part for part in re.split(r"[^a-z0-9]+", words) if part]
    parts = set(ordered_parts)
    collapsed = "".join(ordered_parts)
    return "vin" in parts or collapsed in _SECRET_KEY_PARTS or bool(parts & _SECRET_KEY_PARTS)


def _validation_summary(error: ValidationError) -> str:
    """Format validation failures without echoing secret configuration values."""

    failures: list[str] = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "configuration"
        failures.append(f"{location}: {item['msg']}")
    return "; ".join(failures)
