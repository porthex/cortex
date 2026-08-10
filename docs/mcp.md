# Cortex MCP and private HTTP service

Cortex provides a stable memory surface backed by [Hindsight](https://github.com/vectorize-io/hindsight). Hindsight and its database remain private upstream dependencies; clients connect only to Cortex.

## Install

```shell
python -m pip install .
```

The package pins the official Python MCP SDK to `mcp==2.0.0` and implements the stable `2026-07-28` protocol revision.

## Required environment

```text
CORTEX_HINDSIGHT_URL=http://127.0.0.1:8888
CORTEX_HINDSIGHT_API_KEY=<optional upstream credential>
CORTEX_BANKS_JSON={"team":"Shared team memory"}
CORTEX_MCP_TOKEN=<random client bearer token>
CORTEX_MCP_PUBLIC_URL=http://127.0.0.1:8877/mcp
CORTEX_MCP_HOST=127.0.0.1
CORTEX_MCP_PORT=8877
```

Keep credentials in a service environment or secret manager, never in source control or client URLs. `CORTEX_BANKS_JSON` is an allow-list; no other Hindsight bank is exposed.

## Run

Long-running HTTP service:

```shell
cortex-mcp-http
```

The service exposes:

- `POST /mcp`: authenticated, stateless Streamable HTTP MCP.
- `GET /health`: unauthenticated liveness only; it reveals no account or bank data.
- authenticated `/v1/status`, `/v1/banks`, and `/v1/memories/{retain,recall,reflect}` routes used by the shipped Cortex CLI. The shorter `/v1/{retain,recall,reflect}` server contract remains available for existing direct clients.

For private remote use, bind the service to loopback behind an SSH tunnel or to a private Tailscale address. Do not expose Hindsight, PostgreSQL, or this static-bearer endpoint to the public internet. The MCP Host allowlist is derived from the absolute `CORTEX_MCP_PUBLIC_URL`, so a private reverse proxy can present its public hostname while the listener remains on loopback.

Windowless stdio adapter:

```shell
cortex-mcp-stdio
```

The stdio adapter reads the same Hindsight and bank environment. It writes only newline-delimited MCP messages to stdout and accepts modern MCP `2026-07-28`; the legacy initialize handshake is rejected.

Current Hermes Agent releases still negotiate MCP `2025-11-25` through a
generic Python SDK client identity. Configure Hermes with the explicit
compatibility facade instead:

```shell
cortex-mcp-stdio-hermes
```

This separate executable is the bounded legacy exception. It shares the same
Cortex tools and backend adapter without weakening the modern-only default
endpoint for every stdio client. Remove it once Hermes negotiates
`2026-07-28` natively.

## Client examples

Streamable HTTP clients use URL `/mcp` and send `Authorization: Bearer ***` on every request. Modern stdio clients launch `cortex-mcp-stdio`; current Hermes clients launch `cortex-mcp-stdio-hermes`. Provide credentials through the child environment rather than command-line arguments.

## Verification

```shell
python -m pytest -q
```

The suite covers in-memory contracts, real stdio subprocess discovery and retain → recall → reflect, authenticated Streamable HTTP, direct first modern requests, per-request metadata, header mismatch, unsupported versions, bank denial, and the REST facade.

For the official stateless protocol gate, start the isolated authenticated fixture through its local token-injecting proxy, then run the pinned conformance CLI:

```shell
PYTHONPATH=src uv run uvicorn tests.conformance_proxy:app --host 127.0.0.1 --port 8766
npx -y @modelcontextprotocol/conformance@0.2.0-alpha.10 server \
  --url http://127.0.0.1:8766/mcp \
  --scenario server-stateless \
  --spec-version 2026-07-28 \
  --expected-failures tests/conformance-baseline.yml
```

All 27 mandatory checks execute and pass. The test-only HTTP fixture registers the suite's synthetic `test_missing_capability` tool; that diagnostic is not part of the Cortex product surface. The baseline contains only the three accepted SHOULD warnings: one alpha-tool warning where `serverInfo` is present in the result, plus dynamic prompt/tool list-change warnings for Cortex's intentionally static surface.

Inspector 2.1.0 defaults ad-hoc connections to the legacy protocol era. Use the checked-in configuration to pin the modern 2026-07-28 era; this causes Inspector to perform `server/discover` and send the required per-request metadata and mirror headers:

```shell
npx -y @modelcontextprotocol/inspector@2.1.0 --cli \
  --config tests/inspector-modern.json --server cortex-http \
  --method tools/list --format json
npx -y @modelcontextprotocol/inspector@2.1.0 --cli \
  --config tests/inspector-modern.json --server cortex-http \
  --method tools/call --tool-name cortex_banks --tool-args-json '{}' --format json
npx -y @modelcontextprotocol/inspector@2.1.0 --cli \
  --config tests/inspector-modern.json --server cortex-stdio \
  --method tools/list --format json
npx -y @modelcontextprotocol/inspector@2.1.0 --cli \
  --config tests/inspector-modern.json --server cortex-stdio \
  --method tools/call --tool-name cortex_banks --tool-args-json '{}' --format json
```

## Rollback

1. Stop the `cortex-mcp-http` service and remove client references to `/mcp` or `cortex-mcp-stdio`.
2. Reinstall the previous Cortex release or commit.
3. Leave Hindsight and its database untouched; this package does not migrate or delete banks.
4. Restore the previous service environment. Rotate `CORTEX_MCP_TOKEN` if it was exposed during rollback.
