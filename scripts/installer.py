"""Interactive host-local installer; SSH uses the same code without secrets in transit."""
import json
import os
from pathlib import Path
import platform
import shutil
import uuid

from common import ROOT, STATE, SafetyError, atomic_write, choose, confirm, load_config, log, persist, private_dir, private_ip, prompt, read_env, run, save_json, timestamp
import engine
import environment
import lifecycle
import storage


def configure_library(path, *, library_id=None, existing=False):
    requested = Path(path).expanduser().absolute()
    if requested == Path('/'):
        raise SafetyError('Use a dedicated library directory, not filesystem root')
    parent = requested if requested.exists() else requested.parent
    info = storage.describe(parent)
    if info['kind'] != 'local' and info['target'] == '/':
        raise SafetyError('Network storage is not mounted')
    if not requested.exists():
        requested.mkdir(mode=0o750, exist_ok=False)  # Parent must already exist on the selected disk/share.
    requested = requested.resolve(strict=True)
    marker = requested / '.immich-home-server-library.json'
    if marker.exists():
        value = json.loads(marker.read_text())
        if library_id and value.get('library_id') != library_id:
            raise SafetyError('Selected existing library does not match source library identity')
        if not existing:
            raise SafetyError('Existing library requires migration/restore or reconfiguration, not a fresh empty database')
        library_id = value['library_id']
    else:
        if any(requested.iterdir()):
            raise SafetyError('Directory is not empty or is an unmanaged library. Use a new dedicated directory; no existing files modified.')
        if existing:
            raise SafetyError('Existing library marker missing; select the actual source library')
        library_id = library_id or str(uuid.uuid4())
        save_json(marker, {'format': 1, 'library_id': library_id, 'created_at': timestamp()})
    info = storage.describe(requested)
    info['library_id'] = library_id
    storage.check(info)
    return info


def default_db():
    if platform.system() == 'Darwin':
        return Path.home() / 'Library/Application Support/immich-home-server/postgres'
    return Path.home() / '.local/share/immich-home-server/postgres'


def install(args, *, prepare=False, source=None):
    private_dir(STATE)
    if (STATE / 'config.json').exists():
        if prepare:
            raise SafetyError('Destination already has a deployment; migration requires a fresh destination state')
        return existing_menu(args)
    info = environment.inventory(save=True)
    environment.display(info)
    environment.install_docker()
    environment.verify_docker()
    # Detect abandoned containers in our namespace, never remove/adopt them blindly.
    prefix = environment.docker_prefix()
    if run(prefix + ['ps', '-aq', '--filter', 'label=com.docker.compose.project=' + engine.PROJECT]).stdout.strip():
        raise SafetyError('Existing immich-home-server containers without config; recover the original state before proceeding')
    path = args.storage or storage.select_path()
    print(f'\nSelected photo directory: {path}')
    storage.show_candidate(str(Path(path).expanduser() if Path(path).expanduser().exists() else Path(path).expanduser().parent))
    db = Path(args.db_path).expanduser().absolute() if args.db_path else default_db()
    storage.validate_db(db)
    print(f'Local PostgreSQL directory: {db}')
    private_addresses = [ip for ip in info['lan_ips'] if private_ip(ip)]
    bind_ip = args.bind_ip or prompt('LAN IPv4 address to bind (use static DHCP reservation)', private_addresses[0] if len(private_addresses) == 1 else None)
    if not private_ip(bind_ip):
        raise SafetyError('Choose an explicit private LAN or Tailscale IPv4 address')
    port = int(args.port or (2283 if args.yes else prompt('Immich port', 2283)))
    metadata = engine.release(source['version'] if source else args.version)
    print(f'Immich release: {metadata["tag_name"]}\nRelease notes: {metadata["html_url"]}')
    if not args.yes:
        confirm('Install this configuration? Only this Immich deployment will be created.')
    if db.exists() and any(db.iterdir()):
        raise SafetyError('Existing database directory preserved. Restore its deployment or choose a new empty local DB directory.')
    library = configure_library(path, library_id=source['storage']['library_id'] if source else None,
                                existing=prepare and args.same_storage)
    db.mkdir(mode=0o700, parents=True, exist_ok=True)
    defaults = read_env(ROOT / 'config/defaults.env')
    config = {'format': 1, 'deployment_id': str(uuid.uuid4()), 'platform': info['system'],
              'version': metadata['tag_name'], 'target_version': metadata['tag_name'],
              'bind_ip': bind_ip, 'port': port, 'timezone': args.timezone or defaults['IMMICH_TIMEZONE'],
              'min_free_gib': float(defaults['MIN_FREE_GIB']), 'health_timeout': int(defaults['HEALTH_TIMEOUT_SECONDS']),
              'storage': library, 'db_path': str(db), 'db_storage': storage.describe(db)}
    storage.configure_docker_volume(config)
    persist(config)
    atomic_write(STATE / 'maintenance', 'installation pending validation\n')
    if prepare:
        save_json(STATE / 'migration-blocked', {'transaction': args.transaction, 'phase': 'prepared',
                  'source_deployment_id': source['deployment_id']})
    official = STATE / 'official' / config['version']
    engine.download_release(metadata, official)
    engine.generate_env(config)
    engine.build_compose(config, official)
    engine.check_port(config)
    engine.compose('pull', timeout=1800, capture=False)
    engine.preflight_photo(config)
    lifecycle.install_supervisor()
    (STATE / 'maintenance').unlink()
    if prepare:
        log('Destination prepared at matching version; all application services remain stopped')
        return config
    url = lifecycle.start(config)
    if not lifecycle.doctor():
        raise SafetyError('Post-install doctor failed; inspect before using this deployment')
    onboarding(url)
    return config


def existing_menu(args=None):
    choice = choose('Existing Immich deployment found', ['Status', 'Repair', 'Reconfigure storage',
                    'Update', 'Reinstall application while preserving data', 'Exit'])
    if choice == 0:
        lifecycle.status()
    elif choice in (1, 4):
        config = load_config()
        if args and args.bind_ip:
            if not private_ip(args.bind_ip) or args.bind_ip not in environment.lan_addresses():
                raise SafetyError('Repair bind address must be a private address currently assigned to this host')
            lifecycle.stop()
            config['bind_ip'] = args.bind_ip
            if args.port:
                config['port'] = args.port
            persist(config)
        storage.bounded_check(config)
        storage.validate_db(config['db_path'])
        engine.generate_env(config)
        official = STATE / 'official' / config['version']
        if not official.exists():
            engine.download_release(engine.release(config['version']), official)
        engine.build_compose(config, official)
        lifecycle.install_supervisor()
        if (STATE / 'migration-blocked').exists():
            raise SafetyError('Migration lock preserved. Use documented migration/rollback transaction.')
        if (STATE / 'maintenance').exists():
            confirm('A maintenance transaction is incomplete. Have you reviewed the protected logs/config and verified it is safe to start?', 'RECOVER')
            (STATE / 'maintenance').unlink()
        if choice == 4:
            lifecycle.stop()
            engine.compose('pull', timeout=1800, capture=False)
        lifecycle.start(config, recreate=choice == 4)
        lifecycle.doctor()
    elif choice == 2:
        from operations import reconfigure
        reconfigure()
    elif choice == 3:
        from operations import update
        update()


def onboarding(url):
    print(f'\nImmich is ready.\nServer URL: {url}\n\n'
          'iPhone:\n1. Install Immich.\n2. Enter the server URL.\n3. Create/login to your account.\n'
          '4. Allow full photo access.\n5. Enable backup.\n6. Enable Background App Refresh.\n'
          '7. Choose backup albums.\n8. Keep the phone charging/on Wi-Fi for initial upload.\n')


def inspect():
    environment.display(environment.inventory())
    print('\nMounted storage candidates:')
    for path in storage.candidates():
        print(path)
        storage.show_candidate(path)
