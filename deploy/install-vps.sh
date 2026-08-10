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
tailscale status >/dev/null

# Inspect before modifying: adding /corthex must not replace an existing root handler.
SERVE_STATUS=$(tailscale serve status --json)
python3 "$SOURCE_DIR/deploy/check-serve-private.py" <<<"$SERVE_STATUS"

getent group corthex >/dev/null || groupadd --system corthex
id -u corthex >/dev/null 2>&1 || useradd \
  --system --gid corthex --home-dir /var/lib/corthex --shell /usr/sbin/nologin corthex

install -d -o root -g root -m 0755 /opt/corthex
install -d -o root -g corthex -m 0750 /etc/corthex
install -d -o corthex -g corthex -m 0750 /var/lib/corthex /var/log/corthex

rm -rf /opt/corthex/.venv.next
python3 -m venv /opt/corthex/.venv.next
/opt/corthex/.venv.next/bin/pip install --disable-pip-version-check "$SOURCE_DIR"
/opt/corthex/.venv.next/bin/corthex --help >/dev/null

read_existing() {
  local key=$1
  [[ -r /etc/corthex/corthex.env ]] || return 0
  python3 - /etc/corthex/corthex.env "$key" <<'PY'
import sys
path, wanted = sys.argv[1:]
for line in open(path, encoding="utf-8"):
    if line.startswith(wanted + "="):
        print(line.split("=", 1)[1].rstrip("\n"))
        break
PY
}

existing_token=$(read_existing CORTHEX_TOKEN)
TOKEN=${CORTHEX_TOKEN:-$existing_token}
[[ -n "$TOKEN" ]] || TOKEN=$(openssl rand -base64 48 | tr -d '\n')
existing_hindsight=$(read_existing CORTHEX_HINDSIGHT_URL)
HINDSIGHT_URL=${CORTHEX_HINDSIGHT_URL:-${existing_hindsight:-http://127.0.0.1:9177}}
existing_banks=$(read_existing CORTHEX_ALLOWED_BANKS)
ALLOWED_BANKS=${CORTHEX_ALLOWED_BANKS:-${existing_banks:-corthex}}
existing_database=$(read_existing CORTHEX_DATABASE_URL)
DATABASE_URL=${CORTHEX_DATABASE_URL:-$existing_database}
ENV_TMP=$(mktemp)
trap 'rm -f "$ENV_TMP"; rm -rf /opt/corthex/.venv.next' EXIT
{
  printf 'CORTHEX_TOKEN=%s\n' "$TOKEN"
  printf 'CORTHEX_HINDSIGHT_URL=%s\n' "$HINDSIGHT_URL"
  printf 'CORTHEX_ALLOWED_BANKS=%s\n' "$ALLOWED_BANKS"
  printf 'CORTHEX_STATE_DIR=/var/lib/corthex\n'
  printf 'CORTHEX_LOG_DIR=/var/log/corthex\n'
  if [[ -n "$DATABASE_URL" ]]; then
    printf 'CORTHEX_DATABASE_URL=%s\n' "$DATABASE_URL"
  fi
} >"$ENV_TMP"
install -m 0600 -o root -g corthex "$ENV_TMP" /etc/corthex/corthex.env
install -m 0644 "$SOURCE_DIR/deploy/corthex-remote.service" /etc/systemd/system/corthex-remote.service

rm -rf /opt/corthex/.venv.previous
if [[ -d /opt/corthex/.venv ]]; then
  mv /opt/corthex/.venv /opt/corthex/.venv.previous
fi
mv /opt/corthex/.venv.next /opt/corthex/.venv

systemctl daemon-reload
systemctl enable corthex-remote.service
if ! systemctl restart corthex-remote.service; then
  if [[ -d /opt/corthex/.venv.previous ]]; then
    rm -rf /opt/corthex/.venv
    mv /opt/corthex/.venv.previous /opt/corthex/.venv
    systemctl restart corthex-remote.service || true
  fi
  echo "Corthex failed to start; previous package was restored when available" >&2
  exit 1
fi

for _ in {1..30}; do
  if curl --fail --silent --show-error http://127.0.0.1:8890/health >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent --show-error http://127.0.0.1:8890/health >/dev/null

CORTHEX_TOKEN="$TOKEN" python3 "$SOURCE_DIR/deploy/check-local-auth.py" \
  http://127.0.0.1:8890/v1/status

# This only adds the scoped handler; never reset the pre-existing Serve map.
tailscale serve --bg --yes --set-path /corthex http://127.0.0.1:8890

HOSTNAME=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
printf 'Corthex is active at https://%s/corthex\n' "$HOSTNAME"
printf 'Bearer token stored in /etc/corthex/corthex.env (mode 0600); it was not printed.\n'
