"""Load, validate, and resolve Open WebUI agent profiles.

An agent profile is the complete, provider-neutral description of one system
agent configuration: the ordered tools it may use and the prompt template it
uses for a session.  The registry in :mod:`system_agent.tools` remains the
single source of truth for legal tool names and costs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from shared.config import settings


DEFAULT_PROFILES = {
    "a-interact": "a-interact-default",
    "c-interact": "c-interact-default",
}
DEFAULT_PROFILES_FILE = settings.project_root / "config" / "agent_profiles.json"

ALLOWED_PLACEHOLDERS = frozenset(
    {"db_name", "db_schema", "external_kg", "max_turn", "available_tools"}
)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class InvalidAgentProfile(ValueError):
    """Validation error used by the stable API error contract."""

    code = "INVALID_AGENT_PROFILE"

    def __init__(self, profile: Any, field: str | None, message: str):
        super().__init__(message)
        self.profile = profile
        self.field = field
        self.message = message

    def as_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "profile": self.profile,
            "field": self.field,
            "message": self.message,
        }


@dataclass(frozen=True)
class AgentProfile:
    """Resolved profile data.

    ``prompt_template`` is intentionally only included in an internal
    snapshot.  API responses and persisted task state should use
    :meth:`as_metadata` so raw prompt text is never serialized there.
    """

    name: str
    tools: tuple[str, ...]
    prompt_template: str
    prompt_file: str | None = field(default=None, compare=False, repr=False)

    def as_metadata(self) -> dict[str, Any]:
        return {"name": self.name, "tools": list(self.tools)}

    def as_snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tools": list(self.tools),
            "prompt_template": self.prompt_template,
        }

    # The resolved snapshot is the natural dictionary representation used by
    # the runner when it sends a profile to the system-agent service.
    def as_dict(self) -> dict[str, Any]:
        return self.as_snapshot()

    def __getitem__(self, key: str) -> Any:
        """Expose snapshot fields for callers treating a profile as a mapping."""
        if key == "name":
            return self.name
        if key == "tools":
            return list(self.tools)
        if key == "prompt_template":
            return self.prompt_template
        if key == "prompt_file":
            return self.prompt_file
        raise KeyError(key)


@dataclass(frozen=True)
class AgentProfilesConfig:
    """Parsed profile configuration, with small mapping conveniences."""

    defaults: Mapping[str, str]
    profiles: Mapping[str, AgentProfile]

    def __getitem__(self, key: str) -> Any:
        if key == "defaults":
            return self.defaults
        if key == "profiles":
            return self.profiles
        return self.profiles[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.profiles)

    def __len__(self) -> int:
        return len(self.profiles)

    def keys(self):
        return self.profiles.keys()

    def items(self):
        return self.profiles.items()

    def values(self):
        return self.profiles.values()


def valid_tool_names() -> tuple[str, ...]:
    """Return tool registry names in registry order."""
    # Keep imports local: settings construction must not import the tool
    # registry, and the registry itself imports shared configuration.
    from system_agent.tools import tool_names

    return tuple(tool_names())


VALID_TOOLS = valid_tool_names()


def _invalid(profile: Any, field: str | None, message: str) -> InvalidAgentProfile:
    return InvalidAgentProfile(profile=profile, field=field, message=message)


def _reject_duplicate_keys(pairs: list[tuple[Any, Any]]) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _validate_prompt_template(
    profile_name: Any,
    prompt_template: Any,
    field: str = "prompt_file",
) -> str:
    if not isinstance(prompt_template, str):
        raise _invalid(profile_name, field, "prompt template must be UTF-8 text")

    for match in _PLACEHOLDER_RE.finditer(prompt_template):
        placeholder = match.group(1)
        if placeholder not in ALLOWED_PLACEHOLDERS:
            raise _invalid(
                profile_name,
                field,
                f"unknown prompt placeholder {placeholder!r}",
            )

    # A double-brace expression that did not match the allowed placeholder
    # grammar is also rejected.  This avoids silently sending an unresolved
    # template to the model (fail closed).
    for fragment in re.findall(r"\{\{(.*?)\}\}", prompt_template, flags=re.DOTALL):
        if not re.fullmatch(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*", fragment):
            raise _invalid(
                profile_name,
                field,
                f"invalid prompt placeholder {{{{{fragment}}}}}",
            )

    # Reject unmatched brace markers as well.  Leaving any template syntax
    # unresolved is unsafe because the model would receive an ambiguous
    # prompt rather than a validated snapshot.
    stripped = _PLACEHOLDER_RE.sub("", prompt_template)
    if "{{" in stripped or "}}" in stripped:
        raise _invalid(profile_name, field, "invalid or unresolved prompt placeholder")

    return prompt_template


def _validate_tools(profile_name: Any, tools: Any) -> tuple[str, ...]:
    if not isinstance(profile_name, str) or not profile_name:
        raise _invalid(profile_name, "name", "agent profile names must be non-empty strings")
    if not isinstance(tools, (list, tuple)):
        raise _invalid(profile_name, "tools", "tools must be a JSON array")

    valid = set(valid_tool_names())
    seen: set[str] = set()
    resolved: list[str] = []
    for tool_name in tools:
        if not isinstance(tool_name, str):
            raise _invalid(profile_name, "tools", "tools must contain only strings")
        if tool_name not in valid:
            raise _invalid(profile_name, "tools", f"unknown tool {tool_name!r}")
        if tool_name in seen:
            raise _invalid(profile_name, "tools", f"duplicate tool {tool_name!r}")
        seen.add(tool_name)
        resolved.append(tool_name)
    return tuple(resolved)


def _prompt_path(config_path: Path, profile_name: str, prompt_file: Any) -> Path:
    if not isinstance(prompt_file, str) or not prompt_file:
        raise _invalid(profile_name, "prompt_file", "prompt_file must be a non-empty string")

    config_dir = config_path.resolve().parent
    relative = Path(prompt_file)
    if relative.is_absolute():
        raise _invalid(profile_name, "prompt_file", "prompt_file must be relative to the profile file")

    resolved = (config_dir / relative).resolve()
    try:
        resolved.relative_to(config_dir)
    except ValueError as exc:
        raise _invalid(
            profile_name,
            "prompt_file",
            "prompt_file must stay within the agent profile configuration directory",
        ) from exc
    return resolved


def _read_prompt(config_path: Path, profile_name: Any, prompt_file: Any) -> str:
    if not isinstance(profile_name, str) or not profile_name:
        raise _invalid(profile_name, "name", "agent profile names must be non-empty strings")
    prompt_path = _prompt_path(config_path, profile_name, prompt_file)
    try:
        # Empty files are valid templates; only the encoding/path/readability
        # are validated here.
        prompt_template = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _invalid(
            profile_name,
            "prompt_file",
            f"cannot read prompt_file {prompt_file!r}: {exc}",
        ) from exc
    return _validate_prompt_template(profile_name, prompt_template)


def _validate_config(raw: Any, config_path: Path) -> AgentProfilesConfig:
    if not isinstance(raw, dict):
        raise _invalid(None, "profiles", "agent profiles JSON must be an object")

    required = {"defaults", "profiles"}
    missing = required.difference(raw)
    if missing:
        field = sorted(missing)[0]
        raise _invalid(None, field, f"missing required configuration field {field!r}")
    extra = set(raw).difference(required)
    if extra:
        field = sorted(extra)[0]
        raise _invalid(None, field, f"unknown configuration field {field!r}")

    defaults = raw["defaults"]
    profiles_raw = raw["profiles"]
    if not isinstance(defaults, dict):
        raise _invalid(None, "defaults", "defaults must be an object mapping modes to profile names")
    if not isinstance(profiles_raw, dict):
        raise _invalid(None, "profiles", "profiles must be an object mapping names to profile definitions")

    defaults_result: dict[str, str] = {}
    for mode, profile_name in defaults.items():
        if not isinstance(mode, str) or not mode:
            raise _invalid(profile_name, "defaults", "default mode names must be non-empty strings")
        if not isinstance(profile_name, str) or not profile_name:
            raise _invalid(profile_name, "defaults", f"default for {mode!r} must be a profile name")
        defaults_result[mode] = profile_name

    profiles: dict[str, AgentProfile] = {}
    for profile_name, definition in profiles_raw.items():
        if not isinstance(profile_name, str) or not profile_name:
            raise _invalid(profile_name, "name", "agent profile names must be non-empty strings")
        if not isinstance(definition, dict):
            raise _invalid(profile_name, "profiles", "profile definition must be an object")
        required_profile = {"prompt_file", "tools"}
        missing_profile = required_profile.difference(definition)
        if missing_profile:
            field = sorted(missing_profile)[0]
            raise _invalid(profile_name, field, f"missing required profile field {field!r}")
        extra_profile = set(definition).difference(required_profile)
        if extra_profile:
            field = sorted(extra_profile)[0]
            raise _invalid(profile_name, field, f"profile field {field!r} is not supported")

        tools = _validate_tools(profile_name, definition["tools"])
        prompt_template = _read_prompt(config_path, profile_name, definition["prompt_file"])
        profiles[profile_name] = AgentProfile(
            name=profile_name,
            tools=tools,
            prompt_template=prompt_template,
            prompt_file=definition["prompt_file"],
        )

    for mode, profile_name in defaults_result.items():
        if profile_name not in profiles:
            raise _invalid(
                profile_name,
                "defaults",
                f"default for mode {mode!r} points to unknown agent profile {profile_name!r}",
            )

    return AgentProfilesConfig(defaults=defaults_result, profiles=profiles)


def load_agent_profiles(path: str | Path) -> AgentProfilesConfig:
    """Read and validate an ``agent_profiles.json`` file."""
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _invalid(None, "profiles_file", f"cannot read agent profiles file {config_path}: {exc}") from exc

    try:
        raw = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _invalid(None, "profiles_file", f"invalid agent profiles file {config_path}: {exc}") from exc
    return _validate_config(raw, config_path)


def resolve_agent_profile(
    mode: str,
    profile_name: str | None = None,
    profiles_file: str | Path | None = None,
) -> AgentProfile:
    """Resolve one profile once, using the configured mode default if needed."""
    config = load_agent_profiles(profiles_file or DEFAULT_PROFILES_FILE)
    if profile_name is None:
        if mode not in config.defaults:
            raise _invalid(None, "defaults", f"no default agent profile configured for mode {mode!r}")
        profile_name = config.defaults[mode]
    if not isinstance(profile_name, str) or not profile_name:
        raise _invalid(profile_name, "name", "agent profile name must be a non-empty string")
    try:
        return config.profiles[profile_name]
    except KeyError as exc:
        raise _invalid(profile_name, "name", f"unknown agent profile {profile_name!r}") from exc


def normalize_agent_profile(mode: str, raw: Any) -> AgentProfile:
    """Validate a complete API snapshot without consulting profile files.

    A caller selecting a custom profile must send ``name``, ``tools`` and the
    resolved ``prompt_template``.  This keeps the system-agent service
    independent from the runner's local profile file while retaining a stable
    prompt snapshot for the session.
    """
    del mode  # profiles intentionally have no mode-specific restrictions
    if isinstance(raw, AgentProfile):
        return AgentProfile(
            raw.name,
            _validate_tools(raw.name, raw.tools),
            _validate_prompt_template(raw.name, raw.prompt_template, "prompt_template"),
            raw.prompt_file,
        )
    if not isinstance(raw, dict):
        raise _invalid(raw, "agent_profile", "agent_profile must be a complete snapshot object")

    required = {"name", "tools", "prompt_template"}
    missing = required.difference(raw)
    if missing:
        field = sorted(missing)[0]
        raise _invalid(raw.get("name"), field, f"agent_profile snapshot is missing {field!r}")
    extra = set(raw).difference(required)
    if extra:
        field = sorted(extra)[0]
        raise _invalid(raw.get("name"), field, f"agent_profile snapshot field {field!r} is not supported")

    name = raw["name"]
    tools = _validate_tools(name, raw["tools"])
    prompt_template = _validate_prompt_template(name, raw["prompt_template"], "prompt_template")
    return AgentProfile(name=name, tools=tools, prompt_template=prompt_template)


def resolve_session_agent_profile(
    mode: str,
    agent_profile: Any = None,
) -> AgentProfile:
    """Resolve a top-level session snapshot or the configured mode default."""
    if agent_profile is None:
        return resolve_agent_profile(mode)
    # Keep this helper convenient for callers that pass a session-state
    # wrapper, while the HTTP contract itself remains top-level
    # ``agent_profile``.  A metadata-only state value is deliberately rejected
    # because a custom session needs the prompt template too.
    if (
        isinstance(agent_profile, dict)
        and "agent_profile" in agent_profile
        and not {"name", "tools", "prompt_template"}.issubset(agent_profile)
    ):
        agent_profile = agent_profile["agent_profile"]
        if agent_profile is None:
            return resolve_agent_profile(mode)
    return normalize_agent_profile(mode, agent_profile)
