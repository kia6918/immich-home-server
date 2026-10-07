# Operational procedures

## Before using a production host

Run `./inspect.sh` locally on the target (or use the deployment bundle over SSH).
Review the saved inventory, storage candidates and existing listeners. Select a new
dedicated Immich library directory; leave backup jobs, disks and existing backup trees
alone. Reserve a LAN IP. The installer binds only that address and never edits router
or host firewall policies. Preserve normal SSH known-host verification.

## Backups

`./backup.sh` creates a logical `pg_dump --clean --if-exists` compressed SQL backup and
copies the exact Compose/configuration/env/release assets into a unique bundle. The
manifest stores the application release, PostgreSQL image, library ID and file hashes.
Successful bundles contain `manifest.json`; partial bundles are retained for diagnosis
and never mistaken for complete backups. Existing backup bundles are never overwritten
or pruned. Protect the whole bundle because `.env` contains the database password.

Database/configuration backups do **not** protect original photos/videos. Back up the
entire photo library separately (including library/upload/profile and generated data)
using your existing backup system. Quiesce uploads during a paired DB+library snapshot
so both represent the same point in time. Do not mix independent versions of those
snapshots. External Immich libraries require separately preserving their mount paths.

## Restore and rollback after an update

Use the exact original Immich release and PostgreSQL image from the backup manifest.
`./restore.sh --bundle ...` restores into a fresh local PostgreSQL directory; it never
deletes the old physical database. It preserves current host/storage/LAN settings,
verifies library ID, and requires `RESTORE`. If restoring onto a new host, use migration
preparation and the backup's matching release, then validate the original library.

An update failure leaves `maintenance` in the protected state directory and the stack
stopped. Do not simply downgrade images: Immich does not support database downgrades.
For rollback, stop the supervisor's requested-start state with `stop.sh`, review the
pre-update bundle and original `config.json`, restore its exact release configuration,
regenerate Compose, then use logical restore to a fresh directory. The `Repair` menu
allows explicit `RECOVER` after logs and state have been reviewed. Photos created after
the chosen backup need separate reconciliation; preserve both library and DB versions.

## Migration

1. Run `./migrate.sh` from the controller. Enter source and destination SSH hosts.
2. Source is checked and version/storage metadata retrieved (no passwords printed).
3. Choose same library or copy to a new empty library.
4. Destination's interactive installer selects storage, local DB and bind address;
   it downloads/pulls the matching source release and stays blocked/stopped.
5. Type `MIGRATE`. Source gets a durable migration block and stops before the DB dump.
6. For a copy, confirm size/capacity and type `COPY`; destination rsync pulls from source.
7. Application backup is streamed over SSH and checksummed at destination.
8. Destination restores into a fresh local DB without starting Immich.
9. Same-storage migration releases source's library ownership only after its server is
   stopped. Destination atomically claims ownership and starts.
10. Health, HTTP, upload bind and sampled existing asset checks must pass. Source is
    kept stopped/blocked, with configuration, database and original backup preserved.

Different hostnames that alias the same NAS cannot be automatically proven identical.
Use a consistent share/export hostname and the same library subdirectory/marker. A
share path or library ID mismatch stops same-storage migration. Don't copy when both
paths reference the same filesystem/library. External libraries and arbitrary manual
Compose edits need manual mount mapping and are deliberately rejected by sampled-asset
validation rather than silently losing asset access.

rsync copies keep timestamps/permissions/hierarchy and use partial files without source
deletion or destination overwrite. The receiver holds its selected directory open and
writes relative to that filesystem, preventing an unmount from redirecting copies to
the local directory underneath. Manifests use bounded memory even for large libraries.
SSH permissions and NAS UID mappings must permit
the chosen paths. Hard links/extended attributes/ACLs are not assumed portable between
SMB, NFS, APFS and Linux; original photo bytes and regular-file timestamps are verified.

The rollback command printed by migration stops destination, blocks it, releases its
ownership if held, then claims and starts the preserved source. It validates the
transaction ID. **Rollback after new destination uploads needs a reverse migration of
the current destination DB/library**; the preserved old DB predates those uploads.
Both deployments remain recoverable. Manually remove the old application only after
validation and a separate verified library backup.

The transaction ID and rollback command are printed before freezing the source. To
continue a failed transfer/restore, rerun with the same hosts and
`--resume --transaction <printed-id>` (and `--full-checksum` if used initially).
The destination copy, restore and activation must match its prepared transaction.
Source remains blocked throughout retries. Never resume after rollback or after the
destination has begun accepting new uploads.

## Interrupted operations

Inspect protected `config.json`, `maintenance`, `migration-blocked`, `copy-job.json`,
`migration-destination.json`, backup manifests, and guard/restore logs. Do not remove
ownership directories by guessing whether a host is offline. The source must be stopped
and destination must be stopped before ownership is reclaimed. A failed restore retains
its new DB directory and restores previous configuration, with maintenance still latched.
Repair can resume application setup after review without deleting data.

## Safe uninstall

`./uninstall.sh` requires `UNINSTALL`, stops this stack, makes a final logical DB backup,
removes only this Compose project's containers/network, and disables its supervisor.
It keeps photos, physical DB directories, logical backups, model-cache volume and config.
There is deliberately no automatic photo/database deletion option. Review and remove
application/database files manually only after explicit operator confirmation and
validated backups. Docker and unrelated services remain installed.
