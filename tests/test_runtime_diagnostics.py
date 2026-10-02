"""Traceback visibility and credential redaction without external side effects."""
from contextlib import nullcontext
import logging

import pytest
from edge.runtime.diagnostics import log_exception
from edge.runtime import run_cart as cli
from tests.test_cart_runtime import setup, runtime, acquire, dispatch_next


def test_cli_logs_unexpected_failure_with_stack_and_safety_warning(monkeypatch, caplog):
    monkeypatch.setattr(cli, 'validate_model_path', lambda p: p)
    monkeypatch.setattr(cli, 'cart_lock', lambda _: nullcontext())
    def fail_model(**kwargs):
        raise ValueError('diagnostic model failure')
    monkeypatch.setattr(cli, 'YOLODetector', fail_model)
    assert cli.main(['--cart-id', 'CART-001', '--simulate-weight']) == 1
    assert 'Traceback (most recent call last)' in caplog.text
    assert 'ValueError: diagnostic model failure' in caplog.text
    assert 'in fail_model' in caplog.text and 'run_cart.py' in caplog.text
    assert 'do not replay uncertain events' in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


def test_chained_traceback_redacts_credentials_and_omits_locals(monkeypatch, caplog):
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS', '/secret/service-account.json')
    monkeypatch.setenv('SMARTCART_SECRET', 'environment-secret-value')
    private_local = 'not-for-logs-local'
    try:
        try:
            raise ValueError('password=hunter-secret access_token=access-secret Bearer bearer-secret eyJabc.def.ghi')
        except ValueError as error:
            raise RuntimeError('environment-secret-value /secret/service-account.json') from error
    except RuntimeError as error:
        log_exception(logging.getLogger('test'), 'Operation failed', error)
    assert 'ValueError:' in caplog.text and 'RuntimeError:' in caplog.text
    assert 'direct cause' in caplog.text and 'in test_chained_traceback' in caplog.text
    for secret in ('hunter-secret', 'access-secret', 'bearer-secret', 'eyJabc.def.ghi',
                   'environment-secret-value', '/secret/service-account.json', private_local):
        assert secret not in caplog.text
    assert '[REDACTED]' in caplog.text


def test_private_key_redacted(caplog):
    try:
        raise ValueError('key: -----BEGIN PRIVATE KEY-----\nkey-material\n-----END PRIVATE KEY-----')
    except ValueError as error:
        log_exception(logging.getLogger('test'), 'Failed', error)
    assert 'key-material' not in caplog.text
    assert '[REDACTED PRIVATE KEY]' in caplog.text


@pytest.mark.parametrize('phase', ['recognition', 'session'])
def test_dispatch_preserves_caught_traceback_without_billing(setup, monkeypatch, caplog, phase):
    rt = runtime()
    def fail(*args):
        raise ValueError('dispatch diagnostic failure')
    if phase == 'recognition':
        monkeypatch.setattr(rt.detector, 'detect', fail)
    else:
        monkeypatch.setattr(rt.session, 'process_add', fail)
    acquire(rt, 75)
    assert dispatch_next(rt).status == 'rejected'
    assert 'ValueError: dispatch diagnostic failure' in caplog.text
    assert 'in fail' in caplog.text


def test_acquisition_exception_retains_origin_across_thread(setup, caplog):
    rt = runtime()
    def camera_failure():
        raise ValueError('acquisition diagnostic failure')
    rt.camera.read = camera_failure
    try:
        rt.run()
    except ValueError as error:
        log_exception(logging.getLogger('test'), 'Runtime stopped; do not replay uncertain events.', error)
    else:
        pytest.fail('Acquisition exception was lost')
    assert 'in camera_failure' in caplog.text
    assert 'in acquire_loop' in caplog.text
    assert 'ValueError: acquisition diagnostic failure' in caplog.text
