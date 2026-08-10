# Unified Corthex memory architecture

Corthex is the product and integration layer. [Hindsight](https://github.com/vectorize-io/hindsight) remains the underlying Apache-2.0 semantic-memory engine and is credited as such.

## Authoritative topology

```text
Hermes lifecycle hooks ──loopback──> Corthex bank
Windows trusted clients ──authenticated private VPN gateway──> Corthex bank
                                               │
                                               └── Hindsight + PostgreSQL
```

The authoritative bank id is `corthex`. The legacy `hermes` (VPS) and `cortex` (Windows) banks are immutable migration sources: this workflow never clears or deletes either source. A migration into `corthex` is idempotent because each canonical memory is retained with a stable `corthex-<sha256>` document id and `update_mode=replace`.

Hindsight performs semantic extraction, linking, recall, reflection, and observations. `corthex.migration` adds:

- deterministic NFKC/whitespace/case-folded SHA-256 deduplication;
- stable ordering independent of source order;
- source bank, source memory id, source timestamp, source category, and any metadata exposed by the source export API in `corthex_*` metadata;
- source/category tags and shared observation scope;
- fail-closed validation, backup verification, and atomic Hermes configuration with rollback.

Exact duplicates collapse to one Corthex document. All source provenance and all observed source categories remain attached. Conflicting non-identical facts are not silently discarded; Hindsight can reason over their timestamps and provenance.

## Proving the sources are distinct

Do not infer separation from bank names. Capture all of the following:

### Windows source

Run on the Windows machine in the repository checkout:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Export-CorthexMigration.ps1
```

The script verifies the live loopback API/database, creates native full and `--include-history` bank backups using the exact Windows baseline, exports the `cortex` bank to JSONL, inventories its configuration/mental models/directives, writes SHA-256 hashes, and verifies every byte. It does not mutate the source bank.

Baseline configuration independently identifies:

- Hindsight 0.8.4;
- source bank `cortex`;
- Hindsight API `127.0.0.1:8888`;
- authenticated gateway `127.0.0.1:8877`;
- dedicated PostgreSQL `127.0.0.1:5432`.

### VPS source

```bash
hermes config get memory
curl --fail --silent http://127.0.0.1:9177/health
python3 -m corthex.migration --url http://127.0.0.1:9177 inventory --bank hermes
ss -lntp | grep -E ':(9177|5433)\b'
```

Record the live API version, database binding, bank stats/counts, mental models, directives, and Hermes provider config. A distinct host, API process/port, PostgreSQL instance/port, bank record, and storage root prove backend separation.

## Backup gate

No apply or configuration switch is permitted until both sources have independently verified manifests.

VPS production backup scope:

1. matching-version PostgreSQL custom-format dump (`pg_dump --format=custom`);
2. `pg_restore --list` validation;
3. Hindsight memory JSONL and inventory/template export;
4. Hermes Hindsight profile/config state;
5. SHA-256 + byte count manifest, stored owner-only.

Never copy a live PostgreSQL data directory as the primary backup. Never delete a source bank.

## Deterministic merge

After securely transferring the Windows export to the VPS over the private operator channel:

```bash
python3 -m corthex.migration plan \
  vps-hermes=/secure/backups/vps-hermes-memories.jsonl \
  windows-cortex=/secure/backups/windows-cortex-memories.jsonl \
  --output /secure/backups/corthex-plan.jsonl

# Inspect counts and sample provenance first. This creates/updates only corthex.
python3 -m corthex.migration --url http://127.0.0.1:9177 apply \
  --plan /secure/backups/corthex-plan.jsonl \
  --manifest vps-hermes=/secure/backups/vps/SHA256SUMS.json \
  --manifest windows-cortex=/secure/backups/windows/SHA256SUMS.json \
  --bank corthex \
  --workers 4 \
  --confirm

# After counts/provenance are verified, replace migration-time verbatim mode
# with Corthex's selective, memory-defense-enabled operating policy.
python3 -m corthex.migration --url http://127.0.0.1:9177 finalize \
  --bank corthex \
  --confirm
```

Re-running the exact plan is safe and deterministic. The tool refuses malformed JSON, missing content/source, corrupted normalized records, invalid provenance, or an apply without `--confirm`.

Mental models and directives are inventoried separately because they have engine-native IDs/triggers rather than memory-unit semantics. Import them through Hindsight's bank-template dry run first, then import only after review:

```bash
curl --fail --silent http://127.0.0.1:9177/v1/default/banks/hermes/export > hermes-template.json
curl --fail --silent -X POST -H 'Content-Type: application/json' \
  --data-binary @hermes-template.json \
  'http://127.0.0.1:9177/v1/default/banks/corthex/import?dry_run=true'
```

## Hermes cutover and rollback

Hermes continues to use its native `hindsight` provider because that is the engine integration that performs automatic pre-inference recall and post-response retention. The product identity comes from the authoritative `corthex` bank, Corthex missions, and `corthex-hermes` retention source—not from mislabeling the engine.

After migration verification:

```bash
python3 -m corthex.migration configure-hermes \
  --config "$HERMES_HOME/hindsight/config.json"
```

This accepts only the expected `hermes` source or an idempotent `corthex` rerun, creates a byte-preserving adjacent backup, and atomically replaces the provider config. Restart the Hermes gateway/agent process through its normal supervisor so new sessions load the bank.

Rollback is atomic:

```bash
python3 -m corthex.migration rollback-hermes \
  --config "$HERMES_HOME/hindsight/config.json" \
  --backup "$HERMES_HOME/hindsight/config.json.pre-corthex-<sha>.backup"
```

Rollback changes only routing. Neither source nor Corthex data is deleted.

## Acceptance verification

Use unique non-sensitive canaries and record exact API responses:

1. `inventory --bank corthex` reports expected counts and zero pending operations.
2. Retain a canary through a fresh Hermes process/session, not direct API only.
3. A second fresh Hermes session automatically recalls it without an explicit memory tool prompt.
4. `hindsight_reflect` through Hermes synthesizes the canary with related facts.
5. Restart Hindsight/PostgreSQL and the Hermes supervisor; fresh-session recall still succeeds.
6. Exercise atomic rollback to `hermes`, verify old-bank recall, then cut back to `corthex` and verify Corthex recall.
7. Delete only the synthetic canary document after recording evidence; never clear either source bank.

## Security boundary

- Hindsight API and PostgreSQL stay loopback-bound on the authoritative host.
- Any Windows-to-VPS integration must traverse Tailscale/private networking and an authenticated gateway; never publish ports 9177, 5433, 8877, or 8888 to the public Internet.
- Bearer/API keys stay in environment/credential files with owner-only permissions, never in Git or migration manifests.
- Recalled memory is untrusted data, never instructions, authorization, or permission.
- If the Windows machine or its source bank cannot be reached, stop after the VPS-only reversible stage and report the exact Windows export/transfer operation as a blocker. Do not claim a completed unification.
