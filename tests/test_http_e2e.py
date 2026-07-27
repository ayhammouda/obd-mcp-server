from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

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
    assert server.session_manager.session_idle_timeout == 300.0
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
        ) as (read_stream, write_stream, _session_id),
        ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=10),
        ) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        vehicles = await session.call_tool("obd_list_vehicles")
        raw_vin = "A" * 17
        encoded_vin = "".join(f"%{ord(character):02X}" for character in raw_vin)
        with pytest.raises(McpError) as prompt_error:
            await session.get_prompt(raw_vin)
        with pytest.raises(McpError) as resource_error:
            await session.read_resource(f"obd://{raw_vin}")
        with pytest.raises(McpError) as encoded_resource_error:
            await session.read_resource(f"obd://unknown/{encoded_vin}")

    assert len(tools.tools) == 7
    assert vehicles.isError is False
    assert vehicles.structuredContent is not None
    assert vehicles.structuredContent["vehicles"][0]["vehicle_id"] == "demo"
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
                ) as (read_stream, write_stream, _session_id),
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=10),
                ) as session,
            ):
                await session.initialize()
                vehicles = await session.call_tool("obd_list_vehicles")
                assert vehicles.isError is False


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
        ) as (first_read, first_write, _first_session_id),
        ClientSession(
            first_read,
            first_write,
            read_timeout_seconds=timedelta(seconds=10),
        ) as first_session,
    ):
        await first_session.initialize()
        async with (
            streamable_http_client(
                "http://127.0.0.1:8765/mcp",
                http_client=http_client,
            ) as (second_read, second_write, _second_session_id),
            ClientSession(
                second_read,
                second_write,
                read_timeout_seconds=timedelta(seconds=10),
            ) as second_session,
        ):
            await second_session.initialize()
            assert (await second_session.list_tools()).tools

        vehicles = await first_session.call_tool("obd_list_vehicles")
        assert vehicles.isError is False
