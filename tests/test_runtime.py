"""Local Excel dependencies are restored without touching business inputs."""

import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from sop_runtime import ArtifactDependencyError, ensure_artifact_tool, node_executable


class ArtifactRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sop-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "计划 & Board %keep%"
        (self.root / "scripts").mkdir(parents=True)
        self.link = self.root / "scripts" / "node_modules"
        self.addCleanup(self._remove_link)

    def _remove_link(self):
        if os.path.lexists(self.link):
            if self.link.is_symlink():
                self.link.unlink()
            elif os.name == "nt" and hasattr(os.path, "isjunction") and os.path.isjunction(self.link):
                os.rmdir(self.link)

    def _fake_modules(self, name):
        source = Path(self.temporary.name) / name
        package = source / "@oai" / "artifact-tool" / "package.json"
        package.parent.mkdir(parents=True)
        package.write_text('{"name":"@oai/artifact-tool"}', encoding="utf-8")
        return source

    def test_missing_and_stale_link_are_repaired_without_deleting_target(self):
        old_source = self._fake_modules("old")
        with patch.dict(os.environ, {"SOP_NODE_MODULES_PATH": str(old_source)}):
            ensure_artifact_tool(self.root, "node")
        self.assertTrue((self.link / "@oai/artifact-tool/package.json").is_file())
        (old_source / "@oai/artifact-tool/package.json").unlink()

        new_source = self._fake_modules("new")
        with patch.dict(os.environ, {"SOP_NODE_MODULES_PATH": str(new_source)}):
            ensure_artifact_tool(self.root, "node")
        self.assertEqual((self.link / "@oai/artifact-tool/package.json").read_text(encoding="utf-8"),
                         '{"name":"@oai/artifact-tool"}')
        self.assertTrue(old_source.is_dir())

    def test_existing_real_directory_is_never_removed(self):
        self.link.mkdir()
        marker = self.link / "existing.txt"
        marker.write_text("keep", encoding="utf-8")
        source = self._fake_modules("source")
        with patch.dict(os.environ, {"SOP_NODE_MODULES_PATH": str(source)}):
            with self.assertRaises(ArtifactDependencyError):
                ensure_artifact_tool(self.root, "node")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_concurrent_first_use_creates_one_valid_link(self):
        source = self._fake_modules("source")
        with patch.dict(os.environ, {"SOP_NODE_MODULES_PATH": str(source)}):
            with ThreadPoolExecutor(max_workers=6) as executor:
                list(executor.map(lambda _: ensure_artifact_tool(self.root, "node"), range(12)))
        self.assertTrue((self.link / "@oai/artifact-tool/package.json").is_file())

    def test_real_node_resolves_artifact_tool_after_bootstrap(self):
        node = node_executable()
        bundle = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"
        if not node or not (bundle / "@oai/artifact-tool/package.json").is_file():
            self.skipTest("Bundled spreadsheet runtime unavailable")
        with patch.dict(os.environ, {"SOP_NODE_MODULES_PATH": str(bundle)}):
            ensure_artifact_tool(self.root, node)
        probe = self.root / "scripts" / "probe.mjs"
        probe.write_text("import { Workbook } from '@oai/artifact-tool'; console.log(typeof Workbook.create);",
                         encoding="utf-8")
        completed = subprocess.run([node, str(probe)], capture_output=True, text=True,
                                   timeout=30, cwd=self.root)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "function")


if __name__ == "__main__":
    unittest.main()
