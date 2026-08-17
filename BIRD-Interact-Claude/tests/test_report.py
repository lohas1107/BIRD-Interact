import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.report import _build_timeline, generate_html


class ClaudeReportTests(unittest.TestCase):
    def test_builds_timeline_from_normalized_events(self):
        result = {
            "agent_events": [
                {"type": "user_message", "message": "Solve this"},
                {"type": "assistant_text", "text": "I will submit the query."},
                {"type": "tool_call", "id": "tool-1", "name": "submit_sql", "args": {"sql": "SELECT 1"}},
                {"type": "tool_response", "id": "tool-1", "name": "submit_sql", "response": "SQL passed."},
                {"type": "final_response", "text": "Done", "final": True},
            ],
        }
        timeline = _build_timeline(result)
        self.assertEqual(
            [item["kind"] for item in timeline],
            ["user_msg", "thinking", "tool_call", "tool_response", "final"],
        )
        self.assertEqual(timeline[3]["response"], "SQL passed.")

    def test_builds_timeline_from_claude_events(self):
        result = {
            "agent_events": [
                {"type": "user_message", "message": "Solve this"},
                {
                    "type": "AssistantMessage",
                    "content": [
                        {"text": "I will submit the query."},
                        {
                            "id": "tool-1",
                            "name": "mcp__bird__submit_sql",
                            "input": {"sql": "SELECT 1"},
                        },
                    ],
                },
                {
                    "type": "UserMessage",
                    "content": [
                        {
                            "tool_use_id": "tool-1",
                            "content": "SQL passed.",
                        }
                    ],
                },
                {"type": "ResultMessage", "result": "Done"},
            ],
            "tool_trajectory": [
                {
                    "type": "tool",
                    "tool": "submit_sql",
                    "cost": 3.0,
                    "budget_after": 1.0,
                }
            ],
        }
        timeline = _build_timeline(result)
        kinds = [item["kind"] for item in timeline]
        self.assertEqual(kinds, ["user_msg", "thinking", "tool_call", "tool_response", "final"])
        self.assertEqual(timeline[2]["name"], "submit_sql")

    def test_legacy_lifecycle_events_are_not_rendered(self):
        result = {
            "agent_events": [
                {"type": "SystemMessage", "subtype": "init", "data": {"cwd": "/secret"}},
                {"type": "RateLimitEvent", "uuid": "secret"},
                {"type": "AssistantMessage", "content": [{"thinking": "", "signature": "secret"}]},
                {"type": "ResultMessage", "result": "Done"},
            ]
        }
        timeline = _build_timeline(result)
        self.assertEqual([item["kind"] for item in timeline], ["final"])
        self.assertNotIn("secret", json.dumps(timeline))

    def test_unknown_event_is_ignored_and_adk_events_are_ignored(self):
        result = {
            "adk_events": [{"type": "adk_event", "content": {}}],
            "agent_events": [{"type": "FutureClaudeEvent", "payload": {"x": 1}}],
        }
        timeline = _build_timeline(result)
        self.assertIsNone(timeline)

    def test_report_falls_back_to_tool_trajectory(self):
        data = {
            "mode": "a-interact",
            "metrics": {
                "total_tasks": 1,
                "average_reward": 0.0,
                "phase1_count": 0,
                "phase2_count": 0,
            },
            "results": [
                {
                    "task_id": "fallback-task",
                    "tool_trajectory": [
                        {
                            "type": "tool",
                            "tool": "submit_sql",
                            "args": {"sql": "SELECT 1"},
                            "result": "failed",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "result.json"
            output_path = Path(directory) / "result.html"
            input_path.write_text(json.dumps(data), encoding="utf-8")
            generate_html(str(input_path), str(output_path))
            html = output_path.read_text(encoding="utf-8")

        self.assertIn("submit_sql", html)
        self.assertIn("fallback-task", html)


if __name__ == "__main__":
    unittest.main()
