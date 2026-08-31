from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TOOLS = (
    "msp_capabilities",
    "msp_project",
    "msp_query",
    "msp_apply",
    "msp_schedule",
    "msp_status",
    "msp_analyze",
    "msp_export",
)


class DistributionTests(unittest.TestCase):
    def test_project_config_registers_microsoft_project(self) -> None:
        config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        servers = config["mcpServers"]
        self.assertEqual(set(servers), {"microsoft-project"})
        project = servers["microsoft-project"]
        self.assertEqual(project["args"][-1], "scripts/run-ms-project-mcp.ps1")
        self.assertEqual(project["env"]["MSP_MCP_BACKEND"], "auto")

    def test_package_declares_entry_point_package_and_windows_dependency(self) -> None:
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            config["project"]["scripts"]["microsoft-project-mcp"],
            "ms_project_mcp.server:main",
        )
        self.assertIn("ms_project_mcp*", config["tool"]["setuptools"]["packages"]["find"]["include"])
        self.assertTrue(any("pywin32==312" in item for item in config["project"]["dependencies"]))

    def test_runner_setup_only_path_contains_no_project_activation(self) -> None:
        runner = (ROOT / "scripts" / "run-ms-project-mcp.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$SetupOnly", runner)
        self.assertIn("requirements-ms-project", runner)
        self.assertNotIn("MSProject.Application", runner)
        self.assertNotIn("Dispatch", runner)

        smoke_wrapper = (ROOT / "scripts" / "smoke-ms-project-desktop.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$PreviousPythonPath = $env:PYTHONPATH", smoke_wrapper)
        self.assertIn("$env:PYTHONPATH =", smoke_wrapper)
        self.assertIn("Remove-Item Env:\\PYTHONPATH", smoke_wrapper)
        self.assertIn("$env:PYTHONPATH = $PreviousPythonPath", smoke_wrapper)

    def test_desktop_smoke_refuses_without_consent_before_importing_live_adapter(self) -> None:
        smoke_source = (ROOT / "scripts" / "smoke-ms-project-desktop.py").read_text(encoding="utf-8")
        self.assertNotIn("run_id", smoke_source)
        self.assertIn('idempotency_key=_key("create")', smoke_source)
        self.assertIn('idempotency_key=_key("open")', smoke_source)
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment["MSP_MCP_STATE_DIR"] = temporary
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "smoke-ms-project-desktop.py")],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(completed.returncode, 3, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "NOT_VERIFIED")
        self.assertFalse(payload["activation_attempted"])
        self.assertFalse(payload["fixture_created"])

    def test_stdio_lists_exact_tools_and_capabilities_without_activation(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "verify-ms-project-stdio.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "VERIFIED")
        for probe in payload["probes"]:
            self.assertEqual(tuple(probe["tools"]), EXPECTED_TOOLS)
            self.assertFalse(probe["activation_attempted"])


if __name__ == "__main__":
    unittest.main()
