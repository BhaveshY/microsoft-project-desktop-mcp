from __future__ import annotations

import json
import unittest

from ms_project_mcp.compat import StrEnum
from ms_project_mcp.errors import ErrorCode
from ms_project_mcp.models import Ownership, ProjectRef


class PythonCompatibilityTests(unittest.TestCase):
    def test_string_enums_serialize_without_python_version_specific_hooks(self) -> None:
        self.assertTrue(issubclass(StrEnum, str))
        encoded = json.dumps(
            {
                "ownership": Ownership.SERVER_OWNED,
                "error": ErrorCode.STALE_STATE,
                "project": ProjectRef(session_id="session", project_key="project").model_dump(mode="json"),
            },
            sort_keys=True,
        )
        self.assertEqual(
            json.loads(encoded),
            {
                "error": "stale_state",
                "ownership": "server_owned",
                "project": {"project_key": "project", "session_id": "session"},
            },
        )


if __name__ == "__main__":
    unittest.main()
