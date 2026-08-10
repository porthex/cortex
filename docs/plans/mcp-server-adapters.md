# MCP server and adapters implementation plan

1. Define Corthex request/result models and a backend protocol independent of Hindsight.
2. Add a Hindsight client adapter and deterministic fake-backed contract tests.
3. Register retain, recall, reflect, and banks tools plus bank resources on one official SDK v2 server.
4. Enforce a configured bank allow-list at every tool/resource boundary.
5. Add a constant-time static bearer verifier for Streamable HTTP and denial tests.
6. Add thin `corthex-mcp-http` and `corthex-mcp-stdio` entry points; stdio writes protocol data only to stdout.
7. Verify in-memory, subprocess stdio, and real localhost Streamable HTTP handshakes with isolated fake state.
8. Document setup, client snippets, migration, and rollback; never include live tokens, banks, hosts, or paths.
