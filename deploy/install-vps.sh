#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  printf 'Run as root: sudo %q %q\n' "$0" "${1:-.}" >&2
  exit 2
fi

SOURCE_DIR=${1:-.}
SOURCE_DIR=$(realpath "$SOURCE_DIR")
[[ -f "$SOURCE_DIR/pyproject.toml" ]] || {
  echo "No pyproject.toml in $SOURCE_DIR; integrate the Corthex CLI/server branch first." >&2
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
python3 "$SOURCE_DIR/deploy/check-serve-private.py" --allow-owned-corthex <<<"$SERVE_STATUS"

getent group corthex >/dev/null || groupadd --system corthex
id -u corthex >/dev/null 2>&1 || useradd \
  --system --gid corthex --home-dir /var/lib/corthex --shell /usr/sbin/nologin corthex

install -d -o root -g root -m 0755 /opt/corthex
install -d -o root -g corthex -m 0750 /etc/corthex
install -d -o corthex -g corthex -m 0750 /var/lib/corthex /var/log/corthex

rm -rf /opt/corthex/.venv.next
python3 -m venv /opt/corthex/.venv.next
/opt/corthex/.venv.next/bin/pip install --disable-pip-version-check "$SOURCE_DIR"
/opt/corthex/.venv.next/bin/python -c 'import corthex.mcp_http'

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

RUNTIME_ENV=/etc/corthex/corthex.env
BACKUP_ENV=/etc/corthex/backup.env
UNIT_PATH=/etc/systemd/system/corthex-remote.service
existing_token=$(read_existing "$RUNTIME_ENV" CORTHEX_MCP_TOKEN)
TOKEN=${CORTHEX_MCP_TOKEN:-$existing_token}
[[ -n "$TOKEN" ]] || TOKEN=$(openssl rand -base64 48 | tr -d '\n')
existing_hindsight=$(read_existing "$RUNTIME_ENV" CORTHEX_HINDSIGHT_URL)
HINDSIGHT_URL=${CORTHEX_HINDSIGHT_URL:-${existing_hindsight:-http://127.0.0.1:9177}}
existing_banks=$(read_existing "$RUNTIME_ENV" CORTHEX_BANKS_JSON)
BANKS_JSON=${CORTHEX_BANKS_JSON:-${existing_banks:-'{"corthex":"Corthex memory"}'}}
BANKS_JSON=$(python3 -c 'import json,sys; value=json.loads(sys.argv[1]); assert isinstance(value,dict) and value; print(json.dumps(value,separators=(",",":")))' "$BANKS_JSON")
existing_database=$(read_existing "$BACKUP_ENV" CORTHEX_DATABASE_URL)
# Migrate credentials from an earlier combined environment without exposing them to the service.
[[ -n "$existing_database" ]] || existing_database=$(read_existing "$RUNTIME_ENV" CORTHEX_DATABASE_URL)
DATABASE_URL=${CORTHEX_DATABASE_URL:-$existing_database}
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

cleanup_install() {
  rm -f "$ENV_TMP" "$BACKUP_TMP"
  rm -rf /opt/corthex/.venv.next
}

rollback_install() {
  local rc=$?
  set +e
  if [[ $INSTALL_MUTATED -eq 1 && $INSTALL_COMPLETE -ne 1 ]]; then
    systemctl stop corthex-remote.service >/dev/null 2>&1
    rm -rf /opt/corthex/.venv
    if [[ $HAD_VENV -eq 1 && -d /opt/corthex/.venv.previous ]]; then
      mv /opt/corthex/.venv.previous /opt/corthex/.venv
    fi
    rm -f "$RUNTIME_ENV"
    if [[ $HAD_RUNTIME_ENV -eq 1 && -f /etc/corthex/corthex.env.previous ]]; then
      mv /etc/corthex/corthex.env.previous "$RUNTIME_ENV"
    fi
    rm -f "$BACKUP_ENV"
    if [[ $HAD_BACKUP_ENV -eq 1 && -f /etc/corthex/backup.env.previous ]]; then
      mv /etc/corthex/backup.env.previous "$BACKUP_ENV"
    fi
    rm -f "$UNIT_PATH"
    if [[ $HAD_UNIT -eq 1 && -f /etc/systemd/system/corthex-remote.service.previous ]]; then
      mv /etc/systemd/system/corthex-remote.service.previous "$UNIT_PATH"
    fi
    systemctl daemon-reload >/dev/null 2>&1
    if [[ $WAS_ENABLED -eq 1 ]]; then
      systemctl enable corthex-remote.service >/dev/null 2>&1
    else
      systemctl disable corthex-remote.service >/dev/null 2>&1
    fi
    if [[ $WAS_ACTIVE -eq 1 && $HAD_VENV -eq 1 && $HAD_UNIT -eq 1 ]]; then
      systemctl restart corthex-remote.service >/dev/null 2>&1
    fi
    echo "Corthex installation failed; prior service files were restored" >&2
  fi
  cleanup_install
  exit "$rc"
}
trap 'rollback_install' EXIT

{
  printf 'CORTHEX_MCP_TOKEN=%s\n' "$TOKEN"
  printf 'CORTHEX_HINDSIGHT_URL=%s\n' "$HINDSIGHT_URL"
  printf 'CORTHEX_BANKS_JSON=%s\n' "$BANKS_JSON"
  printf 'CORTHEX_MCP_PUBLIC_URL=https://%s/corthex/mcp\n' "$HOSTNAME"
  printf 'CORTHEX_MCP_HOST=127.0.0.1\n'
  printf 'CORTHEX_MCP_PORT=8890\n'
  printf 'CORTHEX_STATE_DIR=/var/lib/corthex\n'
  printf 'CORTHEX_LOG_DIR=/var/log/corthex\n'
} >"$ENV_TMP"
: >"$BACKUP_TMP"
if [[ -n "$DATABASE_URL" ]]; then
  printf 'CORTHEX_DATABASE_URL=%s\n' "$DATABASE_URL" >"$BACKUP_TMP"
fi

systemctl is-enabled --quiet corthex-remote.service && WAS_ENABLED=1 || true
systemctl is-active --quiet corthex-remote.service && WAS_ACTIVE=1 || true
rm -rf /opt/corthex/.venv.previous
rm -f /etc/corthex/corthex.env.previous /etc/corthex/backup.env.previous \
  /etc/systemd/system/corthex-remote.service.previous
if [[ -d /opt/corthex/.venv ]]; then
  HAD_VENV=1
  mv /opt/corthex/.venv /opt/corthex/.venv.previous
fi
if [[ -f "$RUNTIME_ENV" ]]; then
  HAD_RUNTIME_ENV=1
  cp -a "$RUNTIME_ENV" /etc/corthex/corthex.env.previous
fi
if [[ -f "$BACKUP_ENV" ]]; then
  HAD_BACKUP_ENV=1
  cp -a "$BACKUP_ENV" /etc/corthex/backup.env.previous
fi
if [[ -f "$UNIT_PATH" ]]; then
  HAD_UNIT=1
  cp -a "$UNIT_PATH" /etc/systemd/system/corthex-remote.service.previous
fi
INSTALL_MUTATED=1
mv /opt/corthex/.venv.next /opt/corthex/.venv
install -m 0600 -o root -g corthex "$ENV_TMP" "$RUNTIME_ENV"
install -m 0600 -o root -g root "$BACKUP_TMP" "$BACKUP_ENV"
install -m 0644 "$SOURCE_DIR/deploy/corthex-remote.service" "$UNIT_PATH"

systemctl daemon-reload
systemctl enable corthex-remote.service
systemctl restart corthex-remote.service

for _ in {1..30}; do
  if curl --fail --silent --show-error http://127.0.0.1:8890/health >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent --show-error http://127.0.0.1:8890/health >/dev/null
CORTHEX_MCP_TOKEN="$TOKEN" python3 "$SOURCE_DIR/deploy/check-local-auth.py" \
  http://127.0.0.1:8890/v1/status

# Serialize Corthex installers and re-check immediately before claiming the route.
exec 9>/run/lock/corthex-tailscale-serve.lock
flock 9
SERVE_STATUS=$(tailscale serve status --json)
python3 "$SOURCE_DIR/deploy/check-serve-private.py" --allow-owned-corthex <<<"$SERVE_STATUS"
tailscale serve --bg --yes --set-path /corthex http://127.0.0.1:8890
flock -u 9
INSTALL_COMPLETE=1
rm -rf /opt/corthex/.venv.previous
rm -f /etc/corthex/corthex.env.previous /etc/corthex/backup.env.previous \
  /etc/systemd/system/corthex-remote.service.previous

printf 'Corthex is active at https://%s/corthex\n' "$HOSTNAME"
printf 'Bearer token stored in /etc/corthex/corthex.env (mode 0600); it was not printed.\n'
