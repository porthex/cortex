#!/usr/bin/env bash
set -euo pipefail
umask 077

ENV_FILE=${CORTEX_BACKUP_ENV_FILE:-/etc/cortex/backup.env}
BACKUP_DIR=${CORTEX_BACKUP_DIR:-/var/lib/cortex/backups}
PG_DUMP=${PG_DUMP:-pg_dump}
[[ -r "$ENV_FILE" ]] || { echo "Cannot read $ENV_FILE" >&2; exit 2; }

DATABASE_URL=$(python3 - "$ENV_FILE" <<'PY'
import sys
for line in open(sys.argv[1], encoding="utf-8"):
    if line.startswith("CORTEX_DATABASE_URL="):
        print(line.split("=", 1)[1].rstrip("\n"))
        break
PY
)
[[ -n "$DATABASE_URL" ]] || { echo "CORTEX_DATABASE_URL is not configured" >&2; exit 2; }

install -d -m 0700 "$BACKUP_DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FINAL="$BACKUP_DIR/cortex-$STAMP.dump"
TEMP="$FINAL.tmp"
trap 'rm -f "$TEMP"' EXIT

"$PG_DUMP" --dbname="$DATABASE_URL" --format=custom --compress=9 --file="$TEMP"
chmod 0600 "$TEMP"
mv "$TEMP" "$FINAL"
sha256sum "$FINAL" >"$FINAL.sha256"
chmod 0600 "$FINAL.sha256"
printf '%s\n' "$FINAL"
