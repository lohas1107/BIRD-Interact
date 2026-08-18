"""Open WebUI backend client for OpenAI-compatible local models."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from shared.config import settings

logger = logging.getLogger(__name__)


def _endpoint() -> str:
    return f"{settings.open_webui_base_url.rstrip('/')}/api/chat/completions"


def _headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.open_webui_api_key:
        headers["Authorization"] = f"Bearer {settings.open_webui_api_key}"
    return headers


def _parse_response(response: httpx.Response) -> Dict[str, Any]:
    if response.status_code >= 400:
        detail = response.text[:1000]
        raise RuntimeError(
            f"Open WebUI request failed ({response.status_code}): {detail}"
        )
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError("Open WebUI returned non-JSON response") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Open WebUI returned an invalid response object")
    return payload


def _message_from_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise RuntimeError(f"Open WebUI response has no choices: {payload!r}")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise RuntimeError("Open WebUI response has an invalid message")
    return message


def _payload(
    messages: List[Dict[str, Any]],
    model_name: str,
    temperature: float,
    max_tokens: int,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    return body


class OpenWebUIClient:
    """Small async/sync client that keeps provider details out of BIRD code."""

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model_name: str,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        body = _payload(
            messages,
            model_name,
            temperature,
            max_tokens or settings.open_webui_max_tokens,
            tools,
        )
        timeout = httpx.Timeout(settings.open_webui_timeout)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(_endpoint(), headers=_headers(), json=body)
        return _message_from_response(_parse_response(response))

    def chat_sync(
        self,
        messages: List[Dict[str, Any]],
        model_name: str,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        body = _payload(
            messages,
            model_name,
            temperature,
            max_tokens or settings.open_webui_max_tokens,
        )
        with httpx.Client(
            timeout=settings.open_webui_timeout,
            trust_env=False,
        ) as client:
            response = client.post(_endpoint(), headers=_headers(), json=body)
        return _message_from_response(_parse_response(response))


_client = OpenWebUIClient()


def call_llm(
    messages: list,
    model_name: str = None,
    temperature: float = 0,
    max_tokens: int = 1024,
) -> str:
    """Synchronous text completion used by the user simulator."""
    message = _client.chat_sync(
        messages,
        model_name=model_name or settings.system_agent_model,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    content = message.get("content") or ""
    return content if isinstance(content, str) else str(content)


def get_client() -> OpenWebUIClient:
    return _client
