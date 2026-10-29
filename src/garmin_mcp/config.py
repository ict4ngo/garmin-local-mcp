"""Configuration loading.

Precedence: environment variables > config.toml in the data dir > defaults.
The worker schedule and timezone are environment-only and have no defaults.
The data dir itself resolves as GARMIN_MCP_DATA_DIR > default ~/.garmin-mcp.
No path in this project is ever hardcoded to a machine-specific location.
"""

from __future__ import annotations

import math
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DATA_DIR = Path.home() / ".garmin-mcp"


def _resolve_path(raw: str | os.PathLike) -> Path | None:
    """Resolve a configured path that may contain host template variables.

    MCP hosts are supposed to expand ``${HOME}``-style variables in extension
    config before launching the server, but some pass them through literally
    (Claude Desktop on Windows, where HOME is also typically unset — the server
    then tries to mkdir a directory literally named ``${HOME}`` and fails with
    Access is denied). Returns None for empty values or values that still
    contain an unexpanded ``${...}`` placeholder, so callers fall back to the
    default instead.
    """
    text = os.fspath(raw).strip()
    if not text:
        return None
    text = text.replace("${HOME}", str(Path.home()))
    text = os.path.expandvars(text)
    if "${" in text:
        return None
    return Path(text).expanduser()


def _resolve_path_or_default(raw: str | os.PathLike, source: str) -> Path:
    resolved = _resolve_path(raw)
    if resolved is None:
        # stderr only: stdout is the MCP transport.
        print(
            f"garmin-mcp: ignoring {source}={os.fspath(raw)!r} "
            f"(empty or unexpanded template variable); using {DEFAULT_DATA_DIR}",
            file=sys.stderr,
        )
        return DEFAULT_DATA_DIR
    return resolved

VALID_UNITS = ("metric", "statute")


@dataclass(frozen=True)
class Config:
    data_dir: Path
    database_url: str
    timezone: str
    sync_times: tuple[str, ...]
    units: str
    request_delay_seconds: float
    baseline_window_days: int
    backfill_start_date: str | None
    backfill_enabled: bool
    backfill_empty_streak_days: int

    def __post_init__(self) -> None:
        if not self.sync_times or any(
            not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)
            for value in self.sync_times
        ):
            raise ValueError("sync_times must contain one or more local HH:MM values")
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError(
                f"timezone must be a valid IANA timezone, got {self.timezone!r}"
            ) from exc
        if not math.isfinite(self.request_delay_seconds) or self.request_delay_seconds < 0:
            raise ValueError("request_delay_seconds must be a finite number >= 0")
        if self.backfill_empty_streak_days < 1:
            raise ValueError("backfill_empty_streak_days must be a positive integer")
        if self.backfill_start_date is not None:
            try:
                parsed = date.fromisoformat(self.backfill_start_date)
            except ValueError as exc:
                raise ValueError("backfill_start_date must use YYYY-MM-DD") from exc
            if parsed.isoformat() != self.backfill_start_date:
                raise ValueError("backfill_start_date must use YYYY-MM-DD")

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.toml"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def tokens_dir(self) -> Path:
        env = os.environ.get("GARMINTOKENS")
        if env:
            resolved = _resolve_path(env)
            if resolved is not None:
                return resolved
        return self.data_dir / "tokens"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(
            f"{name} is required; configure it in the worker's Docker Compose environment"
        )
    return value


def http_bearer_token() -> str:
    """Return the manually provisioned HTTP token without exposing its value."""
    value = os.environ.get("GARMIN_MCP_HTTP_BEARER_TOKEN", "").strip()
    if not value:
        raise ValueError("GARMIN_MCP_HTTP_BEARER_TOKEN is required for HTTP serving")
    return value


def _schedule() -> tuple[str, ...]:
    raw = _required_env("GARMIN_MCP_SYNC_TIMES")
    values = [item.strip() for item in raw.split(",")]
    if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item) for item in values):
        raise ValueError(
            "GARMIN_MCP_SYNC_TIMES must be a comma-separated list of local HH:MM values"
        )
    return tuple(dict.fromkeys(values))


def _timezone() -> str:
    value = _required_env("GARMIN_MCP_TIMEZONE")
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(
            f"GARMIN_MCP_TIMEZONE must be a valid IANA timezone name, got {value!r}"
        ) from exc
    return value


def _backfill_from() -> str | None:
    value = os.environ.get("GARMIN_MCP_BACKFILL_FROM", "").strip()
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("GARMIN_MCP_BACKFILL_FROM must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("GARMIN_MCP_BACKFILL_FROM must use YYYY-MM-DD")
    return value


def _backfill_enabled() -> bool:
    value = os.environ.get("GARMIN_MCP_BACKFILL_ENABLED", "true").strip().lower()
    if value not in ("true", "false"):
        raise ValueError("GARMIN_MCP_BACKFILL_ENABLED must be 'true' or 'false'")
    return value == "true"


def load(data_dir: str | os.PathLike | None = None) -> Config:
    """Load config. `data_dir` argument (e.g. from --data-dir) beats the env var."""
    if data_dir is not None:
        base = _resolve_path_or_default(data_dir, "--data-dir")
    else:
        env = os.environ.get("GARMIN_MCP_DATA_DIR")
        base = _resolve_path_or_default(env, "GARMIN_MCP_DATA_DIR") if env else DEFAULT_DATA_DIR

    file_cfg = _read_toml(base / "config.toml")

    database_url = os.environ.get("GARMIN_MCP_DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("GARMIN_MCP_DATABASE_URL is required to connect to PostgreSQL")

    units = str(file_cfg.get("units", "metric")).lower()
    if units not in VALID_UNITS:
        raise ValueError(f"config units must be one of {VALID_UNITS}, got {units!r}")

    try:
        request_delay = float(
            os.environ.get(
                "GARMIN_MCP_REQUEST_DELAY_SECONDS",
                file_cfg.get("request_delay_seconds", 2.0),
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("GARMIN_MCP_REQUEST_DELAY_SECONDS must be a number >= 0") from exc
    if request_delay < 0:
        raise ValueError("GARMIN_MCP_REQUEST_DELAY_SECONDS must be a number >= 0")

    empty_streak_days = int(file_cfg.get("backfill_empty_streak_days", 90))
    if empty_streak_days < 1:
        raise ValueError("backfill_empty_streak_days must be a positive integer")

    return Config(
        data_dir=base,
        database_url=database_url,
        timezone=_timezone(),
        sync_times=_schedule(),
        units=units,
        request_delay_seconds=request_delay,
        baseline_window_days=int(file_cfg.get("baseline_window_days", 28)),
        backfill_start_date=_backfill_from(),
        backfill_enabled=_backfill_enabled(),
        backfill_empty_streak_days=empty_streak_days,
    )
