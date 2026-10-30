# Task 2 report — safe SQLite migration and FIT source retention

## Result

Implemented one-way legacy SQLite import into PostgreSQL and persistent, safe FIT input archival. The migration reads a byte-for-byte temporary snapshot through stdlib `sqlite3` in read-only mode, copies all eight current warehouse tables in one PostgreSQL transaction, and returns per-table source/destination row counts and date coverage. It leaves the original SQLite database and any WAL/SHM/journal files unchanged. The CLI exposes `migrate-sqlite --from PATH`.

FIT bundles are archived before decoding at `raw/fit/<date>/<bundle-id>/`, with deterministic content-derived IDs. Undated input is staged in the persistent raw volume before decoding, then placed at its decoded date. Imports decode only the managed copy, retain API-over-FIT precedence, and reparse handles managed FIT archives. Raw iterators skip symlinked paths and files.

## TDD evidence

- **RED:** Added migration coverage for every current table, including FIT-sourced rows, repeat migration, target transaction failure, and unchanged SQLite/WAL/SHM bytes. The required initial command, `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test ./.venv/bin/uv run pytest tests/test_sqlite_import.py -q`, failed during collection with `ImportError` because `garmin_mcp.sqlite_import` had not yet been implemented.
- **GREEN:** Added the importer archive-once and API-precedence tests plus FIT iterator symlink coverage. Final focused command `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test ./.venv/bin/pytest tests/test_sqlite_import.py tests/test_importer.py tests/test_raw.py -q` passed: **11 passed**.
- A WAL/SHM test initially caught SQLite changing shared-memory bytes while opening the source read-only. The implementation was adjusted to use read-only SQLite against a temporary copy of the database and sidecars; source-byte checks then passed.

## Verification

- `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test ./.venv/bin/pytest -q` — **134 passed, 1 skipped**.
- `./.venv/bin/ruff check .` — passed.
- `./.venv/bin/uv lock --check` — passed.
- `git diff --check` — passed.
- Pytest emitted one existing `PytestRemovedIn10Warning` from the installed AnyIO pytest plugin; no test failures resulted.

## Self-review

- The migration allowlists fixed table names and conflict keys, handles the keyless `demo_info` table idempotently, and streams source rows while keeping all PostgreSQL changes inside a transaction.
- FIT inputs are copied with no-follow/regular-file checks; archives are not moved or deleted from the source. Reparse does not traverse symlinked raw trees.
- The Compose data volume's existing raw archive was not modified.
- No known concerns remain.

## Commit

`feat: migrate legacy SQLite data safely`

## Fix round 1 — redact SQLite migration errors

### Change

The migration CLI no longer prints exception text. On failure it emits a fixed actionable message asking the user to verify the source file and PostgreSQL connection settings, and states that details are omitted to protect credentials. Added a CLI test that injects a `RuntimeError` containing a recognizable PostgreSQL URL/password and asserts the generic failure is useful while the URL and password are absent. SQLite source handling and the PostgreSQL transaction code were unchanged.

### TDD evidence

- **RED:** `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test ./.venv/bin/pytest tests/test_sqlite_import.py::test_migrate_sqlite_cli_redacts_exception_details -q` — **1 failed**. The existing CLI output included `connection failed for postgresql://garmin:recognizable-password@db.example/warehouse`; the actionable-message assertion failed, demonstrating the exception leak.
- **GREEN focused:** `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test ./.venv/bin/pytest tests/test_sqlite_import.py tests/test_importer.py tests/test_raw.py -q` — **12 passed**.
- **GREEN full:** `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test ./.venv/bin/pytest -q` — **135 passed, 1 skipped**.
- `./.venv/bin/ruff check .` — passed.
- `./.venv/bin/uv lock --check` — passed.
- `git diff --check` — passed.
- Pytest emitted the existing AnyIO `PytestRemovedIn10Warning`; no test failures resulted.

### Commit

`fix: redact SQLite migration errors`
