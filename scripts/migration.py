"""Staged cutover; persistent source block prevents simultaneous writers."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tarfile
import tempfile
import uuid

import backup as backups
from common import STATE, SafetyError, atomic_write, choose, confirm, load_config, log, persist, prompt, run, safe_host, save_json, timestamp
import engine
import lifecycle
import operations
import remote
import storage


def export():
    config = load_config()
    storage.bounded_check(config)
    engine.sql('SELECT 1;')
    return config


def freeze(transaction):
    config = load_config()
    if (STATE / 'migration-blocked').exists():
        value = json.loads((STATE / 'migration-blocked').read_text())
        if value.get('transaction') != transaction:
            raise SafetyError('Source is already blocked by another migration')
        return value
    storage.bounded_check(config)
    samples = engine.asset_samples()
    atomic_write(STATE / 'migration-blocked', json.dumps({'transaction': transaction, 'phase': 'freezing'}))
    lifecycle.stop()
    bundle = backups.backup()
    value = {'transaction': transaction, 'phase': 'frozen', 'bundle': str(bundle), 'samples': samples,
             'source_deployment_id': config['deployment_id'], 'library_id': config['storage']['library_id']}
    save_json(STATE / 'migration-blocked', value)
    return value


def handoff_owner(transaction):
    value = json.loads((STATE / 'migration-blocked').read_text())
    if value.get('transaction') != transaction or value.get('phase') != 'frozen':
        raise SafetyError('Source is not frozen for this migration')
    lifecycle.stop()
    storage.bounded_check(load_config())
    lifecycle.release_owner(load_config())
    value['phase'] = 'ownership-released'
    save_json(STATE / 'migration-blocked', value)


def receive_backup():
    directory = STATE / 'backups' / ('received-' + timestamp() + '-' + os.urandom(4).hex())
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tarfile.open(fileobj=sys.stdin.buffer, mode='r|gz') as archive:
        for member in archive:
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isdir() or member.isfile()):
                raise SafetyError('Unsafe member in incoming backup')
            destination = directory / path
            if member.isdir():
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with destination.open('xb') as output:
                    os.chmod(destination, 0o600)
                    shutil.copyfileobj(archive.extractfile(member), output)
    backups.validate_bundle(directory)
    return {'bundle': str(directory)}


def activate(transaction, samples):
    config = load_config()
    storage.bounded_check(config)
    block = STATE / 'migration-blocked'
    save_json(STATE / 'migration-destination.json', {'transaction': transaction, 'phase': 'starting',
              'source_samples': samples})
    block.unlink(missing_ok=True)
    try:
        lifecycle.start(config)
        engine.verify_assets(config, samples)
        if not lifecycle.doctor():
            raise SafetyError('Destination failed final doctor')
        save_json(STATE / 'migration-destination.json', {'transaction': transaction, 'phase': 'healthy',
                  'source_samples': samples})
    except BaseException:
        lifecycle.stop()
        save_json(block, {'transaction': transaction, 'phase': 'failed-destination'})
        raise


def rollback_source(transaction):
    config = load_config()
    block = json.loads((STATE / 'migration-blocked').read_text())
    if block.get('transaction') != transaction:
        raise SafetyError('Rollback transaction mismatch')
    storage.bounded_check(config)
    lifecycle.claim(config)  # Refuses if destination still owns shared storage.
    (STATE / 'migration-blocked').unlink()
    lifecycle.start(config)


def coordinate(args):
    source = safe_host(args.source or prompt('Source SSH host'))
    destination = safe_host(args.destination or prompt('Destination SSH host'))
    if source == destination:
        raise SafetyError('Source and destination must be different hosts')
    port = args.ssh_port
    src_dir = remote.bootstrap(source, port)
    dst_dir = remote.bootstrap(destination, port)
    config = json.loads(remote.execute(source, '_export', port=port, directory=src_dir).stdout)
    print(f'Source version: {config["version"]}\nSource storage: {config["storage"]["source"]}\n'
          f'Source library: {config["storage"]["path"]}')
    mode = choose('Destination photo storage', ['Use the same existing storage', 'Copy library to another storage', 'Cancel'])
    if mode == 2:
        return
    transaction = str(uuid.uuid4())
    # Prepare matching release before stopping source. Destination app cannot start.
    arguments = ['--source-json', json.dumps(config), '--version', config['version']]
    if mode == 0:
        arguments += ['--same-storage']
    remote.execute(destination, '_prepare', arguments, port=port, directory=dst_dir, tty=True, capture=False, timeout=7200)
    dest_config = json.loads(remote.execute(destination, '_config', port=port, directory=dst_dir).stdout)
    same = storage.same_library(config['storage'], dest_config['storage'])
    if (mode == 0) != same:
        raise SafetyError('Selected migration mode disagrees with persisted disk/share/library identity; source remains running')
    print(f'Source: {source} → {config["storage"]["path"]}\nDestination: {destination} → {dest_config["storage"]["path"]}')
    confirm('Freeze source uploads, back up database, transfer and validate destination?', 'MIGRATE')
    frozen = json.loads(remote.execute(source, '_freeze', ['--transaction', transaction],
                        port=port, directory=src_dir, timeout=1800).stdout)
    log('Source stopped and durably blocked; source configuration and data preserved')
    rollback = f'./migrate.sh --rollback --source {source} --destination {destination} --transaction {transaction} --ssh-port {port}'
    print('Rollback command: ' + rollback)
    try:
        if not same:
            copy_remote_library(source, destination, config, dest_config, port, src_dir, dst_dir, full=args.full_checksum)
        bundle = remote.relay_backup(source, destination, frozen['bundle'], source_port=port, destination_port=port)
        remote.execute(destination, '_restore-migration', ['--bundle', bundle], port=port, directory=dst_dir,
                       capture=False, timeout=1800)
        if same:
            remote.execute(source, '_release', ['--transaction', transaction], port=port, directory=src_dir)
        remote.execute(destination, '_activate', ['--transaction', transaction, '--samples-json', json.dumps(frozen['samples'])],
                       port=port, directory=dst_dir, capture=False, timeout=1800)
        url = f'http://{dest_config["bind_ip"]}:{dest_config["port"]}'
        remote.execute(destination, 'status', port=port, directory=dst_dir, capture=False)
        print(f'\nMigration complete.\nOld server: Stopped but preserved.\nNew server: Healthy.\n'
              f'Photo storage: Verified.\nURL: {url}\nRollback command: {rollback}')
        log('If destination accepts new uploads, reverse-migrate its current database/library before rollback to avoid losing those uploads.', 'WARN')
    except BaseException:
        log('Migration paused safely. Source remains preserved and blocked. Destination must be stopped before rollback.', 'ERROR')
        print('Rollback command: ' + rollback)
        raise


def copy_remote_library(source, destination, src, dst, port, src_dir, dst_dir, *, full=False):
    arguments = ['--full-checksum'] if full else []
    expected = json.loads(remote.execute(source, '_manifest', arguments, port=port, directory=src_dir, timeout=7200).stdout)
    free = json.loads(remote.execute(destination, '_space', port=port, directory=dst_dir).stdout)['free']
    if free < expected['bytes'] * 1.15 + 10 * 1024**3:
        raise SafetyError('Insufficient destination capacity for library plus headroom')
    print(f'Copy estimate: {expected["bytes"] / 1024**3:.2f} GiB, {expected["count"]} files')
    confirm('Destination must be able to SSH to source; keys/password are handled by SSH there. Copy without overwriting existing files?', 'COPY')
    remote.execute(destination, '_copy-from', ['--source', source, '--source-path', src['storage']['path'], '--ssh-port', str(port)],
                   port=port, directory=dst_dir, tty=True, capture=False, timeout=7 * 24 * 3600)
    actual = json.loads(remote.execute(destination, '_manifest', arguments, port=port, directory=dst_dir, timeout=7200).stdout)
    operations.verify_manifest(expected, actual)


def rollback(args):
    if not args.source or not args.destination or not args.transaction:
        raise SafetyError('Rollback needs source, destination and transaction printed by migration')
    confirm('Stop destination, release its ownership, and restart preserved source? Destination uploads after cutover require reverse migration first.', 'ROLLBACK')
    port = args.ssh_port
    remote.execute(args.destination, '_rollback-destination', ['--transaction', args.transaction], port=port, capture=False)
    remote.execute(args.source, '_rollback-source', ['--transaction', args.transaction], port=port, capture=False)
    log('Preserved source restarted; destination stopped and blocked')
