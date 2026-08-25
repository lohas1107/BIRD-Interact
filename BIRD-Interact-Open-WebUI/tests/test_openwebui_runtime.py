import asyncio
import unittest
from unittest.mock import patch

from shared.llm import _payload
from system_agent.openwebui_runtime import OpenWebUIRuntime


def _profile(name, tools, prompt=""):
    return {"name": name, "tools": tools, "prompt_template": prompt}


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.responses.pop(0)


class OpenWebUIRuntimeTests(unittest.TestCase):
    def test_profile_controls_schema_and_reset_lifecycle(self):
        async def scenario():
            runtime = OpenWebUIRuntime()
            first_profile = _profile("schema", ["get_schema"], "first")
            other_profile = _profile("other", ["submit_sql"], "second")
            first = await runtime.init_session(
                "task",
                "a-interact",
                state={"db_name": "db"},
                agent_profile=first_profile,
            )
            self.assertEqual(first["agent_profile"], {"name": "schema", "tools": ["get_schema"]})
            session = runtime._sessions[("a-interact", "task")]
            self.assertEqual(session.messages[0]["content"], "first")
            self.assertEqual(session.state["agent_profile"], first["agent_profile"])
            self.assertNotIn("prompt_template", session.state["agent_profile"])

            retained = await runtime.init_session(
                "task",
                "a-interact",
                agent_profile=other_profile,
                reset=False,
            )
            self.assertEqual(retained["agent_profile"], first["agent_profile"])

            replaced = await runtime.init_session(
                "task",
                "a-interact",
                agent_profile=other_profile,
                reset=True,
            )
            self.assertEqual(replaced["agent_profile"]["tools"], ["submit_sql"])
            self.assertEqual(runtime._sessions[("a-interact", "task")].messages[0]["content"], "second")
            cleanup = await runtime.cleanup_session("task", "a-interact")
            self.assertEqual(cleanup["status"], "ok")
            self.assertNotIn(("a-interact", "task"), runtime._sessions)

        asyncio.run(scenario())

    def test_system_prompt_renders_state_and_tool_manifest_once(self):
        async def scenario():
            runtime = OpenWebUIRuntime()
            profile = _profile(
                "ordered",
                ["submit_sql", "get_schema", "ask_user", "search_metadata_5_2"],
                "{{db_name}}|{{db_schema}}|{{external_kg}}|{{max_turn}}|{{available_tools}}",
            )
            await runtime.init_session(
                "ordered",
                "a-interact",
                state={"db_name": "db", "db_schema": "schema", "external_kg": "kg", "max_turn": 4},
                agent_profile=profile,
            )
            system_message = runtime._sessions[("a-interact", "ordered")].messages[0]["content"]
            self.assertIn("db|schema|kg|4|", system_message)
            self.assertIn("- submit_sql: submit the SQL for evaluation. Cost: 3", system_message)
            self.assertIn("- get_schema: get the database schema. Cost: 1", system_message)
            self.assertIn("- ask_user: ask the user a clarification question. Cost: 2", system_message)
            self.assertIn(
                "- search_metadata_5_2: search fixed metadata candidates only; do not infer semantic definitions from metadata. Cost: 2",
                system_message,
            )

            runtime._sessions[("a-interact", "ordered")].state["db_name"] = "changed"
            self.assertEqual(
                runtime._sessions[("a-interact", "ordered")].messages[0]["content"],
                system_message,
            )

        asyncio.run(scenario())

    def test_missing_context_is_empty_and_empty_tools_manifest_is_empty(self):
        async def scenario():
            runtime = OpenWebUIRuntime()
            await runtime.init_session(
                "empty",
                "a-interact",
                agent_profile=_profile("empty", [], "A={{db_name}} B={{db_schema}} C={{external_kg}} D={{max_turn}} E[{{available_tools}}]"),
            )
            content = runtime._sessions[("a-interact", "empty")].messages[0]["content"]
            self.assertEqual(content, "A= B= C= D= E[]")
            fake = _FakeClient([{"content": "done"}])
            with patch("system_agent.openwebui_runtime.get_client", return_value=fake):
                await runtime.run_turn("empty", "a-interact", "hello")
            self.assertEqual(fake.calls[0]["tools"], [])

        asyncio.run(scenario())

    def test_selected_schemas_are_sent_in_profile_order(self):
        async def scenario():
            runtime = OpenWebUIRuntime()
            await runtime.init_session(
                "ordered",
                "a-interact",
                agent_profile=_profile("ordered", ["submit_sql", "get_schema"], ""),
            )
            fake = _FakeClient([{"content": "done"}])
            with patch("system_agent.openwebui_runtime.get_client", return_value=fake):
                await runtime.run_turn("ordered", "a-interact", "hello")
            self.assertEqual(
                [item["function"]["name"] for item in fake.calls[0]["tools"]],
                ["submit_sql", "get_schema"],
            )

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
                state={"budget_remaining": 3.0, "initial_budget": 3.0},
                agent_profile=_profile("schema", ["get_schema"], ""),
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
                agent_profile=_profile("c", ["ask_user", "submit_sql"], ""),
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
