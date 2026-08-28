from __future__ import annotations

import builtins
import sys
import unittest
from unittest.mock import patch

from ms_project_mcp.detection import RegistrySnapshot, probe_desktop_project
from ms_project_mcp.unavailable import UnavailableProjectBackend


class DesktopProjectDetectionTests(unittest.TestCase):
    def test_injected_windows_probe_reports_registration_modules_and_hints(self) -> None:
        module_queries: list[str] = []

        def find_module(name: str):
            module_queries.append(name)
            return object() if name in {"pythoncom", "win32com"} else None

        snapshot = RegistrySnapshot(
            prog_ids=("MSProject.Application", "MSProject.Application.16"),
            clsids=("{PROJECT-CLSID}",),
            executable_paths=("C:\\Program Files\\Microsoft Office\\root\\Office16\\WINPROJ.EXE",),
            version_hints=("16",),
            architecture_hints=("64-bit",),
        )
        detection = probe_desktop_project(
            platform_reader=lambda: "Windows",
            module_finder=find_module,
            registry_reader=lambda: snapshot,
            path_exists=lambda path: path.endswith("WINPROJ.EXE"),
        )
        self.assertTrue(detection.windows)
        self.assertTrue(detection.com_registered)
        self.assertTrue(detection.pywin32_importable)
        self.assertEqual(module_queries, ["pythoncom", "win32com"])
        self.assertEqual(detection.version_hints, ("16",))
        self.assertEqual(detection.architecture_hints, ("64-bit",))
        self.assertEqual(detection.existing_executable_paths, snapshot.executable_paths)
        self.assertFalse(detection.activation_attempted)

    def test_non_windows_probe_skips_registry_and_distinguishes_module_readiness(self) -> None:
        registry_called = False

        def registry_reader():
            nonlocal registry_called
            registry_called = True
            raise AssertionError("registry should not be read")

        detection = probe_desktop_project(
            platform_reader=lambda: "Linux",
            module_finder=lambda name: object() if name == "pythoncom" else None,
            registry_reader=registry_reader,
            path_exists=lambda path: False,
        )
        self.assertFalse(detection.windows)
        self.assertFalse(detection.com_registered)
        self.assertTrue(detection.pythoncom_importable)
        self.assertFalse(detection.win32com_importable)
        self.assertFalse(detection.pywin32_importable)
        self.assertFalse(registry_called)

    def test_probe_captures_registry_reader_failure_without_activation(self) -> None:
        def broken_registry():
            raise PermissionError("registry denied")

        detection = probe_desktop_project(
            platform_reader=lambda: "Windows",
            module_finder=lambda name: None,
            registry_reader=broken_registry,
            path_exists=lambda path: False,
        )
        self.assertEqual(detection.probe_errors, ("registry_probe:PermissionError",))
        self.assertFalse(detection.com_registered)
        self.assertFalse(detection.activation_attempted)

    def test_unavailable_capabilities_include_typed_detection(self) -> None:
        detection = probe_desktop_project(
            platform_reader=lambda: "Windows",
            module_finder=lambda name: None,
            registry_reader=lambda: RegistrySnapshot(clsids=("{CLSID}",)),
            path_exists=lambda path: False,
        )
        backend = UnavailableProjectBackend(detection=detection)
        capabilities = backend.capabilities()
        self.assertIs(capabilities.detection, detection)
        self.assertTrue(capabilities.installed)
        self.assertFalse(capabilities.available)
        self.assertFalse(capabilities.detection.activation_attempted)

    def test_real_local_probe_does_not_import_or_activate_com(self) -> None:
        module_names = ("pythoncom", "win32com", "win32com.client")
        before = {name: name in sys.modules for name in module_names}
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "pythoncom" or name.startswith("win32com"):
                raise AssertionError("Detection attempted to import the COM runtime")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            detection = probe_desktop_project()
        after = {name: name in sys.modules for name in module_names}
        self.assertEqual(after, before)
        self.assertFalse(detection.activation_attempted)
        self.assertIsInstance(detection.com_registered, bool)
        self.assertIsInstance(detection.pywin32_importable, bool)


if __name__ == "__main__":
    unittest.main()
