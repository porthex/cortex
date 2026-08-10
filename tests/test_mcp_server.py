from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

import pytest
from mcp.client import Client

from corthex.contracts import RecallResult, ReflectResult, RetainResult
from corthex.mcp_server import create_mcp_server


@dataclass
class FakeMemory:
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    closed: bool = False

    async def retain(self, bank_id: str, content: str, context: str | None = None) -> RetainResult:
        self.calls.append(("retain", bank_id, content))
        return RetainResult(accepted=True, operation_id="op-1")

    async def recall(self, bank_id: str, query: str, max_tokens: int = 4096) -> RecallResult:
        self.calls.append(("recall", bank_id, query))
        return RecallResult(text=f"remembered: {query}", results=[])

    async def reflect(self, bank_id: str, query: str, context: str | None = None) -> ReflectResult:
        self.calls.append(("reflect", bank_id, query))
        return ReflectResult(text=f"reflection: {query}", facts=[])

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_server_exposes_corthex_tools_and_maps_retain_to_backend() -> None:
    backend = FakeMemory()
    server = create_mcp_server(backend, allowed_banks={"test-bank": "Isolated test bank"})

    async with Client(server, raise_exceptions=True) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        result = await client.call_tool(
            "corthex_retain",
            {"bank_id": "test-bank", "content": "Durable preference"},
        )

    assert names == {"corthex_banks", "corthex_recall", "corthex_reflect", "corthex_retain"}
    assert result.is_error is False
    assert result.structured_content == {"accepted": True, "operation_id": "op-1"}
    assert backend.calls == [("retain", "test-bank", "Durable preference")]
    assert backend.closed is True


@pytest.mark.asyncio
async def test_server_preserves_bank_boundary_for_tools_and_resources() -> None:
    backend = FakeMemory()
    server = create_mcp_server(backend, allowed_banks={"bank-a": "Allowed bank"})

    async with Client(server) as client:
        banks = await client.call_tool("corthex_banks")
        resource = await client.read_resource("corthex://banks/bank-a")
        denied = await client.call_tool(
            "corthex_recall",
            {"bank_id": "bank-b", "query": "must not cross"},
        )

    assert banks.structured_content == {
        "banks": [{"bank_id": "bank-a", "description": "Allowed bank"}]
    }
    assert json.loads(resource.contents[0].text) == {
        "bank_id": "bank-a",
        "description": "Allowed bank",
    }
    assert denied.is_error is True
    assert backend.calls == []
