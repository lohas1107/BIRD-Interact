"""Small no-tools Claude Agent SDK call used by the user simulator."""

from __future__ import annotations

import os

import anyio
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

from shared.config import settings


async def _call(prompt: str, model_name: str, max_tokens: int) -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is set; refusing pay-as-you-go API authentication"
        )
    options = ClaudeAgentOptions(
        model=model_name,
        tools=[],
        allowed_tools=[],
        disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task"],
        permission_mode="dontAsk",
        setting_sources=[],
        skills=[],
        max_turns=1,
        cwd=str(settings.project_root),
        # Claude Code has no temperature option. Limit generated text through
        # the prompt-facing token budget where supported by the CLI.
        max_thinking_tokens=0,
    )
    texts = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            texts.extend(block.text for block in message.content if isinstance(block, TextBlock))
    return "\n".join(texts).strip()


def call_llm(messages: list, model_name: str | None = None, temperature: float = 0,
             max_tokens: int = 1024) -> str:
    """Synchronous compatibility wrapper; invoked inside a worker thread."""
    del temperature
    prompt = "\n\n".join(str(message.get("content", "")) for message in messages)
    return anyio.run(_call, prompt, model_name or settings.user_sim_model, max_tokens)
