# Remote Brain VPS runbook

This runbook installs the public Corthex CLI/server package behind tailnet-only HTTPS. It never exposes raw Hindsight or PostgreSQL. Replace placeholders locally; do not commit credentials or live host identifiers.

Prerequisites:

- Ubuntu with systemd, Python 3.11+, `python3-venv`, `curl`, `openssl`, and PostgreSQL client tools.
- Tailscale installed, connected, MagicDNS/HTTPS enabled, and access policy restricted to intended clients.
- Hindsight reachable only on loopback. Its database URL must be available to the operator for backup; clients never receive it.
- A checked-out Corthex revision containing `pyproject.toml` and the `corthex-mcp-http` entry point.

## Install

Inspect first:

```bash
sudo ss -lntup
sudo ufw status verbose
sudo tailscale status
sudo tailscale serve status --json
```

Install from the exact reviewed checkout:

```bash
sudo CORTHEX_HINDSIGHT_URL=http://127.0.0.1:9177 \
  CORTHEX_BANKS_JSON='{"corthex":"Corthex memory"}' \
  CORTHEX_DATABASE_URL='postgresql://…' \
  ./deploy/install-vps.sh "$PWD"
```

The installer:

1. refuses a public Funnel configuration;
2. creates the non-login `corthex` account;
3. installs the package into `/opt/corthex/.venv`;
4. stores generated `CORTHEX_MCP_TOKEN` material and the validated MCP runtime contract in `/etc/corthex/corthex.env` with mode `0600`, while isolating the database backup credential in root-only `/etc/corthex/backup.env`;
5. enables `corthex-remote.service` on `127.0.0.1:8890`;
6. refuses to replace a conflicting `/corthex` handler, recognizes its exact loopback target during upgrades, then adds only that Tailscale Serve path while preserving other routes.

Read `CORTHEX_MCP_TOKEN` only on the host and move it through an approved secret store. Do not paste it into issues, logs, shell history, or client configuration files that are synced. Upgrades preserve the existing token and `CORTHEX_BANKS_JSON` unless the operator explicitly supplies replacements. The service account cannot read `backup.env`; backup and restore commands run as root. A failed restart, health/auth probe, or Serve update restores the prior package, environment, and unit.

## Verify

```bash
sudo systemctl is-enabled corthex-remote.service
sudo systemctl is-active corthex-remote.service
sudo ss -lntp | grep ':8890'
sudo tailscale serve status --json
```

The installer performs three loopback status probes before adding the Serve route: no credentials and an invalid credential must both return 401, while the generated credential must return 2xx. If any result differs, installation stops before remote ingress is changed.

Then, from an allowed tailnet client, use the protected token to verify:

- `GET https://<magic-dns>/corthex/v1/status`;
- MCP handshake and tool discovery at the documented `/corthex/mcp` endpoint;
- retain, recall, and reflect against a uniquely named disposable test bank;
- denied access to a bank outside the token allowlist;
- reconnect after `sudo systemctl restart corthex-remote.service`.

From a machine outside the tailnet, confirm the MagicDNS endpoint is unroutable and the VPS public address has no listener for 8890, Hindsight, or PostgreSQL. Do not test against the production bank.

## Backup and restore drill

Create a mode-`0600` custom-format database snapshot:

```bash
sudo ./deploy/backup.sh
sudo sha256sum -c /var/lib/corthex/backups/corthex-*.dump.sha256
```

For a representative restore, create a disposable empty PostgreSQL database with separate credentials, then run:

```bash
sudo ./deploy/restore.sh /var/lib/corthex/backups/corthex-<timestamp>.dump \
  'postgresql://restore-user:…@127.0.0.1:5432/corthex_restore_test' \
  --confirm-empty-target
```

Point an isolated Hindsight process at the restored database, verify the disposable test bank's retain/recall data, then remove only that disposable process/database. `restore.sh` refuses the configured source database and any target containing user tables.

Copy backups to encrypted off-host storage using the operator's existing backup system. These dumps contain memories and must be treated as sensitive data.

## Rollback

Remove only the scoped Serve handler, stop the facade, and preserve data:

```bash
sudo tailscale serve --yes --https=443 --set-path /corthex off
sudo systemctl disable --now corthex-remote.service
sudo rm -f /etc/systemd/system/corthex-remote.service
sudo systemctl daemon-reload
```

The scoped `off` command removes only `/corthex`; it was verified not to alter an existing root handler. Never use `tailscale serve reset` on a shared host.

Keep `/etc/corthex`, `/var/lib/corthex`, and backups until a verified restore exists. Removing those paths or the authoritative database is a separate irreversible operation and is not part of rollback.
