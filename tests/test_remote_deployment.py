from __future__ import annotations

import http.server
import os
import re
import socketserver
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
DOCS = ROOT / "docs"


class RemoteDeploymentContractTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_systemd_unit_is_loopback_only_and_least_privilege(self) -> None:
        unit = self.read("deploy/cortex-remote.service")
        self.assertIn("User=cortex", unit)
        self.assertIn("Group=cortex", unit)
        self.assertIn("EnvironmentFile=/etc/cortex/cortex.env", unit)
        self.assertIn(
            "ExecStart=/opt/cortex/.venv/bin/cortex-mcp-http",
            unit,
        )
        self.assertNotIn("cortex serve", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("ProtectHome=true", unit)
        self.assertIn("PrivateTmp=true", unit)
        self.assertIn("ReadWritePaths=/var/lib/cortex /var/log/cortex", unit)

    def test_funnel_detector_rejects_real_map_shape(self) -> None:
        checker = ROOT / "deploy/check-serve-private.py"
        private = subprocess.run(
            ["python3", str(checker)], input='{"Web": {}}', text=True, capture_output=True
        )
        public = subprocess.run(
            ["python3", str(checker)],
            input='{"AllowFunnel": {"host.example:443": true}}',
            text=True,
            capture_output=True,
        )
        self.assertEqual(0, private.returncode)
        self.assertNotEqual(0, public.returncode)

    def test_serve_checker_rejects_existing_cortex_path(self) -> None:
        checker = ROOT / "deploy/check-serve-private.py"
        collision = subprocess.run(
            ["python3", str(checker)],
            input='{"Web":{"host:443":{"Handlers":{"/cortex":{"Proxy":"http://127.0.0.1:1"}}}}}',
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(0, collision.returncode)
        self.assertIn("/cortex", collision.stderr)

    def test_serve_checker_allows_only_the_existing_cortex_target_on_upgrade(self) -> None:
        checker = ROOT / "deploy/check-serve-private.py"
        owned = '{"Web":{"host:443":{"Handlers":{"/cortex":{"Proxy":"http://127.0.0.1:8890"}}}}}'
        other = '{"Web":{"host:443":{"Handlers":{"/cortex":{"Proxy":"http://127.0.0.1:9999"}}}}}'
        accepted = subprocess.run(
            ["python3", str(checker), "--allow-owned-cortex"],
            input=owned,
            text=True,
            capture_output=True,
        )
        rejected = subprocess.run(
            ["python3", str(checker), "--allow-owned-cortex"],
            input=other,
            text=True,
            capture_output=True,
        )
        self.assertEqual(0, accepted.returncode, accepted.stderr)
        self.assertNotEqual(0, rejected.returncode)

    def test_local_auth_checker_requires_denial_and_correct_token(self) -> None:
        token = "unit-test-token"

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                status = 200 if self.headers.get("Authorization") == "Bearer " + token else 401
                self.send_response(status)
                self.end_headers()

            def log_message(self, *_args: object) -> None:
                pass

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            env = dict(os.environ, CORTEX_MCP_TOKEN=token)
            checked = subprocess.run(
                [
                    "python3",
                    str(ROOT / "deploy/check-local-auth.py"),
                    f"http://127.0.0.1:{server.server_address[1]}/v1/status",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            server.shutdown()
        self.assertEqual(0, checked.returncode, checked.stderr)

    def test_installer_preserves_existing_tailscale_serve_routes(self) -> None:
        script = self.read("deploy/install-vps.sh")
        self.assertNotIn("tailscale serve reset", script)
        self.assertIn("tailscale serve status --json", script)
        self.assertIn("check-serve-private.py", script)
        self.assertNotIn('grep -q \'"AllowFunnel"', script)
        self.assertIn("existing_token", script)
        self.assertIn(".venv.next", script)
        self.assertIn("systemctl restart cortex-remote.service", script)
        self.assertIn("check-local-auth.py", script)
        self.assertNotIn("Authorization: Bearer", script)
        self.assertRegex(
            script,
            r"tailscale serve --bg --yes --set-path /cortex http://127\.0\.0\.1:8890",
        )
        self.assertIn("install -m 0600", script)
        self.assertIn("systemctl enable cortex-remote.service", script)
        self.assertIn("systemctl restart cortex-remote.service", script)

    def test_installer_fails_closed_before_mutating_a_legacy_corthex_deployment(self) -> None:
        """Narrow compatibility guard for hosts installed before the rename."""
        script = self.read("deploy/install-vps.sh")
        markers = (
            "/etc/corthex/corthex.env",
            "/opt/corthex/.venv",
            "corthex-remote.service",
            "/var/lib/corthex",
        )
        for marker in markers:
            self.assertIn(marker, script)
        guard = script.index("Legacy Corthex deployment detected")
        self.assertLess(guard, script.index("getent group cortex"))
        self.assertIn("docs/RENAME_UPGRADE.md", script)

    def test_installer_writes_mcp_runtime_environment_contract(self) -> None:
        script = self.read("deploy/install-vps.sh")
        for expected in (
            "/opt/cortex/.venv.next/bin/python -c 'import cortex.mcp_http'",
            "CORTEX_MCP_TOKEN",
            "CORTEX_BANKS_JSON",
            "CORTEX_MCP_PUBLIC_URL",
            "CORTEX_MCP_HOST=127.0.0.1",
            "CORTEX_MCP_PORT=8890",
        ):
            self.assertIn(expected, script)
        self.assertNotIn("CORTEX_ALLOWED_BANKS", script)
        self.assertNotIn("CORTEX_TOKEN=", script)
        self.assertNotIn("cortex-mcp-http --help", script)
        self.assertIn("/etc/cortex/backup.env", script)
        unit = self.read("deploy/cortex-remote.service")
        self.assertNotIn("backup.env", unit)

    def test_installer_does_not_relocate_a_virtualenv_after_entrypoints_are_written(self) -> None:
        script = self.read("deploy/install-vps.sh")
        self.assertIn("/opt/cortex/releases", script)
        self.assertIn("ln -s", script)
        self.assertNotIn("python3 -m venv /opt/cortex/.venv.next", script)
        self.assertNotIn("mv /opt/cortex/.venv.next /opt/cortex/.venv", script)
        self.assertLess(
            script.index("trap 'cleanup_release' EXIT"),
            script.index('python3 -m venv "$NEW_RELEASE"'),
        )

    def test_installer_has_transactional_failure_rollback(self) -> None:
        script = self.read("deploy/install-vps.sh")
        self.assertIn("rollback_install", script)
        self.assertIn("trap 'rollback_install", script)
        self.assertIn("cortex.env.previous", script)
        self.assertIn("cortex-remote.service.previous", script)
        self.assertIn("WAS_ACTIVE", script)
        self.assertIn("systemctl is-active --quiet cortex-remote.service", script)

    def test_installer_serializes_and_rechecks_serve_path_before_claiming_it(self) -> None:
        script = self.read("deploy/install-vps.sh")
        self.assertIn("flock 9", script)
        self.assertGreaterEqual(script.count("tailscale serve status --json"), 2)
        self.assertGreaterEqual(script.count("--allow-owned-cortex"), 2)
        self.assertNotIn("tailscale serve --yes --https=443 --set-path /cortex off", script)

    def test_local_auth_checker_uses_mcp_token_environment_name(self) -> None:
        checker = self.read("deploy/check-local-auth.py")
        self.assertIn("CORTEX_MCP_TOKEN", checker)
        self.assertNotIn('os.environ["CORTEX_TOKEN"]', checker)

    def test_backup_and_restore_use_native_postgres_tools_without_leaking_url(self) -> None:
        backup = self.read("deploy/backup.sh")
        restore = self.read("deploy/restore.sh")
        self.assertIn("pg_dump", backup)
        self.assertIn("--format=custom", backup)
        self.assertIn("chmod 0600", backup)
        self.assertNotIn("set -x", backup)
        self.assertIn("pg_restore", restore)
        self.assertIn("--exit-on-error", restore)
        self.assertIn("--single-transaction", restore)
        self.assertIn('sha256sum "$ARCHIVE"', restore)
        self.assertIn("--confirm-empty-target", restore)
        self.assertNotIn("set -x", restore)

    def test_restore_verifies_the_requested_archive_not_checksum_filename(self) -> None:
        restore = ROOT / "deploy/restore.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requested = root / "requested.dump"
            other = root / "other.dump"
            requested.write_bytes(b"requested")
            other.write_bytes(b"other")
            digest = subprocess.check_output(
                ["sha256sum", str(other)], text=True
            ).split()[0]
            (root / "requested.dump.sha256").write_text(
                f"{digest}  {other}\n", encoding="utf-8"
            )
            completed = subprocess.run(
                [
                    "bash",
                    str(restore),
                    str(requested),
                    "postgresql://restore.invalid/test",
                    "--confirm-empty-target",
                ],
                env={
                    **os.environ,
                    "CORTEX_BACKUP_ENV_FILE": str(root / "missing.env"),
                    "PG_RESTORE": "/bin/true",
                    "PSQL": "/bin/true",
                },
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("checksum", completed.stderr.lower())

    def test_shell_scripts_parse(self) -> None:
        for path in sorted(DEPLOY.glob("*.sh")):
            completed = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True, check=False
            )
            self.assertEqual(0, completed.returncode, f"{path}: {completed.stderr}")

    def test_adr_records_bounded_decision_and_threat_matrix(self) -> None:
        adr = self.read("docs/adr/0001-remote-brain-transport.md")
        for phrase in (
            "Tailscale Serve",
            "Direct tailnet port",
            "SSH local forwarding",
            "raw Hindsight",
            "Threat/test matrix",
            "Stop condition",
        ):
            self.assertIn(phrase, adr)
        self.assertGreaterEqual(len(re.findall(r"^\|", adr, flags=re.MULTILINE)), 8)

    def test_readme_documents_remote_brain_without_private_host_details(self) -> None:
        readme = self.read("README.md")
        self.assertIn("## Remote Brain", readme)
        self.assertIn("Tailscale Serve", readme)
        self.assertIn("Hindsight", readme)
        self.assertIn("docs/REMOTE_BRAIN_VPS.md", readme)
        self.assertNotRegex(readme, r"(?:100\.\d+\.\d+\.\d+|tailaf[0-9a-f]+)")

    def test_operator_doc_has_setup_rollback_and_restore_drill(self) -> None:
        doc = self.read("docs/REMOTE_BRAIN_VPS.md")
        for heading in (
            "## Install",
            "## Verify",
            "## Backup and restore drill",
            "## Rollback",
        ):
            self.assertIn(heading, doc)
        for contract in (
            "cortex-mcp-http",
            "CORTEX_BANKS_JSON",
            "CORTEX_MCP_TOKEN",
            "/cortex/mcp",
            "/cortex/v1/status",
        ):
            self.assertIn(contract, doc)
        self.assertNotIn("CORTEX_ALLOWED_BANKS", doc)
        self.assertNotIn("`cortex serve`", doc)
        self.assertNotRegex(doc, r"(?:100\.\d+\.\d+\.\d+|tailaf[0-9a-f]+)")


if __name__ == "__main__":
    unittest.main()
