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

ENV_FILE=${CORTHEX_ENV_FILE:-/etc/corthex/corthex.env}
PG_RESTORE=${PG_RESTORE:-pg_restore}
PSQL=${PSQL:-psql}
SOURCE_DATABASE_URL=""
if [[ -r "$ENV_FILE" ]]; then
  SOURCE_DATABASE_URL=$(python3 - "$ENV_FILE" <<'PY'
import sys
for line in open(sys.argv[1], encoding="utf-8"):
    if line.startswith("CORTHEX_DATABASE_URL="):
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
sha256sum -c "$CHECKSUM" >/dev/null
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
