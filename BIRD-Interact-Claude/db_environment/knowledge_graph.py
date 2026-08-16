"""Read-only kg-v1 Neo4j repositories for schema and Knowledge graph tools."""

from __future__ import annotations

import re
from collections.abc import Mapping
from threading import Lock
from typing import Any

import httpx
from neo4j import READ_ACCESS, GraphDatabase, exceptions as neo4j_exceptions

from shared.config import settings


DEFAULT_INCLUDE = ("columns", "descriptions", "constraints", "direct_joins")
ALLOWED_INCLUDE = frozenset(DEFAULT_INCLUDE)
DEFAULT_HOPS = 5
MAX_HOPS = 10
DEFAULT_PATHS = 5
MAX_PATHS = 20


class GraphSchemaError(Exception):
    """A deliberate domain/tool error with a stable public error code."""

    STATUS_CODES = {
        "INVALID_REQUEST": 400,
        "DATABASE_NOT_FOUND": 404,
        "TABLE_NOT_FOUND": 404,
        "SAME_TABLE_PATH": 409,
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


class Neo4jSchemaRepository:
    """Small synchronous repository; callers run it off the event loop."""

    def __init__(self, config=settings):
        self._config = config
        self._driver = None
        self._driver_lock = Lock()

    def _get_driver(self):
        if self._driver is None:
            with self._driver_lock:
                if self._driver is None:
                    self._driver = GraphDatabase.driver(
                        self._config.neo4j_uri,
                        auth=(self._config.neo4j_user, self._config.neo4j_password),
                    )
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    @staticmethod
    def _normalize(request: Any) -> dict[str, Any]:
        database_name = str(getattr(request, "database_name", "")).strip().casefold()
        from_table = str(getattr(request, "from_table", "")).strip()
        to_table_raw = getattr(request, "to_table", None)
        to_table = str(to_table_raw).strip() if to_table_raw is not None else None
        if not database_name or not from_table or (to_table is not None and not to_table):
            raise GraphSchemaError(
                "INVALID_REQUEST",
                "database_name and from_table are required; to_table cannot be empty",
            )

        raw_include = getattr(request, "include", None)
        include = list(DEFAULT_INCLUDE) if raw_include is None else raw_include
        if not isinstance(include, list) or any(not isinstance(value, str) for value in include):
            raise GraphSchemaError("INVALID_REQUEST", "include must be a list of strings")
        unknown = sorted(set(include) - ALLOWED_INCLUDE)
        if unknown:
            raise GraphSchemaError(
                "INVALID_REQUEST",
                f"include contains unsupported values: {', '.join(unknown)}",
            )
        # Preserve caller order in the tool contract while avoiding duplicate
        # sections in the response assembly.
        include = list(dict.fromkeys(include))

        hops = getattr(request, "hops", DEFAULT_HOPS)
        paths = getattr(request, "paths", DEFAULT_PATHS)
        if isinstance(hops, bool) or not isinstance(hops, int) or not 1 <= hops <= MAX_HOPS:
            raise GraphSchemaError("INVALID_REQUEST", "hops must be an integer from 1 through 10")
        if isinstance(paths, bool) or not isinstance(paths, int) or not 1 <= paths <= MAX_PATHS:
            raise GraphSchemaError("INVALID_REQUEST", "paths must be an integer from 1 through 20")

        return {
            "database_name": database_name,
            "from_table": from_table.casefold(),
            "to_table": to_table.casefold() if to_table is not None else None,
            "include": include,
            "hops": hops,
            "paths": paths,
        }

    def get_table_schema(self, request: Any) -> dict[str, Any]:
        query = self._normalize(request)
        try:
            driver = self._get_driver()
            with driver.session(
                database=self._config.neo4j_database,
                default_access_mode=READ_ACCESS,
            ) as session:
                return session.execute_read(self._read_schema, query)
        except GraphSchemaError:
            raise
        except (
            neo4j_exceptions.ServiceUnavailable,
            neo4j_exceptions.SessionExpired,
            neo4j_exceptions.AuthError,
            neo4j_exceptions.DatabaseNotFound,
    ) as exc:
            raise GraphSchemaError("NEO4J_UNAVAILABLE", str(exc) or "Neo4j is unavailable") from exc
        except neo4j_exceptions.Neo4jError as exc:
            raise GraphSchemaError("GRAPH_QUERY_FAILED", str(exc) or "Neo4j query failed") from exc
        except OSError as exc:
            raise GraphSchemaError("NEO4J_UNAVAILABLE", str(exc) or "Neo4j is unavailable") from exc

    @staticmethod
    def _read_schema(tx, query: dict[str, Any]) -> dict[str, Any]:
        database_name = query["database_name"]
        database = tx.run(
            """
            MATCH (d:Database {id: $database_name})
            RETURN d.id AS id
            LIMIT 1
            """,
            database_name=database_name,
        ).single()
        if database is None:
            raise GraphSchemaError(
                "DATABASE_NOT_FOUND",
                f"database {database_name!r} does not exist in the kg-v1 graph",
            )

        from_table = Neo4jSchemaRepository._resolve_table(tx, database_name, query["from_table"], "from_table")
        to_table = None
        if query["to_table"] is not None:
            to_table = Neo4jSchemaRepository._resolve_table(tx, database_name, query["to_table"], "to_table")
            if to_table["id"] == from_table["id"]:
                raise GraphSchemaError(
                    "SAME_TABLE_PATH",
                    "to_table must be different from from_table when a join path is requested",
                )

        include = set(query["include"])
        include_columns = "columns" in include or "descriptions" in include
        tables = [
            Neo4jSchemaRepository._table_payload(
                tx,
                from_table,
                "from",
                include_columns=include_columns,
                include_descriptions="descriptions" in include,
                include_constraints="constraints" in include,
                include_direct_joins="direct_joins" in include,
            )
        ]
        if to_table is not None:
            tables.append(
                Neo4jSchemaRepository._table_payload(
                    tx,
                    to_table,
                    "to",
                    include_columns=include_columns,
                    include_descriptions="descriptions" in include,
                    include_constraints="constraints" in include,
                    include_direct_joins="direct_joins" in include,
                )
            )

        warnings = []
        if "descriptions" in include:
            for table in tables:
                for column in table.get("columns", []):
                    if column.get("raw_text") is None:
                        warnings.append({
                            "code": "DESCRIPTION_MISSING",
                            "scope": "column",
                            "table": table["table_name"],
                            "column": column["column_name"],
                            "message": "No source column-meaning entry matched this column.",
                        })

        response = {"tables": tables}
        if to_table is not None:
            paths = Neo4jSchemaRepository._join_paths(
                tx,
                from_table,
                to_table,
                query["hops"],
                query["paths"],
            )
            response["join_paths"] = paths
            if not paths:
                warnings.append({
                    "code": "NO_JOIN_PATH",
                    "scope": "path",
                    "from_table": from_table["table_name"],
                    "to_table": to_table["table_name"],
                    "message": "No declared foreign-key path was found within hops.",
                })
        response["warnings"] = warnings
        return response

    @staticmethod
    def _resolve_table(tx, database_name: str, table_name: str, role: str) -> dict[str, str]:
        record = tx.run(
            """
            MATCH (d:Database {id: $database_name})-[:HAS_TABLE]->(t:Table)
            WHERE t.table_name = $table_name
            RETURN t.id AS id, t.table_name AS table_name
            LIMIT 1
            """,
            database_name=database_name,
            table_name=table_name,
        ).single()
        if record is None:
            raise GraphSchemaError(
                "TABLE_NOT_FOUND",
                f"{role} {table_name!r} does not exist in database {database_name!r}",
            )
        return {"id": record["id"], "table_name": record["table_name"]}

    @staticmethod
    def _table_payload(
        tx,
        table: dict[str, str],
        role: str,
        *,
        include_columns: bool,
        include_descriptions: bool,
        include_constraints: bool,
        include_direct_joins: bool,
    ) -> dict[str, Any]:
        table_record = tx.run(
            "MATCH (t:Table {id: $id}) RETURN t {.*} AS table",
            id=table["id"],
        ).single()
        if table_record is None:
            raise GraphSchemaError("GRAPH_QUERY_FAILED", f"resolved table {table['id']!r} disappeared")
        table_properties = dict(table_record["table"])
        column_records = list(tx.run(
            """
            MATCH (t:Table {id: $id})-[:HAS_COLUMN]->(c:Column)
            RETURN c {.*} AS column
            ORDER BY c.ordinal
            """,
            id=table["id"],
        ))
        columns = [dict(record["column"]) for record in column_records]

        payload: dict[str, Any] = {"role": role, "table_name": table_properties["table_name"]}
        if include_descriptions:
            payload["description"] = table_properties.get("description", "")
        if include_columns:
            payload["columns"] = [Neo4jSchemaRepository._column_payload(column, include_descriptions) for column in columns]
        if include_constraints:
            foreign_key_records = list(tx.run(
                """
                MATCH (t:Table {id: $id})-[:HAS_FOREIGN_KEY]->(fk:ForeignKey)
                OPTIONAL MATCH (fk)-[:REFERENCES_TABLE]->(target:Table)
                RETURN fk {.*} AS foreign_key, target.table_name AS to_table
                ORDER BY fk.fk_key
                """,
                id=table["id"],
            ))
            primary_key = [column["column_name"] for column in columns if column.get("is_primary_key")]
            nullable_columns = [column["column_name"] for column in columns if column.get("nullable")]
            payload["constraints"] = {
                "primary_key": primary_key,
                "nullable_columns": nullable_columns,
                "foreign_keys": [
                    Neo4jSchemaRepository._foreign_key_payload(record)
                    for record in foreign_key_records
                ],
            }
            for record, foreign_key in zip(foreign_key_records, payload["constraints"]["foreign_keys"]):
                foreign_key["to_table"] = record["to_table"]
        if include_direct_joins:
            join_records = list(tx.run(
                """
                MATCH (t:Table {id: $id})-[j:JOINS_TO]-(other:Table)
                RETURN properties(j) AS join,
                       other.table_name AS other_table,
                       startNode(j).id AS start_id,
                       endNode(j).id AS end_id
                ORDER BY j.fk_key
                """,
                id=table["id"],
            ))
            payload["direct_joins"] = [
                Neo4jSchemaRepository._direct_join_payload(record, table)
                for record in join_records
            ]
        return payload

    @staticmethod
    def _column_payload(column: dict[str, Any], include_descriptions: bool) -> dict[str, Any]:
        payload = {
            "column_name": column["column_name"],
            "ordinal": column["ordinal"],
            "column_type": column["column_type"],
            "nullable": column["nullable"],
            "default_expression": column.get("default_expression"),
            "is_primary_key": column["is_primary_key"],
        }
        if include_descriptions:
            payload.update({
                "raw_text": column.get("raw_text"),
                "description": column.get("description"),
                "declared_type": column.get("declared_type"),
                "examples": column.get("examples", []),
            })
        return payload

    @staticmethod
    def _foreign_key_payload(record) -> dict[str, Any]:
        foreign_key = dict(record["foreign_key"])
        return {
            "fk_key": foreign_key["fk_key"],
            "from_columns": list(foreign_key.get("from_columns", [])),
            "to_columns": list(foreign_key.get("to_columns", [])),
            "cardinality": foreign_key["cardinality"],
            "nullable": foreign_key["nullable"],
            # The public contract retains this response key while the graph
            # node itself uses canonical property ``id``.
            "entity_key": foreign_key.get("id", foreign_key.get("entity_key")),
        }

    @staticmethod
    def _direct_join_payload(record, table: dict[str, str]) -> dict[str, Any]:
        join = dict(record["join"])
        start_key = record["start_id"]
        end_key = record["end_id"]
        current_is_start = table["id"] == start_key
        return {
            "target_table": record["other_table"],
            "from_table": start_key.split(":", 1)[1],
            "to_table": end_key.split(":", 1)[1],
            "from_columns": list(join.get("from_columns", [])),
            "to_columns": list(join.get("to_columns", [])),
            "join_condition": join["join_condition"],
            "cardinality": join["cardinality"],
            "direction": "outgoing" if current_is_start else "incoming",
            "fk_key": join["fk_key"],
        }

    @staticmethod
    def _join_paths(tx, from_table: dict[str, str], to_table: dict[str, str], max_hops: int, max_paths: int) -> list[dict[str, Any]]:
        # Neo4j does not accept a parameter in a variable-length bound. The
        # bound is validated as an integer before it is interpolated here.
        query = f"""
            MATCH (from:Table {{id: $from_key}}), (to:Table {{id: $to_key}})
            MATCH p=allShortestPaths((from)-[:JOINS_TO*..{max_hops}]-(to))
            RETURN [node IN nodes(p) | node.table_name] AS table_names,
                   [node IN nodes(p) | node.id] AS node_keys,
                   [edge IN relationships(p) | properties(edge)] AS joins,
                   [edge IN relationships(p) | startNode(edge).id] AS start_keys,
                   [edge IN relationships(p) | endNode(edge).id] AS end_keys
            LIMIT $max_paths
        """
        records = list(tx.run(
            query,
            from_key=from_table["id"],
            to_key=to_table["id"],
            max_paths=max_paths,
        ))
        paths = []
        for record in records:
            table_names = list(record["table_names"])
            node_keys = list(record["node_keys"])
            joins = [dict(join) for join in record["joins"]]
            start_keys = list(record["start_keys"])
            end_keys = list(record["end_keys"])
            hops = []
            for index, join in enumerate(joins):
                forward = node_keys[index] == start_keys[index]
                hops.append({
                    "from_table": table_names[index],
                    "to_table": table_names[index + 1],
                    "from_columns": list(join.get("from_columns", [])) if forward else list(join.get("to_columns", [])),
                    "to_columns": list(join.get("to_columns", [])) if forward else list(join.get("from_columns", [])),
                    "join_condition": join["join_condition"],
                    "cardinality": join["cardinality"],
                    "direction": "outgoing" if forward else "incoming",
                    "fk_key": join["fk_key"],
                })
            paths.append({"tables": table_names, "hops": hops})
        paths.sort(key=lambda path: (tuple(path["tables"]), tuple(hop["fk_key"] for hop in path["hops"])))
        return paths[:max_paths]


KNOWLEDGE_ID_RE = re.compile(r"^(?P<database>[a-z][a-z0-9_]*):(?P<source_id>[0-9]+)$")
KNOWLEDGE_INCLUDE = ("description", "definition", "provenance", "related_columns")
KNOWLEDGE_INCLUDE_SET = frozenset(KNOWLEDGE_INCLUDE)
MAX_KNOWLEDGE_DEPTH = 5
MAX_KNOWLEDGE_NODES = 50


class KnowledgeGraphError(GraphSchemaError):
    """Stable errors exposed by the global-ID knowledge graph API."""

    STATUS_CODES = {
        "INVALID_REQUEST": 400,
        "KNOWLEDGE_NOT_FOUND": 404,
        "NEO4J_UNAVAILABLE": 503,
        "GRAPH_QUERY_FAILED": 500,
    }


class Neo4jKnowledgeRepository:
    """Read-only repository for the semantic ``Knowledge`` dependency graph."""

    def __init__(self, config=settings):
        self._config = config
        self._driver = None
        self._driver_lock = Lock()

    def _get_driver(self):
        if self._driver is None:
            with self._driver_lock:
                if self._driver is None:
                    self._driver = GraphDatabase.driver(
                        self._config.neo4j_uri,
                        auth=(self._config.neo4j_user, self._config.neo4j_password),
                    )
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    @staticmethod
    def _request_value(request: Any, key: str, default: Any = None) -> Any:
        if isinstance(request, Mapping):
            return request.get(key, default)
        return getattr(request, key, default)

    @classmethod
    def _normalize(cls, request: Any) -> dict[str, Any]:
        raw_id = cls._request_value(request, "knowledge_id")
        if not isinstance(raw_id, str):
            raise KnowledgeGraphError(
                "INVALID_REQUEST",
                "knowledge_id must match <database>:<non-negative integer>",
            )
        match = KNOWLEDGE_ID_RE.fullmatch(raw_id)
        if match is None:
            raise KnowledgeGraphError(
                "INVALID_REQUEST",
                "knowledge_id must match <database>:<non-negative integer>",
            )

        database_name = match.group("database")
        source_id = int(match.group("source_id"))
        knowledge_id = f"{database_name}:{source_id}"

        raw_include = cls._request_value(request, "include")
        include = [] if raw_include is None else raw_include
        if not isinstance(include, list) or any(not isinstance(value, str) for value in include):
            raise KnowledgeGraphError("INVALID_REQUEST", "include must be a list of strings")
        unknown = sorted(set(include) - KNOWLEDGE_INCLUDE_SET)
        if unknown:
            raise KnowledgeGraphError(
                "INVALID_REQUEST",
                f"include contains unsupported values: {', '.join(unknown)}",
            )
        include = list(dict.fromkeys(include))

        raw_expand = cls._request_value(request, "expand")
        expand = None
        if raw_expand is not None:
            if isinstance(raw_expand, Mapping):
                expand_values = dict(raw_expand)
            elif hasattr(raw_expand, "model_dump"):
                expand_values = raw_expand.model_dump(exclude_unset=False)
            else:
                expand_values = {
                    key: getattr(raw_expand, key)
                    for key in ("depth", "nodes")
                    if hasattr(raw_expand, key)
                }
            unknown_expand = sorted(set(expand_values) - {"depth", "nodes"})
            if unknown_expand or "depth" not in expand_values or "nodes" not in expand_values:
                raise KnowledgeGraphError(
                    "INVALID_REQUEST",
                    "expand must contain exactly depth and nodes",
                )
            depth = expand_values["depth"]
            max_nodes = expand_values["nodes"]
            if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= MAX_KNOWLEDGE_DEPTH:
                raise KnowledgeGraphError(
                    "INVALID_REQUEST",
                    "expand.depth must be an integer from 0 through 5",
                )
            if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or not 1 <= max_nodes <= MAX_KNOWLEDGE_NODES:
                raise KnowledgeGraphError(
                    "INVALID_REQUEST",
                    "expand.nodes must be an integer from 1 through 50",
                )
            expand = {"depth": depth, "max_nodes": max_nodes}

        return {
            "database_name": database_name,
            "root_id": knowledge_id,
            "include": include,
            "expand": expand,
        }

    @staticmethod
    def _normalize_hidden_ids(database_name: str, hidden_ids: Any) -> list[str]:
        if hidden_ids is None:
            return []
        if isinstance(hidden_ids, (str, bytes)) or not hasattr(hidden_ids, "__iter__"):
            hidden_ids = [hidden_ids]
        normalized = set()
        for hidden_id in hidden_ids:
            if isinstance(hidden_id, bool):
                continue
            if isinstance(hidden_id, int) and hidden_id >= 0:
                normalized.add(f"{database_name}:{hidden_id}")
            elif isinstance(hidden_id, str):
                match = KNOWLEDGE_ID_RE.fullmatch(hidden_id)
                if match is not None:
                    normalized.add(f"{match.group('database')}:{int(match.group('source_id'))}")
        return sorted(normalized)

    def get_knowledge(
        self,
        request: Any,
        hidden_ids: Any = None,
        task_database_name: str | None = None,
    ) -> dict[str, Any]:
        query = self._normalize(request)
        if task_database_name is not None:
            selected_database = str(task_database_name).strip().casefold()
            if query["database_name"] != selected_database:
                raise KnowledgeGraphError(
                    "KNOWLEDGE_NOT_FOUND",
                    f"knowledge {query['root_id']!r} does not exist or is not visible",
                )
        request_hidden_ids = self._request_value(request, "hidden_ids")
        if hidden_ids is None:
            hidden_ids = request_hidden_ids
        query["hidden_ids"] = self._normalize_hidden_ids(query["database_name"], hidden_ids)
        try:
            driver = self._get_driver()
            with driver.session(
                database=self._config.neo4j_database,
                default_access_mode=READ_ACCESS,
            ) as session:
                return session.execute_read(self._read_knowledge, query)
        except GraphSchemaError:
            raise
        except (
            neo4j_exceptions.ServiceUnavailable,
            neo4j_exceptions.SessionExpired,
            neo4j_exceptions.AuthError,
            neo4j_exceptions.DatabaseNotFound,
        ) as exc:
            raise KnowledgeGraphError("NEO4J_UNAVAILABLE", str(exc) or "Neo4j is unavailable") from exc
        except neo4j_exceptions.Neo4jError as exc:
            raise KnowledgeGraphError("GRAPH_QUERY_FAILED", str(exc) or "Neo4j query failed") from exc
        except OSError as exc:
            raise KnowledgeGraphError("NEO4J_UNAVAILABLE", str(exc) or "Neo4j is unavailable") from exc

    @staticmethod
    def _node_from_record(record: Any) -> dict[str, Any]:
        raw_node = record["node"]
        if isinstance(raw_node, Mapping):
            return dict(raw_node)
        try:
            return dict(raw_node)
        except (TypeError, ValueError):
            raise KnowledgeGraphError("GRAPH_QUERY_FAILED", "Neo4j returned an invalid Knowledge node")

    @staticmethod
    def _node_payload(node: dict[str, Any], distance: int, include: set[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "knowledge_id": node.get("id"),
            "name": node.get("name"),
            "type": node.get("type"),
            "distance": distance,
        }
        if "description" in include:
            payload["description"] = node.get("description")
        if "definition" in include:
            payload["definition"] = node.get("definition")
        if "provenance" in include:
            payload["provenance"] = {
                "source_file": node.get("source_file"),
                "source_line": node.get("source_line"),
                "source_id": node.get("source_id"),
            }
        if "related_columns" in include:
            payload["related_columns"] = list(node.get("related_columns", []))
        return payload

    @staticmethod
    def _read_root(tx, query: dict[str, Any]) -> dict[str, Any]:
        record = tx.run(
            """
            MATCH (d:Database {id: $database_name})-[:HAS_KNOWLEDGE]->(k:Knowledge)
            WHERE k.id = $root_id AND NOT k.id IN $hidden_ids
            RETURN k { .id, .name, .type, .description, .definition,
                       .source_file, .source_line, .source_id } AS node
            LIMIT 1
            """,
            database_name=query["database_name"],
            root_id=query["root_id"],
            hidden_ids=query["hidden_ids"],
        ).single()
        if record is None:
            raise KnowledgeGraphError(
                "KNOWLEDGE_NOT_FOUND",
                f"knowledge {query['root_id']!r} does not exist or is not visible",
            )
        return Neo4jKnowledgeRepository._node_from_record(record)

    @staticmethod
    def _read_children(tx, query: dict[str, Any], parent_ids: list[str]) -> list[Any]:
        if not parent_ids:
            return []
        result = tx.run(
            """
            UNWIND range(0, size($parent_ids) - 1) AS parent_index
            MATCH (d:Database {id: $database_name})-[:HAS_KNOWLEDGE]->(parent:Knowledge)
            WHERE parent.id = $parent_ids[parent_index]
              AND NOT parent.id IN $hidden_ids
            MATCH (d)-[:HAS_KNOWLEDGE]->(child:Knowledge)
            MATCH (parent)-[r:DEPENDS_ON]->(child)
            WHERE child.id STARTS WITH $database_prefix
              AND NOT child.id IN $hidden_ids
            RETURN parent.id AS parent_id,
                   r.position AS position,
                   child { .id, .name, .type, .description, .definition,
                           .source_file, .source_line, .source_id } AS node
            ORDER BY position, parent_index, child.id
            """,
            database_name=query["database_name"],
            database_prefix=query["database_name"] + ":",
            parent_ids=parent_ids,
            hidden_ids=query["hidden_ids"],
        )
        return list(result)

    @staticmethod
    def _read_related_columns(
        tx,
        knowledge_ids: list[str],
        database_name: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        if not knowledge_ids:
            return {}
        records = tx.run(
            """
            UNWIND $knowledge_ids AS knowledge_id
            MATCH (k:Knowledge {id: knowledge_id})-[:REFERS_TO_COLUMN]->(c:Column)
            WHERE $database_prefix IS NULL OR c.id STARTS WITH $database_prefix
            RETURN k.id AS knowledge_id,
                   c.id AS column_id,
                   c.table_name AS table_name,
                   c.column_name AS column_name
            ORDER BY knowledge_id, table_name, column_name, column_id
            """,
            knowledge_ids=knowledge_ids,
            database_prefix=(str(database_name).strip().casefold() + ":") if database_name else None,
        )
        related: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            related.setdefault(record["knowledge_id"], []).append({
                "column_id": record["column_id"],
                "table_name": record["table_name"],
                "column_name": record["column_name"],
            })
        return related

    @staticmethod
    def _read_knowledge(tx, query: dict[str, Any]) -> dict[str, Any]:
        include = set(query["include"])
        root = Neo4jKnowledgeRepository._read_root(tx, query)
        root_id = query["root_id"]
        nodes: dict[str, dict[str, Any]] = {root_id: root}
        distances = {root_id: 0}
        ordered_ids = [root_id]
        frontier = [root_id]
        edges: list[dict[str, Any]] = []
        truncated = False
        expand = query["expand"]

        if expand is not None:
            depth_limit = expand["depth"]
            max_nodes = expand["max_nodes"]
            for distance in range(depth_limit):
                if not frontier:
                    break
                records = Neo4jKnowledgeRepository._read_children(tx, query, frontier)
                parent_order = {parent_id: index for index, parent_id in enumerate(frontier)}
                records.sort(
                    key=lambda record: (
                        record["position"],
                        parent_order.get(record["parent_id"], len(frontier)),
                        str(record["node"].get("id")),
                    )
                )
                next_frontier: list[str] = []
                for record in records:
                    child = Neo4jKnowledgeRepository._node_from_record(record)
                    child_id = child.get("id")
                    parent_id = record["parent_id"]
                    position = record["position"]
                    edges.append({
                        "from_knowledge_id": parent_id,
                        "to_knowledge_id": child_id,
                        "type": "DEPENDS_ON",
                        "position": position,
                    })
                    if child_id in nodes:
                        continue
                    if len(nodes) >= max_nodes:
                        truncated = True
                        continue
                    nodes[child_id] = child
                    distances[child_id] = distance + 1
                    ordered_ids.append(child_id)
                    next_frontier.append(child_id)
                frontier = next_frontier

        visible_ids = set(nodes)
        response_edges = [
            edge for edge in edges
            if edge["from_knowledge_id"] in visible_ids and edge["to_knowledge_id"] in visible_ids
        ]
        if "related_columns" in include:
            related = Neo4jKnowledgeRepository._read_related_columns(tx, ordered_ids, query["database_name"])
            for node_id in ordered_ids:
                nodes[node_id]["related_columns"] = related.get(node_id, [])
        return {
            "knowledge_id": root_id,
            "nodes": [
                Neo4jKnowledgeRepository._node_payload(nodes[node_id], distances[node_id], include)
                for node_id in ordered_ids
            ],
            "edges": response_edges,
            "truncated": truncated,
            "warnings": [],
        }


SEARCH_RESOURCE_TYPES = ("knowledge", "table_schema")
SEARCH_RESOURCE_SET = frozenset(SEARCH_RESOURCE_TYPES)
SEARCH_INDEX_CANDIDATES = 100
SEARCH_EMBEDDING_DIMENSIONS = 1024
QWEN_QUERY_INSTRUCTION = (
    "<Instruct>: Retrieve relevant knowledge definitions or table columns for "
    "resolving an ambiguous SQL request.\n"
    "<Query>: {query}"
)


class SemanticSearchRepository:
    """Vector/full-text semantic search over task-visible graph resources."""

    def __init__(self, config=settings):
        self._config = config
        self._driver = None
        self._driver_lock = Lock()

    def _get_driver(self):
        if self._driver is None:
            with self._driver_lock:
                if self._driver is None:
                    self._driver = GraphDatabase.driver(
                        self._config.neo4j_uri,
                        auth=(self._config.neo4j_user, self._config.neo4j_password),
                    )
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    @staticmethod
    def _request_value(request: Any, key: str, default: Any = None) -> Any:
        if isinstance(request, Mapping):
            return request.get(key, default)
        return getattr(request, key, default)

    @classmethod
    def _normalize(cls, request: Any) -> dict[str, Any]:
        raw_queries = cls._request_value(request, "queries")
        if (
            not isinstance(raw_queries, list)
            or not 1 <= len(raw_queries) <= 8
            or any(not isinstance(query, str) or not query.strip() for query in raw_queries)
        ):
            raise GraphSchemaError("INVALID_REQUEST", "queries must contain 1 through 8 non-empty strings")
        queries = [query.strip() for query in raw_queries]

        top_k = cls._request_value(request, "top_k")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
            raise GraphSchemaError("INVALID_REQUEST", "top_k must be an integer from 1 through 20")

        raw_resource_types = cls._request_value(request, "resource_types")
        if (
            not isinstance(raw_resource_types, list)
            or not raw_resource_types
            or any(not isinstance(value, str) for value in raw_resource_types)
        ):
            raise GraphSchemaError("INVALID_REQUEST", "resource_types must contain at least one string")
        unknown = sorted(set(raw_resource_types) - SEARCH_RESOURCE_SET)
        if unknown:
            raise GraphSchemaError(
                "INVALID_REQUEST",
                f"resource_types contains unsupported values: {', '.join(unknown)}",
            )
        resource_types = list(dict.fromkeys(raw_resource_types))
        return {"queries": queries, "top_k": top_k, "resource_types": resource_types}

    def search(
        self,
        request: Any,
        database_name: str,
        hidden_ids: Any = None,
    ) -> dict[str, Any]:
        query = self._normalize(request)
        database_name = str(database_name).strip().casefold()
        if not database_name:
            raise GraphSchemaError("INVALID_REQUEST", "task selected database is required")
        hidden = Neo4jKnowledgeRepository._normalize_hidden_ids(database_name, hidden_ids)
        try:
            embeddings = self._embed_queries(query["queries"])
            driver = self._get_driver()
            with driver.session(
                database=self._config.neo4j_database,
                default_access_mode=READ_ACCESS,
            ) as session:
                return session.execute_read(
                    self._read_search,
                    {
                        **query,
                        "database_name": database_name,
                        "hidden_ids": hidden,
                        "embeddings": embeddings,
                    },
                )
        except GraphSchemaError:
            raise
        except (
            neo4j_exceptions.ServiceUnavailable,
            neo4j_exceptions.SessionExpired,
            neo4j_exceptions.AuthError,
            neo4j_exceptions.DatabaseNotFound,
        ) as exc:
            raise GraphSchemaError("NEO4J_UNAVAILABLE", str(exc) or "Neo4j is unavailable") from exc
        except neo4j_exceptions.Neo4jError as exc:
            raise GraphSchemaError("GRAPH_QUERY_FAILED", str(exc) or "Neo4j query failed") from exc
        except OSError as exc:
            raise GraphSchemaError("NEO4J_UNAVAILABLE", str(exc) or "Neo4j is unavailable") from exc

    def _embed_queries(self, queries: list[str]) -> list[list[float]]:
        payload = {
            "texts": [QWEN_QUERY_INSTRUCTION.format(query=query) for query in queries],
        }
        try:
            with httpx.Client(
                timeout=self._config.embedding_timeout,
                trust_env=False,
            ) as client:
                response = client.post(
                    f"{self._config.embedding_service_url.rstrip('/')}/embed",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise GraphSchemaError("EMBEDDING_UNAVAILABLE", str(exc) or "embedding service unavailable") from exc

        embeddings = data.get("embeddings") if isinstance(data, dict) else None
        if (
            not isinstance(embeddings, list)
            or len(embeddings) != len(queries)
            or any(
                not isinstance(vector, list)
                or len(vector) != SEARCH_EMBEDDING_DIMENSIONS
                or any(not isinstance(value, (int, float)) for value in vector)
                for vector in embeddings
            )
        ):
            raise GraphSchemaError(
                "EMBEDDING_INVALID",
                f"embedding service must return {len(queries)} vectors of dimension {SEARCH_EMBEDDING_DIMENSIONS}",
            )
        return [[float(value) for value in vector] for vector in embeddings]

    @staticmethod
    def _safe_vector_score(score: Any) -> float:
        try:
            value = float(score)
        except (TypeError, ValueError):
            return 0.0
        # Neo4j cosine scores are normally already in [0, 1]; clamp the
        # contract boundary so backend version differences cannot leak scores.
        return max(0.0, min(1.0, value))

    @staticmethod
    def _normalize_fulltext_scores(scores: dict[str, float]) -> dict[str, float]:
        if not scores:
            return {}
        values = list(scores.values())
        minimum = min(values)
        maximum = max(values)
        if maximum == minimum:
            return {key: 1.0 for key in scores}
        return {
            key: max(0.0, min(1.0, (value - minimum) / (maximum - minimum)))
            for key, value in scores.items()
        }

    @staticmethod
    def _fulltext_query(query: str) -> str:
        # Keep exact names, abbreviations and formula identifiers while
        # avoiding Lucene punctuation interpreted as query syntax.
        tokens = re.findall(r"[A-Za-z0-9_]+", query)
        return " ".join(tokens) or query

    @staticmethod
    def _node_from_record(record: Any) -> dict[str, Any]:
        raw = record["node"]
        return dict(raw) if isinstance(raw, Mapping) else dict(raw)

    @staticmethod
    def _resource_label(resource_type: str) -> str:
        return "Knowledge" if resource_type == "knowledge" else "Column"

    @staticmethod
    def _read_database(tx, database_name: str) -> None:
        if tx.run("MATCH (d:Database {id: $database_name}) RETURN d.id LIMIT 1", database_name=database_name).single() is None:
            raise GraphSchemaError(
                "DATABASE_NOT_FOUND",
                f"database {database_name!r} does not exist in the semantic graph",
            )

    @classmethod
    def _read_candidates(
        cls,
        tx,
        query: dict[str, Any],
        resource_type: str,
        embedding: list[float],
        text_query: str,
    ) -> dict[str, dict[str, Any]]:
        label = cls._resource_label(resource_type)
        prefix = query["database_name"] + ":"
        vector_records = tx.run(
            """
            CALL db.index.vector.queryNodes($index_name, $candidate_limit, $embedding)
            YIELD node, score
            WHERE $label IN labels(node)
              AND node.id STARTS WITH $database_prefix
              AND NOT node.id IN $hidden_ids
            RETURN node {.*} AS node, score
            """,
            index_name="semantic_search_embedding",
            candidate_limit=SEARCH_INDEX_CANDIDATES,
            embedding=embedding,
            label=label,
            database_prefix=prefix,
            hidden_ids=query["hidden_ids"] if resource_type == "knowledge" else [],
        )
        vector_hits = {}
        for record in vector_records:
            node = cls._node_from_record(record)
            vector_hits[node["id"]] = {"node": node, "score": cls._safe_vector_score(record["score"])}

        fulltext_records = tx.run(
            """
            CALL db.index.fulltext.queryNodes($index_name, $text_query)
            YIELD node, score
            WHERE $label IN labels(node)
              AND node.id STARTS WITH $database_prefix
              AND NOT node.id IN $hidden_ids
            RETURN node {.*} AS node, score
            """,
            index_name="semantic_search_text",
            text_query=text_query,
            label=label,
            database_prefix=prefix,
            hidden_ids=query["hidden_ids"] if resource_type == "knowledge" else [],
        )
        raw_fulltext = {}
        fulltext_nodes = {}
        for record in fulltext_records:
            node = cls._node_from_record(record)
            key = node["id"]
            raw_fulltext[key] = float(record["score"])
            fulltext_nodes[key] = node
        fulltext_hits = cls._normalize_fulltext_scores(raw_fulltext)

        candidates: dict[str, dict[str, Any]] = {}
        for node_id in set(vector_hits) | set(fulltext_hits):
            vector_hit = vector_hits.get(node_id)
            node = vector_hit["node"] if vector_hit else fulltext_nodes[node_id]
            candidates[node_id] = {
                "node": node,
                "score": max(
                    vector_hit["score"] if vector_hit else 0.0,
                    fulltext_hits.get(node_id, 0.0),
                ),
            }
        return candidates

    @staticmethod
    def _related_columns(
        tx,
        knowledge_ids: list[str],
        database_name: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        if not knowledge_ids:
            return {}
        records = tx.run(
            """
            UNWIND $knowledge_ids AS knowledge_id
            MATCH (k:Knowledge {id: knowledge_id})-[:REFERS_TO_COLUMN]->(c:Column)
            WHERE $database_prefix IS NULL OR c.id STARTS WITH $database_prefix
            RETURN k.id AS knowledge_id, c.id AS column_id,
                   c.table_name AS table_name, c.column_name AS column_name
            ORDER BY knowledge_id, table_name, column_name, column_id
            """,
            knowledge_ids=knowledge_ids,
            database_prefix=(str(database_name).strip().casefold() + ":") if database_name else None,
        )
        result: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            result.setdefault(record["knowledge_id"], []).append({
                "column_id": record["column_id"],
                "table_name": record["table_name"],
                "column_name": record["column_name"],
            })
        return result

    @classmethod
    def _read_search(cls, tx, query: dict[str, Any]) -> dict[str, Any]:
        cls._read_database(tx, query["database_name"])
        merged: dict[str, dict[str, dict[str, Any]]] = {
            resource_type: {} for resource_type in query["resource_types"]
        }
        for embedding, raw_query in zip(query["embeddings"], query["queries"]):
            fulltext_query = cls._fulltext_query(raw_query)
            for resource_type in query["resource_types"]:
                candidates = cls._read_candidates(
                    tx,
                    query,
                    resource_type,
                    embedding,
                    fulltext_query,
                )
                target = merged[resource_type]
                for node_id, candidate in candidates.items():
                    current = target.get(node_id)
                    if current is None or candidate["score"] > current["score"]:
                        target[node_id] = candidate

        related = cls._related_columns(
            tx,
            list(merged.get("knowledge", {}).keys()),
            query["database_name"],
        )
        response: dict[str, Any] = {"knowledge": [], "table_schema": []}
        for resource_type in query["resource_types"]:
            hits = sorted(
                merged[resource_type].values(),
                key=lambda hit: (-hit["score"], str(hit["node"].get("id"))),
            )[: query["top_k"]]
            if resource_type == "knowledge":
                response["knowledge"] = [
                    {
                        "knowledge_id": hit["node"]["id"],
                        "database_name": query["database_name"],
                        "name": hit["node"].get("name"),
                        "type": hit["node"].get("type"),
                        "score": hit["score"],
                        "related_columns": related.get(hit["node"]["id"], []),
                    }
                    for hit in hits
                ]
            else:
                response["table_schema"] = [
                    {
                        "table_id": f"{query['database_name']}:{hit['node'].get('table_name')}",
                        "table_name": hit["node"].get("table_name"),
                        "column_id": hit["node"]["id"],
                        "column_name": hit["node"].get("column_name"),
                        "column_type": hit["node"].get("column_type"),
                        "description": hit["node"].get("description"),
                        "score": hit["score"],
                    }
                    for hit in hits
                ]
        return response
