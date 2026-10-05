from __future__ import annotations

import sys
from pathlib import Path

import pytest
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


@pytest.mark.asyncio
async def test_compiled_stdio_initializes_and_completes_simulator_workflow(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "obd-mcp.toml"
    config_path.write_text(
        """
[storage]
path = "issues.sqlite3"

[[vehicles]]
id = "e2e-demo"
name = "E2E synthetic vehicle"
driver = "simulator"
""".strip(),
        encoding="utf-8",
    )
    diagnostics_path = tmp_path / "server.stderr"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "obd_mcp", "stdio", "--config", str(config_path)],
        cwd=Path.cwd(),
    )

    with diagnostics_path.open("w+", encoding="utf-8") as diagnostics:
        async with (
            stdio_client(parameters, errlog=diagnostics) as (
                read_stream,
                write_stream,
            ),
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=10.0,
            ) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS

            vehicles = await session.call_tool("obd_list_vehicles")
            assert vehicles.is_error is False
            assert vehicles.structured_content is not None
            assert vehicles.structured_content["vehicles"][0]["vehicle_id"] == "e2e-demo"

            readings = await session.call_tool(
                "obd_read_standard_pids",
                {"vehicle_id": "e2e-demo", "pids": ["010C", "0142"]},
            )
            assert readings.is_error is False
            assert readings.structured_content is not None
            assert len(readings.structured_content["readings"]) == 2

            opened = await session.call_tool(
                "obd_open_issue",
                {
                    "vehicle_id": "e2e-demo",
                    "title": "Synthetic observation",
                    "dtc_codes": ["P0300"],
                },
            )
            assert opened.is_error is False
            assert opened.structured_content is not None
            issue_id = opened.structured_content["issue_id"]

            timeline = await session.call_tool(
                "obd_get_issue_timeline",
                {"issue_id": issue_id},
            )
            assert timeline.is_error is False
            assert timeline.structured_content is not None
            assert timeline.structured_content["issue"]["issue_id"] == issue_id

        diagnostics.seek(0)
        assert "Traceback" not in diagnostics.read()
