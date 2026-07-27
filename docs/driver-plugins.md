# Driver plugin guide

Drivers adapt a transport or hardware library to the core's normalized,
read-only interface. They do not add MCP tools, parse model-generated raw
commands, or decide which vehicle operations are safe.

Third-party drivers are disabled by default. Enabling one is an explicit trust
decision because plugin code runs in the server process with the operator's
permissions:

```toml
[extensions]
allow_third_party_drivers = true

[[vehicles]]
id = "lab-vehicle"
name = "Stationary lab vehicle"
driver = "my-adapter"

[vehicles.options]
endpoint = "/explicit/adapter/path"
```

## Registration

An external package registers one lowercase entry point:

```toml
[project]
dependencies = ["obd-mcp-server>=0.1,<0.2"]

[project.entry-points."obd_mcp.drivers"]
my-adapter = "my_obd_adapter:create_driver"
```

The target is a factory that accepts JSON-compatible keyword options from
`[vehicles.options]` after the core rejects sensitive keys and VIN-shaped
values. The server injects the configured `vehicle_id` and `display_name`
after that validation, so those two values are reserved and cannot be
overridden in `[vehicles.options]`. The factory returns a subclass of
`obd_mcp.drivers.DiagnosticDriver`:

```python
from typing import Any

from obd_mcp.drivers import DiagnosticDriver


def create_driver(**options: Any) -> DiagnosticDriver:
    return MyReadOnlyDriver(**options)
```

Built-in names cannot be overridden, and duplicate entry-point names fail
closed. The ordinary `obd-mcp drivers` command lists only built-ins because it
does not opt in to third-party discovery. With explicit opt-in, configuration
validation reads entry-point metadata but does not import the plugin module;
plugin code is imported only when that driver is instantiated to start a
server.

Built-in simulator and ELM327 options have strict core-owned schemas.
Third-party options have only the generic checks above until the factory
validates them. Consequently, `check-config` can confirm entry-point presence
but cannot prove a plugin's option semantics or safety.

## Required interface

A driver implements:

- `list_vehicles`
- `get_vehicle_status`
- `read_standard_pids`
- `read_dtcs`
- `read_ecu_snapshot`
- idempotent `close`

Return the strict models from `obd_mcp.domain`. Re-run the core
`ReadOnlyPolicy` inside the driver before touching a transport, even though the
service already authorizes the request. Serialize access to adapters that
cannot correlate concurrent responses.

There is deliberately no raw command method. A driver must not expose one
through configuration, metadata, profile fields, or a side-channel. It must
not implement clearing, session changes, security access, writes, resets,
routines, actuator control, coding, or flashing.

## Profiles

`read_ecu_snapshot` receives already validated `ProfileReadDefinition` values
from the profile bound to the configured vehicle. Schema version 1 accepts
only UDS and the modeled read services `0x19` and `0x22`. A driver may support
them only by mapping each definition to a reviewed, read-only implementation.
Reject unsupported protocols or identifiers; never turn the fields into an
arbitrary byte payload.

If no profile is bound, the empty definition sequence requests a normalized
standard snapshot. Such a result is still checked against the core Mode 01 PID
allowlist and DTC model. The built-in ELM327 driver currently implements this
standard path but deliberately rejects profile-defined UDS reads.

Profiles remain data-only and cannot register drivers. See
[the profile format](profile-format.md).

## Trust and compatibility

Plugins execute in the server process with the user's permissions. Python
entry points are therefore trusted code, not a sandbox. Although the core
validates returned models, correlation, bounds, and VIN safety, malicious or
defective plugin code can bypass in-process policy through direct hardware,
filesystem, process, or network side effects. Operators should review, pin,
and audit plugin packages separately before setting
`allow_third_party_drivers = true`.

The interface is alpha during `0.1.x`. A plugin should pin a compatible minor
series and test against the lowest and newest supported core releases.

## Conformance checklist

- No hardware access during module import or `check-config`; built-in
  configuration checks are side-effect-free and never connect.
- Explicit endpoint configuration; no automatic serial/network scanning.
- Finite timeouts and bounded response sizes; the final combined MCP
  text/structured result is capped at 1 MiB.
- Cancellation-safe, idempotent cleanup.
- A timed-out operation may drain after the caller receives an error; the core
  fences that vehicle so a second call cannot overlap the draining I/O.
- Full VINs converted to `VinIdentity` before a domain model is returned; no
  VIN-shaped value may appear in errors, metadata, or free-form text.
- Treat `VinIdentity` fingerprints as pseudonymous rather than anonymous data.
- Definition provenance stays on the snapshot while each observation retains
  its own source and confidence.
- Negative tests for every nearby mutable/raw operation.
- Simulator, replay, or mocked-adapter tests in ordinary CI.
- Real-vehicle tests manual, stationary, sanitized, and opt-in.
- Dependency license, vulnerability, and release-artifact review.
