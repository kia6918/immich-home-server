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
        (self.state / 'migration-blocked').write_text('pending')
        with patch('migration.load_config', return_value=config()), patch('migration.storage.bounded_check'), \
             patch('migration.lifecycle.start', side_effect=common.SafetyError('failed health')), patch('migration.lifecycle.stop') as stop:
            with self.assertRaises(common.SafetyError):
                migration.activate('transaction', [])
            stop.assert_called_once()
        self.assertEqual(json.loads((self.state / 'migration-blocked').read_text())['phase'], 'failed-destination')

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
                      patch('engine.compose'), patch('lifecycle.start', side_effect=common.SafetyError('health failed'))]:
                stack.enter_context(p)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(common.SafetyError):
                operations.update('v3.2.5')
        self.assertTrue((self.state / 'maintenance').exists())


if __name__ == '__main__':
    unittest.main()
