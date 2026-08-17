import json
import os
import unittest
from unittest.mock import patch

from claude_agent_sdk import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

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

    def test_sdk_messages_are_projected_without_provider_metadata(self):
        tool_id = "toolu-1"
        tool_names = {}
        assistant = AssistantMessage(
            content=[
                ThinkingBlock("", "signature-that-must-not-be-saved"),
                TextBlock("I will run the query."),
                ToolUseBlock(tool_id, "mcp__bird__submit_sql", {"sql": "SELECT 1"}),
            ],
            model="claude-sonnet",
            usage={"input_tokens": 12, "output_tokens": 4},
            message_id="message-id",
            session_id="session-id",
            uuid="uuid",
        )
        response = UserMessage(
            content=[ToolResultBlock(tool_id, [{"type": "text", "text": "SQL passed."}])],
            tool_use_result={"tool_use_id": tool_id, "content": "duplicate wrapper"},
            uuid="user-uuid",
        )

        projected = ClaudeRuntime._project_sdk_message(assistant, tool_names)
        projected.extend(ClaudeRuntime._project_sdk_message(response, tool_names))
        projected.extend(ClaudeRuntime._project_sdk_message(
            {"type": "SystemMessage", "subtype": "init", "data": {"cwd": "/secret"}},
            tool_names,
        ))
        projected.extend(ClaudeRuntime._project_sdk_message(
            {"type": "RateLimitEvent", "uuid": "rate-limit-id"},
            tool_names,
        ))
        projected.extend(ClaudeRuntime._project_sdk_message(
            {"type": "ResultMessage", "result": "Done", "usage": {"output_tokens": 99}},
            tool_names,
        ))

        self.assertEqual(
            [event["type"] for event in projected],
            ["assistant_text", "tool_call", "tool_response", "final_response"],
        )
        self.assertEqual(projected[1]["name"], "submit_sql")
        self.assertEqual(projected[1]["id"], tool_id)
        self.assertEqual(projected[2]["id"], tool_id)
        self.assertEqual(projected[2]["response"], "SQL passed.")
        serialized = json.dumps(projected)
        for forbidden in ("signature-that-must-not-be-saved", "input_tokens", "message-id", "session-id", "uuid"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
