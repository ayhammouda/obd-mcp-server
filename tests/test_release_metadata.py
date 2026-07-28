from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
MCP_NAME = "io.github.ayhammouda/obd-mcp-server"
REPOSITORY_URL = "https://github.com/ayhammouda/obd-mcp-server"


def _read_json(name: str) -> object:
    return json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))


def test_server_metadata_is_source_only_and_versioned_with_the_package() -> None:
    server = _read_json("server.json")
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert isinstance(server, dict)
    assert server == {
        "$schema": ("https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"),
        "name": MCP_NAME,
        "description": (
            "Safety-first, read-only vehicle diagnostics over MCP with a "
            "simulator-first local workflow."
        ),
        "title": "OBD MCP Server",
        "websiteUrl": REPOSITORY_URL,
        "repository": {
            "url": REPOSITORY_URL,
            "source": "github",
        },
        "version": project["project"]["version"],
    }
    assert len(server["description"]) <= 100
    assert "packages" not in server
    assert "remotes" not in server


def test_project_mcp_config_uses_the_safe_simulator_checkout() -> None:
    config = _read_json(".mcp.json")

    assert config == {
        "mcpServers": {
            "obd": {
                "type": "stdio",
                "command": "uv",
                "args": [
                    "--directory",
                    "${CLAUDE_PROJECT_DIR:-.}",
                    "run",
                    "obd-mcp",
                    "stdio",
                    "--config",
                    "${CLAUDE_PROJECT_DIR:-.}/examples/obd-mcp.toml",
                ],
            }
        }
    }


def test_packaged_readme_declares_the_future_registry_identity_once() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert readme.count(f"<!-- mcp-name: {MCP_NAME} -->") == 1


def test_release_workflow_requires_reviewed_immutable_release_inputs() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    expected_fragments = (
        "create_github_release:",
        "confirm_tag:",
        "inputs.confirm_tag == inputs.tag",
        'git cat-file -t "refs/tags/${RELEASE_TAG}"',
        "git rev-parse origin/main",
        "mcp-publisher_linux_amd64.tar.gz",
        "1370446bbe74d562608e8005a6ccce02d146a661fbd78674e11cc70b9618d6cf",
        "validate server.json",
        "github-draft:",
        "needs: [verify, github-draft]",
        "needs['github-draft'].result == 'success'",
        "gh release create",
        "--draft",
        "gh release upload",
        "gh release edit",
        "--draft=false",
    )
    for fragment in expected_fragments:
        assert fragment in workflow
    assert workflow.index("\n  github-draft:") < workflow.index("\n  publish:")
    assert workflow.index("\n  publish:") < workflow.index("\n  github-release:")


def test_all_third_party_actions_are_pinned_to_commits() -> None:
    action_uses: list[str] = []
    for workflow_path in (PROJECT_ROOT / ".github/workflows").glob("*.yml"):
        workflow = workflow_path.read_text(encoding="utf-8")
        action_uses.extend(re.findall(r"uses:\s*(\S+)", workflow))

    assert action_uses
    assert [
        value for value in action_uses if re.fullmatch(r"[^@]+@[0-9a-f]{40}", value) is None
    ] == []
