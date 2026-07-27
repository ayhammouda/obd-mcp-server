# Repository instructions

This repository implements a safety-critical boundary between AI clients and
vehicle diagnostic interfaces.

## Non-negotiable safety rules

- Vehicle access is read-only. Never add fault clearing, ECU resets, session
  changes, security access, routine control, coding, flashing, actuator tests,
  write-by-identifier, raw command passthrough, or gateway bypass features.
- Enforce safety in the diagnostic core before a request reaches a driver.
  Descriptions, prompts, UI warnings, and MCP annotations are defense in depth;
  they are not policy enforcement.
- New protocol operations are denied unless a reviewed policy explicitly
  allowlists the operation and tests prove mutable operations remain blocked.
- AI-facing tools operate on normalized capabilities. Never expose raw CAN
  frames, arbitrary service IDs, adapter AT commands, or profile-defined tool
  code.
- Do not claim that a vehicle is safe to drive. Return observations,
  uncertainty, and a recommendation to consult a qualified professional.

## Data and licensing rules

- Do not commit OEM manuals, diagnostic databases, screenshots, proprietary
  identifiers, copied standards text, vehicle captures, VINs, or user logs.
- External profiles must declare provenance, scope, license, confidence, and
  redistribution status. Bundled data must have affirmative redistribution
  permission.
- Community-derived data is untrusted configuration, never authoritative
  truth. Validate it before use and label it in every result.
- Keep full VINs out of tool results and logs. Use a local fingerprint unless a
  future, separately reviewed feature requires explicit disclosure.

## Network and runtime rules

- Stdio stdout is protocol-only. Send diagnostics to stderr.
- Streamable HTTP is loopback-only until authenticated remote access is
  designed, implemented, and reviewed.
- Raw capture is disabled by default. Do not weaken local file permissions.
- Preserve cancellation, bounded timeouts, per-vehicle concurrency, and
  response-size limits when adding drivers or tools.

## Required checks

Run these before considering a change complete:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv build
uv run twine check dist/*
uv run pip-audit
```

Add a regression test for every safety-policy change. Hardware tests are
opt-in and must never run in CI against a real vehicle.

