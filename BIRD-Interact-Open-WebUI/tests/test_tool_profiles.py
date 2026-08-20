import json
import tempfile
import unittest
from pathlib import Path

from shared.tool_profiles import (
    VALID_TOOLS,
    load_tool_profiles,
    resolve_tool_profile,
)


class ToolProfileTests(unittest.TestCase):
    def _file(self, value):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.write(value)
        handle.close()
        path = Path(handle.name)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_default_profiles_and_order(self):
        self.assertEqual(
            resolve_tool_profile("a-interact").tools,
            (
                "ask_user",
                "get_schema",
                "get_all_column_meanings",
                "get_column_meaning",
                "get_all_external_knowledge_names",
                "get_knowledge_definition",
                "get_all_knowledge_definitions",
                "execute_sql",
                "submit_sql",
            ),
        )
        self.assertEqual(resolve_tool_profile("c-interact").tools, ("ask_user", "submit_sql"))
        self.assertEqual(set(resolve_tool_profile("a-interact").tools), set(VALID_TOOLS))

    def test_custom_empty_incomplete_and_cross_mode_profiles(self):
        path = self._file(json.dumps({"custom": ["get_schema", "ask_user"], "empty": [], "one": ["get_schema"]}))
        self.assertEqual(resolve_tool_profile("c-interact", "custom", path).tools, ("get_schema", "ask_user"))
        self.assertEqual(resolve_tool_profile("a-interact", "empty", path).tools, ())
        self.assertEqual(resolve_tool_profile("c-interact", "one", path).tools, ("get_schema",))

    def test_rejects_malformed_duplicate_unknown_graph_and_non_string(self):
        invalid = [
            "[]",
            '{"p": "ask_user"}',
            '{"p": [1]}',
            '{"p": ["search_semantic_context"]}',
            '{"p": ["ask_user", "ask_user"]}',
            '{"p": ["ask_user"], "p": []}',
            '{"not-json}',
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_tool_profiles(self._file(value))

    def test_claude_complete_profile_is_rejected(self):
        path = self._file(
            json.dumps(
                {
                    "kg-v1": [
                        "ask_user",
                        "search_semantic_context",
                        "get_knowledge",
                        "get_table_schema",
                        "execute_sql",
                        "submit_sql",
                    ]
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "search_semantic_context"):
            load_tool_profiles(path)


if __name__ == "__main__":
    unittest.main()
