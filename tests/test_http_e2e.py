from __future__ import annotations

from pathlib import Path

import httpx2 as httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from obd_mcp.config import AppConfig, ServerConfig, StorageConfig
from obd_mcp.server import create_server


@pytest.mark.asyncio
async def test_streamable_http_asgi_initializes_over_loopback(tmp_path: Path) -> None:
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=8765),
        storage=StorageConfig(path=tmp_path / "issues.sqlite3"),
    )
    server = create_server(config)
    app = server.streamable_http_app()
    assert server._lowlevel_server.session_manager.session_idle_timeout == 300.0
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8765",
        ) as http_client,
        streamable_http_client(
            "http://127.0.0.1:8765/mcp",
            http_client=http_client,
        ) as (read_stream, write_stream),
        ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=10.0,
        ) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        vehicles = await session.call_tool("obd_list_vehicles")
        raw_vin = "A" * 17
        invalid_tool = await session.call_tool("obd_get_vehicle_status", {"vehicle_id": raw_vin})
        unknown_tool = await session.call_tool(raw_vin)
        encoded_vin = "".join(f"%{ord(character):02X}" for character in raw_vin)
        with pytest.raises(MCPError) as prompt_error:
            await session.get_prompt(raw_vin)
        with pytest.raises(MCPError) as resource_error:
            await session.read_resource(f"obd://{raw_vin}")
        with pytest.raises(MCPError) as encoded_resource_error:
            await session.read_resource(f"obd://unknown/{encoded_vin}")

    assert len(tools.tools) == 7
    assert vehicles.is_error is False
    assert vehicles.structured_content is not None
    assert vehicles.structured_content["vehicles"][0]["vehicle_id"] == "demo"
    assert invalid_tool.is_error is True
    assert unknown_tool.is_error is True
    assert raw_vin not in invalid_tool.model_dump_json()
    assert raw_vin not in unknown_tool.model_dump_json()
    assert raw_vin not in str(prompt_error.value)
    assert raw_vin not in str(resource_error.value)
    assert raw_vin not in str(encoded_resource_error.value)
    assert encoded_vin not in str(encoded_resource_error.value)


@pytest.mark.asyncio
async def test_streamable_http_runtime_survives_sequential_sessions(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=8765),
        storage=StorageConfig(path=tmp_path / "issues.sqlite3"),
    )
    server = create_server(config)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8765",
        ) as http_client,
    ):
        for _ in range(2):
            async with (
                streamable_http_client(
                    "http://127.0.0.1:8765/mcp",
                    http_client=http_client,
                ) as (read_stream, write_stream),
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=10.0,
                ) as session,
            ):
                await session.initialize()
                vehicles = await session.call_tool("obd_list_vehicles")
                assert vehicles.is_error is False


@pytest.mark.asyncio
async def test_streamable_http_session_close_does_not_stop_an_overlapping_session(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=8765),
        storage=StorageConfig(path=tmp_path / "issues.sqlite3"),
    )
    server = create_server(config)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8765",
        ) as http_client,
        streamable_http_client(
            "http://127.0.0.1:8765/mcp",
            http_client=http_client,
        ) as (first_read, first_write),
        ClientSession(
            first_read,
            first_write,
            read_timeout_seconds=10.0,
        ) as first_session,
    ):
        await first_session.initialize()
        async with (
            streamable_http_client(
                "http://127.0.0.1:8765/mcp",
                http_client=http_client,
            ) as (second_read, second_write),
            ClientSession(
                second_read,
                second_write,
                read_timeout_seconds=10.0,
            ) as second_session,
        ):
            await second_session.initialize()
            assert (await second_session.list_tools()).tools

        vehicles = await first_session.call_tool("obd_list_vehicles")
        assert vehicles.is_error is False
