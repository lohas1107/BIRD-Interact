"""Claude Agent SDK runtime for task-scoped interactive sessions."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from shared.config import settings
from system_agent.agent import instruction_for
from system_agent.tools import build_tool_server


class ClaudeRuntime:
    """Own Claude SDK sessions and retain only provider-neutral events.

    The SDK emits a sizeable lifecycle stream for every turn.  It contains
    initialization data, rate-limit notifications, token accounting, signed
    thinking blocks, and several provider-specific IDs.  None of those are
    part of the evaluator result contract, so the session state stores a
    compact logical event stream instead:

    ``user_message``, ``assistant_text``, ``tool_call``, ``tool_response``,
    and ``final_response``.
    """

    _TEXT_LIMIT = 4000
    _USER_MESSAGE_LIMIT = 1200
    _TOOL_PREFIX = "mcp__bird__"

    def __init__(self) -> None:
        self.available = True
        self.error = ""
        self._sessions: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _auth_guard() -> None:
        if os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is set; refusing to risk pay-as-you-go API charges. "
                "Unset it and authenticate Claude Code with /login."
            )

    async def init_session(self, task_id: str, mode: str, state: dict | None = None,
                           reset: bool = False) -> dict:
        self._auth_guard()
        key = (mode, task_id)
        async with self._lock:
            if key in self._sessions and not reset:
                return {"task_id": task_id, "mode": mode, "session_id": task_id, "claude_available": True}
            if key in self._sessions:
                await self._sessions[key]["client"].disconnect()

            session_state = dict(state or {})
            session_state.setdefault("tool_trajectory", [])
            session_state.setdefault("agent_events", [])
            session_state.setdefault("total_reward", 0.0)
            session_state.setdefault("phase1_completed", False)
            session_state.setdefault("phase2_completed", False)
            session_state.setdefault("task_done", False)
            session_state.setdefault("phase_transition_done", False)
            session_state.setdefault("phase_transition_failed", False)
            session_state.setdefault("budget_exhausted", False)
            session_state.setdefault("budget_overdrawn", False)
            session_state.setdefault("phase2_skipped_due_budget", False)
            session_state.setdefault("terminal_reason", None)
            session_state.setdefault("next_action", "resolve_and_submit")
            session_state.setdefault("retryable", False)
            server, allowed_tools = build_tool_server(session_state, mode)
            options = ClaudeAgentOptions(
                model=settings.system_agent_model,
                system_prompt=instruction_for(mode, session_state),
                tools=[],
                mcp_servers={"bird": server},
                strict_mcp_config=True,
                allowed_tools=allowed_tools,
                disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task"],
                permission_mode="dontAsk",
                setting_sources=[],
                skills=[],
                max_turns=60,
                cwd=str(settings.project_root),
            )
            client = ClaudeSDKClient(options=options)
            await client.connect()
            self._sessions[key] = {"client": client, "state": session_state}
            return {"task_id": task_id, "mode": mode, "session_id": task_id, "claude_available": True}

    @staticmethod
    def _value(value: Any, key: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    @classmethod
    def _preview(cls, value: Any, limit: int | None = None) -> str:
        """Convert an event value to bounded, JSON-friendly text."""
        limit = cls._TEXT_LIMIT if limit is None else limit
        try:
            if isinstance(value, (dict, list)):
                text = json.dumps(value, ensure_ascii=False, default=str)
            elif value is None:
                text = ""
            else:
                text = str(value)
        except Exception:
            text = repr(value)
        return text if len(text) <= limit else text[:limit] + "...<truncated>"

    @classmethod
    def _tool_name(cls, name: Any) -> str:
        name = str(name or "tool")
        return name[len(cls._TOOL_PREFIX):] if name.startswith(cls._TOOL_PREFIX) else name

    @classmethod
    def _tool_response_text(cls, value: Any) -> str:
        """Keep the useful tool result while dropping SDK result wrappers."""
        if isinstance(value, list):
            texts = []
            for item in value:
                text = cls._value(item, "text")
                if text:
                    texts.append(str(text))
            if texts:
                return cls._preview("\n".join(texts))
        return cls._preview(value)

    @classmethod
    def _project_block(cls, block: Any, tool_names: dict[str, str]) -> dict | None:
        """Project one Claude content block into a logical event."""
        text = cls._value(block, "text")
        if text:
            return {"type": "assistant_text", "text": cls._preview(text)}

        # Thinking is useful only when it contains readable text.  Its SDK
        # signature is deliberately not copied into the result.
        thinking = cls._value(block, "thinking")
        if thinking:
            return {"type": "assistant_text", "text": cls._preview(thinking)}

        tool_use_id = cls._value(block, "tool_use_id")
        if tool_use_id is not None:
            tool_use_id = str(tool_use_id)
            name = tool_names.get(tool_use_id, cls._tool_name(cls._value(block, "name", "tool")))
            return {
                "type": "tool_response",
                "id": tool_use_id,
                "name": name,
                "response": cls._tool_response_text(cls._value(block, "content")),
            }

        block_name = cls._value(block, "name")
        block_input = cls._value(block, "input")
        if block_name is not None and block_input is not None:
            tool_use_id = cls._value(block, "id")
            tool_use_id = str(tool_use_id) if tool_use_id is not None else ""
            name = cls._tool_name(block_name)
            if tool_use_id:
                tool_names[tool_use_id] = name
            return {
                "type": "tool_call",
                "id": tool_use_id,
                "name": name,
                "args": block_input if isinstance(block_input, dict) else {},
            }

        return None

    @classmethod
    def _project_sdk_message(cls, message: Any, tool_names: dict[str, str]) -> list[dict]:
        """Project one SDK message; lifecycle-only messages yield no events."""
        message_type = cls._value(message, "type", type(message).__name__)
        if message_type == "AssistantMessage":
            content = cls._value(message, "content", [])
            content = content if isinstance(content, list) else []
            return [
                event
                for block in content
                if (event := cls._project_block(block, tool_names)) is not None
            ]

        if message_type == "UserMessage":
            # ToolResultBlock.content is the canonical response.  The SDK's
            # tool_use_result field is a duplicate envelope and is ignored.
            content = cls._value(message, "content", [])
            content = content if isinstance(content, list) else []
            return [
                event
                for block in content
                if cls._value(block, "tool_use_id") is not None
                and (event := cls._project_block(block, tool_names)) is not None
            ]

        if message_type == "ResultMessage":
            result = cls._value(message, "result")
            if result:
                return [{
                    "type": "final_response",
                    "text": cls._preview(result),
                    "final": True,
                }]
        return []

    async def run_turn(self, task_id: str, mode: str, message: str) -> dict:
        key = (mode, task_id)
        if key not in self._sessions:
            await self.init_session(task_id, mode, {}, False)
        entry = self._sessions[key]
        state = entry["state"]
        state["_submitted_this_turn"] = False
        client: ClaudeSDKClient = entry["client"]
        await client.query(message)

        final_text = ""
        tool_names: dict[str, str] = {}
        turn_events = [{
            "type": "user_message",
            "message": self._preview(message, self._USER_MESSAGE_LIMIT),
        }]
        async for sdk_message in client.receive_response():
            turn_events.extend(self._project_sdk_message(sdk_message, tool_names))
            if isinstance(sdk_message, AssistantMessage):
                texts = [block.text for block in sdk_message.content if isinstance(block, TextBlock)]
                if texts:
                    final_text = "\n".join(texts)
            elif isinstance(sdk_message, ResultMessage) and sdk_message.result:
                final_text = sdk_message.result

        # ResultMessage is normally present, but retaining a final logical
        # event also makes the projection robust to an interrupted SDK turn.
        if final_text and not any(event.get("type") == "final_response" for event in turn_events):
            turn_events.append({
                "type": "final_response",
                "text": self._preview(final_text),
                "final": True,
            })

        state.setdefault("agent_events", []).extend(turn_events)
        return {
            "task_id": task_id,
            "mode": mode,
            "session_id": task_id,
            "response": final_text,
            "state": state,
            "claude_available": True,
        }

    async def cleanup_session(self, task_id: str, mode: str) -> dict:
        key = (mode, task_id)
        async with self._lock:
            entry = self._sessions.pop(key, None)
        if entry:
            await entry["client"].disconnect()
        return {"status": "ok", "task_id": task_id}

    async def close(self) -> None:
        entries = list(self._sessions.values())
        self._sessions.clear()
        for entry in entries:
            await entry["client"].disconnect()
