"""Workflow checks exercise ordering and preservation, with no real service mutation."""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_safety import TemporaryState, config, identity
import backup
import cli
import common
import engine
import installer
import lifecycle
import migration
import operations
import remote
import storage


class WorkflowTests(TemporaryState):
    def test_interactive_deploy_local_path(self):
        events = []
        table = {'fstype': 'ext4', 'kind': 'local', 'target': str(self.root), 'source': '/dev/ssd',
                 'mount_root': '/', 'uuid': 'persistent-volume'}
        def describe(path):
            return dict(table, path=str(Path(path).resolve()), relative_path=str(Path(path).relative_to(self.root)))
        inventory = {'hostname': 'fixture-host', 'system': 'Linux', 'ram_gib': 16, 'lan_ips': ['192.168.1.50']}
        metadata = {'tag_name': 'v3.2.4', 'html_url': 'https://github.com/immich-app/immich/releases/tag/v3.2.4'}
        result = subprocess.CompletedProcess([], 0, '', '')
        def compose(*args, **kwargs):
            events.append(('compose', args))
            return result
        def download(_metadata, destination):
            destination.mkdir(parents=True)
            (destination / 'docker-compose.yml').write_text('official release fixture')
            events.append(('download', 'official'))
        with contextlib.ExitStack() as stack:
            patches = [patch('sys.argv', ['cli.py', 'deploy', '--db-path', str(self.root / 'postgres')]),
                       patch('builtins.input', side_effect=['1', '1', '', '', '', 'YES']),
                       patch('installer.environment.inventory', side_effect=lambda **kwargs: events.append(('inventory', kwargs)) or inventory),
                       patch('installer.environment.display'), patch('installer.environment.install_docker'),
                       patch('installer.environment.verify_docker'), patch('installer.environment.docker_prefix', return_value=['docker']),
                       patch('installer.run', return_value=result), patch('storage.candidates', return_value=[str(self.root)]),
                       patch('storage.describe', side_effect=describe), patch('storage.validate_db'),
                       patch('engine.release', return_value=metadata), patch('engine.download_release', side_effect=download),
                       patch('engine.build_compose', side_effect=lambda cfg, path: common.save_json(self.state / 'compose.json', {})),
                       patch('engine.check_port'), patch('engine.compose', side_effect=compose),
                       patch('engine.preflight_photo'), patch('lifecycle.install_supervisor'),
                       patch('lifecycle.storage.bounded_check'), patch('lifecycle.engine.healthy', return_value=True),
                       patch('lifecycle.engine.http', return_value='http://192.168.1.50:2283'),
                       patch('lifecycle.engine.verify_upload_bind'), patch('lifecycle.doctor', return_value=True)]
            for p in patches:
                stack.enter_context(p)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                cli.main()
        cfg = json.loads((self.state / 'config.json').read_text())
        self.assertEqual(cfg['storage']['path'], str(self.root / 'Immich'))
        self.assertEqual(cfg['db_path'], str(self.root / 'postgres'))
        self.assertEqual(cfg['bind_ip'], '192.168.1.50')
        self.assertTrue((self.state / 'enabled').exists())
        self.assertFalse((self.state / 'maintenance').exists())
        self.assertEqual(events[0][0], 'inventory')
        self.assertIn('Immich is ready.', output.getvalue())
        self.assertNotIn(common.read_env(self.state / '.env')['DB_PASSWORD'], output.getvalue())

    def test_remote_deploy_transfers_only_bundle_then_runs_same_installer(self):
        args = SimpleNamespace(host='user@host', ssh_port=None, storage='/mnt/My Photos/Immich',
                               db_path=None, port=2283, bind_ip='192.168.1.50', version='v3.2.4', timezone=None, yes=False)
        with patch('remote.bootstrap', return_value='/home/user/.immich-installer.new') as bootstrap, patch('remote.execute') as execute:
            remote.deploy(args)
        bootstrap.assert_called_once_with('user@host', None)
        self.assertEqual(execute.call_args_list[0].args[1], 'install')
        self.assertIn('/mnt/My Photos/Immich', execute.call_args_list[0].args[2])
        self.assertTrue(execute.call_args_list[0].kwargs['tty'])
        self.assertEqual(execute.call_args_list[-1].args[1], 'status')

    def test_network_compose_fences_nas_without_bind_fallback(self):
        cfg = config()
        cfg['photo_volume'] = {'name': 'immich-home-server-photos-fixture', 'subpath': 'Immich',
                               'options_file': 'network.json', 'protocol': 'smb', 'source': '//nas.local/Photos'}
        common.save_json(self.state / 'network.json', {'type': 'cifs', 'device': '//nas.local/Photos',
                         'o': 'addr=nas.local,username=fixture,password=fixture-secret'})
        services = {name: {'image': 'official/image:v3.2.4', 'volumes': []} for name in engine.SERVICES}
        services['immich-server']['volumes'] = [{'type': 'bind', 'source': cfg['storage']['path'], 'target': '/data'}]
        services['database']['volumes'] = [{'type': 'bind', 'source': cfg['db_path'], 'target': '/var/lib/postgresql/data'}]
        value = engine.harden_compose({'services': services}, cfg)
        photo = next(v for v in value['services']['immich-server']['volumes'] if v['target'] == '/data')
        self.assertEqual(photo['type'], 'volume')
        self.assertEqual(photo['volume'], {'nocopy': True, 'subpath': 'Immich'})
        self.assertEqual(value['volumes']['network-photo']['driver_opts']['device'], '//nas.local/Photos')
        self.assertNotIn('password', json.dumps(cfg))
        self.assertEqual((self.state / 'network.json').stat().st_mode & 0o777, 0o600)

    def test_network_wrong_driver_or_source_refused(self):
        cfg = config()
        cfg['photo_volume'] = {'name': 'fixture', 'subpath': 'Immich', 'options_file': 'network.json',
                               'protocol': 'smb', 'source': '//nas.local/Photos'}
        common.save_json(self.state / 'network.json', {'type': 'none', 'device': '/local/fallback', 'o': 'bind'})
        services = {name: {'image': 'official/image:v3.2.4', 'volumes': []} for name in engine.SERVICES}
        services['immich-server']['volumes'] = [{'type': 'bind', 'source': cfg['storage']['path'], 'target': '/data'}]
        services['database']['volumes'] = [{'type': 'bind', 'source': cfg['db_path'], 'target': '/var/lib/postgresql/data'}]
        with self.assertRaises(common.SafetyError):
            engine.harden_compose({'services': services}, cfg)

    def test_compose_interpolation_roundtrip(self):
        value = {'path': "/Volumes/a'b$$photos$one", 'health': ['echo $VALUE']}
        self.assertEqual(engine.interpolation(engine.interpolation(value, escape=True), escape=False), value)

    def test_ssh_alias_retains_its_configured_port(self):
        args = common.ssh_args('fixture-alias')
        self.assertNotIn('-p', args)
        self.assertEqual(common.ssh_args('fixture', 2222)[1:3], ['-p', '2222'])

    def test_exec_checks_do_not_wait_for_ssh_tty_input(self):
        with patch('engine.docker_prefix', return_value=['docker']), patch('engine.run') as invoke:
            engine.compose('exec', '-T', 'immich-server', 'cat', '/data/.immich-home-server-library.json')
        self.assertIn('--interactive=false', invoke.call_args.args[0])

    def test_failed_migration_activation_stops_and_reblocks(self):
        common.save_json(self.state / 'migration-blocked', {'transaction': 'transaction', 'phase': 'prepared'})
        with patch('migration.load_config', return_value=config()), patch('migration.storage.bounded_check'), \
             patch('migration.lifecycle.start', side_effect=common.SafetyError('failed health')), patch('migration.lifecycle.stop') as stop:
            with self.assertRaises(common.SafetyError):
                migration.activate('transaction', [])
            stop.assert_called_once()
        self.assertEqual(json.loads((self.state / 'migration-blocked').read_text())['phase'], 'failed-destination')

    def test_activation_rejects_unrelated_transaction_before_start(self):
        common.save_json(self.state / 'migration-blocked', {'transaction': 'another', 'phase': 'prepared'})
        with patch('migration.load_config', return_value=config()), patch('storage.bounded_check'), \
             patch('engine.verify_assets'), patch('lifecycle.doctor', return_value=True), patch('lifecycle.start') as start:
            with self.assertRaises(common.SafetyError):
                migration.activate('wrong', [])
            start.assert_not_called()

    def test_rollback_destination_requires_known_transaction(self):
        args = SimpleNamespace(command='_rollback-destination', transaction='unknown')
        with patch('cli.load_config', return_value=config()), patch('lifecycle.stop') as stop:
            with self.assertRaises(common.SafetyError):
                cli.worker(args)
            stop.assert_not_called()

    def test_freeze_resumes_saved_samples_without_running_app(self):
        common.save_json(self.state / 'migration-blocked', {'transaction': 'tx', 'phase': 'freezing', 'samples': []})
        with patch('migration.load_config', return_value=config()), patch('storage.bounded_check'), \
             patch('engine.asset_samples', side_effect=AssertionError('stopped app queried')), \
             patch('lifecycle.stop'), patch('backup.backup', return_value=self.root):
            value = migration.freeze('tx')
        self.assertEqual(value['phase'], 'frozen')

    def test_migration_restore_rejects_wrong_transaction_without_db_mutation(self):
        common.save_json(self.state / 'migration-blocked', {'transaction': 'real', 'phase': 'prepared'})
        with patch('sys.argv', ['cli.py', '_restore-migration', '--transaction', 'wrong', '--bundle', '/fixture']), \
             patch('backup.restore') as restore:
            with self.assertRaises(common.SafetyError):
                cli.main()
            restore.assert_not_called()

    def test_storage_candidates_hide_docker_internal_mounts(self):
        table = [{'target': p, 'fstype': 'cifs'} for p in ['/mnt/photos', '/var/lib/docker/volumes/x/_data']]
        with patch('storage.mounts', return_value=table):
            candidates = storage.candidates()
        self.assertIn('/mnt/photos', candidates)
        self.assertNotIn('/var/lib/docker/volumes/x/_data', candidates)

    def test_nfs_helper_provisioning_only_when_missing(self):
        for missing in (True, False):
            mount = self.root / ('new-nfs-' + str(missing))
            with self.subTest(missing=missing), contextlib.ExitStack() as stack:
                stack.enter_context(patch('builtins.input', side_effect=['2', 'nas.local', '/exports/photos', str(mount), 'YES']))
                stack.enter_context(patch('storage.platform.system', return_value='Linux'))
                stack.enter_context(patch('storage.mounts', return_value=[{'target': '/', 'fstype': 'ext4'}]))
                stack.enter_context(patch('storage.describe', return_value={'kind': 'nfs', 'source': 'nas.local:/exports/photos'}))
                stack.enter_context(patch('storage.shutil.which', return_value=None if missing else '/usr/sbin/mount.nfs'))
                stack.enter_context(patch('pathlib.Path.is_file', return_value=False))
                stack.enter_context(patch('environment.platform_info'))
                invoke = stack.enter_context(patch('storage.run'))
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(storage.add_network(), str(mount))
                installations = [call.args[0] for call in invoke.call_args_list if 'apt-get' in call.args[0]]
                self.assertEqual(len(installations), 2 if missing else 0)
                if missing:
                    self.assertIn('nfs-common', installations[-1])

    def test_operation_lock_refuses_concurrent_mutation(self):
        with common.operation_lock(wait=0):
            with self.assertRaises(common.SafetyError):
                with common.operation_lock(wait=0):
                    self.fail('Concurrent mutation accepted')

    def test_lan_health_check_bypasses_outbound_proxy(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"res":"pong"}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch('engine.urllib.request.build_opener', return_value=opener) as build:
            self.assertEqual(engine.http(config()), 'http://192.168.1.50:2283')
        self.assertEqual(build.call_args.args[0].proxies, {})

    def test_option_like_ssh_username_rejected(self):
        with self.assertRaises(common.SafetyError):
            common.safe_host('-F@server')

    def test_remote_docker_host_override_rejected_before_any_command(self):
        import os
        import environment
        with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://another-host:2375'}), patch('environment.run') as invoke:
            with self.assertRaises(common.SafetyError):
                environment.docker_prefix()
            invoke.assert_not_called()

    def test_interrupted_release_download_can_retry_and_cache_detects_corruption(self):
        directory = self.state / 'official/v3.2.4'
        metadata = {'tag_name': 'v3.2.4', 'html_url': 'https://github.com/immich-app/immich/releases/tag/v3.2.4',
                    'assets': [{'name': f, 'browser_download_url': 'https://github.com/immich-app/immich/releases/download/v3.2.4/' + f}
                               for f in ['docker-compose.yml', 'example.env']]}
        with patch('engine.fetch', side_effect=[b'docker-compose.yml', b'compose fixture', common.SafetyError('network interrupted')]):
            with self.assertRaises(common.SafetyError):
                engine.download_release(metadata, directory)
        self.assertFalse(directory.exists())
        with patch('engine.fetch', side_effect=[b'docker-compose.yml', b'compose fixture', b'env fixture']):
            engine.download_release(metadata, directory)
        engine.verify_release(directory, 'v3.2.4')
        (directory / 'docker-compose.yml').write_text('changed')
        with self.assertRaises(common.SafetyError):
            engine.verify_release(directory, 'v3.2.4')

    def test_repair_preserves_incomplete_release_before_retry(self):
        directory = self.state / 'official/v3.2.4'
        directory.mkdir(parents=True)
        (directory / 'sentinel').write_text('preserved')
        with patch('engine.release', return_value={'tag_name': 'v3.2.4'}), patch('engine.download_release') as download, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(engine.ensure_release('v3.2.4'), directory)
        download.assert_called_once()
        preserved = list((self.state / 'official').glob('v3.2.4.incomplete-*'))
        self.assertEqual(len(preserved), 1)
        self.assertEqual((preserved[0] / 'sentinel').read_text(), 'preserved')

    def test_update_failure_never_automatically_downgrades(self):
        cfg = config()
        official = self.state / 'official/v3.2.5'
        official.mkdir(parents=True)
        with contextlib.ExitStack() as stack:
            for p in [patch('operations.load_config', return_value=cfg), patch('lifecycle.doctor', return_value=True),
                      patch('storage.bounded_check'), patch('engine.release', return_value={'tag_name': 'v3.2.5', 'html_url': 'official', 'body': 'notes'}),
                      patch('operations.confirm'), patch('engine.asset_samples', return_value=[]),
                      patch('lifecycle.stop'), patch('backup.backup', return_value=self.root), patch('backup.database_image', return_value='postgres:fixed'),
                      patch('operations.persist'), patch('engine.generate_env'), patch('engine.build_compose'),
                      patch('engine.ensure_release', return_value=official),
                      patch('engine.compose'), patch('lifecycle.start', side_effect=common.SafetyError('health failed'))]:
                stack.enter_context(p)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(common.SafetyError):
                operations.update('v3.2.5')
        self.assertTrue((self.state / 'maintenance').exists())


if __name__ == '__main__':
    unittest.main()
