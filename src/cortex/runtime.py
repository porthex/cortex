"""Environment contract shared by Cortex MCP launchers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RuntimeConfig:
    hindsight_url: str
    hindsight_api_key: str | None
    banks: dict[str, str]
    mcp_token: str | None
    public_url: str | None
    host: str
    port: int

    @classmethod
    def from_environment(
        cls,
        *,
        require_http_auth: bool,
        environ: Mapping[str, str] | None = None,
    ) -> "RuntimeConfig":
        values = os.environ if environ is None else environ
        hindsight_url = values.get("CORTEX_HINDSIGHT_URL", "http://127.0.0.1:8888").rstrip("/")
        parsed = urlsplit(hindsight_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("CORTEX_HINDSIGHT_URL must be an absolute HTTP(S) URL")

        raw_banks = values.get("CORTEX_BANKS_JSON", "")
        try:
            decoded = json.loads(raw_banks)
        except json.JSONDecodeError as exc:
            raise ValueError("CORTEX_BANKS_JSON must be a JSON object") from exc
        if not isinstance(decoded, dict) or not decoded:
            raise ValueError("CORTEX_BANKS_JSON must contain at least one bank")
        banks = {str(key).strip(): str(value).strip() for key, value in decoded.items()}
        if any(not key for key in banks):
            raise ValueError("CORTEX_BANKS_JSON bank IDs must be non-empty")

        token = values.get("CORTEX_MCP_TOKEN") or None
        public_url = values.get("CORTEX_MCP_PUBLIC_URL") or None
        if require_http_auth:
            if not token:
                raise ValueError("CORTEX_MCP_TOKEN is required for Streamable HTTP")
            if not public_url:
                raise ValueError("CORTEX_MCP_PUBLIC_URL is required for Streamable HTTP")
            parsed_public = urlsplit(public_url)
            if (
                parsed_public.scheme not in {"http", "https"}
                or not parsed_public.hostname
                or parsed_public.username is not None
                or parsed_public.password is not None
            ):
                raise ValueError("CORTEX_MCP_PUBLIC_URL must be an absolute HTTP(S) URL without userinfo")

        host = values.get("CORTEX_MCP_HOST", "127.0.0.1")
        try:
            port = int(values.get("CORTEX_MCP_PORT", "8877"))
        except ValueError as exc:
            raise ValueError("CORTEX_MCP_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("CORTEX_MCP_PORT must be between 1 and 65535")

        return cls(
            hindsight_url=hindsight_url,
            hindsight_api_key=values.get("CORTEX_HINDSIGHT_API_KEY") or None,
            banks=banks,
            mcp_token=token,
            public_url=public_url,
            host=host,
            port=port,
        )
