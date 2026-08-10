#!/usr/bin/env python3
"""Verify that a loopback Corthex endpoint denies bad credentials before ingress."""

from __future__ import annotations

import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def status(url: str, token: str | None) -> int:
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} STATUS_URL", file=sys.stderr)
        return 2
    token = os.environ.get("CORTHEX_MCP_TOKEN", "")
    if not token:
        print("CORTHEX_MCP_TOKEN is not set", file=sys.stderr)
        return 2
    wrong = "invalid-" + token[:8]
    try:
        observed = (status(sys.argv[1], None), status(sys.argv[1], wrong), status(sys.argv[1], token))
    except (OSError, URLError) as exc:
        print(f"Corthex authentication probe failed: {exc}", file=sys.stderr)
        return 1
    if observed[0:2] != (401, 401) or not 200 <= observed[2] < 300:
        print(f"Unexpected authentication statuses: {observed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
