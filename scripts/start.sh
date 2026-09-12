#!/bin/bash
# Start the Classroom Interpreter and open it in a browser.
#
# Everything here is a thing that actually went wrong at least once, not
# defensive boilerplate.
set -u
cd "$(dirname "$0")/.."
ROOT="$PWD"
PORT="${PORT:-8000}"

if [ ! -f .env ]; then
  echo "!! .env is missing. Copy .env.example to .env and put your MIMO_API_KEY in it."
  exit 1
fi

# This repo lives inside iCloud Drive, which evicts files it thinks are cold --
# including the virtualenv. Each read of an evicted file blocks for ~1.75s, and
# with ~2000 of them the backend simply appears to hang for the better part of
# an hour. The reads are network-bound, so fetching them in parallel is the fix;
# `brctl download` made no progress at all.
EVICTED=$(find .venv -type f -print0 2>/dev/null | xargs -0 ls -lO 2>/dev/null | grep -c dataless)
if [ "${EVICTED:-0}" -gt 0 ]; then
  echo ".. $EVICTED files were evicted by iCloud; fetching them (this is why startup would otherwise hang)"
  find .venv -type f -print0 2>/dev/null | xargs -0 ls -lO 2>/dev/null \
    | grep dataless | awk '{print $NF}' \
    | xargs -P 48 -I{} sh -c 'head -c 1 "{}" >/dev/null 2>&1'
fi

if [ ! -f frontend/dist/index.html ]; then
  echo ".. building the frontend"
  (cd frontend && npm ci --silent && npm run build) || exit 1
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "!! something is already listening on port $PORT."
  echo "   Either it is already running (open http://127.0.0.1:$PORT/) or run: PORT=8001 $0"
  exit 1
fi

echo ".. starting on http://127.0.0.1:$PORT"
PYTHONPATH="$ROOT/backend" .venv/bin/python -m uvicorn classroom_interpreter.main:app \
  --host 127.0.0.1 --port "$PORT" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' INT TERM

for _ in $(seq 1 60); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/api/app-info" >/dev/null 2>&1; then
    open "http://127.0.0.1:$PORT/"
    echo ".. open. Press Ctrl+C here to stop recording and shut down."
    wait $SERVER
    exit 0
  fi
  sleep 1
done

echo "!! it did not come up within 60s. Check the output above."
kill $SERVER 2>/dev/null
exit 1
