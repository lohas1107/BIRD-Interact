"""Prompt rendering and profile-aware tool selection for the system agent."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from shared.agent_profiles import (
    AgentProfile,
    _validate_prompt_template,
    normalize_agent_profile,
    resolve_agent_profile,
)
from system_agent.tools import tool_cost


def _cost_text(cost: float) -> str:
    if float(cost).is_integer():
        number = str(int(cost))
    else:
        number = str(cost).rstrip("0").rstrip(".")
    unit = "bird-coin" if cost == 1 else "bird-coins"
    return f"{number} {unit}"


def available_tools_text(tools: List[str] | tuple[str, ...]) -> str:
    """Render an ordered tool manifest for the ``available_tools`` field."""
    return "\n".join(f"- {name}: {_cost_text(tool_cost(name))}" for name in tools)


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
