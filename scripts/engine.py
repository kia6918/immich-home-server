"""Version-pinned official Compose with narrowly scoped safety transformations."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
from string import Template
import urllib.request

from common import ROOT, STATE, SafetyError, atomic_write, env_text, log, private_ip, read_env, run, save_json
from environment import docker_prefix, verify_docker

PROJECT = 'immich-home-server'
SERVICES = {'immich-server', 'immich-machine-learning', 'database', 'redis'}
DOCS = 'https://docs.immich.app/install/docker-compose/'


def fetch(url, *, limit=4 * 1024 * 1024):
    request = urllib.request.Request(url, headers={'User-Agent': 'immich-home-server', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = response.read(limit + 1)
    if len(result) > limit:
        raise SafetyError('Upstream response exceeded expected size')
    return result


def release(version=None):
    if version and not re.fullmatch(r'v\d+\.\d+\.\d+', version):
        raise SafetyError('Use an exact stable release, for example v3.2.4')
    endpoint = 'tags/' + version if version else 'latest'
    value = json.loads(fetch('https://api.github.com/repos/immich-app/immich/releases/' + endpoint))
    if value.get('draft') or value.get('prerelease') or not re.fullmatch(r'v\d+\.\d+\.\d+', value['tag_name']):
        raise SafetyError('Only published stable releases are supported')
    if tuple(map(int, value['tag_name'][1:].split('.'))) < (2, 5, 0):
        raise SafetyError('Releases before v2.5.0 use incompatible restore procedures')
    return value


def download_release(metadata, destination):
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Check the live official installation page at deployment/update time.
    if b'docker-compose.yml' not in fetch(DOCS):
        raise SafetyError('Official installation guidance changed; review before proceeding')
    hashes = {}
    for filename in ('docker-compose.yml', 'example.env'):
        asset = next((a for a in metadata['assets'] if a['name'] == filename), None)
        if not asset:
            raise SafetyError(f'Official release is missing {filename}')
        url = asset['browser_download_url']
        if not url.startswith('https://github.com/immich-app/immich/releases/download/'):
            raise SafetyError('Unexpected upstream asset origin')
        content = fetch(url)
        digest = 'sha256:' + hashlib.sha256(content).hexdigest()
        if asset.get('digest') and asset['digest'] != digest:
            raise SafetyError('Official release asset checksum mismatch')
        atomic_write(destination / filename, content.decode())
        hashes[filename] = digest
    save_json(destination / 'release.json', {'version': metadata['tag_name'], 'url': metadata['html_url'],
              'asset_sha256': hashes, 'notes': metadata.get('body', '')})


def generate_env(config, password=None):
    old = STATE / '.env'
    if password is None:
        password = read_env(old)['DB_PASSWORD'] if old.exists() else secrets.token_hex(32)
    if not re.fullmatch(r'[A-Za-z0-9]{24,}', password):
        raise SafetyError('Database password must be at least 24 alphanumeric characters')
    values = {'UPLOAD_LOCATION': config['storage']['path'], 'DB_DATA_LOCATION': config['db_path'],
              'IMMICH_VERSION': config['version'], 'TZ': config['timezone'],
              'DB_PASSWORD': password, 'DB_USERNAME': 'postgres', 'DB_DATABASE_NAME': 'immich'}
    quoted = dict(line.split('=', 1) for line in env_text(values).splitlines())
    atomic_write(old, Template((ROOT / 'templates/env.template').read_text()).substitute(quoted))


def harden_compose(value, config):
    if set(value.get('services', {})) != SERVICES:
        raise SafetyError('Official service layout changed; refusing unsupported deployment transformation')
    value['name'] = PROJECT
    for name, service in value['services'].items():
        service.pop('container_name', None)
        service['restart'] = 'no'  # Only the storage-aware host supervisor may restart containers.
        service.pop('env_file', None)  # Compose has already resolved these into environment.
        if service.get('privileged') or service.get('network_mode') == 'host':
            raise SafetyError('Unexpected privileged/host-network service upstream')
        image = service.get('image', '')
        if ':latest' in image or ':release' in image or not image:
            raise SafetyError('Unpinned image in deployment')
        if name != 'immich-server' and service.get('ports'):
            raise SafetyError('Unexpected exposed backend port upstream')
        for volume in service.get('volumes', []):
            if volume['type'] == 'bind':
                volume.setdefault('bind', {})['create_host_path'] = False
        if name == 'immich-server':
            photo = [v for v in service.get('volumes', []) if v.get('target') == '/data']
            if len(photo) != 1 or photo[0].get('source') != config['storage']['path']:
                raise SafetyError('Official upload mount layout changed')
            if config['storage']['kind'] != 'local':
                definition = config.get('photo_volume')
                if not definition:
                    raise SafetyError('Network library requires a direct Docker network-volume configuration')
                options = json.loads((STATE / definition['options_file']).read_text())
                expected_type = 'cifs' if definition['protocol'] == 'smb' else 'nfs'
                expected_device = definition['source'] if definition['protocol'] == 'smb' else ':' + definition['source'].split(':', 1)[1]
                if options.get('type') != expected_type or options.get('device') != expected_device:
                    raise SafetyError('Docker network volume identity does not match selected storage')
                service['volumes'] = [v for v in service['volumes'] if v.get('target') != '/data'] + [
                    {'type': 'volume', 'source': 'network-photo', 'target': '/data',
                     'volume': {'nocopy': True, 'subpath': definition['subpath']}}]
                value.setdefault('volumes', {})['network-photo'] = {'name': definition['name'], 'driver': 'local', 'driver_opts': options}
            service['ports'] = [{'target': 2283, 'published': str(config['port']),
                                 'host_ip': config['bind_ip'], 'protocol': 'tcp'}]
            # Docker Desktop does not require or reliably share /etc/localtime. TZ supplies timezone.
            if config['platform'] == 'Darwin':
                service['volumes'] = [v for v in service['volumes'] if v.get('target') != '/etc/localtime']
        if name == 'database':
            db = [v for v in service.get('volumes', []) if v.get('target') == '/var/lib/postgresql/data']
            if len(db) != 1 or db[0].get('source') != config['db_path']:
                raise SafetyError('Official database mount layout changed')
    return value


def build_compose(config, official_dir):
    result = run(docker_prefix() + ['compose', '-p', PROJECT, '--project-directory', str(STATE),
                 '--env-file', str(STATE / '.env'), '-f', str(official_dir / 'docker-compose.yml'),
                 'config', '--format', 'json'])
    value = harden_compose(interpolation(json.loads(result.stdout), escape=False), config)
    # Compose parses the generated document again. Preserve literal dollars after resolution.
    save_json(STATE / 'compose.json', interpolation(value, escape=True))
    compose('config', '-q')


def interpolation(item, *, escape):
    if isinstance(item, str):
        return item.replace('$', '$$') if escape else item.replace('$$', '$')
    if isinstance(item, list):
        return [interpolation(v, escape=escape) for v in item]
    if isinstance(item, dict):
        return {k: interpolation(v, escape=escape) for k, v in item.items()}
    return item


def compose(*args, **kwargs):
    if args and args[0] in {'exec', 'run'}:
        args = (args[0], '--interactive=false', *args[1:])
    return run(compose_arguments(*args), **kwargs)


def compose_arguments(*args):
    return docker_prefix() + ['compose', '-p', PROJECT, '--project-directory', str(STATE),
               '--env-file', str(STATE / '.env'), '-f', str(STATE / 'compose.json'), *args]


def check_port(config):
    if not private_ip(config['bind_ip']):
        raise SafetyError('Bind address must be an explicit private LAN/Tailscale/loopback IPv4 address')
    if not 1 <= int(config['port']) <= 65535:
        raise SafetyError('Invalid HTTP port')
    ids = compose('ps', '-q', 'immich-server', check=False).stdout.strip()
    if ids:
        return
    with socket.socket() as sock:
        try:
            sock.bind((config['bind_ip'], int(config['port'])))
        except OSError as exc:
            raise SafetyError('Selected LAN address/port is unavailable or occupied') from exc


def states():
    result = compose('ps', '-a', '--format', 'json')
    text = result.stdout.strip()
    if not text:
        return []
    if text.startswith('['):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines()]


def healthy():
    items = states()
    return set(item['Service'] for item in items) == SERVICES and all(
        item.get('State') == 'running' and item.get('Health') == 'healthy' for item in items)


def http(config):
    url = f'http://{config["bind_ip"]}:{config["port"]}/api/server/ping'
    with urllib.request.urlopen(url, timeout=10) as response:
        result = json.loads(response.read(4096))
    if result.get('res') != 'pong':
        raise SafetyError('Immich HTTP ping did not return pong')
    return url.removesuffix('/api/server/ping')


def sql(query):
    return compose('exec', '-T', 'database', 'psql', '-U', 'postgres', '-d', 'immich', '-At',
                   '-v', 'ON_ERROR_STOP=1', '-c', query).stdout.strip()


def asset_samples():
    # Ask PostgreSQL for rows as JSON to preserve filenames containing spaces/newlines.
    algorithm = sql("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='asset' AND column_name='checksumAlgorithm');")
    algorithm_column = ',"checksumAlgorithm" AS algorithm' if algorithm == 't' else ", 'sha1' AS algorithm"
    result = sql('SELECT COALESCE(json_agg(t),\'[]\'::json) FROM '
                 '(SELECT id,"originalPath",encode(checksum,\'hex\') AS checksum' + algorithm_column +
                 ' FROM asset WHERE "deletedAt" IS NULL ORDER BY id LIMIT 5) t;')
    return json.loads(result)


def verify_assets(config, samples):
    for item in samples:
        path = item['originalPath']
        if not path.startswith('/data/') or '..' in Path(path).parts:
            raise SafetyError('External libraries require manual mount mapping; migration/update cannot verify this asset')
        relative = path.removeprefix('/data/')
        local = Path(config['storage']['path']) / relative
        algorithm = item.get('algorithm', 'sha1').lower().replace('-', '')
        if algorithm not in {'sha1', 'sha256'}:
            raise SafetyError('Unknown asset checksum algorithm: ' + algorithm)
        with local.open('rb') as stream:
            digest = hashlib.file_digest(stream, algorithm).hexdigest() if hasattr(hashlib, 'file_digest') else None
            if digest is None:
                stream.seek(0)
                hasher = hashlib.new(algorithm)
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    hasher.update(block)
                digest = hasher.hexdigest()
        if digest != item['checksum']:
            raise SafetyError('An existing original asset failed checksum verification')
        # Prove container sees the same original at the database path too.
        compose('exec', '-T', 'immich-server', 'test', '-r', path)
    log(f'Existing original assets verified: {len(samples)}')
    if not samples:
        log('Library has no assets; authenticated upload check remains part of onboarding', 'WARN')


def verify_upload_bind(config):
    output = compose('ps', '-q', 'immich-server').stdout.strip()
    value = json.loads(run(docker_prefix() + ['inspect', output]).stdout)[0]
    match = [m for m in value['Mounts'] if m.get('Destination') == '/data']
    if len(match) != 1 or not match[0]['RW']:
        raise SafetyError('Running upload bind is incorrect')
    if config['storage']['kind'] == 'local':
        if match[0]['Source'] != config['storage']['path']:
            raise SafetyError('Running local upload bind is incorrect')
    elif match[0].get('Name') != config['photo_volume']['name']:
        raise SafetyError('Running NAS upload volume is incorrect')
    marker = compose('exec', '-T', 'immich-server', 'cat', '/data/.immich-home-server-library.json').stdout
    if json.loads(marker).get('library_id') != config['storage']['library_id']:
        raise SafetyError('Container sees a different library identity')
    log('Running upload location verified: ' + config['storage']['path'])


def preflight_photo(config):
    # Uses the official server image with Node as entrypoint; the application never runs.
    # Direct NAS mounts fail in Docker itself when the share is absent; no local fallback.
    if config['storage']['kind'] != 'local':
        info = json.loads(run(docker_prefix() + ['info', '--format', '{{json .}}']).stdout)
        if int(info['ServerVersion'].split('.')[0]) < 26:
            raise SafetyError('Direct network volume subpaths require Docker Engine 26+')
    script = """const fs=require('fs');const path=require('path');
const marker=JSON.parse(fs.readFileSync('/data/.immich-home-server-library.json','utf8'));
if(marker.library_id!==process.argv[1])throw Error('Library identity mismatch');
const dir=fs.mkdtempSync('/data/.immich-docker-probe-');
try{const file=path.join(dir,'probe');fs.writeFileSync(file,'probe',{flag:'wx'});
if(fs.readFileSync(file,'utf8')!=='probe')throw Error('Read/write mismatch');fs.unlinkSync(file);}
finally{fs.rmdirSync(dir);}"""
    compose('run', '--rm', '--no-deps', '--entrypoint', 'node', 'immich-server',
            '-e', script, config['storage']['library_id'], timeout=120)
