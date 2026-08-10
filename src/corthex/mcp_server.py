"""Official MCP server exposing Corthex-owned memory contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from .contracts import BankInfo, BanksResult, MemoryBackend, RecallResult, ReflectResult, RetainResult


def create_mcp_server(
    backend: MemoryBackend,
    *,
    allowed_banks: Mapping[str, str],
    token_verifier=None,
    auth=None,
) -> MCPServer:
    """Create one Corthex MCP server for either HTTP or stdio transport."""
    banks = dict(allowed_banks)
    if not banks:
        raise ValueError("At least one allowed bank is required")

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield None
        finally:
            await backend.close()

    server = MCPServer(
        "Corthex",
        description="Private, bank-scoped long-term memory for AI clients",
        token_verifier=token_verifier,
        auth=auth,
        lifespan=lifespan,
    )

    def require_bank(bank_id: str) -> str:
        if bank_id not in banks:
            raise ValueError(f"Bank is not available: {bank_id}")
        return bank_id

    @server.tool(
        name="corthex_retain",
        description="Retain one durable fact in an explicitly allowed Corthex bank.",
        annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False),
    )
    async def retain(bank_id: str, content: str, context: str | None = None) -> RetainResult:
        return await backend.retain(require_bank(bank_id), content, context)

    @server.tool(
        name="corthex_recall",
        description="Recall relevant long-term memory from an explicitly allowed Corthex bank.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def recall(bank_id: str, query: str, max_tokens: int = 4096) -> RecallResult:
        if not 1 <= max_tokens <= 32768:
            raise ValueError("max_tokens must be between 1 and 32768")
        return await backend.recall(require_bank(bank_id), query, max_tokens)

    @server.tool(
        name="corthex_reflect",
        description="Synthesize an answer from one explicitly allowed Corthex bank.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def reflect(bank_id: str, query: str, context: str | None = None) -> ReflectResult:
        return await backend.reflect(require_bank(bank_id), query, context)

    @server.tool(
        name="corthex_banks",
        description="List only the banks exposed by this Corthex service.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def list_banks() -> BanksResult:
        return BanksResult(
            banks=[BankInfo(bank_id=bank_id, description=description) for bank_id, description in sorted(banks.items())]
        )

    @server.resource(
        "corthex://banks/{bank_id}",
        name="Corthex bank",
        description="Public metadata for one allowed Corthex bank.",
        mime_type="application/json",
    )
    async def bank_resource(bank_id: str) -> str:
        require_bank(bank_id)
        return json.dumps(BankInfo(bank_id=bank_id, description=banks[bank_id]).model_dump())

    return server
