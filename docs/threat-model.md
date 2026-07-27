# Threat model

## Assets

- vehicle availability and safe state;
- diagnostic adapter and host integrity;
- VIN-derived identity, timestamps, DTCs, and issue history;
- private/licensed profile data;
- MCP client trust and local host credentials;
- release artifacts and plugin supply chain.

## Adversaries and failure sources

- a malicious or prompt-injected MCP client;
- a vulnerable, counterfeit, or hostile adapter;
- an untrusted driver plugin or profile;
- another process or user on the local host;
- DNS rebinding or unintended LAN exposure;
- malformed vehicle responses and parser bugs;
- a compromised dependency or release workflow;
- accidental operator misuse.

## Main abuse cases

| Abuse case | Primary mitigation | Residual risk |
|---|---|---|
| Model asks for a write/clear | No such MCP tool; core default-deny policy | Bug or malicious plugin |
| Profile smuggles a mutable request | Declarative schema; immutable service policy | Incorrect service classification |
| Remote caller reaches HTTP | Loopback validation and SDK rebinding protection | Compromised local host |
| Raw response leaks identity | Normalized models, VIN-shaped-value rejection, and pseudonymous fingerprinting | Fingerprint, suffix, DTC, and timestamp correlation |
| Plugin bypasses policy | Third-party plugins disabled by default; explicit opt-in, review, and pinning | Enabled Python code is fully trusted and can bypass in-process controls |
| Concurrent requests confuse ECU replies | Per-vehicle/per-ECU locks and bounded reads | Adapter/ECU-specific behavior |
| Malformed ECU data crashes server | Typed validation, bounded errors, tests | Novel parser bugs |
| Proprietary data enters release | Ignore rules, manifest checks, artifact inspection | Incorrect contributor declaration |
| Supply-chain compromise | Lockfile, audit, pinned release process, least-privilege CI | Registry/account compromise |

## Trust decisions

- Third-party driver discovery and execution are disabled by default. After
  explicit opt-in, plugin code executes in-process and is fully trusted; the
  normalized interface and response validation are not a sandbox. A future
  high-risk deployment should isolate drivers in a separate least-privilege
  process.
- The local host and user account are trusted in the MVP.
- Host/Origin validation is not authentication. HTTP remains loopback-only.
- Software read-only policy does not prove adapter firmware or hardware is
  passive.

## Security acceptance criteria

- stdio stdout contains only MCP protocol traffic;
- non-loopback HTTP configuration is rejected before bind;
- no raw command API exists at the driver, service, or MCP layer;
- VIN-shaped values are rejected from configuration and structured results;
- application and built-in-driver logs contain no full VINs;
- mutable service identifiers fail closed;
- database and newly created parent directory permissions are restrictive;
- error messages do not expose profile contents, serial data, or secrets;
- Uvicorn access logging is disabled so request targets cannot expose vehicle
  identifiers;
- prompt, resource, tool, and final serialized result sizes are bounded;
- packages contain no private profiles, captures, databases, or test state.

Update this document whenever adding a transport, remote authentication,
driver isolation, raw capture, profile registry, or write-adjacent protocol
feature.
