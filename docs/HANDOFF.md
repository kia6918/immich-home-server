# Immich deployment handoff

Updated: 2026-10-07. Goal: reusable SSH Immich deployment repository developed on M4,
then a real i5 deployment with user-selected storage and production backups preserved.

## Current status

- Standalone repository created at `/Users/yqiu/dev/immich-home-server`, branch `dev`.
- Python standard-library implementation with Bash entry points. No Codex dependency
  on targets. Host secrets/state stay outside repository.
- Official documentation checked; latest stable release at this session: `v3.2.4`.
- Core deploy/storage/ownership/supervisor/backup/restore/update/migration implemented.
- Initial fixture test run: 48 passed, one failed because README had not yet been written.
  README now exists; rerun and extend tests before release.
- M4 read-only inventory: macOS 26.6.2, arm64 M4 Pro, 24 GiB RAM. Existing Docker.app
  found, CLI outside normal PATH, daemon unavailable. App opened for disposable testing;
  GUI is locked, so initial setup cannot yet be inspected.
- Requested i5 SSH endpoint and M4 unlock asynchronously; no answer yet.
- No Immich containers started. No real photo library or NAS modified.

## Key decisions and files

- `scripts/storage.py`: mount identity, UUID, root-fallback and DB-locality checks.
- `scripts/engine.py`: release-specific official Compose retrieval and safety hardening.
- `scripts/lifecycle.py`: storage-aware host supervision and library ownership fencing.
- `scripts/backup.py`: pg_dump bundles and fresh-directory transactional restore.
- `scripts/migration.py`: staged cutover/source block/rollback, no live PG directory copy.
- `scripts/remote.py`: small public-code bundle and SSH backup stream relay.
- `scripts/installer.py`, `operations.py`, `cli.py`: interactive commands and workflows.
- `tests/test_safety.py`: unsafe disk actions replaced by fixtures/temp directories.
- `README.md`, `docs/OPERATIONS.md`: setup, limitations and recovery procedures.

## Next work and unresolved validation

1. Finish runtime safety review, especially network mounts disappearing between host
   checks and Docker binding, supervisor crash behavior and migration resume handling.
2. Fix any lifecycle/state issues found by tests; add full interactive mocked workflow
   tests and real disposable Compose integration tests if Docker becomes available.
3. Verify current schema asset queries against the pinned release, actual backup/restore
   and same/new storage migration, including failures and rollback.
4. Commit verified milestones, run shell/static checks and secret exclusion checks.
5. Obtain actual i5 SSH endpoint, inspect services/mounts/backups read-only, present its
   storage candidates to the user. Do not guess its 8TB mount.
6. Deploy only after the user chooses storage; verify Docker/DB/locality/storage guard,
   LAN HTTP, admin/onboarding and a test upload. Record selected library/DB/URL here.
7. Remove only disposable M4 test workloads; leave existing Docker installation intact.

Implemented adapters are not a claim of live Linux/macOS Intel/SMB/NFS testing.
The project and acceptance criteria remain in progress until evidence is recorded.
