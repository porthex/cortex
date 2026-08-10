# Security policy

Cortex handles durable AI memory and should be treated as a sensitive data system.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's **Report a vulnerability** flow in the repository Security tab. Include:

- the affected component and revision;
- reproduction steps using synthetic data;
- expected and observed behavior;
- potential impact;
- any mitigation you have already tested.

Do not include real credentials, private memories, database dumps, or personal data. Maintainers will acknowledge a complete report when operationally possible and coordinate disclosure after a fix is available.

## Supported versions

Cortex is currently pre-release. Security fixes are applied to the latest revision of `main`; no stable version support window has been announced.

## Deployment baseline

A deployment should:

- keep the memory service on a private network;
- authenticate every client and authorize access per tenant or bank;
- terminate encrypted transport at a trusted boundary;
- use dedicated, least-privilege credentials;
- encrypt backups and test restore procedures;
- redact secrets and sensitive data from logs and traces;
- define retention, deletion, and export policies;
- record administrative operations without recording memory bodies;
- fail closed when identity, policy, or storage dependencies are unavailable.

The example configuration is illustrative and is not a production security profile. Replace every placeholder through a secret manager or protected runtime environment; never commit resolved values.

## Sensitive artifacts

The following must not be committed:

- `.env` files or resolved configuration;
- API tokens, passwords, private keys, or session material;
- raw memories, prompts, transcripts, embeddings, or model traces;
- database files, exports, snapshots, or backups;
- private hostnames, internal addresses, usernames, or machine paths.

If sensitive data is committed, rotate or revoke it first. Removing it from the latest commit is not sufficient because Git history and forks may retain it.
