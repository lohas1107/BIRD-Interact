#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost}"
export no_proxy="${no_proxy:-127.0.0.1,localhost}"

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
PID_DIR="${BIRD_INTERACT_PID_DIR:-/tmp/bird-interact-open-webui}"
mkdir -p "$PID_DIR"

for service in system user db; do
    pid_file="$PID_DIR/${service}.pid"
    if [ -f "$pid_file" ]; then
        pid="$(cat "$pid_file")"
        kill "$pid" 2>/dev/null || true
        rm -f "$pid_file"
    fi
done
sleep 1

# Start all three microservices
"$PYTHON_BIN" -m uvicorn system_agent.server:app --host "$HOST" --port "$SYSTEM_PORT" --log-level warning & echo $! > "$PID_DIR/system.pid"
"$PYTHON_BIN" -m uvicorn user_simulator.server:app --host "$HOST" --port "$USER_PORT" --log-level warning & echo $! > "$PID_DIR/user.pid"
"$PYTHON_BIN" -m uvicorn db_environment.server:app --host "$HOST" --port "$DB_PORT" --log-level warning & echo $! > "$PID_DIR/db.pid"

# Wait for all three to be healthy
for i in $(seq 1 30); do
    if curl --noproxy '*' -s "http://127.0.0.1:${SYSTEM_PORT}/health" > /dev/null 2>&1 && \
       curl --noproxy '*' -s "http://127.0.0.1:${USER_PORT}/health" > /dev/null 2>&1 && \
       curl --noproxy '*' -s "http://127.0.0.1:${DB_PORT}/health" > /dev/null 2>&1; then
        echo "ALL_SERVICES_READY (ports ${SYSTEM_PORT}, ${USER_PORT}, ${DB_PORT})"
        exit 0
    fi
    sleep 1
done
echo "SERVICES_FAILED"
exit 1
