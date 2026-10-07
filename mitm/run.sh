#!/usr/bin/env bash
# Run natively: Docker Desktop hides the connecting plug's peer IP.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 tenda_mitm.py "$@"
