# ADR 0001: Remote Brain transport

Status: Accepted
Date: 2026-08-10

## Context

Corthex needs one authoritative VPS instance for desktop AI clients. Clients must not install Hindsight, PostgreSQL, or a local model. The raw Hindsight API and database must never become public. The existing host and intended clients already use Tailscale.

MCP's official transports are stdio and Streamable HTTP. For a remote server, Streamable HTTP requires a network security boundary; a local stdio adapter can forward clients that do not support remote HTTP. Tailscale provides encrypted WireGuard point-to-point transport and Tailscale Serve provides tailnet-only HTTPS to a loopback origin.

## Decision

Use **Tailscale Serve** as the sole remote ingress:

```text
remote client -> tailnet HTTPS -> Tailscale Serve /corthex
              -> 127.0.0.1:8890 Corthex facade
              -> 127.0.0.1:<internal> raw Hindsight
              -> loopback/private PostgreSQL
```

The Corthex facade binds only to `127.0.0.1`, authenticates every request with an explicit bearer token, and enforces the allowed bank set. Tailscale Serve adds `/corthex` without replacing other handlers. Funnel is forbidden. The raw Hindsight API, its UI, PostgreSQL, and backup files are never Serve targets.

The service runs under the dedicated `corthex` account. Code is root-owned under `/opt/corthex`; runtime state is limited to `/var/lib/corthex` and `/var/log/corthex`; secrets are in `/etc/corthex/corthex.env` with mode `0600`. Backups use a consistent PostgreSQL custom-format dump and restores require a separate empty target.

## Alternatives

### Direct tailnet port

A **Direct tailnet port** bound to the exact Tailscale address is somewhat simpler and remains private when firewall rules admit only `tailscale0`. It was rejected because it adds listener/firewall state and does not provide the HTTPS origin that general Streamable HTTP clients expect.

### SSH local forwarding

**SSH local forwarding** (`ssh -L`) needs no additional remote ingress daemon and remains the emergency fallback. It was rejected as the default because every desktop needs a persistent tunnel lifecycle, reconnect behavior, and local port coordination before MCP can connect.

### Public reverse proxy

A public hostname behind an identity proxy was rejected. MCP clients do not need public reachability, and exposing another public authentication plane contradicts the smallest perimeter-first design.

## Threat/test matrix

| Threat or failure | Control | Required test |
|---|---|---|
| Internet reaches Corthex | loopback bind plus tailnet-only Serve; no Funnel | public-interface probe cannot reach 8890 |
| Internet reaches raw Hindsight or PostgreSQL | loopback/private binds; never Serve those ports | listener inventory and public probes show no exposure |
| Non-tailnet client reaches service | Tailscale Serve | request from a device outside the tailnet is denied/unroutable |
| Tailnet member lacks application authority | bearer token on every facade/MCP request | missing and wrong tokens return 401; correct token succeeds |
| Client crosses bank boundary | token-to-bank allowlist | allowed bank works; another bank returns 403 without upstream access |
| Restart loses availability | systemd enable/restart-on-failure and persistent Serve config | restart service and host path; health and MCP reconnect succeed |
| Backup is unusable | `pg_dump --format=custom`, checksum, empty-target restore | verify checksum; restore to disposable DB; compare schema and isolated test-bank data |
| Secret or memory enters Git | protected env/state paths and ignore rules | repository secret scan and clean status |

## Stop condition

Security work stops when every row in the fixed matrix passes. This ADR deliberately excludes public OAuth, multi-region replication, recursive hardening, self-modifying defenses, and an enterprise policy platform.

## Sources

- Tailscale concepts and WireGuard transport: https://tailscale.com/docs/concepts/what-is-tailscale
- Tailscale Serve command and tailnet-only HTTPS: https://tailscale.com/docs/reference/tailscale-cli/serve
- Tailscale access controls: https://tailscale.com/docs/features/access-control/acls
- OpenSSH `ssh(1)` local forwarding (`-L`)
- MCP stable release 2026-07-28: https://github.com/modelcontextprotocol/modelcontextprotocol/releases/tag/2026-07-28
- MCP Streamable HTTP transport: https://github.com/modelcontextprotocol/modelcontextprotocol/blob/2026-07-28/docs/specification/2026-07-28/basic/transports/streamable-http.mdx
