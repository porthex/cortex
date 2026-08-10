"""Real-socket conformance proxy that injects the fixture bearer token."""

from tests.http_fixture import app as candidate


async def app(scope, receive, send):
    if scope["type"] == "http":
        scope = dict(scope)
        headers = list(scope.get("headers", []))
        headers.append((b"authorization", b"Bearer test-token"))
        scope["headers"] = headers
    await candidate(scope, receive, send)
