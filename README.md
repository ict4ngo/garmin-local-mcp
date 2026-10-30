<!-- mcp-name: io.github.anup-shesh/garmin-local-mcp -->

# garmin-local-mcp

**Local-first Garmin data warehouse with an analysis-grade MCP server.**
Sync once, analyze forever, even when the API is down.

![garmin-local-mcp answering questions from a local store, including with the network disconnected](https://raw.githubusercontent.com/anup-shesh/garmin-local-mcp/main/docs/demo.gif)

## Why another Garmin MCP?

Every existing Garmin MCP server follows the same design: a thin live wrapper
around Garmin's rate-limited, unofficial API. Each question your AI assistant
asks becomes one or more live API calls that return huge raw JSON blobs (a
single raw sleep response runs around 230 KB). Multi-month questions like "how
does my sleep correlate with training load?" are impractical, and when Garmin
changes its auth (as it did in March 2026, breaking the whole ecosystem), those
servers go completely dark, even for data they already fetched yesterday.

This project inverts the architecture:

- **Sync once, analyze forever.** Incremental sync into a local warehouse:
  immutable raw JSON snapshots plus a PostgreSQL database, in storage you own.
- **Server-side analysis, compact responses.** Trends, correlations, personal
  baselines, and anomaly detection are computed locally and returned as small
  columnar tables in a single tool call. Typical responses are under 2 KB, so
  nothing floods the model's context.
- **Offline resilience.** An API breakage pauses new syncs only. Every query
  over already-synced history keeps working.
- **A zero-auth fallback.** A standalone decoder for Garmin's undocumented
  wellness FIT messages (sleep score, HRV, skin temperature, sleep stages,
  naps) ingests manually exported bundles with no login at all. No other
  Garmin MCP ships this.
- **Curated tools.** 14 composable tools, not 110.

| | garmin-local-mcp | Typical API-wrapper Garmin MCPs |
|---|---|---|
| Local data store you own | Yes (raw JSON + PostgreSQL) | No |
| Works offline after an API breakage | Yes (analysis over synced history) | No |
| Server-side analysis (trends, correlations, baselines, anomalies) | Yes | No (raw JSON pass-through) |
| Response size discipline | Compact columnar tables, typically < 2 KB | Raw payloads, up to hundreds of KB |
| Zero-auth ingest path | Yes (FIT bundle import) | No |
| Tool count | 14 curated | Often 20 to 110+ |

## Try it without a Garmin account

If you don't own a Garmin, or just want to see what the tools return before
handing over credentials, seed a synthetic store:

This still requires a reachable PostgreSQL database configured through
`GARMIN_MCP_DATABASE_URL`, `GARMIN_MCP_TIMEZONE`, and
`GARMIN_MCP_SYNC_TIMES`; it avoids Garmin login/API access, not the database.

```
pip install garmin-local-mcp
garmin-local-mcp --data-dir ~/.garmin-mcp-demo demo
garmin-local-mcp --data-dir ~/.garmin-mcp-demo serve
```

That generates 180 days across every table, then serves them over MCP. No
login, no network, no account.

The data is generated rather than recorded, but it is not random. A latent
recovery factor drives HRV up while resting heart rate goes down, training
load raises the *next* day's resting heart rate, a six-day illness window sits
in the middle of the range, and a few sleep nights are deliberately missing. So
the analysis tools have something real to find:

| Ask | Returns |
|---|---|
| `correlate(hrv, resting_hr)` | about −0.5, a genuine inverse relationship |
| `correlate(training_load, resting_hr, scan_lags=True)` | ~0 at lag 0, **+0.45 at lag 1** — the effect is next-day |
| `anomalies()` | the illness window, flagged across resting HR, HRV, skin temperature, SpO2 and sleep score at once |
| `gaps()` | the missing sleep nights |

`sync_status` reports `demo_store: true` on these stores, so an assistant can
never present generated numbers as real measurements. The generator is
deterministic — `--seed` reproduces a store exactly, and `--days` changes the
range. `demo` refuses to overwrite a database it did not generate.

## Docker Compose deployment

Docker Compose runs PostgreSQL 17, the authenticated Streamable HTTP MCP server,
and the scheduled sync worker. Docker is the only host runtime dependency.
PostgreSQL is not published to the host; only the MCP HTTP port is exposed on
host port 8000 by default. It binds to loopback by default, so remote clients
cannot connect until `GARMIN_MCP_LAN_IP` is set to the host's private LAN
interface address. The HTTP server requires a static Bearer token.

```sh
cp .env.example .env
```

Edit `.env` before starting. Replace **all** `REPLACE_...` placeholders. Make
two different strong secrets (hex avoids URL-escaping problems in the database
URL), for example:

```sh
openssl rand -hex 32  # use one result for POSTGRES_PASSWORD
openssl rand -hex 32  # use a different result for GARMIN_MCP_HTTP_BEARER_TOKEN
```

Set `GARMIN_MCP_TIMEZONE` to your IANA timezone (for example
`America/Denver`). Protect the file with `chmod 600 .env` and do not commit it.
`GARMIN_MCP_LAN_IP` defaults to `127.0.0.1`; set it to the Docker host's
private LAN interface IP (for example `192.168.1.20`) to allow other trusted
LAN machines to connect. `GARMIN_MCP_HTTP_PORT` defaults to `8000` and changes
the published host port without changing the container port. DNS-rebinding
protection remains enabled: the MCP service accepts only the exact configured
`GARMIN_MCP_LAN_IP:GARMIN_MCP_HTTP_PORT` Host (and loopback Host aliases), not
arbitrary hostnames or other LAN IPs. The remote client URL must use that
configured LAN IP, not `localhost` or `0.0.0.0`; the loopback default remains
available for local development. The worker does not receive
`GARMIN_MCP_HTTP_BEARER_TOKEN`, `GARMIN_MCP_LAN_IP`, or
`GARMIN_MCP_HTTP_PORT`; only the MCP service needs those HTTP settings.
The Compose schedule default is `06:00,18:00`; edit `GARMIN_MCP_SYNC_TIMES` in
`.env` to any comma-separated local `HH:MM` times. For example, setting it to
`07:15,21:45` changes the worker schedule on the next container recreation; it
does not require an image rebuild. The worker itself has no fallback schedule.

Log in and start the services:

```sh
docker compose run --rm mcp login
docker compose up -d
```

The login command stores Garmin tokens in the shared `garmin_data` volume.
Initial history collection and subsequent resumable, throttled backfill run in
the worker; scheduled syncs run at the configured times. For an explicit sync
or to inspect status, use `docker compose run --rm mcp sync --from 2026-01-01`
or `docker compose run --rm mcp status`. CLI commands use the same PostgreSQL
settings and app-data volume as the services.

To migrate an existing SQLite warehouse, stop **every process that writes the
old SQLite warehouse** before copying anything. This includes old app/worker
processes and any other local writers. Copy the main database and any
WAL/SHM/journal sidecars (`-wal`, `-shm`, or `-journal`) together; preserve the
sidecar suffixes. Keep PostgreSQL empty and stop this deployment's MCP and
worker while migrating.
Legacy databases may omit the optional `demo_info` table. The migration leaves
SQLite files unchanged, records a durable source fingerprint/report, and a
repeat with the same source fingerprint is a no-op. It refuses to run without
`--source-stopped`, into a non-empty PostgreSQL warehouse, or after a different
SQLite source has already been imported; it never upserts over newer rows.

```sh
docker compose up -d postgres mcp
docker compose stop mcp sync-worker
docker compose up -d postgres
docker compose cp ./garmin.db mcp:/data/legacy.db
for suffix in -wal -shm -journal; do
  [ ! -e "./garmin.db$suffix" ] || docker compose cp "./garmin.db$suffix" "mcp:/data/legacy.db$suffix"
done
docker compose run --rm --no-deps mcp migrate-sqlite --from /data/legacy.db --source-stopped
docker compose up -d
```

Copy any associated legacy raw snapshot files into the `garmin_data` volume at
the same relative paths referenced by the SQLite `raw_snapshots` rows before
reparse/backup. The command imports only the indexed PostgreSQL rows; it does
not remove or modify source files.

The `postgres_data` volume holds only PostgreSQL's database files. The
`garmin_data` volume is the separate persistent app-data volume shared by MCP
and worker; it holds Garmin tokens, immutable raw API snapshots, and FIT
bundles/files saved there. PostgreSQL is the
rebuildable query index; raw snapshots and tokens remain in the separate
app-data volume. Back up both volumes; they contain sensitive health
information. For a consistent backup, stop the
worker and MCP writer (leave PostgreSQL running), then dump PostgreSQL and
archive the app-data volume:

```sh
mkdir -p backups
docker compose stop sync-worker mcp
docker compose exec -T postgres pg_dump -U garmin -d garmin -Fc > backups/garmin.dump
docker compose run --rm --no-deps --entrypoint tar mcp -czf - -C /data . > backups/garmin-data.tar.gz
docker compose start mcp sync-worker
```

Restore into a new/empty deployment after ensuring the named volumes are not in
use. Start PostgreSQL but keep MCP and the worker stopped; restore the files
first, then PostgreSQL:

```sh
docker compose stop sync-worker mcp
docker compose up -d postgres
docker compose run --rm --no-deps --entrypoint tar mcp -xzf - -C /data < backups/garmin-data.tar.gz
docker compose exec -T postgres pg_restore -U garmin -d garmin --clean --if-exists < backups/garmin.dump
docker compose start mcp sync-worker
```

Keep off-host encrypted copies of both backups. Do not use `docker compose down
-v` for normal maintenance: it deletes the named volumes and all stored data.

### Remote HTTP clients

The HTTP endpoint is `http://<GARMIN_MCP_LAN_IP>:<GARMIN_MCP_HTTP_PORT>/mcp`.
Set `GARMIN_MCP_LAN_IP` to the host's private LAN interface and optionally
change `GARMIN_MCP_HTTP_PORT` in `.env`; recreate the MCP container after edits,
without rebuilding the image. Use the configured static Bearer token. **Plain
HTTP does not encrypt credentials or health data**: keep
this endpoint on a trusted LAN, restrict the configured MCP host port with your
firewall, and never expose it directly to the Internet. For remote access, put it behind a
TLS-terminating reverse proxy (and keep the upstream network private); do not
assume the bearer token is a substitute for HTTPS.

OpenCode V2 remote MCP configuration (for example in `opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "garmin": {
      "type": "remote",
      "url": "https://garmin.example.com/mcp",
      "oauth": false,
      "headers": {
        "Authorization": "Bearer REPLACE_WITH_GARMIN_MCP_HTTP_BEARER_TOKEN"
      }
    }
  }
}
```

Hermes remote MCP configuration uses the URL and static headers directly; it
does not start OAuth:

```yaml
mcp_servers:
  garmin:
    url: https://garmin.example.com/mcp
    headers:
      Authorization: "Bearer REPLACE_WITH_GARMIN_MCP_HTTP_BEARER_TOKEN"
```

Use the reverse-proxy HTTPS URL in these examples, replace the token placeholder,
and keep client config files private. The published MCP package metadata remains
for the **stdio** transport; remote HTTP is a separate Compose deployment.
Direct stdio runs also require a PostgreSQL URL, timezone, and sync schedule
environment variables. The database URL is a secret because it contains DB
credentials.

## Quickstart

Requires Python 3.12+ and a PostgreSQL database. Set `GARMIN_MCP_DATABASE_URL`,
`GARMIN_MCP_TIMEZONE`, and `GARMIN_MCP_SYNC_TIMES` in the environment for every
CLI/stdio process. The Docker Compose setup above supplies these values.

```
pip install garmin-local-mcp
```

Or run it without installing, via [uv](https://docs.astral.sh/uv/):

```
uvx garmin-local-mcp --help
```

**1. Log in once** (MFA supported; tokens persist locally, so future runs never
ask for a password):

```
garmin-local-mcp login
```

**2. Backfill your history.** The sync is resumable, safe to interrupt, and
throttled to be polite to Garmin's servers. A year of history is roughly 1,800
requests; the default delay between requests is 2.0 seconds. For long backfills,
start it and let it run (overnight works well).
If it gets rate limited or interrupted, re-run the same command and it resumes
where it left off.

```
garmin-local-mcp sync --from 2026-01-01
```

**3. Register the MCP server with your client** (see [Client setup](#client-setup)
for Claude Desktop, Cursor, and other clients):

```
claude mcp add --scope user garmin -- garmin-local-mcp serve
```

**4. Ask questions.** Examples of what Claude can now answer from your local
warehouse in one or two tool calls:

- "How does my sleep score correlate with next-day resting HR?"
- "What were my anomalous HRV days this quarter?"
- "Show weekly training load vs sleep for the last 3 months."

## Client setup

The server speaks stdio, so any MCP client works. `pip install garmin-local-mcp`
first (or use the `uvx` variants below, which need nothing installed beyond
[uv](https://docs.astral.sh/uv/)).

**Claude Code**

```
claude mcp add --scope user garmin -- garmin-local-mcp serve
```

**Claude Desktop, one-click:** download `garmin-local-mcp-x.y.z.mcpb` from the
[latest release](https://github.com/anup-shesh/garmin-local-mcp/releases/latest),
then in Claude Desktop open Settings > Extensions > Advanced settings, click
"Install Extension…", and select the file. Requires
[uv](https://docs.astral.sh/uv/getting-started/installation/) on your PATH;
the extension installs and runs the server from PyPI via uvx, so no manual
Python setup is needed. If the install dialog warns about a missing
`Python >=3.12`, you can ignore it: uv provisions its own interpreter.

**Claude Desktop, manual** (Settings, then Developer, then Edit Config; add to
`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "garmin": {
      "command": "garmin-local-mcp",
      "args": ["serve"]
    }
  }
}
```

**Cursor** (`~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project):

```json
{
  "mcpServers": {
    "garmin": {
      "command": "garmin-local-mcp",
      "args": ["serve"]
    }
  }
}
```

**Any other stdio client / no local install** (requires uv):

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uvx",
      "args": ["garmin-local-mcp", "serve"]
    }
  }
}
```

Note: `login` and the initial backfill `sync` are CLI steps (see
[Quickstart](#quickstart)); the MCP server itself never prompts for
credentials.

## The 14 tools

| Tool | What it does |
|---|---|
| `auth_status` | Check whether stored Garmin Connect tokens exist (use before sync, or after an auth error). |
| `sync` | Fetch up to 60 days from Garmin Connect into the local store (default: last 30 days ending yesterday; big backfills belong in the CLI). |
| `sync_status` | Local data coverage per table, last sync time, and pending sync errors. |
| `get_day` | One merged view of a single day: wellness, sleep, HRV, training status, activities, and data-quality flags. |
| `query_metrics` | Columnar time series for one or more metrics between two dates, with daily/weekly/monthly aggregation and optional stats. |
| `query_samples` | Bounded native timestamped samples for selected metrics and a half-open ISO datetime range; returns source timestamps/units without resampling. |
| `compare_time_windows` | Compare caller-defined local-clock windows by per-day means across an inclusive date range, with counts and missing-coverage details. |
| `correlate` | Pearson/Spearman correlation between two metrics, with day-lag support and an optional scan over lags -7..+7. |
| `baselines` | Personal mean +/- sd band per metric over a trailing window (default 28 days), to judge what is normal for this user. |
| `anomalies` | Outlier days (z-score deviations) and sustained streaks (5+ consecutive days on one side of the mean). |
| `list_activities` | Recent activities newest-first as a compact table, filterable by type, date range, and minimum distance. |
| `get_activity` | Full stored summary row for one activity (summary fields only, no GPS or sample streams). |
| `gaps` | Missing days per table plus unresolved sync errors, to find holes worth re-syncing before drawing conclusions. |
| `import_fit` | Zero-auth offline ingest of a manually exported Garmin wellness FIT bundle. |

`sync` and `import_fit` write to the PostgreSQL warehouse and, when source
payloads are fetched or imported, the raw/token app-data volume. The separate
`sync-worker` also writes PostgreSQL and raw snapshots on its schedule without
an MCP client being connected. Read/query tools do not write. The HTTP bearer
token is provided only to the MCP service, never the worker. The server never
prompts: auth problems come back as structured errors with a hint pointing at
the login CLI.

Available metric names include `resting_hr`, `sleep_score`, `hrv`, `steps`,
`stress_avg`, `body_battery_high`, `skin_temp_dev_c`, `vo2max`, `fitness_age`,
`achievable_fitness_age`, `training_load`, and about 25 more; any tool given
an unknown name returns the full list.

## Data layout and ownership

App files live in one directory you own (default `~/.garmin-mcp`, override
with the `GARMIN_MCP_DATA_DIR` environment variable or `--data-dir`); the
PostgreSQL database is configured separately through
`GARMIN_MCP_DATABASE_URL`:

```
~/.garmin-mcp/
├── config.toml                                  # optional settings
├── tokens/                                      # Garmin Connect session tokens
├── raw/daily/YYYY/YYYY-MM-DD/<endpoint>.json    # immutable raw API snapshots
├── raw/activities/<activity_id>.json            # one snapshot per activity
└── ...                                          # raw snapshots, tokens, and saved FIT bundles
```

The raw JSON snapshots are the source of truth and are never overwritten.
`garmin-local-mcp reparse` rebuilds the PostgreSQL tables from raw snapshots
entirely offline, which is the universal escape hatch for schema evolution and
parser fixes. Your data stays on infrastructure you control.

## Data quality note

Garmin watches report a provisional on-device resting heart rate that can
diverge sharply from Garmin Connect's finalized value on nights with sparse
sampling. A real observed case: the watch reported 69 bpm on-device while
Garmin Connect later finalized the same night at 56 bpm.

This project handles that in two ways:

- The API sync stores Garmin Connect's finalized value.
- The FIT importer cross-checks the provisional on-device value against the
  overnight heart-rate floor. A resting HR sitting more than 10 bpm above the
  lowest overnight sample is a rate the watch never actually observed; it gets
  flagged (`rhr_far_above_hr_floor`) and withheld, leaving the field for the
  API to backfill rather than storing a misleading number.

Sparse sleep-stage logging is flagged the same way
(`sparse_sleep_stage_logging`), and flags surface in `get_day` so the analysis
layer knows which numbers to trust.

## Offline / fallback runbook

If Garmin breaks the unofficial API again (it has before):

1. **Everything analytical keeps working.** All query, correlation, baseline,
   anomaly, and gap tools run on your already-synced local history. Only new
   syncs pause.
2. **Keep ingesting without auth.** Download a daily FIT bundle from the
   Garmin Connect website and import it locally (exact steps below).
   `garmin-local-mcp import-fit <folder>` decodes the bundle with zero
   authentication and fills the gap days. FIT-sourced rows never overwrite
   API-sourced rows (unless you pass `--force`).
3. **Resume when the community catches up.** Watch the
   [python-garminconnect](https://github.com/cyberjunky/python-garminconnect)
   project for a fix, upgrade, and run `garmin-local-mcp sync` again. Thanks
   to resumable sync state, it picks up exactly where it stopped.

### Downloading a wellness bundle, step by step

1. Sign in at [connect.garmin.com](https://connect.garmin.com) in any
   browser.
2. Go directly to
   **<https://connect.garmin.com/app/settings/accountInformation>**
   (or click your avatar in the top-right corner, then **Settings**, then
   **Account Information** in the left sidebar).
3. Scroll to the bottom of the page, to the section titled
   **Export Wellness Data** ("Download your wellness FIT files from a
   specific day. This includes data such as steps, sleep, stress, HRV and
   more.").
4. Pick a date in the **Date** field and click **Export**. Your browser
   downloads a small zip for that one day, containing roughly 12 to 15
   binary `.fit` files (`*_WELLNESS.fit`, `*_SLEEP_DATA.fit`,
   `*_HRV_STATUS.fit`, `*_SKIN_TEMP.fit`, `*_METRICS.fit`, and similar).
5. Unzip it into a folder and run:

   ```
   garmin-local-mcp import-fit "path/to/unzipped/folder"
   ```

6. Repeat for each missing day (one bundle per date). The `gaps` tool or
   `garmin-local-mcp status` tells you which days need filling.

Two things worth knowing:

- **Overnight sleep belongs to the wake date.** To get last night's sleep,
  export yesterday's date if you slept into this morning, i.e. the date you
  woke up on.
- This per-day export is instant and separate from Garmin's full account
  export (the "Data Management" link on the same page), which is a bulk
  archive that can take days to arrive by email and is not what
  `import-fit` expects.

## Configuration

Optional `config.toml` in the data directory:

| Key | Default | Meaning |
|---|---|---|
| `timezone` | none; required in the environment | IANA name (e.g. `America/Denver`) used for sync ranges and schedules; there is no system-timezone fallback |
| `units` | `metric` | `metric` or `statute` |
| `request_delay_seconds` | `2.0` | Conservative delay between Garmin requests during backfill/sync |
| `baseline_window_days` | `28` | Default trailing window for the `baselines` tool |

Environment variables:

| Variable | Meaning |
|---|---|
| `GARMIN_MCP_DATA_DIR` | Override the data directory (default `~/.garmin-mcp`) |
| `GARMIN_MCP_DATABASE_URL` | Required PostgreSQL connection URL; contains database credentials and must be kept secret |
| `GARMIN_MCP_TIMEZONE` | Required IANA timezone for local dates and scheduling; there is no system-timezone fallback |
| `GARMIN_MCP_SYNC_TIMES` | Required comma-separated local `HH:MM` schedule for the worker (Compose default: `06:00,18:00`) |
| `GARMIN_MCP_HTTP_BEARER_TOKEN` | Required static Bearer token for `serve-http`; not needed for stdio |
| `GARMIN_MCP_LAN_IP` | Compose host bind address; defaults to `127.0.0.1`; set the host's private LAN IP for remote clients |
| `GARMIN_MCP_HTTP_PORT` | Compose host port; defaults to `8000` and maps to container port 8000 |
| `GARMIN_MCP_REQUEST_DELAY_SECONDS` | Delay between Garmin requests (default `2.0` seconds) |
| `GARMINTOKENS` | Override the token store location (default `<data_dir>/tokens`) |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | Optional, for non-interactive re-login; when set, `garmin-local-mcp login` skips the prompts (MFA may still prompt if your account requires it) |

## Development

```
python -m venv .venv
.venv/bin/pip install -e .[dev]     # Windows: .venv\Scripts\pip install -e .[dev]
pytest
ruff check .
```

The test suite runs fully offline against sanitized JSON fixtures and small
FIT samples; CI never touches the live API.

## Disclaimer

This project is not affiliated with, endorsed by, or supported by Garmin Ltd.
It uses the community [python-garminconnect](https://github.com/cyberjunky/python-garminconnect)
library with your own credentials to access your own data. Garmin's APIs are
unofficial, rate-limited, and can change or break at any time. Sync is
intentionally throttled, but Garmin may still rate-limit or block requests;
there is no guaranteed API quota or availability. When the API changes, your
synced history remains fully usable and the FIT import path keeps working.

All data stays on infrastructure you control. Nothing phones home: no
telemetry, no third-party services, no cloud. Treat your data directory like
the personal health record it is, and never commit it to a repository.

## License

MIT
