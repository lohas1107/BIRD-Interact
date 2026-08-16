#!/usr/bin/env python3
"""Populate semantic graph embeddings through the local OpenAI HTTP gateway."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx
from neo4j import GraphDatabase


DIMENSIONS = 1536
DEFAULT_BATCH_SIZE = 8
MAX_BATCH_SIZE = 128
DEFAULT_MAX_RETRIES = 6
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
RETRY_BASE_SECONDS = 1.0
RETRY_MAX_SECONDS = 30.0


def load_documents(driver, database: str) -> list[dict[str, str]]:
    with driver.session(database=database) as session:
        records = session.run(
            """
            MATCH (n:SemanticSearch)
            WHERE n.search_text IS NOT NULL
            RETURN n.id AS id, n.search_text AS search_text
            ORDER BY n.id
            """
        )
        return [dict(record) for record in records]


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Return a bounded delay, preferring the gateway's Retry-After value."""

    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), RETRY_MAX_SECONDS)
            except ValueError:
                pass
    return min(RETRY_BASE_SECONDS * (2**attempt), RETRY_MAX_SECONDS)


def _request_embeddings(
    client: httpx.Client,
    embedding_url: str,
    texts: list[str],
    *,
    offset: int,
    max_retries: int,
) -> dict[str, Any]:
    """Request one embedding batch, retrying transient gateway/upstream errors."""

    endpoint = f"{embedding_url.rstrip('/')}/embed"
    for attempt in range(max_retries + 1):
        try:
            response = client.post(endpoint, json={"texts": texts})
        except httpx.RequestError:
            if attempt >= max_retries:
                raise
            time.sleep(_retry_delay(None, attempt))
            continue

        if response.status_code in RETRYABLE_STATUS_CODES:
            if attempt >= max_retries:
                response.raise_for_status()
            time.sleep(_retry_delay(response, attempt))
            continue

        response.raise_for_status()
        return response.json()

    raise RuntimeError(f"embedding request exhausted retries at offset {offset}")


def import_embeddings(
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
    neo4j_database: str,
    embedding_url: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    # Keep requests within the embedding service contract even when an
    # operator supplies a larger batch size on the command line.
    batch_size = min(batch_size, MAX_BATCH_SIZE)
    driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
    try:
        documents = load_documents(driver, neo4j_database)
        updated = 0
        with httpx.Client(timeout=120.0, trust_env=False) as client:
            with driver.session(database=neo4j_database) as session:
                for offset in range(0, len(documents), batch_size):
                    batch = documents[offset : offset + batch_size]
                    payload = _request_embeddings(
                        client,
                        embedding_url,
                        [item["search_text"] for item in batch],
                        offset=offset,
                        max_retries=max_retries,
                    )
                    embeddings = payload.get("embeddings", [])
                    if len(embeddings) != len(batch) or any(
                        not isinstance(vector, list) or len(vector) != DIMENSIONS
                        for vector in embeddings
                    ):
                        raise ValueError(f"embedding service returned invalid batch at offset {offset}")
                    for item, embedding in zip(batch, embeddings):
                        session.run(
                            """
                            MATCH (n:SemanticSearch {id: $id})
                            SET n.embedding = $embedding,
                                n.embedding_model = $model
                            """,
                            id=item["id"],
                            embedding=[float(value) for value in embedding],
                            model=payload.get("model", "text-embedding-3-small"),
                        ).consume()
                        updated += 1
        return {"documents": len(documents), "updated": updated, "dimensions": DIMENSIONS}
    finally:
        driver.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--neo4j-uri", default="bolt://127.0.0.1:7687")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password", default="bird-interact-dev")
    parser.add_argument("--neo4j-database", default="neo4j")
    parser.add_argument("--embedding-url", default="http://127.0.0.1:6003")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    args = parser.parse_args()
    print(json.dumps(import_embeddings(**vars(args)), sort_keys=True))


if __name__ == "__main__":
    main()
