#!/usr/bin/env python3
"""Prove root-fallback regression coverage by disabling its validator in memory."""
from unittest.mock import patch
import unittest

from test_safety import IdentityTests
from test_workflows import WorkflowTests
import migration
import storage

baseline = unittest.TestResult()
IdentityTests('test_fallback_root_rejected').run(baseline)
assert baseline.wasSuccessful(), 'Current root-fallback rejection is broken'
with patch.object(storage, 'validate_identity', return_value=None):
    mutated = unittest.TestResult()
    IdentityTests('test_fallback_root_rejected').run(mutated)
assert len(mutated.failures) == 1 and not mutated.errors, 'Regression test did not catch disabled protection'
print('PASS: root-fallback test passes normally and fails when storage identity validation is disabled')

protected = unittest.TestResult()
WorkflowTests('test_activation_rejects_unrelated_transaction_before_start').run(protected)
assert protected.wasSuccessful(), 'Migration transaction binding is broken'
with patch.object(migration, 'validate_destination_transaction', return_value=None):
    mutated = unittest.TestResult()
    WorkflowTests('test_activation_rejects_unrelated_transaction_before_start').run(mutated)
assert len(mutated.failures) == 1 and not mutated.errors, 'Regression test did not catch disabled transaction protection'
print('PASS: migration transaction test fails when transaction validation is disabled')

import inspect
import environment
original = environment.docker_prefix
source = inspect.getsource(original)
source = source.replace("    if os.environ.get('DOCKER_HOST') and not os.environ['DOCKER_HOST'].startswith('unix://'):\n        raise SafetyError('DOCKER_HOST points off-host; unset it and use the target host\\'s local Docker daemon')\n", '')
assert source != inspect.getsource(original), 'Could not locate Docker override protection'
try:
    exec(compile(source, '<in-memory-docker-override-mutation>', 'exec'), environment.__dict__)
    mutated = unittest.TestResult()
    WorkflowTests('test_remote_docker_host_override_rejected_before_any_command').run(mutated)
finally:
    environment.docker_prefix = original
assert len(mutated.failures) == 1 and not mutated.errors, 'Regression did not catch off-host Docker override'
print('PASS: Docker override test fails when off-host override protection is disabled')

import engine
original = engine.download_release
source = inspect.getsource(original)
source = source.replace("staged = destination.with_name(destination.name + '.download-' + os.urandom(4).hex())", "staged = destination")
assert source != inspect.getsource(original), 'Could not locate staged download protection'
try:
    exec(compile(source, '<in-memory-release-cache-mutation>', 'exec'), engine.__dict__)
    mutated = unittest.TestResult()
    WorkflowTests('test_interrupted_release_download_can_retry_and_cache_detects_corruption').run(mutated)
finally:
    engine.download_release = original
assert len(mutated.failures) == 1 and not mutated.errors, 'Regression did not catch premature cache publication'
print('PASS: release retry test fails when partial download is published as a complete cache')
