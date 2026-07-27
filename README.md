# OBD MCP Server

A local-first, read-only, OEM-neutral Model Context Protocol server for vehicle
diagnostics.

The project turns a narrow set of diagnostic observations into structured MCP
tools while keeping raw protocol access and state-changing vehicle operations
outside the AI boundary. It starts with a deterministic simulator, supports an
optional ELM327 adapter, and provides extension points for additional drivers
and externally mounted ECU profiles.

> [!CAUTION]
> This is pre-release diagnostic software, not a safety system or a substitute
> for a qualified technician. It must never be used to decide that a vehicle is
> safe to drive. Do not connect development builds to a moving vehicle.

## Current scope

The `0.1.x` foundation provides:

- seven typed MCP tools for vehicle discovery, standard signal reads, DTC
  reads, source-labeled ECU snapshots, and local issue timelines;
- a hard read-only policy with no raw request or fault-clearing surface;
- a built-in simulator so the complete MCP flow works without hardware;
- an optional `py-obdii` ELM327 integration; the extra installs its dependency,
  and configuration validation does not import it or contact an adapter;
- an entry-point plugin API for future SocketCAN, ISO-TP, K-line, replay, and
  vendor-neutral adapter packages, disabled unless the operator explicitly
  opts in to trusted in-process plugin code;
- declarative, externally mounted profiles with provenance and redistribution
  metadata;
- per-vehicle/per-ECU serialization, short-lived read caching, local SQLite
  issue storage, and pseudonymous VIN fingerprints;
- stdio plus loopback-only Streamable HTTP transports.

It intentionally does **not** include OEM databases, copied standards content,
Renault/CLIP/DDT/PyRen data, ECU coding, clearing, flashing, actuator control,
security access, arbitrary CAN/UDS/KWP requests, remote HTTP access, or raw
capture.

## Quick start from this checkout

Prerequisites: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run obd-mcp
```

Run those commands at the repository root. No public repository or PyPI
installation is assumed by this pre-release quick start. With no arguments,
`obd-mcp` starts stdio transport and uses the safe demo vehicle. Stdio stdout
is reserved for MCP messages.

Inspect the CLI and validate a configuration without connecting to a vehicle:

```bash
uv run obd-mcp --help
uv run obd-mcp check-config --config examples/obd-mcp.toml
uv run obd-mcp drivers
```

For built-in drivers, `check-config` validates the TOML schema, referenced
profiles, driver options, and optional dependency availability without
constructing a driver or connecting to a vehicle. It cannot establish that a
third-party plugin is safe or that live hardware will work.

Run loopback-only Streamable HTTP:

```bash
uv run obd-mcp http --config examples/obd-mcp.toml \
  --host 127.0.0.1 --port 8765
```

The MCP endpoint is `http://127.0.0.1:8765/mcp`. Non-loopback binds are
rejected because the server does not yet implement remote authentication.

## MCP tools

| Tool | Behavior | Vehicle mutation |
|---|---|---|
| `obd_list_vehicles` | Lists configured vehicles and capabilities | None |
| `obd_get_vehicle_status` | Reports connection state and a pseudonymous VIN fingerprint/redacted suffix | None |
| `obd_read_standard_pids` | Reads only fixed, allowlisted Mode 01 PIDs | None |
| `obd_read_dtcs` | Reads diagnostic trouble codes without clearing them | None |
| `obd_read_ecu_snapshot` | Uses the profile bound to the configured vehicle, or returns a normalized standard snapshot when no profile is bound | None |
| `obd_open_issue` | Writes a note to the local issue database, never the vehicle | None |
| `obd_get_issue_timeline` | Reads locally stored issue observations | None |

There is no generic `send`, `query`, `command`, `clear`, or raw-frame tool.

## Codex configuration

Use an absolute checkout path:

```toml
[mcp_servers.obd]
command = "uv"
args = [
  "--directory",
  "/absolute/path/to/obd-mcp-server",
  "run",
  "obd-mcp",
  "stdio",
  "--config",
  "/absolute/path/to/obd-mcp-server/examples/obd-mcp.toml",
]
```

The explicit `stdio` subcommand is recommended in client configuration even
though it is also the CLI default.

## Live ELM327 adapter

The simulator is the default. To install the optional, MIT-licensed adapter
integration:

```bash
uv sync --extra elm327
```

Then configure a vehicle with the `elm327` driver and an explicit serial
transport. The adapter only maps fixed symbolic reads to library commands; it
does not construct arbitrary commands and never imports or calls the library's
clear-DTC operation. The protocol is fixed to a supported OBD transport
(`iso_15765_4_can` by default), verified again after connection, and never
auto-detected. J1939 is not accepted by this Mode 01/03 driver. Adapter
timeouts are restricted to 0.1–5 seconds.

`py-obdii` is currently a beta dependency and is pinned deliberately. Treat
live hardware support as opt-in until it has been validated against your
adapter, vehicle, market, and ignition-state procedure.

Start from
[`examples/elm327.example.toml`](https://github.com/ayhammouda/obd-mcp-server/blob/main/examples/elm327.example.toml),
replace the serial port explicitly, and run `check-config` before connecting.
The server never scans serial or network endpoints automatically.
If a request times out while blocking adapter work is cancelling, the request
returns a bounded error, cleanup drains in the background, and further work
for that vehicle is fenced until draining finishes.

The source test matrix and installed-wheel smoke run on Linux and macOS.
Windows is intentionally unsupported in 0.1 because secure issue-store
permissions currently rely on POSIX file modes; a future Windows release needs
an ACL-aware storage implementation and dedicated security tests. The current
matrix is not evidence that the optional ELM327 path works with a particular
adapter, serial stack, or vehicle; live hardware remains unvalidated and
opt-in.

## Extension model

Third-party Python packages may register a driver under the
`obd_mcp.drivers` entry-point group. They are disabled by default. Enabling
them requires an explicit operator decision:

```toml
[extensions]
allow_third_party_drivers = true
```

Drivers implement the normalized interface and cannot add MCP tools through
the profile or driver contract. However, an enabled Python plugin executes
inside the server process with the user's permissions. It is trusted code and
can bypass in-process controls through arbitrary side effects. Review, pin,
and audit it before opting in.

Enhanced ECU definitions live in external profiles, not executable code:

```text
profile
├── schema version, profile id, name, and version
├── provenance: source, origin, license, redistribution, confidence
├── optional vehicle and ECU selector hints; schema v1 protocol is UDS
└── explicitly allowed UDS reads
    ├── ECU + service + identifier
    └── signal id + bounded decoder (for data identifiers)
```

See
[the profile format](https://github.com/ayhammouda/obd-mcp-server/blob/main/docs/profile-format.md)
and
[driver plugin guide](https://github.com/ayhammouda/obd-mcp-server/blob/main/docs/driver-plugins.md)
before building an extension.

## Safety, privacy, and legal posture

- [Safety policy](https://github.com/ayhammouda/obd-mcp-server/blob/main/docs/safety.md)
- [Privacy defaults](https://github.com/ayhammouda/obd-mcp-server/blob/main/docs/privacy.md)
- [Legal and data policy](https://github.com/ayhammouda/obd-mcp-server/blob/main/docs/legal-and-data-policy.md)
- [Threat model](https://github.com/ayhammouda/obd-mcp-server/blob/main/docs/threat-model.md)

The repository is designed to avoid distributing OEM technical information or
copyrighted standards. A right to access repair information does not by itself
grant a right to redistribute it. External profile authors and users are
responsible for confirming their source and license rights.

Full VIN-shaped values are rejected at configuration, normalized-domain, and
tool-output boundaries. When a driver reports a valid VIN, the public identity
is a deterministic, truncated SHA-256 fingerprint plus the last four
characters. This is pseudonymous vehicle-related data, not anonymous data.

“Legally compliant” is a process and jurisdiction-specific assessment, not a
warranty this repository can make. Obtain legal advice before distributing OEM
data or operating the project commercially.

## Development

```bash
uv sync --all-extras
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv build --build-constraints build-constraints.txt --require-hashes
uv run twine check dist/*
uv run pip-audit
uv run pip-licenses --format=markdown
```

Hardware tests are opt-in and never target a real vehicle in CI. See
[the test strategy](https://github.com/ayhammouda/obd-mcp-server/blob/main/docs/test-strategy.md).

## Publishing status

The repository is open-source-ready but not represented as published on PyPI
or the MCP Registry. The package name, trusted publisher, repository
protection, tag, artifact, and fresh-install smoke test must all be verified
before the first release. Private vulnerability reporting and a private conduct
contact must also be configured. The ELM327 integration has not been validated
against real hardware. See
[the release runbook](https://github.com/ayhammouda/obd-mcp-server/blob/main/docs/publishing.md).

## Contributing

Read
[CONTRIBUTING.md](https://github.com/ayhammouda/obd-mcp-server/blob/main/CONTRIBUTING.md).
Repository contributors must also follow the repository-local instructions.
Safety-policy changes require tests and focused review.

## License

Dual-licensed under the Apache License 2.0 or the MIT License, at your option.
See
[LICENSE](https://github.com/ayhammouda/obd-mcp-server/blob/main/LICENSE),
[LICENSE-MIT](https://github.com/ayhammouda/obd-mcp-server/blob/main/LICENSE-MIT),
and [NOTICE](https://github.com/ayhammouda/obd-mcp-server/blob/main/NOTICE).
