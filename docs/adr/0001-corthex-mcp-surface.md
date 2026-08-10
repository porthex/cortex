# ADR 0001: Corthex-owned MCP surface

Status: Accepted

## Context

The published baseline proxies MCP requests directly to Hindsight and ships a custom C# stdio-to-HTTP bridge. That makes Hindsight's private routes and schemas part of the product surface and is difficult to support cross-platform. Corthex needs one public memory contract, official Streamable HTTP, a windowless stdio mode, explicit remote authentication, and bank isolation.

## Decision

Corthex owns typed retain, recall, reflect, and bank-description contracts behind a `MemoryBackend` protocol. The Hindsight adapter is the only module that imports `hindsight_client`. One official MCP Python SDK v2 `MCPServer` registers Corthex tools/resources and is launched directly in either Streamable HTTP or stdio mode.

Remote HTTP requires a bearer verifier before MCP parsing. The server receives an explicit allow-list of banks and rejects every operation outside it. Stdio inherits the launching process boundary and uses the same bank allow-list. Hindsight and PostgreSQL remain loopback/private dependencies; clients never receive their URLs or types.

The HTTP service is intended to bind a Tailscale address (or loopback behind an SSH tunnel), not a public interface. This branch does not own host provisioning.

The default stdio launcher is modern-only. A separate
`corthex-mcp-stdio-hermes` executable is the explicit compatibility facade for
Hermes Agent's current `2025-11-25` Python SDK client. The executable boundary
prevents silent legacy fallback for every client and can be removed without
changing Corthex's public memory contract.

## Consequences

- Public schemas remain stable if Hindsight changes.
- HTTP and stdio behavior share one implementation.
- Static bearer tokens are suitable for the finite private-network deployment; OAuth can replace the verifier without changing tools.
- Bank enumeration is the configured public allow-list, not unrestricted discovery of backend tenants.
- The old gateway/bridge remain migration inputs, not the new public contract.

## Rejected alternatives

- Continue transparent proxying: leaks Hindsight contracts and cannot enforce Corthex bank semantics.
- Maintain separate HTTP and stdio servers: duplicates schemas and behavior.
- Expose Hindsight publicly with its own token: violates the private perimeter requirement.
