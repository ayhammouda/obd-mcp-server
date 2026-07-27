# Third-party notices

The authoritative notices are the license metadata and license files installed
with each resolved dependency. Generate the current report with:

```bash
uv run pip-licenses --format=markdown
```

Direct dependencies and optional integrations at project creation:

| Package | Purpose | License posture |
|---|---|---|
| `mcp` | Official Model Context Protocol Python SDK | Open-source; verify resolved release |
| `pydantic` | Schema and data validation | MIT |
| `platformdirs` | Local data-directory resolution | MIT |
| `py-obdii` | Optional ELM327 integration | MIT |

The optional GPL-2.0-only `python-OBD` package is not a dependency.

## Synthetic example profile

The original synthetic data in
`examples/profiles/synthetic-powertrain.toml` is offered under
[CC0 1.0 Universal](https://creativecommons.org/publicdomain/zero/1.0/).
It is invented demonstration content and does not describe a real vehicle,
ECU, OEM database, or standards publication. CC0 does not apply to third-party
software, external profiles, or any independently licensed material.

This file is informational and must be refreshed before a release. It does not
replace the third-party license texts or legal review.
