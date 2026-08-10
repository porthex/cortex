"""Explicit legacy stdio fixture used only for Hermes compatibility tests."""

from cortex.mcp_stdio import run_hermes_legacy_stdio
from tests.stdio_fixture import server


if __name__ == "__main__":
    run_hermes_legacy_stdio(server)