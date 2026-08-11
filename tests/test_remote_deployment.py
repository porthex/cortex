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

    def run_backup(
        self,
        root: Path,
        path_scripts: dict[str, str],
        *,
        embedded_home: Path | None = None,
        embedded_script: str | None = None,
        extra_env: dict[str, str] | None = None,
        database_url: str = "postgresql://secret@example.invalid/cortex",
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        path_bin = root / "bin"
        path_bin.mkdir()
        for name, contents in path_scripts.items():
            executable = path_bin / name
            executable.write_text(contents, encoding="utf-8")
            executable.chmod(0o755)
        if embedded_home is not None and embedded_script is not None:
            embedded_dump = embedded_home / ".pg0/installation/18.1.0/bin/pg_dump"
            embedded_dump.parent.mkdir(parents=True)
            embedded_dump.write_text(embedded_script, encoding="utf-8")
            embedded_dump.chmod(0o755)
        env_file = root / "backup.env"
        env_file.write_text(f"CORTEX_DATABASE_URL={database_url}\n", encoding="utf-8")
        backups = root / "backups"
        env = dict(os.environ)
        env.pop("PG_DUMP", None)
        env.pop("SUDO_USER", None)
        env.update(
            {
                "HOME": str(root),
                "PATH": f"{path_bin}:/usr/bin:/bin",
                "CORTEX_BACKUP_ENV_FILE": str(env_file),
                "CORTEX_BACKUP_DIR": str(backups),
                **(extra_env or {}),
            }
        )
        completed = subprocess.run(
            ["bash", str(ROOT / "deploy/backup.sh")],
            env=env,
            capture_output=True,
            text=True,
        )
        return completed, backups

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
        self.assertNotIn('--dbname="$DATABASE_URL"', backup)
        self.assertIn("os.setuid(account.pw_uid)", backup)
        self.assertIn("SELECTED_RUN_AS", backup)
        self.assertIn("pg_restore", restore)
        self.assertIn("--exit-on-error", restore)
        self.assertIn("--single-transaction", restore)
        self.assertIn('sha256sum "$ARCHIVE"', restore)
        self.assertIn("--confirm-empty-target", restore)
        self.assertNotIn("set -x", restore)

    def test_backup_selects_matching_embedded_pg_dump_over_older_path_client(self) -> None:
        old_dump = (
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = --version ]; then echo 'pg_dump (PostgreSQL) 16.14'; exit; fi\n"
            "exit 91\n"
        )
        embedded_dump = (
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = --version ]; then echo 'pg_dump (PostgreSQL) 18.1'; exit; fi\n"
            "printf archive\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed, backups = self.run_backup(
                root,
                {"psql": "#!/bin/sh\nprintf '180001\n'\n", "pg_dump": old_dump},
                embedded_home=root,
                embedded_script=embedded_dump,
            )
            archives = list(backups.glob("*.dump"))
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(1, len(archives))
            self.assertEqual(b"archive", archives[0].read_bytes())
            self.assertNotIn("secret", completed.stdout + completed.stderr)

    def test_backup_finds_pg0_client_in_sudo_users_home(self) -> None:
        embedded_dump = (
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = --version ]; then echo 'pg_dump (PostgreSQL) 18.1'; exit; fi\n"
            "printf archive\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sudo_home = root / "home/hermes"
            completed, backups = self.run_backup(
                root,
                {
                    "psql": "#!/bin/sh\nprintf '180001\n'\n",
                    "pg_dump": (
                        "#!/bin/sh\n"
                        "if [ \"${1:-}\" = --version ]; then echo 'pg_dump (PostgreSQL) 16.14'; exit; fi\n"
                        "exit 91\n"
                    ),
                    "getent": f"#!/bin/sh\nprintf 'hermes:x:1000:1000::%s:/bin/bash\n' '{sudo_home}'\n",
                },
                embedded_home=sudo_home,
                embedded_script=embedded_dump,
                extra_env={"HOME": str(root / "root"), "SUDO_USER": "hermes"},
            )
            archives = list(backups.glob("*.dump"))
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(b"archive", archives[0].read_bytes())
            self.assertNotIn("secret", completed.stdout + completed.stderr)

    def test_backup_honors_explicit_pg_dump_command_from_path(self) -> None:
        explicit_dump = (
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = --version ]; then echo 'pg_dump (PostgreSQL) 18.1'; exit; fi\n"
            "printf explicit\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            completed, backups = self.run_backup(
                Path(directory),
                {"psql": "#!/bin/sh\nprintf '180001\n'\n", "custom-pg-dump": explicit_dump},
                extra_env={"PG_DUMP": "custom-pg-dump"},
            )
            archives = list(backups.glob("*.dump"))
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(b"explicit", archives[0].read_bytes())

    def test_backup_accepts_newer_pg_dump_compatible_with_older_server(self) -> None:
        newer_dump = (
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = --version ]; then echo 'pg_dump (PostgreSQL) 19.0'; exit; fi\n"
            "printf newer\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            completed, backups = self.run_backup(
                Path(directory),
                {"psql": "#!/bin/sh\nprintf '180001\n'\n", "pg_dump": newer_dump},
            )
            archives = list(backups.glob("*.dump")) if backups.exists() else []
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(b"newer", archives[0].read_bytes())

    def test_backup_does_not_fallback_when_explicit_pg_dump_is_incompatible(self) -> None:
        embedded_dump = "#!/bin/sh\necho 'pg_dump (PostgreSQL) 18.1'\nexit 90\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed, backups = self.run_backup(
                root,
                {
                    "psql": "#!/bin/sh\nprintf '180001\n'\n",
                    "explicit-pg-dump": "#!/bin/sh\necho 'pg_dump (PostgreSQL) 16.14'\n",
                },
                embedded_home=root,
                embedded_script=embedded_dump,
                extra_env={"PG_DUMP": "explicit-pg-dump"},
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("PG_DUMP", completed.stderr)
            self.assertIn("PostgreSQL server major 18", completed.stderr)
            self.assertFalse(backups.exists())

    def test_backup_version_probe_failure_does_not_leak_database_url_or_create_archive(self) -> None:
        database_url = "postgresql://user:top-secret@example.invalid/cortex"
        with tempfile.TemporaryDirectory() as directory:
            completed, backups = self.run_backup(
                Path(directory),
                {"psql": "#!/bin/sh\nprintf 'probe failed for %s\n' \"$*\" >&2\nexit 7\n"},
                database_url=database_url,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("Cannot determine", completed.stderr)
            self.assertNotIn(database_url, completed.stdout + completed.stderr)
            self.assertFalse(backups.exists())

    def test_backup_keeps_database_url_out_of_postgres_client_arguments(self) -> None:
        database_url = "postgresql://user:top-secret@example.invalid/cortex"
        psql = (
            "#!/bin/sh\n"
            "printf 'psql:%s\n' \"$*\" >>\"$ARG_LOG\"\n"
            "[ \"$PGHOST\" = example.invalid ] && [ \"$PGUSER\" = user ] && "
            "[ \"$PGPASSWORD\" = top-secret ] && [ \"$PGDATABASE\" = cortex ] && "
            "[ -z \"${PGPORT:-}\" ] && [ -z \"${PGPASSFILE:-}\" ] && "
            "[ -z \"${PGCHANNELBINDING:-}\" ] || exit 12\n"
            "printf '180001\n'\n"
        )
        pg_dump = (
            "#!/bin/sh\n"
            "printf 'pg_dump:%s\n' \"$*\" >>\"$ARG_LOG\"\n"
            "if [ \"${1:-}\" = --version ]; then echo 'pg_dump (PostgreSQL) 18.1'; exit; fi\n"
            "printf archive\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argument_log = root / "arguments.log"
            completed, _ = self.run_backup(
                root,
                {"psql": psql, "pg_dump": pg_dump},
                extra_env={
                    "ARG_LOG": str(argument_log),
                    "PGPORT": "9999",
                    "PGPASSFILE": "/tmp/ambient-passfile",
                    "PGCHANNELBINDING": "require",
                },
                database_url=database_url,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertNotIn(database_url, argument_log.read_text(encoding="utf-8"))

    def test_backup_rejects_unsafe_or_unsupported_database_urls_before_client_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            for index, database_url in enumerate(
                (
                    "postgresql://user:bad%00value@example.invalid/cortex",
                    "postgresql://user:password@example.invalid/cortex?unknown=value",
                )
            ):
                with self.subTest(database_url=database_url):
                    root = parent / str(index)
                    root.mkdir()
                    invoked = root / "invoked"
                    completed, backups = self.run_backup(
                        root,
                        {"psql": f"#!/bin/sh\nprintf invoked >'{invoked}'\n"},
                        extra_env={"PGDATABASE": "ambient"},
                        database_url=database_url,
                    )
                    self.assertNotEqual(0, completed.returncode)
                    self.assertFalse(invoked.exists())
                    self.assertFalse(backups.exists())

    def test_backup_dump_failure_removes_partial_archive_without_leaking_database_url(self) -> None:
        database_url = "postgresql://user:top-secret@example.invalid/cortex"
        failing_dump = (
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = --version ]; then echo 'pg_dump (PostgreSQL) 18.1'; exit; fi\n"
            "printf partial\n"
            "printf 'dump failed for %s\n' \"$*\" >&2\nexit 9\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            completed, backups = self.run_backup(
                Path(directory),
                {"psql": "#!/bin/sh\nprintf '180001\n'\n", "pg_dump": failing_dump},
                database_url=database_url,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("Backup failed", completed.stderr)
            self.assertNotIn(database_url, completed.stdout + completed.stderr)
            self.assertEqual([], list(backups.glob("*")))

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
