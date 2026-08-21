"""System-agent package exports.

The exports are loaded lazily so the profile validator can inspect the tool
registry without creating a circular import through this package initializer.
"""

__all__ = [
    "available_tools_text",
    "build_system_prompt",
    "format_available_tools",
    "render_agent_prompt",
    "render_prompt",
    "render_system_prompt",
    "tool_names_for_mode",
]


def __getattr__(name):
    if name in __all__:
        from system_agent import agent

        return getattr(agent, name)
    raise AttributeError(name)
