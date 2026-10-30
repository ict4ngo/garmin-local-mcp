from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"


def test_readme_matches_current_tools_storage_and_write_scope():
    text = " ".join(README.read_text().split())

    assert "## The 14 tools" in text
    assert "`query_samples`" in text
    assert "`compare_time_windows`" in text
    assert "separate persistent app-data volume" in text
    assert "write to the PostgreSQL warehouse" in text
    assert "raw/token app-data volume" in text
    assert "The worker does not receive `GARMIN_MCP_HTTP_BEARER_TOKEN`" in text


def test_readme_documents_safe_migration_and_compose_settings():
    text = " ".join(README.read_text().split())

    assert "--source-stopped" in text
    assert "WAL/SHM/journal sidecars" in text
    assert "same source fingerprint" in text
    assert "GARMIN_MCP_LAN_IP" in text
    assert "GARMIN_MCP_HTTP_PORT" in text
    assert "edit `GARMIN_MCP_SYNC_TIMES` in `.env`" in text
    assert "2.0 seconds" in text
    assert (
        "`GARMIN_MCP_TIMEZONE` | Required IANA timezone for local dates and scheduling; "
        "there is no system-timezone fallback"
    ) in text
