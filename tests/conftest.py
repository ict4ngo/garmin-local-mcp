"""Shared fixtures: every database test runs against a disposable PostgreSQL
database, created once per test session and dropped afterwards.

Requires a reachable PostgreSQL server. Point GARMIN_MCP_TEST_ADMIN_DSN at the
maintenance database (default: the dev server from compose.dev.yml).
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from garmin_mcp import db

ADMIN_DSN = os.environ.get(
    "GARMIN_MCP_TEST_ADMIN_DSN", "postgresql://garmin:garmin@localhost:5432/postgres"
)


@pytest.fixture(scope="session")
def db_url():
    """One dedicated test database per pytest session."""
    name = f"garmin_mcp_test_{uuid.uuid4().hex[:8]}"
    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    except psycopg.OperationalError as e:
        pytest.exit(
            "PostgreSQL is not reachable for tests "
            f"({ADMIN_DSN.split('@')[-1]}).\n"
            "Start the dev database first:  docker compose -f compose.dev.yml up -d\n"
            f"Original error: {e}",
            returncode=2,
        )
    admin.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    yield ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


@pytest.fixture
def conn(db_url):
    """A connection to a completely fresh schema (drop + migrate)."""
    conn = db.connect(db_url)
    db.drop_all(conn)
    db.migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def fresh_db(db_url):
    """Reset the schema without keeping a connection (for CLI tests)."""
    conn = db.connect(db_url)
    db.drop_all(conn)
    db.migrate(conn)
    conn.close()
    return db_url
