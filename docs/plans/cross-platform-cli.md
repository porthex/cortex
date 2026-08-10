# Cross-platform CLI implementation plan

1. Establish packaging, help, platform configuration paths, and token-safe configuration with test-first vertical slices.
2. Add a dependency-free public HTTP client with stable response/error mapping.
3. Add status/connect/doctor and memory/control commands with human and JSON renderers.
4. Exercise commands against an isolated in-process gateway fixture, including retain→recall and reflect, auth denial, disconnects, bank boundaries, and secret redaction.
5. Run deterministic unit/integration tests, package installation smoke tests, compile checks, diff/secret hygiene, and an independent verification subagent.
6. Commit and open a PR with reproducible setup, verification, and rollback instructions.
