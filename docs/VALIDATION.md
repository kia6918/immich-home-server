# Deployment validation

Evidence from 2026-10-07. These are disposable tests, not a production deployment.

## Automated and static checks

- Python standard-library unittest suite: 70 passing tests on M4 macOS.
- All Bash entry points pass `bash -n`.
- ShellCheck passed with `-x -P SCRIPTDIR` inside disposable Ubuntu.
- Mutation tests: disabling storage identity validation makes root-fallback regression
  fail; disabling migration transaction binding makes wrong-transaction regression fail.
  Off-host Docker override and premature release-cache publication mutations also fail
  their regression tests.
- Actual Docker Compose parser, pinned official v3.2.4 assets: spaces, apostrophes,
  literal dollars, isolated project names and disabled container restart policies pass.

## Live disposable Linux checks

Test environment: M4 ran two isolated Ubuntu 26.04.1 ARM64 VMs, 4 CPU / 8 GiB each.
Docker Engine 29.8.2, Compose 5.6.0, official Immich v3.2.4. No host disk shares.
Synthetic SMB/NFS NAS runs inside one VM. No real NAS or photos involved.

| Workflow | Evidence |
| --- | --- |
| Interactive SSH install | Docker provisioned from official apt repo; exact release pulled; all four services healthy |
| Repeat / Repair | Partial installation recovered without duplicate deployment |
| Local photos / local PostgreSQL | Doctor PASS 8, WARN 0, FAIL 0; paths with spaces work |
| Synthetic upload | Five PNG originals uploaded and authenticated downloads checked byte-for-byte |
| Backup / restore | Logical pg_dump bundle restored into fresh local directory; old directory retained; originals still downloadable |
| Local → SMB reconfiguration | Non-overwriting rsync; 21 files verified; direct Docker CIFS volume; doctor PASS 9 |
| Missing SMB host mount | Guard stops app; explicit start refuses root fallback even with matching marker; underlying directory receives no photos |
| Missing NFS host mount | Guard stops app; explicit start rejects root fallback; underlying directory contains only synthetic marker |
| Same SMB library migration | Matching release, DB-only transfer, original checksums, doctor PASS 9; source stopped and blocked |
| Same-storage rollback | Destination stopped/releases owner before preserved source restarts; source start refused during cutover |
| Controller HTTP access | HTTP ping through explicit loopback SSH tunnel returns pong; tunnel removed |
| SMB → NFS reconfiguration | Non-overwriting copy + manifest verification, native Docker NFS volume; five authenticated originals downloadable |
| New-storage migration | NFS → destination local filesystem; all 21 file checksums verified; logical restore, doctor PASS 8 and five authenticated original downloads pass |
| New-storage rollback | Destination stopped, preserved source restarts using unchanged NFS library/database |
| Explicit update | v3.2.2 → v3.2.4; pre-update DB backup, doctor PASS 8, original checksum checks and authenticated downloads pass; unrelated fixture services/backups preserved |
| Safe uninstall | Full library manifest unchanged; DB/config/valid backups retained; unrelated container ID/running state, network, volume and backup hash unchanged |
| Add SMB/NFS UI | Both native host mounts created interactively; protocol/source/read/write/free-space checks pass; database paths on both mounts refused |
| Interrupted cutover / resume | Destination NFS mount removed after prepare; source reached ownership-released and both writers stayed stopped; remount + same transaction resume passes doctor PASS 9 and five authenticated originals |

## Not yet verified

Both disposable VMs and their network were removed; temporary Lima was uninstalled.
The pre-existing Docker Desktop installation remains, stopped. No Immich remains on M4.
The i5 deployment remains pending.
macOS Intel/Apple Silicon, Linux x86_64, Debian and physical USB adapters have fixture
coverage but no live deployment evidence. Native M4 Docker Desktop could not complete
initial GUI setup while the Mac was locked. No permanent M4 Immich deployment exists.
The i5 endpoint/storage selection and its actual LAN URL remain pending user input.

## Repeating tests

```bash
python3 -m unittest discover -s tests -v
./tests/check-shell.sh
python3 tests/mutation-check.py
python3 tests/compose-smoke.py --compose /path/to/docker-compose
```

`tests/live-assets.py` refuses unmarked hosts. Use only an operator-created
`disposable-test` state marker with an empty disposable installation; `--upload`
creates a dummy admin and five synthetic assets. Never use it on production.
