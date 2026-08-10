# ADR 0002: Cross-platform Cortex CLI

Status: Proposed for integration
Date: 2026-08-10

## Context

Cortex needs one operator/client command on Windows, macOS, and Linux. The CLI must address the public Cortex gateway rather than Hindsight's private API, keep bank selection explicit, support deterministic JSON automation, and avoid writing bearer tokens to project files or command history.

## Decision

Ship a Python 3.10+ distribution with a `cortex` console script and no runtime dependencies. Configuration is stored in the platform user config directory (or `CORTEX_CONFIG`), while credentials come from `CORTEX_TOKEN` or `--token-stdin`. The CLI never persists or prints a token.

The stable HTTP contract is rooted at `/v1`:

- `GET /v1/status`
- `GET /v1/banks`
- `POST /v1/memories/retain`
- `POST /v1/memories/recall`
- `POST /v1/memories/reflect`
- `POST /v1/control/start`
- `POST /v1/control/stop`

Requests carry `Authorization: Bearer …`, `Accept: application/json`, and an explicit bank in the JSON body for memory operations. Responses may be a direct JSON value or the public envelope `{ok,data,error,request_id}`. The CLI maps stable error categories to documented exit codes and emits a stable envelope under `--json`.

`configure` writes only URL, bank, and timeout. `connect` performs an authenticated status probe and may accept a token from stdin for that invocation. `doctor` validates configuration, HTTPS/private-loopback transport, credential presence, and reachability without echoing secrets. `start` and `stop` call public operator endpoints; platform service installation remains a deployment concern, not CLI subprocess guessing.

## Consequences

- The gateway/MCP implementation must expose or adapt the seven narrow `/v1` routes.
- Tailscale remains the private transport perimeter; HTTPS is required for non-loopback URLs.
- Machine consumers get stable envelopes and exit codes; human output remains concise.
- The CLI does not depend on or import Hindsight.
- Local daemon installation and the latest MCP 2026-07-28 protocol implementation stay in sibling workstreams.

## Rejected options

- Calling Hindsight directly: leaks private engine contracts and weakens the perimeter.
- Encoding MCP tool calls in the CLI: couples command UX to tool names and protocol evolution; the public gateway can adapt MCP and REST independently.
- Persisting tokens in JSON: portable but unsafe and easy to leak in diagnostics or backups.
