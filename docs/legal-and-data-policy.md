# Legal and data policy

Last reviewed: 2026-07-27.

This document records the project's compliance posture. It is not legal advice
and does not promise compliance in every jurisdiction or use case.

## Separate code rights from data rights

The repository's original source code is offered under
`Apache-2.0 OR MIT`, at the recipient's option. Neither license grants rights
to:

- vehicle-manufacturer documentation or databases;
- diagnostic-tool software or screenshots;
- ISO, SAE, or other standards publications;
- third-party ECU identifiers, formulas, DTC descriptions, or captures;
- vehicle or product trademarks.

A profile or fixture needs its own lawful source and license even when the code
that loads it is dual-licensed under Apache-2.0 or MIT.

## Repository distribution rule

The public repository and release artifacts must not contain:

- OEM manuals, workshop data, diagnostic databases, screenshots, or software;
- content copied from ISO/SAE standards;
- DDT, CLIP, PyRen, DDT4All, or similar data without a verified license that
  permits the exact redistribution;
- VINs, user logs, raw vehicle captures, or private profile mounts;
- credentials, subscription content, access tokens, or circumvention tooling.

Bundled profiles require an affirmative `redistribution_allowed` declaration,
a declared SPDX/data license, provenance, and maintainer review. “Found
online,” “community,” “reverse engineered,” and “I can access it” are not
licenses.

The bundled synthetic example is original demonstration data marked
`CC0-1.0`; it does not describe a real vehicle or ECU. That narrow dedication
does not apply to third-party profiles or dependencies.

## OEM technical information

Renault's current ASOS conditions are a useful concrete example of why data is
external. The official conditions describe Renault technical information as
protected content, scope its use to qualified repair professionals and
repair/maintenance activity, restrict geography in parts of the offer, and
prohibit uses outside the granted authorization without prior permission.

The core therefore contains no Renault data. A user who lawfully obtains
licensed information may mount a private profile only within their license.
The project must not publish, sync, or convert that profile into a public
database.

Primary source:
[Renault ASOS general conditions and conditions of technical-information use](https://a5o.public.asdh.aws.renault.com/documents/Terms_and_Conditions_EN_V4.pdf).

## Access rights are not redistribution rights

Article 61 of Regulation (EU) 2018/858 requires manufacturers to provide
independent operators with standardized, non-discriminatory access to vehicle
OBD and repair/maintenance information, subject to the regulation's scope and
conditions. Article 63 permits reasonable and proportionate access fees.

That access framework does not say that any recipient may republish licensed
databases or dealership software under this project's license. Keep the
acquisition right, contract terms, copyright/database rights, and
redistribution permission as separate questions.

Primary source:
[Regulation (EU) 2018/858](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32018R0858).

## Standards

This project may identify an interoperable protocol or service by name and
implement independently written behavior. Interoperability, access, or the
ability to observe a vehicle response does not itself grant a right to
redistribute protected material. The project must not reproduce standards
publications, tables, prose, diagrams, or proprietary datasets.

ISO states that its publications and online content are copyright protected
and that reproduction generally requires permission:
[ISO copyright policy](https://www.iso.org/copyright.html).

Contributors implementing protocol behavior must document the source used and
confirm they have the right to contribute the resulting code and test data.

## Third-party software

Direct runtime dependencies are selected for compatible open-source licenses.
Optional hardware integrations remain separate extras.

- `mcp`: official MCP Python SDK.
- `pydantic`: typed validation.
- `platformdirs`: local data-directory resolution.
- `py-obdii`: optional MIT-licensed ELM327 integration.

The GPL-2.0-only `python-OBD` package is intentionally not used. This is a
distribution-policy choice, not a criticism of that project.

Third-party driver plugins are separate distributions and disabled by default.
Enabling one executes its code in-process. Operators and redistributors must
review the plugin's license, dependencies, data sources, and behavior
independently; compatibility with the driver interface is not a license or
security approval.

Before every release:

```bash
uv run pip-licenses --format=markdown
uv run pip-audit
```

Review transitive licenses and notices; a passing command is not a substitute
for legal review.

## Trademarks and affiliation

The project name is OEM-neutral. Manufacturer, vehicle, adapter, and tool names
may be used only as needed to describe interoperability. Do not use OEM logos
or imply authorization, sponsorship, certification, or endorsement.

## Privacy

VINs, timestamps, fault history, locations, and captures can identify a vehicle
or reveal usage. The software defaults to local processing, VIN
fingerprinting, no raw capture, and minimal issue storage. Raw VIN-shaped
values are rejected at configuration and public tool-output boundaries. The
deterministic truncated fingerprint and retained suffix remain pseudonymous
vehicle-related data; they are not anonymous.

Where the GDPR applies, controllers must independently establish a lawful
basis and comply with purpose limitation, data minimization, storage
limitation, security, transparency, and data-subject rights. The project
defaults support those principles but do not complete an operator's
obligations.

Primary sources:

- [GDPR Article 5](https://eur-lex.europa.eu/eli/reg/2016/679/2016-05-04/eng)
- [European Commission GDPR principles](https://commission.europa.eu/law/law-topic/data-protection/information-business-and-organisations/principles-gdpr_en)

## Contributor certification

Contributors certify origin using the Developer Certificate of Origin sign-off
described in `CONTRIBUTING.md`. A data contribution must also include:

- exact source and acquisition method;
- copyright/database owner when known;
- license text or durable license URL;
- redistribution and modification permissions;
- scope, version, and confidence;
- synthetic/replay provenance for tests.

Maintainers may reject data even when a contributor believes it is lawful.

## Release gate

Do not publish a package, container, profile bundle, or registry entry until:

1. the artifact contents have been inspected;
2. private/licensed paths and vehicle captures are absent;
3. direct and transitive license reports are reviewed;
4. vulnerability and secret scans pass;
5. package/repository identifiers are controlled by the maintainer;
6. install instructions use the artifact actually released;
7. jurisdiction-specific counsel has reviewed any OEM-data distribution or
   commercial offering.
