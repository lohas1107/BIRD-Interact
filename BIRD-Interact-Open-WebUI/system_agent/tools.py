"""BIRD service tools exposed as OpenAI-compatible function schemas."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping

import httpx

from shared.config import settings

logger = logging.getLogger(__name__)


def _schema(name: str, description: str, properties: Dict[str, Any] | None = None,
            required: list[str] | None = None) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


@dataclass(frozen=True)
class ToolSpec:
    """The complete runtime definition of one OpenAI-compatible BIRD tool."""

    name: str
    schema: Dict[str, Any]
    cost: float
    handler: Callable[[Dict[str, Any], str, Dict[str, Any]], str]


class GraphToolError(RuntimeError):
    """Structured graph-service failure surfaced as an errored tool result."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


_SCHEMAS = {
    "execute_sql": _schema(
        "execute_sql",
        "Execute a PostgreSQL query against the current task database. Cost: 1 bird-coin.",
        {"sql": {"type": "string", "description": "PostgreSQL query to execute."}},
        ["sql"],
    ),
    "search_semantic_context": _schema(
        "search_semantic_context",
        "Discover candidate Knowledge definitions and table-schema entries with exact-text and semantic retrieval. Search output is candidate discovery only; use get_knowledge to confirm an authoritative definition. Cost: 2 bird-coins.",
        {
            "queries": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "maxItems": 8,
                "description": "One to eight non-empty search queries.",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Maximum results per resource group after query merge.",
            },
            "resource_types": {
                "type": "array",
                "items": {"type": "string", "enum": ["knowledge", "table_schema"]},
                "minItems": 1,
                "description": "Resource groups to search.",
            },
        },
        ["queries", "top_k", "resource_types"],
    ),
    "search_semantic_context_5_2": _schema(
        "search_semantic_context_5_2",
        "Search the fixed metadata-5-2 candidate corpus and optionally candidate Knowledge definitions. Metadata is candidate discovery only; use get_knowledge for authoritative formulas, classifications, thresholds, and conditions. Cost: 2 bird-coins.",
        {
            "queries": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "maxItems": 8,
                "description": "One to eight non-empty search queries.",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Maximum results per resource group after query merge.",
            },
            "resource_types": {
                "type": "array",
                "items": {"type": "string", "enum": ["knowledge", "metadata"]},
                "minItems": 1,
                "description": "Resource groups to search; table_schema is not supported by this tool.",
            },
        },
        ["queries", "top_k", "resource_types"],
    ),
    "get_knowledge": _schema(
        "get_knowledge",
        "Get the authoritative Knowledge definition for a canonical ID and optionally expand ordered DEPENDS_ON dependencies. Cost: 0.5 bird-coins.",
        {
            "knowledge_id": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9_]*:[0-9]+$",
                "description": "Canonical Knowledge ID, for example alien:10.",
            },
            "include": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["description", "definition", "provenance", "related_columns"],
                },
                "description": "Optional node sections; omitted means identity fields only.",
            },
            "expand": {
                "type": "object",
                "properties": {
                    "depth": {"type": "integer", "minimum": 0, "maximum": 5},
                    "nodes": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["depth", "nodes"],
                "additionalProperties": False,
            },
        },
        ["knowledge_id"],
    ),
    "get_table_schema": _schema(
        "get_table_schema",
        "Get physical table columns, descriptions, constraints, direct joins, and optional shortest FK join paths. This describes schema and cardinality, not metric formulas or thresholds. Cost: 0.5 bird-coins.",
        {
            "database_name": {"type": "string", "description": "Graph database name."},
            "from_table": {"type": "string", "description": "Exact table name; matching is case-insensitive."},
            "to_table": {"type": "string", "description": "Optional second exact table name; requests join_paths."},
            "include": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["columns", "descriptions", "constraints", "direct_joins"],
                },
                "description": "Optional sections; defaults to all sections.",
            },
            "hops": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            "paths": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        },
        ["database_name", "from_table"],
    ),
    "get_schema": _schema(
        "get_schema", "Get CREATE TABLE statements for the current task database. Cost: 1 bird-coin."
    ),
    "get_all_column_meanings": _schema(
        "get_all_column_meanings", "Get all database column meanings. Cost: 1 bird-coin."
    ),
    "get_column_meaning": _schema(
        "get_column_meaning",
        "Get the meaning of one database column. Cost: 0.5 bird-coins.",
        {
            "table_name": {"type": "string"},
            "column_name": {"type": "string"},
        },
        ["table_name", "column_name"],
    ),
    "get_all_external_knowledge_names": _schema(
        "get_all_external_knowledge_names",
        "List available external knowledge entries. Cost: 0.5 bird-coins.",
    ),
    "get_knowledge_definition": _schema(
        "get_knowledge_definition",
        "Get one external knowledge definition. Cost: 0.5 bird-coins.",
        {"knowledge_name": {"type": "string"}},
        ["knowledge_name"],
    ),
    "get_all_knowledge_definitions": _schema(
        "get_all_knowledge_definitions",
        "Get all external knowledge definitions. Cost: 1 bird-coin.",
    ),
    "ask_user": _schema(
        "ask_user",
        "Ask the simulated user one clarification question. Cost: 2 bird-coins.",
        {"question": {"type": "string"}},
        ["question"],
    ),
    "submit_sql": _schema(
        "submit_sql",
        "Submit final PostgreSQL for evaluation. Cost: 3 bird-coins.",
        {"sql": {"type": "string"}},
        ["sql"],
    ),
}


def _db_url(path: str) -> str:
    return f"http://127.0.0.1:{settings.db_env_port}{path}"


def _user_url(path: str) -> str:
    return f"http://127.0.0.1:{settings.user_sim_port}{path}"


def _post(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.post(url, json=payload)
    if response.status_code >= 400:
        return {"_error": f"HTTP {response.status_code}: {response.text[:500]}"}
    try:
        return response.json()
    except ValueError:
        return {"_error": f"Invalid JSON response: {response.text[:500]}"}


def _post_graph(path: str, payload: Dict[str, Any], timeout: float = 60.0) -> Dict[str, Any]:
    """Call a graph route and preserve its stable code/message contract."""
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.post(_db_url(path), json=payload)
    except httpx.RequestError as exc:
        raise GraphToolError("NEO4J_UNAVAILABLE", str(exc) or "graph service unavailable") from exc

    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except (ValueError, TypeError, AttributeError):
            detail = None
        if isinstance(detail, dict) and detail.get("code"):
            raise GraphToolError(
                str(detail["code"]),
                str(detail.get("message") or "graph request failed"),
            )
        raise GraphToolError(
            "GRAPH_QUERY_FAILED",
            f"HTTP {response.status_code}: {response.text[:500]}",
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise GraphToolError("GRAPH_QUERY_FAILED", "graph service returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise GraphToolError("GRAPH_QUERY_FAILED", "graph service returned a non-object response")
    return data


def _validate_graph_args(name: str, args: Dict[str, Any]) -> None:
    """Validate graph arguments before spending a network call.

    The same limits are enforced again by the DB service.  Keeping this small
    boundary check in the registry prevents malformed nested tool arguments
    from reaching a service and makes direct runtime dispatch fail closed.
    """
    if not isinstance(args, dict):
        raise GraphToolError("INVALID_REQUEST", "tool arguments must be an object")

    if name == "search_semantic_context":
        queries = args.get("queries")
        if (
            not isinstance(queries, list)
            or not 1 <= len(queries) <= 8
            or any(not isinstance(value, str) or not value.strip() for value in queries)
        ):
            raise GraphToolError("INVALID_REQUEST", "queries must contain 1 through 8 non-empty strings")
        top_k = args.get("top_k")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
            raise GraphToolError("INVALID_REQUEST", "top_k must be an integer from 1 through 20")
        resource_types = args.get("resource_types")
        if (
            not isinstance(resource_types, list)
            or not resource_types
            or any(value not in ("knowledge", "table_schema") for value in resource_types)
        ):
            raise GraphToolError(
                "INVALID_REQUEST",
                "resource_types must contain at least one of knowledge or table_schema",
            )
        return

    if name == "search_semantic_context_5_2":
        queries = args.get("queries")
        if (
            not isinstance(queries, list)
            or not 1 <= len(queries) <= 8
            or any(not isinstance(value, str) or not value.strip() for value in queries)
        ):
            raise GraphToolError("INVALID_REQUEST", "queries must contain 1 through 8 non-empty strings")
        top_k = args.get("top_k")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
            raise GraphToolError("INVALID_REQUEST", "top_k must be an integer from 1 through 20")
        resource_types = args.get("resource_types")
        if (
            not isinstance(resource_types, list)
            or not resource_types
            or any(value not in ("knowledge", "metadata") for value in resource_types)
        ):
            raise GraphToolError(
                "INVALID_REQUEST",
                "resource_types must contain at least one of knowledge or metadata",
            )
        return

    if name == "get_knowledge":
        knowledge_id = args.get("knowledge_id")
        if not isinstance(knowledge_id, str) or re.fullmatch(r"[a-z][a-z0-9_]*:[0-9]+", knowledge_id) is None:
            raise GraphToolError(
                "INVALID_REQUEST",
                "knowledge_id must match <database>:<non-negative integer>",
            )
        include = args.get("include")
        if include is not None and (
            not isinstance(include, list)
            or any(value not in ("description", "definition", "provenance", "related_columns") for value in include)
        ):
            raise GraphToolError("INVALID_REQUEST", "include contains unsupported values")
        expand = args.get("expand")
        if expand is not None:
            if not isinstance(expand, dict) or set(expand) != {"depth", "nodes"}:
                raise GraphToolError("INVALID_REQUEST", "expand must contain exactly depth and nodes")
            depth = expand["depth"]
            nodes = expand["nodes"]
            if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 5:
                raise GraphToolError("INVALID_REQUEST", "expand.depth must be an integer from 0 through 5")
            if isinstance(nodes, bool) or not isinstance(nodes, int) or not 1 <= nodes <= 50:
                raise GraphToolError("INVALID_REQUEST", "expand.nodes must be an integer from 1 through 50")
        return

    if name == "get_table_schema":
        for key in ("database_name", "from_table"):
            if not isinstance(args.get(key), str) or not args[key].strip():
                raise GraphToolError("INVALID_REQUEST", f"{key} is required")
        if "to_table" in args and args["to_table"] is not None and (
            not isinstance(args["to_table"], str) or not args["to_table"].strip()
        ):
            raise GraphToolError("INVALID_REQUEST", "to_table cannot be empty")
        include = args.get("include")
        if include is not None and (
            not isinstance(include, list)
            or any(value not in ("columns", "descriptions", "constraints", "direct_joins") for value in include)
        ):
            raise GraphToolError("INVALID_REQUEST", "include contains unsupported values")
        for key, low, high in (("hops", 1, 10), ("paths", 1, 20)):
            if key in args and args[key] is not None:
                value = args[key]
                if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                    raise GraphToolError("INVALID_REQUEST", f"{key} is out of range")


def _task_payload(task_id: str) -> Dict[str, Any]:
    return {"task_id": task_id}


def _execute_sql(task_id: str, args: Dict[str, Any]) -> str:
    data = _post(_db_url("/execute"), {**_task_payload(task_id), "sql": args.get("sql", "")}, 120.0)
    if "_error" in data:
        return f"SQL Error: {data['_error']}"
    if data.get("success"):
        return data.get("result", "Query executed successfully.")
    return f"SQL Error: {data.get('error') or 'Execution failed (no details)'}"


def _get_schema(task_id: str) -> str:
    data = _post(_db_url("/schema"), _task_payload(task_id), 30.0)
    return data.get("schema", data.get("_error", "Schema not available"))


def _search_semantic_context(task_id: str, args: Dict[str, Any]) -> str:
    payload = {
        "task_id": task_id,
        "queries": args.get("queries"),
        "top_k": args.get("top_k"),
        "resource_types": args.get("resource_types"),
    }
    data = _post_graph(
        "/search/semantic_context",
        payload,
        120.0,
    )
    return json.dumps(data, ensure_ascii=False)


def _search_semantic_context_5_2(task_id: str, args: Dict[str, Any]) -> str:
    payload = {
        "task_id": task_id,
        "queries": args.get("queries"),
        "top_k": args.get("top_k"),
        "resource_types": args.get("resource_types"),
    }
    data = _post_graph(
        "/search/semantic_context_5_2",
        payload,
        120.0,
    )
    # The endpoint has a fixed response shape.  Keep this boundary explicit so
    # a malformed upstream response cannot reintroduce table_schema to the
    # model through the new tool.
    if set(data) != {"knowledge", "metadata"}:
        raise GraphToolError("GRAPH_QUERY_FAILED", "metadata search returned an invalid response shape")
    return json.dumps({"knowledge": data["knowledge"], "metadata": data["metadata"]}, ensure_ascii=False)


def _get_graph_knowledge(task_id: str, args: Dict[str, Any]) -> str:
    payload = {**_task_payload(task_id), "knowledge_id": args.get("knowledge_id")}
    for key in ("include", "expand"):
        if key in args and args[key] is not None:
            payload[key] = args[key]
    data = _post_graph("/knowledge/graph", payload, 60.0)
    return json.dumps(data, ensure_ascii=False)


def _get_table_schema(task_id: str, args: Dict[str, Any]) -> str:
    payload = {
        **_task_payload(task_id),
        "database_name": args.get("database_name"),
        "from_table": args.get("from_table"),
    }
    for key in ("to_table", "include", "hops", "paths"):
        if key in args and args[key] is not None:
            payload[key] = args[key]
    data = _post_graph("/table_schema", payload, 60.0)
    return json.dumps(data, ensure_ascii=False)


def _get_column_meanings(task_id: str) -> str:
    data = _post(_db_url("/all_column_meanings"), _task_payload(task_id), 30.0)
    return data.get("column_meanings", data.get("_error", "{}"))


def _get_column_meaning(task_id: str, args: Dict[str, Any]) -> str:
    payload = {
        **_task_payload(task_id),
        "table_name": args.get("table_name", ""),
        "column_name": args.get("column_name", ""),
    }
    data = _post(_db_url("/column_meaning"), payload, 30.0)
    return data.get("meaning", data.get("_error", "Column meaning not found"))


def _get_knowledge_names(task_id: str) -> str:
    data = _post(_db_url("/knowledge_names"), _task_payload(task_id), 30.0)
    return json.dumps(data.get("names", []), ensure_ascii=False)


def _get_knowledge(task_id: str, args: Dict[str, Any]) -> str:
    payload = {**_task_payload(task_id), "knowledge_name": args.get("knowledge_name", "")}
    data = _post(_db_url("/knowledge"), payload, 30.0)
    return data.get("knowledge", data.get("_error", "Knowledge not found"))


def _get_all_knowledge(task_id: str) -> str:
    data = _post(_db_url("/knowledge"), _task_payload(task_id), 30.0)
    return data.get("knowledge", data.get("_error", "[]"))


def _ask_user(task_id: str, args: Dict[str, Any], state: Dict[str, Any]) -> str:
    question = args.get("question", "")
    data = _post(_user_url("/ask"), {**_task_payload(task_id), "question": question}, 60.0)
    answer = data.get("answer", data.get("_error", "No response from user."))
    history = state.setdefault("dialogue_history", [])
    history.extend([
        {"role": "agent", "content": question},
        {"role": "user", "content": answer},
    ])
    return answer


def _submit_sql(task_id: str, args: Dict[str, Any], state: Dict[str, Any]) -> str:
    data = _post(
        _db_url("/submit"),
        {**_task_payload(task_id), "sql": args.get("sql", "")},
        120.0,
    )
    if "_error" in data:
        return data["_error"]

    raw_msg = data.get("message", "")
    state["_last_submit_raw"] = raw_msg
    if data.get("passed"):
        reward = data.get("reward", 0.0)
        state["total_reward"] = state.get("total_reward", 0.0) + reward
        phase = data.get("phase_completed")
        if phase == 1:
            state["phase1_completed"] = True
            state["current_phase"] = 2
            if data.get("has_follow_up"):
                transition = _post(_user_url("/phase_transition"), _task_payload(task_id), 30.0)
                if "_error" in transition:
                    logger.warning("Phase transition failed for %s: %s", task_id, transition["_error"])
            else:
                state["task_done"] = True
        elif phase == 2:
            state["phase2_completed"] = True
            state["task_done"] = True

    parts = [raw_msg.replace("[exec_err_flg] ", "")]
    if data.get("reward", 0) > 0:
        parts.append(f"Reward: {data['reward']}")
    if data.get("has_follow_up"):
        parts.append(f"Follow-up question: {data.get('follow_up_query', '')}")
    budget = state.get("budget_remaining", 0)
    parts.append(f"Budget remaining: {budget} bird-coins")
    return "\n".join(part for part in parts if part)


def _handle_execute_sql(args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    return _execute_sql(task_id, args)


def _handle_get_schema(args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    return _get_schema(task_id)


def _handle_search_semantic_context(args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    return _search_semantic_context(task_id, args)


def _handle_search_semantic_context_5_2(args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    return _search_semantic_context_5_2(task_id, args)


def _handle_get_knowledge(args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    return _get_graph_knowledge(task_id, args)


def _handle_get_table_schema(args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    return _get_table_schema(task_id, args)


def _handle_get_all_column_meanings(args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    return _get_column_meanings(task_id)


def _handle_get_column_meaning(args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    return _get_column_meaning(task_id, args)


def _handle_get_all_external_knowledge_names(args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    return _get_knowledge_names(task_id)


def _handle_get_knowledge_definition(args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    return _get_knowledge(task_id, args)


def _handle_get_all_knowledge_definitions(args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    return _get_all_knowledge(task_id)


def _handle_ask_user(args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    return _ask_user(task_id, args, state)


def _handle_submit_sql(args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    return _submit_sql(task_id, args, state)


def _build_registry() -> dict[str, ToolSpec]:
    """Build the only source of truth for schemas, costs, and dispatch."""
    definitions = (
        ("execute_sql", 1.0, _handle_execute_sql),
        ("get_schema", 1.0, _handle_get_schema),
        ("search_semantic_context", 2.0, _handle_search_semantic_context),
        ("search_semantic_context_5_2", 2.0, _handle_search_semantic_context_5_2),
        ("get_knowledge", 0.5, _handle_get_knowledge),
        ("get_table_schema", 0.5, _handle_get_table_schema),
        ("get_all_column_meanings", 1.0, _handle_get_all_column_meanings),
        ("get_column_meaning", 0.5, _handle_get_column_meaning),
        ("get_all_external_knowledge_names", 0.5, _handle_get_all_external_knowledge_names),
        ("get_knowledge_definition", 0.5, _handle_get_knowledge_definition),
        ("get_all_knowledge_definitions", 1.0, _handle_get_all_knowledge_definitions),
        ("ask_user", 2.0, _handle_ask_user),
        ("submit_sql", 3.0, _handle_submit_sql),
    )
    return {
        name: ToolSpec(name=name, schema=_SCHEMAS[name], cost=cost, handler=handler)
        for name, cost, handler in definitions
    }


TOOL_REGISTRY: Mapping[str, ToolSpec] = _build_registry()

# Compatibility views for existing callers.  Definitions remain in
# TOOL_REGISTRY; these mappings are derived and contain no independent data.
TOOL_SCHEMAS: Mapping[str, Dict[str, Any]] = {
    name: spec.schema for name, spec in TOOL_REGISTRY.items()
}
TOOL_COSTS: Mapping[str, float] = {
    name: spec.cost for name, spec in TOOL_REGISTRY.items()
}


def tool_names() -> tuple[str, ...]:
    """Return legal logical names in registry insertion order."""
    return tuple(TOOL_REGISTRY)


def schemas_for(names: list[str] | tuple[str, ...]) -> list[Dict[str, Any]]:
    """Return schemas in exactly the order supplied by a session profile."""
    return [TOOL_REGISTRY[name].schema for name in names]


def has_tool(name: str) -> bool:
    return name in TOOL_REGISTRY


def tool_cost(name: str) -> float:
    spec = TOOL_REGISTRY.get(name)
    return spec.cost if spec else 0.0


def execute_tool(
    name: str,
    args: Dict[str, Any],
    task_id: str,
    state: Dict[str, Any],
    allowed_tools: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Dispatch one tool after applying the session profile allowlist."""
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        state["_last_tool_dispatch_error"] = True
        return f"UNKNOWN_TOOL: {name}"

    if allowed_tools is None:
        profile = state.get("agent_profile")
        if isinstance(profile, dict) and "tools" in profile:
            allowed_tools = profile.get("tools") or []
    if allowed_tools is not None and name not in allowed_tools:
        profile = state.get("agent_profile") or {}
        profile_name = profile.get("name", "<unnamed>") if isinstance(profile, dict) else str(profile)
        state["_last_tool_dispatch_error"] = True
        return f"TOOL_NOT_ALLOWED: {name} is not enabled by profile {profile_name}"

    state["_last_tool_dispatch_error"] = False
    try:
        if name in {"search_semantic_context", "search_semantic_context_5_2", "get_knowledge", "get_table_schema"}:
            _validate_graph_args(name, args)
        return spec.handler(args, task_id, state)
    except GraphToolError as exc:
        logger.warning("Graph tool %s failed: %s", name, exc)
        state["_last_tool_dispatch_error"] = True
        return str(exc)
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        state["_last_tool_dispatch_error"] = True
        return f"Error calling {name}: {type(exc).__name__}: {exc}"
