"""Tool-aware prompts used by the Claude Agent SDK system agent."""

from system_agent.tools import TOOL_COSTS


def _tools(state: dict, mode: str | None = None) -> tuple[str, ...]:
    profile = state.get("tool_profile", {})
    if not profile and mode:
        from shared.tool_profiles import resolve_tool_profile
        return resolve_tool_profile(mode).tools
    return tuple(profile.get("tools", ()))


def _semantic_grounding_contract() -> tuple[str, ...]:
    """Rules that prevent plausible but semantically ungrounded SQL."""

    return (
        "- Treat the task's Knowledge source as authoritative. With a graph profile, record the canonical Knowledge ID; with a legacy profile, record the exact definition name returned by the knowledge tool.",
        "- Before drafting or submitting SQL, establish a semantic contract for the task.",
        "- Map every natural-language metric, classification label, event condition, and usability criterion to an explicit Knowledge ID or exact Knowledge definition.",
        "- Obtain every derived-metric formula, threshold, category mapping, and usable/event condition from that definition; follow DEPENDS_ON nodes with get_knowledge when a definition refers to another metric.",
        "- For named metrics and acronyms, search the exact term first. A semantically similar result, raw column, or plausible abbreviation is not a substitute.",
        "- Treat search output as candidate discovery only. Keep Knowledge results separate from table-schema results, confirm the exact definition, and then resolve its required columns and joins.",
        "- Do not invent formulas, thresholds, phase weights, category mappings, NULL rules, or join semantics. Do not replace a defined metric with one of its components.",
        "- Keep an internal mapping of phrase -> Knowledge ID/definition -> formula/threshold -> required columns/joins before drafting SQL. Do not call submit_sql while any required mapping is unresolved.",
        "- If a required definition or mapping cannot be found, ask one focused question with ask_user. The answer is returned synchronously in that tool call; incorporate it and continue. If ask_user is unavailable, do not submit a guessed query.",
        "- execute_sql is only a runtime check for SQL parsing/execution and returned rows; it is not semantic validation. Re-check every expression, filter, join, grouping, and output metric against the resolved Knowledge definitions before submit_sql.",
        "- Before submit_sql, verify the requested output columns and aliases, row grain, counted entity, join cardinality, filters, grouping, ordering direction, and LIMIT. The final SELECT must contain requested outputs, not diagnostic/helper columns.",
        "- Budget policy: reserve the cost of submit_sql and, when a follow-up exists, the next phase's submit. Non-submit tools are rejected when funds are insufficient. submit_sql is the only tool allowed one final overdraw; after that call the session is terminal, with no retry and no Phase 2, regardless of evaluation correctness.",
    )


def instruction_for(mode: str, state: dict) -> str:
    tools = _tools(state, mode)
    if mode == "a-interact":
        lines = [
            "You are a PostgreSQL agent solving a BIRD-Interact task.",
            "Use only the provided BIRD tools. Never inspect local files or use shell commands.",
            "",
            "Mandatory semantic grounding rules:",
            *_semantic_grounding_contract(),
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
                "Use get_table_schema for targeted table columns, descriptions, constraints, and joins; provide the task database_name and from_table, and add to_table when a join path is needed."
            )
        if "search_semantic_context" in tools:
            lines.append(
                "Use search_semantic_context first when the relevant Knowledge ID or column is unclear; it returns separately ranked Knowledge and table_schema results."
            )
        if "get_knowledge" in tools:
            lines.append(
                "Use get_knowledge with knowledge_id such as alien:10; request description, definition, provenance, or related_columns when needed, and use expand.depth/expand.nodes for DEPENDS_ON dependencies."
            )
        knowledge_tools = [name for name in tools if "knowledge" in name or "meaning" in name]
        if knowledge_tools:
            lines.append("Use the available semantic and knowledge tools when definitions are needed.")
        if "execute_sql" in tools:
            lines.append("After semantic grounding, use execute_sql only as an optional runtime check; successful execution is not semantic validation.")
        if "ask_user" in tools:
            lines.append("Use ask_user for any unresolved metric, definition, formula, threshold, or mapping. Ask one focused question; its answer is synchronous, so incorporate it and continue.")
        if "submit_sql" in tools:
            lines.extend([
                "Finish by calling submit_sql only after the semantic contract is complete; manage the budget so submission is the final required action.",
                "An expiring budget is never permission to guess. If submit_sql reports a terminal budget state, stop and do not call another tool.",
                "After a successful Phase 1 submission with a follow-up, continue with the returned follow-up only when the submit response says next_action=phase2; otherwise stop.",
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
            "Ask one focused clarification question at a time with ask_user; its answer is returned synchronously and should be used before continuing.",
            f'You have at most {state.get("max_turn", 0)} clarification turns.',
        ])
    if "execute_sql" in tools:
        lines.append("Use execute_sql only as an optional runtime check; successful execution is not semantic validation.")
    if "submit_sql" in tools:
        lines.extend([
            "Call submit_sql for the current phase only after the supplied schema and Knowledge support every requested metric and condition.",
            "Call submit_sql at most once during each session turn; after it returns, stop and wait for the orchestrator's next message.",
            "Never use prose as a substitute for calling submit_sql.",
        ])
    return "\n".join(lines)


def task_turn_instruction(mode: str, state: dict, user_query: str, budget: float | None = None) -> str:
    tools = set(_tools(state))
    lines = [f"User Query:\n{user_query}"]
    if mode == "a-interact" and budget is not None:
        lines.append(f"You have a budget of {budget:.1f} bird-coins.")
    if mode == "a-interact":
        lines.extend([
            "",
            "Follow the mandatory semantic-grounding contract in the system prompt before drafting or submitting SQL.",
            "Use the current turn to resolve definitions and schema, then verify output shape and joins before submit_sql.",
        ])
    if "get_schema" in tools:
        lines.append("Use get_schema if you need to inspect the database structure.")
    if "get_table_schema" in tools:
        lines.append("Use get_table_schema for targeted schema and join information; add to_table when you need join_paths.")
    if "search_semantic_context" in tools:
        lines.append("Use search_semantic_context to find relevant Knowledge definitions and columns before choosing graph IDs or table names.")
    if "get_knowledge" in tools:
        lines.append("Use get_knowledge with knowledge_id such as alien:10 when a dependency graph or definition is needed.")
    if any("knowledge" in name or "meaning" in name for name in tools):
        lines.append("Use the available semantic tools if you need domain definitions.")
    if "ask_user" in tools:
        if mode == "c-interact":
            lines.append(f'You have {state.get("max_turn", 0)} clarification turns; ask one focused question at a time with ask_user and use its synchronous answer.')
        else:
            lines.append("Use ask_user for any unresolved metric, definition, formula, threshold, or mapping; its answer is synchronous, so incorporate it before continuing.")
    if "execute_sql" in tools:
        lines.append("After semantic grounding, use execute_sql only as an optional runtime check; successful execution is not semantic validation.")
    if "submit_sql" in tools:
        lines.append("Call submit_sql only after every required metric and condition is grounded in Knowledge and the output grain, counted entity, joins, ordering, and requested columns are checked.")
    return "\n\n".join(lines)


def submission_turn_instruction(tools: tuple[str, ...], message: str) -> str:
    if "submit_sql" in tools:
        return (
            f"{message}\nUse the evaluation feedback and the existing semantic contract to revise the current-phase query, "
            "then call submit_sql once. If a definition is still genuinely ambiguous, ask one focused synchronous "
            "question before submitting."
        )
    return message
