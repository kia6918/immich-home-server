#!/usr/bin/env bash
set -Eeuo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
PYTHON="${IMMICH_PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    printf '%s\n' '[ERROR] Python 3.10+ is required. Install it on the target, then rerun.' >&2
    exit 1
fi
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || {
    printf '%s\n' '[ERROR] Python 3.10+ is required.' >&2
    exit 1
}
