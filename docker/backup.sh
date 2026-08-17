#!/bin/sh
# Nightly backup to the NFS-mounted /backups volume, run by supercronic.
# Keeps BACKUP_RETENTION_DAYS (default 31) generations.
set -eu

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="/backups/$STAMP"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-31}"

mkdir -p "$DEST"

echo "[backup] $(date -Is) pg_dump -> $DEST/garmin.dump"
pg_dump --dbname="$GARMIN_MCP_DATABASE_URL" --format=custom --file="$DEST/garmin.dump"

echo "[backup] raw snapshots -> $DEST/raw.tar.gz"
tar -czf "$DEST/raw.tar.gz" -C /data raw

echo "[backup] pruning backups older than $RETENTION_DAYS days"
find /backups -maxdepth 1 -type d -name '20*' -mtime +"$RETENTION_DAYS" -exec rm -rf {} +

echo "[backup] done: $DEST"
