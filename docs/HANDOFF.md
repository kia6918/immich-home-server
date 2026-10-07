# Immich deployment handoff

Updated: 2026-10-07. Goal: portable SSH Immich deployment tooling developed on M4,
then deploy to the existing i5 with user-selected storage and backups preserved.

## Current status

- Public repository: https://github.com/kia6918/immich-home-server, branch `dev`.
  `origin` points to this repository. No monorepo applications changed.
- Publication requested explicitly. The full Git history passed Gitleaks 8.30.1
  with zero findings; the temporary scanner's published checksum was verified.
  Generated secrets, host configuration, backups and libraries remain excluded.
  README clone instructions now use the public repository URL.
- GitHub visibility is PUBLIC and the default branch is `dev`. An anonymous HTTPS
  clone verified public access, the pushed commit and executable deployment entry point.
  Publication checks passed: 70 local tests, shell syntax and whitespace checks;
  [GitHub Actions run 37657645316](https://github.com/kia6918/immich-home-server/actions/runs/37657645316)
  passed all four Linux/macOS × Python 3.10/3.14 jobs. Temporary scan/clone tools are removed.
- Core interactive local/SSH deployment, storage selection, network setup, supervision,
  explicit update, backup/restore, migration/rollback/resume and safe uninstall implemented.
- Bash entry points delegate to Python 3.10+ standard-library code. Targets need no Codex.
  Host state/secrets stay outside Git in `~/.config/immich-home-server`.
- Current official stable release checked and tested: v3.2.4. Release-specific official
  Compose/example env retrieved, pinned and checksummed; partial caches can be repaired
  without overwriting their old contents. Unknown upstream layouts stop safely.
- Verification: 70 unit/workflow tests pass, shell syntax and ShellCheck pass, actual
  Compose parsing with spaces/apostrophes/literal dollars passes. Four mutation checks
  demonstrate regressions fail without identity/transaction/Docker/cache protections.
- Real disposable Ubuntu 26.04.1 ARM64 / Docker 29.8.2 / Compose 5.6.0 tests passed:
  SSH install/Repair, five synthetic authenticated uploads/downloads, logical backup
  and fresh-directory restore, local/SMB/NFS libraries, both missing-mount guards,
  same-storage and new-storage migrations, full-copy checksums, both rollback modes,
  interrupted ownership-released cutover/resume, explicit v3.2.2 → v3.2.4 update,
  safe uninstall and preservation of unrelated Docker/backup fixtures.
- Concrete evidence is in `docs/VALIDATION.md`. No real photo library, NAS or production
  backup was used or modified. Application backups explicitly exclude photo bytes.
- Both disposable Lima VMs and their network are deleted; temporary Lima package is
  uninstalled. Native Docker Desktop opened during testing is stopped; its pre-existing
  installation is retained. No Immich deployment remains on the M4.
- i5 SSH endpoint was requested but no answer received. i5 has not been touched.
  No production LAN URL, photo path or PostgreSQL path has been selected/verified yet.
- macOS Intel/Apple Silicon, Linux x86_64, Debian and physical USB have implemented
  adapters/fixture coverage; live verification is still needed on those platforms.
  Native M4 Docker setup could not finish while its GUI was locked.

## Important decisions and files

- `scripts/storage.py`: filesystem/UUID/source/marker checks, root-fallback rejection,
  internal DB disk validation, interactive mounted/local/USB/SMB/NFS selection.
- `scripts/engine.py`: official pinned Compose, private HTTP bind, isolated service names,
  disabled container restart policies, direct Docker NAS volumes with library subpaths.
- `scripts/lifecycle.py`: systemd/LaunchAgent storage guard and persistent atomic ownership.
- `scripts/backup.py`: protected pg_dump bundles; exact-version fresh-directory restore.
- `scripts/migration.py`: freeze/block → backup/copy → restore → ownership handoff → verify;
  destination workers bound to transaction; preserved source rollback, resumable copies.
- `scripts/remote.py`: ~42 KiB public code bundle; encrypted backup streams without M4 files.
- `scripts/installer.py`, `operations.py`, `cli.py`: interactive and operational workflows.
- `tests/`: temporary/mock safety tests, mutations, real Compose parser, disposable assets.
- `README.md`, `docs/OPERATIONS.md`: installation, limitations, migration/recovery commands.

## Next steps

1. Obtain the i5 SSH alias or `user@host`; inspect OS/resources/mounts/services/listeners
   read-only using `./inspect.sh --host <target>`. Preserve normal SSH host-key handling.
2. Present detected storage candidates to the user. The user must choose the 8TB storage
   and a dedicated Immich directory; never infer its identity or touch backup folders.
3. Run interactive `./deploy.sh` over SSH. Keep PostgreSQL and Docker VM/data on verified
   internal local storage. Provision only missing supported dependencies; preserve
   unrelated services. On a Mac, Docker Desktop initial GUI setup/login is prerequisite.
4. Validate doctor, LAN HTTP from M4, selected storage, DB locality and guard. Complete
   admin/iPhone onboarding and an authorized test upload. Record URL/paths/results here.
5. Live-test remaining physical platforms when available; do not label fixture coverage
   as real deployment validation.

The real i5 acceptance criteria remain pending; do not call the overall project complete.
