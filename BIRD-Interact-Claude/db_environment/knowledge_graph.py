"""Read-only kg-v1 Neo4j repositories for schema and Knowledge graph tools."""

from __future__ import annotations

import re
from collections.abc import Mapping
from threading import Lock
from typing import Any

from neo4j import READ_ACCESS, GraphDatabase, exceptions as neo4j_exceptions

from shared.config import settings


DEFAULT_INCLUDE = ("columns", "descriptions", "constraints", "direct_joins")
ALLOWED_INCLUDE = frozenset(DEFAULT_INCLUDE)
DEFAULT_MAX_HOPS = 5
MAX_MAX_HOPS = 10
DEFAULT_MAX_PATHS = 5
MAX_MAX_PATHS = 20


class GraphSchemaError(Exception):
    """A deliberate domain/tool error with a stable public error code."""

    STATUS_CODES = {
        "INVALID_REQUEST": 400,
        "DATABASE_NOT_FOUND": 404,
        "TABLE_NOT_FOUND": 404,
        "SAME_TABLE_PATH": 409,
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
        database_name = str(getattr(request, "database_name", "")).strip()
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

        max_hops = getattr(request, "max_hops", DEFAULT_MAX_HOPS)
        max_paths = getattr(request, "max_paths", DEFAULT_MAX_PATHS)
        if isinstance(max_hops, bool) or not isinstance(max_hops, int) or not 1 <= max_hops <= MAX_MAX_HOPS:
            raise GraphSchemaError("INVALID_REQUEST", "max_hops must be an integer from 1 through 10")
        if isinstance(max_paths, bool) or not isinstance(max_paths, int) or not 1 <= max_paths <= MAX_MAX_PATHS:
            raise GraphSchemaError("INVALID_REQUEST", "max_paths must be an integer from 1 through 20")

        return {
            "database_name": database_name,
            "from_table": from_table.casefold(),
            "to_table": to_table.casefold() if to_table is not None else None,
            "include": include,
            "max_hops": max_hops,
            "max_paths": max_paths,
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
            MATCH (d:Database {database_name: $database_name})
            RETURN d.entity_key AS entity_key
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
            if to_table["entity_key"] == from_table["entity_key"]:
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
                query["max_hops"],
                query["max_paths"],
            )
            response["join_paths"] = paths
            if not paths:
                warnings.append({
                    "code": "NO_JOIN_PATH",
                    "scope": "path",
                    "from_table": from_table["table_name"],
                    "to_table": to_table["table_name"],
                    "message": "No declared foreign-key path was found within max_hops.",
                })
        response["warnings"] = warnings
        return response

    @staticmethod
    def _resolve_table(tx, database_name: str, table_name: str, role: str) -> dict[str, str]:
        record = tx.run(
            """
            MATCH (d:Database {database_name: $database_name})-[:HAS_TABLE]->(t:Table)
            WHERE t.table_name = $table_name
            RETURN t.entity_key AS entity_key, t.table_name AS table_name
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
        return {"entity_key": record["entity_key"], "table_name": record["table_name"]}

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
            "MATCH (t:Table {entity_key: $entity_key}) RETURN t {.*} AS table",
            entity_key=table["entity_key"],
        ).single()
        if table_record is None:
            raise GraphSchemaError("GRAPH_QUERY_FAILED", f"resolved table {table['entity_key']!r} disappeared")
        table_properties = dict(table_record["table"])
        column_records = list(tx.run(
            """
            MATCH (t:Table {entity_key: $entity_key})-[:HAS_COLUMN]->(c:Column)
            RETURN c {.*} AS column
            ORDER BY c.ordinal
            """,
            entity_key=table["entity_key"],
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
                MATCH (t:Table {entity_key: $entity_key})-[:HAS_FOREIGN_KEY]->(fk:ForeignKey)
                OPTIONAL MATCH (fk)-[:REFERENCES_TABLE]->(target:Table)
                RETURN fk {.*} AS foreign_key, target.table_name AS to_table
                ORDER BY fk.fk_key
                """,
                entity_key=table["entity_key"],
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
                MATCH (t:Table {entity_key: $entity_key})-[j:JOINS_TO]-(other:Table)
                RETURN properties(j) AS join,
                       other.table_name AS other_table,
                       startNode(j).entity_key AS start_entity_key,
                       endNode(j).entity_key AS end_entity_key
                ORDER BY j.fk_key
                """,
                entity_key=table["entity_key"],
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
            "data_type": column["data_type"],
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
            "entity_key": foreign_key["entity_key"],
        }

    @staticmethod
    def _direct_join_payload(record, table: dict[str, str]) -> dict[str, Any]:
        join = dict(record["join"])
        start_key = record["start_entity_key"]
        end_key = record["end_entity_key"]
        current_is_start = table["entity_key"] == start_key
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
            MATCH (from:Table {{entity_key: $from_key}}), (to:Table {{entity_key: $to_key}})
            MATCH p=allShortestPaths((from)-[:JOINS_TO*..{max_hops}]-(to))
            RETURN [node IN nodes(p) | node.table_name] AS table_names,
                   [node IN nodes(p) | node.entity_key] AS node_keys,
                   [edge IN relationships(p) | properties(edge)] AS joins,
                   [edge IN relationships(p) | startNode(edge).entity_key] AS start_keys,
                   [edge IN relationships(p) | endNode(edge).entity_key] AS end_keys
            LIMIT $max_paths
        """
        records = list(tx.run(
            query,
            from_key=from_table["entity_key"],
            to_key=to_table["entity_key"],
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
KNOWLEDGE_INCLUDE = ("summary", "definition", "provenance")
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
    """Read-only repository for the kg-v1 ``Knowledge`` dependency graph."""

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
        raw_id = cls._request_value(request, "id")
        if not isinstance(raw_id, str):
            raise KnowledgeGraphError("INVALID_REQUEST", "id must match <database>:<non-negative integer>")
        match = KNOWLEDGE_ID_RE.fullmatch(raw_id)
        if match is None:
            raise KnowledgeGraphError("INVALID_REQUEST", "id must match <database>:<non-negative integer>")

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
                    for key in ("depth", "max_nodes")
                    if hasattr(raw_expand, key)
                }
            unknown_expand = sorted(set(expand_values) - {"depth", "max_nodes"})
            if unknown_expand or "depth" not in expand_values or "max_nodes" not in expand_values:
                raise KnowledgeGraphError(
                    "INVALID_REQUEST",
                    "expand must contain exactly depth and max_nodes",
                )
            depth = expand_values["depth"]
            max_nodes = expand_values["max_nodes"]
            if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= MAX_KNOWLEDGE_DEPTH:
                raise KnowledgeGraphError(
                    "INVALID_REQUEST",
                    "expand.depth must be an integer from 0 through 5",
                )
            if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or not 1 <= max_nodes <= MAX_KNOWLEDGE_NODES:
                raise KnowledgeGraphError(
                    "INVALID_REQUEST",
                    "expand.max_nodes must be an integer from 1 through 50",
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

    def get_knowledge(self, request: Any, hidden_ids: Any = None) -> dict[str, Any]:
        query = self._normalize(request)
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
            "id": node.get("id"),
            "name": node.get("name"),
            "type": node.get("type"),
            "distance": distance,
        }
        if "summary" in include:
            payload["summary"] = node.get("summary")
        if "definition" in include:
            payload["definition"] = node.get("definition")
        if "provenance" in include:
            payload["provenance"] = {
                "source_file": node.get("source_file"),
                "source_line": node.get("source_line"),
                "source_id": node.get("source_id"),
            }
        return payload

    @staticmethod
    def _read_root(tx, query: dict[str, Any]) -> dict[str, Any]:
        record = tx.run(
            """
            MATCH (d:Database {database_name: $database_name})-[:HAS_KNOWLEDGE]->(k:Knowledge)
            WHERE k.id = $root_id AND NOT k.id IN $hidden_ids
            RETURN k { .id, .name, .type, .summary, .definition,
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
            MATCH (d:Database {database_name: $database_name})-[:HAS_KNOWLEDGE]->(parent:Knowledge)
            WHERE parent.id = $parent_ids[parent_index]
              AND NOT parent.id IN $hidden_ids
            MATCH (d)-[:HAS_KNOWLEDGE]->(child:Knowledge)
            MATCH (parent)-[r:REQUIRES]->(child)
            WHERE child.id STARTS WITH $database_prefix
              AND NOT child.id IN $hidden_ids
            RETURN parent.id AS parent_id,
                   r.position AS position,
                   child { .id, .name, .type, .summary, .definition,
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
                        "from": parent_id,
                        "to": child_id,
                        "type": "REQUIRES",
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
            if edge["from"] in visible_ids and edge["to"] in visible_ids
        ]
        return {
            "root_id": root_id,
            "nodes": [
                Neo4jKnowledgeRepository._node_payload(nodes[node_id], distances[node_id], include)
                for node_id in ordered_ids
            ],
            "edges": response_edges,
            "truncated": truncated,
        }
