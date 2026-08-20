"""Load and validate Open WebUI legacy tool profiles.

The profile file is deliberately small and provider-neutral: it contains the
ordered names of the nine legacy BIRD tools.  The Open WebUI tool registry is
the source of truth for the legal names, so this module never has a second
allowlist that can drift from the executor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.config import settings


DEFAULT_PROFILES = {
    "a-interact": "a-interact-default",
    "c-interact": "c-interact-default",
}
DEFAULT_PROFILES_FILE = settings.project_root / "config" / "tool_profiles.json"


class InvalidToolProfile(ValueError):
    """Validation error suitable for the stable HTTP API error contract."""

    code = "INVALID_TOOL_PROFILE"

    def __init__(self, profile: Any, tool: Any = None, message: str = "invalid tool profile"):
        super().__init__(message)
        self.profile = profile
        self.tool = tool
        self.message = message

    def as_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "profile": self.profile,
            "tool": self.tool,
            "message": self.message,
        }


@dataclass(frozen=True)
class ToolProfile:
    name: str
    tools: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "tools": list(self.tools)}


def valid_tool_names() -> tuple[str, ...]:
    """Return registry names in registry order.

    The import is local to keep this configuration module lightweight during
    settings construction and to make the registry the sole source of truth.
    """
    from system_agent.tools import tool_names

    return tuple(tool_names())


# Backwards-compatible export for callers that used the Claude-side helper.
# It is derived from the registry rather than maintained as an independent
# list of names.
VALID_TOOLS = valid_tool_names()


def _reject_duplicate_keys(pairs: list[tuple[Any, Any]]) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate profile name: {key!r}")
        result[key] = value
    return result


def _invalid(
    profile: Any,
    tool: Any = None,
    message: str = "invalid tool profile",
) -> InvalidToolProfile:
    return InvalidToolProfile(profile=profile, tool=tool, message=message)


def _validate_tools(profile_name: Any, tools: Any) -> tuple[str, ...]:
    if not isinstance(profile_name, str) or not profile_name:
        raise _invalid(profile_name, message="tool profile names must be non-empty strings")
    if not isinstance(tools, (list, tuple)):
        raise _invalid(profile_name, message=f"tool profile {profile_name!r} must be a JSON array")

    valid = set(valid_tool_names())
    seen: set[str] = set()
    resolved: list[str] = []
    for tool_name in tools:
        if not isinstance(tool_name, str):
            raise _invalid(
                profile_name,
                tool=tool_name,
                message=f"tool profile {profile_name!r} contains a non-string tool name",
            )
        if tool_name not in valid:
            raise _invalid(
                profile_name,
                tool=tool_name,
                message="tool is not supported by the Open WebUI P0 registry",
            )
        if tool_name in seen:
            raise _invalid(
                profile_name,
                tool=tool_name,
                message=f"tool profile {profile_name!r} contains duplicate tool {tool_name!r}",
            )
        seen.add(tool_name)
        resolved.append(tool_name)
    return tuple(resolved)


def load_tool_profiles(path: str | Path) -> dict[str, tuple[str, ...]]:
    """Read a profile file, rejecting malformed or ambiguous configuration."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read tool profiles file {path}: {exc}") from exc

    try:
        raw = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid tool profiles file {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("tool profiles JSON must be an object mapping names to tool lists")

    profiles: dict[str, tuple[str, ...]] = {}
    for name, tools in raw.items():
        try:
            profiles[name] = _validate_tools(name, tools)
        except InvalidToolProfile as exc:
            suffix = f": {exc.tool!r}" if exc.tool is not None else ""
            raise ValueError(f"{exc.message}{suffix}") from exc
    return profiles


def resolve_tool_profile(
    mode: str,
    profile_name: str | None = None,
    profiles_file: str | Path | None = None,
) -> ToolProfile:
    """Resolve a named profile once for an evaluation run."""
    if mode not in DEFAULT_PROFILES:
        raise ValueError(f"tool profiles are not supported for mode {mode!r}")
    name = profile_name or DEFAULT_PROFILES[mode]
    profiles = load_tool_profiles(profiles_file or DEFAULT_PROFILES_FILE)
    if name not in profiles:
        raise ValueError(f"unknown tool profile {name!r}")
    return ToolProfile(name=name, tools=profiles[name])


def normalize_tool_profile(
    mode: str,
    raw: Any,
    *,
    profiles_file: str | Path | None = None,
) -> ToolProfile:
    """Normalize a session snapshot or profile name into a validated profile.

    CLI evaluation passes an already-resolved ``{"name", "tools"}`` snapshot
    so custom profiles do not need to be re-read by the service.  A name-only
    value is also accepted for direct API users and is resolved from the
    default/configured profile file.
    """
    if isinstance(raw, ToolProfile):
        return ToolProfile(raw.name, _validate_tools(raw.name, raw.tools))
    if isinstance(raw, str):
        try:
            return resolve_tool_profile(mode, raw, profiles_file)
        except ValueError as exc:
            raise _invalid(raw, message=str(exc)) from exc
    if not isinstance(raw, dict):
        raise _invalid(raw, message="tool profile must be a profile name or object snapshot")

    name = raw.get("name")
    if "tools" not in raw:
        if isinstance(name, str) and name:
            try:
                return resolve_tool_profile(mode, name, profiles_file)
            except ValueError as exc:
                raise _invalid(name, message=str(exc)) from exc
        raise _invalid(name, message="tool profile snapshot must contain a non-empty name and tools")
    try:
        tools = _validate_tools(name, raw.get("tools"))
    except InvalidToolProfile:
        raise
    return ToolProfile(name=name, tools=tools)


def resolve_session_tool_profile(mode: str, state: dict[str, Any]) -> ToolProfile:
    """Resolve the profile carried by session state, falling back by mode."""
    if "tool_profile" not in state:
        try:
            return resolve_tool_profile(mode)
        except ValueError as exc:
            raise _invalid(DEFAULT_PROFILES.get(mode), message=str(exc)) from exc
    return normalize_tool_profile(mode, state["tool_profile"])
