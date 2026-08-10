from __future__ import annotations

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from corthex.contracts import RecallResult, ReflectResult, RetainResult
from corthex.mcp_http import build_http_app


class IsolatedMemory:
    def __init__(self):
        self.items = []

    async def retain(self, bank_id, content, context=None):
        self.items.append(content)
        return RetainResult(accepted=True, operation_id="http-op")

    async def recall(self, bank_id, query, max_tokens=4096):
        return RecallResult(text="\n".join(self.items) or "http recall", results=[])

    async def reflect(self, bank_id, query, context=None):
        return ReflectResult(text="http reflect", facts=[])

    async def close(self):
        return None


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
