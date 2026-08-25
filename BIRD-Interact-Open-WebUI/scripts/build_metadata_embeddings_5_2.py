#!/usr/bin/env python3
"""Build the gitignored NumPy embedding cache for metadata-5-2.

This command is intentionally separate from metadata generation.  It must be
run once after the fixed JSON corpus exists and before an evaluation uses the
metadata profile.  The serving process never calls this command implicitly.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.config import settings
from db_environment.metadata_5_2 import (
    CACHE_EMBEDDINGS_FILENAME,
    CACHE_INDEX_FILENAME,
    CACHE_MANIFEST_FILENAME,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EMBEDDING_FRAGMENT_FIELDS,
    METADATA_SCHEMA_VERSION,
    MetadataSearchError,
    _read_json,
    _validate_metadata_file,
    canonical_manifest_hash,
    metadata_cache_dir,
    metadata_manifest_path,
)


class EmbeddingCacheBuildError(RuntimeError):
    pass


def _fragments(data_dir: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for database in manifest["databases"]:
        path = data_dir / database / f"{database}_metadata.json"
        raw = _read_json(path, label=f"{database} metadata")
        columns = _validate_metadata_file(raw, database)
        for column_id in sorted(columns):
            column = columns[column_id]
            values: list[tuple[str, str, list[str]]] = [
                ("table_name", f"{database} table {column['table_name']}", []),
                ("column_name", f"{column['table_name']} column {column['column_name']}", []),
            ]
            if column["description"].strip():
                values.append(("description", column["description"], []))
            for kind in ("formulas", "disambiguation_rules", "known_discrepancies"):
                for item in column[kind]:
                    values.append((kind, item["value"], list(item["knowledge_refs"])))
            for ordinal, (kind, text, refs) in enumerate(values):
                if not isinstance(text, str) or not text.strip():
                    continue
                if kind not in EMBEDDING_FRAGMENT_FIELDS:
                    raise EmbeddingCacheBuildError(f"unsupported embedding fragment kind {kind!r}")
                result.append({
                    "fragment_id": f"{column_id}:{kind}:{ordinal}",
                    "database_name": database,
                    "column_id": column_id,
                    "kind": kind,
                    "text": text,
                    "knowledge_refs": refs,
                })
    if not result:
        raise EmbeddingCacheBuildError("metadata corpus has no embedding fragments")
    return result


def _embed(texts: list[str], embedding_url: str, timeout: float) -> list[list[float]]:
    vectors: list[list[float]] = []
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            for start in range(0, len(texts), 128):
                batch = texts[start : start + 128]
                response = client.post(f"{embedding_url.rstrip('/')}/embed", json={"texts": batch})
                response.raise_for_status()
                data = response.json()
                current = data.get("embeddings") if isinstance(data, dict) else None
                if (
                    not isinstance(current, list)
                    or len(current) != len(batch)
                    or any(
                        not isinstance(vector, list)
                        or len(vector) != EMBEDDING_DIMENSIONS
                        or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in vector)
                        for vector in current
                    )
                ):
                    raise EmbeddingCacheBuildError(
                        f"embedding service returned invalid batch at offset {start}"
                    )
                vectors.extend([[float(value) for value in vector] for vector in current])
    except EmbeddingCacheBuildError:
        raise
    except (httpx.HTTPError, ValueError, OSError) as exc:
        raise EmbeddingCacheBuildError(f"embedding service unavailable: {exc}") from exc
    return vectors


def build_cache(data_dir: Path, cache_dir: Path, embedding_url: str, timeout: float) -> dict[str, Any]:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - requirements install path
        raise EmbeddingCacheBuildError("NumPy is required to build metadata embeddings") from exc

    manifest = _read_json(metadata_manifest_path(data_dir), label="metadata manifest")
    if manifest.get("schema_version") != METADATA_SCHEMA_VERSION:
        raise EmbeddingCacheBuildError("metadata manifest schema_version is unsupported")
    if manifest.get("embedding_model") != EMBEDDING_MODEL or manifest.get("dimensions") != EMBEDDING_DIMENSIONS:
        raise EmbeddingCacheBuildError("metadata manifest embedding configuration is unsupported")
    databases = manifest.get("databases")
    if not isinstance(databases, list) or not databases:
        raise EmbeddingCacheBuildError("metadata manifest has no databases")

    fragments = _fragments(data_dir, manifest)
    vectors = _embed([item["text"] for item in fragments], embedding_url, timeout)
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.shape != (len(fragments), EMBEDDING_DIMENSIONS) or not np.isfinite(matrix).all():
        raise EmbeddingCacheBuildError("embedding matrix has an invalid shape or non-finite value")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_manifest = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "metadata_source_hash": manifest["metadata_source_hash"],
        "manifest_hash": canonical_manifest_hash(manifest),
        "embedding_model": EMBEDDING_MODEL,
        "dimensions": EMBEDDING_DIMENSIONS,
        "fragment_count": len(fragments),
    }
    index = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "metadata_source_hash": manifest["metadata_source_hash"],
        "manifest_hash": canonical_manifest_hash(manifest),
        "embedding_model": EMBEDDING_MODEL,
        "dimensions": EMBEDDING_DIMENSIONS,
        "fragments": fragments,
    }

    # Build all output in a temporary directory and replace the three cache
    # artifacts together as far as the filesystem permits.
    with tempfile.TemporaryDirectory(dir=cache_dir.parent) as temporary:
        temporary_dir = Path(temporary)
        np.savez_compressed(temporary_dir / CACHE_EMBEDDINGS_FILENAME, embeddings=matrix)
        (temporary_dir / CACHE_INDEX_FILENAME).write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (temporary_dir / CACHE_MANIFEST_FILENAME).write_text(
            json.dumps(cache_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for filename in (CACHE_EMBEDDINGS_FILENAME, CACHE_INDEX_FILENAME, CACHE_MANIFEST_FILENAME):
            (temporary_dir / filename).replace(cache_dir / filename)
    return {"fragments": len(fragments), "dimensions": EMBEDDING_DIMENSIONS}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=settings.data_dir)
    parser.add_argument("--cache-dir", type=Path, default=metadata_cache_dir())
    parser.add_argument("--embedding-url", default=settings.embedding_service_url)
    parser.add_argument("--timeout", type=float, default=settings.embedding_timeout)
    args = parser.parse_args()
    try:
        result = build_cache(args.data.resolve(), args.cache_dir.resolve(), args.embedding_url, args.timeout)
    except (EmbeddingCacheBuildError, MetadataSearchError) as exc:
        print(f"metadata embedding cache build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
