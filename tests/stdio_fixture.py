from __future__ import annotations

import asyncio
import os
from pathlib import Path

from cortex.contracts import RecallItem, RecallResult, ReflectResult, RetainResult
from cortex.mcp_server import create_mcp_server
from cortex.mcp_stdio import run_modern_stdio

import anyio


class FixtureMemory:
    def __init__(self) -> None:
        self.items: list[str] = []

    async def retain(self, bank_id, content, context=None):
        self.items.append(content)
        return RetainResult(accepted=True, operation_id=f"fixture-{len(self.items)}")

    async def recall(self, bank_id, query, max_tokens=4096):
        marker_dir = os.environ.get("CORTEX_CANCELLATION_MARKER_DIR")
        if query == "wait-for-cancellation" and marker_dir:
            marker = Path(marker_dir)
            marker.mkdir(parents=True, exist_ok=True)
            (marker / "started").write_text("started", encoding="utf-8")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                (marker / "cancelled").write_text("cancelled", encoding="utf-8")
                raise
        matches = [RecallItem(text=item, type="world") for item in self.items]
        return RecallResult(text="\n".join(self.items), results=matches)

    async def reflect(self, bank_id, query, context=None):
        return ReflectResult(text=f"Reflected over {len(self.items)} memories", facts=[])

    async def close(self):
        return None


server = create_mcp_server(FixtureMemory(), allowed_banks={"test-bank": "Isolated fixture"})

if __name__ == "__main__":
    anyio.run(run_modern_stdio, server)
