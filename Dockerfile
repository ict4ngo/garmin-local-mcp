# One image, three roles (see docker-compose.yml):
#   app    : garmin-local-mcp serve --transport streamable-http
#   sync   : supercronic running docker/sync.crontab (nightly sync + prune)
#   backup : supercronic running docker/backup.crontab (pg_dump + raw -> NFS)
#
# The /data volume holds raw snapshots + Garmin tokens and survives restarts:
#   docker build -t garmin-local-mcp .
#   docker run -i -v garmin-data:/data garmin-local-mcp
FROM python:3.12-slim

# postgresql-client ships pg_dump for the backup job; curl fetches supercronic.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# supercronic: cron built for containers (foreground, stdout logging).
ARG TARGETARCH
ENV SUPERCRONIC_VERSION=v0.2.33
RUN case "$TARGETARCH" in \
      amd64) sc_arch=amd64 ;; \
      arm64) sc_arch=arm64 ;; \
      *) echo "unsupported arch $TARGETARCH for supercronic" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${sc_arch}" \
       -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY docker ./docker
RUN chmod +x ./docker/*.sh

ENV GARMIN_MCP_DATA_DIR=/data
VOLUME ["/data"]

ENTRYPOINT ["garmin-local-mcp"]
CMD ["serve", "--transport", "streamable-http"]
