#!/usr/bin/env bash
# Flask identify API + static files on 0.0.0.0:8787
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source .venv/bin/activate

# Load GEMINI_API_KEY from box-secrets if not already in env (never echo the key)
if [[ -z "${GEMINI_API_KEY:-}" && -f /home/box/agent-data/box-secrets.json ]]; then
  GEMINI_API_KEY="$(python -c 'import json; from pathlib import Path; print(json.loads(Path("/home/box/agent-data/box-secrets.json").read_text()).get("card",{}).get("GEMINI_API_KEY") or "")' 2>/dev/null || true)"
  export GEMINI_API_KEY
fi
if [[ -n "${GEMINI_API_KEY:-}" ]]; then
  echo "GEMINI_API_KEY: loaded"
else
  echo "GEMINI_API_KEY: missing (vision/text meta will be skipped)"
fi

echo "Starting look-for-pic-web at http://0.0.0.0:8787/"
echo "同一區網請用電腦的 IP，例如 http://192.168.x.x:8787/"
exec python -u server.py
