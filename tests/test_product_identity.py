import importlib
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
