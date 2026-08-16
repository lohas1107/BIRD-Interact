import asyncio
import inspect
import unittest
from unittest.mock import patch

from mcp.types import CallToolRequest, CallToolRequestParams

from shared import llm
from system_agent.agent import instruction_for
from system_agent.tools import build_tool_server


class _Response:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeAsyncClient:
    calls = []
    responses = {}
    failures = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, json):
        self.calls.append((url, json))
        for suffix, error in self.failures.items():
            if url.endswith(suffix):
                raise error
        for suffix, response in self.responses.items():
            if url.endswith(suffix):
                return _Response(response)
        return _Response({})


def _call_handler(server, name, arguments):
    request_type = next(
        key for key in server["instance"].request_handlers
        if key.__name__ == "CallToolRequest"
    )
    handler = server["instance"].request_handlers[request_type]
    request = CallToolRequest(
        params=CallToolRequestParams(name=name, arguments=arguments)
    )
    return asyncio.run(handler(request))


class EvaluationControlTests(unittest.TestCase):
    def setUp(self):
        _FakeAsyncClient.calls = []
        _FakeAsyncClient.responses = {
            "/submit": {
                "passed": False,
                "message": "Your SQL is not correct.",
                "reward": 0.0,
                "phase_completed": None,
                "has_follow_up": False,
            },
            "/phase_transition": {"status": "ok", "phase": 2},
            "/ask": {"answer": "The user answer."},
        }
        _FakeAsyncClient.failures = {}

    def test_budget_exactly_three_allows_one_submit_then_blocks(self):
        state = {
            "task_id": "budget-task",
            "budget_remaining": 3.0,
            "initial_budget": 3.0,
        }
        with patch("system_agent.tools.httpx.AsyncClient", _FakeAsyncClient):
            server, _ = build_tool_server(state, "a-interact")
            first = _call_handler(server, "submit_sql", {"sql": "SELECT 1"})
            second = _call_handler(server, "submit_sql", {"sql": "SELECT 2"})

        submit_calls = [url for url, _ in _FakeAsyncClient.calls if url.endswith("/submit")]
        self.assertEqual(len(submit_calls), 1)
        self.assertEqual(state["budget_remaining"], -1.0)
        self.assertTrue(state["task_done"])
        self.assertTrue(state["budget_exhausted"])
        self.assertEqual(state["terminal_reason"], "budget_exhausted_after_submit")
        self.assertFalse(state["retryable"])
        self.assertIn("Your SQL is not correct", first.root.content[0].text)
        self.assertIn("already complete", second.root.content[0].text)

    def test_budget_overdrawn_submit_is_allowed_once_but_terminal(self):
        state = {
            "task_id": "budget-overdrawn-task",
            "budget_remaining": 2.0,
            "initial_budget": 2.0,
        }
        with patch("system_agent.tools.httpx.AsyncClient", _FakeAsyncClient):
            server, _ = build_tool_server(state, "a-interact")
            first = _call_handler(server, "submit_sql", {"sql": "SELECT 1"})
            second = _call_handler(server, "submit_sql", {"sql": "SELECT 2"})

        submit_calls = [url for url, _ in _FakeAsyncClient.calls if url.endswith("/submit")]
        self.assertEqual(len(submit_calls), 1)
        self.assertEqual(state["budget_remaining"], -1.0)
        self.assertTrue(state["budget_overdrawn"])
        self.assertTrue(state["task_done"])
        self.assertEqual(state["terminal_reason"], "budget_exhausted_after_submit")
        self.assertFalse(state["retryable"])
        self.assertIn("one allowed final submit", first.root.content[0].text)
        self.assertIn("already complete", second.root.content[0].text)

    def test_overdrawn_submit_service_error_is_terminal_without_retry(self):
        _FakeAsyncClient.failures["/submit"] = RuntimeError("submit unavailable")
        state = {
            "task_id": "budget-overdrawn-error",
            "budget_remaining": 2.0,
            "initial_budget": 2.0,
        }
        with patch("system_agent.tools.httpx.AsyncClient", _FakeAsyncClient):
            server, _ = build_tool_server(state, "a-interact")
            response = _call_handler(server, "submit_sql", {"sql": "SELECT 1"})

        submit_calls = [url for url, _ in _FakeAsyncClient.calls if url.endswith("/submit")]
        self.assertEqual(len(submit_calls), 1)
        self.assertTrue(state["task_done"])
        self.assertEqual(state["last_submit_status"], "error")
        self.assertFalse(state["retryable"])
        self.assertIn("Tool error", response.root.content[0].text)

    def test_budget_exhaustion_skips_follow_up_transition(self):
        _FakeAsyncClient.responses["/submit"] = {
            "passed": True,
            "message": "Phase 1 correct.",
            "reward": 0.7,
            "phase_completed": 1,
            "has_follow_up": True,
            "follow_up_query": "Follow up",
        }
        state = {
            "task_id": "budget-follow-up",
            "budget_remaining": 3.0,
            "initial_budget": 3.0,
        }
        with patch("system_agent.tools.httpx.AsyncClient", _FakeAsyncClient):
            server, _ = build_tool_server(state, "a-interact")
            response = _call_handler(server, "submit_sql", {"sql": "SELECT 1"})

        transition_calls = [
            url for url, _ in _FakeAsyncClient.calls
            if url.endswith("/phase_transition")
        ]
        self.assertEqual(transition_calls, [])
        self.assertTrue(state["task_done"])
        self.assertTrue(state["budget_exhausted"])
        self.assertFalse(state["budget_overdrawn"])
        self.assertTrue(state["phase2_skipped_due_budget"])
        self.assertFalse(state.get("phase_transition_done", False))
        self.assertNotIn("Follow-up question:", response.root.content[0].text)
        self.assertIn("Phase 2 skipped", response.root.content[0].text)

    def test_failed_submit_with_budget_is_retryable(self):
        state = {
            "task_id": "retryable-submit",
            "budget_remaining": 10.0,
            "initial_budget": 10.0,
        }
        with patch("system_agent.tools.httpx.AsyncClient", _FakeAsyncClient):
            server, _ = build_tool_server(state, "a-interact")
            first = _call_handler(server, "submit_sql", {"sql": "SELECT 1"})
            second = _call_handler(server, "submit_sql", {"sql": "SELECT 2"})

        submit_calls = [url for url, _ in _FakeAsyncClient.calls if url.endswith("/submit")]
        self.assertEqual(len(submit_calls), 2)
        self.assertFalse(state.get("task_done", False))
        self.assertEqual(state["next_action"], "retry_submit")
        self.assertTrue(state["retryable"])
        self.assertEqual(state["last_submit_status"], "failed")
        self.assertIn("retryable", first.root.content[0].text)
        self.assertIn("retryable", second.root.content[0].text)

    def test_successful_phase_one_transitions_once(self):
        _FakeAsyncClient.responses["/submit"] = {
            "passed": True,
            "message": "Phase 1 correct.",
            "reward": 0.7,
            "phase_completed": 1,
            "has_follow_up": True,
            "follow_up_query": "Follow up",
        }
        state = {
            "task_id": "transition-task",
            "budget_remaining": 10.0,
            "initial_budget": 10.0,
        }
        with patch("system_agent.tools.httpx.AsyncClient", _FakeAsyncClient):
            server, _ = build_tool_server(state, "c-interact")
            _call_handler(server, "submit_sql", {"sql": "SELECT 1"})

        transition_calls = [
            url for url, _ in _FakeAsyncClient.calls
            if url.endswith("/phase_transition")
        ]
        self.assertEqual(len(transition_calls), 1)
        self.assertTrue(state["phase_transition_done"])
        self.assertEqual(state["next_action"], "phase2")
        self.assertFalse(state.get("task_done", False))
        self.assertFalse(state.get("phase_transition_failed", False))

    def test_phase_transition_failure_terminates_follow_up(self):
        _FakeAsyncClient.responses["/submit"] = {
            "passed": True,
            "message": "Phase 1 correct.",
            "reward": 0.7,
            "phase_completed": 1,
            "has_follow_up": True,
            "follow_up_query": "Follow up",
        }
        _FakeAsyncClient.failures["/phase_transition"] = RuntimeError("transition unavailable")
        state = {
            "task_id": "transition-failure-task",
            "budget_remaining": 10.0,
            "initial_budget": 10.0,
        }
        with patch("system_agent.tools.httpx.AsyncClient", _FakeAsyncClient):
            server, _ = build_tool_server(state, "c-interact")
            response = _call_handler(server, "submit_sql", {"sql": "SELECT 1"})

        transition_calls = [
            url for url, _ in _FakeAsyncClient.calls
            if url.endswith("/phase_transition")
        ]
        self.assertEqual(len(transition_calls), 1)
        self.assertTrue(state["phase_transition_failed"])
        self.assertTrue(state["task_done"])
        self.assertIn("task terminated", response.root.content[0].text)

    def test_c_interact_clarification_limit_does_not_call_user_twice(self):
        state = {"task_id": "clarification-task", "max_turn": 1}
        with patch("system_agent.tools.httpx.AsyncClient", _FakeAsyncClient):
            server, _ = build_tool_server(state, "c-interact")
            _call_handler(server, "ask_user", {"question": "Question 1"})
            second = _call_handler(server, "ask_user", {"question": "Question 2"})

        ask_calls = [url for url, _ in _FakeAsyncClient.calls if url.endswith("/ask")]
        self.assertEqual(len(ask_calls), 1)
        self.assertIn("0/1", second.root.content[0].text)

    def test_a_interact_ask_user_returns_synchronous_answer(self):
        state = {
            "task_id": "sync-clarification",
            "budget_remaining": 5.0,
            "initial_budget": 5.0,
        }
        with patch("system_agent.tools.httpx.AsyncClient", _FakeAsyncClient):
            server, _ = build_tool_server(state, "a-interact")
            response = _call_handler(server, "ask_user", {"question": "Question"})

        ask_calls = [url for url, _ in _FakeAsyncClient.calls if url.endswith("/ask")]
        self.assertEqual(len(ask_calls), 1)
        self.assertIn("The user answer.", response.root.content[0].text)
        self.assertIn("returned synchronously", response.root.content[0].text)
        self.assertEqual(state["dialogue_history"][-1]["content"], "The user answer.")

    def test_claude_llm_wrapper_has_no_fake_max_tokens_argument(self):
        self.assertNotIn("max_tokens", inspect.signature(llm._call).parameters)
        self.assertNotIn("max_tokens", inspect.signature(llm.call_llm).parameters)

    def test_prompts_describe_control_contract(self):
        a_prompt = instruction_for("a-interact", {})
        c_prompt = instruction_for("c-interact", {"max_turn": 4})
        self.assertIn("submit_sql: 3 bird-coins", a_prompt)
        self.assertIn("reserve the cost", a_prompt)
        self.assertIn("every natural-language metric", a_prompt)
        self.assertIn("explicit Knowledge ID", a_prompt)
        self.assertIn("successful execution is not semantic validation", a_prompt)
        self.assertIn("An expiring budget is never permission to guess", a_prompt)
        self.assertIn("one final overdraw", a_prompt)
        self.assertIn("output columns and aliases", a_prompt)
        self.assertIn("answer is synchronous", a_prompt)
        self.assertIn("next_action=phase2", a_prompt)
        self.assertNotIn("submit your best SQL", a_prompt)
        self.assertIn("at most 4 clarification turns", c_prompt)
        self.assertIn("synchronously", c_prompt)
        self.assertIn("at most once", c_prompt)


if __name__ == "__main__":
    unittest.main()
