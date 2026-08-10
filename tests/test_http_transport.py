from __future__ import annotations

import asyncio
import json
import socket

import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from corthex.contracts import RecallResult, ReflectResult, RetainResult
from corthex.mcp_http import build_http_app


class IsolatedMemory:
    def __init__(self):
        self.items = []
        self.recall_calls = 0

    async def retain(self, bank_id, content, context=None):
        self.items.append(content)
        return RetainResult(accepted=True, operation_id="http-op")

    async def recall(self, bank_id, query, max_tokens=4096):
        self.recall_calls += 1
        return RecallResult(text="\n".join(self.items) or "http recall", results=[])

    async def reflect(self, bank_id, query, context=None):
        return ReflectResult(text="http reflect", facts=[])

    async def close(self):
        return None


class CancellableMemory(IsolatedMemory):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def recall(self, bank_id, query, max_tokens=4096):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return await super().recall(bank_id, query, max_tokens)


@pytest.mark.asyncio
async def test_streamable_http_requires_bearer_and_discovers_tools() -> None:
    app = build_http_app(
        IsolatedMemory(),
        allowed_banks={"test-bank": "Isolated"},
        token="test-token",
        host="localhost",
    )
    transport = httpx2.ASGITransport(app=app)

    async with app.server.session_manager.run():
        async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8765") as raw:
            denied = await raw.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert denied.status_code == 401
            assert "Bearer" in denied.headers["www-authenticate"]

        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://localhost:8765",
            headers={"Authorization": "Bearer test-token"},
        ) as authenticated:
            direct = await authenticated.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": "direct-first",
                    "method": "tools/list",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        }
                    },
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/list",
                },
            )
            assert direct.status_code == 200
            assert direct.json()["id"] == "direct-first"
            assert "Mcp-Session-Id" not in direct.headers

            resource_uri = "corthex://banks/test-bank"
            resource = await authenticated.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": "resource-read",
                    "method": "resources/read",
                    "params": {
                        "uri": resource_uri,
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                    },
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "resources/read",
                    "Mcp-Name": resource_uri,
                },
            )
            assert resource.status_code == 200, resource.text
            contents = resource.json()["result"]["contents"]
            assert contents[0]["uri"] == resource_uri

            missing_meta = await authenticated.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": "missing", "method": "tools/list", "params": {}},
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/list",
                },
            )
            assert missing_meta.status_code == 400
            assert missing_meta.json()["error"]["code"] == -32602

            notification = await authenticated.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {"progressToken": "test", "progress": 1, "total": 1},
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "notifications/progress",
                },
            )
            assert notification.status_code == 202, notification.text
            assert notification.content == b""

            unsupported_body = {
                "jsonrpc": "2.0",
                "id": "unsupported",
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "1900-01-01",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            }
            unsupported = await authenticated.post(
                "/mcp",
                json=unsupported_body,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "1900-01-01",
                    "Mcp-Method": "tools/list",
                },
            )
            assert unsupported.status_code == 400
            assert unsupported.json()["error"]["code"] == -32022
            assert unsupported.json()["error"]["data"]["requested"] == "1900-01-01"

            unsupported_mismatch = await authenticated.post(
                "/mcp",
                json=unsupported_body,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/list",
                },
            )
            assert unsupported_mismatch.status_code == 400
            assert unsupported_mismatch.json()["error"]["code"] == -32020

            mismatched = await authenticated.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": "mismatch",
                    "method": "tools/list",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        }
                    },
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-11-25",
                    "Mcp-Method": "resources/list",
                },
            )
            assert mismatched.status_code == 400
            assert mismatched.json()["error"]["code"] == -32020

            assert (await authenticated.get("/mcp")).status_code == 405
            assert (await authenticated.delete("/mcp")).status_code == 405

            streams = streamable_http_client(
                "http://localhost:8765/mcp",
                http_client=authenticated,
            )
            async with Client(streams, raise_exceptions=True) as client:
                tools = await client.list_tools()

    assert {tool.name for tool in tools.tools} == {
        "corthex_banks",
        "corthex_recall",
        "corthex_reflect",
        "corthex_retain",
    }


@pytest.mark.asyncio
async def test_http_app_hosts_health_and_v1_memory_facade() -> None:
    app = build_http_app(
        IsolatedMemory(),
        allowed_banks={"test-bank": "Isolated"},
        token="test-token",
        host="localhost",
    )
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8765") as client:
        health = await client.get("/health")
        denied = await client.get("/v1/status")
        headers = {"Authorization": "Bearer test-token"}
        status = await client.get("/v1/status", headers=headers)
        banks = await client.get("/v1/banks", headers=headers)
        retained = await client.post(
            "/v1/retain",
            headers=headers,
            json={"bank_id": "test-bank", "content": "REST memory"},
        )
        recalled = await client.post(
            "/v1/recall",
            headers=headers,
            json={"bank_id": "test-bank", "query": "memory"},
        )
        reflected = await client.post(
            "/v1/reflect",
            headers=headers,
            json={"bank_id": "test-bank", "query": "meaning"},
        )

    assert health.json() == {"status": "ok"}
    assert denied.status_code == 401
    assert status.json()["protocol_version"] == "2026-07-28"
    assert banks.json() == {"banks": [{"bank_id": "test-bank", "description": "Isolated"}]}
    assert retained.json() == {"accepted": True, "operation_id": "http-op"}
    assert recalled.json()["text"] == "REST memory"
    assert reflected.json()["text"] == "http reflect"


@pytest.mark.asyncio
async def test_v1_facade_implements_the_shipped_cli_contract() -> None:
    backend = IsolatedMemory()
    app = build_http_app(
        backend,
        allowed_banks={"test-bank": "Isolated"},
        token="test-token",
        host="localhost",
    )
    transport = httpx2.ASGITransport(app=app)
    headers = {"Authorization": "Bearer test-token"}
    async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8765") as client:
        retained = await client.post(
            "/v1/memories/retain",
            headers=headers,
            json={"bank": "test-bank", "text": "CLI memory"},
        )
        recalled = await client.post(
            "/v1/memories/recall",
            headers=headers,
            json={"bank": "test-bank", "query": "memory", "limit": 10},
        )
        reflected = await client.post(
            "/v1/memories/reflect",
            headers=headers,
            json={"bank": "test-bank", "query": "meaning"},
        )
        forbidden = await client.post(
            "/v1/memories/recall",
            headers=headers,
            json={"bank": "other-bank", "query": "memory", "limit": 10},
        )
        legacy_forbidden = await client.post(
            "/v1/recall",
            headers=headers,
            json={"bank_id": "other-bank", "query": "memory"},
        )
        zero_limit = await client.post(
            "/v1/memories/recall",
            headers=headers,
            json={"bank": "test-bank", "query": "memory", "limit": 0},
        )
        boolean_limit = await client.post(
            "/v1/memories/recall",
            headers=headers,
            json={"bank": "test-bank", "query": "memory", "limit": True},
        )
        unauthorized = await client.get("/v1/status")

    assert retained.json() == {"retained": True, "bank": "test-bank", "operation_id": "http-op"}
    assert recalled.json() == {"bank": "test-bank", "memories": ["CLI memory"]}
    assert reflected.json() == {"bank": "test-bank", "reflection": "http reflect"}
    assert forbidden.status_code == 403
    assert forbidden.json() == {
        "error": {"code": "bank_forbidden", "message": "Bank is not allowed"}
    }
    assert legacy_forbidden.json() == {"error": "bank_forbidden"}
    assert zero_limit.status_code == 400
    assert boolean_limit.status_code == 400
    assert backend.recall_calls == 1
    assert unauthorized.status_code == 401
    assert unauthorized.json() == {
        "error": {"code": "authentication_failed", "message": "Authentication failed"}
    }


@pytest.mark.asyncio
async def test_real_http_stream_close_cancels_in_flight_tool() -> None:
    backend = CancellableMemory()
    app = build_http_app(
        backend,
        allowed_banks={"test-bank": "Isolated"},
        token="test-token",
        host="127.0.0.1",
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
    )
    serve_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with app.server.session_manager.run():
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            request = {
                "jsonrpc": "2.0",
                "id": "cancel-http",
                "method": "tools/call",
                "params": {
                    "name": "corthex_recall",
                    "arguments": {"bank_id": "test-bank", "query": "wait"},
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            }
            body = json.dumps(request).encode()
            writer.write(
                b"POST /mcp HTTP/1.1\r\n"
                + f"Host: 127.0.0.1:{port}\r\n".encode()
                + b"Authorization: Bearer test-token\r\n"
                + b"Accept: application/json, text/event-stream\r\n"
                + b"Content-Type: application/json\r\n"
                + b"MCP-Protocol-Version: 2026-07-28\r\n"
                + b"Mcp-Method: tools/call\r\n"
                + b"Mcp-Name: corthex_recall\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            await asyncio.wait_for(backend.started.wait(), timeout=2)
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(backend.cancelled.wait(), timeout=2)
            assert reader.at_eof()
    finally:
        backend.release.set()
        server.should_exit = True
        await asyncio.wait_for(serve_task, timeout=5)
        listener.close()
