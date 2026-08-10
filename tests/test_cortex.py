import json
import tempfile
import unittest
from pathlib import Path

from cortex.migration import (
    CortexError,
    HindsightAPI,
    apply_items,
    build_retain_item,
    deduplicate_records,
    load_jsonl,
    normalize_record,
    rollback_hermes_config,
    update_hermes_config,
    validate_retain_item,
    verify_backup_manifest,
)


class NormalizeTests(unittest.TestCase):
    def test_normalize_preserves_source_timestamp_category_and_metadata(self):
        record = {
            "id": "m-1",
            "text": "  User   likes dark mode. ",
            "type": "observation",
            "date": "2026-08-01T12:30:00Z",
            "context": "settings",
            "metadata": {"channel": "chat"},
        }
        got = normalize_record(record, source="windows-cortex")
        self.assertEqual(got["content"], "User likes dark mode.")
        self.assertEqual(got["timestamp"], "2026-08-01T12:30:00Z")
        self.assertEqual(got["category"], "observation")
        self.assertEqual(got["provenance"][0]["source"], "windows-cortex")
        self.assertEqual(got["provenance"][0]["source_id"], "m-1")
        self.assertEqual(got["source_metadata"], {"channel": "chat"})

    def test_normalize_reads_hindsight_fact_type(self):
        got = normalize_record(
            {"id": "m-2", "text": "A fact", "fact_type": "experience", "date": "2026-08-01T00:00:00Z"},
            "vps-hermes",
        )
        self.assertEqual(got["category"], "experience")

    def test_normalize_preserves_an_already_normalized_export(self):
        original = normalize_record(
            {"id": "m-9", "text": "A fact", "type": "world", "date": "2026-08-01T00:00:00Z"},
            "vps-hermes",
        )
        replayed = normalize_record(original, "vps-hermes")
        self.assertEqual(replayed, original)
        self.assertEqual(replayed["provenance"][0]["source_id"], "m-9")

    def test_normalize_fails_closed_without_source_or_content(self):
        with self.assertRaises(CortexError):
            normalize_record({"text": "x"}, source="")
        with self.assertRaises(CortexError):
            normalize_record({"id": "x"}, source="vps-hermes")


class DedupTests(unittest.TestCase):
    def test_dedup_is_deterministic_and_combines_provenance(self):
        a = normalize_record(
            {"id": "a", "text": "User likes  dark mode", "type": "world", "date": "2026-08-02T00:00:00Z"},
            "vps-hermes",
        )
        b = normalize_record(
            {"id": "b", "text": " user likes dark mode ", "type": "observation", "date": "2026-08-01T00:00:00Z"},
            "windows-cortex",
        )
        first = deduplicate_records([a, b])
        second = deduplicate_records([b, a])
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["timestamp"], "2026-08-01T00:00:00Z")
        self.assertEqual([p["source"] for p in first[0]["provenance"]], ["vps-hermes", "windows-cortex"])
        self.assertEqual(first[0]["categories"], ["observation", "world"])

    def test_dedup_rejects_provenance_identity_collision(self):
        a = normalize_record({"id": "same", "text": "Same fact", "fact_type": "world", "metadata": {"v": "1"}}, "source")
        b = normalize_record({"id": "same", "text": "same fact", "fact_type": "world", "metadata": {"v": "2"}}, "source")
        with self.assertRaises(CortexError):
            deduplicate_records([a, b])

    def test_dedup_rejects_one_source_identity_with_different_content(self):
        a = normalize_record({"id": "same", "text": "Fact A", "fact_type": "world"}, "source")
        b = normalize_record({"id": "same", "text": "Fact B", "fact_type": "world"}, "source")
        with self.assertRaises(CortexError):
            deduplicate_records([a, b])

    def test_retain_item_is_idempotent_and_serializes_provenance(self):
        record = normalize_record(
            {"id": "m-1", "text": "A durable fact", "type": "world", "date": "2026-08-01T00:00:00Z"},
            "vps-hermes",
        )
        item = build_retain_item(record)
        self.assertTrue(item["document_id"].startswith("cortex-"))
        self.assertEqual(item["update_mode"], "replace")
        self.assertEqual(item["observation_scopes"], "shared")
        provenance = json.loads(item["metadata"]["cortex_provenance"])
        self.assertEqual(provenance[0]["source_id"], "m-1")
        self.assertIn("cortex", item["tags"])
        self.assertEqual(validate_retain_item(item), {"vps-hermes"})

    def test_retain_plan_validation_rejects_tampering(self):
        record = normalize_record({"id": "m", "text": "Fact", "fact_type": "world"}, "vps-hermes")
        item = build_retain_item(record)
        item["content"] = "Tampered"
        with self.assertRaises(CortexError):
            validate_retain_item(item)


class APIPolicyTests(unittest.TestCase):
    def test_plain_http_is_allowed_only_on_loopback(self):
        HindsightAPI("http://127.0.0.1:9177")
        with self.assertRaises(CortexError):
            HindsightAPI("http://100.64.0.2:9177", api_key="secret")

    def test_remote_https_requires_authentication(self):
        with self.assertRaises(CortexError):
            HindsightAPI("https://memory.example.test")
        HindsightAPI("https://memory.example.test", api_key="secret")

    def test_api_url_rejects_embedded_credentials_query_and_fragment(self):
        for url in (
            "https://user:secret@memory.example.test",
            "http://user:secret@localhost:9177",
            "https://memory.example.test?token=secret",
            "https://memory.example.test/#secret",
        ):
            with self.subTest(url=url), self.assertRaises(CortexError):
                HindsightAPI(url, api_key="separate-secret")

    def test_operational_policy_is_selective_and_defensive(self):
        class RecordingAPI(HindsightAPI):
            def __init__(self):
                self.calls = []

            def request(self, method, path, payload=None):
                self.calls.append((method, path, payload))
                return payload

        api = RecordingAPI()
        api.configure_operational("cortex-shared")
        method, path, payload = api.calls[-1]
        self.assertEqual((method, path), ("PATCH", "/v1/default/banks/cortex-shared/config"))
        self.assertEqual(payload["updates"]["retain_extraction_mode"], "concise")
        self.assertTrue(payload["updates"]["memory_defense"]["enabled"])
        self.assertFalse(payload["updates"]["enable_observations"])


class ApplyTests(unittest.TestCase):
    def test_apply_refuses_every_non_cortex_bank(self):
        class FakeAPI:
            def ensure_cortex_bank(self, bank):
                raise AssertionError("must fail before API mutation")

        for source_bank in ("hermes", "cortex", "other"):
            with self.subTest(source_bank=source_bank), self.assertRaises(CortexError):
                apply_items(FakeAPI(), source_bank, [{"content": "x"}])

    def test_apply_batches_every_item_exactly_once(self):
        class FakeAPI:
            def __init__(self):
                self.banks = []
                self.batches = []

            def ensure_cortex_bank(self, bank):
                self.banks.append(bank)

            def retain(self, bank, items):
                self.batches.append((bank, list(items)))

        api = FakeAPI()
        items = [{"content": str(i)} for i in range(53)]
        apply_items(api, "cortex-shared", items, workers=4, batch_size=25)
        self.assertEqual(api.banks, ["cortex-shared"])
        self.assertEqual(sorted(i["content"] for _, batch in api.batches for i in batch), sorted(i["content"] for i in items))
        self.assertTrue(all(bank == "cortex-shared" and len(batch) <= 25 for bank, batch in api.batches))


class HermesConfigTests(unittest.TestCase):
    def test_update_is_atomic_reversible_and_brands_the_cortex_bank(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            original = {"mode": "local_embedded", "bank_id": "hermes", "retain_source": "hermes-agent"}
            path.write_text(json.dumps(original))
            backup = update_hermes_config(path)
            updated = json.loads(path.read_text())
            self.assertEqual(updated["bank_id"], "cortex-shared")
            self.assertEqual(updated["retain_source"], "cortex-hermes")
            self.assertIn("Cortex", updated["bank_mission"])
            self.assertEqual(json.loads(backup.read_text()), original)
            rollback_hermes_config(path, backup)
            self.assertEqual(json.loads(path.read_text()), original)

    def test_idempotent_update_reuses_the_legacy_backup(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(json.dumps({"bank_id": "hermes"}))
            backup = update_hermes_config(path)
            self.assertEqual(update_hermes_config(path), backup)

    def test_rollback_rejects_tampered_backup(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(json.dumps({"bank_id": "hermes"}))
            backup = update_hermes_config(path)
            backup.write_text(json.dumps({"bank_id": "hermes", "tampered": True}))
            with self.assertRaises(CortexError):
                rollback_hermes_config(path, backup)

    def test_rollback_refuses_to_overwrite_an_unrelated_current_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(json.dumps({"bank_id": "hermes"}))
            backup = update_hermes_config(path)
            unrelated = b'{"bank_id":"some-other-bank","owner":"unrelated"}'
            path.write_bytes(unrelated)

            with self.assertRaises(CortexError):
                rollback_hermes_config(path, backup)

            self.assertEqual(path.read_bytes(), unrelated)

    def test_update_fails_closed_if_source_bank_is_unexpected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(json.dumps({"bank_id": "some-other-bank"}))
            with self.assertRaises(CortexError):
                update_hermes_config(path)


class LegacyCorthexCompatibilityTests(unittest.TestCase):
    """Narrow upgrade fixtures for configurations written before the rename."""

    def test_update_converts_legacy_product_bank_without_losing_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            legacy = {"mode": "local_embedded", "bank_id": "corthex", "retain_source": "corthex-hermes"}
            path.write_text(json.dumps(legacy))

            backup = update_hermes_config(path)

            self.assertEqual(json.loads(path.read_text())["bank_id"], "cortex-shared")
            self.assertEqual(json.loads(backup.read_text()), legacy)
            rollback_hermes_config(path, backup)
            self.assertEqual(json.loads(path.read_text()), legacy)


class FileSafetyTests(unittest.TestCase):
    @staticmethod
    def _manifest_for(root, migration_source="vps-hermes"):
        import hashlib

        names = ["memories.jsonl", "inventory.json", "bank.backup", "backend.pgdump"]
        for index, name in enumerate(names, 1):
            (root / name).write_bytes(f"artifact-{index}\n".encode())
        return {
            "schema_version": 1,
            "migration_source": migration_source,
            "source_bank": "hermes",
            "engine": "Hindsight",
            "api_version": "0.8.6",
            "required_artifacts": {
                "memory_export": names[0],
                "inventory_export": names[1],
                "native_bank_backup": names[2],
                "full_backend_backup": names[3],
            },
            "files": [
                {
                    "name": name,
                    "bytes": (root / name).stat().st_size,
                    "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
                }
                for name in names
            ],
        }

    def test_jsonl_rejects_invalid_line(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.jsonl"
            p.write_text('{"id":"ok","text":"x"}\nnot-json\n')
            with self.assertRaises(CortexError):
                load_jsonl(p, "source")

    def test_manifest_accepts_windows_powershell_utf8_bom(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self._manifest_for(root, migration_source="windows-cortex")
            path = root / "SHA256SUMS.json"
            path.write_text(json.dumps(manifest), encoding="utf-8-sig")
            verified = verify_backup_manifest(path, expected_source="windows-cortex")
            self.assertEqual(verified["migration_source"], "windows-cortex")

    def test_manifest_verification_detects_tampering(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self._manifest_for(root)
            (root / "SHA256SUMS.json").write_text(json.dumps(manifest))
            verify_backup_manifest(root / "SHA256SUMS.json", expected_source="vps-hermes")
            (root / "memories.jsonl").write_bytes(b"tampered\n")
            with self.assertRaises(CortexError):
                verify_backup_manifest(root / "SHA256SUMS.json")

    def test_manifest_rejects_empty_missing_or_relabelled_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "SHA256SUMS.json"
            path.write_text(json.dumps({"schema_version": 1, "files": []}))
            with self.assertRaises(CortexError):
                verify_backup_manifest(path, expected_source="vps-hermes")

            manifest = self._manifest_for(root)
            manifest["required_artifacts"].pop("full_backend_backup")
            path.write_text(json.dumps(manifest))
            with self.assertRaises(CortexError):
                verify_backup_manifest(path, expected_source="vps-hermes")

            manifest = self._manifest_for(root, migration_source="windows-cortex")
            path.write_text(json.dumps(manifest))
            with self.assertRaises(CortexError):
                verify_backup_manifest(path, expected_source="vps-hermes")


if __name__ == "__main__":
    unittest.main()
