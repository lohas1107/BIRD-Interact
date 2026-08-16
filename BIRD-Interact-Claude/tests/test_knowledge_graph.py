import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.types import CallToolRequest, CallToolRequestParams

from db_environment.knowledge_graph import (
    GraphSchemaError,
    KnowledgeGraphError,
    Neo4jKnowledgeRepository,
    Neo4jSchemaRepository,
    SemanticSearchRepository,
)
from system_agent.tools import build_tool_server


class _Result(list):
    def single(self):
        return self[0] if self else None


class _KnowledgeTx:
    nodes = {
        "alien:10": {
            "id": "alien:10", "name": "root", "type": "domain_knowledge",
            "description": "root description", "definition": "root definition",
            "source_file": "alien_kb.jsonl", "source_line": 11, "source_id": 10,
        },
        "alien:4": {
            "id": "alien:4", "name": "four", "type": "calculation_knowledge",
            "description": "four description", "definition": "four definition",
            "source_file": "alien_kb.jsonl", "source_line": 5, "source_id": 4,
        },
        "alien:1": {
            "id": "alien:1", "name": "one", "type": "calculation_knowledge",
            "description": "one description", "definition": "one definition",
            "source_file": "alien_kb.jsonl", "source_line": 2, "source_id": 1,
        },
        "alien:0": {
            "id": "alien:0", "name": "zero", "type": "calculation_knowledge",
            "description": "zero description", "definition": "zero definition",
            "source_file": "alien_kb.jsonl", "source_line": 1, "source_id": 0,
        },
    }
    edges = {
        "alien:10": [(0, "alien:4"), (1, "alien:1")],
        "alien:4": [(0, "alien:0")],
    }
    related = {
        "alien:10": [{
            "column_id": "alien:signals:snrratio",
            "table_name": "signals",
            "column_name": "snrratio",
        }],
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

        if "UNWIND $knowledge_ids" in query:
            rows = []
            for knowledge_id in params["knowledge_ids"]:
                for related in self.related.get(knowledge_id, []):
                    rows.append({"knowledge_id": knowledge_id, **related})
            return _Result(rows)

        raise AssertionError(f"unexpected query: {query}")


class KnowledgeGraphRepositoryTests(unittest.TestCase):
    def test_schema_normalization_uses_hops_and_paths(self):
        request = type("Request", (), {
            "database_name": "ALIEN",
            "from_table": "Signals",
            "to_table": "Observatories",
            "include": ["columns", "columns"],
            "hops": 10,
            "paths": 20,
        })()
        normalized = Neo4jSchemaRepository._normalize(request)
        self.assertEqual(normalized["database_name"], "alien")
        self.assertEqual(normalized["from_table"], "signals")
        self.assertEqual(normalized["to_table"], "observatories")
        self.assertEqual(normalized["hops"], 10)
        self.assertEqual(normalized["paths"], 20)

        for field, value in (("hops", 0), ("hops", 11), ("paths", 0), ("paths", 21)):
            invalid = type("Request", (), {
                "database_name": "alien",
                "from_table": "signals",
                "to_table": None,
                "include": None,
                "hops": 5,
                "paths": 5,
            })()
            setattr(invalid, field, value)
            with self.subTest(field=field, value=value), self.assertRaises(GraphSchemaError) as context:
                Neo4jSchemaRepository._normalize(invalid)
            self.assertEqual(context.exception.code, "INVALID_REQUEST")

    def test_knowledge_scope_rejects_a_different_task_database(self):
        with self.assertRaises(KnowledgeGraphError) as context:
            Neo4jKnowledgeRepository().get_knowledge(
                {"knowledge_id": "other:10"},
                hidden_ids=[],
                task_database_name="alien",
            )
        self.assertEqual(context.exception.code, "KNOWLEDGE_NOT_FOUND")

    def test_normalization_enforces_new_contract(self):
        normalized = Neo4jKnowledgeRepository._normalize({
            "knowledge_id": "alien:10",
            "include": ["definition", "description", "definition"],
            "expand": {"depth": 1, "nodes": 20},
        })
        self.assertEqual(normalized["root_id"], "alien:10")
        self.assertEqual(normalized["include"], ["definition", "description"])
        self.assertEqual(normalized["expand"], {"depth": 1, "max_nodes": 20})

        invalid_requests = [
            {"id": "alien:10"},
            {"knowledge_id": "Technosignature"},
            {"knowledge_id": "ALIEN:10"},
            {"knowledge_id": "alien:-1"},
            {"knowledge_id": "alien:1", "include": ["summary"]},
            {"knowledge_id": "alien:1", "expand": {"depth": 1}},
            {"knowledge_id": "alien:1", "expand": {"depth": 1, "nodes": 20, "direction": "out"}},
            {"knowledge_id": "alien:1", "expand": {"depth": 6, "nodes": 20}},
            {"knowledge_id": "alien:1", "expand": {"depth": 1, "nodes": 0}},
        ]
        for request in invalid_requests:
            with self.subTest(request=request), self.assertRaises(GraphSchemaError) as context:
                Neo4jKnowledgeRepository._normalize(request)
            self.assertEqual(context.exception.code, "INVALID_REQUEST")

    def test_bfs_projection_include_related_columns_and_max_nodes(self):
        tx = _KnowledgeTx()
        response = Neo4jKnowledgeRepository._read_knowledge(tx, {
            "database_name": "alien",
            "root_id": "alien:10",
            "include": ["description", "related_columns"],
            "expand": {"depth": 2, "max_nodes": 3},
            "hidden_ids": [],
        })

        self.assertEqual(
            [node["knowledge_id"] for node in response["nodes"]],
            ["alien:10", "alien:4", "alien:1"],
        )
        self.assertEqual([node["distance"] for node in response["nodes"]], [0, 1, 1])
        self.assertTrue(response["truncated"])
        self.assertEqual(
            response["edges"],
            [
                {"from_knowledge_id": "alien:10", "to_knowledge_id": "alien:4", "type": "DEPENDS_ON", "position": 0},
                {"from_knowledge_id": "alien:10", "to_knowledge_id": "alien:1", "type": "DEPENDS_ON", "position": 1},
            ],
        )
        self.assertEqual(response["warnings"], [])
        self.assertEqual(response["nodes"][0]["description"], "root description")
        self.assertEqual(response["nodes"][0]["related_columns"][0]["column_name"], "snrratio")
        self.assertNotIn("summary", response["nodes"][0])

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
        self.assertEqual([node["knowledge_id"] for node in masked["nodes"]], ["alien:10", "alien:1"])
        self.assertEqual(masked["edges"], [{
            "from_knowledge_id": "alien:10",
            "to_knowledge_id": "alien:1",
            "type": "DEPENDS_ON",
            "position": 1,
        }])

    def test_masked_root_is_same_as_not_found(self):
        with self.assertRaises(KnowledgeGraphError) as context:
            Neo4jKnowledgeRepository._read_root(_KnowledgeTx(), {
                "database_name": "alien",
                "root_id": "alien:10",
                "hidden_ids": ["alien:10"],
            })
        self.assertEqual(context.exception.code, "KNOWLEDGE_NOT_FOUND")


class SemanticSearchRepositoryTests(unittest.TestCase):
    class _SearchTx:
        nodes = {
            "alien:1": {"id": "alien:1", "name": "one", "type": "domain_knowledge"},
            "alien:2": {"id": "alien:2", "name": "two", "type": "calculation_knowledge"},
            "alien:3": {"id": "alien:3", "name": "three", "type": "calculation_knowledge"},
            "alien:4": {"id": "alien:4", "name": "four", "type": "domain_knowledge"},
            "alien:signals:snr": {
                "id": "alien:signals:snr", "table_name": "signals", "column_name": "snr",
                "column_type": "numeric", "description": "signal to noise ratio",
            },
            "alien:signals:noise": {
                "id": "alien:signals:noise", "table_name": "signals", "column_name": "noise",
                "column_type": "numeric", "description": "noise floor",
            },
            "alien:signals:quality": {
                "id": "alien:signals:quality", "table_name": "signals", "column_name": "quality",
                "column_type": "numeric", "description": "signal quality",
            },
        }
        vector = {
            ("Knowledge", 1): [("alien:1", 0.3), ("alien:2", 0.8)],
            ("Knowledge", 2): [("alien:1", 0.9), ("alien:3", 0.7)],
            ("Column", 1): [("alien:signals:snr", 0.6), ("alien:signals:noise", 0.2)],
            ("Column", 2): [("alien:signals:noise", 0.95), ("alien:signals:quality", 0.7)],
        }
        fulltext = {
            ("Knowledge", "first"): [("alien:1", 2.0), ("alien:4", 1.0)],
            ("Knowledge", "second"): [("alien:3", 3.0), ("alien:2", 1.0)],
            ("Column", "first"): [("alien:signals:snr", 2.0), ("alien:signals:noise", 1.0)],
            ("Column", "second"): [("alien:signals:noise", 4.0), ("alien:signals:quality", 1.0)],
        }

        def run(self, query, **params):
            if "MATCH (d:Database" in query:
                return _Result([{"id": "alien"}])
            if "CALL db.index.vector.queryNodes" in query:
                key = (params["label"], params["embedding"][0])
                return _Result([
                    {"node": self.nodes[node_id], "score": score}
                    for node_id, score in self.vector[key]
                ])
            if "CALL db.index.fulltext.queryNodes" in query:
                key = (params["label"], params["text_query"])
                return _Result([
                    {"node": self.nodes[node_id], "score": score}
                    for node_id, score in self.fulltext[key]
                ])
            if "UNWIND $knowledge_ids" in query:
                return _Result([{
                    "knowledge_id": "alien:1",
                    "column_id": "alien:signals:snr",
                    "table_name": "signals",
                    "column_name": "snr",
                }])
            raise AssertionError(f"unexpected query: {query}")

    def test_search_merges_each_query_and_limits_each_resource_group(self):
        response = SemanticSearchRepository._read_search(self._SearchTx(), {
            "database_name": "alien",
            "hidden_ids": [],
            "queries": ["first", "second"],
            "embeddings": [[1], [2]],
            "top_k": 2,
            "resource_types": ["knowledge", "table_schema"],
        })

        self.assertEqual([hit["knowledge_id"] for hit in response["knowledge"]], ["alien:1", "alien:3"])
        self.assertEqual([hit["column_id"] for hit in response["table_schema"]], [
            "alien:signals:noise", "alien:signals:snr",
        ])
        self.assertEqual(response["knowledge"][0]["related_columns"][0]["column_name"], "snr")
        for group in (response["knowledge"], response["table_schema"]):
            for hit in group:
                self.assertNotIn("snippet", hit)
                self.assertNotIn("edge", hit)

    def test_search_request_validation(self):
        valid = SemanticSearchRepository._normalize({
            "queries": ["signal quality", "atmosphere"],
            "top_k": 5,
            "resource_types": ["knowledge", "table_schema", "knowledge"],
        })
        self.assertEqual(valid["resource_types"], ["knowledge", "table_schema"])
        invalid = [
            {"queries": [], "top_k": 5, "resource_types": ["knowledge"]},
            {"queries": [""], "top_k": 5, "resource_types": ["knowledge"]},
            {"queries": ["x"], "top_k": 0, "resource_types": ["knowledge"]},
            {"queries": ["x"], "top_k": 21, "resource_types": ["knowledge"]},
            {"queries": ["x"], "top_k": 5, "resource_types": []},
            {"queries": ["x"], "top_k": 5, "resource_types": ["foo"]},
        ]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(GraphSchemaError):
                SemanticSearchRepository._normalize(request)

    def test_score_normalization_and_qwen_query_text(self):
        self.assertEqual(
            SemanticSearchRepository._normalize_fulltext_scores({"a": 2.0, "b": 4.0}),
            {"a": 0.0, "b": 1.0},
        )
        self.assertEqual(
            SemanticSearchRepository._normalize_fulltext_scores({"a": 2.0}),
            {"a": 1.0},
        )
        self.assertEqual(
            SemanticSearchRepository._fulltext_query("SNR < 0.5; noise-floor"),
            "SNR 0 5 noise floor",
        )


class KnowledgeGraphMappingTests(unittest.TestCase):
    def test_lite_source_mapping_has_expected_semantic_graph_counts(self):
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
        self.assertEqual(stats["depends_on_edges"], 1143)
        self.assertEqual(stats["refers_to_column_edges"], 1224)
        self.assertEqual(mapping.count("MERGE (n:Knowledge:SemanticSearch"), 1082)
        self.assertEqual(mapping.count("MERGE (parent)-[:DEPENDS_ON"), 1143)
        self.assertEqual(mapping.count("MERGE (k)-[:REFERS_TO_COLUMN]->(c)"), 1224)
        self.assertIn('MERGE (n:Knowledge:SemanticSearch {id: "fake:74"})', mapping)
        self.assertIn('MERGE (n:Knowledge:SemanticSearch {id: "fake:77"})', mapping)
        self.assertNotIn("KnowledgeSet", mapping)
        self.assertNotIn("REQUIRES", mapping)
        self.assertNotIn("entity_key", mapping)
        self.assertNotIn("summary:", mapping)
        self.assertNotIn("data_type:", mapping)


class KnowledgeGraphToolTests(unittest.TestCase):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"knowledge_id": "alien:10", "nodes": [], "edges": [], "truncated": False, "warnings": []}

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

    def test_tool_adds_task_id_internally_and_uses_new_knowledge_contract(self):
        self._Client.calls = []
        state = {
            "task_id": "graph-task",
            "tool_profile": {"name": "kg-v1", "tools": ["get_knowledge"]},
        }
        with patch("system_agent.tools.httpx.AsyncClient", self._Client):
            server, names = build_tool_server(state, "c-interact")
            response = self._call_handler(server, "get_knowledge", {
                "knowledge_id": "alien:10",
                "include": ["description", "related_columns"],
                "expand": {"depth": 1, "nodes": 20},
            })
        self.assertEqual(names, ["mcp__bird__get_knowledge"])
        self.assertEqual(self._Client.calls[0][1], {
            "task_id": "graph-task",
            "knowledge_id": "alien:10",
            "include": ["description", "related_columns"],
            "expand": {"depth": 1, "nodes": 20},
        })
        self.assertIn('"knowledge_id": "alien:10"', response.root.content[0].text)

    def test_search_tool_keeps_database_scope_internal(self):
        self._Client.calls = []
        state = {
            "task_id": "search-task",
            "tool_profile": {"name": "kg-v1", "tools": ["search_semantic_context"]},
        }
        with patch("system_agent.tools.httpx.AsyncClient", self._Client):
            server, names = build_tool_server(state, "c-interact")
            self._call_handler(server, "search_semantic_context", {
                "queries": ["signal quality"],
                "top_k": 5,
                "resource_types": ["knowledge", "table_schema"],
            })
        self.assertEqual(names, ["mcp__bird__search_semantic_context"])
        self.assertEqual(self._Client.calls[0][1], {
            "task_id": "search-task",
            "queries": ["signal quality"],
            "top_k": 5,
            "resource_types": ["knowledge", "table_schema"],
        })


if __name__ == "__main__":
    unittest.main()
