# Roadmap

## 0.1 — safe MCP foundation

- OEM-neutral domain and policy.
- Simulator and optional ELM327 fixed-read adapter.
- Seven typed MCP tools.
- Local issue timeline and explicitly pseudonymous VIN fingerprinting.
- Stdio and loopback-only HTTP.
- UDS-only external profile schema and driver entry points disabled by default.
- Unit/integration/package/security/legal documentation.

Exit criterion: simulator works end to end, every mutable operation is
unreachable/denied, package artifacts are clean, and CI is green.

## 0.2 — reproducible standard OBD

- Stable ELM327 dependency or maintained adapter abstraction.
- Supported-signal discovery and freeze-frame reads.
- Recorded emulator scenarios with clear licenses.
- Retry/timeout/adapter health behavior.
- Manual hardware QA runbook.

Exit criterion: two or more reputable wired adapters pass the same stationary
read-only conformance suite.

## 0.3 — Linux CAN transport

- SocketCAN and Linux ISO-TP driver package.
- `python-can` virtual-bus and Linux `vcan` jobs.
- Replay format, sanitization tool, and response-correlation tests.
- Separate least-privilege driver process design.

Exit criterion: no raw vehicle command is added to MCP, and all traffic is
generated from core-approved normalized operations.

## 0.4 — enhanced external profiles

- Versioned JSON Schema and migration tooling.
- Signed/checksummed profile bundles.
- Read-only UDS/KWP profile validation and replay conformance.
- Private licensed-profile mount workflow.
- Community-profile license/provenance review process.

Exit criterion: one synthetic and one lawfully distributable real-world
profile pass legal, replay, policy, and labeling gates. OEM data remains
external unless explicit redistribution permission exists.

## 0.5 — operational hardening

- Configurable retention/export/erasure.
- Encrypted-store integration guidance.
- Driver process isolation and reduced privileges.
- Authenticated remote deployment ADR, if a real need exists.
- Backup/restore and observability with privacy budgets.

Remote bind remains prohibited until authentication and a revised threat model
ship together.

## Out of scope

Vehicle writes, fault clearing, coding, flashing, routine/actuator control,
security access, gateway bypass, immobilizer/key work, and roadworthiness
certification are not roadmap items.
