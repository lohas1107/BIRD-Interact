"""Open WebUI-backed session runtime for a-interact and c-interact."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.config import settings
from shared.llm import get_client
from shared.agent_profiles import AgentProfile, resolve_session_agent_profile
from system_agent.agent import build_system_prompt
from system_agent.tools import execute_tool, has_tool, schemas_for, tool_cost

logger = logging.getLogger(__name__)

MAX_MODEL_TURNS = 60


@dataclass
class Session:
    task_id: str
    mode: str
    session_id: str
    state: Dict[str, Any]
    agent_profile: AgentProfile
    messages: List[Dict[str, Any]] = field(default_factory=list)


class OpenWebUIRuntime:
    """Keep BIRD state locally while using Open WebUI as the LLM gateway."""

    def __init__(self) -> None:
        self.available = True
        self.error = ""
        self._sessions: Dict[tuple[str, str], Session] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _preview(value: Any, limit: int = 4000) -> str:
        try:
            text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        except Exception:
            text = repr(value)
        return text if len(text) <= limit else text[:limit] + "...<truncated>"

    @staticmethod
    def _session_id(mode: str, task_id: str) -> str:
        return f"openwebui-{mode.replace('-', '_')}-{task_id}-{uuid.uuid4().hex[:8]}"

    def _new_state(
        self,
        mode: str,
        state: Optional[Dict[str, Any]],
        agent_profile: AgentProfile,
    ) -> Dict[str, Any]:
        result = dict(state or {})
        # Persist only metadata.  The raw template remains private to the
        # Session object and its first system message.
        result["agent_profile"] = agent_profile.as_metadata()
        result.setdefault("mode", mode)
        result.setdefault("tool_trajectory", [])
        result.setdefault("dialogue_history", [])
        result.setdefault("model_turns", 0)
        result.setdefault("total_reward", 0.0)
        result.setdefault("task_done", False)
        result.setdefault("phase1_completed", False)
        result.setdefault("phase2_completed", False)
        result.setdefault("current_phase", 1)
        events = result.setdefault("openwebui_events", result.get("adk_events", []))
        result["adk_events"] = events  # legacy response-key compatibility
        return result

    @staticmethod
    def _session_response(session: Session) -> Dict[str, Any]:
        return {
            "task_id": session.task_id,
            "mode": session.mode,
            "session_id": session.session_id,
            "runtime": "openwebui",
            "agent_profile": session.agent_profile.as_metadata(),
        }

    async def init_session(
        self,
        task_id: str,
        mode: str,
        state: Optional[Dict[str, Any]] = None,
        reset: bool = False,
        agent_profile: Any = None,
    ) -> Dict[str, Any]:
        async with self._lock:
            key = (mode, task_id)
            if key in self._sessions and not reset:
                session = self._sessions[key]
                # reset=False intentionally ignores all incoming state.  In
                # particular, a new profile cannot mutate a live session.
                return self._session_response(session)

            profile = resolve_session_agent_profile(mode, agent_profile)
            session_state = self._new_state(mode, state, profile)
            session = Session(
                task_id=task_id,
                mode=mode,
                session_id=self._session_id(mode, task_id),
                state=session_state,
                agent_profile=profile,
                messages=[
                    {
                        "role": "system",
                        "content": build_system_prompt(profile, session_state),
                    }
                ],
            )
            self._sessions[key] = session
            return self._session_response(session)

    async def cleanup_session(self, task_id: str, mode: str) -> Dict[str, Any]:
        """Drop one runtime session; repeated cleanup is harmless."""
        async with self._lock:
            self._sessions.pop((mode, task_id), None)
        return {"status": "ok", "task_id": task_id}

    @staticmethod
    def _parse_arguments(raw: Any) -> Dict[str, Any]:
        if raw is None or raw == "":
            return {}
        if isinstance(raw, dict):
            return raw
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("tool arguments must be a JSON object")
        return parsed

    @staticmethod
    def _tool_call_id(call: Dict[str, Any], index: int) -> str:
        return str(call.get("id") or f"tool-call-{index}")

    def _record(self, session: Session, event: Dict[str, Any]) -> None:
        session.state.setdefault("openwebui_events", []).append(event)
        session.state["adk_events"] = session.state["openwebui_events"]

    def _budget_result(
        self,
        session: Session,
        name: str,
        args: Dict[str, Any],
        result: str,
        before: Optional[float],
        after: Optional[float],
        *,
        cost: Optional[float] = None,
        is_error: bool = False,
    ) -> str:
        trajectory = session.state.setdefault("tool_trajectory", [])
        trajectory.append({
            "type": "tool",
            "tool": name,
            "args": args,
            "result": self._preview(result),
            "cost": tool_cost(name) if cost is None else cost,
            "budget_before": before,
            "budget_after": after,
            "is_error": is_error,
        })
        if after is not None and after >= 0:
            initial = session.state.get("initial_budget", 0.0)
            result += f"\n\n[SYSTEM NOTE: Remaining budget: {after:.1f}/{initial:.1f}]"
        if session.mode == "c-interact" and name == "ask_user":
            max_turn = session.state.get("max_turn", 0)
            asks_used = sum(
                1
                for item in trajectory
                if item.get("tool") == "ask_user" and not item.get("is_error", False)
            )
            result += f"\n\n[SYSTEM NOTE: Clarification turns remaining: {max(0, max_turn - asks_used)}/{max_turn}]"
        return result

    def _run_tool(self, session: Session, name: str, args: Dict[str, Any]) -> str:
        """Apply profile/turn controls, budget accounting, and dispatch."""
        session.state["_last_tool_is_error"] = False
        allowed = session.agent_profile.tools
        profile_name = session.agent_profile.name

        # Check the registry before the profile.  A typo is UNKNOWN_TOOL even
        # when the profile is restrictive; a known but unselected tool is a
        # profile violation.  Neither path reaches a service handler.
        if not has_tool(name):
            session.state["_last_tool_is_error"] = True
            return self._budget_result(
                session,
                name,
                args,
                f"UNKNOWN_TOOL: {name}",
                session.state.get("budget_remaining"),
                session.state.get("budget_remaining"),
                cost=0.0,
                is_error=True,
            )
        if allowed is not None and name not in allowed:
            session.state["_last_tool_is_error"] = True
            return self._budget_result(
                session,
                name,
                args,
                f"TOOL_NOT_ALLOWED: {name} is not enabled by profile {profile_name}",
                session.state.get("budget_remaining"),
                session.state.get("budget_remaining"),
                cost=0.0,
                is_error=True,
            )

        # A c-interact orchestrator turn may contain at most one submission.
        # Mark the first attempt before dispatch so a failed submission still
        # consumes the one call slot, while the second call never reaches DB.
        if session.mode == "c-interact" and name == "submit_sql":
            if (
                session.state.get("_submitted_this_phase", False)
                or session.state.get("_submitted_this_turn", False)
            ):
                session.state["_last_tool_is_error"] = True
                return self._budget_result(
                    session,
                    name,
                    args,
                    "TOOL_CALL_LIMIT: submit_sql may be called at most once per c-interact turn",
                    session.state.get("budget_remaining"),
                    session.state.get("budget_remaining"),
                    cost=0.0,
                    is_error=True,
                )
            session.state["_submitted_this_phase"] = True
            session.state["_submitted_this_turn"] = True

        cost = tool_cost(name)
        session.state["_last_tool_dispatch_error"] = False
        budget = session.state.get("budget_remaining")
        before = budget
        if budget is not None and budget < cost:
            if name != "submit_sql":
                session.state["_last_tool_is_error"] = True
                return self._budget_result(
                    session,
                    name,
                    args,
                    f"Budget exhausted ({budget:.1f} remaining). You MUST call submit_sql now with your best SQL.",
                    before,
                    budget,
                    cost=cost,
                    is_error=True,
                )
            session.state["budget_remaining"] = -1
        elif budget is not None:
            remaining = budget - cost
            session.state["budget_remaining"] = -1 if name == "submit_sql" and remaining <= 0 else remaining

        after = session.state.get("budget_remaining")
        result = execute_tool(
            name,
            args,
            session.task_id,
            session.state,
            allowed_tools=allowed,
        )
        is_error = bool(session.state.get("_last_tool_dispatch_error", False))
        session.state["_last_tool_is_error"] = is_error
        return self._budget_result(
            session,
            name,
            args,
            result,
            before,
            after,
            cost=cost,
            is_error=is_error,
        )

    async def run_turn(
        self,
        task_id: str,
        mode: str,
        message: str,
        **_: Any,
    ) -> Dict[str, Any]:
        key = (mode, task_id)
        if key not in self._sessions:
            await self.init_session(task_id=task_id, mode=mode, state={}, reset=False)
        session = self._sessions[key]

        # c-interact deliberately permits one submission per orchestrator turn.
        if mode == "c-interact":
            session.state["_submitted_this_phase"] = False
            session.state["_submitted_this_turn"] = False
        session.messages.append({"role": "user", "content": message})
        self._record(session, {"type": "user_message", "message": self._preview(message, 1200)})

        response_text = ""
        last_tool_result = ""
        client = get_client()
        profile_tools = session.agent_profile.tools
        tool_schemas = schemas_for(profile_tools)

        while True:
            if session.state.get("task_done"):
                response_text = response_text or "Task completed."
                break
            if session.state.get("budget_remaining") is not None and session.state.get("budget_remaining") < 0:
                response_text = response_text or "Budget exhausted. Task ended."
                break
            turns = session.state.get("model_turns", 0) + 1
            session.state["model_turns"] = turns
            if turns > MAX_MODEL_TURNS:
                response_text = "Maximum interaction turns reached. Task ended."
                break

            assistant = await client.chat(
                session.messages,
                model_name=settings.system_agent_model,
                temperature=0.0,
                max_tokens=settings.open_webui_max_tokens,
                tools=tool_schemas,
            )
            content = assistant.get("content") or ""
            if not isinstance(content, str):
                content = self._preview(content)
            calls = assistant.get("tool_calls") or []
            assistant_message: Dict[str, Any] = {"role": "assistant", "content": content}
            if calls:
                assistant_message["tool_calls"] = calls
            session.messages.append(assistant_message)
            self._record(session, {
                "type": "assistant_message",
                "content": self._preview(content),
                "tool_calls": self._preview(calls),
            })
            response_text = content or response_text

            if not calls:
                break

            stop_after_submit = False
            for index, call in enumerate(calls):
                function = call.get("function") or {}
                name = function.get("name", "")
                try:
                    args = self._parse_arguments(function.get("arguments", "{}"))
                    result = self._run_tool(session, name, args)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    args = {}
                    result = f"Invalid JSON arguments for {name}: {exc}"
                    session.state["_last_tool_is_error"] = True
                    result = self._budget_result(
                        session,
                        name,
                        args,
                        result,
                        session.state.get("budget_remaining"),
                        session.state.get("budget_remaining"),
                        cost=0.0,
                        is_error=True,
                    )

                is_error = bool(session.state.get("_last_tool_is_error", False))

                call_id = self._tool_call_id(call, index)
                self._record(session, {
                    "type": "tool_call",
                    "tool": name,
                    "tool_call_id": call_id,
                    "args": args,
                    "is_error": is_error,
                })
                session.messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": result,
                })
                self._record(session, {
                    "type": "tool_result",
                    "tool": name,
                    "tool_call_id": call_id,
                    "result": self._preview(result),
                    "is_error": is_error,
                })
                if is_error:
                    self._record(session, {
                        "type": "tool_error",
                        "tool": name,
                        "tool_call_id": call_id,
                        "error": self._preview(result),
                    })
                last_tool_result = result
                if name == "submit_sql":
                    if mode == "c-interact":
                        session.state["_submitted_this_phase"] = True
                        stop_after_submit = True
                    elif session.state.get("task_done"):
                        stop_after_submit = True

            if stop_after_submit:
                response_text = last_tool_result or response_text
                break

        return {
            "task_id": task_id,
            "mode": mode,
            "session_id": session.session_id,
            "response": response_text,
            "state": session.state,
            "runtime": "openwebui",
        }
