# Task 5 Report: Resumable throttled backfill and scheduled worker

## Implementation

- Added required environment-only schedule and IANA timezone configuration.
  `GARMIN_MCP_SYNC_TIMES` accepts local comma-separated `HH:MM` values;
  `GARMIN_MCP_TIMEZONE` is mandatory. Request delay defaults to 2 seconds,
  backfill defaults to enabled with a 90-empty-day stop, and the optional
  lower-bound date and disable flag are validated.
- Added PostgreSQL migrations for per-endpoint backfill cursors/status and
  persistent worker state. The reverse scan resumes after restart, advances
  only after a successful/empty endpoint result, preserves cursor and streak on
  failure/429, honors an inclusive configured lower bound, and stops after the
  configured consecutive-empty threshold when no lower bound is set.
- Added `worker.run_worker(config, client_factory, clock)` and the
  `garmin-local-mcp worker` command. It coalesces overdue schedule occurrences,
  prioritizes due recent syncs between history requests, refreshes all sample
  endpoints for today and yesterday, and leaves ordinary daily endpoint skip
  behavior intact. No schedule or timezone default is embedded in worker code.
- Added the shared PostgreSQL session advisory lock to manual, scheduled, and
  backfill syncs, including worker state/cursor updates. Request pacing is
  persisted across sync calls/processes and applies to activity-detail requests;
  429 retries use exponential 30/60/120-second waits.
- Extended `sync_status` and CLI `status` with schedule, last scheduled run,
  backfill progress/errors, and sample coverage. The Task 4 MCP tool signatures
  and registered tool set remain unchanged; no credentials are returned.

## TDD evidence

1. Added the six requested named worker tests before implementation. The exact
   requested initial run,
   `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test ./.venv/bin/uv run pytest tests/test_worker.py -q`,
   failed at collection with `ImportError: cannot import name 'worker'`, as
   expected while the module did not exist.
2. Added a cross-call throttling regression test before implementing persistent
   pacing. It failed because two separate sync calls produced no delay (`[]`
   instead of one 1.5-second wait); the shared PostgreSQL request timestamp then
   made it pass.
3. Added a regression for a rate-limited endpoint blocking all other backfill
   cursors. It failed with eight calls to the failed endpoint; prioritizing the
   least-recently-updated cursor let another endpoint progress while preserving
   the failed cursor.
4. Focused runs exposed and resolved test-isolation/schema updates, cursor
   expectations, and lock contention coverage. Final focused verification:
   `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test ./.venv/bin/uv run pytest tests/test_worker.py tests/test_sync.py tests/test_server.py -q`
   — **42 passed** (one upstream anyio/pytest deprecation warning).

## Verification

- Full PostgreSQL suite:
  `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test ./.venv/bin/uv run pytest -q`
  — **171 passed, 1 skipped** (one upstream anyio/pytest deprecation warning).
- `./.venv/bin/uv run ruff check .` — passed.
- `./.venv/bin/uv lock --check` — passed; lockfile unchanged.
- `git diff --check` — passed.
- The initial full-suite run found three stale assumptions (schema version 4
  and CLI tests without required schedule environment); the migration and
  PostgreSQL test fixture were updated, and the final full-suite run passed.
- The disposable PostgreSQL test container was used on port 55432 and removed
  after verification.

## Scope and commit

No Compose schedule defaults or external cron service were added; those remain
for Task 7. Intended commit: `feat: schedule resumable Garmin syncs`.
