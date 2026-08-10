# Contributing to Corthex

Thank you for helping improve Corthex. The project is at an early stage, so small, reviewable changes are preferred.

## Before opening a change

1. Search existing issues and pull requests.
2. Open an issue for changes that affect the architecture, memory semantics, security model, data migration, or public interfaces.
3. Never include credentials, private memory content, database exports, production endpoints, personal data, or machine-specific configuration.
4. Use synthetic examples and placeholders such as `change-me` and `memory.example.test`.

## Development workflow

1. Fork or branch from `main`.
2. Keep each pull request focused on one concern.
3. Update documentation and examples when behavior changes.
4. Run `bash ./scripts/check-repository.sh` from the repository root.
5. Describe security implications, migration behavior, rollback steps, and tests in the pull request.

## Memory and migration changes

Changes that retain, recall, transform, export, or delete memories must be fail-closed and reversible. Include tests for:

- tenant or bank isolation;
- authentication and authorization failures;
- deterministic deduplication;
- provenance and timestamp preservation;
- interrupted migrations and rollback;
- redaction of secrets and sensitive content.

Never use real user memories as fixtures.

## Commit and review expectations

Use clear commit messages. Maintainers may ask contributors to split unrelated changes. A pull request is not accepted until required checks pass and a maintainer reviews the security and data-handling impact.

## Licensing contributions

The repository does not yet declare a Corthex project license or an inbound contribution grant. Until maintainers publish explicit contribution terms, please open issues and discussions but do not submit external code or content for merging. License selection and contribution terms are explicit owner decisions still to be made.
