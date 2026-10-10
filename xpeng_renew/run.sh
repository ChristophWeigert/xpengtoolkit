#!/usr/bin/env bash
# Options live in /data/options.json and are read by the worker itself; environment
# variables still win, so the image also runs outside the Supervisor.
set -euo pipefail
echo "[run] xpeng credential renewal worker on port ${PORT:-8090}"
exec python3 /app/renew_server.py
