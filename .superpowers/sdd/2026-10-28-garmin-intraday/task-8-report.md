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

## Round 1: Restrict LAN bind addresses

### Finding and change

- Addressed the open finding: `GARMIN_MCP_LAN_IP` must now be a loopback
  address, an RFC 1918 IPv4 private-unicast address, or an IPv6 ULA address.
  This rejects global/public addresses, unspecified/wildcard addresses,
  multicast, reserved/documentation ranges, and link-local addresses before
  composing the HTTP Host allowlist. The exact configured Host+port allowlist,
  loopback aliases, and enabled DNS-rebinding protection are unchanged.
- The reviewer noted that `create_http_app` can accept arbitrary explicit
  hostnames if called directly. This remains deferred: production calls it only
  with the validated result of `config.http_allowed_hosts()`; no general
  hostname/DNS validation was added.

### TDD evidence

- Added `test_config_rejects_public_or_unsafe_lan_ips` first, parameterized for
  valid private IPv4/IPv6 and loopback addresses, and public, unspecified,
  multicast, reserved/documentation, and link-local IPv4/IPv6 addresses.
- RED: `uv run pytest tests/test_http_server.py::test_config_rejects_public_or_unsafe_lan_ips -q`
  before implementation reported `10 failed, 5 passed`; unsafe values did not
  raise `ValueError`.
- GREEN after implementation: same command reported `15 passed, 1 warning`.

### Round 1 validation outputs

- Focused HTTP/deployment: `GARMIN_MCP_TEST_DATABASE_URL=<local test DB> uv run pytest tests/test_http_server.py tests/test_deployment.py -q`
  - `29 passed, 1 warning in 1.05s`.
- Full PostgreSQL suite: `GARMIN_MCP_TEST_DATABASE_URL=<local test DB> uv run pytest -q`
  - `217 passed, 1 skipped, 1 warning in 34.02s`.
- Ruff: `ruff check .` — `All checks passed!`
- Lock: `uv lock --check` — `Resolved 48 packages in 0.71ms`.
- Compose: `docker compose --env-file .env.example config --quiet` — passed
  with no output.
- Whitespace: `git diff --check` — passed with no output.
- The temporary local PostgreSQL Docker container was removed after testing.
  No homelab or user credentials were accessed.

Round 1 commit: `fix: restrict MCP HTTP LAN bind addresses` (hash reported at
handoff).
