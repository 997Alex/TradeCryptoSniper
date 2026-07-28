#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

# Activate a local venv if one exists; otherwise use whatever python3 is on PATH.
if [ -f venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

exec python3 run_crypto.py
