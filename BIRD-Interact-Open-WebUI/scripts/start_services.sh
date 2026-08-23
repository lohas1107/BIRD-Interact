#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost}"
export no_proxy="${no_proxy:-127.0.0.1,localhost}"

if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$PROJECT_DIR/.env"
    set +a
fi

PYTHON_BIN="python"
if [ -x "$PROJECT_DIR/.conda-py310/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/.conda-py310/bin/python"
elif [ -n "${CONDA_PREFIX:-}" ] && [ -x "${CONDA_PREFIX}/bin/python" ]; then
    PYTHON_BIN="${CONDA_PREFIX}/bin/python"
elif [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
fi

HOST="${SERVICE_HOST:-127.0.0.1}"
SYSTEM_PORT="${SYSTEM_AGENT_PORT:-6000}"
USER_PORT="${USER_SIM_PORT:-6001}"
DB_PORT="${DB_ENV_PORT:-6002}"
KG_ENABLED="${KG_ENABLED:-0}"
EMBEDDING_PORT="${EMBEDDING_SERVICE_PORT:-6003}"

if [ "$KG_ENABLED" = "1" ] && [ -z "${EMBEDDING_API_KEY:-}" ] && [ -z "${OPENAI_EMBEDDING_API_KEY:-}" ]; then
    echo "KG_ENABLED=1 requires EMBEDDING_API_KEY for the embedding endpoint."
    exit 1
fi

pkill -f uvicorn 2>/dev/null || true
sleep 1

# Start all three microservices
"$PYTHON_BIN" -m uvicorn system_agent.server:app --host "$HOST" --port "$SYSTEM_PORT" --log-level warning &
"$PYTHON_BIN" -m uvicorn user_simulator.server:app --host "$HOST" --port "$USER_PORT" --log-level warning &
"$PYTHON_BIN" -m uvicorn db_environment.server:app --host "$HOST" --port "$DB_PORT" --log-level warning &
if [ "$KG_ENABLED" = "1" ]; then
    export EMBEDDING_SERVICE_PORT="$EMBEDDING_PORT"
    export EMBEDDING_MODEL="${EMBEDDING_MODEL:-${OPENAI_EMBEDDING_MODEL:-text-embedding-3-small}}"
    export EMBEDDING_DIMENSIONS="${EMBEDDING_DIMENSIONS:-${OPENAI_EMBEDDING_DIMENSIONS:-1536}}"
    "$PYTHON_BIN" -m uvicorn embedding.server:app --host "$HOST" --port "$EMBEDDING_PORT" --log-level warning &
fi

# Wait for all three to be healthy
for i in $(seq 1 30); do
    if curl --noproxy '*' -s "http://127.0.0.1:${SYSTEM_PORT}/health" > /dev/null 2>&1 && \
       curl --noproxy '*' -s "http://127.0.0.1:${USER_PORT}/health" > /dev/null 2>&1 && \
       curl --noproxy '*' -s "http://127.0.0.1:${DB_PORT}/health" > /dev/null 2>&1; then
        if [ "$KG_ENABLED" != "1" ] || curl --noproxy '*' -s "http://127.0.0.1:${EMBEDDING_PORT}/health" > /dev/null 2>&1; then
            if [ "$KG_ENABLED" = "1" ]; then
                echo "ALL_SERVICES_READY (ports ${SYSTEM_PORT}, ${USER_PORT}, ${DB_PORT}, ${EMBEDDING_PORT})"
            else
                echo "ALL_SERVICES_READY (ports ${SYSTEM_PORT}, ${USER_PORT}, ${DB_PORT})"
            fi
            exit 0
        fi
    fi
    sleep 1
done
echo "SERVICES_FAILED"
exit 1
