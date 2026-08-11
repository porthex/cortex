#!/usr/bin/env bash
set -euo pipefail
umask 077

ENV_FILE=${CORTEX_BACKUP_ENV_FILE:-/etc/cortex/backup.env}
BACKUP_DIR=${CORTEX_BACKUP_DIR:-/var/lib/cortex/backups}
PSQL=${PSQL:-psql}
PG_DUMP_IS_EXPLICIT=false
if [[ ${PG_DUMP+x} ]]; then
  PG_DUMP_IS_EXPLICIT=true
fi
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
export PGDATABASE=$DATABASE_URL
unset DATABASE_URL

SERVER_VERSION_NUM=$("$PSQL" -X -Atqc 'SHOW server_version_num' 2>/dev/null) || {
  echo "Cannot determine the PostgreSQL server version with $PSQL" >&2
  exit 2
}
[[ "$SERVER_VERSION_NUM" =~ ^[0-9]+$ ]] || {
  echo "PostgreSQL returned an invalid server_version_num" >&2
  exit 2
}
SERVER_MAJOR=$((SERVER_VERSION_NUM / 10000))

SUDO_HOME=""
if [[ -n ${SUDO_USER:-} ]]; then
  SUDO_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6 || true)
fi
candidates=()
if $PG_DUMP_IS_EXPLICIT; then
  explicit_pg_dump=$PG_DUMP
  if [[ "$explicit_pg_dump" != */* ]]; then
    explicit_pg_dump=$(command -v "$explicit_pg_dump" 2>/dev/null || true)
  fi
  candidates+=("$explicit_pg_dump")
else
  if PATH_PG_DUMP=$(command -v pg_dump 2>/dev/null); then
    candidates+=("$PATH_PG_DUMP")
  fi
  pg0_homes=("$HOME")
  if [[ -n "$SUDO_HOME" ]]; then
    pg0_homes+=("$SUDO_HOME")
  fi
  shopt -s nullglob
  for pg0_home in "${pg0_homes[@]}"; do
    candidates+=("$pg0_home"/.pg0/installation/*/bin/pg_dump)
  done
  shopt -u nullglob
fi

SELECTED_PG_DUMP=""
SELECTED_AS_SUDO_USER=false
for candidate in "${candidates[@]}"; do
  [[ -x "$candidate" ]] || continue
  candidate_as_sudo_user=false
  if ((EUID == 0)) && [[ -n ${SUDO_USER:-} && -n "$SUDO_HOME" &&
      "$candidate" == "$SUDO_HOME"/.pg0/installation/*/bin/pg_dump ]]; then
    candidate_as_sudo_user=true
    version_output=$(sudo -u "$SUDO_USER" -- "$candidate" --version 2>/dev/null) || continue
  else
    version_output=$("$candidate" --version 2>/dev/null) || continue
  fi
  if [[ "$version_output" =~ PostgreSQL\)\ ([0-9]+)(\.[0-9]+)? ]]; then
    candidate_major=${BASH_REMATCH[1]}
    if ((candidate_major >= SERVER_MAJOR)); then
      SELECTED_PG_DUMP=$candidate
      SELECTED_AS_SUDO_USER=$candidate_as_sudo_user
      break
    fi
  fi
done
[[ -n "$SELECTED_PG_DUMP" ]] || {
  if $PG_DUMP_IS_EXPLICIT; then
    echo "PG_DUMP '$PG_DUMP' is not executable or is incompatible with PostgreSQL server major $SERVER_MAJOR" >&2
  else
    echo "No pg_dump compatible with PostgreSQL server major $SERVER_MAJOR was found on PATH or in a pg0 installation" >&2
    echo "Install the PostgreSQL $SERVER_MAJOR client or set PG_DUMP to its pg_dump executable" >&2
  fi
  exit 2
}

install -d -m 0700 "$BACKUP_DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FINAL="$BACKUP_DIR/cortex-$STAMP.dump"
TEMP="$FINAL.tmp"
trap 'rm -f "$TEMP"' EXIT

if $SELECTED_AS_SUDO_USER; then
  dump_command=(sudo --preserve-env=PGDATABASE -u "$SUDO_USER" -- "$SELECTED_PG_DUMP")
else
  dump_command=("$SELECTED_PG_DUMP")
fi
if ! "${dump_command[@]}" --format=custom --compress=9 >"$TEMP" 2>/dev/null; then
  echo "Backup failed with compatible pg_dump '$SELECTED_PG_DUMP'; no archive was created" >&2
  exit 1
fi
chmod 0600 "$TEMP"
mv "$TEMP" "$FINAL"
sha256sum "$FINAL" >"$FINAL.sha256"
chmod 0600 "$FINAL.sha256"
printf '%s\n' "$FINAL"
