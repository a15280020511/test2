from __future__ import annotations

import json
import unittest
from pathlib import Path


class EphemeralPluginContractTests(unittest.TestCase):
    def test_permanent_requirements_are_dependency_free(self) -> None:
        lines = [
            line.strip()
            for line in Path("requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(lines, [])

    def test_expert_dependencies_live_in_plugin(self) -> None:
        requirements = Path("plugins/expert-team/requirements.txt").read_text(encoding="utf-8")
        self.assertIn("agent-framework-core", requirements)
        self.assertIn("agent-framework-openai", requirements)
        self.assertIn("openrouter", requirements)

    def test_plugin_manifest_is_bounded(self) -> None:
        manifest = json.loads(Path("plugins/expert-team/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "expert-team")
        self.assertEqual(manifest["cleanup"], "always")
        self.assertEqual(
            set(manifest["allowed_operations"]),
            {"model_intelligence", "execute_team", "contract_smoke"},
        )
        self.assertEqual(
            manifest["allowed_modules"],
            ["scripts.action_entrypoint", "scripts.plugin_smoke_entrypoint"],
        )

    def test_runner_has_unconditional_cleanup(self) -> None:
        source = Path("scripts/plugin_runner.py").read_text(encoding="utf-8")
        self.assertIn("finally:", source)
        self.assertIn("shutil.rmtree(temp_root, ignore_errors=True)", source)
        self.assertIn('"cleaned": not temp_root.exists()', source)

    def test_real_smoke_entry_imports_actual_plugin_dependencies(self) -> None:
        source = Path("scripts/plugin_smoke_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn("import agent_framework", source)
        self.assertIn("import openrouter", source)
        self.assertIn("PLUGIN_IMPORT_OK", source)

    def test_deepseek_core_uses_lazy_plugin_imports(self) -> None:
        package_source = Path("expert_team/__init__.py").read_text(encoding="utf-8")
        entry_source = Path("scripts/action_entrypoint.py").read_text(encoding="utf-8")
        self.assertIn("def __getattr__", package_source)
        self.assertNotIn("from .dynamic_team import", package_source)
        self.assertIn("Plugin-only imports", entry_source)


if __name__ == "__main__":
    unittest.main()
