"""Storage-aware ownership, supervised startup, status, and diagnostics."""
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import signal
import socket
import sys
import time

from common import ROOT, STATE, SafetyError, atomic_write, load_config, log, operation_lock, private_dir, run, save_json, timestamp
import engine
import environment
import storage


def claim(config):
    owner = Path(config['storage']['path']) / '.immich-home-server-owner'
    try:
        owner.mkdir(mode=0o700)
    except FileExistsError:
        try:
            current = json.loads((owner / 'owner.json').read_text())
        except (OSError, ValueError) as exc:
            raise SafetyError('Library ownership is incomplete; inspect manually, never automatically reclaim it') from exc
        if current.get('deployment_id') != config['deployment_id']:
            raise SafetyError('Library is owned by another deployment. Stop it and transfer ownership via migrate.')
        return
    save_json(owner / 'owner.json', {'deployment_id': config['deployment_id'], 'hostname': socket.gethostname(),
              'claimed_at': timestamp()})


def release_owner(config):
    owner = Path(config['storage']['path']) / '.immich-home-server-owner'
    current = json.loads((owner / 'owner.json').read_text())
    if current.get('deployment_id') != config['deployment_id']:
        raise SafetyError('Cannot release another deployment\'s ownership')
    if any(item.get('State') == 'running' for item in engine.states() if item['Service'] == 'immich-server'):
        raise SafetyError('Cannot release ownership while Immich is running')
    owner.rename(owner.with_name('.immich-owner-history-' + timestamp() + '-' + os.urandom(3).hex()))


def stop(*, disable=True):
    if disable:
        (STATE / 'enabled').unlink(missing_ok=True)
    if (STATE / 'compose.json').exists():
        engine.compose('stop', '--timeout', '30', timeout=90)
    log('Immich stopped; photos, database, and configuration preserved')


def start(config=None, *, enable=True, recreate=False):
    config = config or load_config()
    if (STATE / 'migration-blocked').exists() or (STATE / 'maintenance').exists():
        raise SafetyError('Deployment is locked for migration/maintenance; resolve the transaction before starting')
    environment.verify_docker()
    storage.bounded_check(config)
    storage.validate_db(config['db_path'])
    actual_db = storage.describe(config['db_path'])
    storage.validate_identity(config['db_storage'], actual_db)
    engine.check_port(config)
    claim(config)
    if enable:
        atomic_write(STATE / 'enabled', 'start requested\n')
    try:
        # Check again immediately before every create/start. Docker cannot create missing host paths.
        storage.bounded_check(config)
        engine.preflight_photo(config)
        storage.bounded_check(config)
        engine.compose('up', '-d', *(['--force-recreate'] if recreate else []), timeout=600)
        deadline = time.monotonic() + config.get('health_timeout', 600)
        while time.monotonic() < deadline:
            storage.bounded_check(config)
            if engine.healthy():
                url = engine.http(config)
                engine.verify_upload_bind(config)
                log('Immich is ready: ' + url)
                return url
            time.sleep(3)
        raise SafetyError('Containers did not become healthy within the startup timeout')
    except BaseException:
        stop()
        raise


def install_runtime():
    destination = private_dir(STATE / 'runtime')
    if ROOT != destination:
        for directory in ('scripts', 'config', 'templates'):
            if (ROOT / directory).is_dir():
                shutil.copytree(ROOT / directory, destination / directory, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for path in ROOT.glob('*.sh'):
            shutil.copy2(path, destination / path.name)
    return destination


def install_supervisor():
    runtime = install_runtime()
    marker = '# Managed by immich-home-server\n'
    if platform.system() == 'Linux':
        unit = Path('/etc/systemd/system/immich-home-server.service')
        if unit.exists() and marker.strip() not in unit.read_text():
            raise SafetyError('Existing service with the same name is unrelated; refusing to overwrite')
        # systemd requires escaped arguments; quote through its syntax rather than a shell.
        def escaped(value):
            return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
        content = marker + '[Unit]\nDescription=Immich storage-aware supervisor\nAfter=docker.service network-online.target\n' \
            'Wants=network-online.target\n\n[Service]\nType=simple\n' + \
            'Environment=' + escaped('IMMICH_HOME=' + str(STATE)) + '\n' + \
            'ExecStart=' + escaped(sys.executable) + ' ' + escaped(runtime / 'scripts/cli.py') + ' guard\n' + \
            'Restart=on-failure\nRestartSec=15\nTimeoutStopSec=100\n\n[Install]\nWantedBy=multi-user.target\n'
        staged = STATE / 'immich-home-server.service'
        atomic_write(staged, content)
        run(['sudo', 'install', '-m', '644', staged, unit], capture=False)
        run(['sudo', 'systemctl', 'daemon-reload'], capture=False)
        run(['sudo', 'systemctl', 'enable', '--now', 'immich-home-server.service'], capture=False)
        run(['sudo', 'systemctl', 'restart', 'immich-home-server.service'], capture=False)
        run(['sudo', 'systemctl', 'is-active', '--quiet', 'immich-home-server.service'])
    else:
        uid = os.getuid()
        run(['launchctl', 'print', f'gui/{uid}'])
        directory = Path.home() / 'Library/LaunchAgents'
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / 'org.immich-home-server.guard.plist'
        if destination.exists():
            value = plistlib.loads(destination.read_bytes())
            if value.get('Label') != 'org.immich-home-server.guard':
                raise SafetyError('Unrelated launch agent preserved')
            run(['launchctl', 'bootout', f'gui/{uid}', destination], check=False)
        value = {'Label': 'org.immich-home-server.guard',
                 'ProgramArguments': [sys.executable, str(runtime / 'scripts/cli.py'), 'guard'],
                 'EnvironmentVariables': {'IMMICH_HOME': str(STATE),
                   'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
                 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15,
                 'StandardOutPath': str(STATE / 'guard.log'), 'StandardErrorPath': str(STATE / 'guard.log')}
        atomic_write(destination, plistlib.dumps(value).decode())
        run(['launchctl', 'bootstrap', f'gui/{uid}', destination])
        log('macOS guard runs in this login session; Docker Desktop must run and paths must be shared', 'WARN')


def guard():
    quitting = False
    def terminate(_signum, _frame):
        nonlocal quitting
        quitting = True
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    log('Storage supervisor running')
    while not quitting:
        try:
            with operation_lock(wait=0):
                if (STATE / 'enabled').exists() and not (STATE / 'maintenance').exists():
                    config = load_config()
                    try:
                        environment.docker_prefix()
                    except SafetyError:
                        # Docker restart may recover; compose restart policies stay disabled.
                        time.sleep(3)
                        continue
                    try:
                        storage.bounded_check(config)
                        storage.validate_identity(config['db_storage'], storage.describe(config['db_path']))
                        claim(config)
                    except (SafetyError, OSError, ValueError) as exc:
                        save_json(STATE / 'fault.json', {'time': timestamp(), 'reason': str(exc)})
                        log('Storage fault: stopping Immich; explicit start required after repair', 'ERROR')
                        stop()
                    else:
                        if not engine.healthy():
                            start(config)
        except SafetyError as exc:
            log(str(exc), 'WARN')
        for _ in range(15):
            if quitting:
                break
            time.sleep(1)
    # Preserve requested-start state across graceful host shutdown, but stop writers.
    try:
        with operation_lock():
            stop(disable=False)
    except SafetyError:
        pass


def status():
    config = load_config()
    print('\nImmich Home Server')
    print(f'Host: {socket.gethostname()}\nOS: {platform.platform()}\nVersion: {config["version"]}')
    print(f'LAN URL: http://{config["bind_ip"]}:{config["port"]}')
    print(f'Database: {config["db_path"]}\nPhoto storage: {config["storage"]["path"]}')
    print(f'Type: {config["storage"]["kind"]}\nSource: {config["storage"]["source"]}')
    try:
        storage.bounded_check(config)
        log('Selected storage identity, permissions, and free space verified')
        usage = shutil.disk_usage(config['storage']['path'])
        print(f'Free: {usage.free / 1024**4:.2f} TiB')
        for item in engine.states():
            print(f'{item["Service"]}: {item.get("State")} / {item.get("Health")}')
        engine.http(config)
        log('HTTP healthy')
    except (SafetyError, OSError, ValueError) as exc:
        log(str(exc), 'WARN')
    if shutil.which('tailscale'):
        result = run(['tailscale', 'ip', '-4'], check=False)
        if result.returncode == 0:
            log(f'Tailscale detected: {result.stdout.strip()} (not bound by this deployment; see README)', 'WARN')
    if (STATE / 'migration-blocked').exists():
        log('Migration lock prevents startup', 'WARN')


def doctor():
    config = load_config()
    counts = {'PASS': 0, 'WARN': 0, 'FAIL': 0}
    checks = [('Docker/Compose', environment.verify_docker, 'Start Docker and verify access'),
              ('Photo storage identity/read/write/free space', lambda: storage.bounded_check(config), 'Reconnect the original storage; do not recreate mount directories'),
              ('Local database filesystem', lambda: storage.validate_db(config['db_path']), 'Move PostgreSQL to an internal local disk using backup/restore'),
              ('Database disk identity', lambda: storage.validate_identity(config['db_storage'], storage.describe(config['db_path'])), 'Reconnect original local DB disk'),
              ('Containers healthy', lambda: require(engine.healthy(), 'Containers are not all healthy'), 'Inspect scoped logs; run start after fixing storage'),
              ('HTTP', lambda: engine.http(config), 'Check configured LAN address/port'),
              ('Database query', lambda: engine.sql('SELECT 1;'), 'Inspect database health/logs')]
    for label, test, advice in checks:
        try:
            test()
            counts['PASS'] += 1
            print(f'PASS {label}')
        except (SafetyError, OSError, ValueError) as exc:
            counts['FAIL'] += 1
            print(f'FAIL {label}: {exc}\n  Action: {advice}')
    if config['storage']['kind'] in {'smb', 'nfs'}:
        source = config['storage']['source']
        host = source.removeprefix('//').split('/')[0] if config['storage']['kind'] == 'smb' else source.split(':')[0]
        port = 445 if config['storage']['kind'] == 'smb' else 2049
        try:
            with socket.create_connection((host, port), timeout=5):
                pass
            print('PASS NAS connectivity')
            counts['PASS'] += 1
        except OSError:
            print('WARN NAS service port unreachable; inspect routing/firewall/NAS')
            counts['WARN'] += 1
    if platform.system() == 'Linux':
        result = run(['systemctl', 'is-active', '--quiet', 'immich-home-server.service'], check=False)
    else:
        result = run(['launchctl', 'print', f'gui/{os.getuid()}/org.immich-home-server.guard'], check=False)
    if result.returncode:
        counts['FAIL'] += 1
        print('FAIL Storage supervisor absent; run deploy → Repair before unattended operation')
    else:
        counts['PASS'] += 1
        print('PASS Storage supervisor registered')
    try:
        logs = engine.compose('logs', '--no-color', '--tail', '30', check=False).stdout
        # Logs may contain user filenames; save privately and do not transmit by default.
        atomic_write(STATE / 'doctor-logs.txt', logs)
        log('Scoped logs saved privately to ' + str(STATE / 'doctor-logs.txt'))
    except SafetyError:
        pass
    print(' '.join(f'{key}: {value}' for key, value in counts.items()))
    return counts['FAIL'] == 0


def require(condition, message):
    if not condition:
        raise SafetyError(message)
