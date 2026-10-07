#!/usr/bin/env python3
"""Synthetic uploads/downloads on an explicitly marked disposable target only."""
import argparse
import binascii
import hashlib
import json
import os
from pathlib import Path
import secrets
import struct
import sys
import urllib.request
import uuid
import zlib

parser = argparse.ArgumentParser()
parser.add_argument('--state', required=True, help='Protected directory with disposable-test marker')
parser.add_argument('--url', required=True)
parser.add_argument('--upload', action='store_true')
args = parser.parse_args()
state = Path(args.state).expanduser().resolve(strict=True)
if not (state / 'disposable-test').is_file():
    raise SystemExit('Refusing a target without an operator-created disposable-test marker')
os.umask(0o077)
auth_path = state / 'test-assets.json'
auth = json.loads(auth_path.read_text()) if auth_path.exists() else {
    'email': 'immich-disposable@example.invalid', 'password': secrets.token_urlsafe(32), 'assets': []}


def request(path, body=None, token=None, content_type='application/json'):
    headers = {'Content-Type': content_type}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    req = urllib.request.Request(args.url.rstrip('/') + path, data=body, headers=headers)
    # Synthetic local HTTP verification must not traverse a user's outbound proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=60) as response:
        value = response.read()
        return json.loads(value) if 'application/json' in response.headers.get('Content-Type', '') else value


if not auth_path.exists():
    if not args.upload:
        raise SystemExit('No test account/assets; use --upload on a disposable target first')
    request('/api/auth/admin-sign-up', {**{key: auth[key] for key in ('email', 'password')}, 'name': 'Disposable test'})
token = request('/api/auth/login', {key: auth[key] for key in ('email', 'password')})['accessToken']


def png(index):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', binascii.crc32(kind + data) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + \
        chunk(b'IDAT', zlib.compress(bytes([0, index * 40, 100, 200]))) + chunk(b'IEND', b'')


if args.upload and not auth['assets']:
    for index in range(5):
        value = png(index)
        boundary = 'immich-test-' + uuid.uuid4().hex
        fields = {'deviceAssetId': uuid.uuid4().hex, 'deviceId': 'disposable-integration-test',
                  'fileCreatedAt': '2026-10-07T12:00:00.000Z', 'fileModifiedAt': '2026-10-07T12:00:00.000Z'}
        data = b''
        for key, item in fields.items():
            data += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{item}\r\n'.encode()
        data += f'--{boundary}\r\nContent-Disposition: form-data; name="assetData"; filename="synthetic-{index}.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + value + f'\r\n--{boundary}--\r\n'.encode()
        result = request('/api/assets', data, token, 'multipart/form-data; boundary=' + boundary)
        auth['assets'].append({'id': result['id'], 'sha256': hashlib.sha256(value).hexdigest()})
    auth_path.write_text(json.dumps(auth))
    auth_path.chmod(0o600)
for asset in auth['assets']:
    value = request('/api/assets/' + asset['id'] + '/original', token=token)
    assert hashlib.sha256(value).hexdigest() == asset['sha256'], 'Original download checksum mismatch'
print('PASS: login and authenticated original download/checksum for ' + str(len(auth['assets'])) + ' synthetic assets')
