#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost}"
export no_proxy="${no_proxy:-127.0.0.1,localhost}"

PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "Python environment missing. Run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
    exit 1
fi

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "Refusing to start: ANTHROPIC_API_KEY would use pay-as-you-go billing."
    exit 1
fi

HOST="${SERVICE_HOST:-127.0.0.1}"
SYSTEM_PORT="${SYSTEM_AGENT_PORT:-6000}"
USER_PORT="${USER_SIM_PORT:-6001}"
DB_PORT="${DB_ENV_PORT:-6002}"

pkill -f uvicorn 2>/dev/null || true
sleep 1

# Start all three microservices
"$PYTHON_BIN" -m uvicorn system_agent.server:app --host "$HOST" --port "$SYSTEM_PORT" --log-level warning &
"$PYTHON_BIN" -m uvicorn user_simulator.server:app --host "$HOST" --port "$USER_PORT" --log-level warning &
"$PYTHON_BIN" -m uvicorn db_environment.server:app --host "$HOST" --port "$DB_PORT" --log-level warning &

# Wait for all three to be healthy
for i in $(seq 1 30); do
    if curl --noproxy '*' -s "http://127.0.0.1:$SYSTEM_PORT/health" > /dev/null 2>&1 && \
       curl --noproxy '*' -s "http://127.0.0.1:$USER_PORT/health" > /dev/null 2>&1 && \
       curl --noproxy '*' -s "http://127.0.0.1:$DB_PORT/health" > /dev/null 2>&1; then
        echo "ALL_SERVICES_READY (ports $SYSTEM_PORT, $USER_PORT, $DB_PORT)"
        exit 0
    fi
    sleep 1
done
echo "SERVICES_FAILED"
exit 1
