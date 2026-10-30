# Task 8 report: configured LAN Host allowlist

## Implementation

- `config.http_allowed_hosts()` validates `GARMIN_MCP_LAN_IP` as an IP address
  and `GARMIN_MCP_HTTP_PORT` as a valid TCP port, then returns that exact host
  and port plus IPv4/IPv6/localhost loopback aliases.
- `create_http_app(mcp_server, expected_token, allowed_hosts)` installs
  `TransportSecuritySettings` before `streamable_http_app()` is built. DNS
  rebinding protection stays enabled; exact origins are derived from the host
  list, and wildcard hosts are rejected. Bearer middleware remains outside the
  MCP app.
- `serve_http` supplies the runtime allowlist. Compose passes LAN IP and
  published host port only to `mcp`; the worker continues to receive neither
  these values nor the HTTP token.
- Updated `.env.example` and README with the configured-IP/port restriction,
  loopback behavior, and remote-client URL requirements.
- Added HTTP integration coverage for successful LAN Host initialization and
  tool call, rejection of unlisted Host (421), missing/wrong Bearer (401),
  localhost use, runtime configuration, and Compose environment isolation.

## TDD evidence

- Tests were added before implementation. The first focused run showed the
  Compose test failing because the MCP environment omitted
  `GARMIN_MCP_LAN_IP` (`KeyError`); HTTP tests could not run then because this
  environment had no `GARMIN_MCP_TEST_DATABASE_URL`.
- With a local disposable PostgreSQL test container available, the LAN-host
  test was run with the configured LAN host intentionally omitted from the test
  allowlist. It failed as expected with `421 Misdirected Request` and the SDK
  log `Invalid Host header: 192.168.1.42:<test-port>`. The allowlist was then
  restored and the focused tests passed.

## Validation outputs

- Focused: `GARMIN_MCP_TEST_DATABASE_URL=<local test DB> uv run pytest tests/test_http_server.py tests/test_deployment.py -q`
  - `14 passed, 1 warning in 1.07s`.
- Full PostgreSQL suite: `GARMIN_MCP_TEST_DATABASE_URL=<local test DB> uv run pytest -q`
  - `202 passed, 1 skipped, 1 warning in 33.83s`.
- Ruff: `ruff check .`
  - `All checks passed!`
- Lock: `uv lock --check`
  - `Resolved 48 packages in 0.66ms`.
- Compose: `docker compose --env-file .env.example config --quiet`
  - Passed with no output.
- Whitespace: `git diff --check`
  - Passed with no output.
- HTTP smoke coverage was exercised through the local Uvicorn integration test:
  configured LAN Host + valid Bearer initialized MCP and called `auth_status`;
  an unlisted Host returned 421. Missing/wrong Bearer returned 401, and the
  loopback client remained usable.
- PostgreSQL ran only in a temporary local Docker container for tests; it was
  removed afterward. No homelab or user credentials were accessed.

## Review / concerns

- Existing third-party AnyIO pytest plugin emits a
  `PytestRemovedIn10Warning`; it does not affect test results.
- The one skipped test is part of the existing suite; no Task 8 checks were
  skipped.

Commit: `fix: allow configured LAN host for MCP HTTP` (hash reported at handoff).
