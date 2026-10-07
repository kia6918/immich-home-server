"""SSH transport: small public-code bundles, no persisted coordinator secrets."""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile

from common import ROOT, SafetyError, log, remote_command, run, safe_host, ssh_args


def code_bundle():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for directory in ('scripts', 'config', 'templates'):
            for path in sorted((ROOT / directory).rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
        for path in sorted(ROOT.glob('*.sh')):
            archive.add(path, arcname=path.name, recursive=False)
        archive.add(ROOT / 'README.md', arcname='README.md')
    return stream.getvalue()


def remote_home(host, port=None):
    return run(ssh_args(host, port) + ['printf %s "$HOME"']).stdout.strip()


def bootstrap(host, port=None):
    log('Connecting over SSH to ' + host)
    run(ssh_args(host, port) + ['true'], capture=False)
    check = run(ssh_args(host, port) + ['command -v python3 >/dev/null && python3 -c "import sys; sys.exit(sys.version_info < (3,10))"'], check=False)
    if check.returncode:
        raise SafetyError('Target needs Python 3.10+ before the installer can run; install it using the target OS package manager')
    result = run(ssh_args(host, port) + ['umask 077; mktemp -d "$HOME/.immich-installer.XXXXXX"'])
    directory = result.stdout.strip()
    if not directory.startswith('/') or any(c in directory for c in '\n\r\x00'):
        raise SafetyError('Remote returned an invalid installer directory')
    args = ssh_args(host, port) + [remote_command(['tar', '-xzf', '-', '-C', directory])]
    proc = subprocess.run(args, input=code_bundle(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
    if proc.returncode:
        raise SafetyError('Installer bundle transfer failed')
    log('SSH connection and portable installer bundle verified')
    return directory


def execute(host, command, arguments=(), *, port=None, tty=False, capture=True, directory=None, timeout=3600):
    directory = directory or (remote_home(host, port) + '/.config/immich-home-server/runtime')
    args = ssh_args(host, port)
    if tty:
        args.insert(1, '-t')
    script = str(Path(directory) / 'scripts/cli.py')
    return run(args + [remote_command(['python3', script, command, *arguments])], capture=capture, timeout=timeout)


def deploy(args):
    directory = bootstrap(args.host, args.ssh_port)
    arguments = []
    for flag, value in (('--storage', args.storage), ('--db-path', args.db_path), ('--port', args.port),
                        ('--bind-ip', args.bind_ip), ('--version', args.version), ('--timezone', args.timezone)):
        if value is not None:
            arguments += [flag, str(value)]
    if args.yes:
        arguments += ['--yes']
    execute(args.host, 'install', arguments, port=args.ssh_port, directory=directory,
            tty=not args.yes, capture=False, timeout=7200)
    execute(args.host, 'status', port=args.ssh_port, capture=False)


def relay_backup(source, destination, bundle, *, source_port=None, destination_port=None):
    # Secret-bearing archive streams directly between SSH processes, never into an M4 file.
    producer = ssh_args(source, source_port) + [remote_command(['tar', '-czf', '-', '-C', bundle, '.'])]
    home = remote_home(destination, destination_port)
    consumer = ssh_args(destination, destination_port) + [remote_command(
        ['python3', home + '/.config/immich-home-server/runtime/scripts/cli.py', '_receive-backup'])]
    with subprocess.Popen(producer, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as src:
        result = subprocess.run(consumer, stdin=src.stdout, capture_output=True, timeout=1800)
        src.stdout.close()
        if src.wait(timeout=30) != 0 or result.returncode != 0:
            raise SafetyError('Backup transfer failed; source backup retained')
    value = json.loads(result.stdout)
    return value['bundle']
