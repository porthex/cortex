#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  printf 'Run as root: sudo %q %q\n' "$0" "${1:-.}" >&2
  exit 2
fi

SOURCE_DIR=${1:-.}
SOURCE_DIR=$(realpath "$SOURCE_DIR")
[[ -f "$SOURCE_DIR/pyproject.toml" ]] || {
  echo "No pyproject.toml in $SOURCE_DIR; integrate the Cortex CLI/server branch first." >&2
  exit 2
}
command -v tailscale >/dev/null
command -v python3 >/dev/null
command -v openssl >/dev/null
command -v curl >/dev/null
command -v flock >/dev/null
tailscale status >/dev/null

# Fail closed before mutation: do not publish alongside Funnel or replace an owned route.
SERVE_STATUS=$(tailscale serve status --json)
python3 "$SOURCE_DIR/deploy/check-serve-private.py" --allow-owned-cortex <<<"$SERVE_STATUS"

getent group cortex >/dev/null || groupadd --system cortex
id -u cortex >/dev/null 2>&1 || useradd \
  --system --gid cortex --home-dir /var/lib/cortex --shell /usr/sbin/nologin cortex

install -d -o root -g root -m 0755 /opt/cortex /opt/cortex/releases
install -d -o root -g cortex -m 0750 /etc/cortex
install -d -o cortex -g cortex -m 0750 /var/lib/cortex /var/log/cortex

rm -f /opt/cortex/.venv.next
NEW_RELEASE="/opt/cortex/releases/$(date -u +%Y%m%dT%H%M%SZ)-$$"
RELEASE_READY=0
cleanup_release() {
  local rc=$?
  if [[ $RELEASE_READY -ne 1 ]]; then
    rm -f /opt/cortex/.venv.next
    rm -rf "$NEW_RELEASE"
  fi
  exit "$rc"
}
trap 'cleanup_release' EXIT
python3 -m venv "$NEW_RELEASE"
ln -s "$NEW_RELEASE" /opt/cortex/.venv.next
/opt/cortex/.venv.next/bin/pip install --disable-pip-version-check "$SOURCE_DIR"
/opt/cortex/.venv.next/bin/python -c 'import cortex.mcp_http'

read_existing() {
  local path=$1
  local key=$2
  [[ -r "$path" ]] || return 0
  python3 - "$path" "$key" <<'PY'
import sys
path, wanted = sys.argv[1:]
for line in open(path, encoding="utf-8"):
    if line.startswith(wanted + "="):
        print(line.split("=", 1)[1].rstrip("\n"))
        break
PY
}

RUNTIME_ENV=/etc/cortex/cortex.env
BACKUP_ENV=/etc/cortex/backup.env
UNIT_PATH=/etc/systemd/system/cortex-remote.service
existing_token=$(read_existing "$RUNTIME_ENV" CORTEX_MCP_TOKEN)
TOKEN=${CORTEX_MCP_TOKEN:-$existing_token}
[[ -n "$TOKEN" ]] || TOKEN=$(openssl rand -base64 48 | tr -d '\n')
existing_hindsight=$(read_existing "$RUNTIME_ENV" CORTEX_HINDSIGHT_URL)
HINDSIGHT_URL=${CORTEX_HINDSIGHT_URL:-${existing_hindsight:-http://127.0.0.1:9177}}
existing_banks=$(read_existing "$RUNTIME_ENV" CORTEX_BANKS_JSON)
BANKS_JSON=${CORTEX_BANKS_JSON:-${existing_banks:-'{"cortex":"Cortex memory"}'}}
BANKS_JSON=$(python3 -c 'import json,sys; value=json.loads(sys.argv[1]); assert isinstance(value,dict) and value; print(json.dumps(value,separators=(",",":")))' "$BANKS_JSON")
existing_database=$(read_existing "$BACKUP_ENV" CORTEX_DATABASE_URL)
# Migrate credentials from an earlier combined environment without exposing them to the service.
[[ -n "$existing_database" ]] || existing_database=$(read_existing "$RUNTIME_ENV" CORTEX_DATABASE_URL)
DATABASE_URL=${CORTEX_DATABASE_URL:-$existing_database}
HOSTNAME=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
ENV_TMP=$(mktemp)
BACKUP_TMP=$(mktemp)
INSTALL_MUTATED=0
INSTALL_COMPLETE=0
HAD_VENV=0
HAD_RUNTIME_ENV=0
HAD_BACKUP_ENV=0
HAD_UNIT=0
WAS_ENABLED=0
WAS_ACTIVE=0
PREVIOUS_RELEASE=""

cleanup_install() {
  rm -f "$ENV_TMP" "$BACKUP_TMP"
  rm -f /opt/cortex/.venv.next
  if [[ $INSTALL_COMPLETE -ne 1 ]]; then
    rm -rf "$NEW_RELEASE"
  fi
}

rollback_install() {
  local rc=$?
  set +e
  if [[ $INSTALL_MUTATED -eq 1 && $INSTALL_COMPLETE -ne 1 ]]; then
    systemctl stop cortex-remote.service >/dev/null 2>&1
    rm -rf /opt/cortex/.venv
    if [[ $HAD_VENV -eq 1 && -d /opt/cortex/.venv.previous ]]; then
      mv /opt/cortex/.venv.previous /opt/cortex/.venv
    fi
    rm -f "$RUNTIME_ENV"
    if [[ $HAD_RUNTIME_ENV -eq 1 && -f /etc/cortex/cortex.env.previous ]]; then
      mv /etc/cortex/cortex.env.previous "$RUNTIME_ENV"
    fi
    rm -f "$BACKUP_ENV"
    if [[ $HAD_BACKUP_ENV -eq 1 && -f /etc/cortex/backup.env.previous ]]; then
      mv /etc/cortex/backup.env.previous "$BACKUP_ENV"
    fi
    rm -f "$UNIT_PATH"
    if [[ $HAD_UNIT -eq 1 && -f /etc/systemd/system/cortex-remote.service.previous ]]; then
      mv /etc/systemd/system/cortex-remote.service.previous "$UNIT_PATH"
    fi
    systemctl daemon-reload >/dev/null 2>&1
    if [[ $WAS_ENABLED -eq 1 ]]; then
      systemctl enable cortex-remote.service >/dev/null 2>&1
    else
      systemctl disable cortex-remote.service >/dev/null 2>&1
    fi
    if [[ $WAS_ACTIVE -eq 1 && $HAD_VENV -eq 1 && $HAD_UNIT -eq 1 ]]; then
      systemctl restart cortex-remote.service >/dev/null 2>&1
    fi
    echo "Cortex installation failed; prior service files were restored" >&2
  fi
  cleanup_install
  exit "$rc"
}
RELEASE_READY=1
trap 'rollback_install' EXIT

{
  printf 'CORTEX_MCP_TOKEN=%s\n' "$TOKEN"
  printf 'CORTEX_HINDSIGHT_URL=%s\n' "$HINDSIGHT_URL"
  printf 'CORTEX_BANKS_JSON=%s\n' "$BANKS_JSON"
  printf 'CORTEX_MCP_PUBLIC_URL=https://%s/cortex/mcp\n' "$HOSTNAME"
  printf 'CORTEX_MCP_HOST=127.0.0.1\n'
  printf 'CORTEX_MCP_PORT=8890\n'
  printf 'CORTEX_STATE_DIR=/var/lib/cortex\n'
  printf 'CORTEX_LOG_DIR=/var/log/cortex\n'
} >"$ENV_TMP"
: >"$BACKUP_TMP"
if [[ -n "$DATABASE_URL" ]]; then
  printf 'CORTEX_DATABASE_URL=%s\n' "$DATABASE_URL" >"$BACKUP_TMP"
fi

systemctl is-enabled --quiet cortex-remote.service && WAS_ENABLED=1 || true
systemctl is-active --quiet cortex-remote.service && WAS_ACTIVE=1 || true
rm -rf /opt/cortex/.venv.previous
rm -f /etc/cortex/cortex.env.previous /etc/cortex/backup.env.previous \
  /etc/systemd/system/cortex-remote.service.previous
if [[ -e /opt/cortex/.venv || -L /opt/cortex/.venv ]]; then
  HAD_VENV=1
  PREVIOUS_RELEASE=$(readlink -f /opt/cortex/.venv || true)
  mv /opt/cortex/.venv /opt/cortex/.venv.previous
fi
if [[ -f "$RUNTIME_ENV" ]]; then
  HAD_RUNTIME_ENV=1
  cp -a "$RUNTIME_ENV" /etc/cortex/cortex.env.previous
fi
if [[ -f "$BACKUP_ENV" ]]; then
  HAD_BACKUP_ENV=1
  cp -a "$BACKUP_ENV" /etc/cortex/backup.env.previous
fi
if [[ -f "$UNIT_PATH" ]]; then
  HAD_UNIT=1
  cp -a "$UNIT_PATH" /etc/systemd/system/cortex-remote.service.previous
fi
INSTALL_MUTATED=1
mv -T /opt/cortex/.venv.next /opt/cortex/.venv
install -m 0600 -o root -g cortex "$ENV_TMP" "$RUNTIME_ENV"
install -m 0600 -o root -g root "$BACKUP_TMP" "$BACKUP_ENV"
install -m 0644 "$SOURCE_DIR/deploy/cortex-remote.service" "$UNIT_PATH"

systemctl daemon-reload
systemctl enable cortex-remote.service
systemctl restart cortex-remote.service

for _ in {1..30}; do
  if curl --fail --silent --show-error http://127.0.0.1:8890/health >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent --show-error http://127.0.0.1:8890/health >/dev/null
CORTEX_MCP_TOKEN="$TOKEN" python3 "$SOURCE_DIR/deploy/check-local-auth.py" \
  http://127.0.0.1:8890/v1/status

# Serialize Cortex installers and re-check immediately before claiming the route.
exec 9>/run/lock/cortex-tailscale-serve.lock
flock 9
SERVE_STATUS=$(tailscale serve status --json)
python3 "$SOURCE_DIR/deploy/check-serve-private.py" --allow-owned-cortex <<<"$SERVE_STATUS"
tailscale serve --bg --yes --set-path /cortex http://127.0.0.1:8890
flock -u 9
INSTALL_COMPLETE=1
rm -rf /opt/cortex/.venv.previous
if [[ -n "$PREVIOUS_RELEASE" && "$PREVIOUS_RELEASE" == /opt/cortex/releases/* ]]; then
  rm -rf "$PREVIOUS_RELEASE"
fi
rm -f /etc/cortex/cortex.env.previous /etc/cortex/backup.env.previous \
  /etc/systemd/system/cortex-remote.service.previous

printf 'Cortex is active at https://%s/cortex\n' "$HOSTNAME"
printf 'Bearer token stored in /etc/cortex/cortex.env (mode 0600); it was not printed.\n'
