"""Local metadata corpus and retrieval for the ``metadata-5-2-kg`` profile.

The corpus is deliberately separate from the Neo4j-backed kg-v1 repository.
Metadata is generated once by :mod:`scripts.generate_metadata_5_2` and its
embeddings are generated once by :mod:`scripts.build_metadata_embeddings_5_2`.
The service never creates or repairs either artifact while serving evaluation
requests: missing or stale artifacts are an explicit service error.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

import httpx

from shared.config import settings

try:
    from db_environment.knowledge_graph import GraphSchemaError
except ImportError:  # Keep offline corpus tooling usable without Neo4j extras.
    class GraphSchemaError(Exception):
        STATUS_CODES = {
            "INVALID_REQUEST": 400,
            "EMBEDDING_UNAVAILABLE": 503,
            "EMBEDDING_INVALID": 502,
            "NEO4J_UNAVAILABLE": 503,
            "GRAPH_QUERY_FAILED": 500,
        }

        def __init__(self, code: str, message: str):
            self.code = code
            self.message = message
            super().__init__(f"{code}: {message}")

        @property
        def status_code(self) -> int:
            return self.STATUS_CODES.get(self.code, 500)

try:  # NumPy is an explicit runtime dependency, but keep imports fail-closed.
    import numpy as np
except ImportError:  # pragma: no cover - exercised in incomplete deployments
    np = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

METADATA_SCHEMA_VERSION = "metadata-5-2-v1"
GENERATOR_MODEL = "gpt-5.6-sol"
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
MANIFEST_FILENAME = "metadata_manifest.json"
METADATA_FILENAME_SUFFIX = "_metadata.json"
CACHE_DIRNAME = ".cache/metadata-5-2-kg"
CACHE_EMBEDDINGS_FILENAME = "metadata_embeddings.npz"
CACHE_INDEX_FILENAME = "metadata_embedding_index.json"
CACHE_MANIFEST_FILENAME = "cache_manifest.json"

DATASET_FIELDS = (
    "formulas",
    "classification_rules",
    "table_structure",
    "disambiguation_rules",
    "known_discrepancies",
)
COLUMN_FIELDS = (
    "column_identity_references",
    "observed_value_range",
)
EMBEDDING_FRAGMENT_FIELDS = (
    "table_name",
    "column_name",
    "description",
    "formulas",
    "disambiguation_rules",
    "known_discrepancies",
)
RESOURCE_TYPES = ("knowledge", "metadata")
RESOURCE_TYPE_SET = frozenset(RESOURCE_TYPES)


class MetadataSearchError(GraphSchemaError):
    """Stable public error for the local metadata backend."""

    STATUS_CODES = {
        **GraphSchemaError.STATUS_CODES,
        "METADATA_UNAVAILABLE": 503,
        "METADATA_INVALID": 500,
    }


def _as_dict(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MetadataSearchError("METADATA_INVALID", f"{label} must be an object")
    return value


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MetadataSearchError("METADATA_UNAVAILABLE", f"missing {label}: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MetadataSearchError("METADATA_INVALID", f"cannot read {label} {path}: {exc}") from exc
    return _as_dict(raw, label=label)


def metadata_manifest_path(data_dir: Path | None = None) -> Path:
    return (data_dir or settings.data_dir) / MANIFEST_FILENAME


def metadata_cache_dir(project_root: Path | None = None) -> Path:
    return (project_root or settings.project_root) / CACHE_DIRNAME


def canonical_manifest_hash(manifest: Mapping[str, Any]) -> str:
    """Hash the fixed manifest without depending on JSON whitespace."""
    payload = dict(manifest)
    # The manifest may carry this derived convenience field.  Excluding it
    # avoids a self-referential hash and lets cache builders reproduce it.
    payload.pop("manifest_hash", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_fragment(value: Any, *, label: str) -> dict[str, Any]:
    """Validate one internal semantic fragment and keep only its contract."""
    if isinstance(value, str):
        # Accept plain strings when reading hand-authored fixtures.  Generated
        # corpora always use the explicit object form with refs.
        return {"value": value, "knowledge_refs": []}
    if not isinstance(value, dict):
        raise MetadataSearchError("METADATA_INVALID", f"{label} must be a fragment object")
    if set(value) - {"value", "knowledge_refs"}:
        extra = sorted(set(value) - {"value", "knowledge_refs"})[0]
        raise MetadataSearchError("METADATA_INVALID", f"{label} has unsupported field {extra!r}")
    text = value.get("value")
    refs = value.get("knowledge_refs", [])
    if not isinstance(text, str):
        raise MetadataSearchError("METADATA_INVALID", f"{label}.value must be a string")
    if (
        not isinstance(refs, list)
        or any(not isinstance(ref, str) or not ref for ref in refs)
    ):
        raise MetadataSearchError("METADATA_INVALID", f"{label}.knowledge_refs must be strings")
    return {"value": text, "knowledge_refs": list(dict.fromkeys(refs))}


def _normalize_fragments(value: Any, *, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MetadataSearchError("METADATA_INVALID", f"{label} must be an array")
    return [_normalize_fragment(item, label=f"{label}[{index}]") for index, item in enumerate(value)]


def _public_fragments(value: Any, *, label: str, hidden_ids: set[str] | None = None) -> list[str]:
    hidden = hidden_ids or set()
    return [
        item["value"]
        for item in _normalize_fragments(value, label=label)
        if not (set(item["knowledge_refs"]) & hidden)
    ]


def _field_from_mapping(mapping: Mapping[str, Any], field: str, *, label: str) -> Any:
    value = mapping.get(field)
    if field in (*DATASET_FIELDS, "column_identity_references"):
        if field == "table_structure":
            if value is None:
                return {}
            if not isinstance(value, dict):
                raise MetadataSearchError("METADATA_INVALID", f"{label}.{field} must be an object")
            return copy.deepcopy(value)
        return _normalize_fragments(value, label=f"{label}.{field}")
    if field == "observed_value_range":
        if value is None:
            return ""
        if not isinstance(value, str):
            raise MetadataSearchError("METADATA_INVALID", f"{label}.{field} must be a string")
        return value
    raise AssertionError(field)


def _metadata_block(value: Any, *, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MetadataSearchError("METADATA_INVALID", f"{label} must be an object")
    return value


def _resolve_table_field(
    raw: Mapping[str, Any],
    table: Mapping[str, Any],
    column: Mapping[str, Any],
    field: str,
    *,
    database: str,
    table_name: str,
    column_name: str,
) -> Any:
    """Resolve relevant metadata without copying the complete dataset.

    The canonical generator writes relevant fragments under the table or
    column block.  The root dataset block is only a provenance/coverage
    summary.  The limited root fallback keeps small test fixtures convenient
    while never using it for ``table_structure`` unless it is keyed by table.
    """
    column_block = _metadata_block(column.get("metadata"), label=f"{database}:{table_name}:{column_name}.metadata")
    table_block = _metadata_block(table.get("metadata"), label=f"{database}:{table_name}.metadata")

    if field in column:
        return _field_from_mapping(column, field, label=f"{database}:{table_name}:{column_name}")
    if field in column_block:
        return _field_from_mapping(column_block, field, label=f"{database}:{table_name}:{column_name}.metadata")
    if field in table:
        return _field_from_mapping(table, field, label=f"{database}:{table_name}")
    if field in table_block:
        return _field_from_mapping(table_block, field, label=f"{database}:{table_name}.metadata")
    if field == "table_structure":
        direct = table.get(field)
        if direct is not None:
            return _field_from_mapping(table, field, label=f"{database}:{table_name}")
        root = raw.get(field)
        if isinstance(root, dict):
            table_value = root.get(table_name) or root.get(f"{database}:{table_name}")
            if table_value is not None:
                if not isinstance(table_value, dict):
                    raise MetadataSearchError(
                        "METADATA_INVALID",
                        f"{database}:{table_name}.table_structure must be an object",
                    )
                return copy.deepcopy(table_value)
        return {}
    # Do not fall back to a complete dataset-level semantic list.  Doing so
    # would duplicate unrelated formulas/rules on every returned column.
    return [] if field != "observed_value_range" else ""


def _validate_metadata_file(raw: Mapping[str, Any], database: str) -> dict[str, dict[str, Any]]:
    schema_version = raw.get("schema_version")
    if schema_version != METADATA_SCHEMA_VERSION:
        raise MetadataSearchError(
            "METADATA_INVALID",
            f"{database} metadata schema_version must be {METADATA_SCHEMA_VERSION!r}",
        )
    raw_database = raw.get("database", raw.get("database_name"))
    if raw_database != database:
        raise MetadataSearchError(
            "METADATA_INVALID",
            f"metadata database {raw_database!r} does not match {database!r}",
        )
    # Validate dataset-level fields even though only relevant table/column
    # fragments are exposed by the search response.
    for field in DATASET_FIELDS:
        if field == "table_structure":
            value = raw.get(field, {})
            if value is not None and not isinstance(value, dict):
                raise MetadataSearchError("METADATA_INVALID", f"{database}.{field} must be an object")
        else:
            _normalize_fragments(raw.get(field, []), label=f"{database}.{field}")

    tables = raw.get("tables")
    if not isinstance(tables, list) or not tables:
        raise MetadataSearchError("METADATA_INVALID", f"{database}.tables must be a non-empty array")

    columns: dict[str, dict[str, Any]] = {}
    table_names: set[str] = set()
    for table_index, table in enumerate(tables):
        if not isinstance(table, dict):
            raise MetadataSearchError("METADATA_INVALID", f"{database}.tables[{table_index}] must be an object")
        table_name = table.get("table_name")
        table_id = table.get("table_id")
        expected_table_id = f"{database}:{table_name}" if isinstance(table_name, str) else None
        if (
            not isinstance(table_name, str)
            or not table_name
            or table_name.casefold() != table_name
            or table_id != expected_table_id
            or table_name in table_names
        ):
            raise MetadataSearchError("METADATA_INVALID", f"invalid or duplicate table identity in {database}")
        table_names.add(table_name)
        _metadata_block(table.get("metadata"), label=f"{database}:{table_name}.metadata")
        if "table_structure" in table:
            _field_from_mapping(table, "table_structure", label=f"{database}:{table_name}")
        table_columns = table.get("columns")
        if not isinstance(table_columns, list) or not table_columns:
            raise MetadataSearchError("METADATA_INVALID", f"{database}:{table_name}.columns must be a non-empty array")
        column_names: set[str] = set()
        for column_index, column in enumerate(table_columns):
            if not isinstance(column, dict):
                raise MetadataSearchError(
                    "METADATA_INVALID",
                    f"{database}:{table_name}.columns[{column_index}] must be an object",
                )
            column_name = column.get("column_name")
            column_id = column.get("column_id")
            expected_column_id = f"{database}:{table_name}:{column_name}" if isinstance(column_name, str) else None
            column_key = f"{database}:{table_name}:{column_name}" if isinstance(column_name, str) else ""
            if (
                not isinstance(column_name, str)
                or not column_name
                or column_name.casefold() != column_name
                or column_id != expected_column_id
                or column_name in column_names
                or column_id in columns
            ):
                raise MetadataSearchError("METADATA_INVALID", f"invalid or duplicate column identity in {database}:{table_name}")
            column_names.add(column_name)
            column_type = column.get("column_type")
            description = column.get("description", "")
            if not isinstance(column_type, str) or not column_type:
                raise MetadataSearchError("METADATA_INVALID", f"{column_key}.column_type must be a string")
            if description is None:
                description = ""
            if not isinstance(description, str):
                raise MetadataSearchError("METADATA_INVALID", f"{column_key}.description must be a string")
            column_block = _metadata_block(column.get("metadata"), label=f"{column_key}.metadata")
            for field in (*DATASET_FIELDS, *COLUMN_FIELDS):
                if field in column_block:
                    _field_from_mapping(column_block, field, label=f"{column_key}.metadata")
            # Direct column fields are accepted for compact fixtures.
            for field in (*DATASET_FIELDS, *COLUMN_FIELDS):
                if field in column:
                    _field_from_mapping(column, field, label=column_key)
            resolved = {
                "table_id": table_id,
                "table_name": table_name,
                "column_id": column_id,
                "column_name": column_name,
                "column_type": column_type,
                "description": description,
                "formulas": _resolve_table_field(raw, table, column, "formulas", database=database, table_name=table_name, column_name=column_name),
                "classification_rules": _resolve_table_field(raw, table, column, "classification_rules", database=database, table_name=table_name, column_name=column_name),
                "table_structure": _resolve_table_field(raw, table, column, "table_structure", database=database, table_name=table_name, column_name=column_name),
                "disambiguation_rules": _resolve_table_field(raw, table, column, "disambiguation_rules", database=database, table_name=table_name, column_name=column_name),
                "known_discrepancies": _resolve_table_field(raw, table, column, "known_discrepancies", database=database, table_name=table_name, column_name=column_name),
                "column_identity_references": _resolve_table_field(raw, table, column, "column_identity_references", database=database, table_name=table_name, column_name=column_name),
                "observed_value_range": _resolve_table_field(raw, table, column, "observed_value_range", database=database, table_name=table_name, column_name=column_name),
            }
            columns[column_id] = resolved
    return columns


def _normalize_request(request: Any) -> dict[str, Any]:
    def value(key: str, default: Any = None) -> Any:
        if isinstance(request, Mapping):
            return request.get(key, default)
        return getattr(request, key, default)

    queries = value("queries")
    if (
        not isinstance(queries, list)
        or not 1 <= len(queries) <= 8
        or any(not isinstance(item, str) or not item.strip() for item in queries)
    ):
        raise MetadataSearchError("INVALID_REQUEST", "queries must contain 1 through 8 non-empty strings")
    top_k = value("top_k")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
        raise MetadataSearchError("INVALID_REQUEST", "top_k must be an integer from 1 through 20")
    resource_types = value("resource_types")
    if (
        not isinstance(resource_types, list)
        or not resource_types
        or any(not isinstance(item, str) for item in resource_types)
    ):
        raise MetadataSearchError("INVALID_REQUEST", "resource_types must contain at least one string")
    unknown = sorted(set(resource_types) - RESOURCE_TYPE_SET)
    if unknown:
        raise MetadataSearchError(
            "INVALID_REQUEST",
            f"resource_types contains unsupported values: {', '.join(unknown)}",
        )
    return {
        "queries": [item.strip() for item in queries],
        "top_k": top_k,
        "resource_types": list(dict.fromkeys(resource_types)),
    }


def _safe_cosine(query_vectors: Any, corpus_vectors: Any) -> Any:
    if np is None:  # pragma: no cover - guarded by repository load
        raise MetadataSearchError("METADATA_UNAVAILABLE", "NumPy is required for metadata retrieval")
    queries = np.asarray(query_vectors, dtype=np.float32)
    corpus = np.asarray(corpus_vectors, dtype=np.float32)
    if queries.ndim != 2 or corpus.ndim != 2 or queries.shape[1] != corpus.shape[1]:
        raise MetadataSearchError("METADATA_INVALID", "embedding matrix dimensions do not match")
    query_norms = np.linalg.norm(queries, axis=1)
    corpus_norms = np.linalg.norm(corpus, axis=1)
    denominators = query_norms[:, None] * corpus_norms[None, :]
    dot = queries @ corpus.T
    scores = np.divide(dot, denominators, out=np.zeros_like(dot), where=denominators != 0)
    # Scores in the public contract are bounded like the existing semantic
    # search scores. Negative cosine similarity is not a useful candidate.
    return np.clip(np.nan_to_num(scores, nan=0.0, posinf=0.0, neginf=0.0), 0.0, 1.0)


class MetadataSearchRepository:
    """Read-only metadata corpus and local NumPy cosine retrieval."""

    def __init__(self, config=settings):
        self._config = config
        self._load_lock = threading.Lock()
        self._loaded = False
        self._manifest: dict[str, Any] | None = None
        self._columns: dict[str, dict[str, Any]] = {}
        self._fragment_index: list[dict[str, Any]] = []
        self._embeddings: Any = None

    def close(self) -> None:
        # The local repository owns no socket or database resource.  Clear the
        # in-memory snapshot so tests and service reloads can re-open artifacts.
        with self._load_lock:
            self._loaded = False
            self._manifest = None
            self._columns = {}
            self._fragment_index = []
            self._embeddings = None

    @staticmethod
    def _dimensions(manifest: Mapping[str, Any]) -> int:
        dimensions = manifest.get("dimensions")
        if dimensions != EMBEDDING_DIMENSIONS:
            raise MetadataSearchError(
                "METADATA_INVALID",
                f"metadata dimensions must be {EMBEDDING_DIMENSIONS}",
            )
        return EMBEDDING_DIMENSIONS

    def _load(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            if np is None:
                raise MetadataSearchError("METADATA_UNAVAILABLE", "NumPy is required for metadata retrieval")

            data_dir = Path(self._config.data_dir)
            manifest = _read_json(metadata_manifest_path(data_dir), label="metadata manifest")
            required = {
                "schema_version",
                "generator_model",
                "embedding_model",
                "dimensions",
                "metadata_source_hash",
            }
            missing = sorted(required - set(manifest))
            if missing:
                raise MetadataSearchError("METADATA_INVALID", f"metadata manifest is missing {missing[0]!r}")
            if manifest["schema_version"] != METADATA_SCHEMA_VERSION:
                raise MetadataSearchError("METADATA_INVALID", "metadata manifest schema_version is unsupported")
            if manifest["generator_model"] != GENERATOR_MODEL:
                raise MetadataSearchError("METADATA_INVALID", "metadata generator_model is unsupported")
            if manifest["embedding_model"] != EMBEDDING_MODEL:
                raise MetadataSearchError("METADATA_INVALID", "metadata embedding_model is unsupported")
            dimensions = self._dimensions(manifest)

            databases = manifest.get("databases")
            if not isinstance(databases, list) or not databases or any(not isinstance(db, str) for db in databases):
                raise MetadataSearchError("METADATA_INVALID", "metadata manifest databases must be non-empty strings")
            columns: dict[str, dict[str, Any]] = {}
            for database in databases:
                if database.casefold() != database or not database:
                    raise MetadataSearchError("METADATA_INVALID", f"invalid metadata database {database!r}")
                path = data_dir / database / f"{database}{METADATA_FILENAME_SUFFIX}"
                raw = _read_json(path, label=f"{database} metadata")
                loaded_columns = _validate_metadata_file(raw, database)
                overlap = set(columns).intersection(loaded_columns)
                if overlap:
                    raise MetadataSearchError("METADATA_INVALID", f"duplicate metadata column {sorted(overlap)[0]}")
                columns.update(loaded_columns)

            cache_dir = metadata_cache_dir(Path(self._config.project_root))
            cache_manifest = _read_json(cache_dir / CACHE_MANIFEST_FILENAME, label="metadata cache manifest")
            if cache_manifest.get("schema_version") != METADATA_SCHEMA_VERSION:
                raise MetadataSearchError("METADATA_INVALID", "metadata cache schema_version is unsupported")
            if cache_manifest.get("metadata_source_hash") != manifest["metadata_source_hash"]:
                raise MetadataSearchError("METADATA_INVALID", "metadata cache source hash does not match corpus")
            if cache_manifest.get("manifest_hash") != canonical_manifest_hash(manifest):
                raise MetadataSearchError("METADATA_INVALID", "metadata cache manifest hash does not match corpus")
            if cache_manifest.get("embedding_model") != EMBEDDING_MODEL or cache_manifest.get("dimensions") != dimensions:
                raise MetadataSearchError("METADATA_INVALID", "metadata cache embedding configuration does not match corpus")

            index_raw = _read_json(cache_dir / CACHE_INDEX_FILENAME, label="metadata embedding index")
            fragments = index_raw.get("fragments")
            if not isinstance(fragments, list) or not fragments:
                raise MetadataSearchError("METADATA_INVALID", "metadata embedding index has no fragments")
            normalized_fragments: list[dict[str, Any]] = []
            for index, fragment in enumerate(fragments):
                if not isinstance(fragment, dict):
                    raise MetadataSearchError("METADATA_INVALID", f"metadata fragment {index} is not an object")
                required_fragment = {"fragment_id", "database_name", "column_id", "kind", "text", "knowledge_refs"}
                if not required_fragment.issubset(fragment):
                    raise MetadataSearchError("METADATA_INVALID", f"metadata fragment {index} is incomplete")
                db = fragment["database_name"]
                column_id = fragment["column_id"]
                kind = fragment["kind"]
                text = fragment["text"]
                refs = fragment["knowledge_refs"]
                if (
                    not isinstance(db, str)
                    or not isinstance(column_id, str)
                    or column_id not in columns
                    or columns[column_id]["table_id"].split(":", 1)[0] != db
                    or not isinstance(kind, str)
                    or kind not in EMBEDDING_FRAGMENT_FIELDS
                    or not isinstance(text, str)
                    or not text.strip()
                    or not isinstance(refs, list)
                    or any(not isinstance(ref, str) or not ref for ref in refs)
                ):
                    raise MetadataSearchError("METADATA_INVALID", f"invalid metadata fragment {index}")
                normalized_fragments.append({
                    "fragment_id": str(fragment["fragment_id"]),
                    "database_name": db,
                    "column_id": column_id,
                    "kind": kind,
                    "text": text,
                    "knowledge_refs": list(dict.fromkeys(refs)),
                })

            try:
                embeddings = np.load(cache_dir / CACHE_EMBEDDINGS_FILENAME, allow_pickle=False)
                matrix = np.asarray(embeddings["embeddings"], dtype=np.float32)
            except FileNotFoundError as exc:
                raise MetadataSearchError("METADATA_UNAVAILABLE", f"missing metadata embeddings: {cache_dir}") from exc
            except (OSError, ValueError, KeyError) as exc:
                raise MetadataSearchError("METADATA_INVALID", f"cannot load metadata embeddings: {exc}") from exc
            finally:
                try:
                    embeddings.close()  # type: ignore[union-attr]
                except (UnboundLocalError, AttributeError):
                    pass
            if matrix.ndim != 2 or matrix.shape != (len(normalized_fragments), dimensions):
                raise MetadataSearchError(
                    "METADATA_INVALID",
                    f"metadata embeddings must have shape ({len(normalized_fragments)}, {dimensions})",
                )
            if not np.isfinite(matrix).all():
                raise MetadataSearchError("METADATA_INVALID", "metadata embeddings contain non-finite values")

            self._manifest = manifest
            self._columns = columns
            self._fragment_index = normalized_fragments
            self._embeddings = matrix
            self._loaded = True

    def _embed_queries(self, queries: list[str], dimensions: int) -> list[list[float]]:
        try:
            with httpx.Client(timeout=self._config.embedding_timeout, trust_env=False) as client:
                response = client.post(
                    f"{self._config.embedding_service_url.rstrip('/')}/embed",
                    json={"texts": queries},
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise MetadataSearchError("EMBEDDING_UNAVAILABLE", str(exc) or "embedding service unavailable") from exc
        embeddings = data.get("embeddings") if isinstance(data, dict) else None
        if (
            not isinstance(embeddings, list)
            or len(embeddings) != len(queries)
            or any(
                not isinstance(vector, list)
                or len(vector) != dimensions
                or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in vector)
                for vector in embeddings
            )
        ):
            raise MetadataSearchError(
                "EMBEDDING_INVALID",
                f"embedding service must return {len(queries)} vectors of dimension {dimensions}",
            )
        return [[float(value) for value in vector] for vector in embeddings]

    @staticmethod
    def _public_column(column: Mapping[str, Any], score: float, hidden_ids: set[str]) -> dict[str, Any]:
        result = {
            "table_id": column["table_id"],
            "table_name": column["table_name"],
            "column_id": column["column_id"],
            "column_name": column["column_name"],
            "column_type": column["column_type"],
            "description": column["description"],
            "formulas": _public_fragments(column["formulas"], label=f"{column['column_id']}.formulas", hidden_ids=hidden_ids),
            "classification_rules": _public_fragments(column["classification_rules"], label=f"{column['column_id']}.classification_rules", hidden_ids=hidden_ids),
            "table_structure": copy.deepcopy(column["table_structure"]),
            "disambiguation_rules": _public_fragments(column["disambiguation_rules"], label=f"{column['column_id']}.disambiguation_rules", hidden_ids=hidden_ids),
            "known_discrepancies": _public_fragments(column["known_discrepancies"], label=f"{column['column_id']}.known_discrepancies", hidden_ids=hidden_ids),
            "column_identity_references": _public_fragments(column["column_identity_references"], label=f"{column['column_id']}.column_identity_references", hidden_ids=hidden_ids),
            "observed_value_range": column["observed_value_range"],
            "score": float(score),
        }
        return result

    def _search_metadata(self, database_name: str, queries: list[str], top_k: int, hidden_ids: set[str]) -> list[dict[str, Any]]:
        manifest = self._manifest or {}
        dimensions = self._dimensions(manifest)
        candidates = [
            (index, fragment)
            for index, fragment in enumerate(self._fragment_index)
            if fragment["database_name"] == database_name
            and not (set(fragment["knowledge_refs"]) & hidden_ids)
        ]
        if not candidates:
            return []
        query_embeddings = self._embed_queries(queries, dimensions)
        corpus_indices = [index for index, _ in candidates]
        scores = _safe_cosine(query_embeddings, self._embeddings[corpus_indices])
        # Max over queries at fragment level, then max over all fragments for a
        # column.  This makes repeated fragments useful without returning the
        # same column more than once.
        fragment_scores = np.max(scores, axis=0)
        column_scores: dict[str, float] = {}
        for (_, fragment), score in zip(candidates, fragment_scores):
            column_id = fragment["column_id"]
            column_scores[column_id] = max(column_scores.get(column_id, 0.0), float(score))
        ranked = sorted(column_scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]
        return [self._public_column(self._columns[column_id], score, hidden_ids) for column_id, score in ranked]

    def search(
        self,
        request: Any,
        database_name: str,
        hidden_ids: Any = None,
        knowledge_searcher: Callable[..., dict[str, Any]] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        query = _normalize_request(request)
        database_name = str(database_name).strip().casefold()
        if not database_name:
            raise MetadataSearchError("INVALID_REQUEST", "task selected database is required")
        self._load()
        all_hidden = {
            str(item).strip()
            for item in (hidden_ids or [])
            if isinstance(item, str) and item.strip()
        }
        response: dict[str, list[dict[str, Any]]] = {"knowledge": [], "metadata": []}
        if "knowledge" in query["resource_types"]:
            if knowledge_searcher is None:
                raise MetadataSearchError("NEO4J_UNAVAILABLE", "knowledge search backend is not configured")
            knowledge_result = knowledge_searcher(
                {
                    "queries": query["queries"],
                    "top_k": query["top_k"],
                    "resource_types": ["knowledge"],
                },
                database_name,
                all_hidden,
            )
            if not isinstance(knowledge_result, dict):
                raise MetadataSearchError("GRAPH_QUERY_FAILED", "knowledge search returned a non-object response")
            knowledge = knowledge_result.get("knowledge", [])
            if not isinstance(knowledge, list):
                raise MetadataSearchError("GRAPH_QUERY_FAILED", "knowledge search returned invalid results")
            response["knowledge"] = knowledge
        if "metadata" in query["resource_types"]:
            response["metadata"] = self._search_metadata(
                database_name,
                query["queries"],
                query["top_k"],
                all_hidden,
            )
        return response


__all__ = [
    "CACHE_DIRNAME",
    "CACHE_EMBEDDINGS_FILENAME",
    "CACHE_INDEX_FILENAME",
    "CACHE_MANIFEST_FILENAME",
    "COLUMN_FIELDS",
    "DATASET_FIELDS",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_FRAGMENT_FIELDS",
    "EMBEDDING_MODEL",
    "GENERATOR_MODEL",
    "MANIFEST_FILENAME",
    "METADATA_SCHEMA_VERSION",
    "MetadataSearchError",
    "MetadataSearchRepository",
    "canonical_manifest_hash",
    "metadata_cache_dir",
    "metadata_manifest_path",
]
