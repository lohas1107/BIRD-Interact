"""HTTP gateway for OpenAI text embeddings used by semantic search/import."""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from openai import OpenAI, RateLimitError
from pydantic import BaseModel


load_dotenv()

MODEL_NAME = os.getenv(
    "EMBEDDING_MODEL",
    os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
)
DIMENSIONS = int(
    os.getenv("EMBEDDING_DIMENSIONS", os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "1536"))
)
DEFAULT_API_BASE_URL = "https://api.openai.com/v1"
MAX_TEXTS = 128

app = FastAPI(title="BIRD-Interact OpenAI Embedding Service", version="2.0.0")


class EmbedRequest(BaseModel):
    texts: list[str]


class Embedder:
    def __init__(self) -> None:
        self._client: OpenAI | None = None
        self._error: str = ""

    def _load(self) -> None:
        if self._client is not None:
            return
        if self._error:
            raise RuntimeError(self._error)
        # Do not reuse the chat-model credential.  The aliases are accepted
        # for deployments that used the earlier explicit OpenAI naming, but
        # OPENAI_API_KEY is deliberately not a fallback.
        api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_EMBEDDING_API_KEY")
        if not api_key:
            self._error = "EMBEDDING_API_KEY is required"
            raise RuntimeError(self._error)
        base_url = (
            os.getenv("EMBEDDING_API_BASE_URL")
            or os.getenv("OPENAI_EMBEDDING_API_BASE_URL")
            or os.getenv("OPENAI_EMBEDDING_BASE_URL")
            or DEFAULT_API_BASE_URL
        )
        timeout = float(os.getenv("EMBEDDING_TIMEOUT", "60"))
        try:
            self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        except Exception as exc:  # pragma: no cover - exercised by deployment
            self._error = (
                f"unable to initialize embedding client: "
                f"{type(exc).__name__}: {exc}"
            )
            raise RuntimeError(self._error) from exc

    def encode(self, texts: list[str]) -> list[list[float]]:
        self._load()
        try:
            response = self._client.embeddings.create(
                model=MODEL_NAME,
                input=texts,
                dimensions=DIMENSIONS,
            )
        except RateLimitError:
            raise
        except Exception as exc:  # pragma: no cover - exercised by deployment
            raise RuntimeError(
                f"embedding request failed: {type(exc).__name__}: {exc}"
            ) from exc

        records = sorted(response.data, key=lambda item: item.index)
        vectors = [list(item.embedding) for item in records]
        if len(vectors) != len(texts) or any(len(vector) != DIMENSIONS for vector in vectors):
            actual_dimensions = len(vectors[0]) if vectors else 0
            raise RuntimeError(
                f"OpenAI returned {len(vectors)} vectors of dimension "
                f"{actual_dimensions}; expected {len(texts)} vectors of dimension {DIMENSIONS}"
            )
        return [[float(value) for value in vector] for vector in vectors]


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    return Embedder()


@app.post("/embed")
def embed(request: EmbedRequest):
    if not 1 <= len(request.texts) <= MAX_TEXTS:
        raise HTTPException(status_code=400, detail="texts must contain 1 through 128 items")
    if any(not isinstance(text, str) or not text.strip() for text in request.texts):
        raise HTTPException(status_code=400, detail="texts must contain non-empty strings")
    try:
        vectors = get_embedder().encode(request.texts)
    except RateLimitError as exc:
        response = getattr(exc, "response", None)
        response_headers = getattr(response, "headers", None)
        retry_after = response_headers.get("retry-after") if response_headers else None
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        raise HTTPException(status_code=429, detail=str(exc), headers=headers) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "embeddings": vectors,
        "model": MODEL_NAME,
        "dimensions": DIMENSIONS,
    }


@app.get("/health")
def health():
    embedder = get_embedder()
    try:
        embedder._load()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": "healthy",
        "service": "embedding",
        "model": MODEL_NAME,
        "dimensions": DIMENSIONS,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("EMBEDDING_SERVICE_PORT", "6003")),
    )
