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
    "get_all_column_meanings": 1.0,
    "get_column_meaning": 0.5,
    "get_all_external_knowledge_names": 0.5,
    "get_knowledge_definition": 0.5,
    "get_all_knowledge_definitions": 1.0,
    "ask_user": 2.0,
    "submit_sql": 3.0,
}


def _text(value: Any) -> dict:
    return {"content": [{"type": "text", "text": str(value)}]}


def _preview(value: Any, limit: int = 2000) -> str:
    value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return value if len(value) <= limit else value[:limit] + "...<truncated>"


def build_tool_server(state: dict, mode: str):
    """Build tools whose closures own one task's deterministic benchmark state."""

    async def call_service(port: int, path: str, payload: dict, timeout: float = 120.0) -> dict:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(f"http://127.0.0.1:{port}{path}", json=payload)
            response.raise_for_status()
            return response.json()

    async def run_tool(name: str, args: dict, operation) -> dict:
        if state.get("task_done"):
            return _text("Task is already complete. Do not call more tools.")
        if mode == "c-interact" and state.get("_submitted_this_turn"):
            return _text("SQL was already submitted in this turn. Stop and await feedback.")

        cost = TOOL_COSTS[name]
        before = state.get("budget_remaining")
        if mode == "a-interact" and before is not None:
            if before < cost and name != "submit_sql":
                return _text(
                    f"Budget exhausted ({before:.1f} remaining). You MUST call submit_sql now."
                )
            state["budget_remaining"] = before - cost if before >= cost else -1.0

        try:
            result = await operation()
        except Exception as exc:
            result = f"Tool error: {type(exc).__name__}: {exc}"

        # The reference runtime stops immediately after the final free submit.
        # Mirror that deterministically so an exhausted session cannot
        # keep making zero-cost submissions.
        if mode == "a-interact" and name == "submit_sql" and state.get("budget_remaining", 0) < 0:
            state["task_done"] = True

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
        state.setdefault("tool_trajectory", []).append(event)

        notes = []
        if mode == "a-interact" and state.get("budget_remaining", -1) >= 0:
            notes.append(
                f"Remaining budget: {state['budget_remaining']:.1f}/{state.get('initial_budget', 0):.1f}"
            )
        return _text(str(result) + ("\n\n[SYSTEM NOTE: " + "; ".join(notes) + "]" if notes else ""))

    task_id = state["task_id"]

    @tool("execute_sql", "Execute a read-only PostgreSQL query. Cost: 1 bird-coin.", {"sql": str})
    async def execute_sql(args):
        async def op():
            data = await call_service(settings.db_env_port, "/execute", {"task_id": task_id, "sql": args["sql"]})
            return data.get("result") if data.get("success") else f"SQL Error: {data.get('error', 'Execution failed')}"
        return await run_tool("execute_sql", args, op)

    @tool("get_schema", "Get the complete database schema. Cost: 1 bird-coin.", {})
    async def get_schema(args):
        async def op():
            return (await call_service(settings.db_env_port, "/schema", {"task_id": task_id})).get("schema", "")
        return await run_tool("get_schema", args, op)

    @tool("get_all_column_meanings", "Get all column descriptions. Cost: 1 bird-coin.", {})
    async def get_all_column_meanings(args):
        async def op():
            return (await call_service(settings.db_env_port, "/all_column_meanings", {"task_id": task_id})).get("column_meanings", "{}")
        return await run_tool("get_all_column_meanings", args, op)

    @tool("get_column_meaning", "Get one column description. Cost: 0.5 bird-coins.", {"table_name": str, "column_name": str})
    async def get_column_meaning(args):
        async def op():
            payload = {"task_id": task_id, "table_name": args["table_name"], "column_name": args["column_name"]}
            return (await call_service(settings.db_env_port, "/column_meaning", payload)).get("meaning", "Not found")
        return await run_tool("get_column_meaning", args, op)

    @tool("get_all_external_knowledge_names", "List external knowledge names. Cost: 0.5 bird-coins.", {})
    async def get_all_external_knowledge_names(args):
        async def op():
            data = await call_service(settings.db_env_port, "/knowledge_names", {"task_id": task_id})
            return json.dumps(data.get("names", []), ensure_ascii=False)
        return await run_tool("get_all_external_knowledge_names", args, op)

    @tool("get_knowledge_definition", "Get one external knowledge definition. Cost: 0.5 bird-coins.", {"knowledge_name": str})
    async def get_knowledge_definition(args):
        async def op():
            payload = {"task_id": task_id, "knowledge_name": args["knowledge_name"]}
            return (await call_service(settings.db_env_port, "/knowledge", payload)).get("knowledge", "Not found")
        return await run_tool("get_knowledge_definition", args, op)

    @tool("get_all_knowledge_definitions", "Get all external knowledge definitions. Cost: 1 bird-coin.", {})
    async def get_all_knowledge_definitions(args):
        async def op():
            return (await call_service(settings.db_env_port, "/knowledge", {"task_id": task_id})).get("knowledge", "[]")
        return await run_tool("get_all_knowledge_definitions", args, op)

    @tool("ask_user", "Ask one clarification question. Cost: 2 bird-coins.", {"question": str})
    async def ask_user(args):
        async def op():
            if mode == "c-interact":
                used = state.get("clarification_turns_used", 0)
                maximum = state.get("max_turn", 0)
                if used >= maximum:
                    return f"Clarification limit reached ({maximum}). Call submit_sql now."
                state["clarification_turns_used"] = used + 1
            data = await call_service(settings.user_sim_port, "/ask", {"task_id": task_id, "question": args["question"]})
            answer = data.get("answer", "No response from user.")
            state.setdefault("dialogue_history", []).extend([
                {"role": "agent", "content": args["question"]},
                {"role": "user", "content": answer},
            ])
            return answer
        result = await run_tool("ask_user", args, op)
        if mode == "c-interact":
            remaining = max(0, state.get("max_turn", 0) - state.get("clarification_turns_used", 0))
            result["content"][0]["text"] += f"\n\n[SYSTEM NOTE: Clarification turns remaining: {remaining}]"
        return result

    @tool("submit_sql", "Submit final PostgreSQL for evaluation. Cost: 3 bird-coins.", {"sql": str})
    async def submit_sql(args):
        async def op():
            data = await call_service(settings.db_env_port, "/submit", {"task_id": task_id, "sql": args["sql"]})
            state["_submitted_this_turn"] = True
            raw = data.get("message", "")
            state["_last_submit_raw"] = raw
            if data.get("passed"):
                state["total_reward"] = state.get("total_reward", 0.0) + data.get("reward", 0.0)
                phase = data.get("phase_completed")
                if phase == 1:
                    state["phase1_completed"] = True
                    state["current_phase"] = 2
                    if data.get("has_follow_up") and mode == "a-interact":
                        await call_service(settings.user_sim_port, "/phase_transition", {"task_id": task_id})
                    elif not data.get("has_follow_up"):
                        state["task_done"] = True
                elif phase == 2:
                    state["phase2_completed"] = True
                    state["task_done"] = True
            parts = [raw.replace("[exec_err_flg] ", "")]
            if data.get("reward", 0):
                parts.append(f"Reward: {data['reward']}")
            if data.get("has_follow_up"):
                parts.append(f"Follow-up question: {data.get('follow_up_query', '')}")
            return "\n".join(parts)
        return await run_tool("submit_sql", args, op)

    all_tools = [execute_sql, get_schema, get_all_column_meanings, get_column_meaning,
                 get_all_external_knowledge_names, get_knowledge_definition,
                 get_all_knowledge_definitions, ask_user, submit_sql]
    selected = all_tools if mode == "a-interact" else [ask_user, submit_sql]
    return create_sdk_mcp_server(name="bird", version="1.0.0", tools=selected), [f"mcp__bird__{t.name}" for t in selected]
