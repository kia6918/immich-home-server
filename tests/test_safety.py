import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import backup
import common
import engine
import environment
import installer
import lifecycle
import migration
import operations
import remote
import storage

FIXTURES = Path(__file__).parent / 'fixtures'


def identity(path='/mnt/photos/Immich', protocol='smb'):
    return {'path': path, 'target': '/mnt/photos', 'fstype': 'cifs' if protocol == 'smb' else 'nfs4',
            'kind': protocol, 'source': '//nas.local/Photos' if protocol == 'smb' else 'nas.local:/exports/photos',
            'uuid': '', 'mount_root': '/', 'relative_path': 'Immich', 'library_id': 'library-1'}


def config(path='/mnt/photos/Immich'):
    return {'storage': identity(path), 'deployment_id': 'server-1', 'db_path': '/internal/postgres',
            'version': 'v3.2.4', 'target_version': 'v3.2.4', 'platform': 'Linux',
            'timezone': 'America/Toronto', 'port': 2283, 'bind_ip': '192.168.1.50'}


class MountTests(unittest.TestCase):
    def setUp(self):
        self.table = storage.parse_mountinfo((FIXTURES / 'linux-mountinfo.txt').read_text())

    def test_local_mount(self):
        info = storage.containing_mount('/home/someone/My Photos', self.table)
        self.assertEqual(info['fstype'], 'ext4')
        self.assertEqual(storage.kind(info['fstype']), 'local')

    def test_usb_spaces(self):
        info = storage.containing_mount('/media/My Photos/Immich', self.table)
        self.assertEqual(info['source'], '/dev/sdb1')
        self.assertEqual(info['target'], '/media/My Photos')

    def test_smb(self):
        info = storage.containing_mount('/mnt/photos/Immich', self.table)
        self.assertEqual(storage.kind(info['fstype']), 'smb')
        self.assertEqual(info['source'], '//nas.local/Photos')

    def test_nfs(self):
        info = storage.containing_mount('/mnt/nfs/Immich', self.table)
        self.assertEqual(storage.kind(info['fstype']), 'nfs')

    def test_mount_prefix_boundary(self):
        self.assertEqual(storage.containing_mount('/mnt/photos-other', self.table)['target'], '/')

    def test_malformed_mountinfo(self):
        for text in ('invalid', '1 2 - ext4 dev', '1 2 3 4 5 - x y'):
            with self.subTest(text=text), self.assertRaises(common.SafetyError):
                storage.parse_mountinfo(text)

    def test_macos_intel_arm_share_parser(self):
        table = storage.parse_macos_mount((FIXTURES / 'macos-mount.txt').read_text())
        self.assertEqual(storage.containing_mount('/Volumes/My Photos/Immich', table)['fstype'], 'apfs')
        self.assertEqual(storage.containing_mount('/Volumes/NAS Photos/Immich', table)['fstype'], 'smbfs')
        self.assertEqual(storage.containing_mount('/Volumes/NFS/Immich', table)['fstype'], 'nfs')

    def test_unsupported_filesystem(self):
        for value in ('exfat', 'tmpfs', 'fuse.sshfs', 'overlay', 'unknown'):
            with self.subTest(value=value), self.assertRaises(common.SafetyError):
                storage.kind(value)

    def test_db_network_rejected(self):
        with patch('pathlib.Path.exists', return_value=True):
            for path in ('/mnt/photos/postgres', '/mnt/nfs/postgres'):
                with self.subTest(path=path), self.assertRaises(common.SafetyError):
                    storage.validate_db(path, self.table, require_internal=False)

    def test_db_local_accepted(self):
        with patch('pathlib.Path.exists', return_value=True):
            self.assertEqual(storage.validate_db('/internal/postgres', self.table, require_internal=False)['fstype'], 'ext4')

    def test_db_usb_rejected(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({'blockdevices': [{'tran': 'usb', 'rm': False}]}), '')
        with patch('pathlib.Path.exists', return_value=True), patch('storage.platform.system', return_value='Linux'), patch('storage.run', return_value=result):
            with self.assertRaisesRegex(common.SafetyError, 'USB'):
                storage.validate_db('/media/My Photos/postgres', self.table)


class IdentityTests(unittest.TestCase):
    def test_identity_match(self):
        storage.validate_identity(identity(), identity())

    def test_fallback_root_rejected(self):
        actual = dict(identity(), target='/', fstype='ext4', kind='local', source='/dev/nvme0n1p2')
        with self.assertRaises(common.SafetyError):
            storage.validate_identity(identity(), actual)

    def test_missing_mount_wrong_source_type_subdir(self):
        for change in ({'source': '//other/Photos'}, {'source': '//nas.local/Other'}, {'fstype': 'nfs4'},
                       {'target': '/mnt/other'}, {'mount_root': '/subdir'}, {'relative_path': 'Other'}):
            with self.subTest(change=change), self.assertRaises(common.SafetyError):
                storage.validate_identity(identity(), dict(identity(), **change))

    def test_local_uuid_match_device_renumbering(self):
        old = dict(identity(), kind='local', fstype='ext4', source='/dev/sdb1', uuid='uuid-123')
        storage.validate_identity(old, dict(old, source='/dev/sdc1'))
        with self.assertRaises(common.SafetyError):
            storage.validate_identity(old, dict(old, uuid='other'))

    def test_source_normalization(self):
        self.assertEqual(storage.canonical_source('//yan@NAS.local./Photos/', 'smb'), '//nas.local/Photos')
        self.assertEqual(storage.canonical_source('NAS.local.:/exports/photos/', 'nfs'), 'nas.local:/exports/photos')
        self.assertNotEqual(storage.canonical_source('//nas/Photos', 'smb'), storage.canonical_source('//nas/photos', 'smb'))

    def test_same_storage_mount_locations_can_differ(self):
        old = identity()
        new = dict(old, target='/Volumes/NAS', path='/Volumes/NAS/Immich', fstype='smbfs')
        self.assertTrue(storage.same_library(old, new))

    def test_new_storage_requires_copy(self):
        self.assertFalse(storage.same_library(identity(), dict(identity(), source='//other/Photos')))
        self.assertFalse(storage.same_library(identity(), dict(identity(), relative_path='Other')))
        self.assertFalse(storage.same_library(identity(), dict(identity(), library_id='other')))

    def test_local_same_storage(self):
        old = dict(identity(), kind='local', fstype='apfs', uuid='disk-uuid')
        self.assertTrue(storage.same_library(old, dict(old, target='/other', path='/other/Immich')))
        self.assertFalse(storage.same_library(old, dict(old, uuid='different')))


class TemporaryState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='immich-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.patches = []
        for module in (common, engine, installer, lifecycle, migration, operations, backup, environment):
            p = patch.object(module, 'STATE', self.state)
            p.start()
            self.addCleanup(p.stop)


class ConfigTests(TemporaryState):
    def test_env_roundtrip_spaces_quotes_dollars_hash(self):
        values = {'PATH_VALUE': "/Volumes/My Photos/a'b\\c$stuff#yes", 'SECRET': 'abcd1234'}
        path = self.root / 'config.env'
        common.atomic_write(path, common.env_text(values))
        self.assertEqual(common.read_env(path), values)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_env_is_never_executed(self):
        sentinel = self.root / 'executed'
        path = self.root / 'config.env'
        path.write_text('VALUE=$(touch ' + str(sentinel) + ')\n')
        self.assertIn('$(touch', common.read_env(path)['VALUE'])
        self.assertFalse(sentinel.exists())

    def test_invalid_env_duplicate_key_and_controls(self):
        for text in ('A=1\nA=2\n', 'export A=1\n', 'bad=2\n', "A='unterminated\n"):
            path = self.root / 'bad.env'
            path.write_text(text)
            with self.subTest(text=text), self.assertRaises(common.SafetyError):
                common.read_env(path)
        with self.assertRaises(common.SafetyError):
            common.env_text({'VALUE': 'line\nbreak'})

    def test_generate_secure_env_preserves_existing_password(self):
        cfg = config('/Volumes/My Photos/Immich')
        engine.generate_env(cfg)
        value = common.read_env(self.state / '.env')
        self.assertEqual(value['UPLOAD_LOCATION'], '/Volumes/My Photos/Immich')
        self.assertEqual(value['IMMICH_VERSION'], 'v3.2.4')
        self.assertEqual(value['TZ'], 'America/Toronto')
        self.assertEqual(len(value['DB_PASSWORD']), 64)
        engine.generate_env(cfg)
        self.assertEqual(common.read_env(self.state / '.env')['DB_PASSWORD'], value['DB_PASSWORD'])

    def test_os_release_is_data(self):
        self.assertEqual(environment.os_release('ID=ubuntu\nVERSION_ID="24.04"\nBAD=$(rm anything)\n')['VERSION_ID'], '24.04')

    def test_non_public_binds_and_ssh_input(self):
        for address in ('0.0.0.0', '8.8.8.8', '::', '169.254.2.4'):
            self.assertFalse(common.private_ip(address))
        for address in ('192.168.1.50', '10.0.0.1', '100.122.103.107', '127.0.0.1'):
            self.assertTrue(common.private_ip(address))
        for value in ('-oProxyCommand=evil', 'user@host;touch file', 'host\ncommand'):
            with self.assertRaises(common.SafetyError):
                common.safe_host(value)
        self.assertEqual(common.safe_host('user@server-mini.local'), 'user@server-mini.local')

    def test_remote_argument_quoting(self):
        import shlex
        args = ['python3', '/home/My Name/cli.py', 'install', '--storage', '/Volumes/My Photos/$photos']
        self.assertEqual(shlex.split(common.remote_command(args)), args)

    def test_symlink_secret_rejected(self):
        original = self.root / 'untouched'
        original.write_text('keep')
        (self.state / '.env').symlink_to(original)
        with self.assertRaises(common.SafetyError):
            common.atomic_write(self.state / '.env', 'overwrite')
        self.assertEqual(original.read_text(), 'keep')


class StorageCheckTests(TemporaryState):
    def setUp(self):
        super().setUp()
        self.library = self.root / 'My Photos'
        self.library.mkdir()
        self.info = identity(str(self.library))
        common.save_json(self.library / '.immich-home-server-library.json', {'library_id': 'library-1'})

    def test_read_write_and_free_space(self):
        with patch('storage.describe', return_value=self.info):
            result = storage.check(self.info, minimum_gib=0)
        self.assertGreater(result.free, 0)
        self.assertEqual([p.name for p in self.library.iterdir()], ['.immich-home-server-library.json'])

    def test_missing_marker_rejected(self):
        (self.library / '.immich-home-server-library.json').unlink()
        with patch('storage.describe', return_value=self.info), self.assertRaises(common.SafetyError):
            storage.check(self.info, minimum_gib=0)

    def test_wrong_library_id_rejected(self):
        common.save_json(self.library / '.immich-home-server-library.json', {'library_id': 'other'})
        with patch('storage.describe', return_value=self.info), self.assertRaises(common.SafetyError):
            storage.check(self.info, minimum_gib=0)

    def test_low_space_rejected(self):
        with patch('storage.describe', return_value=self.info), self.assertRaises(common.SafetyError):
            storage.check(self.info, minimum_gib=10**10)

    def test_no_start_after_missing_storage(self):
        with patch('lifecycle.environment.verify_docker'), patch('lifecycle.storage.bounded_check', side_effect=common.SafetyError('missing mount')), patch('lifecycle.engine.compose') as compose:
            with self.assertRaisesRegex(common.SafetyError, 'missing mount'):
                lifecycle.start(config())
            compose.assert_not_called()
        self.assertFalse((self.state / 'enabled').exists())

    def test_source_block_prevents_start(self):
        (self.state / 'migration-blocked').write_text('blocked')
        with patch('lifecycle.engine.compose') as compose:
            with self.assertRaises(common.SafetyError):
                lifecycle.start(config())
            compose.assert_not_called()

    def test_two_writers_are_rejected(self):
        first = dict(config(str(self.library)), deployment_id='one')
        lifecycle.claim(first)
        lifecycle.claim(first)
        with self.assertRaises(common.SafetyError):
            lifecycle.claim(dict(first, deployment_id='two'))

    def test_no_automatic_stale_owner_recovery(self):
        (self.library / '.immich-home-server-owner').mkdir()
        with self.assertRaises(common.SafetyError):
            lifecycle.claim(config(str(self.library)))

    def test_owner_release_requires_stopped_server(self):
        cfg = config(str(self.library))
        lifecycle.claim(cfg)
        with patch('engine.states', return_value=[{'Service': 'immich-server', 'State': 'running'}]), self.assertRaises(common.SafetyError):
            lifecycle.release_owner(cfg)
        self.assertTrue((self.library / '.immich-home-server-owner').exists())

    def test_owner_transfer_preserves_history(self):
        cfg = config(str(self.library))
        lifecycle.claim(cfg)
        with patch('engine.states', return_value=[]):
            lifecycle.release_owner(cfg)
        self.assertFalse((self.library / '.immich-home-server-owner').exists())
        self.assertEqual(len(list(self.library.glob('.immich-owner-history-*'))), 1)
        lifecycle.claim(dict(cfg, deployment_id='new'))


class ComposeTests(unittest.TestCase):
    def official(self):
        cfg = config()
        services = {name: {'image': 'official/image:v3.2.4', 'restart': 'always', 'container_name': name,
                          'volumes': [], 'env_file': ['.env']} for name in engine.SERVICES}
        services['immich-server']['volumes'] = [{'type': 'bind', 'source': cfg['storage']['path'], 'target': '/data'}]
        services['database']['volumes'] = [{'type': 'bind', 'source': cfg['db_path'], 'target': '/var/lib/postgresql/data'}]
        return {'name': 'immich', 'services': services}

    def test_hardening_and_port(self):
        cfg = config()
        cfg['storage']['kind'] = 'local'
        value = engine.harden_compose(self.official(), cfg)
        self.assertEqual(value['name'], 'immich-home-server')
        for service in value['services'].values():
            self.assertEqual(service['restart'], 'no')
            self.assertNotIn('container_name', service)
            for volume in service['volumes']:
                self.assertFalse(volume['bind']['create_host_path'])
        self.assertEqual(value['services']['immich-server']['ports'][0]['host_ip'], '192.168.1.50')

    def test_changed_upstream_layout_rejected(self):
        value = self.official()
        value['services']['unexpected'] = {}
        with self.assertRaises(common.SafetyError):
            engine.harden_compose(value, config())

    def test_latest_image_exposure_and_wrong_path_rejected(self):
        for mutation in ('latest', 'port', 'privileged', 'upload'):
            value = self.official()
            if mutation == 'latest':
                value['services']['redis']['image'] = 'redis:latest'
            elif mutation == 'port':
                value['services']['database']['ports'] = ['5432:5432']
            elif mutation == 'privileged':
                value['services']['redis']['privileged'] = True
            else:
                value['services']['immich-server']['volumes'][0]['source'] = '/wrong'
            with self.subTest(mutation=mutation), self.assertRaises(common.SafetyError):
                engine.harden_compose(value, config())


class CopyBackupTests(TemporaryState):
    def test_manifest_and_copy_verification(self):
        source = self.root / 'source'
        destination = self.root / 'destination'
        source.mkdir()
        (source / 'My Photos').mkdir()
        (source / 'My Photos/original.jpg').write_bytes(b'original photo bytes')
        (source / '.immich-home-server-library.json').write_text('{}')
        (source / '.immich-home-server-owner').mkdir()
        (source / '.immich-home-server-owner/owner.json').write_text('{}')
        expected = operations.library_manifest(source)
        shutil = __import__('shutil')
        shutil.copytree(source, destination)
        operations.verify_manifest(expected, operations.library_manifest(destination))
        self.assertEqual(expected['count'], 1)
        self.assertEqual(expected['bytes'], 20)
        (destination / 'My Photos/original.jpg').write_bytes(b'changed photo bytes!')
        with self.assertRaises(common.SafetyError):
            operations.verify_manifest(expected, operations.library_manifest(destination))

    def test_full_checksum_all_files(self):
        for name in ('a', 'b', 'c'):
            (self.root / name).write_text(name)
        result = operations.library_manifest(self.root, full=True)
        self.assertEqual(result['count'], 3)
        self.assertEqual(result['checksum_files'], 3)

    def test_nonempty_destination_and_symlinks_rejected(self):
        (self.root / 'existing.jpg').write_text('preserve')
        with self.assertRaises(common.SafetyError):
            operations.ensure_empty_destination(self.root)
        (self.root / 'link').symlink_to(self.root / 'existing.jpg')
        with self.assertRaises(common.SafetyError):
            operations.library_manifest(self.root)
        self.assertEqual((self.root / 'existing.jpg').read_text(), 'preserve')

    def test_backup_checksum_and_path_rejection(self):
        bundle = self.root / 'bundle'
        bundle.mkdir()
        names = ('database.sql.gz', 'config.json', 'config.env', '.env', 'compose.json')
        for name in names:
            (bundle / name).write_text('data')
        manifest = {'format': 1, 'version': 'v3.2.4', 'photos_included': False,
                    'sha256': {name: backup.file_hash(bundle / name) for name in names}}
        common.save_json(bundle / 'manifest.json', manifest)
        backup.validate_bundle(bundle)
        (bundle / '.env').write_text('corrupted')
        with self.assertRaises(common.SafetyError):
            backup.validate_bundle(bundle)
        manifest['sha256']['../outside'] = 'hash'
        common.save_json(bundle / 'manifest.json', manifest)
        with self.assertRaises(common.SafetyError):
            backup.validate_bundle(bundle)

    def test_restore_rejects_version_before_stopping(self):
        with patch('backup.load_config', return_value=config()), patch('backup.validate_bundle', return_value=(self.root, {'version': 'v3.1.0'})), patch('backup.lifecycle.stop') as stop:
            with self.assertRaises(common.SafetyError):
                backup.restore(self.root, confirmed=True)
            stop.assert_not_called()

    def test_rsync_never_deletes_or_overwrites(self):
        result = subprocess.CompletedProcess([], 0, 'rsync version 3.4.0', '')
        destination = self.root / 'My Photos'
        destination.mkdir()
        with patch('operations.run', return_value=result) as invoke:
            operations.rsync_copy('/source/My Photos/', str(destination) + '/')
        args = invoke.call_args.args[0]
        self.assertIn('--ignore-existing', args)
        self.assertIn('--partial', args)
        self.assertNotIn('--delete', args)
        self.assertEqual(args[-2:], ['/source/My Photos/', './'])
        self.assertTrue(invoke.call_args.kwargs['pass_fds'])

    def test_code_bundle_excludes_secrets_and_tests(self):
        # The public deployment bundle explicitly includes code/templates only.
        value = remote.code_bundle()
        with tarfile.open(fileobj=io.BytesIO(value), mode='r:gz') as archive:
            names = archive.getnames()
        self.assertIn('deploy.sh', names)
        self.assertIn('scripts/storage.py', names)
        self.assertNotIn('.env', names)
        self.assertFalse(any(name.startswith(('tests/', '.git/', 'backups/')) for name in names))

    def test_migration_source_freezes_before_dump(self):
        events = []
        with patch('migration.load_config', return_value=config()), patch('migration.storage.bounded_check'), \
             patch('migration.engine.asset_samples', return_value=[]), \
             patch('migration.lifecycle.stop', side_effect=lambda: events.append('stop')), \
             patch('migration.backups.backup', side_effect=lambda: events.append('dump') or self.root):
            frozen = migration.freeze('transaction-1')
        self.assertEqual(events, ['stop', 'dump'])
        self.assertEqual(frozen['phase'], 'frozen')
        self.assertTrue((self.state / 'migration-blocked').exists())

    def test_migration_does_not_reclaim_other_owner_on_rollback(self):
        common.save_json(self.state / 'migration-blocked', {'transaction': 'transaction-1'})
        with patch('migration.load_config', return_value=config()), patch('migration.storage.bounded_check'), \
             patch('migration.lifecycle.claim', side_effect=common.SafetyError('other owner')), \
             patch('migration.lifecycle.start') as start:
            with self.assertRaises(common.SafetyError):
                migration.rollback_source('transaction-1')
            start.assert_not_called()
        self.assertTrue((self.state / 'migration-blocked').exists())


if __name__ == '__main__':
    unittest.main()
