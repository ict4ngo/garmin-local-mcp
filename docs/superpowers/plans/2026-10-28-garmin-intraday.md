# Garmin Intraday MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect Garmin’s available timestamped health/training samples into a persistent PostgreSQL-backed homelab service and make them queryable through an authenticated MCP endpoint.

**Architecture:** Docker Compose runs a PostgreSQL service, a Streamable HTTP MCP service, and a separate scheduled sync worker. PostgreSQL is the query index; lossless Garmin API/FIT source files and credentials live in a separate persistent app-data volume. A resumable low-rate historical backfill shares the sync engine and yields to the twice-daily recent-data schedule.

**Tech Stack:** Python 3.12+, `psycopg` 3, PostgreSQL 17, MCP Python SDK v1/FastMCP, Starlette + Uvicorn for bearer-protected Streamable HTTP, `python-garminconnect`, Docker Compose, pytest, Ruff, uv.

**Spec:** `docs/superpowers/specs/2026-10-28-garmin-intraday-design.md`

## Global Constraints

- Python requirement remains `>=3.12`.
- Keep the MCP SDK on v1; use `mcp>=1.30,<2` for the tested Streamable HTTP app API.
- Raise `garminconnect` to `>=0.3.19,<0.4` by Task 3; upstream added `get_daily_movement()` in 0.3.19.
- Use plain PostgreSQL; do not add TimescaleDB unless measurements later justify it.
- Keep PostgreSQL private to Compose; expose only the bearer-protected MCP HTTP endpoint. Bind its host port to a configurable IP, defaulting to loopback; users set the homelab's private LAN IP in `.env` for remote clients.
- Keep MCP SDK DNS-rebinding/Host validation enabled; allow only loopback and the configured private LAN IP plus published port.
- Keep PostgreSQL data and the raw Garmin/token app data in separate persistent volumes.
- Supply schedule and IANA timezone through Docker Compose/environment; the worker code must not contain a fallback schedule. Compose defaults to `06:00,18:00` and both values are editable through `.env` without rebuilding the image.
- Automatic historical backfill is resumable, at most one Garmin request is in flight, waits 2 seconds between requests by default, and backs off exponentially after rate limits.
- Never invent/interpolate finer-grained samples; preserve raw source payloads and timestamp/time-zone meaning.
- Preserve existing daily MCP tools and SQLite data through a non-destructive one-time migration. Stop old SQLite writers and explicitly confirm this before migration; require an empty PostgreSQL target, make same-source retry a no-op, and never overwrite newer PostgreSQL rows.
- Never bake secrets into the image, use weak token/password defaults, or log Garmin/database/MCP credentials.
- PostgreSQL-backed tests require a dedicated `GARMIN_MCP_TEST_DATABASE_URL`; never point tests or migration fixtures at a real Garmin warehouse.

## Review Focus

- Garmin changes sample array shape or timestamp encoding: pin fixture tests for pair/object arrays, missing samples, epoch milliseconds, and timezone retention in Task 4.
- A current-day refresh corrects or omits points: pin deduplication/latest-value and immutable-snapshot behavior in Task 3.
- Backfill is interrupted or receives 429 while a scheduled sync is due: pin persisted cursor, delay/backoff, and schedule priority in Task 5.
- SQLite migration fails midway or contains FIT-only summaries: pin rollback/source preservation and all-table row-count parity in Task 2.
- HTTP auth is absent/wrong or a valid client initializes Streamable HTTP: pin 401s, valid MCP initialize/tool call, and stdio compatibility in Task 6.

---

## File Structure

- `src/garmin_mcp/db.py` — PostgreSQL schema/versioning, connection factory, generic upserts, sync-state operations, and PostgreSQL advisory locks.
- `src/garmin_mcp/config.py` — PostgreSQL URL, raw-data paths, backfill/schedule values, HTTP bind settings, and bearer token loading.
- `src/garmin_mcp/sqlite_import.py` — read-only legacy SQLite inspection and non-destructive migration into PostgreSQL.
- `src/garmin_mcp/endpoints.py` — Garmin fetch methods and pure parsers for daily and timestamped payloads.
- `src/garmin_mcp/raw.py` — immutable versioned API/activity snapshots and retained FIT bundle archive.
- `src/garmin_mcp/samples.py` — normalized sample row types, validation, and upsert identity.
- `src/garmin_mcp/timeseries.py` — sample range query and caller-defined time-window comparisons.
- `src/garmin_mcp/worker.py` — independent scheduled sync/backfill process and persistent progress handling.
- `src/garmin_mcp/http_auth.py` — static bearer-token ASGI middleware for the remote MCP app; no OAuth issuer is advertised for the manually provisioned token.
- `src/garmin_mcp/server.py`, `src/garmin_mcp/cli.py` — MCP tools and CLI/HTTP/worker/migration entry points.
- `tests/conftest.py` — isolated PostgreSQL test database fixture, configured by `GARMIN_MCP_TEST_DATABASE_URL`.
- `compose.yaml`, `Dockerfile`, `.github/workflows/ci.yml` — homelab services, persistence, runtime commands, and PostgreSQL CI service.
- `README.md`, `manifest.json`, `server.json` — deployment, migration, client, and current package/transport documentation.

## Tasks

### Task 1: PostgreSQL runtime backend

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Modify: `src/garmin_mcp/config.py`, `src/garmin_mcp/db.py`
- Modify: `src/garmin_mcp/analysis.py`, `src/garmin_mcp/raw.py`, `src/garmin_mcp/sync.py`, `src/garmin_mcp/importer.py`, `src/garmin_mcp/demo.py`, `src/garmin_mcp/server.py`, `src/garmin_mcp/cli.py`
- Modify: `tests/test_db.py`, `tests/test_config.py`, `tests/test_analysis.py`, `tests/test_raw.py`, `tests/test_sync.py`, `tests/test_importer.py`, `tests/test_demo.py`, `tests/test_server.py`
- Create: `tests/conftest.py`, `tests/test_postgres.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces `Config.database_url: str` from required `GARMIN_MCP_DATABASE_URL`; raw files/tokens remain under `GARMIN_MCP_DATA_DIR`.
- Produces `db.connect(database_url: str) -> psycopg.Connection` with mapping rows, PostgreSQL migrations, and existing `db.upsert`, `db.upsert_partial`, `db.mark_sync`, and `db.synced_ok` behavior.
- All runtime query functions continue receiving a connection and returning the same JSON-able results as before.

- [ ] **Step 1: Add PostgreSQL integration tests** named `test_schema_created`, `test_migrate_idempotent`, `test_upsert_partial_preserves_existing_columns`, `test_sync_state_retry_count`, `test_transaction_rolls_back`, `test_real_values_preserve_double_precision`, and `test_test_database_url_is_safely_isolated`. Require `GARMIN_MCP_TEST_DATABASE_URL`; fail closed unless its database name ends in `_test`, and reject it if it matches a pre-existing `GARMIN_MCP_DATABASE_URL` before test fixtures override that setting. Truncate only the guarded test database between tests.
- [ ] **Step 2: Run the new tests against PostgreSQL 17 and confirm failure.** Start a disposable local test service with `docker run --rm -d --name garmin-test-pg -e POSTGRES_USER=garmin -e POSTGRES_PASSWORD=garmin -e POSTGRES_DB=garmin_test -p 127.0.0.1:55432:5432 postgres:17`, then run `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test uv run pytest tests/test_postgres.py -q`. Expected: FAIL because the PostgreSQL connection/schema interface does not exist yet; remove the test container after the test cycle.
- [ ] **Step 3: Implement the PostgreSQL store.** Add `psycopg[binary]>=3.2,<4`, set dependency floors to `mcp>=1.30,<2` and `garminconnect>=0.3.7,<0.4` (Task 3 raises the Garmin floor for its complete method set), add `Config.database_url`, a PostgreSQL schema/migration table, `dict_row` connections, `%s` parameters, and the existing generic DB helpers. Adapt runtime SQL and row handling in the listed modules, including `demo.is_demo` (replace `sqlite_master` use) and positional scalar reads. Use `DOUBLE PRECISION` for existing SQLite `REAL` columns so daily numeric values retain 64-bit precision. Preserve existing table names, row semantics, and daily tool responses.
- [ ] **Step 4: Port the current suite and CI.** Run database-backed tests against the PostgreSQL fixture; add a PostgreSQL 17 service to the Ubuntu CI job. Keep Windows coverage for database-independent parser/config/auth tests rather than silently skipping the full database suite.
- [ ] **Step 5: Verify backend compatibility.** Run: `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test uv run pytest -q`. Expected: all PostgreSQL-backed tests pass, including daily analytics, sync, demo safety, and MCP tool tests. Run `uv run ruff check .` and `uv lock --check`.
- [ ] **Step 6: Commit** as `feat: move warehouse to PostgreSQL`.

### Task 2: Safe SQLite migration and FIT source retention

**Files:**
- Create: `src/garmin_mcp/sqlite_import.py`
- Modify: `src/garmin_mcp/cli.py`, `src/garmin_mcp/importer.py`, `src/garmin_mcp/raw.py`, `src/garmin_mcp/db.py`
- Test: `tests/test_sqlite_import.py`, `tests/test_importer.py`, `tests/test_raw.py`

**Interfaces:**
- Produces `sqlite_import.migrate_sqlite(source: Path, database_url: str, *, source_stopped: bool = False) -> dict` with source/destination row counts and coverage. It rejects `source_stopped=False`, imports only into an empty PostgreSQL warehouse, and uses a source fingerprint/migration record so an identical retry is a no-op rather than a conflict overwrite.
- Produces CLI command `garmin-local-mcp migrate-sqlite --from PATH --source-stopped`; it never deletes or modifies the source SQLite file and refuses to run without the explicit quiescence confirmation.
- Treat `demo_info` as optional in legacy SQLite files; a normal non-demo warehouse need not contain it.
- `importer.import_bundle(conn, folder, force=False)` continues returning the current report and additionally archives original FIT inputs in the persistent raw volume before decoding.

- [ ] **Step 1: Write migration tests** named `test_sqlite_migration_copies_every_table`, `test_sqlite_migration_allows_missing_demo_info`, `test_sqlite_migration_is_idempotent_without_overwriting_newer_rows`, `test_sqlite_migration_refuses_nonempty_target`, `test_sqlite_migration_requires_stopped_source`, and `test_sqlite_migration_failure_keeps_source_unchanged`. Populate every required legacy table, including `source='fit'`; assert optional `demo_info`, destination parity, unchanged source bytes/sidecars, and no overwrite on retry.
- [ ] **Step 2: Run `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test uv run pytest tests/test_sqlite_import.py -q` and confirm failure** because the migrator is not implemented.
- [ ] **Step 3: Implement transactional migration** using Python `sqlite3` only as a read-only source and psycopg for PostgreSQL. Require `--source-stopped` and document that every process writing the old SQLite warehouse must be stopped before copying the database and WAL/SHM sidecars. Treat `demo_info` as optional. Refuse a non-empty target unless a stored source fingerprint proves this exact import already completed; same-source retries return the recorded result without re-upserting. Store the fingerprint/report marker transactionally, import the existing daily/activity/sync/raw/demo rows, and rollback the target transaction on any error. Keep the source database and its WAL/SHM files untouched.
- [ ] **Step 4: Write FIT archive tests** named `test_import_archives_fit_source_once` and `test_api_rows_not_overwritten`; assert repeated imports retain one durable copy and preserve current API-over-FIT precedence.
- [ ] **Step 5: Implement FIT archival** under `<data_dir>/raw/fit/<date>/<bundle-id>/` before decoding; teach raw iteration/reparse to recognize the archive without following arbitrary external paths.
- [ ] **Step 6: Verify** with `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test uv run pytest tests/test_sqlite_import.py tests/test_importer.py tests/test_raw.py -q`. Expected: migration preserves every table and FIT source files are retained. Commit as `feat: migrate legacy SQLite data safely`.

### Task 3: Timestamped Garmin ingestion and lossless snapshots

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Modify: `src/garmin_mcp/db.py`, `src/garmin_mcp/endpoints.py`, `src/garmin_mcp/raw.py`, `src/garmin_mcp/sync.py`
- Create: `src/garmin_mcp/samples.py`
- Test: `tests/test_endpoints.py`, `tests/test_raw.py`, `tests/test_sync.py`, `tests/test_samples.py`
- Create: fixtures under `tests/fixtures/`

**Interfaces:**
- Produces `Sample(source_endpoint: str, source_id: str, metric: str, observed_at: datetime, source_date: str, sample_index: int, value: float, unit: str | None, local_timestamp: str | None)`.
- Produces `samples.upsert_samples(conn, samples: Sequence[Sample], fetched_at: datetime) -> int`.
- Adds date-scoped fetch/parse support for stress, Body Battery, heart-rate, SpO2, respiration, daily movement, and training-readiness payloads where timestamped samples are present; parses available sleep arrays; and fetches/decodes activity details with `garminconnect.parse_activity_detail_metrics()`.

- [ ] **Step 1: Add fixture-backed parser/storage/sync tests** named `test_parse_stress_samples`, `test_parse_body_battery_samples`, `test_parse_sleep_samples`, `test_parse_activity_detail_samples`, `test_sample_upsert_is_idempotent`, `test_snapshot_refresh_keeps_prior_version`, `test_current_day_sample_refresh_is_idempotent`, and `test_activity_detail_fetch_is_not_repeated`. Assert exact timestamps/values/units, missing-array tolerance, and no generated/interpolated points.
- [ ] **Step 2: Run targeted parser tests and confirm failure**: `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test uv run pytest tests/test_endpoints.py tests/test_samples.py -q`.
- [ ] **Step 3: Implement normalized samples.** Add `samples` schema with `TIMESTAMPTZ` UTC instants, original local timestamp/date, unit/source identity, `sample_index`, and a unique identity on `(source_endpoint, source_id, metric, observed_at, sample_index)`. Add an index beginning with `observed_at`; corrected same-identity observations update the indexed value while raw snapshot versions remain immutable. Add `activity_detail_state` keyed by `activity_id` so stable activity streams are not fetched on every daily listing.
- [ ] **Step 4: Add Garmin parsers/fetchers** for `get_stress_data(date)`, `get_body_battery(date, date)`, `get_heart_rates(date)`, `get_spo2_data(date)`, `get_respiration_data(date)`, `get_daily_movement(date)`, `get_training_readiness(date)` when timestamped data is present, sleep-detail arrays, `get_activity_details(activity_id)`, and `parse_activity_detail_metrics`. Raise the floor to `garminconnect>=0.3.19,<0.4`; tagged upstream docs/releases establish that `get_daily_movement()` arrived in 0.3.19, while the activity detail parser existed earlier. Update `uv.lock`. Only normalize shapes verified by fixtures; preserve unknown fields in raw payloads.
- [ ] **Step 5: Version refreshed raw payloads** rather than deleting earlier same-day snapshots. Keep legacy snapshot paths readable, index every version in `raw_snapshots`, and make `reparse` choose the latest snapshot per endpoint/date for materialized rows while retaining all versions on disk. Test with `test_snapshot_refresh_keeps_prior_version`.
- [ ] **Step 6: Extend sync registration** so current-day stress/Body Battery/sleep samples refresh on repeated calls and activity samples are fetched once per activity detail version tracked in PostgreSQL. Make re-runs idempotent and keep daily summary ingestion unchanged; pin with `test_current_day_sample_refresh_is_idempotent` and `test_activity_detail_fetch_is_not_repeated`.
- [ ] **Step 7: Verify**: `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test uv run pytest tests/test_endpoints.py tests/test_samples.py tests/test_raw.py tests/test_sync.py -q`. Expected: exact native samples persist once, refreshed raw versions remain available, and daily rows retain current semantics. Commit as `feat: archive Garmin timestamped samples`.

### Task 4: Arbitrary time-series queries and window comparisons

**Files:**
- Create: `src/garmin_mcp/timeseries.py`
- Modify: `src/garmin_mcp/metrics.py`, `src/garmin_mcp/server.py`, `src/garmin_mcp/demo.py`, `src/garmin_mcp/cli.py`
- Test: `tests/test_timeseries.py`, `tests/test_server.py`, `tests/test_demo.py`

**Interfaces:**
- `timeseries.query_samples(conn, metrics: list[str], start: datetime, end: datetime, timezone: str, limit: int = 1000, offset: int = 0) -> dict` returns `[timestamp, metric, value, unit]` rows, total/truncated metadata, and uses the half-open interval `[start, end)`.
- `timeseries.compare_time_windows(conn, metric: str, start_date: date, end_date: date, windows: list[TimeWindow], timezone: str) -> dict` reports per-window daily means, overall mean/median, valid-day count, sample count, and missing coverage.
- `TimeWindow` is a Pydantic model in `timeseries.py` with `name: str`, `start_time: str` (`HH:MM`), and `end_time: str` (`HH:MM`).
- Register MCP tools `query_samples` and `compare_time_windows`; existing daily `query_metrics` remains unchanged.

- [ ] **Step 1: Write DB/MCP/demo-safety tests** named `test_query_samples_uses_half_open_range`, `test_query_samples_local_time_handles_dst`, `test_compare_windows_midday_vs_evening`, `test_compare_windows_reports_missing_days`, `test_query_samples_caps_and_paginates`, `test_demo_refuses_sample_only_real_store`, and `test_demo_force_clears_sample_and_worker_state`. Assert exact query boundaries, timezone grouping, per-day means, sample counts, cap metadata, and that real sample-only data cannot be mixed with generated demo rows.
- [ ] **Step 2: Run `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test uv run pytest tests/test_timeseries.py tests/test_server.py -q` and confirm the new tests fail.**
- [ ] **Step 3: Implement** a registry of queryable sample metrics and the range query with a hard maximum of 5,000 rows. Require aware ISO timestamps or apply the configured IANA timezone to naive inputs; return timestamps in UTC plus the source-local timestamp when available.
- [ ] **Step 4: Implement window comparisons** using caller-named `TimeWindow(name, start_time, end_time)` values. Compute each day’s mean within each window, then compare daily means so days with denser sampling do not dominate; include counts and mark incomplete coverage. Support windows crossing midnight.
- [ ] **Step 5: Register tools** with FastMCP and preserve current `query_metrics` schemas. Verify compact summaries and bounded raw responses.
- [ ] **Step 6: Extend demo data-safety checks.** Include `samples`, `activity_detail_state`, `backfill_state`, and `worker_state` when deciding whether a store contains real data and when clearing a demo store. A sample-only real warehouse must be refused without `--force`; a forced demo must not leave sample/worker rows behind. Keep SQLite migration markers intact. Verify with the two named `tests/test_demo.py` cases from Step 1.
- [ ] **Step 7: Verify**: `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test uv run pytest tests/test_timeseries.py tests/test_server.py tests/test_demo.py -q`. Expected: exact points and valid comparisons are returned without treating absent samples as zero, and demo data never coexists with real sample/worker rows. Commit as `feat: query Garmin samples by time`.

### Task 5: Resumable throttled backfill and scheduled worker

**Files:**
- Create: `src/garmin_mcp/worker.py`
- Modify: `src/garmin_mcp/config.py`, `src/garmin_mcp/db.py`, `src/garmin_mcp/sync.py`, `src/garmin_mcp/server.py`, `src/garmin_mcp/cli.py`
- Test: `tests/test_worker.py`, `tests/test_sync.py`, `tests/test_server.py`

**Interfaces:**
- `worker.run_worker(config: Config, client_factory=auth.get_client, clock=...) -> int` runs independently of MCP clients.
- Config requires non-empty `sync_times` from `GARMIN_MCP_SYNC_TIMES` (no Python default), explicit IANA `timezone` from `GARMIN_MCP_TIMEZONE`, `request_delay_seconds` (default `2.0`), optional `backfill_start_date`, and `backfill_empty_streak_days` (default `90`).
- Environment overrides are `GARMIN_MCP_SYNC_TIMES`, `GARMIN_MCP_TIMEZONE`, `GARMIN_MCP_REQUEST_DELAY_SECONDS`, `GARMIN_MCP_BACKFILL_FROM`, and `GARMIN_MCP_BACKFILL_ENABLED`.
- CLI adds `garmin-local-mcp worker`; PostgreSQL stores resumable per-endpoint backfill cursor/status.

- [ ] **Step 1: Write deterministic worker/config tests** named `test_worker_runs_at_configured_times`, `test_worker_requires_configured_schedule`, `test_backfill_resumes_after_restart`, `test_backfill_rate_limit_preserves_cursor`, `test_due_recent_sync_preempts_backfill`, and `test_sync_advisory_lock_serializes_callers`, using a fake clock/client.
- [ ] **Step 2: Run `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test uv run pytest tests/test_worker.py -q` and confirm failure.**
- [ ] **Step 3: Implement persistent backfill progress.** Start at the newest date and walk backward per temporal endpoint; without a configured cutoff, stop an endpoint after 90 consecutive empty days. Persist cursor and empty streak; allow `GARMIN_MCP_BACKFILL_FROM` to set an explicit lower bound and `GARMIN_MCP_BACKFILL_ENABLED=false` to disable automatic history collection. Read schedule and timezone only from `GARMIN_MCP_SYNC_TIMES` and `GARMIN_MCP_TIMEZONE`; fail startup with an actionable error when missing/invalid instead of using a code-defined schedule.
- [ ] **Step 4: Implement throttling and mutual exclusion.** Use one worker/request at a time, a configurable 2-second inter-request delay, exponential backoff on 429, and a PostgreSQL session advisory lock shared by scheduled, backfill, and manual sync paths. Between backfill requests, run any due current/recent-day sync before continuing history.
- [ ] **Step 5: Implement timezone-aware schedule loop.** Coalesce missed occurrences into one recent catch-up after restart; refresh current day and yesterday’s granular endpoints on each scheduled pass, while retaining ordinary daily endpoint skip behavior.
- [ ] **Step 6: Extend `sync_status`** with backfill progress, last scheduled run, samples coverage, and errors without exposing credentials.
- [ ] **Step 7: Verify**: `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test uv run pytest tests/test_worker.py tests/test_sync.py tests/test_server.py -q`. Expected: restart resumes progress, 429 does not lose cursor, scheduled work takes priority, and no concurrent Garmin sync proceeds without the lock. Commit as `feat: schedule resumable Garmin syncs`.

### Task 6: Bearer-protected Streamable HTTP MCP service

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Create: `src/garmin_mcp/http_auth.py`
- Modify: `src/garmin_mcp/server.py`, `src/garmin_mcp/cli.py`, `src/garmin_mcp/config.py`
- Test: `tests/test_http_server.py`, `tests/test_server.py`

**Interfaces:**
- `http_auth.create_http_app(mcp_server: FastMCP, expected_token: str) -> Starlette` mounts the FastMCP `/mcp` ASGI app and requires `Authorization: Bearer <token>` on MCP requests.
- `server.serve_http(config: Config | None = None) -> int` serves that app with Uvicorn; CLI adds `serve-http --host/--port` overrides. Keep the CLI default on loopback for safety; Task 7's Compose command explicitly passes `--host 0.0.0.0 --port 8000` for LAN access.
- Static-token validation uses `hmac.compare_digest`; it does not advertise OAuth metadata or create tokens. Clients provide the token as a header.

- [ ] **Step 1: Write HTTP integration tests** named `test_http_requires_bearer_token`, `test_http_rejects_wrong_token`, `test_streamable_http_initialize_and_tool_call`, and `test_stdio_tools_remain_registered`. Send initialize/list-tools/tool-call requests and assert 401s or successful responses as appropriate.
- [ ] **Step 2: Run `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test uv run pytest tests/test_http_server.py -q` and confirm the new HTTP/auth tests fail.**
- [ ] **Step 3: Add direct `starlette>=1,<2` and `uvicorn>=0.52,<1` dependencies** compatible with the MCP v1 SDK pinned in Task 1. Build a stateless JSON Streamable HTTP app at `/mcp`, start the FastMCP session manager in the ASGI lifespan, and wrap it in Starlette bearer middleware. Require `GARMIN_MCP_HTTP_BEARER_TOKEN`; fail startup if absent/empty and never log it.
- [ ] **Step 4: Add `serve-http`** without changing the existing `serve` stdio default. Bind/port are configurable; expose no unauthenticated MCP route.
- [ ] **Step 5: Verify protocol and protection.** Use the MCP Python Streamable HTTP client with a valid header for the integration test; assert missing/wrong tokens return HTTP 401 and valid initialization/tool calls succeed. Task 7 documents client-specific headers.
- [ ] **Step 6: Verify**: `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test uv run pytest tests/test_http_server.py tests/test_server.py -q`. Expected: authenticated Streamable HTTP MCP works, missing/bad token is rejected, and stdio remains intact. Commit as `feat: serve authenticated MCP over HTTP`.

### Task 7: Compose deployment, backups, CI, and user docs

**Files:**
- Create: `compose.yaml`, `.env.example`
- Modify: `Dockerfile`, `.github/workflows/ci.yml`, `README.md`, `manifest.json`, `server.json`
- Test: `tests/test_deployment.py`

**Interfaces:**
- Compose services: `postgres` (`postgres:17`, health check, private network), `mcp` (`serve-http`), and `sync-worker` (`worker`).
- Persistent volumes: `postgres_data` for PostgreSQL data and `garmin_data` shared by app/worker for raw snapshots, FIT bundles, and Garmin tokens.
- Required runtime values: `GARMIN_MCP_DATABASE_URL`, database password, `GARMIN_MCP_HTTP_BEARER_TOKEN`, `GARMIN_MCP_TIMEZONE`, and `GARMIN_MCP_SYNC_TIMES`. Compose provides `GARMIN_MCP_SYNC_TIMES=06:00,18:00` as its editable default; the worker has no fallback schedule.
- Host publishing uses `GARMIN_MCP_LAN_IP` (safe Compose default `127.0.0.1`) and `GARMIN_MCP_HTTP_PORT` (default `8000`); users set the homelab's private LAN IP in `.env` for other machines to connect.

- [ ] **Step 1: Add deployment/config checks** named `test_compose_keeps_database_private`, `test_compose_publishes_authenticated_mcp_only`, `test_compose_shares_raw_volume_with_worker`, `test_compose_schedule_override_reaches_worker`, and `test_compose_lan_bind_and_port_are_configurable`; assert PostgreSQL is not published, MCP defaults to loopback, a configured private LAN IP and host port are forwarded, PostgreSQL has its own named volume, and changing schedule values changes the worker environment without rebuilding.
- [ ] **Step 2: Run `uv run pytest tests/test_deployment.py -q` and confirm failure** because Compose configuration is absent.
- [ ] **Step 3: Add Compose and container commands** for PostgreSQL, HTTP MCP, and worker. Run the MCP service as `garmin-local-mcp serve-http --host 0.0.0.0 --port 8000`; publish it only on `${GARMIN_MCP_LAN_IP:-127.0.0.1}` at `${GARMIN_MCP_HTTP_PORT:-8000}`. Add PostgreSQL health-gated startup, distinct required credentials, and no public DB listener. Keep CLI login/migrate commands runnable with `docker compose run --rm` using the same app-data volume.
- [ ] **Step 4: Update README and metadata** with bootstrapping/login, the stop-all-writers prerequisite for SQLite migration, migration's empty-target/idempotent behavior, optional `demo_info`, volume contents/backup and restore, throttled backfill behavior, Compose `.env` LAN-IP/port/schedule/timezone/token settings, HTTPS reverse-proxy caution, and Garmin unofficial API limits. Show how to edit `GARMIN_MCP_SYNC_TIMES` and `GARMIN_MCP_LAN_IP` without rebuilding. Add an OpenCode V2 remote config example with `oauth: false` and an Authorization header, plus a Hermes `url`/`headers` example (neither should start OAuth for the static token). Keep published stdio metadata truthful; document remote HTTP separately. Update the tool inventory, sample-query/comparison tools, write operations, and effective config defaults.
- [ ] **Step 5: Verify Compose and CI.** Run `docker compose config`, full tests against `GARMIN_MCP_TEST_DATABASE_URL` (use a temporary PostgreSQL test service published only on loopback when running from the host), then `docker compose up -d` for the deployment smoke test. CI must run all DB tests against PostgreSQL on Ubuntu and parser/config/auth tests on Windows; run Ruff and `uv lock --check`.
- [ ] **Step 6: Verify persistence** by writing a sample/raw snapshot, recreating app and PostgreSQL containers without removing volumes, and checking the sample and raw file remain available. Commit as `feat: add homelab Docker Compose deployment`.

### Task 8: Allow the configured LAN Host without weakening DNS protection

**Files:**
- Modify: `src/garmin_mcp/http_auth.py`, `src/garmin_mcp/server.py`, `src/garmin_mcp/config.py`
- Modify: `compose.yaml`, `.env.example`, `README.md`
- Test: `tests/test_http_server.py`, `tests/test_deployment.py`

**Interfaces:**
- `create_http_app(mcp_server, expected_token, allowed_hosts)` configures `TransportSecuritySettings` with DNS-rebinding protection enabled and explicit host/origin allowlists before creating the Streamable HTTP app.
- MCP runtime receives `GARMIN_MCP_LAN_IP` and `GARMIN_MCP_HTTP_PORT`; it allows that exact published Host plus loopback hosts, not arbitrary hosts.

- [ ] **Step 1: Add failing HTTP/Compose tests** named `test_http_accepts_configured_lan_host`, `test_http_rejects_unlisted_host`, and `test_compose_passes_host_allowlist_inputs_to_mcp_only`. Assert valid bearer + configured LAN Host reaches MCP, valid bearer + unknown Host returns 421, missing bearer remains 401, and the worker does not receive HTTP-only variables.
- [ ] **Step 2: Run the focused tests** against PostgreSQL/ASGI and confirm the LAN Host test fails with the SDK's current `Invalid Host header` response.
- [ ] **Step 3: Implement explicit allowed hosts.** Derive host and published port from validated runtime configuration; include exact `<GARMIN_MCP_LAN_IP>:<GARMIN_MCP_HTTP_PORT>` and loopback patterns. Keep DNS rebinding protection enabled, bearer authentication outside it, and avoid a wildcard host allowlist.
- [ ] **Step 4: Pass the LAN IP and published port only to the MCP service in Compose**, document that remote client URLs must use the configured LAN IP (not `localhost` or `0.0.0.0`), and retain the loopback default for private/local development.
- [ ] **Step 5: Verify**: focused HTTP/deployment tests, the full PostgreSQL test suite, Ruff, lock check, `docker compose config`, and an HTTP smoke request using configured LAN Host plus a valid bearer. Confirm unknown Host remains rejected. Commit as `fix: allow configured LAN host for MCP HTTP`.

## Spec Coverage Self-Check

- Native timestamped ingestion and raw retention: Task 3; source/FIT preservation and old SQLite rows: Task 2.
- Arbitrary timestamp queries and time-window comparisons: Task 4; existing daily analytics compatibility: Tasks 1 and 4.
- Twice-daily sync, throttled resumable history, restart behavior, and sync status: Task 5.
- PostgreSQL service, separate raw/database volumes, and no host runtime dependencies: Tasks 1 and 7.
- Authenticated remote Streamable HTTP for both clients plus retained stdio: Task 6; client configuration examples: Task 7.
- Authenticated HTTP Host allowlist/DNS-rebinding boundary: Task 8.
- Acceptance validation for parsers, query coverage, sync/backfill, migration, HTTP auth, Compose persistence, and docs is assigned to the corresponding task above.

## Execution Order and Handoff

Run Tasks 1–8 in order. Task 1 provides the database interface; Task 2 protects existing data; Tasks 3–5 build the temporal data path; Task 6 adds authenticated HTTP; Task 7 packages the deployment; Task 8 configures FastMCP's Host allowlist for the Compose-published LAN address without disabling DNS-rebinding protection. Keep existing daily tools and tests passing. After Task 8, run the complete CI commands, a Compose smoke test, and a final review against every acceptance item in the design spec.
