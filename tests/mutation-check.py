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
