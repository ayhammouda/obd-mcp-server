# Privacy defaults

## Data the project may process

- configured vehicle labels and local IDs;
- connection status and driver capabilities;
- decoded signal observations and DTCs;
- a deterministic, truncated VIN fingerprint and redacted suffix when a driver
  reports a valid VIN;
- issue titles, notes, timestamps, and observations;
- externally mounted profile metadata.

Raw bus captures, precise location, cloud telemetry, and full VIN storage are
not part of the default product.

## Defaults

| Control | Default |
|---|---|
| Processing location | Local machine |
| Network bind | Stdio or loopback |
| Product telemetry | None |
| Full VIN in configuration, MCP results, CLI output, or application logs | Rejected |
| VIN representation | Deterministic SHA-256 fingerprint and redacted suffix |
| Raw capture | Disabled |
| Issue database | Local SQLite |
| File permissions | Owner-only on supported POSIX systems (Linux and macOS) |
| Automatic cloud sync | None |

The initial fingerprint is pseudonymous, not anonymous: it is deterministic
and can correlate the same VIN across observations, and the redacted form
retains the last four characters. Protect it as vehicle-related data. A future
per-install secret would reduce cross-install correlation but is not wired in
the current release.

The configuration loader rejects VIN-shaped text before parsing, including
common marked and bounded hyphen-, underscore-, or whitespace-grouped forms.
CLI summaries redact sensitive path
components. Normalized domain fields and bounded JSON-only metadata reject
VIN-shaped, binary, arbitrary-object, oversized, or deeply nested values. The
MCP facade recursively checks and serializes every final result, including
FastMCP's text and structured copies, against a 1 MiB limit before returning
it. A
driver or hardware library may transiently observe a VIN in memory to derive
`VinIdentity`; plugin and adapter authors remain responsible for avoiding
their own side-channel logs, files, or network transmission. Enabled
third-party plugins are trusted in-process code and are not contained by these
application-level checks.

## Operator responsibilities

The operator chooses the purpose and retention period for issue data and
external profiles. Delete the local database when it is no longer needed,
protect host backups, and avoid placing the data directory in a cloud-synced
folder unless that is an informed choice.

A deployment used for other people's vehicles, a business, insurance,
employment, fleet monitoring, or remote service may create materially
different privacy and regulatory obligations. Perform a data-protection review
before such use.

## Future features

Encryption at rest, configurable retention jobs, export/erasure tooling, and
authenticated multi-user deployments require explicit design work. They must
not be implied by the current local file-permission controls.
