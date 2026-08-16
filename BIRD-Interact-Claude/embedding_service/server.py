"""Batch Qwen/Qwen3-Embedding-0.6B service used by semantic search/import."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


MODEL_NAME = os.getenv("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")
DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))
MAX_TEXTS = 128

app = FastAPI(title="BIRD-Interact Qwen Embedding Service", version="1.0.0")


class EmbedRequest(BaseModel):
    texts: list[str]


class Embedder:
    def __init__(self) -> None:
        self._model: Any = None
        self._tokenizer: Any = None
        self._error: str = ""

    def _load(self) -> None:
        if self._model is not None:
            return
        if self._error:
            raise RuntimeError(self._error)
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
            # Left padding makes the final hidden state the final token for
            # every sequence in the batch, matching Qwen's retrieval examples.
            if hasattr(self._tokenizer, "padding_side"):
                self._tokenizer.padding_side = "left"
            self._model = AutoModel.from_pretrained(MODEL_NAME)
            self._model.eval()
            self._torch = torch
        except Exception as exc:  # pragma: no cover - exercised by deployment
            self._error = f"unable to load {MODEL_NAME}: {type(exc).__name__}: {exc}"
            raise RuntimeError(self._error) from exc

    def encode(self, texts: list[str]) -> list[list[float]]:
        self._load()
        torch = self._torch
        encoded = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            return_tensors="pt",
        )
        with torch.inference_mode():
            output = self._model(**encoded)
            vectors = output.last_hidden_state[:, -1, :]
            vectors = torch.nn.functional.normalize(vectors, p=2, dim=1)
        if vectors.shape[1] < DIMENSIONS:
            raise RuntimeError(
                f"model returned {vectors.shape[1]} dimensions; expected at least {DIMENSIONS}"
            )
        vectors = vectors[:, :DIMENSIONS]
        vectors = torch.nn.functional.normalize(vectors, p=2, dim=1)
        return vectors.cpu().tolist()


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
