#!/usr/bin/env python3
"""Public CLI and narrowly scoped SSH worker commands."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import sys

from common import STATE, SafetyError, atomic_write, choose, load_config, log, operation_lock, prompt, save_json
import backup
import engine
import installer
import lifecycle
import migration
import operations
import remote
import storage


def parser():
    result = argparse.ArgumentParser(description='Portable Immich installer. See README.md for safety and prerequisites.')
    result.add_argument('command', nargs='?', default='deploy', choices=[
        'deploy', 'install', 'inspect', 'start', 'stop', 'status', 'doctor', 'backup', 'restore', 'update',
        'uninstall', 'reconfigure', 'migrate', 'guard', 'detect-os', 'detect-storage', 'select-storage', 'install-docker', 'check-storage',
        '_probe', '_prepare', '_export', '_config', '_freeze', '_release', '_receive-backup',
        '_restore-migration', '_activate', '_manifest', '_space', '_copy-from', '_rollback-source', '_rollback-destination'])
    for option in ('host', 'storage', 'db-path', 'bind-ip', 'version', 'timezone', 'bundle', 'source',
                   'destination', 'source-path', 'transaction', 'source-json', 'samples-json', 'config-json', 'backup-dir'):
        result.add_argument('--' + option)
    result.add_argument('--port', type=int)
    result.add_argument('--ssh-port', type=int)
    for option in ('yes', 'same-storage', 'full-checksum', 'rollback', 'resume'):
        result.add_argument('--' + option, action='store_true')
    return result


def worker(args):
    command = args.command
    if command in {'_config', '_export'}:
        return load_config() if command == '_config' else migration.export()
    if command == '_probe':
        config = json.loads(args.config_json)
        usage = storage.check(config['storage'], config.get('min_free_gib', 10))
        return {'free': usage.free}
    if command == '_freeze':
        return migration.freeze(args.transaction)
    if command == '_release':
        migration.handoff_owner(args.transaction)
        return {'released': True}
    if command == '_receive-backup':
        return migration.receive_backup()
    if command == '_manifest':
        return operations.library_manifest(load_config()['storage']['path'], full=args.full_checksum)
    if command == '_space':
        config = load_config()
        storage.bounded_check(config)
        if not (STATE / 'copy-job.json').exists():
            operations.ensure_empty_destination(config['storage']['path'])
        return {'free': shutil.disk_usage(config['storage']['path']).free}
    if command == '_rollback-source':
        migration.rollback_source(args.transaction)
        return {'restored': True}
    if command == '_rollback-destination':
        config = load_config()
        transaction_file = STATE / 'migration-destination.json'
        pending = transaction_file if transaction_file.exists() else STATE / 'migration-blocked'
        if not pending.exists() or json.loads(pending.read_text()).get('transaction') != args.transaction:
            raise SafetyError('Destination belongs to a different transaction')
        lifecycle.stop()
        save_json(STATE / 'migration-blocked', {'transaction': args.transaction, 'phase': 'rolled-back-destination'})
        owner = Path(config['storage']['path']) / '.immich-home-server-owner/owner.json'
        if owner.exists() and json.loads(owner.read_text()).get('deployment_id') == config['deployment_id']:
            lifecycle.release_owner(config)
        return {'stopped': True}
    raise SafetyError('Unknown worker command')


def main():
    args = parser().parse_args()
    command = args.command
    if command == 'deploy':
        print('\nImmich Home Server Installer')
        if not args.host:
            target = choose('Deployment target', ['This computer', 'Remote computer over SSH'])
            if target == 1:
                args.host = prompt('SSH host (alias or user@host)')
        if args.host:
            remote.deploy(args)
            return
        command = 'install'
    if command == 'migrate':
        migration.rollback(args) if args.rollback else migration.coordinate(args)
        return
    if args.host:
        arguments = []
        for flag, value in (('--version', args.version), ('--bundle', args.bundle), ('--backup-dir', args.backup_dir)):
            if value is not None:
                arguments += [flag, value]
        remote.execute(args.host, command, arguments, port=args.ssh_port,
                       tty=command in {'install', 'restore', 'update', 'uninstall', 'reconfigure', 'select-storage', 'install-docker'}, capture=False)
        return
    if command == 'guard':
        lifecycle.guard()
        return
    # Read-only/continuous commands must not hold the deployment lock while waiting on a worker.
    if command == '_probe':
        print(json.dumps(worker(args)))
        return
    with operation_lock():
        if command == 'install':
            installer.install(args)
        elif command == 'inspect':
            installer.inspect()
        elif command == 'detect-os':
            import environment
            print(json.dumps(environment.platform_info(), indent=2))
        elif command == 'detect-storage':
            for path in storage.candidates():
                print(path)
                storage.show_candidate(path)
        elif command == 'select-storage':
            print(storage.select_path())
        elif command == 'install-docker':
            import environment
            environment.install_docker()
        elif command == 'check-storage':
            storage.bounded_check(load_config())
            log('Storage verified')
        elif command == 'start':
            lifecycle.start()
        elif command == 'stop':
            lifecycle.stop()
        elif command == 'status':
            lifecycle.status()
        elif command == 'doctor':
            if not lifecycle.doctor():
                sys.exit(1)
        elif command == 'backup':
            backup.backup(args.backup_dir)
        elif command == 'restore':
            bundle = args.bundle or prompt('Application backup directory')
            if not (STATE / 'config.json').exists():
                checked, _manifest = backup.validate_bundle(bundle)
                source = json.loads((checked / 'config.json').read_text())
                from common import confirm
                confirm('Prepare a new host using this backup\'s exact release? Select its existing photo library; the old writer must be stopped/fenced.', 'PREPARE')
                args.same_storage = True
                installer.install(args, prepare=True, source=source)
                backup.restore(bundle, migration=True)
                (STATE / 'migration-blocked').unlink()
                lifecycle.start()
                engine.verify_assets(load_config(), engine.asset_samples())
            else:
                backup.restore(bundle)
        elif command == 'update':
            operations.update(args.version)
        elif command == 'uninstall':
            operations.uninstall()
        elif command == 'reconfigure':
            operations.reconfigure()
        elif command == '_prepare':
            installer.install(args, prepare=True, source=json.loads(args.source_json))
        elif command == '_restore-migration':
            migration.validate_destination_transaction(args.transaction)
            backup.restore(args.bundle, confirmed=True, migration=True)
        elif command == '_activate':
            migration.activate(args.transaction, json.loads(args.samples_json))
        elif command == '_copy-from':
            migration.validate_destination_transaction(args.transaction)
            config = load_config()
            storage.bounded_check(config)
            source = args.source_path
            if not source or not source.startswith('/') or any(c in source for c in '\n\r\x00'):
                raise SafetyError('Invalid remote library path')
            job = {'source': args.source, 'source_path': source, 'destination': config['storage']['path']}
            receipt = STATE / 'copy-job.json'
            if receipt.exists():
                if json.loads(receipt.read_text()) != job:
                    raise SafetyError('An unrelated copy job already exists')
            else:
                operations.ensure_empty_destination(config['storage']['path'])
                save_json(receipt, job)
            from common import safe_host
            safe_host(args.source)
            operations.rsync_copy(args.source + ':' + source.rstrip('/') + '/',
                                  config['storage']['path'].rstrip('/') + '/',
                                  ssh='ssh' + (' -p ' + str(args.ssh_port) if args.ssh_port else '') + ' -o ConnectTimeout=10',
                                  destination_storage=config['storage'])
        elif command.startswith('_'):
            with contextlib.redirect_stdout(sys.stderr):
                value = worker(args)
            print(json.dumps(value))


if __name__ == '__main__':
    os.umask(0o077)
    try:
        main()
    except (SafetyError, OSError, ValueError, KeyError) as exc:
        print('[ERROR] ' + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        log('Interrupted; state and data preserved. Inspect doctor and handoff before resuming.', 'WARN')
        sys.exit(130)
