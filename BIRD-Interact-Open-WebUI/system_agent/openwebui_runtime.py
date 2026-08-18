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
from system_agent.agent import build_system_prompt, tool_names_for_mode
from system_agent.tools import TOOL_COSTS, execute_tool, schemas_for

logger = logging.getLogger(__name__)

MAX_MODEL_TURNS = 60


@dataclass
class Session:
    task_id: str
    mode: str
    session_id: str
    state: Dict[str, Any]
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

    def _new_state(self, mode: str, state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        result = dict(state or {})
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

    async def init_session(
        self,
        task_id: str,
        mode: str,
        state: Optional[Dict[str, Any]] = None,
        reset: bool = False,
    ) -> Dict[str, Any]:
        async with self._lock:
            key = (mode, task_id)
            if key in self._sessions and not reset:
                session = self._sessions[key]
                return {
                    "task_id": task_id,
                    "mode": mode,
                    "session_id": session.session_id,
                    "runtime": "openwebui",
                }

            session_state = self._new_state(mode, state)
            session = Session(
                task_id=task_id,
                mode=mode,
                session_id=self._session_id(mode, task_id),
                state=session_state,
                messages=[
                    {
                        "role": "system",
                        "content": build_system_prompt(mode, session_state),
                    }
                ],
            )
            self._sessions[key] = session
            return {
                "task_id": task_id,
                "mode": mode,
                "session_id": session.session_id,
                "runtime": "openwebui",
            }

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
    ) -> str:
        trajectory = session.state.setdefault("tool_trajectory", [])
        trajectory.append({
            "type": "tool",
            "tool": name,
            "args": args,
            "result": self._preview(result),
            "cost": TOOL_COSTS.get(name, 0.0),
            "budget_before": before,
            "budget_after": after,
        })
        if after is not None and after >= 0:
            initial = session.state.get("initial_budget", 0.0)
            result += f"\n\n[SYSTEM NOTE: Remaining budget: {after:.1f}/{initial:.1f}]"
        if session.mode == "c-interact" and name == "ask_user":
            max_turn = session.state.get("max_turn", 0)
            asks_used = sum(1 for item in trajectory if item.get("tool") == "ask_user")
            result += f"\n\n[SYSTEM NOTE: Clarification turns remaining: {max(0, max_turn - asks_used)}/{max_turn}]"
        return result

    def _run_tool(self, session: Session, name: str, args: Dict[str, Any]) -> str:
        cost = TOOL_COSTS.get(name, 0.0)
        budget = session.state.get("budget_remaining")
        before = budget
        if budget is not None and budget < cost:
            if name != "submit_sql":
                return self._budget_result(
                    session,
                    name,
                    args,
                    f"Budget exhausted ({budget:.1f} remaining). You MUST call submit_sql now with your best SQL.",
                    before,
                    budget,
                )
            session.state["budget_remaining"] = -1
        elif budget is not None:
            remaining = budget - cost
            session.state["budget_remaining"] = -1 if name == "submit_sql" and remaining <= 0 else remaining

        after = session.state.get("budget_remaining")
        result = execute_tool(name, args, session.task_id, session.state)
        return self._budget_result(session, name, args, result, before, after)

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
        session.messages.append({"role": "user", "content": message})
        self._record(session, {"type": "user_message", "message": self._preview(message, 1200)})

        response_text = ""
        last_tool_result = ""
        client = get_client()
        tool_schemas = schemas_for(tool_names_for_mode(mode))

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
                    result = self._budget_result(session, name, args, result, None, session.state.get("budget_remaining"))

                call_id = self._tool_call_id(call, index)
                self._record(session, {
                    "type": "tool_call",
                    "tool": name,
                    "tool_call_id": call_id,
                    "args": args,
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
