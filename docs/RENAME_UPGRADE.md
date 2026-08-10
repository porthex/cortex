# Rename upgrade: Corthex to Cortex

This is the narrow compatibility procedure for a host installed before the product rename. The legacy spelling appears here only because the procedure must identify existing files, services, routes, environment keys, and the former Hindsight destination bank exactly. New installations must use only Cortex names.

Do not run the normal installer over a detected legacy deployment. It fails closed before mutation because both services use loopback port `8890`.

## 1. Capture rollback evidence

Run as root on the server while the existing service is healthy:

```bash
install -d -m 0700 /root/cortex-rename-rollback
cp -a /etc/corthex /root/cortex-rename-rollback/etc-corthex
cp -a /etc/systemd/system/corthex-remote.service /root/cortex-rename-rollback/
cp -a /var/lib/corthex /root/cortex-rename-rollback/var-lib-corthex
systemctl is-enabled corthex-remote.service > /root/cortex-rename-rollback/service-enabled.txt
systemctl is-active corthex-remote.service > /root/cortex-rename-rollback/service-active.txt
tailscale serve status --json > /root/cortex-rename-rollback/tailscale-serve.json
sha256sum /root/cortex-rename-rollback/corthex-remote.service > /root/cortex-rename-rollback/SHA256SUMS
```

Create and verify the PostgreSQL and Hindsight exports required by [Unified Cortex memory architecture](UNIFIED_MEMORY.md). The former product bank is `corthex`; treat it as an immutable migration source alongside `hermes` and the Windows `cortex` source. Apply the verified plan only to `cortex-shared`.

## 2. Carry credentials without printing them

Load the existing root-only files into the current root shell and map them to the new contract. Do not use `set -x`, paste values into command history, or relax file permissions.

```bash
set -a
. /etc/corthex/corthex.env
[[ ! -f /etc/corthex/backup.env ]] || . /etc/corthex/backup.env
set +a

export CORTEX_MCP_TOKEN="$CORTHEX_MCP_TOKEN"
export CORTEX_HINDSIGHT_URL="$CORTHEX_HINDSIGHT_URL"
export CORTEX_DATABASE_URL="${CORTHEX_DATABASE_URL:-}"
export CORTEX_BANKS_JSON='{"cortex-shared":"Cortex shared memory"}'
```

Before cutover, verify `cortex-shared` counts, provenance, retain/recall/reflect, and backups. Do not delete the former `corthex` bank.

## 3. Cut over transactionally

```bash
systemctl stop corthex-remote.service
sudo -E CORTEX_CONFIRMED_RENAME_UPGRADE=1 ./deploy/install-vps.sh "$PWD"

curl --fail --silent http://127.0.0.1:8890/health
CORTEX_MCP_TOKEN="$CORTEX_MCP_TOKEN" python3 deploy/check-local-auth.py \
  http://127.0.0.1:8890/v1/status
```

The installer creates the new `/opt/cortex`, `/etc/cortex`, and `/var/lib/cortex` surfaces and publishes `/cortex`. Only after the new local and tailnet checks pass:

```bash
systemctl disable corthex-remote.service
tailscale serve --yes --https=443 --set-path /corthex off
```

Keep the rollback archive and former bank until the owner closes the rollback window. Removing legacy files, units, or banks is a separate irreversible operation.

## 4. Roll back

If any new-path acceptance check fails, stop the new service and restore the old route/service without modifying memory:

```bash
systemctl stop cortex-remote.service
tailscale serve --yes --https=443 --set-path /cortex off
systemctl enable --now corthex-remote.service
tailscale serve --bg --yes --set-path /corthex http://127.0.0.1:8890
```

Verify old-path authentication and recall against the captured baseline before investigating the failed cutover.
