import os
import unittest
from unittest.mock import patch

from system_agent.agent import instruction_for
from system_agent.claude_runtime import ClaudeRuntime
from system_agent.tools import build_tool_server


class ClaudeRuntimeTests(unittest.TestCase):
    def test_a_interact_registers_only_bird_tools(self):
        state = {"task_id": "test", "budget_remaining": 10.0, "initial_budget": 10.0}
        _, names = build_tool_server(state, "a-interact")
        self.assertEqual(len(names), 9)
        self.assertTrue(all(name.startswith("mcp__bird__") for name in names))

    def test_c_interact_registers_only_ask_and_submit(self):
        state = {"task_id": "test", "max_turn": 3}
        _, names = build_tool_server(state, "c-interact")
        self.assertEqual(names, ["mcp__bird__ask_user", "mcp__bird__submit_sql"])

    def test_c_prompt_interpolates_task_state(self):
        prompt = instruction_for("c-interact", {
            "db_name": "db1", "db_schema": "CREATE TABLE t(x int)",
            "external_kg": "knowledge", "max_turn": 4,
        })
        self.assertIn("db1", prompt)
        self.assertIn("CREATE TABLE", prompt)
        self.assertIn("at most 4", prompt)

    def test_api_key_is_rejected(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "not-a-real-key"}):
            with self.assertRaisesRegex(RuntimeError, "pay-as-you-go"):
                ClaudeRuntime._auth_guard()


if __name__ == "__main__":
    unittest.main()
