"""Logical database backups and non-destructive fresh-directory restores."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from common import STATE, SafetyError, atomic_write, confirm, load_config, log, persist, private_dir, run, save_json, timestamp
import engine
import lifecycle
import storage


def backup(destination=None):
    config = load_config()
    storage.validate_db(config['db_path'])
    storage.validate_identity(config['db_storage'], storage.describe(config['db_path']))
    # Quiesced update/migration/uninstall workflows stop writers first. Start only DB.
    engine.compose('up', '-d', 'database', timeout=600)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if engine.compose('exec', '-T', 'database', 'pg_isready', '-U', 'postgres', '-d', 'immich', check=False).returncode == 0:
            break
        time.sleep(2)
    else:
        raise SafetyError('Database unavailable for logical backup')
    base = Path(destination).expanduser().absolute() if destination else STATE / 'backups'
    # Never chmod or modify an existing production backup root. Only own our newly created bundle.
    if not base.exists():
        base.mkdir(mode=0o700, parents=True)
    if base.is_symlink():
        raise SafetyError('Backup destination cannot be a symlink')
    bundle = base / ('application-' + timestamp() + '-' + os.urandom(4).hex())
    bundle.mkdir(mode=0o700, exist_ok=False)
    partial = bundle / 'database.sql.gz.partial'
    args = engine.docker_prefix() + ['compose', '-p', engine.PROJECT, '--project-directory', str(STATE),
           '--env-file', str(STATE / '.env'), '-f', str(STATE / 'compose.json'), 'exec', '-T', 'database',
           'pg_dump', '--clean', '--if-exists', '--username=postgres', '--dbname=immich']
    with partial.open('xb') as output, (bundle / 'dump-error.log').open('xb') as error:
        output.chmod(0o600) if hasattr(output, 'chmod') else os.chmod(partial, 0o600)
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=error) as proc:
            with gzip.GzipFile(fileobj=output, mode='wb') as compressed:
                shutil.copyfileobj(proc.stdout, compressed)
            if proc.wait() != 0:
                raise SafetyError('Database backup failed; partial bundle preserved for diagnosis')
    if partial.stat().st_size < 100:
        raise SafetyError('Database dump unexpectedly small')
    partial.rename(bundle / 'database.sql.gz')
    for filename in ('config.json', 'config.env', '.env', 'compose.json'):
        shutil.copy2(STATE / filename, bundle / filename)
        (bundle / filename).chmod(0o600)
    shutil.copytree(STATE / 'official' / config['version'], bundle / 'official')
    hashes = {}
    for path in sorted(bundle.rglob('*')):
        if path.is_file():
            hashes[str(path.relative_to(bundle))] = file_hash(path)
    save_json(bundle / 'manifest.json', {'format': 1, 'version': config['version'],
              'created_at': timestamp(), 'library_id': config['storage']['library_id'],
              'postgres_image': database_image(), 'sha256': hashes, 'photos_included': False})
    log('Application backup: ' + str(bundle))
    log('This backup contains database/configuration/secrets, NOT photos or videos. Protect it and back up the library separately.', 'WARN')
    return bundle


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def database_image():
    return json.loads((STATE / 'compose.json').read_text())['services']['database']['image']


def validate_bundle(bundle):
    bundle = Path(bundle).expanduser().resolve(strict=True)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    if manifest.get('format') != 1 or manifest.get('photos_included') is not False:
        raise SafetyError('Unknown backup format')
    required = {'database.sql.gz', 'config.json', 'config.env', '.env', 'compose.json'}
    if not required.issubset(manifest.get('sha256', {})):
        raise SafetyError('Incomplete backup manifest')
    for relative, digest in manifest['sha256'].items():
        path = bundle / relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink() or not path.is_file():
            raise SafetyError('Invalid backup manifest path')
        if file_hash(path) != digest:
            raise SafetyError('Backup checksum mismatch: ' + relative)
    return bundle, manifest


def restore(bundle, *, confirmed=False, migration=False):
    config = load_config()
    bundle, manifest = validate_bundle(bundle)
    if manifest['version'] != config['version'] or manifest['postgres_image'] != database_image():
        raise SafetyError('Restore requires exact matching Immich version and PostgreSQL image')
    if manifest['library_id'] != config['storage']['library_id']:
        raise SafetyError('Backup belongs to a different photo library')
    if not confirmed:
        confirm('Replace the active database with this backup? Current database directory will be preserved.', 'RESTORE')
    lifecycle.stop()
    atomic_write(STATE / 'maintenance', 'restore in progress; do not start until transaction completes\n')
    old = dict(config)
    save_json(STATE / ('pre-restore-' + timestamp() + '.json'), config)
    new_db = Path(config['db_path']).with_name('postgres-restore-' + timestamp() + '-' + os.urandom(3).hex())
    storage.validate_db(new_db)
    new_db.mkdir(mode=0o700, exist_ok=False)
    config['db_path'] = str(new_db)
    config['db_storage'] = storage.describe(new_db)
    try:
        persist(config)
        engine.generate_env(config)
        engine.build_compose(config, STATE / 'official' / config['version'])
        engine.compose('up', '-d', 'database', timeout=600)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if engine.compose('exec', '-T', 'database', 'pg_isready', '-U', 'postgres', '-d', 'immich', check=False).returncode == 0:
                break
            time.sleep(2)
        else:
            raise SafetyError('Fresh database did not become ready')
        args = engine.docker_prefix() + ['compose', '-p', engine.PROJECT, '--project-directory', str(STATE),
               '--env-file', str(STATE / '.env'), '-f', str(STATE / 'compose.json'), 'exec', '-T', 'database',
               'psql', '--username=postgres', '--dbname=immich', '--single-transaction', '--set', 'ON_ERROR_STOP=on']
        error_path = STATE / 'restore-error.log'
        with error_path.open('wb') as error, gzip.open(bundle / 'database.sql.gz', 'rt') as source:
            os.chmod(error_path, 0o600)
            with subprocess.Popen(args, stdin=subprocess.PIPE, stdout=error, stderr=error, text=True) as proc:
                try:
                    for line in source:
                        proc.stdin.write(line.replace("SELECT pg_catalog.set_config('search_path', '', false);",
                                         "SELECT pg_catalog.set_config('search_path', 'public, pg_catalog', true);"))
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
                if proc.wait() != 0:
                    raise SafetyError('Transactional restore failed; see protected restore-error.log')
        engine.sql('SELECT 1;')
        (STATE / 'maintenance').unlink()
        log('Database restored; previous directory preserved: ' + old['db_path'])
        if not migration:
            lifecycle.start(config)
            engine.verify_assets(config, engine.asset_samples())
    except BaseException:
        lifecycle.stop()
        persist(old)
        engine.generate_env(old)
        engine.build_compose(old, STATE / 'official' / old['version'])
        # Keep maintenance latch. Do not silently restart an unverified recovery.
        log('Restore failed. Previous configuration restored; both database directories preserved. Inspect maintenance latch and logs.', 'ERROR')
        raise
