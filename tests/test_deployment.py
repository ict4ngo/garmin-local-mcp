"""Deployment configuration checks use Compose's normalized configuration."""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def compose_config(
    tmp_path: Path,
    schedule: str = "06:00,18:00",
    *,
    lan_ip: str | None = None,
    http_port: int | None = None,
) -> dict:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=deployment-test-db-password-0123456789abcdef\n"
        "GARMIN_MCP_HTTP_BEARER_TOKEN=deployment-test-token-0123456789abcdef\n"
        "GARMIN_MCP_TIMEZONE=America/Denver\n"
        f"GARMIN_MCP_SYNC_TIMES={schedule}\n"
        + (f"GARMIN_MCP_LAN_IP={lan_ip}\n" if lan_ip is not None else "")
        + (f"GARMIN_MCP_HTTP_PORT={http_port}\n" if http_port is not None else "")
    )
    environment = os.environ.copy()
    for key in (
        "POSTGRES_PASSWORD",
        "GARMIN_MCP_HTTP_BEARER_TOKEN",
        "GARMIN_MCP_TIMEZONE",
        "GARMIN_MCP_SYNC_TIMES",
        "GARMIN_MCP_LAN_IP",
        "GARMIN_MCP_HTTP_PORT",
    ):
        environment.pop(key, None)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(ROOT),
            "--env-file",
            str(env_file),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_compose_keeps_database_private(tmp_path):
    config = compose_config(tmp_path)
    postgres = config["services"]["postgres"]

    assert not postgres.get("ports")
    assert postgres["image"].startswith("postgres:17")
    assert any(
        mount["type"] == "volume"
        and mount["source"] == "postgres_data"
        and mount["target"] == "/var/lib/postgresql/data"
        for mount in postgres["volumes"]
    )


def test_compose_publishes_authenticated_mcp_only(tmp_path):
    config = compose_config(tmp_path)
    services = config["services"]
    published = [
        (name, port)
        for name, service in services.items()
        for port in service.get("ports", [])
    ]

    assert len(published) == 1
    service_name, port = published[0]
    assert service_name == "mcp"
    assert str(port["published"]) == "8000"
    assert str(port["target"]) == "8000"
    assert port["host_ip"] == "127.0.0.1"
    assert services["mcp"]["command"] == [
        "serve-http",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    assert services["mcp"]["environment"]["GARMIN_MCP_HTTP_BEARER_TOKEN"]
    assert "GARMIN_MCP_HTTP_BEARER_TOKEN" not in services["sync-worker"]["environment"]


def test_compose_shares_raw_volume_with_worker(tmp_path):
    config = compose_config(tmp_path)
    services = config["services"]

    for name in ("mcp", "sync-worker"):
        assert any(
            mount["type"] == "volume"
            and mount["source"] == "garmin_data"
            and mount["target"] == "/data"
            for mount in services[name]["volumes"]
        )
        assert services[name]["environment"]["GARMIN_MCP_DATA_DIR"] == "/data"
    assert config["volumes"]["garmin_data"]


def test_compose_schedule_override_reaches_worker(tmp_path):
    first = compose_config(tmp_path, "06:00,18:00")
    second = compose_config(tmp_path, "07:15,21:45")

    assert first["services"]["sync-worker"]["environment"]["GARMIN_MCP_SYNC_TIMES"] == (
        "06:00,18:00"
    )
    assert second["services"]["sync-worker"]["environment"]["GARMIN_MCP_SYNC_TIMES"] == (
        "07:15,21:45"
    )
    assert first["services"]["sync-worker"].get("build") == second["services"][
        "sync-worker"
    ].get("build")


def test_compose_lan_bind_and_port_are_configurable(tmp_path):
    config = compose_config(tmp_path, lan_ip="192.168.1.42", http_port=8123)
    [port] = config["services"]["mcp"]["ports"]

    assert port["host_ip"] == "192.168.1.42"
    assert str(port["published"]) == "8123"
    assert str(port["target"]) == "8000"
    assert not config["services"]["postgres"].get("ports")


def test_compose_passes_host_allowlist_inputs_to_mcp_only(tmp_path):
    config = compose_config(tmp_path, lan_ip="192.168.1.143", http_port=8123)
    services = config["services"]
    mcp_environment = services["mcp"]["environment"]
    worker_environment = services["sync-worker"]["environment"]

    assert mcp_environment["GARMIN_MCP_LAN_IP"] == "192.168.1.143"
    assert str(mcp_environment["GARMIN_MCP_HTTP_PORT"]) == "8123"
    assert "GARMIN_MCP_LAN_IP" not in worker_environment
    assert "GARMIN_MCP_HTTP_PORT" not in worker_environment
