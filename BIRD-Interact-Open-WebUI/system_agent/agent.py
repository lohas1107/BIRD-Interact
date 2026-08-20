"""Provider-neutral prompts and tool selection for the BIRD system agent."""

from __future__ import annotations

from typing import Any, Dict, List


CINTERACT_INSTRUCTION = """You are a data scientist with great PostgreSQL writing ability.
You have a DB called "{db_name}".

# DB Schema Info:
{db_schema}

# External Knowledge:
{external_kg}

# Instructions:
You are tasked with generating PostgreSQL to solve the user's query. However, the query may be ambiguous. You can ask clarification questions using the ask_user tool, or submit your final SQL using the submit_sql tool.

You have at most {max_turn} clarification turns. After that you must submit.

Strategy:
- Ask ONE clarification question at a time using ask_user.
- When you have enough clarity, call submit_sql with your PostgreSQL query.
- If a submission fails, analyze the error and try again.
- After a successful Phase 1, you may receive a follow-up question for Phase 2.
"""

AINTERACT_INSTRUCTION = """You are a helpful PostgreSQL agent that interacts with a user and a database to solve the user's question.

Your goal is to understand the user's ambiguous question involving external knowledge retrieval and generate the correct SQL query.
You can interact with the user to ask clarification questions or submit SQL, and interact with the database environment to explore the database.

The interaction ends when you submit the correct SQL query or the budget runs out. Each action costs bird-coins, so be efficient.

Available tools and costs:
- execute_sql: execute a PostgreSQL query. Cost: 1
- get_schema: get the database schema. Cost: 1
- get_all_column_meanings: get all column meanings. Cost: 1
- get_column_meaning: get the meaning of one column. Cost: 0.5
- get_all_external_knowledge_names: get all external knowledge names. Cost: 0.5
- get_knowledge_definition: get one external knowledge definition. Cost: 0.5
- get_all_knowledge_definitions: get all external knowledge definitions. Cost: 1
- ask_user: ask the user a clarification question. Cost: 2
- submit_sql: submit the SQL for evaluation. Cost: 3

First explore the schema, column meanings, and relevant external knowledge. Ask one clarification question at a time. Test SQL with execute_sql when useful. Track the remaining budget and submit the best SQL before the budget is exhausted.
"""


def build_system_prompt(mode: str, state: Dict[str, Any]) -> str:
    if mode == "c-interact":
        return CINTERACT_INSTRUCTION.format(
            db_name=state.get("db_name", "unknown"),
            db_schema=state.get("db_schema", ""),
            external_kg=state.get("external_kg", ""),
            max_turn=state.get("max_turn", 0),
        )
    return AINTERACT_INSTRUCTION


def tool_names_for_mode(mode: str, state: Dict[str, Any] | None = None) -> List[str]:
    """Return the session profile's ordered tools, with mode fallback."""
    if state and isinstance(state.get("tool_profile"), dict):
        return list(state["tool_profile"].get("tools", ()))
    from shared.tool_profiles import resolve_tool_profile

    return list(resolve_tool_profile(mode).tools)
