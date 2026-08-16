#!/usr/bin/env python3
"""Populate semantic graph embeddings through the independent HTTP service."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx
from neo4j import GraphDatabase


DIMENSIONS = 1024


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


def import_embeddings(
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
    neo4j_database: str,
    embedding_url: str,
    batch_size: int = 32,
) -> dict[str, Any]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    # Keep requests within the embedding service contract even when an
    # operator supplies a larger batch size on the command line.
    batch_size = min(batch_size, 128)
    driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
    try:
        documents = load_documents(driver, neo4j_database)
        updated = 0
        with httpx.Client(timeout=120.0, trust_env=False) as client:
            with driver.session(database=neo4j_database) as session:
                for offset in range(0, len(documents), batch_size):
                    batch = documents[offset : offset + batch_size]
                    response = client.post(
                        f"{embedding_url.rstrip('/')}/embed",
                        json={"texts": [item["search_text"] for item in batch]},
                    )
                    response.raise_for_status()
                    payload = response.json()
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
                            model=payload.get("model", "Qwen/Qwen3-Embedding-0.6B"),
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
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args()
    print(json.dumps(import_embeddings(**vars(args)), sort_keys=True))


if __name__ == "__main__":
    main()
