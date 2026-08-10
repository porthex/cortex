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

    @staticmethod
    def _invalid_response() -> CorthexError:
        return CorthexError("invalid_response", "Corthex returned an invalid response", 7)

    @staticmethod
    def _valid_error(error: object) -> bool:
        return (
            isinstance(error, dict)
            and isinstance(error.get("code"), str)
            and isinstance(error.get("message"), str)
        )

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
            if not isinstance(error_body, dict) or not self._valid_error(error_body.get("error")):
                raise self._invalid_response() from exc
            if exc.code in {401, 403}:
                raise CorthexError("authentication_failed", "Authentication failed", 3) from exc
            error = error_body["error"]
            code = self._safe_remote_code(error.get("code"), "remote_error")
            fallback = f"Corthex returned HTTP {exc.code}"
            message = self._safe_remote_message(error.get("message"), fallback)
            exit_code = 4 if exc.code == 404 else 5
            raise CorthexError(code, message, exit_code) from exc
        except (URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
            raise CorthexError("connection_failed", "Unable to connect to Corthex", 6, True) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorthexError("invalid_response", "Corthex returned invalid JSON", 7) from exc

        if isinstance(raw, dict) and "ok" in raw:
            if not isinstance(raw["ok"], bool):
                raise self._invalid_response()
            if not raw["ok"]:
                error = raw.get("error")
                if not self._valid_error(error):
                    raise self._invalid_response()
                assert isinstance(error, dict)
                raise CorthexError(
                    self._safe_remote_code(error.get("code"), "remote_error"),
                    self._safe_remote_message(error.get("message"), "Request failed"),
                    5,
                )
            if "data" not in raw:
                raise self._invalid_response()
            return raw["data"]
        if not isinstance(raw, (dict, list)):
            raise self._invalid_response()
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
