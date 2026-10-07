#!/usr/bin/env python3
"""Parse the real pinned release with real Compose; no daemon or containers needed."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import engine

parser = argparse.ArgumentParser()
parser.add_argument('--compose', default='docker compose')
parser.add_argument('--version', default='v3.2.4')
args = parser.parse_args()
with tempfile.TemporaryDirectory(prefix='immich-compose-smoke-') as temporary:
    state = Path(temporary).resolve()
    engine.STATE = state
    metadata = engine.release(args.version)
    engine.download_release(metadata, state / 'official')
    cfg = {'storage': {'kind': 'local', 'path': str(state / "My Photos/a'b$$photos")},
           'db_path': str(state / 'postgres'), 'platform': 'Darwin', 'version': args.version,
           'timezone': 'America/Toronto', 'port': 2283, 'bind_ip': '127.0.0.1'}
    engine.generate_env(cfg)
    base = shlex.split(args.compose) + ['--project-directory', str(state), '--env-file', str(state / '.env')]
    def parse(path):
        result = subprocess.run(base + ['-f', str(path), 'config', '--format', 'json'],
                                check=True, text=True, capture_output=True, timeout=60)
        return engine.interpolation(json.loads(result.stdout), escape=False)
    value = parse(state / 'official/docker-compose.yml')
    common.save_json(state / 'compose.json', engine.interpolation(engine.harden_compose(value, cfg), escape=True))
    result = parse(state / 'compose.json')
    assert result['services']['immich-server']['volumes'][0]['source'] == cfg['storage']['path']
    assert all(service['restart'] == 'no' for service in result['services'].values())
    assert not any('container_name' in service for service in result['services'].values())
    assert result['services']['immich-server']['ports'][0]['host_ip'] == '127.0.0.1'
    print('PASS: official ' + args.version + ' Compose roundtrip, path escaping, isolated names and guarded restarts')
