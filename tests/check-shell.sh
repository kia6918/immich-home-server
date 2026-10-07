#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
while IFS= read -r -d '' script; do
    bash -n "$script"
done < <(find "$ROOT" -name '*.sh' -not -path '*/.git/*' -print0)
printf '%s\n' '[OK] All shell entry points pass bash -n'
