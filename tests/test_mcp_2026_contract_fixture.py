from __future__ import annotations

import json
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "mcp-2026-07-28-contract.json"

REQUIRED_CASES = {
    "direct-request-without-discovery",
    "missing-meta",
    "missing-protocol-version",
    "missing-client-capabilities",
    "unsupported-protocol-version",
    "http-protocol-header-mismatch",
    "http-method-header-mismatch",
    "http-name-header-mismatch",
    "http-get-rejected",
    "http-delete-rejected",
    "legacy-session-header-ignored",
    "stdio-cancellation-notification",
    "http-cancellation-by-stream-close",
    "server-jsonrpc-request-rejected",
    "client-jsonrpc-response-rejected",
    "unauthenticated-http-rejected",
    "wrong-audience-token-rejected",
    "legacy-initialize-diagnostic",
}


def load_fixture() -> dict:
    with FIXTURE.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_fixture_pins_the_modern_protocol_and_official_tools() -> None:
    fixture = load_fixture()

    assert fixture["protocol_version"] == "2026-07-28"
    assert fixture["sdk"] == {"package": "mcp", "version": "2.0.0"}
    assert fixture["inspector"] == {"package": "@modelcontextprotocol/inspector", "version": "2.1.0"}
    assert fixture["conformance"]["package"] == "@modelcontextprotocol/conformance"
    assert fixture["conformance"]["version"] == "0.2.0-alpha.10"


def test_fixture_covers_every_obsolete_architecture_regression() -> None:
    fixture = load_fixture()
    cases = {case["id"]: case for case in fixture["cases"]}

    assert REQUIRED_CASES <= cases.keys()
    assert all(case["required"] for case in cases.values())
    assert cases["direct-request-without-discovery"]["expected"]["accepted"] is True
    assert cases["missing-meta"]["expected"]["jsonrpc_error"] == -32602
    assert cases["missing-protocol-version"]["expected"]["jsonrpc_error"] == -32602
    assert cases["missing-client-capabilities"]["expected"]["jsonrpc_error"] == -32602
    assert cases["unsupported-protocol-version"]["expected"]["jsonrpc_error"] == -32022
    assert cases["http-protocol-header-mismatch"]["expected"]["jsonrpc_error"] == -32020
    assert cases["http-method-header-mismatch"]["expected"]["jsonrpc_error"] == -32020
    assert cases["http-name-header-mismatch"]["expected"]["jsonrpc_error"] == -32020
    assert cases["legacy-session-header-ignored"]["expected"]["response_header_absent"] == "Mcp-Session-Id"
    assert cases["stdio-cancellation-notification"]["request"]["method"] == "notifications/cancelled"
    assert cases["http-cancellation-by-stream-close"]["request"]["signal"] == "close_response_stream"


def test_fixture_does_not_make_legacy_or_extensions_the_core() -> None:
    fixture = load_fixture()

    assert fixture["core"]["initialize_required"] is False
    assert fixture["core"]["protocol_sessions"] is False
    assert fixture["core"]["server_jsonrpc_requests"] is False
    assert fixture["core"]["client_jsonrpc_responses"] is False
    assert fixture["core"]["experimental_extensions"] == []
    assert fixture["legacy"]["mode"] == "bounded-dual-era-only"
    assert fixture["legacy"]["enabled_by_default"] is False
