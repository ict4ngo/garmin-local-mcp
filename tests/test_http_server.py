"""Authenticated Streamable HTTP integration tests."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from garmin_mcp import config, server
from garmin_mcp.cli import build_parser
from garmin_mcp.http_auth import create_http_app

TOKEN = "test-shared-token"


def test_serve_http_cli_has_bind_defaults_and_overrides() -> None:
    parser = build_parser()
    defaults = parser.parse_args(["serve-http"])
    assert (defaults.host, defaults.port) == ("127.0.0.1", 8000)

    overrides = parser.parse_args(["serve-http", "--host", "0.0.0.0", "--port", "8081"])
    assert (overrides.host, overrides.port) == ("0.0.0.0", 8081)
    assert parser.parse_args(["serve"]).command == "serve"


@pytest.fixture(scope="module")
def http_server(tmp_path_factory) -> Iterator[tuple[str, str]]:
    env_names = (
        "GARMIN_MCP_DATA_DIR",
        "GARMIN_MCP_DATABASE_URL",
        "GARMIN_MCP_SYNC_TIMES",
        "GARMIN_MCP_TIMEZONE",
    )
    original_env = {name: os.environ.get(name) for name in env_names}
    original_config = server._config
    database_url = os.environ.get("GARMIN_MCP_TEST_DATABASE_URL")
    if not database_url:
        pytest.fail("GARMIN_MCP_TEST_DATABASE_URL is required for PostgreSQL tests")
    os.environ["GARMIN_MCP_DATA_DIR"] = str(tmp_path_factory.mktemp("http-server"))
    os.environ["GARMIN_MCP_DATABASE_URL"] = database_url
    os.environ["GARMIN_MCP_SYNC_TIMES"] = "09:00"
    os.environ["GARMIN_MCP_TIMEZONE"] = "UTC"
    server._config = None

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    lan_host = f"192.168.1.42:{port}"
    app = create_http_app(
        server.mcp,
        TOKEN,
        allowed_hosts=[lan_host, "127.0.0.1:*", "localhost:*"],
    )

    instance = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, lifespan="on")
    )
    thread = threading.Thread(
        target=instance.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not instance.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not instance.started:
        instance.should_exit = True
        thread.join(timeout=5)
        for name, value in original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        server._config = original_config
        pytest.fail("Uvicorn did not start the HTTP test server")

    try:
        yield f"http://127.0.0.1:{port}/mcp", lan_host
    finally:
        instance.should_exit = True
        thread.join(timeout=10)
        for name, value in original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        server._config = original_config
        if thread.is_alive():
            pytest.fail("Uvicorn HTTP test server did not stop")


def test_http_requires_bearer_token(http_server: tuple[str, str]) -> None:
    response = httpx.post(
        http_server[0],
        headers={"Host": http_server[1]},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    )
    assert response.status_code == 401


def test_http_rejects_wrong_token(http_server: tuple[str, str]) -> None:
    response = httpx.post(
        http_server[0],
        headers={
            "Authorization": "Bearer not-the-token",
            "Host": http_server[1],
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    )
    assert response.status_code == 401


def test_http_startup_requires_bearer_token(monkeypatch) -> None:
    monkeypatch.delenv("GARMIN_MCP_HTTP_BEARER_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GARMIN_MCP_HTTP_BEARER_TOKEN"):
        server.serve_http()


def test_streamable_http_initialize_and_tool_call(http_server: tuple[str, str]) -> None:
    async def exercise_mcp() -> None:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {TOKEN}"}
        ) as http_client:
            async with streamable_http_client(
                http_server[0], http_client=http_client
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert "auth_status" in {tool.name for tool in tools.tools}
                    result = await session.call_tool("auth_status", {})
                    assert result.isError is False
                    assert json.loads(result.content[0].text)["logged_in"] is False

    asyncio.run(exercise_mcp())


def test_http_accepts_configured_lan_host(http_server: tuple[str, str]) -> None:
    async def exercise_mcp() -> None:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Host": http_server[1],
            }
        ) as http_client:
            async with streamable_http_client(
                http_server[0], http_client=http_client
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool("auth_status", {})
                    assert result.isError is False
                    assert json.loads(result.content[0].text)["logged_in"] is False

    asyncio.run(exercise_mcp())


def test_http_rejects_unlisted_host(http_server: tuple[str, str]) -> None:
    response = httpx.post(
        http_server[0],
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Host": "192.168.1.143:8000",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    )
    assert response.status_code == 421


def test_http_allowed_hosts_follow_compose_runtime_config(monkeypatch) -> None:
    monkeypatch.setenv("GARMIN_MCP_LAN_IP", "192.168.1.143")
    monkeypatch.setenv("GARMIN_MCP_HTTP_PORT", "8123")

    assert config.http_allowed_hosts() == [
        "192.168.1.143:8123",
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
    ]


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("192.168.1.143", True),
        ("10.0.0.2", True),
        ("fc00::1", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("8.8.8.8", False),
        ("2001:4860:4860::8888", False),
        ("0.0.0.0", False),
        ("::", False),
        ("224.0.0.1", False),
        ("ff02::1", False),
        ("240.0.0.1", False),
        ("192.0.2.10", False),
        ("169.254.1.1", False),
        ("fe80::1", False),
    ],
)
def test_config_rejects_public_or_unsafe_lan_ips(
    monkeypatch, address: str, allowed: bool
) -> None:
    monkeypatch.setenv("GARMIN_MCP_LAN_IP", address)

    if allowed:
        result = config.http_allowed_hosts()
        assert result[0].startswith(f"[{address}]:" if ":" in address else f"{address}:")
    else:
        with pytest.raises(ValueError, match="loopback or private unicast"):
            config.http_allowed_hosts()
