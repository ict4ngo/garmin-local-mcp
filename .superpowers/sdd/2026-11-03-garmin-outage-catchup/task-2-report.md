# Task 2 report: scheduled catch-up status and documentation

## Changes

- Added additive `scheduled_catchup` status with `through`, local-date `target`
  (`today - 2` in the configured IANA timezone), and nonnegative
  `pending_days`.
- Status derives a legacy cursor without writing it: an existing
  `last_scheduled_run` is interpreted in the configured timezone and shifted
  back two calendar days; if no run exists, the fallback cursor is the target.
- Added the three requested deterministic status tests. The legacy test also
  verifies the cursor remains absent after status is read.
- Documented seven-day resumable recent catch-up separately from the
  90-consecutive-empty-day historical backfill, including the possibility of
  long catch-ups after an outage.

## TDD evidence

- RED: with the implementation temporarily absent, the focused catch-up test
  selection failed both selected tests with `KeyError: 'scheduled_catchup'`.
  The requested legacy test name does not contain `catchup`, so it is not
  selected by `-k catchup`.
- GREEN: after implementation,
  `pytest tests/test_server.py -k catchup -q` passed (2 passed), and
  `pytest tests/test_server.py -k 'catchup or legacy_cursor' -q` passed all
  three requested tests (3 passed).

## Verification

- Full PostgreSQL suite: `pytest -q` — **227 passed, 1 skipped**. This is ten
  passes above the brief's stated 217-pass baseline.
- `ruff check .` — passed.
- `uv lock --check` — passed.
- `docker compose config --quiet` — passed with required environment values
  supplied for validation.
- `git diff --check` — passed.

## Self-review

- Existing status fields and their meanings are unchanged; the new object is
  additive.
- Derivation uses only configured-timezone date and persisted worker state; it
  does not initialize/update worker state or invoke Garmin/network code.
- The documented recent recovery does not reset or force historical backfill;
  status exposes the persisted high-water mark and leaves scheduling and retry
  behavior untouched.
