"""Stable public Corthex memory contracts."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class RetainResult(BaseModel):
    accepted: bool
    operation_id: str | None = None


class RecallItem(BaseModel):
    text: str
    type: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecallResult(BaseModel):
    text: str
    results: list[RecallItem] = Field(default_factory=list)


class ReflectResult(BaseModel):
    text: str
    facts: list[dict[str, Any]] = Field(default_factory=list)


class BankInfo(BaseModel):
    bank_id: str
    description: str


class BanksResult(BaseModel):
    banks: list[BankInfo]


class MemoryBackend(Protocol):
    async def retain(self, bank_id: str, content: str, context: str | None = None) -> RetainResult: ...

    async def recall(self, bank_id: str, query: str, max_tokens: int = 4096) -> RecallResult: ...

    async def reflect(self, bank_id: str, query: str, context: str | None = None) -> ReflectResult: ...

    async def close(self) -> None: ...
