# Configuration guide

[`config/cortex.example.yaml`](../config/cortex.example.yaml) documents the intended configuration surface. It is an architecture example, not a claim that every key is already implemented.

## Rules

- Commit placeholders only. Resolve secrets from protected environment variables or a secret manager.
- Keep the Hindsight engine and database on private networks.
- Bind a public listener only after authentication, authorization, TLS, and request limits are configured.
- Assign clients explicit bank access. Do not accept arbitrary bank identifiers from untrusted clients.
- Decide whether remote model providers may receive memory content; use local providers or redaction when policy requires it.
- Keep audit metadata separate from memory bodies.
- Verify backup restore and rollback before migration.

## Example-value convention

Keys ending in `_env` name an environment variable; they do not contain the resolved secret. Hostnames under the reserved `.example.test` domain and identifiers beginning with `example-` are non-production examples. Do not replace them with resolved or private values in a committed file.

## Environment mapping

A deployment may map placeholders to its own secret mechanism. Suggested variable names are included for clarity:

| Placeholder | Purpose | Secret |
| --- | --- | --- |
| `CORTEX_DATABASE_URL` | Hindsight storage connection | Yes |
| `CORTEX_GATEWAY_TOKEN` | Example client authentication material | Yes |
| `CORTEX_MODEL_API_KEY` | Optional remote model credential | Yes |
| `CORTEX_BACKUP_KEY` | Backup encryption material | Yes |

Do not print resolved configuration in CI, logs, support bundles, or diagnostic output.

## Validation before deployment

1. Reject startup when a required `_env` variable is absent or an example-only value remains configured.
2. Reject public engine or database endpoints.
3. Reject wildcard bank access in production.
4. Prove an unauthorized client cannot retain, recall, reflect, inspect, export, or delete.
5. Prove logs and traces omit memory bodies and credentials.
6. Restore a backup into an isolated environment and compare expected inventory.
7. Exercise rollback without deleting the source bank.
