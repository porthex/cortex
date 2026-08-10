#!/usr/bin/env python3
"""Fail closed when a Tailscale Serve status contains public Funnel ingress."""

from __future__ import annotations

import json
import sys
from typing import Any


def has_public_funnel(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"AllowFunnel", "IsFunnelOn"} and bool(child):
                return True
            if has_public_funnel(child):
                return True
    elif isinstance(value, list):
        return any(has_public_funnel(item) for item in value)
    return False


def has_corthex_path(value: Any) -> bool:
    if isinstance(value, dict):
        return "/corthex" in value or any(has_corthex_path(child) for child in value.values())
    if isinstance(value, list):
        return any(has_corthex_path(item) for item in value)
    return False


def main() -> int:
    try:
        status = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Cannot validate Tailscale Serve status: {exc}", file=sys.stderr)
        return 2
    if has_public_funnel(status):
        print("Tailscale Funnel is configured; Corthex requires tailnet-only Serve", file=sys.stderr)
        return 1
    if has_corthex_path(status):
        print("Tailscale Serve path /corthex already exists; refusing to replace it", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
