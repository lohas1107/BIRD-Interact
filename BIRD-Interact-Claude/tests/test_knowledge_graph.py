import asyncio
import tempfile
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch

from mcp.types import CallToolRequest, CallToolRequestParams
from db_environment.knowledge_graph import (
    KnowledgeGraphError,
    Neo4jKnowledgeRepository,
)
from system_agent.tools import build_tool_server


class _Result(list):
    def single(self):
        return self[0] if self else None


class _KnowledgeTx:
    nodes = {
        "alien:10": {
            "id": "alien:10", "name": "root", "type": "domain_knowledge",
            "summary": "root summary", "definition": "root definition",
            "source_file": "alien_kb.jsonl", "source_line": 11, "source_id": 10,
        },
        "alien:4": {
            "id": "alien:4", "name": "four", "type": "calculation_knowledge",
            "summary": "four summary", "definition": "four definition",
            "source_file": "alien_kb.jsonl", "source_line": 5, "source_id": 4,
        },
        "alien:1": {
            "id": "alien:1", "name": "one", "type": "calculation_knowledge",
            "summary": "one summary", "definition": "one definition",
            "source_file": "alien_kb.jsonl", "source_line": 2, "source_id": 1,
        },
        "alien:0": {
            "id": "alien:0", "name": "zero", "type": "calculation_knowledge",
            "summary": "zero summary", "definition": "zero definition",
            "source_file": "alien_kb.jsonl", "source_line": 1, "source_id": 0,
        },
    }
    edges = {
        "alien:10": [(0, "alien:4"), (1, "alien:1")],
        "alien:4": [(0, "alien:0")],
    }

    def __init__(self):
        self.child_queries = 0

    def run(self, query, **params):
        if "WHERE k.id = $root_id" in query:
            root_id = params["root_id"]
            if root_id in params["hidden_ids"]:
                return _Result()
            node = self.nodes.get(root_id)
            return _Result([{"node": node}]) if node else _Result()

        if "UNWIND range" in query:
            self.child_queries += 1
            rows = []
            hidden = set(params["hidden_ids"])
            for parent_id in params["parent_ids"]:
                if parent_id in hidden:
                    continue
                for position, child_id in self.edges.get(parent_id, []):
                    if child_id in hidden:
                        continue
                    rows.append({
                        "parent_id": parent_id,
                        "position": position,
                        "node": self.nodes[child_id],
                    })
            return _Result(rows)

        raise AssertionError(f"unexpected query: {query}")


class KnowledgeGraphRepositoryTests(unittest.TestCase):
    def test_normalization_enforces_global_id_and_expand_contract(self):
        normalized = Neo4jKnowledgeRepository._normalize({
            "id": "alien:10",
            "include": ["definition", "summary", "definition"],
            "expand": {"depth": 1, "max_nodes": 20},
        })
        self.assertEqual(normalized["root_id"], "alien:10")
        self.assertEqual(normalized["include"], ["definition", "summary"])

        invalid_requests = [
            {"id": "Technosignature"},
            {"id": "ALIEN:10"},
            {"id": "alien:-1"},
            {"id": "alien:1", "include": ["name"]},
            {"id": "alien:1", "expand": {"depth": 1}},
            {"id": "alien:1", "expand": {"depth": 1, "max_nodes": 20, "direction": "out"}},
            {"id": "alien:1", "expand": {"depth": 6, "max_nodes": 20}},
            {"id": "alien:1", "expand": {"depth": 1, "max_nodes": 0}},
        ]
        for request in invalid_requests:
            with self.subTest(request=request), self.assertRaises(KnowledgeGraphError) as context:
                Neo4jKnowledgeRepository._normalize(request)
            self.assertEqual(context.exception.code, "INVALID_REQUEST")

    def test_bfs_projection_include_and_max_nodes(self):
        tx = _KnowledgeTx()
        response = Neo4jKnowledgeRepository._read_knowledge(tx, {
            "database_name": "alien",
            "root_id": "alien:10",
            "include": ["summary"],
            "expand": {"depth": 2, "max_nodes": 3},
            "hidden_ids": [],
        })

        self.assertEqual(
            [node["id"] for node in response["nodes"]],
            ["alien:10", "alien:4", "alien:1"],
        )
        self.assertEqual([node["distance"] for node in response["nodes"]], [0, 1, 1])
        self.assertTrue(response["truncated"])
        self.assertEqual(
            response["edges"],
            [
                {"from": "alien:10", "to": "alien:4", "type": "REQUIRES", "position": 0},
                {"from": "alien:10", "to": "alien:1", "type": "REQUIRES", "position": 1},
            ],
        )
        self.assertIn("summary", response["nodes"][0])
        self.assertNotIn("definition", response["nodes"][0])
        self.assertNotIn("provenance", response["nodes"][0])

    def test_root_only_does_not_traverse_and_hidden_dependency_is_not_leaked(self):
        tx = _KnowledgeTx()
        root_only = Neo4jKnowledgeRepository._read_knowledge(tx, {
            "database_name": "alien",
            "root_id": "alien:10",
            "include": [],
            "expand": None,
            "hidden_ids": [],
        })
        self.assertEqual(len(root_only["nodes"]), 1)
        self.assertEqual(tx.child_queries, 0)

        masked = Neo4jKnowledgeRepository._read_knowledge(tx, {
            "database_name": "alien",
            "root_id": "alien:10",
            "include": [],
            "expand": {"depth": 2, "max_nodes": 20},
            "hidden_ids": ["alien:4"],
        })
        self.assertEqual([node["id"] for node in masked["nodes"]], ["alien:10", "alien:1"])
        self.assertEqual(masked["edges"], [{
            "from": "alien:10", "to": "alien:1", "type": "REQUIRES", "position": 1,
        }])

    def test_masked_root_is_same_as_not_found(self):
        with self.assertRaises(KnowledgeGraphError) as context:
            Neo4jKnowledgeRepository._read_root(_KnowledgeTx(), {
                "database_name": "alien",
                "root_id": "alien:10",
                "hidden_ids": ["alien:10"],
            })
        self.assertEqual(context.exception.code, "KNOWLEDGE_NOT_FOUND")


class KnowledgeGraphMappingTests(unittest.TestCase):
    def test_lite_source_mapping_has_expected_knowledge_graph_counts(self):
        module_path = (
            Path(__file__).resolve().parents[2]
            / "knowledge-graph"
            / "20160814"
            / "generate_mapping.py"
        )
        spec = importlib.util.spec_from_file_location("kg_generate_mapping", module_path)
        generate_mapping = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generate_mapping)

        source = Path(__file__).resolve().parents[1] / "bird-interact-lite"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mapping.cypher"
            stats = generate_mapping.emit(source, output)
            mapping = output.read_text(encoding="utf-8")
        self.assertEqual(stats["knowledge_nodes"], 1082)
        self.assertEqual(stats["requires_edges"], 1143)
        self.assertEqual(mapping.count("MERGE (n:Knowledge"), 1082)
        self.assertEqual(mapping.count("MERGE (parent)-[:REQUIRES"), 1143)
        self.assertIn('MERGE (n:Knowledge {id: "fake:74"})', mapping)
        self.assertIn('MERGE (n:Knowledge {id: "fake:77"})', mapping)
        self.assertNotIn("KnowledgeSet", mapping)


class KnowledgeGraphToolTests(unittest.TestCase):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"root_id": "alien:10", "nodes": [], "edges": [], "truncated": False}

    class _Client:
        calls = []

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            self.calls.append((url, json))
            return KnowledgeGraphToolTests._Response()

    @staticmethod
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

    def test_tool_adds_task_id_internally_and_keeps_graph_request_model_facing(self):
        self._Client.calls = []
        state = {
            "task_id": "graph-task",
            "tool_profile": {"name": "kg-v1", "tools": ["get_knowledge"]},
        }
        with patch("system_agent.tools.httpx.AsyncClient", self._Client):
            server, names = build_tool_server(state, "c-interact")
            response = self._call_handler(server, "get_knowledge", {
                "id": "alien:10",
                "include": ["summary"],
                "expand": {"depth": 1, "max_nodes": 20},
            })
        self.assertEqual(names, ["mcp__bird__get_knowledge"])
        self.assertEqual(self._Client.calls[0][1], {
            "task_id": "graph-task",
            "id": "alien:10",
            "include": ["summary"],
            "expand": {"depth": 1, "max_nodes": 20},
        })
        self.assertIn('"root_id": "alien:10"', response.root.content[0].text)


if __name__ == "__main__":
    unittest.main()
