## Summary

<!-- What changed and why? -->

## Safety boundary

- [ ] No vehicle write, clear, reset, session, security, routine, actuator,
      coding, flashing, gateway-bypass, or raw-command capability was added.
- [ ] Policy changes include positive and negative regression tests.
- [ ] MCP stdout remains protocol-clean and HTTP remains loopback-only.

## Data and dependencies

- [ ] No VIN, capture, OEM database, copied standard, credential, or private
      profile is included.
- [ ] New data has provenance, license, scope, and redistribution permission.
- [ ] New dependencies and licenses are documented and audited.

## Validation

<!-- List exact commands and any opt-in simulator/hardware evidence. -->

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy src`
- [ ] `uv run pytest`
- [ ] `uv build && uv run twine check dist/*`
- [ ] `uv run pip-audit`
- [ ] Commits include DCO sign-off.

