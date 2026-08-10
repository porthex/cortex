import pytest
from mcp.client import Client

from corthex.mcp_stdio import build_stdio_server
from tests.test_mcp_server import FakeMemory


@pytest.mark.asyncio
async def test_stdio_entrypoint_builds_same_public_server() -> None:
    server = build_stdio_server(FakeMemory(), allowed_banks={"test-bank": "Isolated"})

    async with Client(server) as client:
        tools = await client.list_tools()

    assert {tool.name for tool in tools.tools} == {
        "corthex_banks",
        "corthex_recall",
        "corthex_reflect",
        "corthex_retain",
    }
