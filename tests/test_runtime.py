import pytest

from cortex.runtime import RuntimeConfig


def test_runtime_config_reads_public_environment_contract(monkeypatch) -> None:
    monkeypatch.setenv("CORTEX_HINDSIGHT_URL", "http://127.0.0.1:8888")
    monkeypatch.setenv("CORTEX_HINDSIGHT_API_KEY", "upstream-secret")
    monkeypatch.setenv("CORTEX_BANKS_JSON", '{"test-bank":"Isolated bank"}')
    monkeypatch.setenv("CORTEX_MCP_TOKEN", "client-secret")
    monkeypatch.setenv("CORTEX_MCP_PUBLIC_URL", "https://brain.example.invalid/mcp")

    config = RuntimeConfig.from_environment(require_http_auth=True)

    assert config.hindsight_url == "http://127.0.0.1:8888"
    assert config.banks == {"test-bank": "Isolated bank"}
    assert config.mcp_token == "client-secret"
    assert config.public_url == "https://brain.example.invalid/mcp"


def test_http_runtime_rejects_missing_client_token(monkeypatch) -> None:
    monkeypatch.setenv("CORTEX_BANKS_JSON", '{"test-bank":"Isolated bank"}')
    monkeypatch.delenv("CORTEX_MCP_TOKEN", raising=False)

    with pytest.raises(ValueError, match="CORTEX_MCP_TOKEN"):
        RuntimeConfig.from_environment(require_http_auth=True)


@pytest.mark.parametrize(
    "public_url",
    ["/cortex/mcp", "https://user:pass@brain.example.invalid/cortex/mcp"],
)
def test_http_runtime_rejects_invalid_public_url(monkeypatch, public_url) -> None:
    monkeypatch.setenv("CORTEX_BANKS_JSON", '{"test-bank":"Isolated bank"}')
    monkeypatch.setenv("CORTEX_MCP_TOKEN", "client-secret")
    monkeypatch.setenv("CORTEX_MCP_PUBLIC_URL", public_url)

    with pytest.raises(ValueError, match="CORTEX_MCP_PUBLIC_URL"):
        RuntimeConfig.from_environment(require_http_auth=True)
