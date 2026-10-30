# The image includes the CLI for stdio, HTTP, worker, login, and migration use.
# Compose runs the authenticated HTTP server and worker with shared persistent
# app data; CLI commands can be run with `docker compose run --rm mcp <command>`.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV GARMIN_MCP_DATA_DIR=/data
VOLUME ["/data"]

ENTRYPOINT ["garmin-local-mcp"]
CMD ["serve"]
