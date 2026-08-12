import importlib
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROHIBITED_IDENTITY_VARIANTS = (
    "cort" + "hex",
    "cortex" + "memorybrowser",
)


def get_tracked_paths(root: Path) -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    return [entry.decode(errors="surrogateescape") for entry in output.split(b"\0") if entry]


def find_prohibited_identity_residuals(root: Path, tracked_paths: list[str]) -> tuple[list[str], list[str]]:
    residual_paths = []
    residual_text = []
    for relative in tracked_paths:
        if any(variant in relative.casefold() for variant in PROHIBITED_IDENTITY_VARIANTS):
            residual_paths.append(relative)
        try:
            text = (root / relative).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(variant in text.casefold() for variant in PROHIBITED_IDENTITY_VARIANTS):
            residual_text.append(relative)
    return residual_paths, residual_text


class ProductIdentityTests(unittest.TestCase):
    def test_identity_gate_checks_all_tracked_text_extensions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "Gateway.cs"
            source.write_text("class " + "Cortex" + "MemoryBrowser {}", encoding="utf-8")

            residual_paths, residual_text = find_prohibited_identity_residuals(root, ["Gateway.cs"])

        self.assertEqual(residual_paths, [])
        self.assertEqual(residual_text, ["Gateway.cs"])

    def test_identity_gate_preserves_unusual_git_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
            relative = "odd name-ü-with-tab\t-and-newline\n.cs"
            source = root / relative
            source.write_text("class " + "Cortex" + "MemoryBrowser {}", encoding="utf-8")
            subprocess.run(["git", "add", "--", relative], cwd=root, check=True)

            tracked_paths = get_tracked_paths(root)
            residual_paths, residual_text = find_prohibited_identity_residuals(root, tracked_paths)

        self.assertEqual(tracked_paths, [relative])
        self.assertEqual(residual_paths, [])
        self.assertEqual(residual_text, [relative])

    def test_source_tree_uses_cortex_identity_exclusively(self):
        tracked_paths = get_tracked_paths(ROOT)
        residual_paths, residual_text = find_prohibited_identity_residuals(ROOT, tracked_paths)

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
