#!/usr/bin/env bash
# Prepare the mounted disk, then hand off to uvicorn as a non-root user.
#
# The disk arrives owned by root, so the chown has to happen here, at runtime —
# a chown in the Dockerfile would apply to the image's empty directory and be
# shadowed the moment the volume is mounted over it.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/var/data}"

mkdir -p "$DATA_DIR"
chown -R app:app "$DATA_DIR"

# Fail loudly rather than starting up and losing every saved note silently.
if ! gosu app test -w "$DATA_DIR"; then
  echo "FATAL: $DATA_DIR is not writable by the app user." >&2
  echo "On Render, check the disk is mounted at exactly this path." >&2
  exit 1
fi
echo "data dir: $DATA_DIR ($(df -h "$DATA_DIR" | awk 'NR==2{print $4}') free)"

# --proxy-headers: behind Render's load balancer the app sees the proxy's
#   address and http://, not the client's address and https://. Without this,
#   anything deriving a scheme or client IP from the request is wrong.
# --workers 1: see the note in the Dockerfile. Do not raise this.
exec gosu app uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips='*' \
  --timeout-keep-alive 65
