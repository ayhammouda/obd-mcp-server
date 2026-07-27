# External profile format

Profiles add vehicle/ECU metadata without changing MCP tools or driver code.
They are untrusted declarative input.

## Principles

- OEM-neutral core.
- One profile version has a narrow vehicle/network/ECU scope.
- Every profile carries source, origin, license, confidence, and redistribution
  metadata; decoded signals carry units.
- Profiles only select core-approved read operations.
- No scripts, templates, imports, arbitrary payloads, or adapter commands.
- A profile never overrides a core denial.
- Schema version 1 is UDS-only and permits only the modeled read services
  `0x19` (ReadDTCInformation) and `0x22` (ReadDataByIdentifier).
- Profiles are bound by vehicle configuration, not selected by an MCP caller.
- Profiles and configuration must not contain VINs. Configuration and public
  output boundaries reject VIN-shaped text, and release review scans bundled
  profile content.

## Conceptual document

```json
{
  "schema_version": 1,
  "profile_id": "example.synthetic.powertrain",
  "name": "Synthetic powertrain example",
  "version": "1.0.0",
  "provenance": {
    "source": "synthetic",
    "origin": "Generated solely for tests",
    "license": "CC0-1.0",
    "redistribution_allowed": true,
    "confidence": 1.0,
    "notes": "Does not describe a real vehicle."
  },
  "selector": {
    "protocol": "uds",
    "manufacturer": null,
    "model": null,
    "model_year_min": null,
    "model_year_max": null,
    "ecu_ids": ["engine"]
  },
  "reads": [
    {
      "name": "Synthetic counter",
      "ecu_id": "engine",
      "service": "0x22",
      "identifier": "0xF40D",
      "signal_id": "synthetic_counter",
      "description": "Deterministic demonstration value.",
      "decoder": {
        "data_type": "uint16",
        "byte_offset": 0,
        "byte_length": 2,
        "byte_order": "big",
        "scale": 1.0,
        "value_offset": 0.0,
        "unit": "count"
      }
    }
  ]
}
```

The implementation's Pydantic models are the normative schema for `0.1.x`.
Exported JSON Schema will be versioned before a public profile registry is
introduced.

`selector.protocol` is fixed to `uds` in schema version 1. The optional
manufacturer, model, model-year, and ECU fields are descriptive matching
hints; they do not authorize an operation, select an arbitrary transport, or
prove a profile applies to a vehicle.

Schema version 1 rejects identity-bearing identifiers, including UDS DID
`0xF190` (VIN), for every decoder type. New identifiers that can expose a VIN,
serial number, cryptographic identity, account identifier, or another
linkable vehicle/person identifier must be classified as sensitive and denied
in the immutable core policy before a profile may use them.

When a vehicle has a configured profile, `obd_read_ecu_snapshot` uses that
binding and only the reads declared for the requested ECU. The MCP tool has no
`profile_id` argument. When no profile is bound, the tool requests a normalized
standard snapshot constrained to the core's Mode 01 PID allowlist and DTC
model. Drivers may reject either capability they do not implement; notably,
the built-in ELM327 integration currently rejects profile-defined UDS reads.

## Distribution modes

### Bundled

The maintainer invokes validation in bundled mode based on a trusted
repository/package location; a profile cannot self-declare itself safe to
bundle.

Allowed only when:

- `redistribution_allowed` is true;
- a license is declared and compatible with distribution;
- provenance is reviewable;
- fixtures contain no personal/vehicle data;
- artifact checks include the profile intentionally.

### Private

Mounted from outside the repository for local use. The loader may accept
`redistribution_allowed: false`, but the profile must remain outside packages,
containers, examples, logs, tests, and support bundles. The user remains
responsible for their license terms.

## Source labels

| Label | Meaning |
|---|---|
| `standard` | Independently implemented interoperable definition with documented lawful source |
| `licensed-oem` | Used under an OEM/owner license; normally private and non-redistributable |
| `community` | Non-authoritative community definition with a verified license |
| `synthetic` | Invented test/demo data with no claim about a real vehicle |

“Community” is not itself permission to redistribute.

## Validation pipeline

1. Parse and reject unknown/unsafe structure, including duplicate JSON object
   keys at any nesting level.
2. Validate provenance and distribution rules.
3. Require the schema-version-1 UDS protocol and validate `0x19`/`0x22`
   service shape against the immutable core policy.
4. Reject duplicate or ambiguous ECU/identifier definitions.
5. Reject duplicate signal IDs within an ECU.
6. Validate units, finite scales/offsets, decoder bounds, and transformed
   numeric ranges.
7. Run replay/synthetic fixtures.
8. Record the version and artifact hash in the distribution review.
9. Publish capabilities with source and confidence labels.

For a built-in vehicle configuration, `obd-mcp check-config` performs this
profile validation and strict driver-option validation without constructing a
driver or contacting a vehicle.

## Plugin boundary

Profiles do not register Python entry points. Driver packages may register
under `obd_mcp.drivers`, but profile parsing and policy enforcement stay in the
core.
