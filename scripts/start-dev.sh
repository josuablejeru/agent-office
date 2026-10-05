#!/usr/bin/env bash
# Starts the backend (127.0.0.1:8000) and the frontend dev server (127.0.0.1:5173),
# then prints the link that signs this browser in with the local API token.
set -euo pipefail
cd "$(dirname "$0")/.."

HOME_DIR="${AGENT_OFFICE_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/agent-office}"
TOKEN_FILE="$HOME_DIR/secrets/api-token"

uv run uvicorn --factory backend.main:create_app --host 127.0.0.1 --port 8000 --reload &
backend_pid=$!
trap 'kill "$backend_pid" 2>/dev/null || true' EXIT

until [ -s "$TOKEN_FILE" ] && curl -s -o /dev/null http://127.0.0.1:8000/docs; do
  kill -0 "$backend_pid" 2>/dev/null || { echo "backend failed to start" >&2; exit 1; }
  sleep 0.3
done
echo
echo "  Open agent-office:  http://127.0.0.1:5173/#token=$(cat "$TOKEN_FILE")"
echo

cd frontend
[ -d node_modules ] || npm install
npm run dev
