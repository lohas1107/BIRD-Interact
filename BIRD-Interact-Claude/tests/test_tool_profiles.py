import json
import tempfile
import unittest
from pathlib import Path

from shared.tool_profiles import (
    VALID_TOOLS,
    load_tool_profiles,
    resolve_tool_profile,
)
from system_agent.agent import instruction_for, task_turn_instruction
from system_agent.tools import build_tool_server


class ToolProfileTests(unittest.TestCase):
    def _file(self, text):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink, missing_ok=True)
        return tmp.name

    def test_defaults_match_existing_tool_sets(self):
        self.assertEqual(set(resolve_tool_profile("a-interact").tools), set(VALID_TOOLS))
        self.assertNotIn("search_semantic_context", resolve_tool_profile("a-interact").tools)
        self.assertEqual(resolve_tool_profile("c-interact").tools, ("ask_user", "submit_sql"))

    def test_custom_order_controls_server_allowlist_and_prompt(self):
        profile = {"name": "custom", "tools": ["get_schema", "execute_sql"]}
        state = {"task_id": "t", "tool_profile": profile}
        _, allowed = build_tool_server(state, "c-interact")
        self.assertEqual(allowed, ["mcp__bird__get_schema", "mcp__bird__execute_sql"])
        prompt = instruction_for("c-interact", state)
        turn = task_turn_instruction("c-interact", state, "query")
        self.assertIn("get_schema", turn)
        self.assertIn("execute_sql", prompt)
        self.assertNotIn("ask_user", prompt + turn)
        self.assertNotIn("submit_sql", prompt + turn)

    def test_empty_profile_builds_empty_server(self):
        state = {"task_id": "t", "tool_profile": {"name": "empty", "tools": []}}
        _, allowed = build_tool_server(state, "a-interact")
        self.assertEqual(allowed, [])

    def test_loader_rejects_invalid_inputs(self):
        bad = [
            '[]',
            '{"p": "ask_user"}',
            '{"p": [1]}',
            '{"p": ["unknown"]}',
            '{"p": ["ask_user", "ask_user"]}',
            '{"p": [], "p": []}',
            '{not-json}',
        ]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                load_tool_profiles(self._file(text))

    def test_unknown_profile_and_missing_file_fail(self):
        path = self._file(json.dumps({"known": []}))
        with self.assertRaisesRegex(ValueError, "unknown tool profile"):
            resolve_tool_profile("a-interact", "missing", path)
        with self.assertRaisesRegex(ValueError, "cannot read"):
            load_tool_profiles(path + ".missing")

    def test_incomplete_profile_is_not_rejected(self):
        path = self._file(json.dumps({"schema-only": ["get_schema"]}))
        self.assertEqual(
            resolve_tool_profile("c-interact", "schema-only", path).tools,
            ("get_schema",),
        )


if __name__ == "__main__":
    unittest.main()
