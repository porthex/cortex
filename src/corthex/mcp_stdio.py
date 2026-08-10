"""Windowless stdio launcher for Corthex MCP.

Stdout is reserved for newline-delimited MCP messages; diagnostics use stderr.
"""

from __future__ import annotations

from collections.abc import Mapping

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.runner import _serve_modern_stream
from mcp.server.stdio import stdio_server

from .contracts import MemoryBackend
from .hindsight_adapter import HindsightMemoryBackend
from .mcp_server import create_mcp_server
from .runtime import RuntimeConfig


def build_stdio_server(backend: MemoryBackend, *, allowed_banks: Mapping[str, str]):
    return create_mcp_server(backend, allowed_banks=allowed_banks)


async def run_modern_stdio(server) -> None:
    """Run only MCP 2026-07-28 on stdio.

    The exact-pinned SDK exposes its modern loop internally but its convenience
    runner is dual-era. This narrow adapter intentionally selects the modern
    loop so legacy initialize cannot silently become Corthex's core.
    """
    lowlevel = server._lowlevel_server
    async with stdio_server() as (read_stream, write_stream):
        async with lowlevel.lifespan(lowlevel) as lifespan_state:
            await _serve_modern_stream(
                lowlevel,
                read_stream,
                write_stream,
                lifespan_state=lifespan_state,
                raise_exceptions=False,
            )


def main() -> None:
    config = RuntimeConfig.from_environment(require_http_auth=False)
    backend = HindsightMemoryBackend(
        base_url=config.hindsight_url,
        api_key=config.hindsight_api_key,
    )
    server = build_stdio_server(backend, allowed_banks=config.banks)
    anyio.run(run_modern_stdio, server)


def run_hermes_legacy_stdio(server: MCPServer) -> None:
    """Run the explicit, deployment-bounded legacy facade for Hermes Agent.

    Hermes currently identifies its SDK client generically as ``mcp`` and
    negotiates the 2025-11-25 era.  Keeping this in a separate executable
    avoids making the authoritative stdio endpoint dual-era for every client.
    """

    server.run("stdio")


def main_hermes_legacy() -> None:
    config = RuntimeConfig.from_environment(require_http_auth=False)
    backend = HindsightMemoryBackend(
        base_url=config.hindsight_url,
        api_key=config.hindsight_api_key,
    )
    server = create_mcp_server(
        backend,
        allowed_banks=config.banks,
    )
    run_hermes_legacy_stdio(server)


if __name__ == "__main__":
    main()
