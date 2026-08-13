"""Load and resolve evaluation-wide MCP tool profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from shared.config import settings


VALID_TOOLS = (
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

DEFAULT_PROFILES = {
    "a-interact": "a-interact-default",
    "c-interact": "c-interact-default",
}
DEFAULT_PROFILES_FILE = settings.project_root / "config" / "tool_profiles.json"


@dataclass(frozen=True)
class ToolProfile:
    name: str
    tools: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"name": self.name, "tools": list(self.tools)}


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate profile name: {key!r}")
        result[key] = value
    return result


def load_tool_profiles(path: str | Path) -> dict[str, tuple[str, ...]]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read tool profiles file {path}: {exc}") from exc
    try:
        raw = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid tool profiles file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("tool profiles JSON must be an object mapping names to tool lists")

    valid = set(VALID_TOOLS)
    profiles = {}
    for name, tools in raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("tool profile names must be non-empty strings")
        if not isinstance(tools, list):
            raise ValueError(f"tool profile {name!r} must be a JSON array")
        seen = set()
        for tool_name in tools:
            if not isinstance(tool_name, str):
                raise ValueError(f"tool profile {name!r} contains a non-string tool name")
            if tool_name not in valid:
                raise ValueError(f"tool profile {name!r} contains unknown tool {tool_name!r}")
            if tool_name in seen:
                raise ValueError(f"tool profile {name!r} contains duplicate tool {tool_name!r}")
            seen.add(tool_name)
        profiles[name] = tuple(tools)
    return profiles


def resolve_tool_profile(
    mode: str,
    profile_name: str | None = None,
    profiles_file: str | Path | None = None,
) -> ToolProfile:
    if mode not in DEFAULT_PROFILES:
        raise ValueError(f"tool profiles are not supported for mode {mode!r}")
    name = profile_name or DEFAULT_PROFILES[mode]
    profiles = load_tool_profiles(profiles_file or DEFAULT_PROFILES_FILE)
    if name not in profiles:
        raise ValueError(f"unknown tool profile {name!r}")
    return ToolProfile(name=name, tools=profiles[name])
