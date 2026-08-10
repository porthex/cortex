# ADR 0001: Pin Cortex to MCP 2026-07-28 stateless architecture

Status: Accepted
Date: 2026-08-10
Decision owners: Cortex MCP maintainers

## Decision

Cortex implements the official stable MCP protocol revision `2026-07-28` as its core wire architecture. Each request is self-contained: the server derives protocol version and client capabilities from that request, never from an earlier request, connection, process, or HTTP session.[1][2][3]

The implementation language is Python 3.10+ and the protocol dependency is pinned exactly to official Python SDK `mcp==2.0.0` (or `mcp[cli]==2.0.0` when the CLI extra is required), with transitive dependencies locked. SDK v2 is the current stable line and explicitly supports MCP `2026-07-28` and earlier revisions.[12][13] Do not use `mcp>=2,<3`, an unpinned Git branch, or `main`.

Cortex supports the two standard transports:

- Streamable HTTP for remote clients.
- A windowless stdio adapter for clients that require subprocess transport.

Legacy initialization-based behavior is not the core. A bounded dual-era adapter may be added only for a named, tested client that demonstrably requires an official legacy revision. Modern traffic must remain stateless even when legacy support is enabled.[4][5]

No experimental extension is in Cortex's required surface. Extensions are opt-in capability entries and must fall back to core behavior or reject when the peer did not negotiate them.[4]

## Compatibility matrix

| Concern | Modern core (`2026-07-28`) | Legacy (`2025-11-25` and earlier) | Cortex rule |
|---|---|---|---|
| Lifecycle | No negotiation handshake; each request is accepted/rejected independently | `initialize` then `notifications/initialized` | Modern only by default |
| Version | `_meta.io.modelcontextprotocol/protocolVersion` on every request | Negotiated during `initialize`; later HTTP header | Body field is authoritative; HTTP header must match |
| Client capabilities | `_meta.io.modelcontextprotocol/clientCapabilities` on every request | Connection/session scoped | Never cache as request authority |
| Discovery | `server/discover` is mandatory on servers but optional for clients | Initialization result | Direct first RPC must work without discovery |
| HTTP session | None; no `Mcp-Session-Id` | Optional session ID, DELETE termination | Ignore legacy header; never mint or echo it in modern mode |
| HTTP stream | One POST per message; JSON or request-scoped SSE reply | GET SSE stream and POSTs may be session-bound | GET/DELETE return 405 in modern mode |
| Server-to-client input | `InputRequiredResult` and client retry (MRTR) | Server-initiated JSON-RPC requests | Modern server must never emit JSON-RPC requests |
| Client response direction | Client sends requests/notifications only | Client responds to server requests | Reject client JSON-RPC responses in modern mode |
| Cancellation | stdio notification; HTTP response-stream close | Cancellation notification/session semantics | Transport-specific behavior only |
| Cross-call state | Explicit server-minted handle passed in each request | Implicit connection/session state possible | Explicit IDs only |

The official compatibility algorithm is bounded and error-aware.[4]
A dual-era stdio client probes with `server/discover`; recognized modern errors mean retry with a supported version, while any other error or timeout permits legacy fallback.[4][6]
A dual-era HTTP client attempts modern POST and inspects a 400 body; recognized modern errors require correction/retry, not fallback.[4][7]
Era classification may be cached by stdio process or HTTP origin.[4]

## Normative contract

### Base messages and errors

- Every MCP message MUST be UTF-8 JSON-RPC 2.0. Request IDs are string or integer, non-null, and unique while in flight. Responses carry the same ID when readable. Notifications have no ID and receive no response.[3][5]
- Result responses MUST include `resultType`; ordinary completion is `complete`, while MRTR uses `input_required`. Clients MUST treat absent `resultType` from an earlier-version server as `complete`.[3][11]
- General failures use JSON-RPC codes `-32700` and `-32600` through `-32603`. MCP reserves `-32020` through `-32099`; Cortex must use only specified meanings: `HeaderMismatch=-32020`, `MissingRequiredClientCapability=-32021`, and `UnsupportedProtocolVersion=-32022`.[3]
- Modern servers do not initiate JSON-RPC requests and clients do not send JSON-RPC responses. Server input needs use MRTR; notifications are the only permitted unsolicited message shape.[5][11]

### Per-request metadata and versioning

Every request MUST include these body fields.[3]

```json
{
  "params": {
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": {},
      "io.modelcontextprotocol/clientInfo": {
        "name": "example-client",
        "version": "1.0.0"
      }
    }
  }
}
```

`protocolVersion` and `clientCapabilities` are required; `clientInfo` is optional but SHOULD be included. A missing required field is `-32602 Invalid params`; on HTTP it is `400 Bad Request`. The server MUST NOT use an undeclared capability; a required missing capability returns `-32021` with `data.requiredCapabilities` and HTTP 400. Server results SHOULD include `_meta.io.modelcontextprotocol/serverInfo`, but identity fields are informational and must not drive security or behavior.[3]

An unsupported requested version MUST return `-32022` with `data.supported` and `data.requested`; HTTP uses 400. The client SHOULD retry a mutually supported version and MUST NOT reinterpret a recognized modern error as permission to fall back to `initialize`. Servers MUST implement `server/discover`, but no other request depends on calling it first.[4]

### stdio

- The client launches the server and exchanges one newline-delimited JSON-RPC message per line. Messages MUST NOT contain embedded newlines.
- The server reads stdin and writes only valid MCP messages to stdout; logging belongs on stderr.
- The client sends requests and notifications, never responses. The server sends responses and notifications, never requests.
- All protocol metadata is in the JSON body; there is no header layer.
- To cancel, the client MUST send `notifications/cancelled` with the in-flight request ID. The server SHOULD stop promptly and MUST NOT send further messages for that request.
- EOF on stdin is graceful shutdown. A restarted process has no protocol session; clients retry lost requests and reopen subscriptions.[6][8]

### Streamable HTTP

- The server MUST expose one MCP endpoint supporting POST. Every client JSON-RPC message is a new POST with one request or notification body; a client MUST NOT POST a JSON-RPC response.
- Each request POST MUST advertise both `application/json` and `text/event-stream`. A request reply is either one JSON object or a request-scoped SSE stream; clients MUST support both. Accepted notifications return empty HTTP 202.
- Every request POST MUST include `MCP-Protocol-Version` and `Mcp-Method`; `tools/call`, `resources/read`, and `prompts/get` also require `Mcp-Name`. Header names compare case-insensitively, values case-sensitively. Header encoding/decoding rules apply before comparison.
- The JSON body is authoritative. A missing, malformed, or mismatched required mirror header MUST produce HTTP 400 and JSON-RPC `HeaderMismatch` (`-32020`).
- Unsupported versions return HTTP 400/`-32022`. Unknown methods return HTTP 404/`-32601`.
- The server MUST validate a present `Origin`; invalid origin returns HTTP 403. Local-only servers SHOULD bind loopback, and all connections SHOULD be authenticated.
- An SSE stream may carry only notifications related to that request followed by its final response. It MUST NOT carry an independent server request. `subscriptions/listen` owns long-lived change notifications. `Last-Event-ID` resumption is unsupported.
- Closing a request's SSE response stream MUST cancel that request; no client `notifications/cancelled` POST is expected. The server SHOULD stop work and MUST send nothing further for it.
- Modern mode has no GET stream, protocol session, DELETE termination, or resumability. GET and DELETE SHOULD return 405; `Mcp-Session-Id` and `Last-Event-ID` are ignored, not minted or echoed.[7][8]

### Authorization

HTTP authorization follows MCP's OAuth 2.1 framework when OAuth is used; stdio SHOULD obtain credentials from the launching environment instead.[3][9]

For protected HTTP deployments:

- Bearer tokens travel in the `Authorization` header on every protected request, never in a query string. Invalid or expired tokens return HTTP 401; insufficient permissions return HTTP 403.[9]
- Servers MUST implement OAuth Protected Resource Metadata with at least one `authorization_servers` entry and one of the required discovery mechanisms. Clients support both `WWW-Authenticate` `resource_metadata` and well-known discovery.[9]
- Clients MUST send the RFC 8707 `resource` parameter. Servers MUST validate the token is intended for themselves and MUST NOT pass the inbound token through to an upstream API.[10]
- Clients MUST implement PKCE, verify advertised PKCE support, use `S256` when capable, validate redirect URIs/state, bind credentials to the authorization-server issuer, and reject issuer mismatches.[10]

Cortex's initial private-network static bearer deployment is a narrower product choice, not an OAuth claim.
It still requires a token on every request, constant-time validation, no query-string credentials, wrong-token denial, and bank authorization independent of self-reported MCP identity.[9][10]
If public OAuth discovery is advertised, all normative OAuth requirements above become part of acceptance.[9][10]

## Verification gates

1. Run the repository fixture tests:

   ```text
   pytest -q tests/test_mcp_2026_contract_fixture.py
   ```

2. Wire every case in `tests/fixtures/mcp-2026-07-28-contract.json` to the real HTTP and stdio adapters. A fake/in-memory SDK client is not transport evidence.
3. Run official conformance against protocol `2026-07-28`. Pin the frozen referee used by the official revision requirements: `@modelcontextprotocol/conformance@0.2.0-alpha.10`. This is intentionally not the latest stable package: official stable `v0.1.16` predates the 2026 revision and cannot establish full 2026 wire conformance. Never use unpinned `main`.[14][15][16]
4. Run official Inspector exactly at `@modelcontextprotocol/inspector@2.1.0` against both transports and capture discovery/tool/resource evidence.[17][18]
5. Verify with real Hermes plus one other real MCP client, including direct first request, reconnect/restart, cancellation, wrong/no token, and isolated-bank retain → recall and reflect.
6. Fail CI if the SDK pin is a range, if modern code requires `initialize`, if `Mcp-Session-Id` is minted/echoed, or if per-request metadata/mismatch tests are absent.

The official conformance repository independently labels the 2026 wire as stateless with per-request `_meta`, makes `server-stateless` a required server scenario, and separates it from dated stateful runs.[14][15]
Inspector 2.1.0 independently exercises modern/legacy era selection, so it is an interoperability check rather than the source of normative truth.[17][18]

## Consequences

- Stateless means no implicit protocol context, not “no state anywhere.” Authentication state, in-flight work, caches, subscriptions, and application handles remain valid when explicitly scoped and referenced.[3]
- Existing `CortexMcpStdioBridge.cs` is legacy architecture: it learns protocol version from `initialize`, stores `Mcp-Session-Id`, and reuses both on later HTTP requests. It remains migration evidence only and must not be the Cortex core.
- SDK convenience APIs are accepted only when black-box transport tests prove they emit the pinned wire contract.

## Sources

[1] https://github.com/modelcontextprotocol/modelcontextprotocol/releases/tag/2026-07-28
[2] https://github.com/modelcontextprotocol/modelcontextprotocol/blob/2026-07-28/docs/specification/2026-07-28/architecture/index.mdx
[3] https://github.com/modelcontextprotocol/modelcontextprotocol/blob/2026-07-28/docs/specification/2026-07-28/basic/index.mdx
[4] https://github.com/modelcontextprotocol/modelcontextprotocol/blob/2026-07-28/docs/specification/2026-07-28/basic/versioning.mdx
[5] https://github.com/modelcontextprotocol/modelcontextprotocol/blob/2026-07-28/docs/specification/2026-07-28/basic/transports/index.mdx
[6] https://github.com/modelcontextprotocol/modelcontextprotocol/blob/2026-07-28/docs/specification/2026-07-28/basic/transports/stdio.mdx
[7] https://github.com/modelcontextprotocol/modelcontextprotocol/blob/2026-07-28/docs/specification/2026-07-28/basic/transports/streamable-http.mdx
[8] https://github.com/modelcontextprotocol/modelcontextprotocol/blob/2026-07-28/docs/specification/2026-07-28/basic/patterns/cancellation.mdx
[9] https://github.com/modelcontextprotocol/modelcontextprotocol/blob/2026-07-28/docs/specification/2026-07-28/basic/authorization/index.mdx
[10] https://github.com/modelcontextprotocol/modelcontextprotocol/blob/2026-07-28/docs/specification/2026-07-28/basic/authorization/security-considerations.mdx
[11] https://github.com/modelcontextprotocol/modelcontextprotocol/blob/2026-07-28/docs/specification/2026-07-28/changelog.mdx
[12] https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0
[13] https://github.com/modelcontextprotocol/python-sdk/blob/v2.0.0/README.md
[14] https://github.com/modelcontextprotocol/conformance/blob/c321dd32035556e6769d3724a8ee97d87c3faaac/requirements/2026-07-28.yaml
[15] https://github.com/modelcontextprotocol/conformance/blob/c321dd32035556e6769d3724a8ee97d87c3faaac/src/connection/stateless.ts
[16] https://github.com/modelcontextprotocol/conformance/releases/tag/v0.1.16
[17] https://github.com/modelcontextprotocol/inspector/releases/tag/2.1.0
[18] https://github.com/modelcontextprotocol/inspector/blob/2.1.0/README.md
