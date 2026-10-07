# Immich deployment handoff

Updated: 2026-10-07. Goal: reusable SSH Immich deployment repository developed on M4,
then a real i5 deployment with user-selected storage and production backups preserved.

## Current status

- Standalone repository created at `/Users/yqiu/dev/immich-home-server`, branch `dev`.
- Python standard-library implementation with Bash entry points. No Codex dependency
  on targets. Host secrets/state stay outside repository.
- Official documentation checked; latest stable release at this session: `v3.2.4`.
- Core deploy/storage/ownership/supervisor/backup/restore/update/migration implemented.
- Automated suite: 65 tests pass. Shell syntax passes. Actual official Compose v3.2.4
  parsing passed on M4 with spaces, apostrophes and literal dollars. Mutation check
  proved root-fallback regression test fails when identity validation is disabled.
- M4 read-only inventory: macOS 26.6.2, arm64 M4 Pro, 24 GiB RAM. Existing Docker.app
  found, CLI outside normal PATH, daemon unavailable. App opened for disposable testing;
  GUI is locked, so initial setup cannot yet be inspected.
- Requested i5 SSH endpoint and M4 unlock asynchronously; no answer yet.
- Disposable Ubuntu 26.04 ARM64 Lima VM `immich-test` created (8 GiB RAM, 35 GiB sparse
  disk), no host filesystem shares. Real SSH deployment installed Docker from official
  apt repository and Immich v3.2.4. Repair handled a partial install without duplication.
- Real doctor: PASS 8 / WARN 0 / FAIL 0. Five synthetic PNG assets uploaded and downloaded
  via authenticated API with exact checksum matching. Logical backup and fresh-directory
  restore passed; five original assets verified after restore.
- No real photo library/NAS/backup data modified. Temporary SMB/NFS servers and mounts
  exist only in disposable VMs. Live SMB reconfiguration/copy and missing-mount fault
  passed: guard stopped writers, fallback root directory retained only fixture marker.
- Same-SMB-storage migration to second VM `immich-dest` passed (no photo copy), doctor
  PASS 9 / WARN 0 / FAIL 0; authenticated original downloads passed. Source start was
  refused while blocked. Printed rollback command passed; only one server running.
- NFS reconfiguration/copy passed with five authenticated original downloads. New-storage
  migration (NFS to destination local path, full checksum) is currently in progress.
- Both VMs use private Lima network `immich-test-net`, no host filesystem shares or
  automatic service port forwarding. Fixture NAS runs on source guest only.
- Hardened transactions for destination copy/restore/activation/rollback, resumed freeze
  uses saved samples, copy pins destination directory FD, manifests use constant memory.
  Added bounded lock waiting and network helper package provisioning.

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

1. Finish new-storage migration/full checksums and rollback; test NFS missing mount.
   Exercise real explicit version update and safe uninstall with preserved fixture data.
2. Fix lifecycle issues found by live tests; rerun focused unit/mutation/Compose checks.
   Mac native Docker remains unavailable; Intel/macOS/Debian live checks unverified.
3. Record concrete evidence in docs/VALIDATION.md; no production URL is available yet.
4. Commit verified milestones, run shell/static checks and secret exclusion checks.
5. Obtain actual i5 SSH endpoint, inspect services/mounts/backups read-only, present its
   storage candidates to the user. Do not guess its 8TB mount.
6. Deploy only after the user chooses storage; verify Docker/DB/locality/storage guard,
   LAN HTTP, admin/onboarding and a test upload. Record selected library/DB/URL here.
7. Remove only disposable M4 test workloads; leave existing Docker installation intact.

Implemented adapters are not a claim of live Linux/macOS Intel/SMB/NFS testing.
The project and acceptance criteria remain in progress until evidence is recorded.
