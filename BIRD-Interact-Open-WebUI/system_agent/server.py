"""System Agent Service (Port 6000).

Wraps the LLM agent behind a FastAPI endpoint so the orchestrator
talks to ALL three components through HTTP ports:
  - System Agent:    port 6000  (this service)
  - User Simulator:  port 6001
  - DB Environment:  port 6002

Supports two modes:
  - /chat: Simple prompt-in, text-out (used by c-interact)
  - /init_session + /run_session: Open WebUI-backed session runtime
"""

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared.config import settings
from shared.agent_profiles import InvalidAgentProfile
from system_agent.openwebui_runtime import OpenWebUIRuntime

logger = logging.getLogger(__name__)
app = FastAPI(title="BIRD-Interact System Agent", version="1.0.0")
runtime = OpenWebUIRuntime()


# ── Request / Response models ──────────────────────────────────────────────

class SessionInitRequest(BaseModel):
    task_id: str
    mode: str = "a-interact"
    state: Dict[str, Any] = Field(default_factory=dict)
    reset: bool = True
    # The runner sends a complete resolved snapshot.  If omitted, the service
    # resolves the configured default for the request mode.
    agent_profile: Optional[Any] = None


class SessionRunRequest(BaseModel):
    task_id: str
    message: str
    mode: str = "a-interact"


class SessionCleanupRequest(BaseModel):
    task_id: str
    mode: str = "a-interact"


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.post("/init_session")
async def init_session(req: SessionInitRequest):
    """Initialize a local BIRD session backed by Open WebUI."""
    if not runtime.available:
        raise HTTPException(status_code=503, detail=f"Open WebUI runtime unavailable: {runtime.error}")
    try:
        return await runtime.init_session(
            task_id=req.task_id,
            mode=req.mode,
            state=dict(req.state),
            reset=req.reset,
            agent_profile=req.agent_profile,
        )
    except InvalidAgentProfile as exc:
        raise HTTPException(status_code=400, detail=exc.as_detail()) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_AGENT_PROFILE",
                "profile": req.agent_profile,
                "field": "agent_profile",
                "message": str(exc),
            },
        ) from exc


@app.post("/run_session")
async def run_session(req: SessionRunRequest):
    """Run one model/tool turn on an existing task session."""
    if not runtime.available:
        raise HTTPException(status_code=503, detail=f"Open WebUI runtime unavailable: {runtime.error}")
    return await runtime.run_turn(
        task_id=req.task_id,
        mode=req.mode,
        message=req.message,
    )


@app.post("/cleanup_session")
async def cleanup_session(req: SessionCleanupRequest):
    """Release the in-process runtime state for one task."""
    return await runtime.cleanup_session(req.task_id, req.mode)


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "system_agent",
        "model": settings.system_agent_model,
        "runtime": "openwebui",
        "openwebui_available": runtime.available,
        "openwebui_base_url": settings.open_webui_base_url,
        "openwebui_error": runtime.error,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=settings.system_agent_port)
