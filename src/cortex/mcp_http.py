"""Authenticated Streamable HTTP launcher for Cortex MCP."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any, cast
from urllib.parse import urlsplit

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from .auth import StaticTokenVerifier
from .contracts import MemoryBackend, RecallResult, ReflectResult, RetainResult
from .hindsight_adapter import HindsightMemoryBackend
from .mcp_server import create_mcp_server
from .runtime import RuntimeConfig


class AuthenticatedMcpApp:
    """Small ASGI perimeter for the private static-bearer deployment."""

    def __init__(
        self,
        app: Any,
        server: Any,
        verifier: StaticTokenVerifier,
        backend: MemoryBackend,
        allowed_banks: Mapping[str, str],
    ) -> None:
        self._app = app
        self.server = server
        self._verifier = verifier
        self._backend = backend
        self._banks = dict(allowed_banks)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "").rstrip("/") or "/"
            method_name = scope.get("method")
            if path == "/health" and method_name == "GET":
                await self._send_json(send, 200, {"status": "ok"})
                return
            if path == "/mcp" and method_name in {"GET", "DELETE"}:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 405,
                        "headers": [(b"allow", b"POST")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
            headers = {name.lower(): value for name, value in scope.get("headers", [])}
            authorization = headers.get(b"authorization", b"").decode("latin-1")
            scheme, separator, token = authorization.partition(" ")
            accepted = (
                bool(separator)
                and scheme.casefold() == "bearer"
                and await self._verifier.verify_token(token) is not None
            )
            if not accepted:
                body = b'{"error":{"code":"authentication_failed","message":"Authentication failed"}}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"www-authenticate", b'Bearer realm="cortex"'),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
            if path.startswith("/v1/"):
                await self._handle_v1(path, method_name, receive, send)
                return
            if path == "/mcp" and method_name == "POST":
                chunks: list[bytes] = []
                more = True
                while more:
                    event = await receive()
                    chunks.append(event.get("body", b""))
                    more = event.get("more_body", False)
                body = b"".join(chunks)
                try:
                    message = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await self._send_error(send, None, -32700, "Parse error")
                    return
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    await self._send_error(send, None, -32600, "Invalid Request")
                    return
                if "method" not in message:
                    await self._send_error(send, message.get("id"), -32600, "Client responses are not accepted")
                    return
                method = message.get("method")
                params = message.get("params")
                meta = params.get("_meta") if isinstance(params, dict) else None
                protocol_key = "io.modelcontextprotocol/protocolVersion"
                capabilities_key = "io.modelcontextprotocol/clientCapabilities"
                if "id" not in message and not isinstance(meta, dict):
                    meta = {
                        protocol_key: headers.get(b"mcp-protocol-version", b"").decode("latin-1"),
                        capabilities_key: {},
                    }
                if not isinstance(meta, dict) or protocol_key not in meta or capabilities_key not in meta:
                    await self._send_error(send, message.get("id"), -32602, "Modern request metadata is required")
                    return
                requested = meta[protocol_key]
                mirrored_version = headers.get(b"mcp-protocol-version", b"").decode("latin-1")
                mirrored_method = headers.get(b"mcp-method", b"").decode("latin-1")
                request_name_fields = {
                    "tools/call": "name",
                    "resources/read": "uri",
                    "prompts/get": "name",
                }
                request_name_field: str | None = (
                    request_name_fields.get(method) if isinstance(method, str) else None
                )
                body_name = (
                    params.get(request_name_field)
                    if isinstance(params, dict) and request_name_field is not None
                    else None
                )
                mirrored_name = headers.get(b"mcp-name", b"").decode("latin-1")
                if (
                    mirrored_version != requested
                    or mirrored_method != method
                    or (request_name_field is not None and mirrored_name != body_name)
                ):
                    await self._send_error(send, message.get("id"), -32020, "Required HTTP mirror header mismatch")
                    return
                if requested != "2026-07-28":
                    await self._send_error(
                        send,
                        message.get("id"),
                        -32022,
                        "Unsupported protocol version",
                        {"supported": ["2026-07-28"], "requested": requested},
                    )
                    return
                if "id" not in message:
                    await send({"type": "http.response.start", "status": 202, "headers": []})
                    await send({"type": "http.response.body", "body": b""})
                    return

                replayed = False

                async def replay_receive():
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await receive()

                await self._app(scope, replay_receive, send)
                return
        await self._app(scope, receive, send)

    async def _handle_v1(self, path, method, receive, send) -> None:
        if path == "/v1/status" and method == "GET":
            await self._send_json(
                send,
                200,
                {"status": "ready", "protocol_version": "2026-07-28", "bank_count": len(self._banks)},
            )
            return
        if path == "/v1/banks" and method == "GET":
            await self._send_json(
                send,
                200,
                {
                    "banks": [
                        {"bank_id": bank_id, "description": description}
                        for bank_id, description in sorted(self._banks.items())
                    ]
                },
            )
            return
        operations = {
            "/v1/retain": ("retain", False),
            "/v1/recall": ("recall", False),
            "/v1/reflect": ("reflect", False),
            "/v1/memories/retain": ("retain", True),
            "/v1/memories/recall": ("recall", True),
            "/v1/memories/reflect": ("reflect", True),
        }
        if method != "POST" or path not in operations:
            error = (
                {"error": {"code": "not_found", "message": "Route not found"}}
                if path.startswith("/v1/memories/")
                else {"error": "not_found"}
            )
            await self._send_json(
                send,
                404,
                error,
            )
            return
        chunks: list[bytes] = []
        more = True
        while more:
            event = await receive()
            chunks.append(event.get("body", b""))
            more = event.get("more_body", False)
        try:
            payload = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError):
            error = (
                {"error": {"code": "invalid_json", "message": "Request body is not valid JSON"}}
                if path.startswith("/v1/memories/")
                else {"error": "invalid_json"}
            )
            await self._send_json(
                send,
                400,
                error,
            )
            return
        operation, cli_contract = operations[path]
        bank_key = "bank" if cli_contract else "bank_id"
        bank_id = payload.get(bank_key) if isinstance(payload, dict) else None
        if bank_id not in self._banks:
            error = (
                {"error": {"code": "bank_forbidden", "message": "Bank is not allowed"}}
                if cli_contract
                else {"error": "bank_forbidden"}
            )
            await self._send_json(
                send,
                403,
                error,
            )
            return
        cli_limit = None
        if cli_contract and operation == "recall":
            cli_limit = payload.get("limit", 10)
            if isinstance(cli_limit, bool) or not isinstance(cli_limit, int) or cli_limit < 1:
                await self._send_json(
                    send,
                    400,
                    {"error": {"code": "invalid_request", "message": "limit must be positive"}},
                )
                return
        try:
            if operation == "retain":
                content_key = "text" if cli_contract else "content"
                result = await self._backend.retain(
                    bank_id, payload[content_key], payload.get("context")
                )
            elif operation == "recall":
                result = await self._backend.recall(bank_id, payload["query"], payload.get("max_tokens", 4096))
            else:
                result = await self._backend.reflect(bank_id, payload["query"], payload.get("context"))
        except (KeyError, TypeError, ValueError) as exc:
            error = (
                {"error": {"code": "invalid_request", "message": str(exc)}}
                if cli_contract
                else {"error": "invalid_request", "message": str(exc)}
            )
            await self._send_json(
                send,
                400,
                error,
            )
            return
        if not cli_contract:
            response = result.model_dump(mode="json")
        elif operation == "retain":
            retained = cast(RetainResult, result)
            response = {
                "retained": retained.accepted,
                "bank": bank_id,
                "operation_id": retained.operation_id,
            }
        elif operation == "recall":
            recalled = cast(RecallResult, result)
            memories = [item.text for item in recalled.results]
            if not memories and recalled.text:
                memories = [recalled.text]
            assert isinstance(cli_limit, int)
            response = {"bank": bank_id, "memories": memories[:cli_limit]}
        else:
            reflected = cast(ReflectResult, result)
            response = {"bank": bank_id, "reflection": reflected.text}
        await self._send_json(send, 200, response)

    @staticmethod
    async def _send_error(send, request_id, code: int, message: str, data=None) -> None:
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "error": error}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _send_json(send, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_http_app(
    backend: MemoryBackend,
    *,
    allowed_banks: Mapping[str, str],
    token: str,
    host: str = "127.0.0.1",
    public_url: str | None = None,
) -> AuthenticatedMcpApp:
    server = create_mcp_server(backend, allowed_banks=allowed_banks)
    transport_security = None
    if public_url:
        parsed = urlsplit(public_url)
        public_hostname = parsed.hostname or ""
        if ":" in public_hostname:
            public_hostname = f"[{public_hostname}]"
        authority = public_hostname
        if parsed.port is not None:
            authority = f"{authority}:{parsed.port}"
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[authority],
            allowed_origins=[f"{parsed.scheme}://{authority}"],
        )
    inner = server.streamable_http_app(
        stateless_http=True,
        host=host,
        transport_security=transport_security,
    )
    return AuthenticatedMcpApp(inner, server, StaticTokenVerifier(token), backend, allowed_banks)


def main() -> None:
    config = RuntimeConfig.from_environment(require_http_auth=True)
    backend = HindsightMemoryBackend(
        base_url=config.hindsight_url,
        api_key=config.hindsight_api_key,
    )
    app = build_http_app(
        backend,
        allowed_banks=config.banks,
        token=config.mcp_token or "",
        host=config.host,
        public_url=config.public_url,
    )
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
