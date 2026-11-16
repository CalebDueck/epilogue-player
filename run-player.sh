#!/bin/sh
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
cd "$APP_DIR"
if python3 - <<'PY' >/dev/null 2>&1
from urllib.request import urlopen
urlopen("http://127.0.0.1:8766/", timeout=0.5).close()
PY
then
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open http://127.0.0.1:8766 >/dev/null 2>&1 || true
  fi
  exit 0
fi
python3 host-player/server.py &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
python3 - <<'PY'
import time
from urllib.request import urlopen
for _ in range(50):
    try:
        urlopen("http://127.0.0.1:8766/", timeout=0.2).close()
        break
    except Exception:
        time.sleep(0.1)
else:
    raise SystemExit("Epilogue GB Player did not start on 127.0.0.1:8766")
PY
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open http://127.0.0.1:8766 >/dev/null 2>&1 || true
fi
wait "$SERVER_PID"
