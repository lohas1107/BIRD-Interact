"""Claude Agent SDK runtime for task-scoped interactive sessions."""

from __future__ import annotations

import asyncio
import dataclasses
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
    def _serialize(message: Any) -> dict:
        if dataclasses.is_dataclass(message):
            data = dataclasses.asdict(message)
        else:
            data = {"repr": repr(message)}
        data["type"] = type(message).__name__
        return data

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
        turn_events = [{"type": "user_message", "message": message[:1200]}]
        async for sdk_message in client.receive_response():
            turn_events.append(self._serialize(sdk_message))
            if isinstance(sdk_message, AssistantMessage):
                texts = [block.text for block in sdk_message.content if isinstance(block, TextBlock)]
                if texts:
                    final_text = "\n".join(texts)
            elif isinstance(sdk_message, ResultMessage) and sdk_message.result:
                final_text = sdk_message.result

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
