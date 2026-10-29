"""Deployment configuration checks use Compose's normalized configuration."""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def compose_config(tmp_path: Path, schedule: str = "06:00,18:00") -> dict:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=deployment-test-db-password-0123456789abcdef\n"
        "GARMIN_MCP_HTTP_BEARER_TOKEN=deployment-test-token-0123456789abcdef\n"
        "GARMIN_MCP_TIMEZONE=America/Denver\n"
        f"GARMIN_MCP_SYNC_TIMES={schedule}\n"
    )
    environment = os.environ.copy()
    for key in (
        "POSTGRES_PASSWORD",
        "GARMIN_MCP_HTTP_BEARER_TOKEN",
        "GARMIN_MCP_TIMEZONE",
        "GARMIN_MCP_SYNC_TIMES",
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
    assert services["mcp"]["command"] == [
        "serve-http",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    assert services["mcp"]["environment"]["GARMIN_MCP_HTTP_BEARER_TOKEN"]


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
