# Garmin intraday data and homelab MCP service

**Status:** Approved by the user on 2026-10-28. Final review added safety
clarifications for migration and LAN binding; implementation is in progress.

## Intent

Keep an independent, durable copy of the timestamped Garmin health and training
measurements that Garmin Connect makes available, so the user can query and
compare any time period through an MCP server in a Docker-based homelab. The
motivating question is whether stress tends to be higher in the middle of the
day than in the evening. Daily averages cannot answer that reliably.

The user owns the Garmin data and wants the finest native resolution available
(minute-level when Garmin returns minute-level samples), rather than a daily-only
view. Old detailed history is desirable but secondary to starting reliable
collection at deployment. The user prefers an automatic historical backfill if
it can be throttled and allowed to take hours or days.

## Existing project context

- `src/garmin_mcp/endpoints.py` currently fetches date-keyed wellness, sleep,
  HRV, training-status, fitness-age, and activity payloads. The usersummary
  parser stores daily stress average/maximum and Body Battery high/low; the
  separate Body Battery endpoint is deliberately omitted.
- Sleep payload snapshots can contain intraday arrays, but the parser currently
  extracts summaries only. Raw API snapshots are retained and SQLite is the
  query index (`src/garmin_mcp/sync.py`, `src/garmin_mcp/raw.py`).
- `query_metrics` currently returns date-based series with daily/weekly/monthly
  aggregation. Synchronization is explicit; successful endpoint/date pairs are
  skipped on subsequent syncs.
- `Dockerfile` persists `/data`, but starts a `stdio` MCP server. There is no
  built-in recurring schedule or network listener.
- The project allows `garminconnect>=0.3.6,<0.4`; the exact temporal endpoint
  methods and payloads must be verified against the installed compatible
  version, not assumed from the library's newest source alone.

## Goals

1. Inventory and ingest the timestamped series available through the supported
   Garmin Connect client, prioritizing stress, Body Battery, sleep details,
   heart-rate/wellness series, and activity samples where the upstream API
   provides them.
2. Retain source payloads and expose normalized samples at their native
   timestamps. Preserve timestamp/time-zone information and do not interpolate
   or fabricate finer-grained values.
3. Make samples queryable across arbitrary archived date/time ranges. Support
   useful time-of-day comparisons (for example, caller-defined midday and
   evening windows across a chosen date range) without requiring the model to
   retrieve an unbounded raw series. Keep access to individual samples as well
   as summaries, and retain the existing daily tools.
4. Begin autonomous collection on deployment, with two scheduled syncs per day
   as the Compose default, roughly morning and later in the day. The schedule
   must be supplied through Docker Compose/runtime configuration, not baked into
   the worker code. Also attempt a slow, resumable initial historical backfill.
   A restarted container should resume both scheduled work and backfill;
   repeated syncs must be safe.
5. Run a separate PostgreSQL database service in Docker Compose with its own
   persistent volume. Keep the API service and scheduled ingestion in Compose
   too; do not require a database server or language runtime on the host.
6. Persist raw Garmin snapshots and tokens separately from PostgreSQL's data
   directory, so the raw archive remains a recoverable source of truth.
7. Offer an authenticated remote MCP endpoint usable by both Hermes and
   OpenCode. Require a bearer token for HTTP MCP requests.

## Proposed behavior

### Collection and archive

- Use date-scoped Garmin endpoints where that is how the upstream client
  exposes the data. A scheduled run should refresh detailed samples for the
  current day even if that date was fetched earlier, because the day’s series
  may have grown. Also revisit a recent completed day to catch delayed final
  readings, subject to conservative request pacing and observed API limits.
- Read run times and timezone from runtime configuration passed by Docker
  Compose; the worker must not silently choose a hard-coded schedule. Provide
  Compose defaults of 06:00 and 18:00 in the configured timezone, while making
  both editable without rebuilding the image. Avoid minute-by-minute polling.
- On initial authenticated deployment, automatically attempt the oldest
  historical range Garmin makes available for each supported temporal
  endpoint. Do not impose an arbitrary fixed history cutoff; expose an optional
  operator-configured start date/cutoff and a way to disable the backfill.
- Throttle that backfill conservatively: at most one Garmin request in flight,
  a configurable delay between requests, and exponential backoff when rate
  limited. Persist progress so a restart resumes where it stopped. The backfill
  may take hours or days and must not block MCP startup; scheduled recent-data
  syncs should not be starved by it. Garmin publishes no safe request quota, so
  document the default as conservative rather than guaranteed safe.
- Make ingestion idempotent and resilient to 429/rate-limit errors, partial
  endpoint failures, process restarts, and missed scheduled runs. Keep daily
  summary ingestion working independently of granular endpoints.
- Keep raw responses as the recoverable source of truth. Repeated same-day
  fetches must not silently destroy prior source payloads; version refreshed
  snapshots or provide an equivalent lossless strategy. The normalized sample
  index should avoid duplicate observations and handle upstream corrections in
  a documented way.
- Report backfill coverage, progress, failures, and gaps. Do not imply that
  Garmin exposes complete minute-level history for every metric/account/device.

### Database and persistence

- Use plain PostgreSQL as the separate query-store service. PostgreSQL's native
  timestamp support and indexes are sufficient for the expected personal
  time-series workload; do not add TimescaleDB or another time-series extension
  unless measured needs later justify the extra operational complexity.
- The Compose file should define the app, the PostgreSQL service, and the
  independent background sync worker (the worker may reuse the app image).
  Give PostgreSQL a named persistent volume for its own data directory. Keep
  the Garmin raw archive and tokens in a separate persistent app-data volume
  shared with the worker. Do not publish the PostgreSQL port to the LAN; only
  the authenticated MCP endpoint needs LAN access.
- Treat PostgreSQL as the rebuildable index and the raw archive as source of
  truth. Preserve refreshed API snapshots losslessly, and retain imported FIT
  bundles in the raw volume so future FIT-derived rows can also be rebuilt.
  Because current FIT imports store summaries without retaining their source
  bundle, preserve those existing rows during migration rather than assuming
  they can be reparsed later.
- Replace SQLite as the ongoing runtime database rather than maintaining two
  writable backends. Provide a one-time, non-destructive SQLite-to-PostgreSQL
  migration for existing installations: leave the source SQLite file intact
  until the PostgreSQL copy is validated, and report row counts/coverage. Require
  all old SQLite writers to be stopped before migration and require an explicit
  operator confirmation. Import only into an empty PostgreSQL warehouse; repeat
  migration of the same source is a no-op, and do not overwrite newer PostgreSQL
  rows. Legacy databases may not contain the optional demo marker table.
- Configure database credentials separately from the MCP bearer token. Keep
  credentials out of the image and logs; Compose secrets or required runtime
  environment variables may be used. PostgreSQL should accept connections only
  on the private Compose network.

### Time-series access and analysis

- Add a time-based MCP query for selecting metrics and a start/end timestamp,
  returning samples with their source timestamps and units when known. Support
  local-time interpretation for questions about parts of a day and make the
  relevant timezone explicit.
- Add a bounded summary/comparison path for caller-defined time windows over a
  date range (e.g. compare two daily windows). Include sample counts and make
  missing data visible; do not treat absent samples as zero or claim a
  comparison is reliable when coverage is inadequate.
- Do not silently aggregate granular samples to daily values. Any resampling
  or statistics beyond the native points should be explicit in the request or
  response. Preserve existing daily `query_metrics` behavior for compatibility.

### MCP network service and Docker

- Serve remote clients using MCP **Streamable HTTP**, the documented common
  remote transport for OpenCode V2 and Hermes. The current `stdio` mode should
  remain available for existing local MCP use and CLI workflows, connecting
  to the configured PostgreSQL service.
- Require a configured bearer token for network MCP requests. Read the secret
  from runtime configuration/environment; never bake it into the image, expose
  it in logs, or provide a weak default. Reject missing and invalid tokens.
- Provide a Docker/Compose deployment example with separate persistent
  PostgreSQL and app-data volumes, configurable port/schedule/timezone/token,
  and clear login/bootstrap steps. Schedule values must be visible and
  overridable in the Compose environment (for example via `.env`).
  Bind the published MCP port to a configurable host IP; default to loopback
  for safety and document setting it to the homelab's private LAN IP for remote
  clients. The published host port must also be configurable. Keep PostgreSQL
  private. The service should not be exposed publicly by default. Recommend
  HTTPS termination at a trusted reverse proxy if the traffic crosses an
  untrusted network.
- The recurring sync must not depend on an MCP client being connected. Coordinate
  scheduler writes and MCP reads safely with PostgreSQL and expose useful sync
  status/errors.

## Constraints and caveats

- Garmin Connect endpoints used by `python-garminconnect` are unofficial,
  private web-service interfaces, not a Garmin API contract. Endpoint fields,
  sample spacing, retention, and availability can vary by account/device and
  change without notice.
- The client returns some arrays raw; inspect representative payloads and
  validate timestamps, units, local/UTC semantics, and actual cadence before
  interpreting them. Preserve original payloads when normalization is
  uncertain.
- No guaranteed safe polling rate or same-day publication latency is known.
  Two daily syncs are the accepted target, not a promise that every sample is
  immediately available.
- A network bearer token protects the MCP endpoint but does not encrypt plain
  HTTP. Public internet exposure is out of scope.

## Non-goals

- Reconstructing data Garmin never exposed, fabricating samples, or promising
  complete historical minute-level coverage.
- Real-time streaming/alerts or frequent minute-level polling.
- Replacing Garmin Connect, changing the Garmin login flow, or automatically
  modifying the user’s Hermes/OpenCode configuration.
- Running a second writable SQLite warehouse, or adding TimescaleDB before
  ordinary PostgreSQL performance demonstrates a need.
- Removing the existing daily summaries or FIT import fallback.

## Acceptance evidence

- Fixtures containing representative granular payloads produce exact
  timestamped samples (including timezone and source metadata) with no
  interpolation; parser tests cover missing/variant arrays and repeated fetches.
- Query tests cover arbitrary ranges, day-window comparisons, missing coverage,
  bounded results, and preservation of existing daily query behavior.
- Sync tests show current-day refresh despite prior successful sync state,
  safe repeated ingestion, recoverable raw payload versions, retries/resumption,
  and visible endpoint errors.
- HTTP MCP tests prove Streamable HTTP initialization/tool calls work and that
  missing/wrong bearer tokens are rejected while a valid token succeeds.
- Docker deployment validation shows scheduled sync can run without a client,
  PostgreSQL and raw archives survive their respective container recreation
  through separate volumes, the database is not published outside Compose, and
  the authenticated endpoint is reachable from both client machines on the LAN.
- Migration tests import existing SQLite summaries (including FIT-derived
  rows) into PostgreSQL without changing or deleting the original database;
  the migrated row counts and coverage are reported.
- Documentation explains how to bootstrap credentials, configure schedules
  and token secrets, connect Hermes/OpenCode, inspect coverage, and understand
  Garmin API limitations.

## Builder decisions

The implementation planner should choose and document: a supported PostgreSQL
client/migration strategy; how to detect the oldest date each endpoint can
actually serve; the exact supported set of temporal metrics based on verified
payloads; normalized sample schema and correction semantics; safe SQLite import
and validation; scheduler process layout; time-zone policy; response-size/paging
limits; a conservative default request delay/backoff; and the minimal
authentication/HTTP configuration. Keep decisions within the goals and
constraints above, and surface any choice that would materially reduce data
retention, LAN security, or historical/time-window queryability before
implementation.
