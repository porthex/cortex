from __future__ import annotations

import json
import threading
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class IsolatedGateway(AbstractContextManager["IsolatedGateway"]):
    token = "isolated-test-token"

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.memories: dict[str, list[str]] = {}
        self.status_code = 200
        self.status_body: object = {"ok": True, "data": {"state": "ready", "version": "test"}}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _respond(self, status: int, body: object) -> None:
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:
                owner.requests.append(
                    {"method": "GET", "path": self.path, "authorization": self.headers.get("Authorization")}
                )
                if self.headers.get("Authorization") != f"Bearer {owner.token}":
                    self._respond(401, {"error": {"code": f"bad {self.headers.get('Authorization')}", "message": f"denied {self.headers.get('Authorization')}"}})
                    return
                if self.path == "/v1/status":
                    self._respond(owner.status_code, owner.status_body)
                    return
                if self.path == "/v1/banks":
                    self._respond(200, {"ok": True, "data": {"banks": sorted(owner.memories)}})
                    return
                self._respond(404, {"error": {"code": "not_found", "message": "missing"}})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append(
                    {"method": "POST", "path": self.path, "authorization": self.headers.get("Authorization"), "payload": payload}
                )
                if self.headers.get("Authorization") != f"Bearer {owner.token}":
                    self._respond(401, {"error": {"code": f"bad {self.headers.get('Authorization')}", "message": f"denied {self.headers.get('Authorization')}"}})
                    return
                bank = payload.get("bank")
                if self.path == "/v1/memories/retain":
                    owner.memories.setdefault(bank, []).append(payload["text"])
                    self._respond(200, {"ok": True, "data": {"retained": True, "bank": bank}})
                    return
                if self.path == "/v1/memories/recall":
                    matches = [text for text in owner.memories.get(bank, []) if payload["query"].lower() in text.lower()]
                    self._respond(200, {"ok": True, "data": {"bank": bank, "memories": matches}})
                    return
                if self.path == "/v1/memories/reflect":
                    self._respond(200, {"ok": True, "data": {"bank": bank, "reflection": f"reflection: {payload['query']}"}})
                    return
                if self.path in {"/v1/control/start", "/v1/control/stop"}:
                    self._respond(200, {"ok": True, "data": {"state": "ready" if self.path.endswith("start") else "stopped"}})
                    return
                self._respond(404, {"error": {"code": "not_found", "message": "missing"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "IsolatedGateway":
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
