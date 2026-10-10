#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export TEMP_MAIL_CONFIG="${TEMP_MAIL_CONFIG:-$PWD/config/config.yaml}"
port="${PORT:-8000}"
pids=()

stop_services() {
  trap '' INT TERM
  if ((${#pids[@]} == 0)); then
    return
  fi
  kill -TERM "${pids[@]}" 2>/dev/null || true
  local deadline=$((SECONDS + 15))
  while ((SECONDS < deadline)); do
    local running=false
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        running=true
      fi
    done
    if [[ "$running" == false ]]; then
      break
    fi
    sleep 0.2
  done
  kill -KILL "${pids[@]}" 2>/dev/null || true
  for pid in "${pids[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
}

trap stop_services EXIT
trap 'exit 0' INT TERM

if [[ ! -e "$TEMP_MAIL_CONFIG" ]]; then
  mkdir -p -- "$(dirname -- "$TEMP_MAIL_CONFIG")"
  python scripts/init_config.py --output "$TEMP_MAIL_CONFIG" --template config.example.yaml
fi

python -m app.server --host 0.0.0.0 --port "$port" --timeout-graceful-shutdown 10 &
pids+=("$!")

# Wait for database initialization before starting the worker.
deadline=$((SECONDS + 30))
until python -c 'import sys, urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:" + sys.argv[1] + "/health/ready", timeout=1).close()' "$port" 2>/dev/null; do
  if ! kill -0 "${pids[0]}" 2>/dev/null || ((SECONDS >= deadline)); then
    exit 1
  fi
  sleep 0.2
done

python -m app.worker &
pids+=("$!")

# Any unexpected exit ends the container so its restart policy can recover both services.
while true; do
  for pid in "${pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      if wait "$pid"; then
        exit 1
      else
        exit "$?"
      fi
    fi
  done
  sleep 0.2
done
