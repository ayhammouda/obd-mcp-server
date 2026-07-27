# Publishing runbook

No release exists merely because a workflow file exists. Complete and record
each gate for the first and every subsequent release.

## One-time setup

- [ ] Create and verify `github.com/ayhammouda/obd-mcp-server`.
- [ ] Reserve and control the intended PyPI project name.
- [ ] Confirm that the source and distribution names do not collide with an
      unrelated package.
- [ ] Enable branch protection/rulesets and require CI/security review.
- [ ] Enable and test private vulnerability reporting, update `SECURITY.md`,
      and enable Dependabot security updates.
- [ ] Publish and test a private conduct-reporting contact, then update
      `CODE_OF_CONDUCT.md`.
- [ ] Configure a PyPI trusted publisher for the exact repository/workflow.
- [ ] Decide whether MCP Registry publication is desired; validate its current
      metadata schema from official docs at that time.
- [ ] Configure release environments with maintainer approval.

Until these are complete, documentation uses checkout/source installation and
must not claim PyPI or MCP Registry availability.

The optional ELM327 integration also remains explicitly unvalidated on real
hardware. CI covers simulator, mocked-adapter, and installed-wheel behavior
only. Do not turn that into an adapter, vehicle, or platform compatibility
claim before a stationary manual hardware run is reviewed and sanitized.

## Prepare

1. Start from reviewed `main` with a clean tree.
2. Update version and changelog once, then regenerate `uv.lock`.
3. Confirm dependency licenses and advisories:

   ```bash
   uv lock --check
   uv run pip-licenses --format=markdown
   uv run pip-audit
   uv run pip-audit -r build-constraints.txt --disable-pip
   ```

4. Run all quality gates:

   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src
   uv run pytest
   uv run python scripts/check_repository_data.py
   uv run obd-mcp check-config --config examples/obd-mcp.toml
   ```

5. Build once:

   ```bash
   uv build --build-constraints build-constraints.txt --require-hashes
   uv run twine check dist/*
   uv run python scripts/check_distribution.py dist/*
   mkdir rebuilt
   uv build dist/*.tar.gz --wheel --out-dir rebuilt \
     --build-constraints build-constraints.txt --require-hashes
   cmp dist/*.whl rebuilt/*.whl
   ```

## Inspect the exact artifacts

```bash
python3 -m zipfile -l dist/*.whl
tar -tzf dist/*.tar.gz
shasum -a 256 dist/*
```

Confirm:

- no `profiles/private`, `profiles/licensed`, captures, databases, VINs,
  credentials, `.env`, logs, test state, or internal agent instructions;
- required license, notice, README, and package metadata exist;
- the synthetic example profile is the intended CC0-1.0 content and contains
  no material from a real vehicle, OEM database, or standards publication;
- wheel imports without optional extras;
- sdist rebuild produces an equivalent wheel.

## Fresh-install smoke

Use a new temporary environment and install the wheel, not the checkout:

```bash
uv venv --python 3.11 /tmp/obd-mcp-release-venv
uv pip install --python /tmp/obd-mcp-release-venv/bin/python dist/*.whl
/tmp/obd-mcp-release-venv/bin/obd-mcp --version
/tmp/obd-mcp-release-venv/bin/obd-mcp --help
/tmp/obd-mcp-release-venv/bin/obd-mcp check-config \
  --config examples/obd-mcp.toml
/tmp/obd-mcp-release-venv/bin/python scripts/smoke_mcp.py \
  --command /tmp/obd-mcp-release-venv/bin/obd-mcp
```

The final command runs MCP `initialize`, `tools/list`, and a simulator call
over compiled stdio and confirms the seven expected tools.

## Tag and publish

- Tag only the reviewed commit.
- Use PyPI OIDC trusted publishing with provenance; do not store a long-lived
  API token.
- Publish the already tested artifact rather than rebuilding in the publish
  job.
- Verify the PyPI page, hashes, metadata, license, README, and fresh install.
- Create a GitHub release referencing the same hashes.
- Publish an MCP Registry entry only after validating the current official
  schema and a real client installation.

## Current external blockers

The source tree alone cannot complete these items:

- the GitHub repository and package registry names must be created and
  controlled by the maintainer;
- branch rules, private vulnerability reporting, and a private conduct contact
  must be configured and verified on the live repository;
- the PyPI trusted-publisher identity and release environment must be created;
- the optional ELM327 integration needs a reviewed stationary hardware run
  before any compatibility claim;
- OEM, standards-derived, licensed, or commercial data use needs
  jurisdiction- and contract-specific legal review.

The project makes no representation that those gates have already passed.

## Rollback

Published package files are generally immutable. If a release is defective:

1. yank the affected PyPI version without deleting audit history;
2. publish a fixed patch version;
3. update the GitHub advisory/release notes;
4. rotate credentials if supply-chain compromise is suspected;
5. document the incident and prevention action.
