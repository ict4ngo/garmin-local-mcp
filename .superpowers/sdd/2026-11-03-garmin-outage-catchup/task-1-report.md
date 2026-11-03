# Task 1: Durable scheduled catch-up cursor

## Implementation

- Added the `scheduled_catchup_through` worker-state cursor. It initializes before a scheduled recent sync: from the previous scheduled run's local calendar date minus two days, or to `today - 2 days` when no previous run exists.
- Added `_run_scheduled_catchup_step`, which attempts at most seven consecutive dates through `today - 2 days`, invokes the ordinary daily and sample endpoints without force flags, and advances the cursor only after a report with no endpoint errors or abort.
- A clean scheduled recent sync triggers at most one catch-up step. Catch-up is not run from the idle/backfill loop, so a failed chunk waits until the next scheduled cycle; `sync_state` allows completed endpoint-days to be reused on retry.
- Left scheduled recent-sync behavior and historical backfill unchanged.

## TDD evidence

- RED: `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@127.0.0.1:55432/garmin_test .venv/bin/pytest tests/test_worker.py -k catchup -q` — **7 failed**, as expected before implementation: the catch-up step did not exist and scheduling did not invoke it.
- GREEN: the same filtered command after implementation — **7 passed, 14 deselected**.
- The first test attempt without `GARMIN_MCP_TEST_DATABASE_URL` could not collect/setup PostgreSQL tests; the test URL above was then provided using a disposable local PostgreSQL 17 container.

## Verification

- `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@127.0.0.1:55432/garmin_test .venv/bin/pytest tests/test_worker.py -q` — **21 passed**.
- `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@127.0.0.1:55432/garmin_test .venv/bin/pytest -q` — **224 passed, 1 skipped**.
- Both pytest runs emitted one existing `PytestRemovedIn10Warning` from the installed AnyIO pytest plugin.

## Self-review

- Catch-up uses the same `sync.sync_range` path, advisory lock, request throttle, local-date boundary, and sync-state skip behavior as existing sync work.
- The cursor remains unchanged on partial endpoint errors or aborts; no retry is initiated in the idle loop.
- No actual Garmin sync was run.
