#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python_bin="${HADAN_PYTHON:-.venv/bin/python}"
"$python_bin" -m hadan.cli init
"$python_bin" -m hadan.cli worker --queue main &
worker_pid=$!
trap 'kill "$worker_pid" 2>/dev/null || true' EXIT INT TERM
"$python_bin" -m hadan.cli serve --host 127.0.0.1 --port "${HADAN_PORT:-8080}"
