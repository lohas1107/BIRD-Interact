"""BIRD service tools exposed as OpenAI-compatible function schemas."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

import httpx

from shared.config import settings

logger = logging.getLogger(__name__)

TOOL_COSTS = {
    "execute_sql": 1.0,
    "get_schema": 1.0,
    "get_all_column_meanings": 1.0,
    "get_column_meaning": 0.5,
    "get_all_external_knowledge_names": 0.5,
    "get_knowledge_definition": 0.5,
    "get_all_knowledge_definitions": 1.0,
    "ask_user": 2.0,
    "submit_sql": 3.0,
}


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


TOOL_SCHEMAS = {
    "execute_sql": _schema(
        "execute_sql",
        "Execute a PostgreSQL query against the current task database. Cost: 1 bird-coin.",
        {"sql": {"type": "string", "description": "PostgreSQL query to execute."}},
        ["sql"],
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


def schemas_for(names: list[str]) -> list[Dict[str, Any]]:
    return [TOOL_SCHEMAS[name] for name in names]


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


def execute_tool(name: str, args: Dict[str, Any], task_id: str, state: Dict[str, Any]) -> str:
    try:
        if name == "execute_sql":
            return _execute_sql(task_id, args)
        if name == "get_schema":
            return _get_schema(task_id)
        if name == "get_all_column_meanings":
            return _get_column_meanings(task_id)
        if name == "get_column_meaning":
            return _get_column_meaning(task_id, args)
        if name == "get_all_external_knowledge_names":
            return _get_knowledge_names(task_id)
        if name == "get_knowledge_definition":
            return _get_knowledge(task_id, args)
        if name == "get_all_knowledge_definitions":
            return _get_all_knowledge(task_id)
        if name == "ask_user":
            return _ask_user(task_id, args, state)
        if name == "submit_sql":
            return _submit_sql(task_id, args, state)
        return f"Unknown tool: {name}"
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return f"Error calling {name}: {type(exc).__name__}: {exc}"
