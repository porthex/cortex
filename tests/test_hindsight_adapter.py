from __future__ import annotations

from types import SimpleNamespace

import pytest

from corthex.hindsight_adapter import HindsightMemoryBackend


class FakeHindsightClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def retain(self, *args, **kwargs):
        self.calls.append(("retain", args, kwargs))
        return SimpleNamespace(success=True, operation_id="retain-7")

    def recall(self, *args, **kwargs):
        self.calls.append(("recall", args, kwargs))
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    text="Stored fact",
                    type="world",
                    scores=SimpleNamespace(final=0.91),
                    metadata={"source": "test"},
                )
            ]
        )

    def reflect(self, *args, **kwargs):
        self.calls.append(("reflect", args, kwargs))
        return SimpleNamespace(text="A considered answer", based_on={"facts": ["fact-1"]})


@pytest.mark.asyncio
async def test_adapter_maps_retain_without_leaking_hindsight_types() -> None:
    client = FakeHindsightClient()
    backend = HindsightMemoryBackend(client=client)

    result = await backend.retain("bank-a", "Remember this", "project context")

    assert result.model_dump() == {"accepted": True, "operation_id": "retain-7"}
    assert client.calls == [
        (
            "retain",
            (),
            {"bank_id": "bank-a", "content": "Remember this", "context": "project context"},
        )
    ]


@pytest.mark.asyncio
async def test_adapter_maps_recall_to_stable_corthex_result() -> None:
    client = FakeHindsightClient()
    backend = HindsightMemoryBackend(client=client)

    result = await backend.recall("bank-a", "What matters?", 512)

    assert result.text == "Stored fact"
    assert result.results[0].model_dump() == {
        "text": "Stored fact",
        "type": "world",
        "score": 0.91,
        "metadata": {"source": "test"},
    }
    assert client.calls == [
        ("recall", (), {"bank_id": "bank-a", "query": "What matters?", "max_tokens": 512})
    ]


@pytest.mark.asyncio
async def test_adapter_maps_reflect_to_stable_corthex_result() -> None:
    client = FakeHindsightClient()
    backend = HindsightMemoryBackend(client=client)

    result = await backend.reflect("bank-a", "What follows?", "current task")

    assert result.text == "A considered answer"
    assert result.facts == []
    assert client.calls == [
        (
            "reflect",
            (),
            {"bank_id": "bank-a", "query": "What follows?", "context": "current task"},
        )
    ]
