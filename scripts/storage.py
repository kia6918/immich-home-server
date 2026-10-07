"""Mount-aware storage discovery, identity, and fail-closed validation."""
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import tempfile
from urllib.parse import quote, unquote

from common import ROOT, SafetyError, choose, confirm, log, prompt, run

NETWORK = {'cifs', 'smbfs', 'smb3', 'nfs', 'nfs4'}
LOCAL = {'apfs', 'hfs', 'hfsplus', 'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'zfs', 'ufs'}
IGNORED = {'proc', 'sysfs', 'devtmpfs', 'tmpfs', 'devpts', 'overlay', 'squashfs', 'autofs', 'cgroup2'}


def mount_unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def parse_mountinfo(text):
    mounts = []
    for line in text.splitlines():
        before, sep, after = line.partition(' - ')
        if not sep:
            raise SafetyError('Invalid Linux mount table')
        fields, tail = before.split(), after.split()
        if len(fields) < 6 or len(tail) < 3:
            raise SafetyError('Incomplete Linux mount table')
        mounts.append({'target': mount_unescape(fields[4]), 'source': mount_unescape(tail[1]),
                       'fstype': tail[0], 'mount_root': mount_unescape(fields[3]),
                       'options': fields[5] + ',' + tail[2], 'device': fields[2]})
    return mounts


def parse_macos_mount(text):
    mounts = []
    for line in text.splitlines():
        match = re.fullmatch(r'(.+) on (.+) \(([^, )]+)(.*)\)', line)
        if match:
            mounts.append({'source': mount_unescape(match[1]), 'target': mount_unescape(match[2]),
                           'fstype': match[3], 'options': match[4], 'mount_root': '/', 'device': match[1]})
    if not mounts:
        raise SafetyError('Could not parse macOS mount table')
    return mounts


def mounts():
    if platform.system() == 'Linux':
        return parse_mountinfo(Path('/proc/self/mountinfo').read_text())
    if platform.system() == 'Darwin':
        return parse_macos_mount(run(['/sbin/mount']).stdout)
    raise SafetyError('Unsupported OS for mount detection')


def containing_mount(path, table):
    path = os.path.realpath(path)
    found = [m for m in table if path == m['target'] or path.startswith(m['target'].rstrip('/') + '/')]
    if not found:
        raise SafetyError(f'No mounted filesystem covers {path}')
    return max(found, key=lambda m: len(m['target']))


def kind(fstype):
    if fstype in {'cifs', 'smbfs', 'smb3'}:
        return 'smb'
    if fstype in {'nfs', 'nfs4'}:
        return 'nfs'
    if fstype in LOCAL:
        return 'local'
    raise SafetyError(f'Unsupported filesystem {fstype}; cannot establish safe storage identity')


def canonical_source(source, protocol):
    if protocol == 'smb':
        value = unquote(source).removeprefix('//')
        if '@' in value:
            value = value.split('@', 1)[1]
        server, separator, share = value.partition('/')
        if not separator or not server or not share:
            raise SafetyError('Invalid SMB mount source')
        # Keep share spelling: SMB servers can have case-sensitive exports.
        return '//' + server.lower().rstrip('.') + '/' + share.rstrip('/')
    if protocol == 'nfs':
        server, separator, export = source.partition(':')
        if not separator or not export.startswith('/'):
            raise SafetyError('Invalid NFS mount source')
        return server.lower().rstrip('.') + ':' + export.rstrip('/')
    return source


def disk_identity(mount):
    if kind(mount['fstype']) != 'local':
        return ''
    if platform.system() == 'Darwin':
        result = run(['/usr/sbin/diskutil', 'info', '-plist', mount['target']])
        info = plistlib.loads(result.stdout.encode())
        identity = info.get('VolumeUUID') or info.get('DiskUUID')
    else:
        result = run(['findmnt', '-n', '-o', 'UUID', '-T', mount['target']], check=False)
        identity = result.stdout.strip()
        if not identity and mount['fstype'] == 'zfs':
            result = run(['zfs', 'get', '-H', '-o', 'value', 'guid', mount['source']], check=False)
            identity = result.stdout.strip() if result.returncode == 0 else ''
    if not identity:
        raise SafetyError(f'No persistent filesystem UUID for {mount["target"]}; cannot safely identify disk')
    return identity


def describe(path, table=None):
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise SafetyError('Storage path must be a directory')
    mount = containing_mount(str(path), mounts() if table is None else table)
    protocol = kind(mount['fstype'])
    result = dict(mount, path=str(path), kind=protocol, uuid=disk_identity(mount))
    result['source'] = canonical_source(mount['source'], protocol)
    result['relative_path'] = os.path.relpath(path, mount['target'])
    return result


def validate_identity(expected, actual):
    for field in ('target', 'fstype', 'kind', 'mount_root'):
        if expected[field] != actual[field]:
            raise SafetyError(f'Storage {field} changed: expected {expected[field]}, found {actual[field]}')
    if expected['kind'] == 'local':
        if not expected.get('uuid') or expected['uuid'] != actual.get('uuid'):
            raise SafetyError('Storage filesystem UUID changed or unavailable')
    elif canonical_source(expected['source'], expected['kind']) != actual['source']:
        raise SafetyError('Network share/export changed')
    if expected['target'] != '/' and actual['target'] == '/':
        raise SafetyError('Selected storage fell back to the local root filesystem')
    if expected['relative_path'] != actual['relative_path']:
        raise SafetyError('Library location within the mounted filesystem changed')


def validate_db(path, table=None, *, require_internal=True):
    path = Path(path).expanduser()
    ancestor = path
    while not ancestor.exists():
        if ancestor == ancestor.parent:
            raise SafetyError('Database parent unavailable')
        ancestor = ancestor.parent
    table = mounts() if table is None else table
    mount = containing_mount(str(ancestor), table)
    if kind(mount['fstype']) != 'local':
        raise SafetyError('PostgreSQL must be on a local filesystem; SMB/NFS are forbidden')
    if require_internal:
        if platform.system() == 'Darwin':
            info = plistlib.loads(run(['/usr/sbin/diskutil', 'info', '-plist', mount['target']]).stdout.encode())
            if not info.get('Internal', False):
                raise SafetyError('PostgreSQL must use an internal local disk')
        else:
            result = run(['lsblk', '-s', '-J', '-o', 'NAME,TYPE,TRAN,RM', mount['source']], check=False)
            if result.returncode == 0:
                def external(items):
                    return any(item.get('tran') in {'usb', 'iscsi'} or item.get('rm') or
                               external(item.get('children', [])) for item in items)
                if external(json.loads(result.stdout).get('blockdevices', [])):
                    raise SafetyError('PostgreSQL must not use removable/USB/iSCSI storage')
            else:
                raise SafetyError('Cannot validate database disk hardware with lsblk')
    return mount


def check(storage, minimum_gib=10, *, writable=True):
    actual = describe(storage['path'])
    validate_identity(storage, actual)
    marker = Path(storage['path']) / '.immich-home-server-library.json'
    if not marker.is_file() or json.loads(marker.read_text()).get('library_id') != storage['library_id']:
        raise SafetyError('Library identity marker missing or changed')
    usage = shutil.disk_usage(storage['path'])
    if usage.free < float(minimum_gib) * 1024 ** 3:
        raise SafetyError(f'Photo storage has less than {minimum_gib} GiB free')
    if writable:
        fd, path = tempfile.mkstemp(prefix='.immich-write-check-', dir=storage['path'])
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(b'immich-storage-check\n')
                stream.flush()
                os.fsync(stream.fileno())
            if Path(path).read_bytes() != b'immich-storage-check\n':
                raise SafetyError('Storage read/write verification failed')
        finally:
            os.unlink(path)  # Only the uniquely named probe created above.
    return usage


def bounded_check(config):
    # A disconnected hard NFS mount must not hang the supervisor indefinitely.
    import subprocess
    proc = subprocess.Popen([os.sys.executable, str(ROOT / 'scripts/cli.py'), '_probe', '--config-json', json.dumps(config)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        output, error = proc.communicate(timeout=12)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        # Do not join an uninterruptible NFS I/O task. The supervisor must still stop writers.
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
        raise SafetyError('Storage probe timed out; selected storage may be offline') from exc
    if proc.returncode:
        raise SafetyError((error.strip() or output.strip() or 'Storage probe failed').removeprefix('[ERROR] '))


def configure_docker_volume(config):
    """Direct NAS mounts fence writes even if the host's mountpoint falls back to root."""
    info = config['storage']
    if info['kind'] == 'local':
        config.pop('photo_volume', None)
        return
    relative = info['relative_path']
    if info.get('mount_root', '/') != '/':
        raise SafetyError('Network bind mounts/subexports require explicit path mapping; select the original mounted share/export')
    if relative == '.' or '..' in Path(relative).parts:
        raise SafetyError('Network library must be a dedicated subdirectory within the share/export')
    from common import STATE, save_json
    endpoint = hashlib.sha256((info['source'] + '/' + relative).encode()).hexdigest()[:12]
    name = 'immich-home-server-photos-' + config['deployment_id'][:8] + '-' + endpoint
    if info['kind'] == 'nfs':
        server, export = info['source'].split(':', 1)
        nfs_version = prompt('Docker NFS protocol version (must match NAS)', '4' if info['fstype'] == 'nfs4' else '3')
        if nfs_version not in {'3', '4', '4.1', '4.2'}:
            raise SafetyError('Unsupported NFS protocol version')
        options = {'type': 'nfs', 'device': ':' + export,
                   'o': f'addr={server},rw,nfsvers={nfs_version},hard,nosuid,nodev'}
    else:
        import getpass
        server = info['source'].removeprefix('//').split('/')[0]
        username = prompt('SMB username for Docker to mount this share (empty for guest)')
        options = {'type': 'cifs', 'device': info['source'],
                   'o': f'addr={server},rw,vers=3.1.1,nosuid,nodev,file_mode=0660,dir_mode=0770'}
        if username:
            password = getpass.getpass('SMB password for Docker (protected target-only configuration): ')
            if any(c in username + password for c in '\n\r\x00,'):
                raise SafetyError('Docker native SMB options cannot encode commas/control characters in credentials; use NFS or a NAS account with an encodable password')
            options['o'] += f',username={username},password={password}'
        else:
            options['o'] += ',guest'
    # Never place credentials in the exported public deployment metadata.
    credentials = STATE / ('network-volume-' + endpoint + '.json')
    save_json(credentials, options)
    config['photo_volume'] = {'name': name, 'subpath': relative, 'options_file': credentials.name,
                              'source': info['source'], 'protocol': info['kind']}


def candidates():
    table = mounts()
    result = [str(Path.home())]
    for item in table:
        if item['fstype'] not in LOCAL | NETWORK or item['target'] == '/':
            continue
        if item['target'].startswith(('/System/', '/private/', '/boot', '/snap/', '/dev/', '/proc/', '/var/lib/docker/', '/var/lib/containerd/')):
            continue
        if item['target'] not in result:
            result.append(item['target'])
    return result


def show_candidate(path):
    try:
        info = describe(path)
        usage = shutil.disk_usage(path)
        print(f'    {info["kind"]} / {info["fstype"]}; total {usage.total / 1024**4:.2f} TiB; '
              f'free {usage.free / 1024**4:.2f} TiB; writable {"yes" if os.access(path, os.W_OK) else "no"}')
        print(f'    mount: {info["target"]}; source: {info["source"]}')
    except (OSError, SafetyError) as exc:
        print(f'    unavailable: {exc}')


def add_network():
    protocol = ('smb', 'nfs')[choose('Add network storage', ['SMB', 'NFS'])]
    server = prompt('Server hostname/IP')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', server):
        raise SafetyError('Invalid server name')
    export = prompt('Share name' if protocol == 'smb' else 'Export path')
    if not export or any(c in export for c in '\n\r\x00'):
        raise SafetyError('Invalid share/export')
    if protocol == 'smb' and '/' in export:
        raise SafetyError('Enter one SMB share name, then choose its library subdirectory separately')
    if protocol == 'nfs' and not export.startswith('/'):
        raise SafetyError('NFS export must be absolute')
    target = Path(prompt('Mount path')).expanduser().absolute()
    if target == Path('/') or containing_mount(str(target), mounts())['target'] == str(target):
        raise SafetyError('Mount path is already a mount or is root')
    if target.exists() and any(target.iterdir()):
        raise SafetyError('Mount path must be empty; existing files will not be hidden')
    confirm(f'Mount {protocol.upper()} {server}/{export} at {target}? Existing disks/files are preserved.')
    if platform.system() == 'Linux':
        helper, package = ('mount.cifs', 'cifs-utils') if protocol == 'smb' else ('mount.nfs', 'nfs-common')
        if not shutil.which(helper) and not (Path('/sbin') / helper).is_file():
            import environment
            environment.platform_info()  # Refuse unsupported distributions before apt mutations.
            log('Installing required network mount helper: ' + package)
            run(['sudo', 'apt-get', 'update'], capture=False, timeout=600)
            run(['sudo', 'apt-get', 'install', '-y', package], capture=False, timeout=600)
    run(['sudo', 'mkdir', '-p', target], capture=False)
    if protocol == 'nfs':
        options = 'resvport,nosuid,nodev' if platform.system() == 'Darwin' else 'nosuid,nodev'
        run(['sudo', 'mount', '-t', 'nfs', '-o', options, server + ':' + export, target], capture=False, timeout=120)
    elif platform.system() == 'Darwin':
        username = prompt('SMB username (empty for guest)')
        source = '//' + (quote(username, safe='') + '@' if username else '') + server + '/' + quote(export, safe='')
        # Native password prompt; no password in argv, logs, repository, or our config.
        run(['sudo', 'chown', f'{os.getuid()}:{os.getgid()}', target], capture=False)
        run(['/sbin/mount_smbfs', source, target], capture=False, timeout=120)
    else:
        import getpass
        from common import STATE, atomic_write, private_dir
        username = prompt('SMB username (empty for guest)')
        options = f'uid={os.getuid()},gid={os.getgid()},nosuid,nodev'
        if username:
            password = getpass.getpass('SMB password (stored in root-only target credential file): ')
            if any(c in username + password for c in '\n\r\x00'):
                raise SafetyError('Credentials must not contain newlines')
            cred_name = hashlib.sha256((server + '/' + export).encode()).hexdigest()[:16] + '.credentials'
            credentials = Path('/etc/immich-home-server') / cred_name
            with tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / 'credentials'
                atomic_write(local, f'username={username}\npassword={password}\n')
                run(['sudo', 'install', '-d', '-m', '700', credentials.parent], capture=False)
                run(['sudo', 'install', '-m', '600', local, credentials], capture=False)
            options += ',credentials=' + str(credentials)
        else:
            options += ',guest'
        run(['sudo', 'mount', '-t', 'cifs', '-o', options, '//' + server + '/' + export, target],
            capture=False, timeout=120)
    info = describe(target)
    expected = canonical_source(('//' if protocol == 'smb' else '') + server +
                                ('/' if protocol == 'smb' else ':') + export, protocol)
    if info['kind'] != protocol or info['source'] != expected:
        raise SafetyError('Mounted source does not match requested share/export')
    log('Mount accepted. No fstab or existing mount configuration changed. Remount after reboot using OS tools.', 'WARN')
    return str(target)


def select_path():
    paths = candidates()
    print('\nChoose where Immich photos/videos will be stored')
    for index, path in enumerate(paths, 1):
        print(f'{index}. {path}')
        show_candidate(path)
    print(f'{len(paths)+1}. Enter custom path\n{len(paths)+2}. Add network storage')
    answer = prompt(f'Select storage [1-{len(paths)+2}]')
    if not answer.isdigit() or not 1 <= int(answer) <= len(paths) + 2:
        raise SafetyError('Invalid storage selection')
    number = int(answer)
    base = prompt('Existing storage path') if number == len(paths) + 1 else (
        add_network() if number == len(paths) + 2 else paths[number - 1])
    base = str(Path(base).expanduser().resolve(strict=True))
    show_candidate(base)
    return prompt('Immich library directory (new dedicated directory, or existing Immich library)', str(Path(base) / 'Immich'))


def same_library(source, destination):
    if source.get('library_id') != destination.get('library_id') or not source.get('library_id'):
        return False
    if source['kind'] != destination['kind'] or source['relative_path'] != destination['relative_path']:
        return False
    if source['kind'] == 'local':
        return bool(source.get('uuid')) and source['uuid'] == destination.get('uuid')
    return canonical_source(source['source'], source['kind']) == canonical_source(destination['source'], destination['kind'])
