"""Small shared primitives; configuration is data, never shell code."""
import contextlib
import datetime
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get('IMMICH_HOME', Path.home() / '.config/immich-home-server')).expanduser().absolute()


class SafetyError(RuntimeError):
    pass


def log(message, level='OK'):
    print(f'[{level}] {message}', flush=True)


def run(args, *, capture=True, check=True, timeout=60, **kwargs):
    try:
        result = subprocess.run([str(a) for a in args], text=True,
                                capture_output=capture, timeout=timeout, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SafetyError(f'{args[0]} unavailable or timed out: {exc}') from exc
    if check and result.returncode:
        # Do not expose command arguments or captured output: either can contain secrets.
        raise SafetyError(f'{args[0]} failed (exit {result.returncode}). Run doctor or inspect protected logs.')
    return result


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise SafetyError(f'Refusing symlink for private state: {path}')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise SafetyError(f'Refusing symlink: {path}')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + '\n')


def read_env(path):
    result = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Z][A-Z0-9_]*)=(.*)', line)
        if not match or match[1] in result:
            raise SafetyError(f'Invalid/duplicate environment key on line {number}')
        value = match[2]
        if value.startswith("'"):
            if len(value) < 2 or not value.endswith("'"):
                raise SafetyError(f'Invalid quoted value on line {number}')
            value = value[1:-1].replace("\\'", "'").replace('\\\\', '\\')
        elif value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise SafetyError(f'Invalid quoted value on line {number}') from exc
        result[match[1]] = value
    return result


def env_text(values):
    lines = []
    for key, value in values.items():
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
            raise SafetyError('Invalid environment key')
        value = str(value)
        if any(ord(c) < 32 for c in value):
            raise SafetyError('Control characters are not allowed in configuration')
        # Compose dotenv single quotes are literal (including $ and #).
        lines.append(key + "='" + value.replace('\\', '\\\\').replace("'", "\\'") + "'")
    return '\n'.join(lines) + '\n'


def load_config():
    try:
        config = json.loads((STATE / 'config.json').read_text())
    except (OSError, ValueError) as exc:
        raise SafetyError(f'No valid deployment at {STATE}; run ./deploy.sh') from exc
    return config


def persist(config):
    private_dir(STATE)
    save_json(STATE / 'config.json', config)
    storage = config['storage']
    atomic_write(STATE / 'config.env', env_text({
        'IMMICH_PHOTO_PATH': storage['path'], 'IMMICH_PORT': config['port'],
        'IMMICH_TIMEZONE': config['timezone'], 'DB_DATA_PATH': config['db_path'],
        'STORAGE_TYPE': storage['kind'], 'STORAGE_MOUNT': storage['target'],
        'STORAGE_EXPECTED_SOURCE': storage['source'],
        'CURRENT_VERSION': config['version'], 'TARGET_VERSION': config.get('target_version', config['version'])}))


@contextlib.contextmanager
def operation_lock():
    private_dir(STATE)
    with (STATE / 'operation.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SafetyError('Another deployment operation is running') from exc
        yield


def prompt(label, default=None):
    suffix = f' [{default}]' if default is not None else ''
    answer = input(label + suffix + ': ').strip()
    return answer or (str(default) if default is not None else '')


def confirm(label, phrase='YES'):
    if prompt(f'{label}\nType {phrase} to continue') != phrase:
        raise SafetyError('Cancelled; data preserved')


def choose(label, options):
    print('\n' + label)
    for index, item in enumerate(options, 1):
        print(f'{index}. {item}')
    while True:
        answer = prompt(f'Select [1-{len(options)}]')
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        log('Choose a listed number', 'WARN')


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def private_ip(value):
    try:
        ip = ipaddress.ip_address(value)
        # Explicit RFC1918, Tailscale CGNAT, or loopback; no public or wildcard binds.
        allowed = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', '127.0.0.0/8')
        return ip.version == 4 and any(ip in ipaddress.ip_network(net) for net in allowed)
    except ValueError:
        return False


def safe_host(host):
    if not re.fullmatch(r'(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*', host):
        raise SafetyError('Use an SSH alias or user@host (IPv6 via an SSH config alias)')
    return host


def ssh_args(host, port=22):
    safe_host(host)
    if not 1 <= int(port) <= 65535:
        raise SafetyError('Invalid SSH port')
    return ['ssh', '-p', str(port), '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3', host]


def remote_command(arguments):
    return shlex.join([str(a) for a in arguments])
