# ADR-0001: Python core and thin MCP facade

**Status:** Accepted  
**Date:** 2026-07-27  
**Decider:** Project maintainer

## Context

The project needs a current MCP implementation, mature automotive integration
options, deterministic tests without hardware, and strict separation between
AI tools and vehicle protocols. It must remain generic beyond one manufacturer.

## Decision

Use Python 3.11+ with the stable 1.x MCP Python SDK. Keep the diagnostic domain
and driver interfaces independent of MCP, and implement the MCP server as a
thin adapter over `DiagnosticService`.

The core is licensed under Apache-2.0. Hardware libraries are optional extras;
the initial ELM327 integration uses MIT-licensed `py-obdii`. GPL-only
`python-OBD` is deliberately not a dependency so downstream users are not
silently moved to a copyleft distribution obligation.

## Options considered

### Python

| Dimension | Assessment |
|---|---|
| MCP support | Official SDK with typed tools and current transports |
| Automotive ecosystem | Strong serial, CAN, ISO-TP, UDS, and test tooling |
| Complexity | Low for an extensible local service |
| Packaging | Mature, but optional native/hardware dependencies need care |

**Pros:** Best fit for the research report's automotive stack; fast simulator
and plugin development; strong validation with Pydantic.

**Cons:** Async/thread boundaries around blocking hardware libraries require
care; Linux remains the target for future SocketCAN work.

### TypeScript

| Dimension | Assessment |
|---|---|
| MCP support | Strong official SDK |
| Automotive ecosystem | Usable SocketCAN libraries, weaker overall depth |
| Complexity | Low for MCP, medium for vehicle protocols |
| Packaging | Familiar single-package CLI |

**Pros:** Closely mirrors the reference GSC MCP project.

**Cons:** Would optimize the facade rather than the diagnostic core and reduce
reuse of mature Python automotive libraries.

### Go

| Dimension | Assessment |
|---|---|
| MCP support | Official SDK |
| Automotive ecosystem | Good CAN foundations, more protocol work |
| Complexity | Medium |
| Packaging | Excellent static deployment |

**Pros:** Predictable concurrency and appliance packaging.

**Cons:** Higher initial implementation cost for profiles, decoders, and
hardware abstractions.

## Consequences

- MCP transport changes do not affect driver or policy code.
- Drivers can be tested directly without launching an MCP server.
- Blocking adapter libraries must run away from the event loop.
- Package extras and third-party licenses are part of the release checklist.
- A future language-specific sidecar can still implement the normalized driver
  contract without changing MCP tools.

## Follow-up

- Add a Linux SocketCAN/ISO-TP driver only after replay and virtual-CAN tests.
- Re-evaluate `py-obdii` when it reaches a stable release.
- Revisit MCP 2.x only through a separate migration ADR.

