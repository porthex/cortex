import io
import json
import tempfile
import unittest
from pathlib import Path

from cortex.cli import main
from tests.fake_gateway import IsolatedGateway


class ConfigureCommandTests(unittest.TestCase):
    def test_configure_writes_non_secret_config_and_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cortex" / "config.json"
            stdout = io.StringIO()
            stderr = io.StringIO()

            code = main(
                [
                    "--json",
                    "configure",
                    "--url",
                    "https://brain.example.test",
                    "--bank",
                    "test-bank",
                    "--timeout",
                    "12.5",
                ],
                environ={
                    "CORTEX_CONFIG": str(config_path),
                    "CORTEX_TOKEN": "must-not-be-persisted",
                },
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(code, 0, stderr.getvalue())
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved,
                {
                    "bank": "test-bank",
                    "timeout": 12.5,
                    "url": "https://brain.example.test",
                },
            )
            self.assertNotIn("must-not-be-persisted", config_path.read_text())
            response = json.loads(stdout.getvalue())
            self.assertTrue(response["ok"])
            self.assertEqual(response["command"], "configure")
            self.assertEqual(response["data"]["bank"], "test-bank")
            self.assertNotIn("must-not-be-persisted", stdout.getvalue())


class StatusCommandTests(unittest.TestCase):
    def test_status_uses_authenticated_public_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory, IsolatedGateway() as gateway:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"url": gateway.url, "bank": "isolated", "timeout": 2}),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            code = main(
                ["--json", "status"],
                environ={
                    "CORTEX_CONFIG": str(config_path),
                    "CORTEX_TOKEN": gateway.token,
                },
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(code, 0, stderr.getvalue())
            response = json.loads(stdout.getvalue())
            self.assertEqual(response["data"]["state"], "ready")
            self.assertEqual(gateway.requests[-1]["path"], "/v1/status")
            self.assertEqual(
                gateway.requests[-1]["authorization"], f"Bearer {gateway.token}"
            )
            self.assertNotIn(gateway.token, stdout.getvalue())


class MemoryCommandTests(unittest.TestCase):
    def run_json(self, arguments: list[str], config_path: Path, gateway: IsolatedGateway) -> tuple[int, dict]:
        stdout = io.StringIO()
        code = main(
            ["--json", *arguments],
            environ={"CORTEX_CONFIG": str(config_path), "CORTEX_TOKEN": gateway.token},
            stdout=stdout,
            stderr=io.StringIO(),
        )
        return code, json.loads(stdout.getvalue())

    def test_retain_recall_reflect_and_banks_preserve_bank_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory, IsolatedGateway() as gateway:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"url": gateway.url, "bank": "isolated-a", "timeout": 2}),
                encoding="utf-8",
            )

            code, retained = self.run_json(["retain", "durable alpha fact"], config_path, gateway)
            self.assertEqual(code, 0)
            self.assertTrue(retained["data"]["retained"])
            code, recalled = self.run_json(["recall", "alpha"], config_path, gateway)
            self.assertEqual(code, 0)
            self.assertEqual(recalled["data"]["memories"], ["durable alpha fact"])
            code, reflected = self.run_json(["reflect", "what matters?"], config_path, gateway)
            self.assertEqual(code, 0)
            self.assertEqual(reflected["data"]["bank"], "isolated-a")
            code, banks = self.run_json(["banks"], config_path, gateway)
            self.assertEqual(code, 0)
            self.assertEqual(banks["data"]["banks"], ["isolated-a"])

            memory_requests = [request for request in gateway.requests if "/memories/" in request["path"]]
            self.assertEqual({request["payload"]["bank"] for request in memory_requests}, {"isolated-a"})


class FailureContractTests(unittest.TestCase):
    def test_malformed_gateway_shapes_return_stable_invalid_response_without_leakage(self) -> None:
        malformed_responses = [
            (200, {"ok": False, "error": "malformed"}),
            (200, {"ok": False, "error": None}),
            (200, {"ok": False, "error": {}}),
            (200, {"ok": False, "error": {"code": [], "message": "no"}}),
            (200, {"ok": "false", "error": {"code": "rejected", "message": "no"}}),
            (200, {"ok": True}),
            (200, "malformed-direct-response"),
            (401, {"error": "malformed"}),
            (401, {"error": {}}),
            (500, {"error": "malformed"}),
            (500, {"error": {"code": "failed"}}),
        ]

        with tempfile.TemporaryDirectory() as directory, IsolatedGateway() as gateway:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"url": gateway.url, "bank": "isolated", "timeout": 2}),
                encoding="utf-8",
            )
            environment = {
                "CORTEX_CONFIG": str(config_path),
                "CORTEX_TOKEN": gateway.token,
            }

            for status_code, body in malformed_responses:
                with self.subTest(status_code=status_code, body=body):
                    gateway.status_code = status_code
                    gateway.status_body = body
                    stdout = io.StringIO()
                    stderr = io.StringIO()

                    code = main(
                        ["--json", "status"],
                        environ=environment,
                        stdout=stdout,
                        stderr=stderr,
                    )

                    self.assertEqual(code, 7)
                    self.assertEqual(
                        json.loads(stdout.getvalue()),
                        {
                            "ok": False,
                            "command": "status",
                            "data": None,
                            "error": {
                                "code": "invalid_response",
                                "message": "Cortex returned an invalid response",
                                "retryable": False,
                            },
                        },
                    )
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertNotIn(gateway.token, stdout.getvalue() + stderr.getvalue())

    def test_json_usage_error_is_a_stable_json_envelope(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            ["--json", "configure", "--url", "https://brain.example.test"],
            environ={},
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"]["code"], "usage_error")
        self.assertEqual(payload["command"], "configure")
        self.assertEqual(stderr.getvalue(), "")

    def test_wrong_token_and_disconnect_have_stable_exit_codes_without_secret_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            with IsolatedGateway() as gateway:
                config_path.write_text(
                    json.dumps({"url": gateway.url, "bank": "isolated", "timeout": 0.2}),
                    encoding="utf-8",
                )
                stdout = io.StringIO()
                wrong_token = "wrong-secret-token"
                code = main(
                    ["--json", "status"],
                    environ={"CORTEX_CONFIG": str(config_path), "CORTEX_TOKEN": wrong_token},
                    stdout=stdout,
                    stderr=io.StringIO(),
                )
                self.assertEqual(code, 3)
                self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "authentication_failed")
                self.assertNotIn(wrong_token, stdout.getvalue())

            disconnected_output = io.StringIO()
            code = main(
                ["--json", "status"],
                environ={"CORTEX_CONFIG": str(config_path), "CORTEX_TOKEN": "still-secret"},
                stdout=disconnected_output,
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 6)
            self.assertEqual(json.loads(disconnected_output.getvalue())["error"]["code"], "connection_failed")
            self.assertNotIn("still-secret", disconnected_output.getvalue())

    def test_configure_rejects_cleartext_remote_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            code = main(
                ["--json", "configure", "--url", "http://brain.example.test", "--bank", "x"],
                environ={"CORTEX_CONFIG": str(Path(directory) / "config.json")},
                stdout=output,
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_configuration")

    def test_configure_rejects_non_finite_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            code = main(
                ["--json", "configure", "--url", "https://brain.example.test", "--bank", "x", "--timeout", "nan"],
                environ={"CORTEX_CONFIG": str(Path(directory) / "config.json")},
                stdout=output,
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_configuration")

    def test_explicit_empty_bank_is_rejected_instead_of_using_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory, IsolatedGateway() as gateway:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"url": gateway.url, "bank": "default", "timeout": 2}), encoding="utf-8"
            )
            output = io.StringIO()
            code = main(
                ["--json", "retain", "fact", "--bank", ""],
                environ={"CORTEX_CONFIG": str(config_path), "CORTEX_TOKEN": gateway.token},
                stdout=output,
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_configuration")


class ConnectionCommandTests(unittest.TestCase):
    def test_connect_accepts_token_on_stdin_without_persisting_or_printing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory, IsolatedGateway() as gateway:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"url": gateway.url, "bank": "isolated", "timeout": 2}),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            code = main(
                ["--json", "connect", "--token-stdin"],
                environ={"CORTEX_CONFIG": str(config_path)},
                stdin=io.StringIO(gateway.token + "\n"),
                stdout=stdout,
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["data"]["state"], "ready")
            self.assertNotIn(gateway.token, stdout.getvalue())
            self.assertNotIn(gateway.token, config_path.read_text())

    def test_doctor_reports_transport_credentials_and_reachability(self) -> None:
        with tempfile.TemporaryDirectory() as directory, IsolatedGateway() as gateway:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"url": gateway.url, "bank": "isolated", "timeout": 2}),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            code = main(
                ["--json", "doctor"],
                environ={"CORTEX_CONFIG": str(config_path), "CORTEX_TOKEN": gateway.token},
                stdout=stdout,
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 0)
            checks = json.loads(stdout.getvalue())["data"]["checks"]
            self.assertEqual(checks, {"configuration": "ok", "credentials": "ok", "reachability": "ok", "transport": "ok"})


class OperatorCommandTests(unittest.TestCase):
    def test_start_and_stop_use_public_control_contract_with_human_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory, IsolatedGateway() as gateway:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"url": gateway.url, "bank": "isolated", "timeout": 2}),
                encoding="utf-8",
            )
            environment = {"CORTEX_CONFIG": str(config_path), "CORTEX_TOKEN": gateway.token}
            started = io.StringIO()
            stopped = io.StringIO()

            self.assertEqual(main(["start"], environ=environment, stdout=started, stderr=io.StringIO()), 0)
            self.assertIn("state: ready", started.getvalue())
            self.assertEqual(main(["stop"], environ=environment, stdout=stopped, stderr=io.StringIO()), 0)
            self.assertIn("state: stopped", stopped.getvalue())
            self.assertEqual(
                [request["path"] for request in gateway.requests[-2:]],
                ["/v1/control/start", "/v1/control/stop"],
            )


if __name__ == "__main__":
    unittest.main()
