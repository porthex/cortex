#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 ARCHIVE TARGET_DATABASE_URL --confirm-empty-target" >&2
  exit 2
}

[[ $# -eq 3 ]] || usage
ARCHIVE=$1
TARGET_DATABASE_URL=$2
[[ $3 == --confirm-empty-target ]] || usage
[[ -r "$ARCHIVE" ]] || { echo "Archive is not readable: $ARCHIVE" >&2; exit 2; }

ENV_FILE=${CORTEX_BACKUP_ENV_FILE:-/etc/cortex/backup.env}
PG_RESTORE=${PG_RESTORE:-pg_restore}
PSQL=${PSQL:-psql}
SOURCE_DATABASE_URL=""
if [[ -r "$ENV_FILE" ]]; then
  SOURCE_DATABASE_URL=$(python3 - "$ENV_FILE" <<'PY'
import sys
for line in open(sys.argv[1], encoding="utf-8"):
    if line.startswith("CORTEX_DATABASE_URL="):
        print(line.split("=", 1)[1].rstrip("\n"))
        break
PY
)
fi
[[ -z "$SOURCE_DATABASE_URL" || "$TARGET_DATABASE_URL" != "$SOURCE_DATABASE_URL" ]] || {
  echo "Refusing to restore over the configured source database" >&2
  exit 1
}

CHECKSUM="$ARCHIVE.sha256"
[[ -r "$CHECKSUM" ]] || { echo "Checksum file is not readable: $CHECKSUM" >&2; exit 2; }
EXPECTED_CHECKSUM=$(python3 - "$CHECKSUM" <<'PY'
import re
import sys

fields = open(sys.argv[1], encoding="utf-8").read().split()
if not fields or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
    raise SystemExit("Checksum file does not contain a SHA-256 digest")
print(fields[0].lower())
PY
)
ACTUAL_CHECKSUM=$(sha256sum "$ARCHIVE" | python3 -c 'import sys; print(sys.stdin.read().split()[0].lower())')
[[ "$ACTUAL_CHECKSUM" == "$EXPECTED_CHECKSUM" ]] || {
  echo "Archive checksum does not match: $ARCHIVE" >&2
  exit 1
}
"$PG_RESTORE" --list "$ARCHIVE" >/dev/null
USER_TABLES=$("$PSQL" "$TARGET_DATABASE_URL" -X -Atqc \
  "select count(*) from pg_catalog.pg_tables where schemaname not in ('pg_catalog','information_schema');")
[[ "$USER_TABLES" == "0" ]] || {
  echo "Target is not empty; restore is refused" >&2
  exit 1
}

"$PG_RESTORE" --dbname="$TARGET_DATABASE_URL" --exit-on-error --single-transaction --no-owner --no-privileges "$ARCHIVE"
"$PSQL" "$TARGET_DATABASE_URL" -X -Atqc \
  "select count(*) from pg_catalog.pg_tables where schemaname not in ('pg_catalog','information_schema');"
