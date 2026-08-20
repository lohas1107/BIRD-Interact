import asyncio
import unittest
from unittest.mock import patch

from shared.llm import _payload
from system_agent.openwebui_runtime import OpenWebUIRuntime


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class OpenWebUIRuntimeTests(unittest.TestCase):
    def test_profile_controls_schema_and_reset_lifecycle(self):
        async def scenario():
            runtime = OpenWebUIRuntime()
            first = await runtime.init_session(
                "task",
                "a-interact",
                state={"tool_profile": {"name": "schema", "tools": ["get_schema"]}},
            )
            retained = await runtime.init_session(
                "task",
                "a-interact",
                state={"tool_profile": {"name": "other", "tools": ["submit_sql"]}},
                reset=False,
            )
            self.assertEqual(retained["tool_profile"], first["tool_profile"])

            replaced = await runtime.init_session(
                "task",
                "a-interact",
                state={"tool_profile": {"name": "other", "tools": ["submit_sql"]}},
                reset=True,
            )
            self.assertEqual(replaced["tool_profile"]["tools"], ["submit_sql"])
            cleanup = await runtime.cleanup_session("task", "a-interact")
            self.assertEqual(cleanup["status"], "ok")
            self.assertNotIn(("a-interact", "task"), runtime._sessions)

        asyncio.run(scenario())

    def test_selected_schemas_are_sent_in_profile_order_and_empty_is_empty(self):
        async def scenario():
            runtime = OpenWebUIRuntime()
            await runtime.init_session(
                "ordered",
                "a-interact",
                state={"tool_profile": {"name": "ordered", "tools": ["submit_sql", "get_schema"]}},
            )
            fake = _FakeClient([{"content": "done"}])
            with patch("system_agent.openwebui_runtime.get_client", return_value=fake):
                await runtime.run_turn("ordered", "a-interact", "hello")
            self.assertEqual(
                [item["function"]["name"] for item in fake.calls[0]["tools"]],
                ["submit_sql", "get_schema"],
            )

            await runtime.init_session(
                "empty",
                "a-interact",
                state={"tool_profile": {"name": "empty", "tools": []}},
            )
            empty_fake = _FakeClient([{"content": "done"}])
            with patch("system_agent.openwebui_runtime.get_client", return_value=empty_fake):
                await runtime.run_turn("empty", "a-interact", "hello")
            self.assertEqual(empty_fake.calls[0]["tools"], [])

        asyncio.run(scenario())

    def test_empty_tools_are_omitted_from_openai_payload(self):
        payload = _payload([], "model", 0.0, 10, [])
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    def test_unauthorized_and_unknown_calls_are_audited_without_handler_or_budget(self):
        async def scenario():
            runtime = OpenWebUIRuntime()
            await runtime.init_session(
                "task",
                "a-interact",
                state={
                    "tool_profile": {"name": "schema", "tools": ["get_schema"]},
                    "budget_remaining": 3.0,
                    "initial_budget": 3.0,
                },
            )
            session = runtime._sessions[("a-interact", "task")]
            with patch("system_agent.openwebui_runtime.execute_tool") as dispatch:
                result = runtime._run_tool(session, "execute_sql", {"sql": "SELECT 1"})
                unknown = runtime._run_tool(session, "not_a_tool", {})
            dispatch.assert_not_called()
            self.assertIn("TOOL_NOT_ALLOWED", result)
            self.assertIn("UNKNOWN_TOOL", unknown)
            self.assertEqual(session.state["budget_remaining"], 3.0)
            errors = [item for item in session.state["tool_trajectory"] if item["is_error"]]
            self.assertEqual([item["cost"] for item in errors], [0.0, 0.0])
            self.assertEqual(
                [(item["budget_before"], item["budget_after"]) for item in errors],
                [(3.0, 3.0), (3.0, 3.0)],
            )

        asyncio.run(scenario())

    def test_c_interact_rejects_second_submit_without_dispatch(self):
        async def scenario():
            runtime = OpenWebUIRuntime()
            await runtime.init_session(
                "task",
                "c-interact",
                state={"tool_profile": {"name": "c", "tools": ["ask_user", "submit_sql"]}},
            )
            session = runtime._sessions[("c-interact", "task")]
            with patch("system_agent.openwebui_runtime.execute_tool", return_value="submitted") as dispatch:
                first = runtime._run_tool(session, "submit_sql", {"sql": "SELECT 1"})
                second = runtime._run_tool(session, "submit_sql", {"sql": "SELECT 2"})
            self.assertEqual(first, "submitted")
            self.assertIn("TOOL_CALL_LIMIT", second)
            self.assertEqual(dispatch.call_count, 1)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
