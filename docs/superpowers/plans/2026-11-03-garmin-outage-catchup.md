# Garmin Scheduled Outage Catch-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resume missed recent Garmin dates after worker outages without re-fetching successful history or losing progress on another interruption.

**Architecture:** Preserve the existing twice-daily yesterday/today sync and historical backfill. Add a separate PostgreSQL `worker_state` cursor for scheduled catch-up, process at most one chunk of seven older calendar days after each successful scheduled refresh, skip endpoint-days already `ok`/`empty`, and advance only after a clean chunk. A failed chunk waits for the next scheduled cycle instead of tight-loop retrying. Report the catch-up cursor, target, and pending-day count from `sync_status`.

**Tech Stack:** Python 3.12+, PostgreSQL, psycopg, pytest.

**Spec:** `docs/superpowers/specs/2026-10-28-garmin-intraday-design.md` — “Sync and raw data” outage-recovery requirement.

## Global Constraints

- PostgreSQL is the durable index and raw archive; no new dependency or secondary writable store.
- Keep the configured local 06:00/18:00 schedule and recent refresh for yesterday/today.
- Preserve the existing 90-consecutive-empty historical backfill policy; scheduled catch-up is separate.
- Use the shared PostgreSQL sync advisory lock and existing Garmin request throttle.
- Do not force re-download of older endpoint-days already marked `ok` or `empty`; retries must preserve raw snapshots and samples.

## Review Focus

- Existing deployments have `last_scheduled_run` but no catch-up cursor: initialize conservatively from the previous scheduled date and safely replay only dates not already complete.
- A chunk with one error or rate-limit abort must not advance the durable cursor; successful endpoint-days must be skipped on retry.
- Two scheduled times on one local date must still refresh yesterday/today without moving the older catch-up cursor past an unprocessed date.
- After a long downtime, keep the batch to seven calendar days and never let catch-up delay a due scheduled refresh.
- `sync_status` must show zero pending days when caught up and a correct pending range after restart.

---

### Task 1: Durable scheduled catch-up cursor

**Files:**
- Modify: `src/garmin_mcp/worker.py`
- Modify: `tests/test_worker.py`

**Interfaces:**
- Use existing `db.get_worker_state` / `db.set_worker_state` with key `scheduled_catchup_through`.
- Add `_run_scheduled_catchup_step(config, conn, client, *, today) -> bool`; one invocation handles at most seven consecutive dates, all daily and sample endpoints, and returns whether work was performed. Call it at most once after a scheduled refresh with no errors/abort; never from the worker's idle loop.
- `through` means the latest date fully processed by catch-up. The target is `today - 2 days`, leaving yesterday/today to the normal recent sync. If no cursor exists, initialize it before the scheduled pass to two days before the local date of the prior `last_scheduled_run`, conservatively replaying that run's yesterday/today window; with no prior run, initialize to the target.

- [ ] **Step 1: Add failing tests** named `test_catchup_cursor_initializes_conservatively_from_last_run`, `test_catchup_cursor_defaults_to_recent_window_without_last_run`, `test_catchup_processes_at_most_seven_days`, `test_catchup_skips_ok_and_empty_endpoint_days`, `test_catchup_retries_errors_without_advancing_cursor`, `test_catchup_waits_until_next_schedule_after_failure`, and `test_due_scheduled_sync_precedes_catchup`.
- [ ] **Step 2: Run `pytest tests/test_worker.py -k catchup -q`** and confirm the new recovery tests fail because the worker currently syncs only yesterday/today.
- [ ] **Step 3: Implement cursor initialization before the scheduled pass and one bounded step.** After a clean scheduled recent sync, run at most one catch-up chunk; defer the next chunk or any retry to the next scheduled cycle. Do not pass `force` or `force_endpoints` for catch-up so successful endpoint-days are reused.
- [ ] **Step 4: Verify restart semantics.** Advance `scheduled_catchup_through` transactionally only if the range report has no endpoint errors and is not aborted. On failure leave it unchanged; `sync_state` then skips completed dates and retries errors.
- [ ] **Step 5: Run `pytest tests/test_worker.py -q`** and verify all worker tests pass.

### Task 2: Observable status and user documentation

**Files:**
- Modify: `src/garmin_mcp/server.py`
- Modify: `tests/test_server.py`
- Modify: `README.md`

**Interfaces:**
- Add `scheduled_catchup` to `sync_status` with `through`, `target`, and nonnegative `pending_days`.
- Keep existing response keys and meanings unchanged.

- [ ] **Step 1: Add failing status tests** named `test_sync_status_reports_zero_catchup_backlog`, `test_sync_status_reports_pending_catchup_days`, and `test_sync_status_derives_legacy_cursor_without_mutating_state`.
- [ ] **Step 2: Implement status derivation** from persistent worker state and the configured timezone; do not make network calls from `sync_status`.
- [ ] **Step 3: Document** that recent catch-up is separate from historical backfill, uses seven-day resumable chunks, and may take time after long outages.
- [ ] **Step 4: Run `pytest tests/test_server.py -k catchup -q`** and verify the new status is additive and deterministic.

### Final verification

- [ ] Run the full PostgreSQL test suite; expected baseline is 217 passed, 1 skipped.
- [ ] Run Ruff, `uv lock --check`, Compose config validation, and `git diff --check`.
- [ ] Review the diff for schedule priority, no forced historical re-fetch, correct local-date boundaries, and persistent retry behavior.
