"""Read-only inventory and conservative Docker provisioning."""
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import tempfile

from common import STATE, SafetyError, atomic_write, confirm, log, private_dir, run, save_json, timestamp
import storage


def os_release(text):
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r'([A-Z_]+)=(.*)', line)
        if match:
            result[match[1]] = match[2].strip('"\'')
    return result


def platform_info():
    system, arch = platform.system(), platform.machine()
    if arch not in {'x86_64', 'amd64', 'arm64', 'aarch64'}:
        raise SafetyError(f'Unsupported CPU architecture: {arch}')
    if system == 'Darwin':
        version = run(['sw_vers', '-productVersion']).stdout.strip()
        memory = int(run(['sysctl', '-n', 'hw.memsize']).stdout)
        cpu = run(['sysctl', '-n', 'machdep.cpu.brand_string'], check=False).stdout.strip() or arch
        release = {'ID': 'macos', 'VERSION_ID': version}
    elif system == 'Linux':
        release = os_release(Path('/etc/os-release').read_text())
        supported = {'ubuntu': {'22.04', '24.04', '26.04'}, 'debian': {'12', '13'}}
        if release.get('VERSION_ID') not in supported.get(release.get('ID'), set()):
            raise SafetyError('Supported Linux targets: Ubuntu 22.04/24.04/26.04 or Debian 12/13; add an explicit adapter for other distributions')
        memory = int(re.search(r'MemTotal:\s+(\d+)', Path('/proc/meminfo').read_text())[1]) * 1024
        cpu_text = Path('/proc/cpuinfo').read_text()
        model = re.search(r'(?:model name|Hardware)\s+:\s*(.+)', cpu_text)
        cpu = model[1] if model else arch
        if arch in {'x86_64', 'amd64'}:
            flags = re.search(r'flags\s+:\s*(.+)', cpu_text)
            required = {'ssse3', 'sse4_1', 'sse4_2', 'popcnt', 'cx16'}
            if not flags or not required.issubset(set(flags[1].split())):
                raise SafetyError('Current Immich machine learning requires x86-64-v2; required CPU features are absent')
    else:
        raise SafetyError(f'Unsupported OS: {system}')
    return dict(release, system=system, architecture=arch, ram_gib=round(memory / 1024**3, 1),
                cpu=cpu, cores=os.cpu_count(), hostname=socket.gethostname())


def lan_addresses():
    if platform.system() == 'Linux':
        result = run(['ip', '-j', '-4', 'address'], check=False)
        if result.returncode == 0:
            return [item['local'] for interface in json.loads(result.stdout)
                    if interface.get('ifname') != 'lo' and not interface.get('ifname', '').startswith(('docker', 'br-', 'veth'))
                    for item in interface.get('addr_info', []) if item.get('family') == 'inet']
    else:
        text = run(['ifconfig']).stdout
        return [a for a in re.findall(r'\binet (\d+\.\d+\.\d+\.\d+)', text) if not a.startswith('127.')]
    return []


def docker_prefix():
    if os.environ.get('DOCKER_HOST') and not os.environ['DOCKER_HOST'].startswith('unix://'):
        raise SafetyError('DOCKER_HOST points off-host; unset it and use the target host\'s local Docker daemon')
    if not shutil.which('docker'):
        # Docker Desktop CLI can exist outside the SSH login PATH.
        desktop = Path('/Applications/Docker.app/Contents/Resources/bin/docker')
        if desktop.exists():
            if run([desktop, 'info'], check=False, timeout=15).returncode == 0:
                return [str(desktop)]
            raise SafetyError('Docker Desktop installed but daemon is unavailable; finish setup and start Desktop on target')
        raise SafetyError('Docker is not installed')
    if run(['docker', 'info'], check=False).returncode == 0:
        return ['docker']
    if platform.system() == 'Linux' and run(['sudo', '-n', 'docker', 'info'], check=False).returncode == 0:
        return ['sudo', '-n', 'docker']
    raise SafetyError('Docker daemon unavailable or access denied. Start Docker; on Linux run sudo -v first.')


def verify_docker():
    prefix = docker_prefix()
    info = json.loads(run(prefix + ['info', '--format', '{{json .}}']).stdout)
    if info.get('OSType') != 'linux':
        raise SafetyError('Docker must run Linux containers')
    if int(info['ServerVersion'].split('.')[0]) < 25:
        raise SafetyError('Docker Engine 25+ required for current official health checks')
    run(prefix + ['compose', 'version'])
    if info.get('MemTotal', 0) < 6 * 1024**3 or info.get('NCPU', 0) < 2:
        raise SafetyError('Docker daemon/VM needs at least 6 GiB memory and two CPUs')
    context = run(prefix + ['context', 'inspect'], check=False)
    if context.returncode == 0:
        endpoints = json.loads(context.stdout)[0].get('Endpoints', {})
        host = endpoints.get('docker', {}).get('Host', '')
        if not host.startswith('unix://'):
            raise SafetyError('Docker context points off-host; storage checks must run on the Docker host')
    root = info.get('DockerRootDir')
    if platform.system() == 'Linux' and root:
        storage.validate_db(root)
    if platform.system() == 'Darwin':
        # Docker Desktop disk-image location is part of DB locality, even with local host bind mounts.
        settings = Path.home() / 'Library/Group Containers/group.com.docker/settings-store.json'
        if not settings.exists():
            settings = settings.with_name('settings.json')
        if not settings.exists():
            raise SafetyError('Docker Desktop settings unavailable; cannot verify its VM disk is on local internal storage')
        values = json.loads(settings.read_text())
        disk = values.get('diskImageLocation') or values.get('dataFolder') or values.get('DiskImageLocation')
        if not disk:
            default = Path.home() / 'Library/Containers/com.docker.docker/Data/vms/0/data'
            if not default.exists():
                raise SafetyError('Cannot determine Docker Desktop VM disk location; set/verify its disk image location first')
            disk = str(default)
        storage.validate_db(os.path.expanduser(disk))
    return prefix


def inventory(save=False):
    info = platform_info()
    info['lan_ips'] = lan_addresses()
    info['mounts'] = storage.mounts()
    info['home_disk'] = dict(zip(('total', 'used', 'free'), shutil.disk_usage(Path.home())))
    info['tailscale'] = bool(shutil.which('tailscale'))
    info['docker'] = {}
    try:
        prefix = docker_prefix()
        for key, args in {'containers': ['ps', '-a', '--format', '{{json .}}'],
                          'networks': ['network', 'ls', '--format', '{{json .}}'],
                          'volumes': ['volume', 'ls', '--format', '{{json .}}'],
                          'compose': ['compose', 'version']}.items():
            info['docker'][key] = run(prefix + args, check=False).stdout
    except SafetyError as exc:
        info['docker']['error'] = str(exc)
    if platform.system() == 'Linux':
        info['listeners'] = run(['ss', '-lntup'], check=False).stdout
    else:
        info['listeners'] = run(['lsof', '-nP', '-iTCP', '-sTCP:LISTEN'], check=False).stdout
    if save:
        save_json(private_dir(STATE / 'inventory') / f'{timestamp()}-{os.urandom(3).hex()}.json', info)
    return info


def display(info):
    for key in ('hostname', 'system', 'VERSION_ID', 'architecture', 'cpu', 'cores', 'ram_gib', 'lan_ips'):
        log(f'{key}: {info[key]}')
    log(f'Internal/home disk free: {info["home_disk"]["free"] / 1024**3:.1f} GiB')
    log('Docker: ' + info['docker'].get('compose', info['docker'].get('error', 'unknown')).strip())
    print('Existing containers:\n' + info['docker'].get('containers', '(Docker unavailable)'))
    print('Listening ports:\n' + info['listeners'])
    if info['ram_gib'] < 6:
        raise SafetyError('At least 6 GiB RAM required for this full Immich deployment')


def install_docker():
    info = platform_info()
    if shutil.which('docker') or Path('/Applications/Docker.app').exists():
        verify_docker()
        log('Using existing Docker installation')
        return
    if info['system'] == 'Darwin':
        log('Install Docker Desktop for your Mac architecture: https://docs.docker.com/desktop/setup/install/mac-install/', 'WARN')
        log('Start it in the target Mac login session, accept setup, share your selected folders, then rerun deploy.', 'WARN')
        raise SafetyError('Docker Desktop needs initial interactive OS setup; no permanent software installed by this command')
    packages = ['docker.io', 'docker-compose', 'docker-compose-v2', 'podman-docker', 'containerd', 'runc']
    conflicts = []
    for package in packages:
        result = run(['dpkg-query', '-W', '-f=${db:Status-Status}', package], check=False)
        if result.returncode == 0 and result.stdout == 'installed':
            conflicts.append(package)
    if conflicts:
        raise SafetyError('Existing Docker/container runtime packages detected; refusing to replace them: ' + ', '.join(conflicts))
    distro = info['ID']
    confirm(f'Install Docker Engine and Compose from Docker\'s official {distro} apt repository on this host?')
    run(['sudo', 'apt-get', 'update'], capture=False, timeout=600)
    run(['sudo', 'apt-get', 'install', '-y', 'ca-certificates', 'curl'], capture=False, timeout=600)
    source_path = Path('/etc/apt/sources.list.d/immich-docker.sources')
    if source_path.exists():
        raise SafetyError('Existing apt source preserved; inspect it before installing Docker')
    codename = info.get('UBUNTU_CODENAME') or info.get('VERSION_CODENAME')
    if not codename or not re.fullmatch('[a-z]+', codename):
        raise SafetyError('Invalid apt distribution codename')
    arch = run(['dpkg', '--print-architecture']).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        key = Path(tmp) / 'docker.asc'
        run(['curl', '-fsSL', f'https://download.docker.com/linux/{distro}/gpg', '-o', key])
        run(['sudo', 'install', '-d', '-m', '755', '/etc/apt/keyrings'], capture=False)
        run(['sudo', 'install', '-m', '644', key, '/etc/apt/keyrings/immich-docker.asc'], capture=False)
        source = Path(tmp) / 'docker.sources'
        atomic_write(source, f'Types: deb\nURIs: https://download.docker.com/linux/{distro}\nSuites: {codename}\n'
                     f'Components: stable\nArchitectures: {arch}\nSigned-By: /etc/apt/keyrings/immich-docker.asc\n')
        run(['sudo', 'install', '-m', '644', source, source_path], capture=False)
    run(['sudo', 'apt-get', 'update'], capture=False, timeout=600)
    run(['sudo', 'apt-get', 'install', '-y', 'docker-ce', 'docker-ce-cli', 'containerd.io',
         'docker-buildx-plugin', 'docker-compose-plugin'], capture=False, timeout=600)
    run(['sudo', 'systemctl', 'start', 'docker'], capture=False)
    verify_docker()
