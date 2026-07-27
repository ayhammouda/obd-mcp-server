from __future__ import annotations

import json
from importlib.machinery import ModuleSpec
from pathlib import Path

import pytest

from obd_mcp import __version__, cli
from obd_mcp.config import AppConfig, ServerConfig
from obd_mcp.drivers import registry as driver_registry


def test_help_lists_transport_and_utility_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "stdio" in output
    assert "http" in output
    assert "check-config" in output
    assert "drivers" in output


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"obd-mcp {__version__}"


def test_argparse_does_not_echo_vin_shaped_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_vin = "A" * 17

    with pytest.raises(SystemExit) as exc_info:
        cli.main([raw_vin])

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert raw_vin not in captured.err
    assert "sensitive input withheld" in captured.err


def test_missing_config_path_does_not_echo_vin_shaped_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_vin = "A" * 17
    config_path = tmp_path / f"{raw_vin}.toml"

    assert cli.main(["check-config", "--config", str(config_path)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert raw_vin not in captured.err
    assert "sensitive input withheld" in captured.err


def test_default_command_runs_stdio_without_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: list[AppConfig] = []

    def fake_run(config: AppConfig) -> int:
        received.append(config)
        return 0

    monkeypatch.setattr(cli, "run_stdio", fake_run)

    assert cli.main([]) == 0
    assert received[0].vehicles[0].driver == "simulator"
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "arguments",
    [
        ["--config", "{config}", "stdio"],
        ["stdio", "--config", "{config}"],
    ],
)
def test_config_is_accepted_before_or_after_subcommand(
    arguments: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "obd.toml"
    config_path.write_text("[[vehicles]]\nid = 'configured'\n", encoding="utf-8")
    received: list[AppConfig] = []
    monkeypatch.setattr(cli, "run_stdio", lambda config: received.append(config) or 0)

    rendered = [str(config_path) if value == "{config}" else value for value in arguments]
    assert cli.main(rendered) == 0
    assert received[0].vehicles[0].id == "configured"


def test_check_config_prints_safe_json_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        """
[storage]
path = "issues.sqlite3"

[[vehicles]]
id = "demo-two"
driver = "simulator"
""".strip(),
        encoding="utf-8",
    )

    assert cli.main(["check-config", "--config", str(config_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["extensions"] == {"allow_third_party_drivers": False}
    assert payload["vehicles"] == [
        {
            "driver": "simulator",
            "id": "demo-two",
            "name": "Demo Vehicle",
            "profile": None,
        }
    ]
    assert "options" not in payload["vehicles"][0]


def test_check_config_redacts_vin_shaped_config_parent_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_vin = "A" * 17
    config_dir = tmp_path / raw_vin
    config_dir.mkdir()
    config_path = config_dir / "obd.toml"
    config_path.write_text("[storage]\npath = 'issues.sqlite3'\n", encoding="utf-8")

    assert cli.main(["check-config", "--config", str(config_path)]) == 0

    captured = capsys.readouterr()
    assert raw_vin not in captured.out
    assert raw_vin not in captured.err
    assert json.loads(captured.out)["storage"]["path"] == "<sensitive path withheld>"


def test_check_config_redacts_vin_shaped_profile_symlink_targets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_vin = "A" * 17
    target_dir = tmp_path / raw_vin
    target_dir.mkdir()
    profile_target = target_dir / "profile.toml"
    profile_target.write_text(
        (Path("examples/profiles/synthetic-powertrain.toml")).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    profile_link = tmp_path / "profile-link.toml"
    profile_link.symlink_to(profile_target)
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        """
[[vehicles]]
id = "demo"
profile = "profile-link.toml"
""".strip(),
        encoding="utf-8",
    )

    assert cli.main(["check-config", "--config", str(config_path)]) == 0

    captured = capsys.readouterr()
    assert raw_vin not in captured.out
    assert raw_vin not in captured.err
    assert json.loads(captured.out)["vehicles"][0]["profile"] == "<sensitive path withheld>"


def test_http_rejects_remote_host_before_server_start(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def fake_run(_config: AppConfig) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(cli, "run_http", fake_run)

    assert cli.main(["http", "--host", "0.0.0.0"]) == 2  # noqa: S104
    assert called is False
    assert "loopback" in capsys.readouterr().err


def test_check_config_validates_profile_contents(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path = tmp_path / "invalid-profile.toml"
    profile_path.write_text("", encoding="utf-8")
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        """
[[vehicles]]
id = "demo"
profile = "invalid-profile.toml"
""".strip(),
        encoding="utf-8",
    )

    assert cli.main(["check-config", "--config", str(config_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "profile_validation_error" in captured.err


def test_check_config_rejects_unknown_driver(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        """
[extensions]
allow_third_party_drivers = true

[[vehicles]]
id = "demo"
driver = "missing-driver"
""".strip(),
        encoding="utf-8",
    )

    assert cli.main(["check-config", "--config", str(config_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown diagnostic driver" in captured.err


def test_stdio_rejects_missing_elm327_port_before_server_start(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_run(_config: AppConfig) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(cli, "run_stdio", fake_run)
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        """
[[vehicles]]
id = "hardware"
driver = "elm327"
""".strip(),
        encoding="utf-8",
    )

    assert cli.main(["stdio", "--config", str(config_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert called is False
    assert "port" in captured.err
    assert "Traceback" not in captured.err


def test_check_config_rejects_missing_elm327_dependency_without_importing_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        """
[[vehicles]]
id = "hardware"
driver = "elm327"

[vehicles.options]
port = "/dev/fake"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda _name: None)

    assert cli.main(["check-config", "--config", str(config_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "py-obdii" in captured.err


def test_check_config_rejects_duplicate_elm327_physical_ports(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = tmp_path / "adapter"
    alias = tmp_path / "adapter-alias"
    port.touch()
    alias.symlink_to(port)
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        f"""
[[vehicles]]
id = "hardware-one"
driver = "elm327"

[vehicles.options]
port = "{port}"

[[vehicles]]
id = "hardware-two"
driver = "elm327"

[vehicles.options]
port = "{alias}"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli.importlib.util,
        "find_spec",
        lambda name: ModuleSpec(name, loader=None),
    )

    assert cli.main(["check-config", "--config", str(config_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "same physical port" in captured.err


def test_third_party_drivers_require_explicit_opt_in(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_opt_ins: list[bool] = []

    class FakeRegistry:
        def __init__(self, *, allow_third_party: bool = False) -> None:
            registry_opt_ins.append(allow_third_party)

        def names(self) -> tuple[str, ...]:
            return ("elm327", "plugin-driver", "simulator")

    monkeypatch.setattr(driver_registry, "DriverRegistry", FakeRegistry)
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        """
[[vehicles]]
id = "plugin-vehicle"
driver = "plugin-driver"
""".strip(),
        encoding="utf-8",
    )

    assert cli.main(["check-config", "--config", str(config_path)]) == 2
    assert "allow_third_party_drivers" in capsys.readouterr().err

    config_path.write_text(
        """
[extensions]
allow_third_party_drivers = true

[[vehicles]]
id = "plugin-vehicle"
driver = "plugin-driver"
""".strip(),
        encoding="utf-8",
    )

    assert cli.main(["check-config", "--config", str(config_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert registry_opt_ins == [True]


def test_check_config_validates_simulator_options_without_constructing_driver(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from obd_mcp.drivers import simulator

    def fail_if_constructed(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("check-config must not construct a simulator driver")

    monkeypatch.setattr(simulator.SimulatorDriver, "__init__", fail_if_constructed)
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        """
[[vehicles]]
id = "demo"
driver = "simulator"

[vehicles.options.pid_values]
"9999" = 1
""".strip(),
        encoding="utf-8",
    )

    assert cli.main(["check-config", "--config", str(config_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "simulator options" in captured.err


@pytest.mark.parametrize(
    "options_toml",
    [
        """
[vehicles.options.dtcs]
engine = ["NOT-A-DTC"]
""",
        """
[vehicles.options.dtcs]
ghost = ["P0300"]
""",
        """
[vehicles.options.pid_values]
"010C" = 1e100
""",
        """
[vehicles.options.pid_values]
"010C" = true
""",
        """
[[vehicles.options.ecus]]
ecu_id = "engine"
name = "Engine one"
protocol = "simulated"

[[vehicles.options.ecus]]
ecu_id = "engine"
name = "Engine two"
protocol = "simulated"
""",
        """
[vehicles.options]
dtcs = {}

[[vehicles.options.ecus]]
ecu_id = "body"
name = "Body"
protocol = "simulated"
""",
        """
[vehicles.options]
seed = 9223372036854775808
""",
    ],
)
def test_check_config_rejects_simulator_runtime_failures(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    options_toml: str,
) -> None:
    config_path = tmp_path / "obd.toml"
    config_path.write_text(
        (
            """
[[vehicles]]
id = "demo"
driver = "simulator"
""".strip()
            + "\n"
            + options_toml.strip()
        ),
        encoding="utf-8",
    )

    assert cli.main(["check-config", "--config", str(config_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "simulator options" in captured.err


@pytest.mark.parametrize(
    "example",
    ["examples/obd-mcp.toml", "examples/elm327.example.toml"],
)
def test_check_config_accepts_bundled_examples_without_connecting(
    example: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli.importlib.util,
        "find_spec",
        lambda name: ModuleSpec(name, loader=None),
    )

    assert cli.main(["check-config", "--config", example]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_drivers_lists_builtins(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["drivers"]) == 0
    assert capsys.readouterr().out.splitlines() == ["elm327", "simulator"]


def test_http_command_applies_validated_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[AppConfig] = []
    monkeypatch.setattr(cli, "run_http", lambda config: received.append(config) or 0)

    assert cli.main(["http", "--host", "::1", "--port", "8765"]) == 0
    assert received[0].server.host == "::1"
    assert received[0].server.port == 8765


def test_transport_runners_select_sdk_transports_and_keep_http_diagnostics_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from obd_mcp import server as server_module

    transports: list[str] = []

    class FakeServer:
        def run(self, transport: str) -> None:
            transports.append(transport)

    monkeypatch.setattr(server_module, "create_server", lambda _config: FakeServer())
    config = AppConfig(server=ServerConfig(host="::1", port=8765))

    assert cli.run_stdio(config) == 0
    assert capsys.readouterr().out == ""
    assert cli.run_http(config) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "http://[::1]:8765/mcp" in captured.err
    assert transports == ["stdio", "streamable-http"]


def test_keyboard_interrupt_is_reported_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupt(_config: AppConfig) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_stdio", interrupt)

    assert cli.main([]) == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "interrupted" in captured.err
