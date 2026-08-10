import pytest

from corthex.auth import StaticTokenVerifier


@pytest.mark.asyncio
async def test_static_token_verifier_accepts_exact_token_and_rejects_others() -> None:
    verifier = StaticTokenVerifier("correct horse", scopes=["corthex:memory"])

    accepted = await verifier.verify_token("correct horse")

    assert accepted is not None
    assert accepted.client_id == "corthex-static-client"
    assert accepted.scopes == ["corthex:memory"]
    assert await verifier.verify_token("wrong") is None
    assert await verifier.verify_token("") is None
