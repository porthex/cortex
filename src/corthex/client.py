from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Config


@dataclass
class CorthexError(Exception):
    code: str
    message: str
    exit_code: int
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


class Client:
    def __init__(self, config: Config, token: str) -> None:
        if not token:
            raise CorthexError("missing_credentials", "CORTHEX_TOKEN is not set", 3)
        self.config = config
        self._token = token

    def _safe_remote_message(self, message: object, fallback: str) -> str:
        text = str(message or fallback)
        if self._token:
            text = text.replace(f"Bearer {self._token}", "Bearer [redacted]")
            text = text.replace(self._token, "[redacted]")
        return text

    def _safe_remote_code(self, code: object, fallback: str) -> str:
        text = str(code or fallback)
        if self._token:
            text = text.replace(self._token, "[redacted]")
        return text if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", text) else fallback

    def request(self, method: str, path: str, payload: object | None = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.config.url}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "corthex-cli/0.1.0",
            },
        )
        try:
            with urlopen(request, timeout=self.config.timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                error_body = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_body = {}
            error = error_body.get("error", {}) if isinstance(error_body, dict) else {}
            code = (
                "authentication_failed"
                if exc.code in {401, 403}
                else self._safe_remote_code(error.get("code"), "remote_error")
            )
            fallback = "Authentication failed" if exc.code in {401, 403} else f"Corthex returned HTTP {exc.code}"
            message = fallback if exc.code in {401, 403} else self._safe_remote_message(error.get("message"), fallback)
            exit_code = 3 if exc.code in {401, 403} else 4 if exc.code == 404 else 5
            raise CorthexError(code, message, exit_code) from exc
        except (URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
            raise CorthexError("connection_failed", "Unable to connect to Corthex", 6, True) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorthexError("invalid_response", "Corthex returned invalid JSON", 7) from exc

        if isinstance(raw, dict) and "ok" in raw:
            if not raw.get("ok"):
                error = raw.get("error") or {}
                raise CorthexError(
                    self._safe_remote_code(error.get("code"), "remote_error"),
                    self._safe_remote_message(error.get("message"), "Request failed"),
                    5,
                )
            return raw.get("data")
        return raw

    def status(self) -> Any:
        return self.request("GET", "/v1/status")

    def banks(self) -> Any:
        return self.request("GET", "/v1/banks")

    def retain(self, text: str, bank: str) -> Any:
        return self.request("POST", "/v1/memories/retain", {"bank": bank, "text": text})

    def recall(self, query: str, bank: str, limit: int) -> Any:
        return self.request(
            "POST", "/v1/memories/recall", {"bank": bank, "query": query, "limit": limit}
        )

    def reflect(self, query: str, bank: str) -> Any:
        return self.request("POST", "/v1/memories/reflect", {"bank": bank, "query": query})

    def control(self, action: str) -> Any:
        if action not in {"start", "stop"}:
            raise ValueError("control action must be start or stop")
        return self.request("POST", f"/v1/control/{action}", {})
