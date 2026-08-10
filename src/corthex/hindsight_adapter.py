"""Adapter from Corthex public contracts to the Hindsight client."""

from __future__ import annotations

import asyncio
from typing import Any

from hindsight_client import Hindsight

from .contracts import RecallItem, RecallResult, ReflectResult, RetainResult


class HindsightMemoryBackend:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        if client is None:
            if not base_url:
                raise ValueError("base_url is required when client is not supplied")
            client = Hindsight(base_url=base_url, api_key=api_key)
        self._client = client

    async def retain(self, bank_id: str, content: str, context: str | None = None) -> RetainResult:
        response = await asyncio.to_thread(
            self._client.retain,
            bank_id=bank_id,
            content=content,
            context=context,
        )
        return RetainResult(
            accepted=bool(response.success),
            operation_id=getattr(response, "operation_id", None),
        )

    async def recall(self, bank_id: str, query: str, max_tokens: int = 4096) -> RecallResult:
        response = await asyncio.to_thread(
            self._client.recall,
            bank_id=bank_id,
            query=query,
            max_tokens=max_tokens,
        )
        items = [
            RecallItem(
                text=item.text,
                type=getattr(item, "type", None),
                score=getattr(getattr(item, "scores", None), "final", None),
                metadata=getattr(item, "metadata", None) or {},
            )
            for item in response.results
        ]
        return RecallResult(text="\n\n".join(item.text for item in items), results=items)

    async def reflect(self, bank_id: str, query: str, context: str | None = None) -> ReflectResult:
        response = await asyncio.to_thread(
            self._client.reflect,
            bank_id=bank_id,
            query=query,
            context=context,
        )
        return ReflectResult(text=response.text, facts=[])

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await asyncio.to_thread(close)
