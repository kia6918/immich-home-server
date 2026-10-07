# Immich Home Server

Use Python standard library for parsers and workflow logic; shell entry points use
`set -Eeuo pipefail`. Never source generated environment files. Run
`python3 -m unittest discover -s tests -v` and `./tests/check-shell.sh` before committing.
Keep `docs/HANDOFF.md` current. Do not claim platform or live deployment validation
without recorded evidence. Never delete libraries, database directories, existing
backups, or unrelated Docker resources. No automatic version upgrades. Local tests
use temporary directories and fixtures, never real photo libraries or NAS data.
