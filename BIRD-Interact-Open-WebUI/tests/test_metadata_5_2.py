import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from db_environment.metadata_5_2 import (
    MetadataSearchError,
    MetadataSearchRepository,
    _validate_metadata_file,
    canonical_manifest_hash,
)
from scripts.generate_metadata_5_2 import MetadataCorpusGenerator
from shared.agent_profiles import resolve_agent_profile
from system_agent import tools


def _column(table, name, description, refs=None):
    return {
        "column_id": f"alien:{table}:{name}",
        "column_name": name,
        "column_type": "numeric",
        "description": description,
        "metadata": {
            "formulas": [{"value": description, "knowledge_refs": refs or []}],
            "classification_rules": [],
            "table_structure": {},
            "disambiguation_rules": [],
            "known_discrepancies": [],
            "column_identity_references": [],
            "observed_value_range": "",
        },
    }


class _EmbeddingResponse:
    def __init__(self, vectors):
        self.vectors = vectors

    def raise_for_status(self):
        return None

    def json(self):
        return {"embeddings": self.vectors}


class _EmbeddingClient:
    vectors = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def post(self, url, json):
        return _EmbeddingResponse(self.vectors[: len(json["texts"])])


class MetadataSearchTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.data = root / "data"
        self.data.mkdir()
        (self.data / "alien").mkdir()
        self.cache = root / ".cache" / "metadata-5-2-kg"
        self.cache.mkdir(parents=True)
        self.config = SimpleNamespace(
            data_dir=self.data,
            project_root=root,
            embedding_timeout=1.0,
            embedding_service_url="http://embedding",
        )

        self.manifest = {
            "schema_version": "metadata-5-2-v1",
            "generator_model": "gpt-5.6-sol",
            "embedding_model": "text-embedding-3-small",
            "dimensions": 1536,
            "metadata_source_hash": "source-hash",
            "databases": ["alien"],
            "column_count": 2,
        }
        (self.data / "metadata_manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        metadata = {
            "schema_version": "metadata-5-2-v1",
            "database": "alien",
            "formulas": [],
            "classification_rules": [],
            "table_structure": {},
            "disambiguation_rules": [],
            "known_discrepancies": [],
            "tables": [{
                "table_id": "alien:signals",
                "table_name": "signals",
                "metadata": {"table_structure": {}},
                "columns": [
                    _column("signals", "score", "visible score", ["alien:7"]),
                    _column("signals", "name", "visible name"),
                ],
            }],
        }
        (self.data / "alien" / "alien_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

        fragments = []
        for column_id, column_name, text, refs in [
            ("alien:signals:score", "score", "score", []),
            ("alien:signals:score", "score", "visible score", ["alien:7"]),
            ("alien:signals:name", "name", "name", []),
            ("alien:signals:name", "name", "visible name", []),
        ]:
            kind = "description" if text.startswith("visible") else "column_name"
            fragments.append({
                "fragment_id": f"{column_id}:{kind}:{len(fragments)}",
                "database_name": "alien",
                "column_id": column_id,
                "kind": kind,
                "text": text,
                "knowledge_refs": refs,
            })
        cache_manifest = {
            "schema_version": "metadata-5-2-v1",
            "metadata_source_hash": "source-hash",
            "manifest_hash": canonical_manifest_hash(self.manifest),
            "embedding_model": "text-embedding-3-small",
            "dimensions": 1536,
        }
        (self.cache / "cache_manifest.json").write_text(json.dumps(cache_manifest), encoding="utf-8")
        (self.cache / "metadata_embedding_index.json").write_text(json.dumps({"fragments": fragments}), encoding="utf-8")
        matrix = np.zeros((len(fragments), 1536), dtype=np.float32)
        matrix[0, 0] = 1.0
        matrix[1, 0] = 1.0
        matrix[2, 1] = 1.0
        matrix[3, 1] = 1.0
        np.savez_compressed(self.cache / "metadata_embeddings.npz", embeddings=matrix)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_metadata_response_merges_fragments_and_masks_hidden_refs(self):
        _EmbeddingClient.vectors = [
            [1.0] + [0.0] * 1535,
            [0.5, 0.5] + [0.0] * 1534,
        ]
        repository = MetadataSearchRepository(self.config)
        with patch("db_environment.metadata_5_2.httpx.Client", _EmbeddingClient):
            response = repository.search(
                {"queries": ["score", "name"], "top_k": 1, "resource_types": ["metadata"]},
                "alien",
                {"alien:7"},
            )
        self.assertEqual(set(response), {"knowledge", "metadata"})
        self.assertEqual(len(response["metadata"]), 1)
        self.assertEqual(response["metadata"][0]["column_id"], "alien:signals:score")
        self.assertEqual(response["metadata"][0]["formulas"], [])
        self.assertEqual(response["metadata"][0]["score"], 1.0)
        self.assertNotIn("knowledge_refs", json.dumps(response))
        self.assertNotIn("table_schema", response)

    def test_new_tool_forwards_server_task_and_fixed_response_shape(self):
        class Response:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"knowledge": [], "metadata": []}

        class Client:
            calls = []

            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, url, json):
                self.calls.append((url, json))
                return Response()

        state = {"agent_profile": {"name": "metadata-5-2-kg", "tools": ["search_semantic_context_5_2"]}}
        args = {"task_id": "model-controlled", "queries": ["x"], "top_k": 1, "resource_types": ["metadata"]}
        with patch("system_agent.tools.httpx.Client", Client):
            result = tools.execute_tool("search_semantic_context_5_2", args, "server-task", state)
        self.assertEqual(json.loads(result), {"knowledge": [], "metadata": []})
        self.assertEqual(Client.calls[0][1]["task_id"], "server-task")

    def test_missing_cache_and_embedding_failure_are_fail_closed(self):
        cache_file = self.cache / "metadata_embeddings.npz"
        cache_file.unlink()
        with self.assertRaises(MetadataSearchError) as raised:
            MetadataSearchRepository(self.config).search(
                {"queries": ["x"], "top_k": 1, "resource_types": ["metadata"]},
                "alien",
            )
        self.assertEqual(raised.exception.code, "METADATA_UNAVAILABLE")

        # Recreate the cache, then make the embedding gateway fail.
        matrix = np.zeros((4, 1536), dtype=np.float32)
        np.savez_compressed(cache_file, embeddings=matrix)
        import httpx

        class BrokenClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                raise httpx.ConnectError("offline")

            def __exit__(self, *args):
                return None

        with patch("db_environment.metadata_5_2.httpx.Client", BrokenClient):
            with self.assertRaises(MetadataSearchError) as raised:
                MetadataSearchRepository(self.config).search(
                    {"queries": ["x"], "top_k": 1, "resource_types": ["metadata"]},
                    "alien",
                )
        self.assertEqual(raised.exception.code, "EMBEDDING_UNAVAILABLE")


class MetadataCorpusSmokeTests(unittest.TestCase):
    def test_generated_lite_metadata_artifacts_load(self):
        source = Path(__file__).resolve().parents[1] / "bird-interact-lite"
        manifest_path = source / "metadata_manifest.json"
        self.assertTrue(manifest_path.exists(), "run the one-time metadata generator first")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], "metadata-5-2-v1")
        self.assertEqual(manifest["generator_model"], "gpt-5.6-sol")
        self.assertEqual(manifest["embedding_model"], "text-embedding-3-small")
        self.assertEqual(manifest["dimensions"], 1536)
        self.assertEqual(len(manifest["databases"]), 18)
        columns = 0
        for database in manifest["databases"]:
            path = source / database / f"{database}_metadata.json"
            self.assertTrue(path.exists(), path)
            columns += len(_validate_metadata_file(json.loads(path.read_text(encoding="utf-8")), database))
        self.assertEqual(columns, 2286)
        self.assertEqual(manifest["column_count"], columns)

    def test_all_lite_sources_parse_to_2286_columns_without_forbidden_task_fields(self):
        source = Path(__file__).resolve().parents[1] / "bird-interact-lite"

        class EmptyRangeReader:
            def read(self, database, tables):
                return {
                    table["table_name"]: {column["column_name"]: "" for column in table["columns"]}
                    for table in tables
                }

        result = MetadataCorpusGenerator(source, EmptyRangeReader()).generate()
        self.assertEqual(len(result["databases"]), 18)
        self.assertEqual(result["manifest"]["column_count"], 2286)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("sql_snippet", serialized)
        self.assertNotIn("external_knowledge", serialized)
        for database, metadata in result["databases"].items():
            self.assertEqual(metadata["schema_version"], "metadata-5-2-v1")
            self.assertEqual(metadata["database"], database)
            for table in metadata["tables"]:
                for column in table["columns"]:
                    self.assertEqual(column["column_id"], f"{database}:{table['table_name']}:{column['column_name']}")
                    self.assertIsInstance(column["metadata"]["observed_value_range"], str)
                    self.assertEqual(column["metadata"]["known_discrepancies"], [])


class MetadataProfileAndToolTests(unittest.TestCase):
    def test_profile_and_tool_contract(self):
        for mode in ("a-interact", "c-interact"):
            profile = resolve_agent_profile(mode, "metadata-5-2-kg")
            self.assertIn("search_semantic_context_5_2", profile.tools)
        schema = tools.TOOL_SCHEMAS["search_semantic_context_5_2"]
        self.assertEqual(
            schema["function"]["parameters"]["properties"]["resource_types"]["items"]["enum"],
            ["knowledge", "metadata"],
        )


if __name__ == "__main__":
    unittest.main()
