#!/usr/bin/env bash
# Start the proxy and expose it on a public URL.
#
# The script does not change the Endor tenant. It prints the URL to put on the
# Jira integration, and the Jira site from .env that requests will be sent to.
#
# Uses one static ngrok domain from NGROK_URL, so the address does not change
# between runs. Ctrl-C stops the proxy and the tunnel.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8080}"
ENV_FILE="$ROOT/.env"

RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/jira-variables-proxy.XXXXXX")"
SHIM_LOG="$RUN_DIR/shim.log"
TUNNEL_LOG="$RUN_DIR/tunnel.log"
SHIM_PID=""
TAIL_PID=""
TUNNEL_PID=""
PUBLIC_URL=""
JIRA_BASE_URL=""

require_env_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "missing ${ENV_FILE}; copy .env.example and fill it in" >&2
    exit 1
  fi
  JIRA_BASE_URL="$(PYTHONPATH="$ROOT/src" python3 - "$ENV_FILE" <<'PY'
import sys
from jira_variables_proxy.config import Config
print(Config.from_dotenv(sys.argv[1]).jira_base_url)
PY
)"
}

write_dotenv() {
  PYTHONPATH="$ROOT/src" python3 - "$ENV_FILE" "$RUN_DIR/.env" "$1" ":$PORT" <<'PY'
import os
import sys
from jira_variables_proxy.config import parse_dotenv

source, dest, public_url, listen = sys.argv[1:]
values = parse_dotenv(open(source, encoding="utf-8").read())
values["PUBLIC_BASE_URL"] = public_url
values["LISTEN_ADDR"] = listen

def quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    for key, value in values.items():
        handle.write(f"{key}={quote(value)}\n")
PY
}

start_shim() {
  if [[ -n "$SHIM_PID" ]] && kill -0 "$SHIM_PID" 2>/dev/null; then
    kill "$SHIM_PID" 2>/dev/null || true
    wait "$SHIM_PID" 2>/dev/null || true
  fi
  write_dotenv "$1"
  pushd "$RUN_DIR" >/dev/null
  env -u PUBLIC_BASE_URL -u JIRA_BASE_URL -u ENDOR_API_URL \
    -u ENDOR_API_CREDENTIALS_KEY -u ENDOR_API_CREDENTIALS_SECRET -u LISTEN_ADDR \
    PYTHONUNBUFFERED=1 PYTHONPATH="$ROOT/src" \
    python3 -m jira_variables_proxy >>"$SHIM_LOG" 2>&1 &
  SHIM_PID=$!
  popd >/dev/null
  local attempt
  for attempt in $(seq 1 50); do
    if curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null; then
      return 0
    fi
    if ! kill -0 "$SHIM_PID" 2>/dev/null; then
      echo "proxy exited during startup" >&2
      cat "$SHIM_LOG" >&2
      return 1
    fi
    sleep 0.2
  done
  echo "proxy did not become healthy" >&2
  return 1
}

ngrok_available() {
  if [[ -n "${NGROK_AUTHTOKEN:-}" ]]; then
    return 0
  fi
  ngrok config check >/dev/null 2>&1
}

read_ngrok_url() {
  curl -sf "http://127.0.0.1:4040/api/tunnels" | python3 -c '
import json, sys
data = json.load(sys.stdin)
for tunnel in data.get("tunnels", []):
    if tunnel.get("proto") == "https" and tunnel.get("public_url"):
        print(tunnel["public_url"])
        break
'
}

start_ngrok() {
  local -a args=(http "$PORT" --url "$NGROK_URL" --log stdout --log-format json)
  if [[ -n "${NGROK_AUTHTOKEN:-}" ]]; then
    args+=(--authtoken "$NGROK_AUTHTOKEN")
  fi
  ngrok "${args[@]}" >>"$TUNNEL_LOG" 2>&1 &
  TUNNEL_PID=$!
  local attempt
  for attempt in $(seq 1 40); do
    if curl -sf "http://127.0.0.1:4040/api/tunnels" >/dev/null 2>&1; then
      PUBLIC_URL="$(read_ngrok_url)"
      if [[ -n "$PUBLIC_URL" ]]; then
        return 0
      fi
    fi
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
      echo "ngrok exited. If this is ERR_NGROK_4018, set NGROK_AUTHTOKEN." >&2
      cat "$TUNNEL_LOG" >&2
      return 1
    fi
    sleep 0.5
  done
  echo "ngrok did not publish a URL" >&2
  cat "$TUNNEL_LOG" >&2
  return 1
}

print_instructions() {
  echo
  echo "Set the Jira integration URL to:"
  echo "  ${PUBLIC_URL}"
  echo "Jira requests are forwarded to:"
  echo "  ${JIRA_BASE_URL}"
  echo
  echo "Leave the Jira user and API token as they are for that Jira site."
  echo "Ctrl-C stops the proxy and the tunnel."
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$TAIL_PID" ]]; then
    kill "$TAIL_PID" 2>/dev/null || true
  fi
  if [[ -n "$SHIM_PID" ]]; then
    kill "$SHIM_PID" 2>/dev/null || true
  fi
  if [[ -n "$TUNNEL_PID" ]]; then
    kill "$TUNNEL_PID" 2>/dev/null || true
  fi
  wait "$SHIM_PID" 2>/dev/null || true
  wait "$TAIL_PID" 2>/dev/null || true
  wait "$TUNNEL_PID" 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT INT TERM

require_env_file

if [[ -z "${NGROK_URL:-}" ]]; then
  echo "Set NGROK_URL to the static domain from the ngrok dashboard." >&2
  echo "  NGROK_URL=https://your-name.ngrok-free.app ./scripts/stand-up.sh" >&2
  echo "Also run: ngrok config add-authtoken <token>" >&2
  echo "A random address expires, and Endor then shows \"no tunnel\"." >&2
  exit 1
fi
NGROK_URL="${NGROK_URL%/}"
if ! ngrok_available; then
  echo "ngrok is not authenticated. Run: ngrok config add-authtoken <token>" >&2
  exit 1
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port ${PORT} is already in use" >&2
  exit 1
fi

echo "starting proxy on :${PORT}"
touch "$SHIM_LOG"
start_shim "http://127.0.0.1:${PORT}"
tail -f "$SHIM_LOG" &
TAIL_PID=$!

echo "starting ngrok at ${NGROK_URL}"
start_ngrok

echo "public URL ${PUBLIC_URL}"
start_shim "$PUBLIC_URL"

echo "checking the public tenant_info route"
curl -sf "${PUBLIC_URL}/_edge/tenant_info"
echo
print_instructions
wait "$SHIM_PID"
