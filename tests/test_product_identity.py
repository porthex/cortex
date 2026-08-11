import importlib
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY = "cort" + "hex"
TEXT_SUFFIXES = {
    ".cmd",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".service",
    ".sh",
    ".svg",
    ".toml",
    ".txt",
    ".vbs",
    ".yaml",
    ".yml",
}


class ProductIdentityTests(unittest.TestCase):
    def test_source_tree_uses_cortex_identity_exclusively(self):
        residual_paths = []
        residual_text = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in {".git", ".venv", "build", "dist"} for part in path.parts):
                continue
            relative = path.relative_to(ROOT).as_posix()
            if LEGACY in relative.casefold():
                residual_paths.append(relative)
            if path.suffix.casefold() in TEXT_SUFFIXES:
                text = path.read_text(encoding="utf-8")
                if LEGACY in text.casefold():
                    residual_text.append(relative)

        self.assertEqual(residual_paths, [])
        self.assertEqual(residual_text, [])

    def test_cortex_package_metadata_does_not_claim_an_undecided_license(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        project_section = pyproject.split("[project]", 1)[1].split("\n[", 1)[0]
        licensing_status = (ROOT / "LICENSES.md").read_text(encoding="utf-8")

        self.assertIsNone(re.search(r"(?m)^\s*license\s*=", project_section))
        self.assertIn("Cortex does not currently include a project license.", licensing_status)
        self.assertIn("Hindsight", licensing_status)
        self.assertIn("MIT License", licensing_status)

    def test_repository_hygiene_rejects_license_metadata_while_status_is_undecided(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "check-repository.sh").write_bytes(
                (ROOT / "scripts" / "check-repository.sh").read_bytes()
            )
            (root / "pyproject.toml").write_text(
                '[project]\nname = "cortex"\nversion = "0.1.0"\nlicense = "MIT"\n',
                encoding="utf-8",
            )
            (root / "LICENSES.md").write_text(
                "Cortex does not currently include a project license.\n",
                encoding="utf-8",
            )
            for relative in (
                "README.md",
                "CONTRIBUTING.md",
                "SECURITY.md",
                "THIRD_PARTY_NOTICES.md",
                "docs/architecture.md",
                "docs/configuration.md",
                "config/cortex.example.yaml",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)

            result = subprocess.run(
                ["bash", "./scripts/check-repository.sh"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("package metadata declares a Cortex license while LICENSES.md says it is undecided", result.stderr)

    def test_documented_test_gate_installs_locked_test_dependencies(self):
        canonical_gate = "uv run --locked --extra test pytest -q\npython -m compileall -q src tests"
        stale_gate = "PYTHONPATH=src python -m unittest discover -s tests -v"

        for relative in ("README.md", "docs/cli.md"):
            with self.subTest(relative=relative):
                documentation = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(canonical_gate, documentation)
                self.assertNotIn(stale_gate, documentation)

    def test_cortex_package_and_documentation_assets_exist(self):
        self.assertIsNotNone(importlib.import_module("cortex"))
        for relative in (
            "assets/cortex-mark.svg",
            "assets/cortex-shared-memory-flow.svg",
            "assets/cortex-remote-brain.svg",
            "config/cortex.example.yaml",
            "deploy/cortex-remote.service",
            "docs/adr/0001-cortex-mcp-surface.md",
            "scripts/Export-CortexMigration.ps1",
        ):
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file())


if __name__ == "__main__":
    unittest.main()
