# Immich Home Server

A portable, interactive SSH installer with selectable photo storage, local PostgreSQL,
explicit version updates, logical backups, and staged migration. The development Mac
does not become the production server.

```bash
git clone <your-repository-url> immich-home-server
cd immich-home-server
./deploy.sh
```

This repository has no machine addresses, disk names, credentials, or mounted libraries.
The installer asks for the target and storage. Generated state stays on that target in
`~/.config/immich-home-server` (mode 700); secret files have mode 600. The Git remote
must be set to your own repository before the clone command can be used elsewhere.

## Prerequisites and platform support

Python 3.10+, Bash, SSH, tar, and Docker Engine 25+ with the Compose plugin are required.
Linux targets: Ubuntu 22.04/24.04/26.04 and Debian 12/13 on amd64/arm64. The installer can
install Docker from its official apt repository after showing the change. It refuses
to replace existing container runtimes. Other Linux distributions stop with an error.
An adapter in `scripts/environment.py` can add a distribution after validation.

Intel and Apple Silicon macOS use Docker Desktop running Linux containers. Initial
Desktop installation/setup is performed in the target Mac's GUI; SSH can perform the
remaining installation. Python must be installed separately (e.g. Homebrew Python).
Docker Desktop needs an active login session, its VM disk on an internal local disk,
and access to the chosen host folders. Its default VM resources must meet Immich's
requirements. The LaunchAgent starts at login, not before login. A headless Linux
server is the preferred always-on deployment.

Immich currently requires 6 GiB RAM, two cores, and x86-64-v2 for current amd64 machine
learning images. No hardware acceleration is enabled automatically. See the
[official requirements](https://docs.immich.app/install/requirements/).

## Architecture and safety

```
M4 / repository ── SSH code bundle ── deployment host
                                      ├─ storage-aware supervisor
                                      ├─ pinned official Immich Compose services
                                      ├─ PostgreSQL on internal local storage
                                      └─ chosen local / USB / SMB / NFS photo library
```

The installer checks the [current official Compose guidance](https://docs.immich.app/install/docker-compose/),
resolves a stable release to an exact version, retrieves that release's Compose and
example environment files, checks published asset digests where supplied, and records
SHA-256 hashes. It uses Compose itself to parse YAML, then applies a small safety
transformation to the resolved configuration. Unknown upstream service/mount layouts
stop installation rather than being guessed.

One project (`immich-home-server`) owns its containers and network. The installer saves
the existing container/network/volume/listener inventory before creating anything.
No prune, formatting, router changes, broad cleanup, or unattended version upgrades.
Existing production backups are never rotated or edited. New application backups get
unique protected directories.

PostgreSQL defaults to `~/.local/share/immich-home-server/postgres` on Linux and
`~/Library/Application Support/immich-home-server/postgres` on macOS. Local filesystem
and internal disk checks reject SMB/NFS, removable USB storage, and unsafe unknown
filesystems. A logical restore creates a new local PostgreSQL directory and preserves
the old one. A live PostgreSQL data directory is never copied between hosts.

Photo storage is identified by mount target, filesystem type, persistent volume UUID
(local), normalized share/export source (network), relative library path, and a unique
library marker. The check tests read/write access and configurable minimum free space.
Missing storage, root fallback, changed UUID/source, wrong marker, or low free space
prevents startup. Docker cannot create missing bind directories. For SMB/NFS, the
container uses a native Docker network volume with the exact share/export and existing
library subpath. If the host mount directory falls back to local root, the container
still has a real network mount and cannot upload into that local fallback. An isolated
container preflight verifies marker and read/write access. This needs Docker Engine
26+ and Compose with volume-subpath support.

Docker restart policies are disabled; systemd (Linux) or launchd (macOS) owns restarts and checks
storage every 15 seconds. A storage fault stops the stack and requires an explicit
`start` after repair. Timeout-isolated probes prevent a hard NFS mount from hanging
the supervisor forever. Do not start this project manually with raw Docker commands.

An atomic ownership directory in the library prevents two managed deployments from
writing to it. Ownership does not expire automatically. Migration stops and blocks
the source before transferring ownership. Only tooling-managed deployments honor
this lock; an independently configured Immich instance must be stopped manually.

The HTTP port binds only to an explicitly selected private LAN/Tailscale IPv4 address,
never a wildcard/public address. Set a DHCP reservation so it remains stable. No router
ports or public reverse proxy are configured. LAN HTTP is intended for a trusted LAN;
use an HTTPS proxy or VPN separately if you need stronger transport protection.

## Storage selection

`deploy.sh` lists mounted local and network storage with path, filesystem, source,
capacity, free space, and writability. You choose a dedicated library directory. Paths
containing spaces are supported. A custom path's parent must already exist on the
intended storage. The installer never formats disks or initializes a nonempty library.

Supported local filesystems include APFS/HFS+, ext4, XFS, Btrfs, and ZFS with a persistent
UUID. USB photo disks are supported; USB PostgreSQL is refused. FAT/exFAT, unknown FUSE,
and filesystems without trustworthy identity are refused.

For an unmounted share, choose **Add network storage → SMB or NFS**. The installer
requires an empty mount directory, mounts the exact server/share/export, and tests it.
Linux SMB credentials are installed in a root-only `/etc/immich-home-server` file.
macOS uses the native `mount_smbfs` password prompt without persisting passwords in
the repository. Docker's independent SMB mount also needs credentials, stored in
protected target-only volume configuration and the Docker daemon's private volume
metadata. Passwords are never put in command arguments or printed. Docker native SMB
options cannot encode comma/control characters in credentials; use NFS for that case.
Guest SMB is supported. NFS relies on server-side
permissions; configure the exporting NAS for the deployment user/container access.
Linux needs `cifs-utils` for SMB and `nfs-common` for NFS (install these if mount reports
a missing helper).

The tool deliberately does not edit existing `/etc/fstab`, automount, or NAS configuration.
Mounts added interactively must be remounted after reboot, or made persistent using
your OS's mount manager. Until then the supervisor fails closed. On macOS, saved SMB
credentials can be managed with the OS Keychain/Finder. Do not place passwords in
shell history. Inspect `config.env` for the required mount source/path, then run
`./scripts/check-storage.sh` before `./start.sh`.

## Commands

| Task | Command |
| --- | --- |
| Interactive local/SSH deployment | `./deploy.sh` |
| Read-only platform/service/storage inventory | `./inspect.sh` |
| Host-local installation | `./install-local.sh` |
| Start / stop | `./start.sh` / `./stop.sh` |
| Status | `./status.sh` |
| Diagnose, with PASS/WARN/FAIL | `./doctor.sh` |
| Application backup | `./backup.sh` |
| Backup to another existing destination | `./backup.sh --backup-dir '/path/Second Backup'` |
| Restore application backup | `./restore.sh --bundle '/path/application-TIMESTAMP-ID'` |
| Explicit update | `./update.sh --version vX.Y.Z` |
| Migration | `./migrate.sh` |
| Reconfigure storage | `./reconfigure-storage.sh` |
| Safe application removal | `./uninstall.sh` |

Most operational commands accept `--host user@server` and `--ssh-port 22` from the
controller. On the target, use the retained command bundle at
`~/.config/immich-home-server/runtime`. No Codex installation is needed there.

Optional deployment arguments:

```bash
./deploy.sh --host server-user@server --storage '/mnt/photos/Immich' \
  --bind-ip 192.168.1.50 --port 2283 --version v3.2.4
```

Without `--yes`, the plan is shown and confirmed. `--yes` approves the installation
plan only; it never suppresses data restore/migration/update confirmations or Docker
provisioning confirmation. SSH host keys use normal OpenSSH verification; they are
never automatically accepted. An SSH alias supports alternate usernames, ports,
jump hosts, keys, and IPv6 without storing credentials in the repository.

Re-running deployment detects existing state and offers Status, Repair, Reconfigure,
Update, Reinstall while preserving data, or Exit. Partial operations leave maintenance
latches and protected logs rather than silently starting an uncertain database.
To change a host's private bind address, rerun deploy with `--bind-ip <assigned-ip>` and
choose Repair. A stable DHCP reservation is recommended.

## Backup, restore, update, and migration

See [operational procedures](docs/OPERATIONS.md). **Application backups contain database
metadata, configuration, and secrets; they do not contain original photos/videos.**
Arrange a separate library backup. Immich also makes database backups in its library;
those are not a replacement for a separate photo/library backup.

Restore validates bundle checksums, exact Immich version, PostgreSQL image, and library
identity; requires typing `RESTORE`; stops the stack; restores transactionally into a
fresh local directory; and preserves the previous database. Host-specific storage and
network settings are retained; the backup includes prior settings for recovery review.

Update runs doctor, displays release notes and current/target versions, requires
`UPDATE`, stops writers, backs up DB/configuration, fetches the intended release,
starts it, checks health/HTTP, and verifies several stored original assets against
their database checksums. PostgreSQL image changes require review/logical migration
rather than automatic use of an old physical DB with a new image. No automatic
container downgrade against a migrated schema.

Migration supports the same existing library or an rsync copy to an empty new library.
The destination runs the exact source release. Source remains stopped and recoverable.
File names/counts/bytes and a deterministic sample of SHA-256 checksums are compared;
`--full-checksum` verifies every file. New-storage copying needs rsync 3+ and SSH access
**from destination to source**. Database/config backups stream between SSH connections
through the controller without being saved on its disk. Source files are never deleted.
The rollback command is printed before cutover and again on completion/failure.
Resume an interrupted copy with `./migrate.sh --resume --source <alias> --destination
<alias> --transaction <printed-id>`; completed files are not overwritten.

Storage reconfiguration offers an existing copy of the same library, a non-destructive
copy, or cancellation. It never silently moves files or points an existing database at
an unrelated library.

## iPhone and remote access

After installation, follow the printed server URL. Create the first administrator in
the web UI; install the iPhone app, enter the URL, grant full photo access, enable backup
and Background App Refresh, choose backup albums, and keep the phone charging/on Wi-Fi
for the initial upload. No iCloud/iPhone deletion or Apple Photos import is performed.

Tailscale is optional and detected. You can select a Tailscale IPv4 address as the bind
address. A LAN-only bind is not automatically exposed on the Tailscale address; use
a separately configured Tailscale Serve proxy if needed. The installer does not change
Tailscale access controls or enable Funnel/public exposure.

## Tests and validation status

```bash
python3 -m unittest discover -s tests -v
./tests/check-shell.sh
python3 tests/mutation-check.py
# Optional official release parsing without starting containers:
python3 tests/compose-smoke.py --compose 'docker compose'
```

Tests use temporary directories and mount/Docker fixtures. They cover mount parsing,
local/USB/SMB/NFS identification, root fallback, DB locality, immutable library identity,
environment parsing, secret permissions, service isolation, ownership transfer,
copy/backup verification, and migration ordering. Platform support describes implemented
adapters; live platform/mount/deployment evidence is recorded in
[the validation record](docs/VALIDATION.md) and [handoff](docs/HANDOFF.md). A test pass alone does not certify an untested host or NAS.

## Official sources

- [Docker Compose installation](https://docs.immich.app/install/docker-compose/)
- [Immich hardware/storage requirements](https://docs.immich.app/install/requirements/)
- [Database backup/restore](https://docs.immich.app/administration/backup-and-restore/)
- [Version upgrades and compatibility](https://docs.immich.app/install/upgrading/)
- [Official release assets](https://github.com/immich-app/immich/releases)
- [Docker Engine Ubuntu](https://docs.docker.com/engine/install/ubuntu/),
  [Debian](https://docs.docker.com/engine/install/debian/),
  [Docker Desktop macOS](https://docs.docker.com/desktop/setup/install/mac-install/)
