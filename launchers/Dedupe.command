#!/bin/bash
# Double-click launcher for Dedupe web UI (macOS)
# Opens Terminal, starts the local UI, and opens your browser.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${DEDUPE_PORT:-8765}"
HOST="127.0.0.1"
URL="http://${HOST}:${PORT}/"

banner() {
  echo ""
  echo "╔══════════════════════════════════════╗"
  echo "║           Dedupe — Media UI          ║"
  echo "╚══════════════════════════════════════╝"
  echo ""
}

die() {
  echo ""
  echo "ERROR: $1" >&2
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display alert \"Dedupe\" message \"$1\" as critical" >/dev/null 2>&1 || true
  fi
  echo ""
  echo "Press Enter to close…"
  read -r _
  exit 1
}

# Prefer project venv; fall back to python3
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  die "Python 3 not found. Install Python 3.11+ and run: python3 -m venv .venv && source .venv/bin/activate && pip install -e ."
fi

export PYTHONPATH="$ROOT/src:${PYTHONPATH:-}"

# Ensure package is importable (editable install preferred)
if ! "$PYTHON" -c "import dedupe" 2>/dev/null; then
  echo "Installing Dedupe into the local environment…"
  if [[ -x "$ROOT/.venv/bin/pip" ]]; then
    "$ROOT/.venv/bin/pip" install -e "$ROOT" -q || die "pip install failed. From the project folder run: pip install -e ."
  else
    "$PYTHON" -m pip install -e "$ROOT" -q || die "pip install failed. From the project folder run: pip install -e ."
  fi
fi

# Reuse a current Dedupe process, but restart an older process from this checkout.
# Flask serves static files from disk while its route table is fixed at process start;
# blindly reusing an old process can therefore show new buttons backed by missing APIs.
if command -v lsof >/dev/null 2>&1; then
  PID="$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1)"
  if [[ -n "$PID" ]]; then
    EXPECTED_API_VERSION="$("$PYTHON" -c 'from dedupe.web.app import WEB_API_VERSION; print(WEB_API_VERSION)')"
    RUNNING_API_VERSION="$(curl -sf "$URL/api/status" 2>/dev/null \
      | "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin).get("web_api_version", ""))' \
      2>/dev/null || true)"

    if [[ "$RUNNING_API_VERSION" == "$EXPECTED_API_VERSION" ]]; then
      banner
      echo "Dedupe is already running on port $PORT."
      echo "Opening $URL"
      open "$URL" 2>/dev/null || true
      echo ""
      echo "Press Enter to close this window (server keeps running)…"
      read -r _
      exit 0
    fi

    PROCESS_CWD="$(lsof -a -p "$PID" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
    PROCESS_COMMAND="$(ps -p "$PID" -o command= 2>/dev/null || true)"
    if [[ "$PROCESS_CWD" == "$ROOT" && "$PROCESS_COMMAND" == *"dedupe.cli ui"* ]]; then
      echo "Restarting an outdated Dedupe server…"
      kill "$PID"
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
        sleep 0.2
      done
      if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        die "The outdated Dedupe server did not stop. Close it and try again."
      fi
    else
      die "Port $PORT is being used by another application. Close it or set DEDUPE_PORT to another port."
    fi
  fi
fi

banner
echo "Project:  $ROOT"
echo "Python:   $PYTHON"
echo "URL:      $URL"
echo ""
echo "Starting… (close this window or press Ctrl+C to stop)"
echo "──────────────────────────────────────────────────────"

# Open browser shortly after server binds
(
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.4
    if command -v curl >/dev/null 2>&1; then
      if curl -sf -o /dev/null "$URL" 2>/dev/null; then
        open "$URL" 2>/dev/null || true
        exit 0
      fi
    else
      open "$URL" 2>/dev/null || true
      exit 0
    fi
  done
  open "$URL" 2>/dev/null || true
) &

exec "$PYTHON" -m dedupe.cli ui --port "$PORT" --no-browser
