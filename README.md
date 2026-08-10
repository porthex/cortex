<p align="center">
  <img src="assets/corthex-mark.svg" width="152" alt="Corthex by Porthex: a silver intelligence signal on a black field">
</p>

<h1 align="center">Corthex</h1>

<p align="center"><strong>Corthex gives your AI tools one shared long-term memory.</strong></p>

Corthex keeps durable preferences, decisions, and context available across sessions without turning every transcript into permanent memory.

## How it works

1. An AI client sends selected context through a Corthex client or adapter.
2. An authenticated gateway passes approved memory operations to Hindsight.
3. Another connected client can recall that context later from the same bank.

Corthex uses [Hindsight](https://github.com/vectorize-io/hindsight) as its underlying memory engine. Hindsight organizes memory into banks and provides the `retain`, `recall`, and `reflect` operations.[1] Corthex supplies the client integration, access boundary, policy, migration, and operations around it.

## What ships today

This repository contains five working parts:

- A dependency-free Python 3.10+ CLI for a compatible local or remote Corthex `/v1` gateway.
- An authenticated `corthex-mcp-http` service exposing stateless MCP at `/mcp` and the CLI facade at `/v1`, plus windowless modern and Hermes-compatible stdio adapters.
- A private Linux VPS deployment path that publishes only the authenticated Corthex facade through tailnet-only Tailscale Serve HTTPS.
- Deterministic migration tools for combining existing Hindsight banks without deleting the sources.
- The preserved Windows Cortex Brain baseline that Corthex is being generalized from.

The Windows baseline already demonstrates the full local shape:

- Codex, ChatGPT Desktop, and Claude Desktop connect to one Hindsight bank through client adapters.
- A loopback-only, bearer-authenticated gateway keeps clients away from Hindsight and PostgreSQL directly.
- Hindsight, PostgreSQL, and an Ollama model can run locally.
- A written memory policy limits routine retention to durable context and treats recalled text as untrusted data.
- Hindsight's Memory Browser makes retained information inspectable.
- Installer, deep-sleep lifecycle, health checks, backup, restore, and uninstall scripts handle routine operations without deleting memory by default.

The cross-platform CLI adds explicit configuration, stable JSON output, strict transport rules, and commands for status, banks, retain, recall, reflect, start, and stop. Tokens come from the environment or standard input and are not saved in its config file.

## Try the CLI

Install from a checkout:

```sh
python -m pip install .
```

Configure a compatible Corthex gateway, then provide the token separately:

```sh
corthex configure --url https://brain.example.ts.net --bank my-bank
export CORTHEX_TOKEN='replace-with-client-token'

corthex doctor
corthex retain "Prefer concise release notes"
corthex recall "release notes"
corthex reflect "What communication preferences have been retained?"
```

Use `corthex --json <command>` for machine-readable output. See the [CLI guide](docs/cli.md) for the complete command contract and exit codes.

## Remote Brain

The Linux VPS deployment runs one authoritative Corthex instance on loopback and publishes only its authenticated facade through tailnet-only Tailscale Serve HTTPS. The facade implements both the bundled CLI contract and MCP transport, so desktop clients can connect without installing Hindsight, PostgreSQL, or a local model. Raw Hindsight and PostgreSQL remain unreachable through remote routes.

The systemd unit uses a dedicated non-login account and bounded writable directories. The reviewed install command also performs transactional upgrades without replacing unrelated Tailscale Serve routes. Native PostgreSQL backup and empty-target restore scripts provide a reproducible recovery drill. See the [Remote Brain VPS runbook](docs/REMOTE_BRAIN_VPS.md) and [transport ADR](docs/adr/0001-remote-brain-transport.md).

## Memory and lifecycle

Corthex is selective long-term memory, not a transcript recorder. The shipped baseline policy excludes raw conversations, assistant output, source files, logs, and tool output from routine retention. Client behavior remains explicit and model-dependent: Corthex does not silently scrape chats, and an AI client must be configured before it can use the memory bank.

The migration toolkit provides a fail-closed path to a shared `corthex` bank. It normalizes and deduplicates records, preserves source provenance, requires verified backup manifests before applying a plan, and leaves source banks intact. Read [Unified Corthex memory architecture](docs/UNIFIED_MEMORY.md) before using it.

## Current limits

Corthex is early software, not a finished hosted service.

The repository ships a Linux VPS deployment path, not a general-purpose cross-platform server installer. The CLI needs a compatible `/v1` gateway, and each AI client still needs an adapter plus explicit configuration. The preserved Windows gateway uses one fixed bank; the per-client bank authorization, redaction, audit, and broader lifecycle controls in the [target architecture](docs/architecture.md) are design requirements, not shipped behavior.

Local models and storage can reduce outside data flow, but they do not make a deployment private by themselves. Operators still choose the network boundary, retention policy, model providers, storage, and backup location.

## Repository map

- [`docs/cli.md`](docs/cli.md): install, configure, and use the cross-platform client
- [`docs/mcp.md`](docs/mcp.md): configure the authenticated HTTP service and stdio adapters
- [`docs/adr/0001-mcp-2026-architecture.md`](docs/adr/0001-mcp-2026-architecture.md): pinned MCP 2026-07-28 protocol contract
- [`docs/REMOTE_BRAIN_VPS.md`](docs/REMOTE_BRAIN_VPS.md): deploy and operate a private Remote Brain on Linux
- [`docs/adr/0001-remote-brain-transport.md`](docs/adr/0001-remote-brain-transport.md): private transport decision and threat matrix
- [`docs/UNIFIED_MEMORY.md`](docs/UNIFIED_MEMORY.md): migration, backup gates, verification, and rollback
- [`docs/architecture.md`](docs/architecture.md): target trust boundaries and security invariants
- [`docs/configuration.md`](docs/configuration.md): intended deployment configuration and validation
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md): Hindsight attribution
- [`SECURITY.md`](SECURITY.md): report a vulnerability privately

## Sources

[1] https://github.com/vectorize-io/hindsight — Hindsight official repository

## Development

Run the platform-independent test suite:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests
```

Before contributing, read [CONTRIBUTING.md](CONTRIBUTING.md). The Corthex project license and inbound contribution terms remain undecided; see [LICENSES.md](LICENSES.md).

<p align="center"><sub>Built by <a href="https://porthex.io">Porthex</a>.</sub></p>
