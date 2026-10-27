"""All Excel entry points use the offline writer when Node cannot load its package."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sop_backup
import sop_export
import sop_workbook
from sop_runtime import ArtifactDependencyError


class ExcelDispatchTest(unittest.TestCase):
    WRITERS = (
        (sop_workbook, sop_workbook.WorkbookSync),
        (sop_export, sop_export.PlanExport),
        (sop_backup, sop_backup.SnapshotBackup),
    )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.payload = {"output_path": str(self.root / "result.xlsx")}

    def writer(self, cls):
        instance = cls.__new__(cls)
        instance.root = self.root
        if cls is sop_export.PlanExport:
            instance.timeout = 5
        return instance

    def test_missing_node_package_falls_back_for_all_excel_outputs(self):
        for module, cls in self.WRITERS:
            with self.subTest(writer=cls.__name__):
                instance = self.writer(cls)
                with patch.object(module, "python_excel_requested", return_value=False), \
                     patch.object(module, "node_executable", return_value="node"), \
                     patch.object(module.Path, "is_file", return_value=True), \
                     patch.object(module, "ensure_artifact_tool", side_effect=ArtifactDependencyError("missing")), \
                     patch.object(instance, "_python_writer") as offline:
                    instance._node_writer(self.payload)
                    offline.assert_called_once_with(self.payload)

    def test_no_node_executable_falls_back_for_all_excel_outputs(self):
        for module, cls in self.WRITERS:
            with self.subTest(writer=cls.__name__):
                instance = self.writer(cls)
                with patch.object(module, "python_excel_requested", return_value=False), \
                     patch.object(module, "node_executable", return_value=None), \
                     patch.object(instance, "_python_writer") as offline:
                    instance._node_writer(self.payload)
                    offline.assert_called_once_with(self.payload)

    def test_node_import_error_falls_back_for_all_excel_outputs(self):
        failure = subprocess.CompletedProcess([], 1, "", "Error [ERR_MODULE_NOT_FOUND]: Cannot find package '@oai/artifact-tool'")
        for module, cls in self.WRITERS:
            with self.subTest(writer=cls.__name__):
                instance = self.writer(cls)
                with patch.object(module, "python_excel_requested", return_value=False), \
                     patch.object(module, "node_executable", return_value="node"), \
                     patch.object(module.Path, "is_file", return_value=True), \
                     patch.object(module, "ensure_artifact_tool"), \
                     patch.object(module.subprocess, "run", return_value=failure), \
                     patch.object(instance, "_python_writer") as offline:
                    instance._node_writer(self.payload)
                    offline.assert_called_once_with(self.payload)

    def test_force_python_does_not_start_node(self):
        for module, cls in self.WRITERS:
            with self.subTest(writer=cls.__name__):
                instance = self.writer(cls)
                with patch.object(module, "python_excel_requested", return_value=True), \
                     patch.object(module, "node_executable", side_effect=AssertionError("Node must not start")), \
                     patch.object(instance, "_python_writer") as offline:
                    instance._node_writer(self.payload)
                    offline.assert_called_once_with(self.payload)


if __name__ == "__main__":
    unittest.main()
