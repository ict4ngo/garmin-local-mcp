# Final-review fix report — Garmin intraday MCP

## Findings addressed

1. Made SQLite `demo_info` optional; normal legacy warehouses without it migrate and report zero rows with `present: false`.
2. Added a transactional, durable SQLite source fingerprint/report marker. First import requires an empty PostgreSQL app warehouse; same-source retries return a no-op report without re-upserting, while unmarked data and a different prior source are rejected. The emptiness check covers daily/summary, raw/sync, samples, activity detail, backfill, and worker tables.
3. Migration now requires `source_stopped=True`; CLI requires `--source-stopped`. It copies the main SQLite file and WAL/SHM/journal sidecars to a temporary snapshot, fingerprints and reads those copies, and leaves source bytes unchanged. README explains stopping all SQLite writers before copying the database and sidecars.
4. Demo refusal and cleanup cover `samples`, `activity_detail_state`, `backfill_state`, and `worker_state`; tests confirm sample-only real data is refused and forced cleanup preserves the SQLite import marker.
5. Compose publishes HTTP only at `${GARMIN_MCP_LAN_IP:-127.0.0.1}:${GARMIN_MCP_HTTP_PORT:-8000}:8000`; `.env.example` and README describe loopback safety and private-LAN setup. PostgreSQL remains unpublished.
6. Removed `GARMIN_MCP_HTTP_BEARER_TOKEN` from the shared worker environment; only MCP receives it.
7. Corrected README's 14-tool inventory, storage/write scope, timezone/request-delay defaults, migration instructions/idempotency, sample tools, LAN/port setup, and schedule editing.
8. Raw JSON snapshots now write to same-directory temporary files and publish with atomic replacement; failed replacement leaves no visible partial JSON or index row.

## TDD evidence

RED runs were performed before production implementation:

- `GARMIN_MCP_TEST_DATABASE_URL=<guarded loopback test URL> .venv/bin/python -m pytest tests/test_sqlite_import.py -q` — **11 failed, 1 passed**. Expected failures showed the missing `source_stopped` API/CLI confirmation and uncovered absent optional-marker handling, migration marker/overwrite protection, and target-table checks.
- `GARMIN_MCP_TEST_DATABASE_URL=<guarded loopback test URL> .venv/bin/python -m pytest tests/test_deployment.py tests/test_documentation.py -q` — **4 failed, 3 passed**. Expected failures showed no explicit loopback host bind/private-IP/port override and stale/missing README claims.
- `GARMIN_MCP_TEST_DATABASE_URL=<guarded loopback test URL> .venv/bin/python -m pytest tests/test_demo.py -q -k 'sample_only or force_clears'` — **2 failed**. The sample-only warehouse was accepted for demo generation; the new marker/schema cleanup expectation was absent.
- `GARMIN_MCP_TEST_DATABASE_URL=<guarded loopback test URL> .venv/bin/python -m pytest tests/test_raw.py -q -k 'failed_atomic'` — **1 failed** because the old direct write did not call atomic replacement.

GREEN runs after implementation:

- Focused final fix suite: `GARMIN_MCP_TEST_DATABASE_URL=<guarded loopback test URL> .venv/bin/python -m pytest tests/test_sqlite_import.py tests/test_demo.py tests/test_deployment.py tests/test_raw.py tests/test_documentation.py -q` — **41 passed**, one pre-existing AnyIO/Pytest deprecation warning.
- Full PostgreSQL suite: `GARMIN_MCP_TEST_DATABASE_URL=<guarded loopback test URL> .venv/bin/python -m pytest -q` — **198 passed, 1 skipped**, one pre-existing `PytestRemovedIn10Warning` from AnyIO's pytest plugin.

The guarded PostgreSQL 17 test service was bound only to `127.0.0.1:55432`; its final verification runs used a passwordless local test URL in PostgreSQL trust mode. No production database, HTTP bearer token, or Garmin token was used. An early expected-RED pytest diagnostic printed the disposable test DSN as fixture context; that test-only service was stopped and replaced with the passwordless loopback test service before final runs.

## Deployment and repository verification

- `tests/test_deployment.py` plus documentation checks are included in the focused result above; Compose-normalized assertions cover loopback default, private-IP/custom-port override, MCP-only publication, worker token isolation, and no PostgreSQL port.
- `docker compose --env-file .env.example config` — **exit 0**.
- HTTP bind smoke, using the running MCP app and an unauthenticated request:
  - `loopback default: bound/reachable on 127.0.0.1:8000; unauthenticated request rejected (401)`
  - `private LAN custom port: bound/reachable on 192.168.1.129:18123; unauthenticated request rejected (401)`
  - The private-IP check used the host's assigned private interface; no PostgreSQL port was published. The test PostgreSQL service remained loopback-only.
- `.venv/bin/ruff check .` — **All checks passed!**
- `.venv/bin/uv lock --check` — **Resolved 48 packages**; lockfile valid and unchanged.
- `git diff --check` and `git show --check HEAD` — **exit 0**, no whitespace errors.

## Files changed

- Deployment/docs: `.env.example`, `compose.yaml`, `README.md`.
- Runtime: `src/garmin_mcp/cli.py`, `src/garmin_mcp/db.py`, `src/garmin_mcp/raw.py`, `src/garmin_mcp/sqlite_import.py`.
- Tests: `tests/conftest.py`, `tests/test_db.py`, `tests/test_demo.py`, `tests/test_deployment.py`, `tests/test_documentation.py`, `tests/test_postgres.py`, `tests/test_raw.py`, `tests/test_sqlite_import.py`.

## Commit and self-review

- Fix-wave implementation commit: `4170c16f4e1101f5ebf00a4eb8846fc625bb38c0` (`fix: harden migration and deployment safety`).
- Self-review confirmed migration marker/report insertion shares the target transaction; same-source retries do not upsert; data-bearing targets are refused before imports; SQLite reads use copied files; demo cleanup omits the migration marker; Compose never publishes PostgreSQL and excludes the bearer token from the worker; atomic-write failures clean up temporary files.

## Concerns

- Source quiescence is an explicit operator attestation (`--source-stopped`), not an automatic OS-level detection of every possible SQLite writer; this matches the approved requirement.
- One unrelated skipped test remains, and the suite emits the existing AnyIO/Pytest deprecation warning.
- The early expected-RED DSN diagnostic noted above was limited to a disposable local test credential; final verification used a passwordless loopback-only PostgreSQL test service.
