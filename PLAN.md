# Plan: garmin-local-mcp fork — unattended homelab deployment

Goal: fork `anup-shesh/garmin-local-mcp` (MIT) and turn it into an always-on,
unattended Garmin data warehouse running in Docker in the homelab, with
cron-driven sync, PostgreSQL instead of SQLite, and (later) granular
per-sample data.

## 1. Target architecture

```
┌─────────────────────────── docker compose ───────────────────────────┐
│                                                                      │
│  garmin-app (python:3.12-slim)        postgres:16-alpine             │
│  ├─ MCP server (HTTP/SSE, always on)  ├─ warehouse tables            │
│  └─ CLI: sync / import-fit / status   └─ volume: pgdata              │
│                                                                      │
│  garmin-sync (same image, cron)       volumes:                       │
│  └─ supercronic: nightly sync job     ├─ /data/raw   (JSON snapshots)│
│                                       ├─ /data/tokens (Garmin auth)  │
│  garmin-backup (postgres client)      └─ /backups → NFS server       │
│  └─ nightly pg_dump + raw tar                                        │
│                                                                      │
│  caddy (reverse proxy, basic auth) → app:8080 (MCP over HTTP)        │
└──────────────────────────────────────────────────────────────────────┘
```

- **App container**: long-running MCP server for analysis queries.
- **Sync container**: same image, different entrypoint — a cron scheduler
  (supercronic or ofelia) runs `garmin-local-mcp sync` nightly. Keeping sync
  in its own container means a hung sync never blocks the MCP server.
- **Postgres container**: the warehouse. Raw JSON snapshots stay on a volume
  (filesystem, not DB) — they remain the source of truth.

## 2. Database: SQLite → PostgreSQL

Why replace SQLite:

- Always-on server + nightly cron = concurrent writers/readers; SQLite gives
  `database is locked` races (no busy timeout in `db.py:83-90`).
- A single file on a volume is fragile for backups and has no real
  concurrency story; Postgres gives WAL, MVCC, `pg_dump`, replication later.
- Granular sample data (millions of rows) benefits from real indexing,
  partitioning, and later optionally TimescaleDB.

Migration notes (effort is moderate — SQL use is already centralized):

- `db.py` is the only connection factory; `connect()`, `migrate()`,
  `upsert_partial()`, `mark_sync()`, `synced_ok()` are the whole DB API.
  Replace with `psycopg` (v3) behind the same function signatures.
- `upsert_partial` already uses standard `ON CONFLICT ... DO UPDATE` —
  portable as-is (change `?` placeholders to `%s`).
- `analysis.py` queries are plain SQL; check for SQLite-isms
  (`strftime`, integer division, `PRAGMA`) and port.
- `sqlite3.Row` dict access (`row["col"]`) → `psycopg.rows.dict_row`.
- Schema: keep tables identical; convert `TEXT` timestamps to `timestamptz`
  where sensible, `CHECK` constraints carry over.
- Keep SQLite support? **No** — drop it to avoid a dual-backend abstraction.
  (Raw snapshots + `reparse` already make the DB disposable/rebuildable.)
- Config: add `database_url` to `config.toml` / `GARMIN_MCP_DATABASE_URL` env.

## 3. Docker-first deployment

Existing `Dockerfile` is a good base (already: slim image, `/data` volume,
`GARMIN_MCP_DATA_DIR=/data`). Changes:

- Add `docker-compose.yml`:
  - `postgres:16-alpine` with healthcheck, `pgdata` volume, credentials via
    env file (never committed).
  - `app`: build from Dockerfile, `command: serve --transport http`,
    depends_on postgres healthy, restart: unless-stopped.
  - `sync`: same image, `command: cron` (new subcommand wrapping
    supercronic or a simple loop), restart: unless-stopped.
  - Optional `backup` service: nightly `pg_dump` + tar of `/data/raw` to a
    second volume/restic target.
- Run container as non-root user; set restrictive permissions on
  `/data/tokens` (0700) at startup — addresses the permissions gap from the
  upstream code review.
- Tokens: initial `garmin-local-mcp login` requires interactivity (MFA).
  Procedure: run once locally or via `docker compose run --rm app login`,
  tokens persist in the shared volume thereafter. Add a healthcheck that
  fails loudly when tokens expire (`login --status` already exists).

## 4. Unattended operation

- **Cron schedule** (in sync container):
  - `15 4 * * *` — `sync` (defaults to last 7 days ending yesterday;
    incremental, idempotent).
  - `30 4 * * 0` — weekly `sync --from <30d ago>` safety net for gaps.
  - `0 5 * * *` — `status` output + gap check logged.
- **Failure handling**:
  - Sync already records per-(endpoint, date) errors in `sync_state` and
    resumes; keep that.
  - Add alerting on repeated failure: exit-code based ping to a healthchecks
    endpoint (or Gotify/ntfy push) from the cron wrapper.
  - Garmin auth breakage (happened March 2026) must not kill the stack:
    MCP server keeps answering from the warehouse; only sync degrades.
- **Observability**:
  - Structured logs to stdout (compose logging driver → Loki or plain files).
  - `status` subcommand already reports coverage + pending errors; add a
    `--json` flag so the cron wrapper can alert on gaps.

## 5. MCP transport: stdio → HTTP

The current server is stdio-only (`server.py`), fine for a laptop but not for
a homelab server that clients reach over the network.

- Add `serve --transport http` using the MCP SDK's streamable-HTTP/SSE
  transport (mcp 1.x supports SSE; check SDK version — pyproject pins
  `mcp>=1.0,<2`).
- Bind to the container network only; expose via reverse proxy (Traefik/
  Caddy) with auth (basic auth or OAuth proxy) — health data, no public
  access.
- Keep stdio mode working for local/dev use.

## 6. Granular sample data (phase 2, the original motivation)

- **Audit first**: inspect one existing `raw/daily/.../sleep.json` snapshot —
  the sleep endpoint already stores per-minute HR/stress/movement arrays in
  the raw layer. Years of history may already be on disk.
- New `samples` table (Postgres):
  `(ts timestamptz, metric text, value real, source text)` with a BRIN or
  btree index on `ts`; consider monthly partitioning once volume grows.
- Parser: extract sample arrays from raw sleep JSON during sync/reparse.
- FIT importer: keep individual `monitoring` heart-rate and `stress_level`
  records instead of collapsing to min/avg/max (`fitdecode.py:82-123`).
  Also store the FIT bundle itself under `raw/` so rebuilds don't lose it
  (upstream bug: FIT rows are unrecoverable after reparse).
- New MCP tools: `query_samples(metric, start, end, bucket=1h)` returning
  hourly aggregates by default; raw resolution on demand for narrow ranges.
- Backfill: reparse existing raw snapshots to populate history for free.

## 7. Upstream bugs to fix early (from code review)

1. `reparse` leaves stale rows (`sync.py:200-236`) — rebuild must clear and
   reconstruct tables transactionally.
2. FIT imports not persisted as raw source (`importer.py:18-45`).
3. Empty FIT bundles create hollow rows (`fitdecode.py` / `importer.py`).
4. File permissions for tokens/raw data (`login.py`, `raw.py`).
5. Concurrent-writer races (moot after Postgres move, but raw-snapshot
   writes should still be atomic: temp file + rename).
6. FIT sleep-stage totals miss the final segment and aren't clipped to the
   sleep window (`fitdecode.py:187-202`).

## 8. Phases / milestones

| Phase | Deliverable | Exit criteria |
|---|---|---|
| 0 | Fork, branch `homelab`, CI green | tests + ruff pass |
| 1 | Postgres backend | full test suite green on PG; `reparse` rebuilds identically |
| 2 | docker-compose stack (app + sync + postgres) | `compose up` → synced warehouse, MCP reachable over HTTP |
| 3 | Unattended hardening | 2 weeks unattended: cron sync, alerts, backups, token-expiry detection |
| 4 | Granular samples | hourly HR/stress queryable for historical dates |
| 5 | Optional: upstream PRs | fixes 1–3, 5–6 offered back to `anup-shesh/garmin-local-mcp` |

## 9. Decisions (finalized)

- **Postgres**: dedicated `postgres:16-alpine` container owned by this
  compose stack (not a shared homelab instance).
- **MCP HTTP auth**: reverse proxy terminates auth with **basic auth**;
  the app itself binds to the internal Docker network only.
- **Backup target**: **NFS server**. Nightly `pg_dump` + tar of `/data/raw`
  written to an NFS-mounted volume.
- **Raw snapshot retention**: **5 years**, enforced by a periodic prune job
  (snapshots older than 5 years are deleted along with their
  `raw_snapshots` index rows).

## 10. Non-goals

- No multi-user support; single Garmin account.
- No cloud dependency; everything stays in the homelab.
- No rewrite of analysis tools — they stay SQL-based and DB-agnostic.
