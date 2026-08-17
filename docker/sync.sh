#!/bin/sh
# Nightly/deep Garmin sync, run by supercronic in the sync container.
#   sync.sh nightly  -> default range (last 7 days ending yesterday)
#   sync.sh deep     -> last 30 days, filling any gaps or retried errors
set -eu

MODE="${1:-nightly}"
echo "[sync] $(date -Is) starting $MODE sync"

if [ "$MODE" = "deep" ]; then
  FROM="$(date -d '30 days ago' +%Y-%m-%d)"
  garmin-local-mcp sync --from "$FROM"
else
  garmin-local-mcp sync
fi

echo "[sync] coverage after run:"
garmin-local-mcp status
