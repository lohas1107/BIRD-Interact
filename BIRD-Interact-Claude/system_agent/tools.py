"""Per-session in-process MCP tools for the Claude Agent SDK."""

from __future__ import annotations

import json
from typing import Any

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

from shared.config import settings

TOOL_COSTS = {
    "execute_sql": 1.0,
    "get_schema": 1.0,
    "search_semantic_context": 1.0,
    "get_table_schema": 0.5,
    "get_knowledge": 0.5,
    "get_all_column_meanings": 1.0,
    "get_column_meaning": 0.5,
    "get_all_external_knowledge_names": 0.5,
    "get_knowledge_definition": 0.5,
    "get_all_knowledge_definitions": 1.0,
    "ask_user": 2.0,
    "submit_sql": 3.0,
}

BUDGET_TERMINAL_REASON = "budget_exhausted_after_submit"
PHASE1_COMPLETE_REASON = "phase1_complete"
PHASE2_COMPLETE_REASON = "phase2_complete"
PHASE_TRANSITION_FAILED_REASON = "phase_transition_failed"


def _text(value: Any) -> dict:
    return {"content": [{"type": "text", "text": str(value)}]}


def _preview(value: Any, limit: int = 2000) -> str:
    value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return value if len(value) <= limit else value[:limit] + "...<truncated>"


class ToolServiceError(RuntimeError):
    """Preserve structured service error codes in the agent-visible result."""


def build_tool_server(state: dict, mode: str):
    """Build tools whose closures own one task's deterministic benchmark state."""

    async def call_service(port: int, path: str, payload: dict, timeout: float = 120.0) -> dict:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(f"http://127.0.0.1:{port}{path}", json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                try:
                    detail = response.json().get("detail")
                except (ValueError, TypeError):
                    detail = None
                if isinstance(detail, dict) and detail.get("code"):
                    raise ToolServiceError(
                        f"{detail['code']}: {detail.get('message', 'service request failed')}"
                    ) from exc
                raise
            return response.json()

    def mark_budget_terminal() -> None:
        """Mark the one allowed final submit as terminal without inferring success."""

        state["budget_exhausted"] = True
        state["terminal_reason"] = BUDGET_TERMINAL_REASON
        state["next_action"] = "stop"
        state["retryable"] = False
        state["task_done"] = True

    async def run_tool(name: str, args: dict, operation) -> dict:
        if state.get("task_done"):
            response = _text("Task is already complete. Do not call more tools.")
            response["is_error"] = True
            return response
        if mode == "c-interact" and state.get("_submitted_this_turn"):
            response = _text("SQL was already submitted in this turn. Stop and await feedback.")
            response["is_error"] = True
            return response

        cost = TOOL_COSTS[name]
        before = state.get("budget_remaining")
        budget_terminal = False
        budget_overdrawn = False
        if mode == "a-interact" and before is not None:
            if before < cost and name != "submit_sql":
                available = state.get("tool_profile", {}).get("tools", ())
                instruction = (
                    "Only submit_sql may use the one allowed final overdraw; do not spend on exploration, and submit only a semantically grounded query."
                    if "submit_sql" in available else
                    "No further tool call can be made."
                )
                response = _text(
                    f"Budget is insufficient for {name} ({before:.1f} remaining). {instruction}"
                )
                response["is_error"] = True
                return response
            after = before - cost
            if name == "submit_sql" and after <= 0:
                # Preserve the historical negative sentinel for result/report
                # compatibility, but use explicit state below to decide why
                # the session is terminal.
                budget_terminal = True
                budget_overdrawn = before < cost
                state["budget_remaining"] = -1.0
                state["budget_overdrawn"] = budget_overdrawn
                state["_budget_terminal_after_submit"] = True
            else:
                state["budget_remaining"] = after

        tool_failed = False
        try:
            result = await operation()
        except Exception as exc:
            result = f"Tool error: {type(exc).__name__}: {exc}"
            tool_failed = True

        if tool_failed and name == "submit_sql":
            state["last_submit_status"] = "error"

        if mode == "a-interact" and name == "submit_sql" and budget_terminal:
            # The submission was allowed to run even though it exhausted or
            # overran the budget.  Its correctness is independent from the
            # terminal budget state; never offer a retry or Phase 2 here.
            mark_budget_terminal()
        elif tool_failed and name == "submit_sql":
            state["next_action"] = "retry_submit"
            state["retryable"] = True

        event = {
            "type": "tool",
            "tool": name,
            "args": args,
            "result": _preview(result),
        }
        if mode == "a-interact":
            event.update({
                "cost": cost,
                "budget_before": before,
                "budget_after": state.get("budget_remaining"),
            })
        if name == "submit_sql":
            event.update({
                "passed": state.get("last_submit_passed"),
                "phase": state.get("last_submit_phase"),
                "status": state.get("last_submit_status"),
                "terminal_reason": state.get("terminal_reason"),
                "next_action": state.get("next_action"),
                "retryable": state.get("retryable"),
                "budget_terminal": budget_terminal,
                "budget_overdrawn": state.get("budget_overdrawn", False),
            })
        state.setdefault("tool_trajectory", []).append(event)

        notes = []
        if mode == "a-interact" and state.get("budget_remaining", -1) >= 0:
            notes.append(
                f"Budget remaining: {state['budget_remaining']:.1f}/"
                f"{state.get('initial_budget', 0):.1f} bird-coins"
            )
        if name == "submit_sql":
            if state.get("terminal_reason") == BUDGET_TERMINAL_REASON:
                notes.append(
                    "Terminal: this was the one allowed final submit; do not call more tools."
                )
                if state.get("phase2_skipped_due_budget"):
                    notes.append("Phase 2 was skipped because no budget remained.")
            elif state.get("task_done"):
                notes.append("Task complete; do not call more tools.")
            elif state.get("next_action") == "phase2":
                notes.append("Next action: continue with Phase 2.")
            elif state.get("next_action") == "retry_submit":
                notes.append("This submission is retryable while budget remains.")
        response = _text(str(result) + ("\n\n[SYSTEM NOTE: " + "; ".join(notes) + "]" if notes else ""))
        if tool_failed:
            response["is_error"] = True
        return response

    task_id = state["task_id"]

    @tool(
        "execute_sql",
        "Execute a read-only PostgreSQL query as an optional runtime check. It checks parse/execution and returned rows only; it does not validate semantic correctness. Cost: 1 bird-coin.",
        {"sql": str},
    )
    async def execute_sql(args):
        async def op():
            data = await call_service(settings.db_env_port, "/execute", {"task_id": task_id, "sql": args["sql"]})
            return data.get("result") if data.get("success") else f"SQL Error: {data.get('error', 'Execution failed')}"
        return await run_tool("execute_sql", args, op)

    @tool(
        "get_schema",
        "Get the physical database schema. Use it for tables, columns, and types; it does not define domain metrics or thresholds. Cost: 1 bird-coin.",
        {},
    )
    async def get_schema(args):
        async def op():
            return (await call_service(settings.db_env_port, "/schema", {"task_id": task_id})).get("schema", "")
        return await run_tool("get_schema", args, op)

    @tool(
        "search_semantic_context",
        "Discover candidate Knowledge definitions and table-schema entries with exact-text and semantic retrieval. Search the exact metric name and its natural-language phrase together when needed. Results are candidates, not semantic confirmation; if no authoritative definition is returned, or only related columns/candidates appear, stop repeating searches and use ask_user to obtain the exact definition, formula, threshold, conditions, and validity rule. Cost: 1 bird-coin.",
        {
            "type": "object",
            "properties": {
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
                    "description": "Maximum results per resource group after multi-query merge.",
                },
                "resource_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["knowledge", "table_schema"]},
                    "minItems": 1,
                    "description": "Resource groups to search.",
                },
            },
            "required": ["queries", "top_k", "resource_types"],
            "additionalProperties": False,
        },
    )
    async def search_semantic_context(args):
        async def op():
            payload = {"task_id": task_id, **args}
            data = await call_service(settings.db_env_port, "/search/semantic_context", payload)
            return json.dumps(data, ensure_ascii=False)
        return await run_tool("search_semantic_context", args, op)

    @tool(
        "get_table_schema",
        "Get physical table columns, descriptions, constraints, direct joins, and optional shortest FK join paths. This describes schema and cardinality, not metric formulas or thresholds. Cost: 0.5 bird-coins.",
        {
            "type": "object",
            "properties": {
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
            "required": ["database_name", "from_table"],
            "additionalProperties": False,
        },
    )
    async def get_table_schema(args):
        async def op():
            payload = {
                "task_id": task_id,
                "database_name": args["database_name"],
                "from_table": args["from_table"],
            }
            for key in ("to_table", "include", "hops", "paths"):
                if key in args and args[key] is not None:
                    payload[key] = args[key]
            data = await call_service(settings.db_env_port, "/table_schema", payload)
            return json.dumps(data, ensure_ascii=False)
        return await run_tool("get_table_schema", args, op)

    @tool(
        "get_knowledge",
        "Get the authoritative Knowledge definition for a canonical ID and optionally expand ordered DEPENDS_ON dependencies. Use its formula, threshold, conditions, and related columns; do not substitute a raw column or a similar search result. Cost: 0.5 bird-coins.",
        {
            "type": "object",
            "properties": {
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
            "required": ["knowledge_id"],
            "additionalProperties": False,
        },
    )
    async def get_knowledge(args):
        async def op():
            payload = {"task_id": task_id, "knowledge_id": args["knowledge_id"]}
            for key in ("include", "expand"):
                if key in args and args[key] is not None:
                    payload[key] = args[key]
            data = await call_service(settings.db_env_port, "/knowledge/graph", payload)
            return json.dumps(data, ensure_ascii=False)
        return await run_tool("get_knowledge", args, op)

    @tool(
        "get_all_column_meanings",
        "Get physical column meanings. Use them to map a resolved metric to columns; column descriptions do not replace a Knowledge formula or threshold. Cost: 1 bird-coin.",
        {},
    )
    async def get_all_column_meanings(args):
        async def op():
            return (await call_service(settings.db_env_port, "/all_column_meanings", {"task_id": task_id})).get("column_meanings", "{}")
        return await run_tool("get_all_column_meanings", args, op)

    @tool(
        "get_column_meaning",
        "Get one physical column meaning. Use it for schema interpretation only; it is not a substitute for a domain Knowledge definition. Cost: 0.5 bird-coins.",
        {"table_name": str, "column_name": str},
    )
    async def get_column_meaning(args):
        async def op():
            payload = {"task_id": task_id, "table_name": args["table_name"], "column_name": args["column_name"]}
            return (await call_service(settings.db_env_port, "/column_meaning", payload)).get("meaning", "Not found")
        return await run_tool("get_column_meaning", args, op)

    @tool(
        "get_all_external_knowledge_names",
        "List exact names of the task's external Knowledge definitions. Use a returned name with get_knowledge_definition; do not infer a metric from a name alone. Cost: 0.5 bird-coins.",
        {},
    )
    async def get_all_external_knowledge_names(args):
        async def op():
            data = await call_service(settings.db_env_port, "/knowledge_names", {"task_id": task_id})
            return json.dumps(data.get("names", []), ensure_ascii=False)
        return await run_tool("get_all_external_knowledge_names", args, op)

    @tool(
        "get_knowledge_definition",
        "Get one exact external Knowledge definition, including its formula, threshold, or condition when documented. Treat the returned definition as authoritative and do not invent missing details. Cost: 0.5 bird-coins.",
        {"knowledge_name": str},
    )
    async def get_knowledge_definition(args):
        async def op():
            payload = {"task_id": task_id, "knowledge_name": args["knowledge_name"]}
            return (await call_service(settings.db_env_port, "/knowledge", payload)).get("knowledge", "Not found")
        return await run_tool("get_knowledge_definition", args, op)

    @tool(
        "get_all_knowledge_definitions",
        "Get all external Knowledge definitions for discovery and exact semantic grounding. Use definitions rather than similar column names or guessed formulas. Cost: 1 bird-coin.",
        {},
    )
    async def get_all_knowledge_definitions(args):
        async def op():
            return (await call_service(settings.db_env_port, "/knowledge", {"task_id": task_id})).get("knowledge", "[]")
        return await run_tool("get_all_knowledge_definitions", args, op)

    @tool(
        "ask_user",
        "Ask one focused clarification question when a required metric, definition, formula, threshold, condition, or validity rule is unresolved. Request the exact missing semantic detail rather than asking a broad question or offering an inferred choice. The answer is returned synchronously in this same tool call; incorporate it into the semantic mapping before continuing. Cost: 2 bird-coins.",
        {"question": str},
    )
    async def ask_user(args):
        async def op():
            if mode == "c-interact":
                used = state.get("clarification_turns_used", 0)
                maximum = state.get("max_turn", 0)
                if used >= maximum:
                    available = state.get("tool_profile", {}).get("tools", ())
                    suffix = " Call submit_sql now." if "submit_sql" in available else ""
                    return f"Clarification limit reached ({maximum}).{suffix}"
                state["clarification_turns_used"] = used + 1
            data = await call_service(settings.user_sim_port, "/ask", {"task_id": task_id, "question": args["question"]})
            answer = data.get("answer", "No response from user.")
            state.setdefault("dialogue_history", []).extend([
                {"role": "agent", "content": args["question"]},
                {"role": "user", "content": answer},
            ])
            return answer
        result = await run_tool("ask_user", args, op)
        if not result.get("is_error") and mode == "a-interact":
            result["content"][0]["text"] += (
                "\n\n[SYSTEM NOTE: The answer was returned synchronously; incorporate it and continue.]"
            )
        if mode == "c-interact":
            maximum = state.get("max_turn", 0)
            remaining = max(0, maximum - state.get("clarification_turns_used", 0))
            result["content"][0]["text"] += (
                f"\n\n[SYSTEM NOTE: Clarification turns remaining: "
                f"{remaining}/{maximum}]"
            )
        return result

    @tool(
        "submit_sql",
        "Submit the current phase's PostgreSQL query for evaluation. Before calling, verify formula operators/constants/NULL rules, every filter/CASE/HAVING threshold and condition, every join key/type/cardinality, output row grain and counted entity, final SELECT columns and aliases, JSON aggregate/key/value-object shape when requested, and ASC/DESC ordering plus LIMIT. Do not submit a guessed formula, changed group grain, extra diagnostic column, or substituted JSON shape. This is the only tool allowed one final budget overdraw; when it exhausts the budget, the session becomes terminal and cannot retry or enter Phase 2. Cost: 3 bird-coins.",
        {"sql": str},
    )
    async def submit_sql(args):
        async def op():
            data = await call_service(settings.db_env_port, "/submit", {"task_id": task_id, "sql": args["sql"]})
            state["_submitted_this_turn"] = True
            raw = data.get("message", "")
            state["_last_submit_raw"] = raw
            state["last_submit_passed"] = bool(data.get("passed"))
            state["last_submit_phase"] = data.get("phase_completed")
            state["last_submit_status"] = "passed" if data.get("passed") else "failed"
            state["retryable"] = False
            if data.get("passed"):
                state["total_reward"] = state.get("total_reward", 0.0) + data.get("reward", 0.0)
                phase = data.get("phase_completed")
                if phase == 1:
                    state["phase1_completed"] = True
                    state["current_phase"] = 2
                    has_follow_up = bool(data.get("has_follow_up"))
                    budget_terminal = state.get("_budget_terminal_after_submit", False)
                    if has_follow_up and budget_terminal:
                        state["phase2_skipped_due_budget"] = True
                        state["terminal_reason"] = BUDGET_TERMINAL_REASON
                        state["next_action"] = "stop"
                        state["retryable"] = False
                        state["task_done"] = True
                    elif has_follow_up:
                        if not state.get("phase_transition_done", False):
                            try:
                                await call_service(
                                    settings.user_sim_port,
                                    "/phase_transition",
                                    {"task_id": task_id},
                                )
                            except Exception as exc:
                                state["phase_transition_failed"] = True
                                state["task_done"] = True
                                state["terminal_reason"] = PHASE_TRANSITION_FAILED_REASON
                                state["next_action"] = "stop"
                                state["retryable"] = False
                                state["_phase_transition_error"] = str(exc)
                                parts = [
                                    raw.replace("[exec_err_flg] ", ""),
                                    "Phase transition failed; task terminated.",
                                ]
                                return "\n".join(parts)
                            state["phase_transition_done"] = True
                        state["next_action"] = "phase2"
                        state["retryable"] = False
                    elif not has_follow_up:
                        state["task_done"] = True
                        state["terminal_reason"] = PHASE1_COMPLETE_REASON
                        state["next_action"] = "stop"
                        state["retryable"] = False
                elif phase == 2:
                    state["phase2_completed"] = True
                    state["task_done"] = True
                    state["terminal_reason"] = PHASE2_COMPLETE_REASON
                    state["next_action"] = "stop"
                    state["retryable"] = False
            elif not state.get("_budget_terminal_after_submit", False):
                state["next_action"] = "retry_submit"
                state["retryable"] = True
            parts = [raw.replace("[exec_err_flg] ", "")]
            if data.get("reward", 0):
                parts.append(f"Reward: {data['reward']}")
            if data.get("has_follow_up") and not state.get("phase2_skipped_due_budget"):
                parts.append(f"Follow-up question: {data.get('follow_up_query', '')}")
            elif data.get("has_follow_up") and state.get("phase2_skipped_due_budget"):
                parts.append("Phase 2 skipped because the final submit exhausted the budget.")
            return "\n".join(parts)
        return await run_tool("submit_sql", args, op)

    all_tools = [execute_sql, get_schema, search_semantic_context, get_table_schema, get_knowledge, get_all_column_meanings, get_column_meaning,
                 get_all_external_knowledge_names, get_knowledge_definition,
                 get_all_knowledge_definitions, ask_user, submit_sql]
    by_name = {item.name: item for item in all_tools}
    profile = state.get("tool_profile")
    if profile is None:
        from shared.tool_profiles import resolve_tool_profile
        profile = resolve_tool_profile(mode).as_dict()
        state["tool_profile"] = profile
    selected = [by_name[name] for name in profile["tools"]]
    return create_sdk_mcp_server(name="bird", version="1.0.0", tools=selected), [f"mcp__bird__{t.name}" for t in selected]
