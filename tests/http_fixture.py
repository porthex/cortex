"""Isolated real-socket HTTP fixture for Inspector/conformance verification."""

from __future__ import annotations

from corthex.contracts import RecallItem, RecallResult, ReflectResult, RetainResult
from corthex.mcp_http import build_http_app


class FixtureMemory:
    def __init__(self) -> None:
        self.items: list[str] = []

    async def retain(self, bank_id, content, context=None):
        self.items.append(content)
        return RetainResult(accepted=True, operation_id=f"http-{len(self.items)}")

    async def recall(self, bank_id, query, max_tokens=4096):
        return RecallResult(
            text="\n".join(self.items),
            results=[RecallItem(text=item, type="world") for item in self.items],
        )

    async def reflect(self, bank_id, query, context=None):
        return ReflectResult(text=f"Reflected over {len(self.items)} memories", facts=[])

    async def close(self):
        return None


app = build_http_app(
    FixtureMemory(),
    allowed_banks={"test-bank": "Isolated HTTP fixture"},
    token="test-token",
    host="127.0.0.1",
)
