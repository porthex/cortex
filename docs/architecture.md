# Architecture

Cortex is intended to give multiple AI clients one governed long-term memory boundary. Hindsight supplies the underlying memory engine; the target Cortex architecture adds shared client integration, access control, policy, lifecycle, and operations around it.

> This document defines a target architecture and required security invariants. The preserved Cortex Brain baseline currently provides a loopback, bearer-authenticated gateway to one fixed bank. It does not yet implement or prove the per-client authorization, policy, audit, or migration controls below.

## Logical components

```text
AI clients
    │ authenticated requests
    ▼
Cortex gateway
    ├── identity and tenant/bank authorization
    ├── input validation and request limits
    ├── memory policy and redaction
    └── audit events without memory bodies
    │ private engine API
    ▼
Hindsight memory engine
    ├── retain / recall / reflect
    ├── model providers
    └── metadata and provenance
    │ least-privilege connection
    ▼
Durable storage
    └── encrypted backups and tested restore
```

An optional inspection interface may read through the same authenticated policy boundary. It must not connect directly to storage or bypass tenant isolation.

## Required trust boundaries

1. **Client boundary.** Every client is untrusted until authenticated. Client identity must map explicitly to allowed tenants or banks.
2. **Gateway boundary.** The target gateway must validate requests, apply memory policy, and emit metadata-only audit events. Failure to establish identity or policy state must deny the request.
3. **Engine boundary.** Hindsight is reachable only from Cortex services on a private network. Cortex does not expose the engine or database directly to clients.
4. **Storage boundary.** Runtime credentials are scoped to the required database and injected outside version control. Backups are encrypted and access-controlled separately.
5. **Model boundary.** Remote model calls are a data-egress boundary. Deployments must choose providers and redaction policy appropriate for their data.

## Target operations

### Retain

The target gateway must authenticate the client, validate the target bank, apply retention and redaction policy, then ask Hindsight to retain the permitted content. Provenance should include a stable client identifier, source event identifier, creation time, and policy version.

### Recall

The target gateway must authorize the bank before query execution, apply limits, and filter the result according to current policy. A client must not be able to select an arbitrary bank by naming it in a request.

### Reflect

In the target design, reflection runs as an explicitly authorized operation with bounded inputs and outputs. Generated mental models must remain attached to their source bank and provenance.

## Required invariants

- A request never crosses tenant or bank boundaries.
- Authentication, authorization, and policy failures are fail-closed.
- Raw memory bodies, prompts, and credentials are absent from operational logs.
- Imports are idempotent and preserve source identifiers, timestamps, and provenance.
- Destructive migrations require a verified backup and a tested rollback path.
- No source bank is deleted as part of migration.

## Deployment shape

The architecture does not require a specific orchestrator. A production deployment should keep gateway, engine, and storage on private networks; expose only the authenticated gateway; and inject secret values at runtime. See [the configuration guide](configuration.md).

## Relationship to Hindsight

[Hindsight](https://github.com/vectorize-io/hindsight) is a separate open-source project and the underlying memory engine. Cortex composes and operates Hindsight; it does not fork, copy, or relicense Hindsight source. See [third-party notices](../THIRD_PARTY_NOTICES.md).
