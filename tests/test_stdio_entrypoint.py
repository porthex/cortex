import asyncio
import json
import os
from pathlib import Path
import sys

import pytest
from mcp.client import Client

from cortex.mcp_stdio import build_stdio_server
from tests.test_mcp_server import FakeMemory


@pytest.mark.asyncio
async def test_stdio_entrypoint_builds_same_public_server() -> None:
    server = build_stdio_server(FakeMemory(), allowed_banks={"test-bank": "Isolated"})

    async with Client(server) as client:
        tools = await client.list_tools()

    assert {tool.name for tool in tools.tools} == {
        "cortex_banks",
        "cortex_recall",
        "cortex_reflect",
        "cortex_retain",
    }


@pytest.mark.asyncio
async def test_installed_hermes_entrypoint_discovers_and_calls_banks() -> None:
    executable = Path(sys.executable).parent / "cortex-mcp-stdio-hermes"
    environment = dict(os.environ)
    environment["CORTEX_BANKS_JSON"] = json.dumps({"test-bank": "Isolated"})
    process = await asyncio.create_subprocess_exec(
        str(executable),
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdin = process.stdin
    stdout = process.stdout
    stderr_stream = process.stderr

    async def exchange(message: dict) -> dict:
        stdin.write(json.dumps(message).encode() + b"\n")
        await stdin.drain()
        return json.loads(await asyncio.wait_for(stdout.readline(), timeout=10))

    initialized = await exchange(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "mcp", "version": "0.1.0"},
            },
        }
    )
    stdin.write(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}).encode() + b"\n"
    )
    await stdin.drain()
    tools = await exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    banks = await exchange(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "cortex_banks", "arguments": {}},
        }
    )

    stdin.close()
    await asyncio.wait_for(process.wait(), timeout=10)
    stderr = (await stderr_stream.read()).decode()

    assert initialized["result"]["protocolVersion"] == "2025-11-25"
    assert {tool["name"] for tool in tools["result"]["tools"]} >= {"cortex_banks"}
    assert banks["result"]["structuredContent"] == {
        "banks": [{"bank_id": "test-bank", "description": "Isolated"}]
    }
    assert process.returncode == 0, stderr
