from pathlib import Path

import pytest

from garmin_mcp.config import DEFAULT_DATA_DIR, load


@pytest.fixture(autouse=True)
def database_url(monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_DATABASE_URL", "postgresql://garmin:garmin@localhost/garmin")
    monkeypatch.setenv("GARMIN_MCP_SYNC_TIMES", "08:00,18:00")
    monkeypatch.setenv("GARMIN_MCP_TIMEZONE", "America/New_York")


def test_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMIN_MCP_DATA_DIR", raising=False)
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    cfg = load(tmp_path)
    assert cfg.data_dir == tmp_path
    assert cfg.units == "metric"
    assert cfg.request_delay_seconds == 2.0
    assert cfg.baseline_window_days == 28
    assert cfg.timezone == "America/New_York"
    assert cfg.sync_times == ("08:00", "18:00")
    assert cfg.database_url == "postgresql://garmin:garmin@localhost/garmin"
    assert cfg.raw_dir == tmp_path / "raw"
    assert cfg.tokens_dir == tmp_path / "tokens"


def test_env_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_DATA_DIR", str(tmp_path / "elsewhere"))
    cfg = load()
    assert cfg.data_dir == tmp_path / "elsewhere"


def test_default_data_dir_is_home():
    assert DEFAULT_DATA_DIR == Path.home() / ".garmin-mcp"


def test_env_data_dir_literal_home_template(monkeypatch):
    # Some MCP hosts pass manifest template variables through unexpanded
    # (Claude Desktop on Windows): expand ${HOME} ourselves.
    monkeypatch.setenv("GARMIN_MCP_DATA_DIR", "${HOME}/.garmin-mcp")
    cfg = load()
    assert cfg.data_dir == Path.home() / ".garmin-mcp"


def test_env_data_dir_unknown_template_falls_back(monkeypatch):
    monkeypatch.delenv("SOME_UNSET_VAR_XYZ", raising=False)
    monkeypatch.setenv("GARMIN_MCP_DATA_DIR", "${SOME_UNSET_VAR_XYZ}/data")
    cfg = load()
    assert cfg.data_dir == DEFAULT_DATA_DIR


def test_env_data_dir_empty_falls_back(monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_DATA_DIR", "  ")
    cfg = load()
    assert cfg.data_dir == DEFAULT_DATA_DIR


def test_tokens_env_literal_template_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMINTOKENS", "${HOME_UNSET_XYZ}/tokens")
    cfg = load(tmp_path)
    assert cfg.tokens_dir == tmp_path / "tokens"


def test_config_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    (tmp_path / "config.toml").write_text(
        'timezone = "UTC"\nunits = "statute"\n'
        "request_delay_seconds = 2.5\nbaseline_window_days = 14\n"
    )
    cfg = load(tmp_path)
    assert cfg.timezone == "America/New_York"
    assert cfg.units == "statute"
    assert cfg.request_delay_seconds == 2.5
    assert cfg.baseline_window_days == 14


def test_schedule_and_timezone_are_required_environment_values(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMIN_MCP_SYNC_TIMES")
    monkeypatch.setenv("GARMIN_MCP_TIMEZONE", "America/New_York")
    with pytest.raises(ValueError, match="GARMIN_MCP_SYNC_TIMES"):
        load(tmp_path)


def test_schedule_and_timezone_are_parsed_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_SYNC_TIMES", "06:15,20:45")
    monkeypatch.setenv("GARMIN_MCP_TIMEZONE", "Europe/Paris")
    monkeypatch.setenv("GARMIN_MCP_REQUEST_DELAY_SECONDS", "3.5")
    monkeypatch.setenv("GARMIN_MCP_BACKFILL_FROM", "2020-01-02")
    monkeypatch.setenv("GARMIN_MCP_BACKFILL_ENABLED", "false")

    cfg = load(tmp_path)

    assert cfg.sync_times == ("06:15", "20:45")
    assert cfg.timezone == "Europe/Paris"
    assert cfg.request_delay_seconds == 3.5
    assert cfg.backfill_start_date == "2020-01-02"
    assert cfg.backfill_enabled is False


@pytest.mark.parametrize(
    ("schedule", "timezone", "message"),
    [("25:00", "UTC", "HH:MM"), ("08:00", "Not/AZone", "IANA")],
)
def test_invalid_schedule_or_timezone_fails_actionably(
    tmp_path, monkeypatch, schedule, timezone, message
):
    monkeypatch.setenv("GARMIN_MCP_SYNC_TIMES", schedule)
    monkeypatch.setenv("GARMIN_MCP_TIMEZONE", timezone)
    with pytest.raises(ValueError, match=message):
        load(tmp_path)


def test_tokens_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMINTOKENS", str(tmp_path / "tok"))
    cfg = load(tmp_path)
    assert cfg.tokens_dir == tmp_path / "tok"


def test_database_url_is_required(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMIN_MCP_DATABASE_URL")
    with pytest.raises(ValueError, match="GARMIN_MCP_DATABASE_URL is required"):
        load(tmp_path)
