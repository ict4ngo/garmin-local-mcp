import os
from collections.abc import Mapping

import pytest
from psycopg.conninfo import conninfo_to_dict


def _database_identity(database_url: str) -> tuple[str, str, str | None]:
    params = conninfo_to_dict(database_url)
    host = (params.get("host") or "localhost").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        host = "loopback"
    return host, params.get("port") or "5432", params.get("dbname")


def _validated_test_database_url(environment: Mapping[str, str]) -> str:
    value = environment.get("GARMIN_MCP_TEST_DATABASE_URL", "").strip()
    if not value:
        raise ValueError("GARMIN_MCP_TEST_DATABASE_URL is required for PostgreSQL tests")
    try:
        test_params = conninfo_to_dict(value)
    except Exception as exc:
        raise ValueError("GARMIN_MCP_TEST_DATABASE_URL must be a valid PostgreSQL URL") from exc
    database_name = test_params.get("dbname")
    if not database_name or not database_name.endswith("_test"):
        raise ValueError("GARMIN_MCP_TEST_DATABASE_URL database name must end in _test")

    application_url = environment.get("GARMIN_MCP_DATABASE_URL", "").strip()
    if application_url:
        try:
            same_database = _database_identity(value) == _database_identity(application_url)
        except Exception as exc:
            raise ValueError(
                "GARMIN_MCP_TEST_DATABASE_URL cannot be safely compared with "
                "GARMIN_MCP_DATABASE_URL"
            ) from exc
        if same_database:
            raise ValueError(
                "GARMIN_MCP_TEST_DATABASE_URL must differ from GARMIN_MCP_DATABASE_URL"
            )
    return value


@pytest.fixture
def test_database_url() -> str:
    try:
        return _validated_test_database_url(os.environ)
    except ValueError as exc:
        pytest.fail(str(exc))


@pytest.fixture
def postgres_connection(test_database_url, monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_DATABASE_URL", test_database_url)
    from garmin_mcp import db

    conn = db.connect(test_database_url)
    conn.execute(
        "TRUNCATE TABLE demo_info, raw_snapshots, sync_state, activities, training_status, "
        "hrv, sleep, daily_wellness RESTART IDENTITY CASCADE"
    )
    try:
        yield conn
    finally:
        conn.close()
