from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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


def _tool_payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and "result" in structured:
        value = structured["result"]
        if isinstance(value, dict):
            return value
    for block in getattr(result, "content", ()):
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
    raise RuntimeError("msp_capabilities returned no JSON object")


async def _probe(mode: str, state_dir: Path) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["MSP_MCP_BACKEND"] = mode
    environment["MSP_MCP_STATE_DIR"] = str(state_dir)
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(repo_root) if not existing else os.pathsep.join((str(repo_root), existing))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ms_project_mcp.server"],
        env=environment,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = tuple(tool.name for tool in listed.tools)
            if names != EXPECTED_TOOLS:
                raise RuntimeError(f"unexpected tool list: {names!r}")
            response = await session.call_tool("msp_capabilities", {})
            payload = _tool_payload(response)
            if payload.get("ok") is False:
                raise RuntimeError(f"capabilities failed: {payload!r}")
            # MCP SDK versions differ in whether a single top-level `result`
            # envelope is retained in structuredContent.
            capabilities = payload.get("result", payload)
            if capabilities.get("activates_desktop") is not False:
                raise RuntimeError("capability probe must report activates_desktop=false")
            if mode == "mock" and capabilities.get("backend") != "mock":
                raise RuntimeError("explicit mock mode did not select the mock backend")
            detection = capabilities.get("detection")
            if mode == "auto" and detection and detection.get("activation_attempted") is not False:
                raise RuntimeError("auto detection attempted COM activation")
            return {
                "mode": mode,
                "tools": list(names),
                "backend": capabilities.get("backend"),
                "available": capabilities.get("available"),
                "activation_attempted": (detection or {}).get("activation_attempted", False),
            }


async def _main() -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="ms-project-mcp-stdio-") as temporary:
        root = Path(temporary)
        return [
            await _probe("mock", root / "mock"),
            await _probe("auto", root / "auto"),
        ]


if __name__ == "__main__":
    results = asyncio.run(_main())
    print(json.dumps({"status": "VERIFIED", "probes": results}, sort_keys=True))
