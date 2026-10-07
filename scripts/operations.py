"""Explicit update, safe uninstall, and non-destructive storage copy/reconfiguration."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import backup as backups
from common import STATE, SafetyError, atomic_write, choose, confirm, load_config, log, persist, prompt, run, save_json, timestamp
import engine
import installer
import lifecycle
import storage


def update(target=None):
    config = load_config()
    if not lifecycle.doctor():
        raise SafetyError('Resolve doctor failures before update')
    storage.bounded_check(config)
    metadata = engine.release(target)
    version = metadata['tag_name']
    current_tuple = tuple(map(int, config['version'][1:].split('.')))
    target_tuple = tuple(map(int, version[1:].split('.')))
    if target_tuple <= current_tuple:
        raise SafetyError('Target must be a newer exact release; downgrades are unsupported')
    print(f'CURRENT_VERSION={config["version"]}\nTARGET_VERSION={version}\nRelease notes: {metadata["html_url"]}')
    print(metadata.get('body', ''))
    confirm('Read the release notes and required intermediate upgrades. Proceed with this explicit update?', 'UPDATE')
    samples = engine.asset_samples()
    lifecycle.stop()
    atomic_write(STATE / 'maintenance', 'update in progress\n')
    bundle = backups.backup()
    old_image = backups.database_image()
    config['target_version'] = version
    persist(config)
    official = engine.ensure_release(version, metadata)
    next_config = dict(config, version=version)
    engine.generate_env(next_config)
    engine.build_compose(next_config, official)
    if backups.database_image() != old_image:
        # Never let a PG image change mutate an existing physical data directory.
        engine.generate_env(config)
        engine.build_compose(config, STATE / 'official' / config['version'])
        log(f'Backup preserved at {bundle}; old deployment remains stopped.', 'WARN')
        raise SafetyError('PostgreSQL image changed. Use matching-version migration with logical restore after reviewing upstream database upgrade requirements.')
    persist(next_config)
    try:
        log('Pulling official images for explicit target ' + version)
        engine.compose('pull', '--quiet', timeout=1800, capture=False)
        storage.bounded_check(next_config)
        (STATE / 'maintenance').unlink()
        lifecycle.start(next_config)
        engine.verify_assets(next_config, samples)
        if not lifecycle.doctor():
            raise SafetyError('Updated deployment failed doctor')
        log(f'Update complete: {version}. Pre-update application backup: {bundle}')
    except BaseException:
        lifecycle.stop()
        atomic_write(STATE / 'maintenance', 'update failed; use pre-update backup and original version for rollback\n')
        log('Update failed; stopped for recovery. Do not downgrade images against the migrated DB. See docs/OPERATIONS.md.', 'ERROR')
        raise


def library_manifest(root, *, full=False):
    import hashlib
    root = Path(root)
    count, size, checksum_count = 0, 0, 0
    entries, checksums = hashlib.sha256(), hashlib.sha256()
    def traversal_error(error):
        raise SafetyError('Cannot inspect every library directory: ' + str(error))
    for current, directories, names in os.walk(root, followlinks=False, onerror=traversal_error):
        directories[:] = sorted(d for d in directories if not d.startswith(('.immich-home-server-owner', '.immich-owner-history', '.immich-partial', '.immich-docker-probe-')))
        for directory in directories:
            if (Path(current) / directory).is_symlink():
                raise SafetyError('Library contains directory symlinks; manually map external libraries')
        for name in sorted(names):
            if name.startswith(('.immich-home-server-', '.immich-write-check-')):
                continue
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                raise SafetyError('Library contains symlinks/special files; copy refused')
            relative = str(path.relative_to(root))
            stat = path.stat()
            count += 1
            size += stat.st_size
            entries.update((json.dumps([relative, stat.st_size, int(stat.st_mtime)], ensure_ascii=True) + '\n').encode())
            # Deterministic sample across whole tree; all files checked when full=True.
            if full or count == 1 or int(hashlib.sha256(relative.encode()).hexdigest()[:8], 16) % 100 == 0:
                checksums.update((json.dumps([relative, backups.file_hash(path)], ensure_ascii=True) + '\n').encode())
                checksum_count += 1
    return {'count': count, 'bytes': size, 'entries_sha256': entries.hexdigest(),
            'checksums_sha256': checksums.hexdigest(), 'checksum_files': checksum_count, 'full_checksum': full}


def verify_manifest(expected, actual):
    if expected != actual:
        raise SafetyError('Library verification failed: file names/counts/bytes/checksums differ')


def ensure_empty_destination(path):
    unexpected = [p.name for p in Path(path).iterdir() if p.name != '.immich-home-server-library.json']
    if unexpected:
        raise SafetyError('Copy destination is not empty. Existing files will not be overwritten.')


def copy_library(source, destination, *, full=False):
    source_path, dest_path = Path(source['path']), Path(destination['path'])
    if source_path == dest_path or source_path in dest_path.parents or dest_path in source_path.parents:
        raise SafetyError('Source and destination overlap')
    storage.check(source)
    storage.check(destination)
    ensure_empty_destination(dest_path)
    expected = library_manifest(source_path, full=full)
    if shutil.disk_usage(dest_path).free < expected['bytes'] * 1.15 + 10 * 1024**3:
        raise SafetyError('Destination needs library size plus 15% headroom and 10 GiB free')
    print(f'Source: {source_path}\nDestination: {dest_path}\nData size: {expected["bytes"] / 1024**3:.2f} GiB / {expected["count"]} files')
    confirm('Copy this library without deleting source files or overwriting destination files?', 'COPY')
    rsync_copy(str(source_path) + '/', str(dest_path) + '/', destination_storage=destination)
    verify_manifest(expected, library_manifest(dest_path, full=full))
    log('Library copy verified by names, file count, byte count, and checksums')


def rsync_copy(source, destination, *, ssh=None, destination_storage=None):
    version = run(['rsync', '--version']).stdout
    args = ['rsync', '-a', '--partial', '--partial-dir=.immich-partial', '--ignore-existing', '--no-owner', '--no-group', '--progress']
    # Apple's bundled rsync does not support -s; argv still preserves spaces for local paths.
    if 'version 3.' in version:
        args += ['--protect-args']
    elif ssh:
        raise SafetyError('Remote copy with paths containing spaces requires rsync 3+ on both hosts')
    args += ['--exclude=.immich-home-server-*', '--exclude=.immich-owner-history-*', '--exclude=.immich-write-check-*']
    if ssh:
        args += ['-e', ssh]
    # Source/destination always absolute or validated user@host:/absolute/path; no shell evaluation.
    fd = os.open(destination.rstrip('/'), os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Pin the already selected destination directory before starting rsync. Even a
        # lazy unmount after this point cannot redirect relative writes to host root.
        if os.fstat(fd).st_dev != os.stat(destination).st_dev:
            raise SafetyError('Copy destination filesystem changed while opening it')
        if destination_storage:
            storage.check(destination_storage)
        run(args + ['--', source, './'], capture=False, timeout=7 * 24 * 3600,
            pass_fds=(fd,), preexec_fn=lambda: os.fchdir(fd))
    finally:
        os.close(fd)


def reconfigure():
    config = load_config()
    choice = choose('Reconfigure photo storage', ['Point to an existing copy of this Immich library', 'Copy existing library to new storage', 'Cancel'])
    if choice == 2:
        return
    path = storage.select_path()
    print(f'Source: {config["storage"]["path"]}\nDestination: {path}')
    confirm('Stop this deployment and reconfigure photo storage? Files at the source remain preserved.', 'RECONFIGURE')
    samples = engine.asset_samples()
    lifecycle.stop()
    atomic_write(STATE / 'maintenance', 'storage reconfiguration pending verification\n')
    backups.backup()
    old = dict(config)
    save_json(STATE / ('pre-storage-' + timestamp() + '.json'), old)
    destination = installer.configure_library(path, library_id=config['storage']['library_id'], existing=choice == 0)
    if choice == 1:
        copy_library(config['storage'], destination)
    else:
        engine.verify_assets(dict(config, storage=destination), [])
        verify_manifest(library_manifest(config['storage']['path']), library_manifest(destination['path']))
    if storage.same_library(config['storage'], destination):
        raise SafetyError('Same underlying library; no storage reconfiguration needed. Maintenance latch preserved for explicit recovery.')
    config['storage'] = destination
    storage.configure_docker_volume(config)
    persist(config)
    engine.generate_env(config)
    engine.build_compose(config, STATE / 'official' / config['version'])
    (STATE / 'maintenance').unlink()
    try:
        lifecycle.start(config)
        engine.verify_assets(config, samples)
    except BaseException:
        lifecycle.stop()
        atomic_write(STATE / 'maintenance', 'storage reconfiguration failed; use pre-storage config for rollback\n')
        raise
    log('Storage reconfigured; source data remains preserved')


def uninstall():
    config = load_config()
    confirm('Stop and remove ONLY this Immich project\'s containers/network? Photo library, DB directory, backups and config remain.', 'UNINSTALL')
    lifecycle.stop()
    backups.backup()  # DB container stopped: bring only database up to take final logical backup.
    engine.compose('down', timeout=120)  # No -v, --rmi, or orphan removal.
    if os.uname().sysname == 'Linux':
        run(['sudo', 'systemctl', 'disable', '--now', 'immich-home-server.service'], capture=False)
    else:
        path = Path.home() / 'Library/LaunchAgents/org.immich-home-server.guard.plist'
        run(['launchctl', 'bootout', f'gui/{os.getuid()}', path], check=False)
    log('Application removed. Photos, database, backups, model cache and configuration retained. No files deleted.')
