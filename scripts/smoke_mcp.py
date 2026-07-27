#!/usr/bin/env python3
"""Smoke an installed OBD MCP executable over stdio."""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "obd_list_vehicles",
    "obd_get_vehicle_status",
    "obd_read_standard_pids",
    "obd_read_dtcs",
    "obd_read_ecu_snapshot",
    "obd_open_issue",
    "obd_get_issue_timeline",
}


async def smoke(command: str) -> None:
    # macOS exposes /tmp through a symlink. The server deliberately rejects
    # symlinked SQLite parents, so create this disposable directory beneath the
    # canonical checkout used to launch the release smoke.
    with tempfile.TemporaryDirectory(
        prefix="obd-mcp-smoke-",
        dir=Path.cwd().resolve(),
    ) as temporary:
        root = Path(temporary)
        config = root / "obd-mcp.toml"
        config.write_text(
            """
[storage]
path = "issues.sqlite3"

[[vehicles]]
id = "artifact-demo"
name = "Artifact smoke vehicle"
driver = "simulator"
""".strip(),
            encoding="utf-8",
        )
        parameters = StdioServerParameters(
            command=command,
            args=["stdio", "--config", str(config)],
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=15),
            ) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            if names != EXPECTED_TOOLS:
                raise RuntimeError(f"unexpected tool set: {sorted(names)}")
            vehicles = await session.call_tool("obd_list_vehicles")
            if vehicles.isError or vehicles.structuredContent is None:
                raise RuntimeError("installed MCP vehicle discovery failed")
            vehicle_id = vehicles.structuredContent["vehicles"][0]["vehicle_id"]
            if vehicle_id != "artifact-demo":
                raise RuntimeError(f"unexpected vehicle id: {vehicle_id}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", default="obd-mcp")
    args = parser.parse_args()
    asyncio.run(smoke(args.command))
    print("Installed stdio MCP smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
