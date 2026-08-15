"""Tool-aware prompts used by the Claude Agent SDK system agent."""

from system_agent.tools import TOOL_COSTS


def _tools(state: dict, mode: str | None = None) -> tuple[str, ...]:
    profile = state.get("tool_profile", {})
    if not profile and mode:
        from shared.tool_profiles import resolve_tool_profile
        return resolve_tool_profile(mode).tools
    return tuple(profile.get("tools", ()))


def instruction_for(mode: str, state: dict) -> str:
    tools = _tools(state, mode)
    if mode == "a-interact":
        lines = [
            "You are a PostgreSQL agent solving a BIRD-Interact task.",
            "Use only the provided BIRD tools. Never inspect local files or use shell commands.",
        ]
        if tools:
            lines.extend(["", "Available tool costs:"])
            lines.extend(
                f"- {name}: {TOOL_COSTS[name]:g} bird-coin" +
                ("s" if TOOL_COSTS[name] != 1 else "")
                for name in tools
            )
        if "get_schema" in tools:
            lines.append("Use get_schema when database structure is needed.")
        if "get_table_schema" in tools:
            lines.append(
                "Use get_table_schema for targeted table columns, descriptions, constraints, and joins; provide database_name and from_table, and add to_table when a join path is needed."
            )
        knowledge_tools = [name for name in tools if "knowledge" in name or "meaning" in name]
        if knowledge_tools:
            lines.append("Use the available semantic and knowledge tools when definitions are needed.")
        if "execute_sql" in tools:
            lines.append("Test candidate SQL with execute_sql when useful.")
        if "ask_user" in tools:
            lines.append("Clarify genuine ambiguities with ask_user, one question at a time.")
        if "submit_sql" in tools:
            lines.extend([
                "Finish by calling submit_sql and stay within the task budget.",
                "When the budget is exhausted, submit your best SQL once if necessary and do not call any more tools.",
                "After a successful Phase 1 submission with a follow-up, continue with the returned follow-up and submit Phase 2.",
            ])
        return "\n".join(lines)

    lines = [
        "You are a data scientist with great PostgreSQL writing ability.",
        f'You have a DB called "{state.get("db_name", "")}".',
        "",
        "# DB Schema Info:",
        state.get("db_schema", ""),
        "",
        "# External Knowledge:",
        state.get("external_kg", ""),
        "",
        "# Instructions:",
        "Generate PostgreSQL for the user's query. The query may be ambiguous.",
    ]
    if "ask_user" in tools:
        lines.extend([
            "Ask one clarification question at a time with ask_user.",
            f'You have at most {state.get("max_turn", 0)} clarification turns.',
        ])
    if "execute_sql" in tools:
        lines.append("Use execute_sql to test candidate queries when useful.")
    if "submit_sql" in tools:
        lines.extend([
            "Call submit_sql with the final query, at most once during each session turn.",
            "After submit_sql returns, stop and wait for the next user message.",
            "Never use prose as a substitute for calling submit_sql.",
        ])
    return "\n".join(lines)


def task_turn_instruction(mode: str, state: dict, user_query: str, budget: float | None = None) -> str:
    tools = set(_tools(state))
    lines = [f"User Query:\n{user_query}"]
    if mode == "a-interact" and budget is not None:
        lines.append(f"You have a budget of {budget:.1f} bird-coins.")
    if "get_schema" in tools:
        lines.append("Use get_schema if you need to inspect the database structure.")
    if "get_table_schema" in tools:
        lines.append("Use get_table_schema for targeted schema and join information; add to_table when you need join_paths.")
    if any("knowledge" in name or "meaning" in name for name in tools):
        lines.append("Use the available semantic tools if you need domain definitions.")
    if "ask_user" in tools:
        if mode == "c-interact":
            lines.append(f'You have {state.get("max_turn", 0)} clarification turns; ask one question at a time with ask_user.')
        else:
            lines.append("Use ask_user to clarify genuine ambiguities.")
    if "execute_sql" in tools:
        lines.append("Use execute_sql to test SQL when useful.")
    if "submit_sql" in tools:
        lines.append("Call submit_sql with your final PostgreSQL query.")
    return "\n\n".join(lines)


def submission_turn_instruction(tools: tuple[str, ...], message: str) -> str:
    if "submit_sql" in tools:
        return f"{message}\nPlease fix the query and call submit_sql."
    return message
