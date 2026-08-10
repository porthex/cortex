from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client


@pytest.mark.asyncio
async def test_real_stdio_subprocess_discovers_and_runs_memory_flow() -> None:
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "tests.stdio_fixture"],
        env=environment,
        cwd=root,
    )

    async with Client(stdio_client(parameters), raise_exceptions=True) as client:
        tools = await client.list_tools()
        retained = await client.call_tool(
            "cortex_retain",
            {"bank_id": "test-bank", "content": "Subprocess memory"},
        )
        recalled = await client.call_tool(
            "cortex_recall",
            {"bank_id": "test-bank", "query": "memory"},
        )
        reflected = await client.call_tool(
            "cortex_reflect",
            {"bank_id": "test-bank", "query": "meaning"},
        )

    assert {tool.name for tool in tools.tools} == {
        "cortex_banks",
        "cortex_recall",
        "cortex_reflect",
        "cortex_retain",
    }
    assert retained.structured_content == {"accepted": True, "operation_id": "fixture-1"}
    assert recalled.structured_content["text"] == "Subprocess memory"
    assert reflected.structured_content["text"] == "Reflected over 1 memories"


@pytest.mark.asyncio
async def test_stdio_rejects_legacy_initialize_in_modern_only_mode() -> None:
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.stdio_fixture",
        cwd=root,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    request = {
        "jsonrpc": "2.0",
        "id": "legacy-init",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "legacy-probe", "version": "1"},
        },
    }
    process.stdin.write(json.dumps(request).encode() + b"\n")
    await process.stdin.drain()
    response = json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=10))
    process.stdin.close()
    await asyncio.wait_for(process.wait(), timeout=10)

    assert response["id"] == "legacy-init"
    assert response["error"]["code"] in {-32602, -32022}
    assert "2026-07-28" in json.dumps(response["error"])


@pytest.mark.asyncio
async def test_explicit_hermes_compatibility_launcher_accepts_legacy_initialize() -> None:
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.stdio_legacy_fixture",
        cwd=root,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "mcp", "version": "0.1.0"},
        },
    }
    process.stdin.write(json.dumps(request).encode() + b"\n")
    await process.stdin.drain()
    response = json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=10))
    process.terminate()
    await process.wait()

    assert response["id"] == 1
    assert response["result"]["protocolVersion"] == "2025-11-25"


@pytest.mark.asyncio
async def test_real_stdio_cancelled_notification_cancels_in_flight_tool(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    environment["CORTEX_CANCELLATION_MARKER_DIR"] = str(tmp_path)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.stdio_fixture",
        cwd=root,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    stdin = process.stdin
    stdout = process.stdout
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }

    def send(message: dict) -> None:
        stdin.write(json.dumps(message).encode() + b"\n")

    send(
        {
            "jsonrpc": "2.0",
            "id": "cancel-stdio",
            "method": "tools/call",
            "params": {
                "name": "cortex_recall",
                "arguments": {"bank_id": "test-bank", "query": "wait-for-cancellation"},
                "_meta": meta,
            },
        }
    )
    await stdin.drain()
    for _ in range(1000):
        if (tmp_path / "started").exists():
            break
        await asyncio.sleep(0.01)
    assert (tmp_path / "started").exists()

    send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "cancel-stdio", "reason": "test"},
        }
    )
    await stdin.drain()
    for _ in range(200):
        if (tmp_path / "cancelled").exists():
            break
        await asyncio.sleep(0.01)
    assert (tmp_path / "cancelled").exists()

    send(
        {
            "jsonrpc": "2.0",
            "id": "after-cancel",
            "method": "tools/call",
            "params": {"name": "cortex_banks", "arguments": {}, "_meta": meta},
        }
    )
    await stdin.drain()
    response = json.loads(await asyncio.wait_for(stdout.readline(), timeout=10))
    stdin.close()
    await asyncio.wait_for(process.wait(), timeout=10)

    assert response["id"] == "after-cancel"
    assert response["result"]["structuredContent"]["banks"][0]["bank_id"] == "test-bank"
