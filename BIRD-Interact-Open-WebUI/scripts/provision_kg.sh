#!/usr/bin/env bash
set -euo pipefail

# Explicit, rerunnable kg-v1 provisioning.  RESET_KG=1 is intentionally
# required before reset.cypher can run because reset.cypher deletes the whole
# selected Neo4j database.

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE_DIR="$(cd "$PROJECT_DIR/.." && pwd)"
cd "$PROJECT_DIR"
KG_DIR="${KG_DIR:-$WORKSPACE_DIR/knowledge-graph/20160814}"

if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$PROJECT_DIR/.env"
    set +a
fi

DATASET="${DATASET:-lite}"
DATASET_ROOT="${DATASET_ROOT:-$PROJECT_DIR/bird-interact-${DATASET}}"
NEO4J_CONTAINER="${NEO4J_CONTAINER:-bird_interact_neo4j}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-${NEO4J_PASSWORD_ENV:-bird-interact-dev}}"
NEO4J_DATABASE="${NEO4J_DATABASE:-neo4j}"
NEO4J_HEALTH_TIMEOUT="${NEO4J_HEALTH_TIMEOUT:-120}"
EMBEDDING_URL="${EMBEDDING_SERVICE_URL:-http://127.0.0.1:${EMBEDDING_SERVICE_PORT:-6003}}"
export NEO4J_PASSWORD

case "$DATASET" in
    lite|full) ;;
    *) echo "DATASET must be lite or full" >&2; exit 2 ;;
esac

if [ ! -d "$DATASET_ROOT" ]; then
    echo "Dataset source does not exist: $DATASET_ROOT" >&2
    exit 1
fi
if [ ! -f "$KG_DIR/schema.cypher" ] || [ ! -f "$KG_DIR/generate_mapping.py" ]; then
    echo "kg-v1 assets are missing from $KG_DIR" >&2
    exit 1
fi
if [ -z "${EMBEDDING_API_KEY:-}" ] && [ -z "${OPENAI_EMBEDDING_API_KEY:-}" ]; then
    echo "EMBEDDING_API_KEY is required by the embedding service" >&2
    exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="python"
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required for Neo4j provisioning" >&2
    exit 1
fi

docker compose --profile kg up -d neo4j

for i in $(seq 1 "$NEO4J_HEALTH_TIMEOUT"); do
    status="$(docker inspect --format '{{.State.Health.Status}}' "$NEO4J_CONTAINER" 2>/dev/null || true)"
    if [ "$status" = "healthy" ]; then
        break
    fi
    if [ "$i" -eq "$NEO4J_HEALTH_TIMEOUT" ]; then
        echo "Neo4j healthcheck did not become healthy" >&2
        exit 1
    fi
    sleep 1
done

if ! curl --noproxy '*' --fail --silent "$EMBEDDING_URL/health" >/dev/null; then
    echo "Embedding service is unavailable at $EMBEDDING_URL" >&2
    exit 1
fi

run_cypher_file() {
    local file="$1"
    docker exec "$NEO4J_CONTAINER" cypher-shell \
        -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" -d "$NEO4J_DATABASE" \
        --format plain < "$file"
}

if [ "${RESET_KG:-0}" = "1" ]; then
    echo "Resetting Neo4j database $NEO4J_DATABASE (RESET_KG=1)"
    run_cypher_file "$KG_DIR/reset.cypher"
fi

echo "Applying kg-v1 schema"
run_cypher_file "$KG_DIR/schema.cypher"

echo "Generating mapping for DATASET=$DATASET from $DATASET_ROOT"
"$PYTHON_BIN" "$KG_DIR/generate_mapping.py" \
    --source "$DATASET_ROOT" \
    --output "$KG_DIR/mapping.cypher"

echo "Applying generated mapping"
run_cypher_file "$KG_DIR/mapping.cypher"

echo "Importing semantic embeddings"
"$PYTHON_BIN" "$KG_DIR/import_embeddings.py" \
    --neo4j-uri "${NEO4J_URI:-bolt://127.0.0.1:7687}" \
    --neo4j-user "$NEO4J_USER" \
    --neo4j-password "$NEO4J_PASSWORD" \
    --neo4j-database "$NEO4J_DATABASE" \
    --embedding-url "$EMBEDDING_URL"

if [ "$DATASET" = "lite" ]; then
    echo "Running lite graph validation"
    run_cypher_file "$KG_DIR/validate.cypher"
else
    echo "Running full graph invariant validation"
    docker exec "$NEO4J_CONTAINER" cypher-shell \
        -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" -d "$NEO4J_DATABASE" \
        "MATCH (n:SemanticSearch) WHERE n.search_text IS NULL OR n.embedding IS NULL OR size(n.embedding) <> 1536 RETURN count(n) AS invalid_semantic_nodes;"
fi

echo "KG_PROVISIONED dataset=$DATASET database=$NEO4J_DATABASE"
