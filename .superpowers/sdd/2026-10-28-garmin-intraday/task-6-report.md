# Task 6: Bearer-protected Streamable HTTP MCP service

## Result

Implemented authenticated Streamable HTTP MCP at `/mcp` using the existing global FastMCP instance, its session-manager lifespan, stateless JSON responses, Starlette Bearer middleware, and Uvicorn. HTTP startup now requires a non-empty `GARMIN_MCP_HTTP_BEARER_TOKEN`; token comparison uses `hmac.compare_digest`. The existing stdio `serve` command and tool registrations remain available. Added `serve-http` host/port flags and direct Starlette/Uvicorn dependencies while retaining `mcp>=1.30,<2`.

## TDD evidence

- **RED:** Before adding the HTTP app, the requested focused pytest command failed during collection with `ModuleNotFoundError: garmin_mcp.http_auth`, demonstrating the missing implementation. The CLI test also failed with argparse rejecting the absent `serve-http` command.
- **GREEN:** After implementation, focused HTTP and server tests passed. They exercise missing and wrong token 401s, actual MCP initialize/list-tools/tool-call over Uvicorn, required token startup, CLI bind defaults/overrides, and stdio initialize/list-tools/tool-call compatibility.
- The first server fixture attempt exposed the SDK's one-shot session-manager lifespan; the integration fixture was changed to run one module-scoped server lifecycle, after which the protocol tests passed.

## Verification

- Focused: `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test ./.venv/bin/uv run pytest tests/test_http_server.py tests/test_server.py -q` — **21 passed**.
- Full PostgreSQL suite: `GARMIN_MCP_TEST_DATABASE_URL=postgresql://garmin:garmin@localhost:55432/garmin_test ./.venv/bin/uv run pytest -q` — **179 passed, 1 skipped**.
- `./.venv/bin/uv run ruff check .` — passed.
- `./.venv/bin/uv lock --check` — passed.
- `git diff --check` — passed.

Pytest reports one existing AnyIO plugin deprecation warning (`CallSpec2` renamed to `CallSpec`); no test failures resulted.

## Scope

No Compose or README changes. HTTP authentication uses a manually provisioned shared token; no OAuth metadata or issuer was added, and the token is not logged.

Commit: `feat: serve authenticated MCP over HTTP`.
