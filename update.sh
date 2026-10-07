#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/scripts/common.sh"
exec "$PYTHON" "$ROOT/scripts/cli.py" update "$@"
