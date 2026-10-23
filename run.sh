#!/usr/bin/env bash
# Start the YouTube Notes app.
set -e
cd "$(dirname "$0")"

# Create a virtualenv on first run.
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -r requirements.txt
fi

# Load .env for HOST/PORT if present.
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

echo "▶  YouTube Notes running at http://${HOST}:${PORT}"
exec ./.venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
