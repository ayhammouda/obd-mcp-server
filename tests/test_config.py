from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from obd_mcp.config import AppConfig, ConfigError, ServerConfig, VehicleConfig, load_config

_SYNTHETIC_VIN = "1" + ("A" * 16)


def test_no_config_uses_local_demo_simulator() -> None:
    config = load_config()

    assert config.server.host == "127.0.0.1"
    assert config.vehicles == [VehicleConfig(id="demo", name="Demo Vehicle", driver="simulator")]
    assert config.privacy.vin_suffix_length == 4


def test_load_config_resolves_storage_and_profile_paths(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile = profiles / "compact.toml"
    profile.write_text("", encoding="utf-8")
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        """
[server]
host = "localhost"
port = 8765

[storage]
path = "state/issues.sqlite3"

[privacy]
vin_suffix_length = 4

[[vehicles]]
id = "garage-car"
name = "Garage Car"
driver = "simulator"
profile = "profiles/compact.toml"

[vehicles.options]
seed = 7
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.server.host == "127.0.0.1"
    assert config.storage.path == (tmp_path / "state/issues.sqlite3").resolve()
    assert config.vehicles[0].profile == profile.resolve()
    assert config.vehicles[0].options == {"seed": 7}


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.168.1.10", "example.com"],  # noqa: S104
)
def test_server_rejects_non_loopback_hosts(host: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        ServerConfig(host=host)


def test_explicit_missing_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(tmp_path / "missing.toml")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("password", "not-allowed"),
        ("apiKey", "not-allowed"),
        ("vin", _SYNTHETIC_VIN),
        ("label", _SYNTHETIC_VIN),
    ],
)
def test_vehicle_options_reject_secrets_and_full_vins(key: str, value: str) -> None:
    with pytest.raises(ValidationError, match=r"sensitive|VIN"):
        VehicleConfig(options={key: value})


@pytest.mark.parametrize(
    ("driver", "options", "message"),
    [
        ("simulator", {"unknown": True}, "unknown"),
        ("elm327", {}, "port"),
        ("elm327", {"port": " /dev/fake "}, "port"),
        ("elm327", {"port": "/dev/fake", "unknown": True}, "unknown"),
        ("elm327", {"port": "/dev/fake", "baudrate": True}, "baudrate"),
        ("elm327", {"port": "/dev/fake", "baudrate": 38_400.0}, "baudrate"),
        ("elm327", {"port": "/dev/fake", "baudrate": 1_199}, "baudrate"),
        ("elm327", {"port": "/dev/fake", "baudrate": 2_000_001}, "baudrate"),
        ("elm327", {"port": "/dev/fake", "timeout_seconds": True}, "timeout_seconds"),
        ("elm327", {"port": "/dev/fake", "timeout_seconds": "5"}, "timeout_seconds"),
        ("elm327", {"port": "/dev/fake", "timeout_seconds": float("nan")}, "finite"),
        ("elm327", {"port": "/dev/fake", "timeout_seconds": float("inf")}, "finite"),
        ("elm327", {"port": "/dev/fake", "timeout_seconds": 0.09}, "timeout_seconds"),
        ("elm327", {"port": "/dev/fake", "timeout_seconds": 5.01}, "timeout_seconds"),
        ("elm327", {"port": "/dev/fake", "protocol": "auto"}, "protocol"),
        ("elm327", {"port": "/dev/fake", "protocol": "sae_j1939_can"}, "protocol"),
    ],
)
def test_builtin_driver_options_use_strict_bounded_schemas(
    driver: str,
    options: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        VehicleConfig(driver=driver, options=options)  # type: ignore[arg-type]


def test_extensions_require_explicit_third_party_driver_opt_in() -> None:
    assert AppConfig().extensions.allow_third_party_drivers is False

    opted_in = AppConfig.model_validate({"extensions": {"allow_third_party_drivers": True}})

    assert opted_in.extensions.allow_third_party_drivers is True


@pytest.mark.parametrize("value", [1, "true"])
def test_extensions_reject_coerced_third_party_driver_opt_in(value: object) -> None:
    with pytest.raises(ValidationError, match="allow_third_party_drivers"):
        AppConfig.model_validate({"extensions": {"allow_third_party_drivers": value}})


def test_server_log_level_cannot_enable_sdk_payload_debugging() -> None:
    with pytest.raises(ValidationError, match="log_level"):
        ServerConfig(log_level="DEBUG")  # type: ignore[arg-type]


def test_duplicate_vehicle_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unique"):
        AppConfig(vehicles=[VehicleConfig(), VehicleConfig()])


def test_configuration_bounds_match_public_and_core_vehicle_limits() -> None:
    with pytest.raises(ValidationError, match="at most 128 characters"):
        VehicleConfig(id="v" * 129)
    with pytest.raises(ValidationError, match="at most 128 items"):
        AppConfig(vehicles=[VehicleConfig(id=f"vehicle-{index}") for index in range(129)])


@pytest.mark.parametrize(
    "privacy_toml",
    [
        "allow_full_vin = true",
        "vin_suffix_length = 17",
    ],
)
def test_privacy_configuration_cannot_enable_full_vins(
    privacy_toml: str,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "obd.toml"
    config_path.write_text(f"[privacy]\n{privacy_toml}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid configuration"):
        load_config(config_path)


def test_missing_profile_is_reported_during_load(tmp_path: Path) -> None:
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        """
[[vehicles]]
id = "car"
profile = "missing-profile.toml"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"profile.*does not exist"):
        load_config(config_path)


def test_config_validation_errors_do_not_echo_sensitive_values(tmp_path: Path) -> None:
    config_path = tmp_path / "obd.toml"
    secret = "should-never-appear-in-an-error"
    config_path.write_text(
        f"""
[[vehicles]]
id = "car"

[vehicles.options]
password = "{secret}"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)

    assert "sensitive option" in str(exc_info.value)
    assert secret not in str(exc_info.value)


def test_raw_config_rejects_embedded_vin_tokens_without_echoing_them(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        f"""
[server]
http_path = "/trace/{_SYNTHETIC_VIN}/mcp"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)

    assert "VIN" in str(exc_info.value)
    assert _SYNTHETIC_VIN not in str(exc_info.value)


def test_raw_config_rejects_vin_tokens_in_comments_without_echoing_them(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        f"# private vehicle identifier: {_SYNTHETIC_VIN}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)

    assert "VIN" in str(exc_info.value)
    assert _SYNTHETIC_VIN not in str(exc_info.value)
