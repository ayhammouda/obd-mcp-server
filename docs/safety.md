# Safety policy

## Safety invariant

No MCP request, profile, plugin option, or model-generated value may cause a
vehicle state change.

The project is an observation gateway. It is not a bidirectional diagnostic
console.

## Defense layers

1. MCP exposes only named, typed capabilities.
2. The service validates every request with a central allowlist.
3. Drivers implement normalized reads, not raw command submission.
4. Profiles can only narrow approved reads.
5. Per-vehicle and per-ECU locks prevent overlapping diagnostic exchanges.
6. Tests assert known mutable operations remain unreachable and denied.
7. Third-party Python drivers are disabled unless the operator explicitly
   opts in to trusted in-process code.

MCP annotations and tool descriptions communicate intent but are not trusted
as enforcement. The plugin interface is not a sandbox: an enabled malicious
plugin can bypass application controls through arbitrary Python side effects.
Only enable reviewed and pinned plugins in an environment where that trust is
acceptable.

## Permitted behavior

- list configured vehicles and driver capabilities;
- report connection status without returning a full VIN;
- read fixed, allowlisted standard Mode 01 PIDs;
- read stored/pending/permanent DTC categories only when a driver has a
  dedicated read implementation;
- read selected enhanced identifiers already declared in a validated profile
  and approved by the immutable core policy;
- return a normalized standard ECU snapshot when no enhanced profile is bound;
- store and read local issue notes.

## Permanently out of scope

- clear/reset DTCs or freeze-frame data;
- diagnostic session changes;
- ECU reset, communication control, or DTC-setting control;
- security access, seed/key, authentication, or gateway bypass;
- write-by-identifier/memory, download/upload, transfer, or flashing;
- routine control, actuator tests, input/output control, coding, adaptation,
  immobilizer/key operations, or calibration;
- arbitrary CAN/K-line/DoIP/UDS/KWP/OBD payloads;
- adapter commands that can alter vehicle/protocol state outside a narrowly
  reviewed connection setup;
- use while driving or unattended actuation.

## Profile policy

A service identifier is not enough to prove an operation safe on every ECU.
An enhanced read needs all of:

- an immutable core operation classification;
- a validated vehicle/network/ECU profile;
- an explicit identifier allowlist;
- a read-only driver capability;
- source, version, and confidence labels;
- replay or hardware-in-loop evidence for the intended vehicle scope.

The initial enhanced policy recognizes only narrowly modeled read-data/read-DTC
operations. Unsupported services fail closed.

## Diagnostic output

Tool results are observations, not conclusions about roadworthiness. Clients
should:

- distinguish standard, licensed, community, and synthetic sources;
- state uncertainty and missing context;
- avoid definitive root-cause claims from a DTC alone;
- recommend a qualified technician for safety-critical systems or ambiguous
  conditions;
- advise stopping and following the vehicle manual/emergency guidance when
  immediate danger indicators are present, without inventing thresholds.

## Physical use

- Prefer a reputable wired adapter and a stationary vehicle.
- Follow the adapter and vehicle manufacturer's procedures.
- Do not backprobe ECUs, bypass gateways, or add relay/GPIO actuation.
- A custom bridge needs an independent automotive electrical design review,
  protection, isolation, and failure analysis.
- Never assume “read-only software” makes unknown or counterfeit hardware
  electrically safe.

## Reporting a safety issue

Do not publish a working exploit or unsafe vehicle command in a public issue.
Follow `SECURITY.md` and include the affected driver, operation, adapter, and
whether a real vehicle was involved.
