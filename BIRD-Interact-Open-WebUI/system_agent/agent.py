"""Prompt rendering and profile-aware tool selection for the system agent."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from shared.agent_profiles import (
    AgentProfile,
    _validate_prompt_template,
    normalize_agent_profile,
    resolve_agent_profile,
)


_TOOL_PROMPT_ORDER = (
    "execute_sql",
    "get_schema",
    "get_all_column_meanings",
    "get_column_meaning",
    "get_all_external_knowledge_names",
    "get_knowledge_definition",
    "get_all_knowledge_definitions",
    "ask_user",
    "submit_sql",
)

_TOOL_PROMPT_TEXT = {
    "execute_sql": "execute a PostgreSQL query. Cost: 1",
    "get_schema": "get the database schema. Cost: 1",
    "get_all_column_meanings": "get all column meanings. Cost: 1",
    "get_column_meaning": "get the meaning of one column. Cost: 0.5",
    "get_all_external_knowledge_names": "get all external knowledge names. Cost: 0.5",
    "get_knowledge_definition": "get one external knowledge definition. Cost: 0.5",
    "get_all_knowledge_definitions": "get all external knowledge definitions. Cost: 1",
    "ask_user": "ask the user a clarification question. Cost: 2",
    "submit_sql": "submit the SQL for evaluation. Cost: 3",
}


def available_tools_text(tools: List[str] | tuple[str, ...]) -> str:
    """Render the available-tools manifest in the original prompt wording."""
    selected = set(tools)
    return "\n".join(
        f"- {name}: {_TOOL_PROMPT_TEXT[name]}"
        for name in _TOOL_PROMPT_ORDER
        if name in selected
    )


def render_system_prompt(agent_profile: AgentProfile | Mapping[str, Any], state: Mapping[str, Any]) -> str:
    """Render the complete system prompt from one resolved profile template.

    Runtime context is only substituted when the profile template asks for
    it.  No mode-specific prompt or implicit context is appended here.
    """
    if isinstance(agent_profile, AgentProfile):
        profile = AgentProfile(
            agent_profile.name,
            agent_profile.tools,
            _validate_prompt_template(
                agent_profile.name,
                agent_profile.prompt_template,
                "prompt_template",
            ),
            agent_profile.prompt_file,
        )
    else:
        # A dictionary passed here must be a complete snapshot.  Validation is
        # repeated at this boundary so an unresolved template can never reach
        # the model even if this helper is used outside the HTTP runtime.
        profile = normalize_agent_profile(str(state.get("mode", "")), dict(agent_profile))

    values = {
        "db_name": state.get("db_name", ""),
        "db_schema": state.get("db_schema", ""),
        "external_kg": state.get("external_kg", ""),
        "max_turn": state.get("max_turn", ""),
        "available_tools": available_tools_text(profile.tools),
    }

    import re

    placeholder_re = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            # This should already have been rejected by profile validation;
            # keep rendering fail-closed for callers that construct profiles
            # directly.
            raise ValueError(f"unknown prompt placeholder {name!r}")
        value = values[name]
        return "" if value is None else str(value)

    return placeholder_re.sub(replace, profile.prompt_template)


# Descriptive alias for callers that want to emphasize the template flow.
render_agent_prompt = render_system_prompt
render_prompt = render_system_prompt
format_available_tools = available_tools_text


def build_system_prompt(agent_profile: AgentProfile | Mapping[str, Any], state: Dict[str, Any]) -> str:
    """Compatibility name used by the Open WebUI runtime."""
    return render_system_prompt(agent_profile, state)


def tool_names_for_mode(mode: str, state: Dict[str, Any] | None = None) -> List[str]:
    """Return the session profile's ordered tools, with configured fallback."""
    if state and isinstance(state.get("agent_profile"), dict):
        return list(state["agent_profile"].get("tools", ()))
    return list(resolve_agent_profile(mode).tools)
