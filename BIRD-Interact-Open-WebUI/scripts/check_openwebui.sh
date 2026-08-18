#!/usr/bin/env bash
# Verify the remote model and the authenticated Open WebUI backend path.
set -euo pipefail

BASE_URL="${OPEN_WEBUI_BASE_URL:-http://127.0.0.1:3000}"
MODEL="${SYSTEM_AGENT_MODEL:-google/gemma-4-31B-it}"

if [[ -z "${OPEN_WEBUI_API_KEY:-}" ]]; then
    echo "OPEN_WEBUI_API_KEY is required" >&2
    exit 2
fi

auth=(-H "Authorization: Bearer ${OPEN_WEBUI_API_KEY}")
models_json="$(curl --noproxy '*' -fsS "${BASE_URL%/}/api/models" "${auth[@]}")"
MODEL_JSON="$models_json" EXPECTED_MODEL="$MODEL" python - <<'PY'
import json
import os

data = json.loads(os.environ["MODEL_JSON"])
expected = os.environ["EXPECTED_MODEL"]
models = {item.get("id") for item in data.get("data", [])}
if expected not in models:
    raise SystemExit(f"model not exposed by Open WebUI: {expected}")
print(f"OPENWEBUI_MODEL_OK {expected}")
PY

response="$(curl --noproxy '*' -fsS "${BASE_URL%/}/api/chat/completions" "${auth[@]}" \
    -H 'Content-Type: application/json' \
    --data "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply exactly: OPENWEBUI_SMOKE_OK\"}],\"temperature\":0,\"max_tokens\":32,\"stream\":false}")"
RESPONSE_JSON="$response" python - <<'PY'
import json
import os

data = json.loads(os.environ["RESPONSE_JSON"])
content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
if content != "OPENWEBUI_SMOKE_OK":
    raise SystemExit(f"unexpected model response: {content!r}")
print("OPENWEBUI_CHAT_OK")
PY
