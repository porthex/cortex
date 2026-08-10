# Active MCP worker contract review — 2026-08-10

Candidate reviewed: Kanban task `t_68efcb13`, branch `feature/cortex-mcp-server-adapters`, dirty/uncommitted snapshot.

Verdict: NO-COMMIT for MCP 2026 architecture acceptance. The Cortex tool surface and bank allow-list are a useful foundation, but the snapshot does not yet prove the required wire architecture.

## Blocking findings

1. `pyproject.toml` uses `mcp>=2,<3`, violating the exact stable pin requirement. Use `mcp==2.0.0` (and commit a transitive lock).
2. `tests/test_mcp_server.py` exercises an in-memory `Client(server)` only. It does not prove stdio framing, Streamable HTTP POST/header/body validation, cancellation, no-session behavior, authorization, or legacy negotiation.
3. No `mcp_http.py` or `mcp_stdio.py` launch modules existed in the reviewed snapshot even though console entry points named them.
4. No test covered required per-request `_meta`, direct first RPC without discovery, `-32602`, `-32020`, `-32021`, `-32022`, removed GET/DELETE/session behavior, or server-request/client-response direction.
5. The active ADR described “SDK v2” but did not pin protocol revision `2026-07-28` or define the modern-versus-legacy contract.

## Independent execution

Running the worker venv's full pytest command from its workspace exited 2 during collection because `cortex` was not importable. This may reflect an incomplete editable install while the worker was still active, but it is not passing evidence.

## Required remediation

Consume `docs/adr/0001-mcp-2026-architecture.md`, `constraints-mcp.txt`, and `tests/fixtures/mcp-2026-07-28-contract.json` from this branch. Wire every required fixture case to the real HTTP and stdio adapters, then run the pinned official conformance referee and Inspector plus two real clients.
