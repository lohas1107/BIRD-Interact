"""Claude Agent SDK system-agent service (port 6000)."""

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared.config import settings
from system_agent.claude_runtime import ClaudeRuntime

app = FastAPI(title="BIRD-Interact Claude System Agent", version="2.0.0")
runtime = ClaudeRuntime()


class SessionInitRequest(BaseModel):
    task_id: str
    mode: str = "a-interact"
    state: dict[str, Any] = Field(default_factory=dict)
    reset: bool = True


class SessionRunRequest(BaseModel):
    task_id: str
    message: str
    mode: str = "a-interact"


class SessionCleanupRequest(BaseModel):
    task_id: str
    mode: str = "a-interact"


@app.post("/init_session")
async def init_session(req: SessionInitRequest):
    try:
        return await runtime.init_session(req.task_id, req.mode, req.state, req.reset)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/run_session")
async def run_session(req: SessionRunRequest):
    try:
        return await runtime.run_turn(req.task_id, req.mode, req.message)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/cleanup_session")
async def cleanup_session(req: SessionCleanupRequest):
    return await runtime.cleanup_session(req.task_id, req.mode)


@app.on_event("shutdown")
async def shutdown():
    await runtime.close()


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "system_agent",
        "model": settings.system_agent_model,
        "claude_available": runtime.available,
        "claude_error": runtime.error,
    }
