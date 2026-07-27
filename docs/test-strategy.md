# Test strategy

The test suite proves the safety boundary before it proves hardware breadth.

## Layers

### Unit

- policy allow/deny matrix;
- profile provenance and redistribution validation;
- Pydantic schema bounds;
- VIN-shaped input/output rejection, pseudonymous fingerprinting, and error
  redaction;
- cache expiry and concurrency locks;
- issue/timeline persistence and permissions;
- driver registry and optional dependency failures;
- third-party driver discovery disabled by default and explicit opt-in paths.

### Service integration

- simulator through `DiagnosticService`;
- multiple vehicles and unknown IDs;
- cached versus fresh observations;
- enhanced reads with and without an approved profile;
- cancellation/close lifecycle.

### MCP end-to-end

- SDK client initialization;
- exact tool list and annotations;
- structured outputs;
- compiled stdio subprocess with protocol-clean stdout;
- Streamable HTTP on loopback;
- non-loopback configuration rejection;
- bounded errors with no VIN/profile leakage.

### Packaging

- wheel and sdist build;
- `twine check`;
- package-content allowlist;
- fresh isolated install;
- `obd-mcp --version`, `--help`, config check, and stdio `tools/list`;
- no profiles/private paths, captures, databases, or `AGENTS.md` in the wheel.

The current CI matrix runs source tests on Python 3.11–3.14 on Linux, plus
Python 3.13 on macOS. It also installs and smokes the exact wheel on Linux and
macOS. Windows is not supported in 0.1: secure issue-store permissions rely on
POSIX file modes, and Windows support requires an ACL-aware implementation and
security tests. This is packaging/simulator evidence, not real ELM327 adapter
or vehicle compatibility evidence.

### Replay and virtual bus

Future SocketCAN/ISO-TP work adds:

- sanitized, independently generated replay fixtures;
- `python-can` virtual-bus tests on every platform it supports;
- Linux `vcan` tests in a separate job;
- timeouts, multi-frame boundaries, malformed responses, and ECU correlation.

### Hardware in loop

Manual and opt-in only:

- stationary vehicle, one adapter, one vehicle at a time;
- documented ignition state and emergency disconnect;
- fixed read list;
- no clear/write/routine/security/session commands;
- sanitized results and no captures in public CI artifacts.

## Required gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv build --build-constraints build-constraints.txt --require-hashes
uv run twine check dist/*
uv run pip-audit
```

Coverage has an initial branch-aware floor of 85%. Coverage cannot substitute
for explicit negative safety tests.

## Release evidence

The release run records:

- source commit and clean tree;
- exact Python/uv/lockfile versions;
- all gate outputs;
- license and vulnerability reports;
- wheel/sdist hashes and contents;
- fresh-install and stdio MCP smoke;
- tag-to-commit match.
