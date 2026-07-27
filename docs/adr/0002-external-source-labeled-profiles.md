# ADR-0002: External source-labeled profiles

**Status:** Accepted  
**Date:** 2026-07-27  
**Decider:** Project maintainer

## Context

Standard emissions diagnostics are only a subset of vehicle diagnostics.
Enhanced identifiers are ECU-, vehicle-, market-, software-, and
database-version-specific. OEM technical information and standards content may
be contractually or legally restricted, while community data can have unclear
provenance and uncertain correctness.

## Decision

Ship an OEM-neutral core with no OEM database. Load enhanced definitions from
declarative external profiles. Every profile declares:

- source, origin, and confidence;
- license and redistribution permission;
- vehicle/network/ECU scope;
- explicit read service and identifier;
- unit, decoder, and version.

Schema version 1 is deliberately limited to UDS and the modeled read-only
services `0x19` and `0x22`. A profile is bound to a configured vehicle; an MCP
caller cannot select an arbitrary profile. With no profile binding, the ECU
snapshot capability returns only the core-constrained standard snapshot.

Independent profile bundles should carry an integrity hash or signature in
their distribution manifest; that manifest is outside schema version 1.

Profiles can only select operations already permitted by the immutable core
policy. They cannot add code, raw requests, MCP tools, or mutable services.

Bundled data requires affirmative redistribution permission and a declared
license. Private mounted data may be used locally when redistribution is
disallowed, but packaging checks must exclude it.

## Options considered

### Bundle community/OEM databases

Fastest route to broad coverage, but creates unacceptable provenance,
redistribution, accuracy, maintenance, and user-expectation risks.

### Hard-code identifiers

Simple initially, but couples releases to specific ECUs and obscures source and
license information.

### External declarative profiles

Adds validation and profile-management work, but preserves a clean legal and
safety boundary while keeping the core useful with standard data.

## Consequences

- The initial repository has limited enhanced coverage by design.
- Users retain responsibility for lawful access to private data.
- Every enhanced result communicates its exact source label and confidence.
- Profile schemas need versioning, replay tests, and migration tooling.
- A profile registry may be added later, but only with license verification
  and signed artifacts.
