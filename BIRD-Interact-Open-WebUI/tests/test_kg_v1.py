import asyncio
import json
import os
import unittest
from unittest.mock import patch

from db_environment.knowledge_graph import (
    GraphSchemaError,
    KnowledgeGraphError,
    Neo4jKnowledgeRepository,
    Neo4jSchemaRepository,
    SemanticSearchRepository,
)
from embedding.server import Embedder
from shared.agent_profiles import resolve_agent_profile
from system_agent import tools
from system_agent.openwebui_runtime import OpenWebUIRuntime


KG_TOOLS = [
    "ask_user",
    "search_semantic_context",
    "get_knowledge",
    "get_table_schema",
    "execute_sql",
    "submit_sql",
]


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _Client:
    calls = []
    response = _Response(payload={"knowledge": [], "table_schema": []})

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def post(self, url, json):
        self.calls.append((url, json))
        return self.response


class KgV1ProfileTests(unittest.TestCase):
    def test_profile_resolves_for_both_modes_without_changing_defaults(self):
        a_profile = resolve_agent_profile("a-interact", "kg-v1")
        c_profile = resolve_agent_profile("c-interact", "kg-v1")
        self.assertEqual(a_profile.tools, tuple(KG_TOOLS))
        self.assertEqual(c_profile.tools, tuple(KG_TOOLS))
        self.assertEqual(resolve_agent_profile("a-interact").name, "a-interact-default")
        self.assertEqual(resolve_agent_profile("c-interact").name, "c-interact-default")
        self.assertNotIn("get_schema", a_profile.tools)
        self.assertNotIn("get_knowledge_definition", a_profile.tools)
        self.assertNotIn("{{unknown}}", a_profile.prompt_template)

    def test_schema_cost_and_nested_contract(self):
        self.assertEqual([tools.tool_cost(name) for name in KG_TOOLS], [2.0, 2.0, 0.5, 0.5, 1.0, 3.0])
        self.assertEqual(
            tools.TOOL_SCHEMAS["search_semantic_context"]["function"]["parameters"]["required"],
            ["queries", "top_k", "resource_types"],
        )
        expand = tools.TOOL_SCHEMAS["get_knowledge"]["function"]["parameters"]["properties"]["expand"]
        self.assertEqual(expand["required"], ["depth", "nodes"])
        self.assertFalse(expand["additionalProperties"])
        self.assertEqual(
            tools.TOOL_SCHEMAS["get_table_schema"]["function"]["parameters"]["required"],
            ["database_name", "from_table"],
        )


class KgV1ToolTests(unittest.TestCase):
    def setUp(self):
        _Client.calls = []
        _Client.response = _Response(payload={"knowledge": [], "table_schema": []})

    def test_task_id_is_server_injected_and_graph_result_is_json_text(self):
        state = {"agent_profile": {"name": "kg-v1", "tools": ["search_semantic_context"]}}
        args = {
            "task_id": "model-controlled-task",
            "queries": ["signal quality"],
            "top_k": 5,
            "resource_types": ["knowledge", "table_schema"],
        }
        with patch("system_agent.tools.httpx.Client", _Client):
            result = tools.execute_tool("search_semantic_context", args, "server-task", state)
        self.assertEqual(json.loads(result), {"knowledge": [], "table_schema": []})
        self.assertEqual(_Client.calls[0][1]["task_id"], "server-task")
        self.assertNotEqual(_Client.calls[0][1]["task_id"], args["task_id"])

    def test_http_graph_error_is_an_error_and_preserves_code(self):
        _Client.response = _Response(
            status_code=404,
            payload={"detail": {"code": "KNOWLEDGE_NOT_FOUND", "message": "hidden"}},
        )
        state = {"agent_profile": {"name": "kg-v1", "tools": ["get_knowledge"]}}
        with patch("system_agent.tools.httpx.Client", _Client):
            result = tools.execute_tool("get_knowledge", {"knowledge_id": "alien:10"}, "task", state)
        self.assertEqual(result, "KNOWLEDGE_NOT_FOUND: hidden")
        self.assertTrue(state["_last_tool_dispatch_error"])

    def test_invalid_nested_arguments_fail_closed_before_http(self):
        state = {"agent_profile": {"name": "kg-v1", "tools": ["get_knowledge"]}}
        with patch("system_agent.tools.httpx.Client", side_effect=AssertionError("network call")):
            result = tools.execute_tool(
                "get_knowledge",
                {"knowledge_id": "alien:10", "expand": {"depth": 1}},
                "task",
                state,
            )
        self.assertTrue(result.startswith("INVALID_REQUEST:"))
        self.assertTrue(state["_last_tool_dispatch_error"])


class _Result(list):
    def single(self):
        return self[0] if self else None


class _KnowledgeTx:
    nodes = {
        "alien:10": {"id": "alien:10", "name": "root", "type": "domain"},
        "alien:4": {"id": "alien:4", "name": "four", "type": "calculation"},
        "alien:1": {"id": "alien:1", "name": "one", "type": "calculation"},
        "alien:0": {"id": "alien:0", "name": "zero", "type": "calculation"},
    }
    edges = {"alien:10": [(0, "alien:4"), (1, "alien:1")], "alien:4": [(0, "alien:0")]}

    def run(self, query, **params):
        if "WHERE k.id = $root_id" in query:
            node = self.nodes.get(params["root_id"])
            return _Result([{"node": node}]) if node and node["id"] not in params["hidden_ids"] else _Result()
        if "UNWIND range" in query:
            rows = []
            for parent_id in params["parent_ids"]:
                for position, child_id in self.edges.get(parent_id, []):
                    if parent_id not in params["hidden_ids"] and child_id not in params["hidden_ids"]:
                        rows.append({
                            "parent_id": parent_id,
                            "position": position,
                            "node": self.nodes[child_id],
                        })
            return _Result(rows)
        if "UNWIND $knowledge_ids" in query:
            return _Result()
        raise AssertionError(f"unexpected query: {query}")


class KgV1GraphBackendTests(unittest.TestCase):
    def test_database_scope_and_stable_request_errors(self):
        with self.assertRaises(KnowledgeGraphError) as context:
            Neo4jKnowledgeRepository().get_knowledge(
                {"knowledge_id": "other:10"},
                hidden_ids=[],
                task_database_name="alien",
            )
        self.assertEqual(context.exception.code, "KNOWLEDGE_NOT_FOUND")
        with self.assertRaises(GraphSchemaError) as context:
            Neo4jSchemaRepository._normalize({
                "database_name": "alien",
                "from_table": "signals",
                "to_table": None,
                "include": None,
                "hops": 0,
                "paths": 5,
            })
        self.assertEqual(context.exception.code, "INVALID_REQUEST")

    def test_bfs_expansion_masks_hidden_dependency_and_edges(self):
        response = Neo4jKnowledgeRepository._read_knowledge(_KnowledgeTx(), {
            "database_name": "alien",
            "root_id": "alien:10",
            "include": [],
            "expand": {"depth": 2, "max_nodes": 20},
            "hidden_ids": ["alien:4"],
        })
        self.assertEqual([node["knowledge_id"] for node in response["nodes"]], ["alien:10", "alien:1"])
        self.assertEqual(response["edges"][0]["to_knowledge_id"], "alien:1")
        self.assertFalse(any(node["knowledge_id"] == "alien:4" for node in response["nodes"]))

    def test_semantic_search_scores_are_merged_and_bounded(self):
        self.assertEqual(
            SemanticSearchRepository._normalize_fulltext_scores({"a": 2.0, "b": 4.0}),
            {"a": 0.0, "b": 1.0},
        )
        self.assertEqual(
            SemanticSearchRepository._fulltext_query("SNR < 0.5; noise-floor"),
            "SNR 0 5 noise floor",
        )


class KgV1RuntimeTests(unittest.TestCase):
    def test_openai_loop_sends_six_tools_and_roundtrips_role_tool(self):
        class FakeClient:
            def __init__(self):
                self.calls = []
                self.responses = [
                    {
                        "content": "",
                        "tool_calls": [{
                            "id": "call-1",
                            "function": {
                                "name": "get_knowledge",
                                "arguments": json.dumps({"knowledge_id": "alien:10"}),
                            },
                        }],
                    },
                    {"content": "done"},
                ]

            async def chat(self, messages, **kwargs):
                self.calls.append({"messages": messages, **kwargs})
                return self.responses.pop(0)

        async def scenario():
            runtime = OpenWebUIRuntime()
            await runtime.init_session(
                "task",
                "a-interact",
                agent_profile={
                    "name": "kg-v1",
                    "tools": KG_TOOLS,
                    "prompt_template": "kg",
                },
            )
            fake = FakeClient()
            with patch("system_agent.openwebui_runtime.get_client", return_value=fake), patch(
                "system_agent.openwebui_runtime.execute_tool", return_value='{"nodes": []}'
            ):
                result = await runtime.run_turn("task", "a-interact", "find the metric")
            self.assertEqual(
                [item["function"]["name"] for item in fake.calls[0]["tools"]],
                KG_TOOLS,
            )
            tool_messages = [item for item in fake.calls[1]["messages"] if item["role"] == "tool"]
            self.assertEqual(tool_messages[0]["tool_call_id"], "call-1")
            self.assertEqual(result["response"], "done")

        asyncio.run(scenario())


class EmbeddingConfigurationTests(unittest.TestCase):
    def test_embedding_client_uses_dedicated_key_and_base_url(self):
        with patch.dict(
            os.environ,
            {
                "EMBEDDING_API_KEY": "embedding-secret",
                "EMBEDDING_API_BASE_URL": "https://embeddings.example/v1",
                "OPENAI_API_KEY": "chat-secret",
            },
            clear=False,
        ), patch("embedding.server.OpenAI") as client:
            Embedder()._load()

        kwargs = client.call_args.kwargs
        self.assertEqual(kwargs["api_key"], "embedding-secret")
        self.assertEqual(kwargs["base_url"], "https://embeddings.example/v1")
        self.assertNotEqual(kwargs["api_key"], "chat-secret")

    def test_chat_key_is_not_an_embedding_credential_fallback(self):
        with patch.dict(
            os.environ,
            {
                "EMBEDDING_API_KEY": "",
                "OPENAI_EMBEDDING_API_KEY": "",
                "OPENAI_API_KEY": "chat-secret",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "EMBEDDING_API_KEY is required"):
                Embedder()._load()


if __name__ == "__main__":
    unittest.main()
