#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATASET_PATH="${BIRD_INTERACT_DATASET:-${SCRIPT_DIR}/../../BIRD-Interact-Claude/bird-interact-lite}"

: "${NEO4J_URI:?Set NEO4J_URI, for example neo4j://localhost:7687}"
: "${NEO4J_USER:?Set NEO4J_USER}"
: "${NEO4J_PASSWORD:?Set NEO4J_PASSWORD}"
: "${NEO4J_DATABASE:?Set the disposable NEO4J_DATABASE explicitly}"

if [[ "${ALLOW_FULL_NEO4J_RESET:-}" != "YES" ]]; then
  echo "Refusing destructive reset. Verify NEO4J_DATABASE=${NEO4J_DATABASE}, then set ALLOW_FULL_NEO4J_RESET=YES." >&2
  exit 2
fi

python3 "${SCRIPT_DIR}/generate_mapping.py" \
  --dataset "${DATASET_PATH}" \
  --output "${SCRIPT_DIR}/mapping.cypher"

neo4j_args=(-a "${NEO4J_URI}" -u "${NEO4J_USER}" -p "${NEO4J_PASSWORD}" -d "${NEO4J_DATABASE}")
cypher-shell "${neo4j_args[@]}" -f "${SCRIPT_DIR}/reset.cypher"
cypher-shell "${neo4j_args[@]}" -f "${SCRIPT_DIR}/mapping.cypher"
cypher-shell "${neo4j_args[@]}" -f "${SCRIPT_DIR}/validate.cypher"
