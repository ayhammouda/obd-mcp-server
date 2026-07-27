# Contributing

Thank you for helping build a safe, generic vehicle-diagnostics boundary.

## Before opening a change

Read:

- `AGENTS.md`
- `docs/safety.md`
- `docs/legal-and-data-policy.md`
- `docs/architecture.md`

Changes that add vehicle writes, raw command passthrough, fault clearing,
security access, coding, flashing, routine/actuator control, gateway bypass, or
unlicensed data will not be accepted.

## Development setup

From a current checkout:

```bash
uv sync --all-extras
uv run pytest
```

The GitHub repository is a pre-publication setup item. Clone instructions
should be added only after the repository exists and its ownership is
verified.

Create a focused branch, keep the change small, and add tests. Do not use a
real vehicle for ordinary development or CI.

For built-in drivers, `obd-mcp check-config` validates driver options,
profiles, and optional dependency availability without constructing a driver
or connecting to a vehicle. Third-party Python drivers are disabled by default
and execute as trusted in-process code only after
`extensions.allow_third_party_drivers = true`; a plugin contribution requires
separate code, dependency, license, and threat review.

## Required checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv build --build-constraints build-constraints.txt --require-hashes
uv run twine check dist/*
uv run pip-audit
```

Safety-policy changes need positive and negative tests. Driver changes should
include simulator, replay, or mocked-adapter tests; hardware evidence is
supplementary and must be anonymized.

## Data contributions

Do not open a pull request containing OEM databases, copied standards content,
VINs, logs, or raw vehicle captures.

A distributable profile needs:

- exact provenance and acquisition method;
- copyright/database owner when known;
- SPDX or durable license reference;
- explicit redistribution and modification permission;
- vehicle/network/ECU/version scope;
- confidence and validation fixtures;
- no personal or vehicle-identifying data.

Maintainers may require independent legal review or decline the contribution.

## Developer Certificate of Origin

Every commit must include a sign-off certifying the
[Developer Certificate of Origin 1.1](https://developercertificate.org/):

```bash
git commit -s -m "feat: describe the change"
```

The sign-off means you have the right to submit the contribution under this
project's dual-license terms, allowing recipients to choose Apache-2.0 or MIT.
The sign-off name and email must match the commit author, and merge commits are
checked too. It does not cure missing rights in third-party data.

## Pull requests

Explain:

- the user-visible behavior;
- why the change remains read-only;
- new dependencies and their licenses;
- data provenance, if applicable;
- validation performed;
- hardware touched, if any.

Do not mix unrelated refactors with a safety or driver change.

The maintainer must publish a verified private conduct-reporting contact and
enable private vulnerability reporting before accepting public contributions.
Until those channels exist, do not send sensitive conduct or security details
through public issues.
