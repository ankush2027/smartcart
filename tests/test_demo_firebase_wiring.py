"""Exercise demo entry-point wiring with real SQLite and offline Firebase/devices."""
from datetime import datetime
import socket
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from edge.api import run_server
from edge.billing import billing
from edge.database import database
from edge.database.product_repository import add_product
from edge.demo import run_demo
from edge.detection.product_resolver import Detection
from edge.firebase import client as firebase
from edge.payment.service import PaymentService
from tests.test_e2e_lifecycle import _FakeReference


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail('No network permitted')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setenv('SMARTCART_DATABASE_PATH', str(tmp_path / 'demo.db'))
    monkeypatch.setenv('SMARTCART_ALLOWED_CARTS', 'CART-001')
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_path / 'demo.db')
    monkeypatch.setattr(firebase, '_initialized', False)
    monkeypatch.setattr(firebase, '_injected_ref', None)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    root = _FakeReference()
    events = []
    initialize = firebase.initialize

    def init():
        events.append('initialize')
        initialize(_fake_ref=root)

    monkeypatch.setattr(firebase, 'initialize', init)

    def verify(token):
        assert firebase._initialized
        events.append('authenticate')
        if token not in ('owner', 'other'):
            raise ValueError('Invalid offline token')
        return {'uid': token}

    # Exercise the default production verifier boundary, not a demo auth bypass.
    monkeypatch.setitem(sys.modules, 'firebase_admin', SimpleNamespace(
        auth=SimpleNamespace(verify_id_token=verify)))

    def prepare(path):
        assert firebase._initialized
        events.append('prepare')
        database.initialize_database()
        add_product('Snack', 'snack', 20, 50, 'Snack')

    monkeypatch.setattr(run_demo, 'prepare_demo_database', prepare)
    supervisor = SimpleNamespace(run_forever=lambda: None, request_stop=Mock())

    def supervise(**kwargs):
        assert firebase._initialized
        supervisor.factory = kwargs['runtime_factory']
        events.append('supervisor')
        return supervisor

    monkeypatch.setattr(run_demo, 'CartSupervisor', supervise)
    monkeypatch.setattr(run_demo, 'threading', SimpleNamespace(
        Thread=lambda **kwargs: SimpleNamespace(start=lambda: None, join=lambda **kw: None)))
    from edge.model import yolo_detector
    from edge.detection import camera, mog2
    detector = SimpleNamespace(class_names=('Snack',), detect=lambda frame: [Detection('Snack', .95)])
    monkeypatch.setattr(yolo_detector, 'YOLODetector', lambda **kw: detector)
    monkeypatch.setattr(camera, 'open_camera', lambda index: SimpleNamespace(release=lambda: None))
    monkeypatch.setattr(mog2, 'MOG2Processor', lambda: object())

    def run(check, *, supervised=True):
        def serve(app, **kwargs):
            assert firebase._initialized
            events.append('serve')
            check(app.test_client(), supervisor)
        import waitress
        monkeypatch.setattr(waitress, 'serve', serve)
        args = ['--db-path', str(tmp_path / 'demo.db'), '--prepare-db']
        if supervised:
            args.append('--with-supervisor')
        assert run_demo.main(args) == 0

    return SimpleNamespace(root=root, events=events, run=run)


def pair(client):
    response = client.post('/pair-cart', json={'cart_id': 'CART-001'},
                           headers={'Authorization': 'Bearer owner'})
    assert response.status_code == 200
    return response.json['session_id']


def dispatch(runtime, delta):
    weight = runtime.weights.inject_delta(delta)
    now = datetime.now()
    return runtime.dispatch({'confirmed': {
        'confirmed': True, 'event_type': weight.event_type,
        'weight_change': weight.weight_change,
        'movement_timestamp': now, 'weight_timestamp': weight.timestamp,
    }, 'frame': np.zeros((2, 2, 3), dtype=np.uint8)})


@pytest.mark.parametrize('supervised', [False, True])
def test_demo_initializes_before_serving_and_retains_auth(harness, supervised):
    def check(client, supervisor):
        assert client.post('/pair-cart', json={'cart_id': 'CART-001'}).status_code == 401
        assert client.post('/pair-cart', json={'cart_id': 'CART-001'},
                           headers={'Authorization': 'Bearer invalid'}).status_code == 403
        pair(client)
    harness.run(check, supervised=supervised)
    assert harness.events.index('initialize') < harness.events.index('prepare')
    assert harness.events.index('initialize') < harness.events.index('serve')
    assert harness.events.index('serve') < harness.events.index('authenticate')


@pytest.mark.parametrize('failure', ['missing_url', 'missing_credentials', 'invalid_credentials', 'invalid_reference'])
def test_configuration_failure_stops_before_side_effects(monkeypatch, tmp_path, caplog, failure):
    monkeypatch.delenv('SMARTCART_FIREBASE_URL', raising=False)
    monkeypatch.delenv('GOOGLE_APPLICATION_CREDENTIALS', raising=False)
    if failure == 'missing_credentials':
        monkeypatch.setenv('SMARTCART_FIREBASE_URL', 'https://offline.invalid')
    secret = 'private-credential-detail-must-not-be-logged'

    def fail(*args):
        raise ValueError(secret)

    if failure == 'invalid_credentials':
        monkeypatch.setattr(firebase, 'initialize', fail)
    elif failure == 'invalid_reference':
        monkeypatch.setattr(firebase, 'initialize', lambda: None)
        monkeypatch.setattr(firebase, 'get_reference', fail)
    prepare = Mock(side_effect=AssertionError('Must not prepare DB'))
    monkeypatch.setattr(run_demo, 'prepare_demo_database', prepare)
    monkeypatch.setattr(run_demo, 'create_demo_app', Mock(side_effect=AssertionError('Must not build app')))
    import waitress
    serve = Mock(side_effect=AssertionError('Must not serve'))
    monkeypatch.setattr(waitress, 'serve', serve)
    path = tmp_path / 'not-created.db'
    assert run_demo.main(['--db-path', str(path), '--with-supervisor']) == 1
    assert not path.exists()
    prepare.assert_not_called()
    serve.assert_not_called()
    assert 'SMARTCART_FIREBASE_URL' in caplog.text
    assert 'GOOGLE_APPLICATION_CREDENTIALS' in caplog.text
    assert secret not in caplog.text


def test_supervised_runtime_projects_simulated_add_remove(harness):
    def check(client, supervisor):
        sid = pair(client)
        runtime = supervisor.factory(sid)
        assert runtime.session._firebase_ref is harness.root
        assert dispatch(runtime, 50).status == 'committed'
        item = harness.root.child(f'sessions/{sid}/bill_items/1')
        assert item.last_set_data['quantity'] == 1
        assert harness.root.child(f'sessions/{sid}').last_update_data['total_amount'] == 20
        assert harness.root.child(f'sessions/{sid}/event_log').last_push_data['event_type'] == 'ADD'
        assert dispatch(runtime, -50).status == 'committed'
        assert item.deleted
        assert harness.root.child(f'sessions/{sid}').last_update_data['total_amount'] == 0
        assert harness.root.child(f'sessions/{sid}/event_log').last_push_data['event_type'] == 'REMOVE'
        assert billing.get_bill(sid) == []
    harness.run(check)


@pytest.mark.parametrize('delta', [50, -50])
def test_runtime_sync_failure_preserves_committed_bill(harness, monkeypatch, delta):
    def check(client, supervisor):
        sid = pair(client)
        runtime = supervisor.factory(sid)
        if delta < 0:
            assert dispatch(runtime, 50).status == 'committed'
        node = harness.root.child(f'sessions/{sid}')
        monkeypatch.setattr(node, 'update', Mock(side_effect=OSError('offline')))
        assert dispatch(runtime, delta).status == 'committed_sync_failed'
        assert runtime.session.last_mutation_committed
        assert billing.get_session_meta(sid)['total_amount'] == (20 if delta > 0 else 0)
        assert billing.get_session_log(sid)[-1][3] == ('ADD' if delta > 0 else 'REMOVE')
    harness.run(check)


@pytest.mark.parametrize('sync_fails', [False, True])
def test_demo_payment_verifies_and_reconciles_initialized_root(harness, monkeypatch, sync_fails):
    submit = Mock(wraps=PaymentService.submit_evidence)
    monkeypatch.setattr(PaymentService, 'submit_evidence', lambda self, *args: submit(self, *args))

    def check(client, supervisor):
        sid = pair(client)
        runtime = supervisor.factory(sid)
        assert dispatch(runtime, 50).status == 'committed'
        headers = {'Authorization': 'Bearer owner', 'Idempotency-Key': 'checkout'}
        frozen = client.post(f'/sessions/{sid}/checkout', json={}, headers=headers)
        cid = frozen.json['checkout_id']
        payment = client.post(f'/sessions/{sid}/checkouts/{cid}/payment', json={}, headers=headers)
        assert payment.json['state'] == 'ORDER_READY'
        route = f'/demo/sessions/{sid}/checkouts/{cid}/simulate-payment-success'
        assert client.post(route).status_code == 401
        assert client.post(route, headers={'Authorization': 'Bearer invalid'}).status_code == 403
        assert client.post(route, headers={'Authorization': 'Bearer other'}).status_code == 403
        submit.assert_not_called()
        if sync_fails:
            monkeypatch.setattr(harness.root.child(f'sessions/{sid}'), 'update',
                                Mock(side_effect=OSError('offline')))
        response = client.post(route, headers=headers)
        assert response.status_code == 200
        assert response.json['state'] == 'COMPLETED'
        assert response.json['projection_sync'] == ('reconciliation_required' if sync_fails else 'succeeded')
        assert submit.call_count == 1
        meta = billing.get_session_meta(sid)
        assert meta['end_time'] is not None
        if not sync_fails:
            projection = harness.root.child(f'sessions/{sid}').last_update_data
            assert projection['end_time'] == meta['end_time']
            assert projection['bill_items']['1']['quantity'] == 1
        state = client.get(f'/sessions/{sid}/checkouts/{cid}/payment', headers=headers).json
        assert state['state'] == 'COMPLETED'
        assert state['verified_at'] is not None
    harness.run(check)


def test_normal_startup_still_disables_payments(harness, monkeypatch):
    from edge.payment.fake import FakeProvider
    monkeypatch.setattr(FakeProvider, '__init__', Mock(side_effect=AssertionError('No normal fake provider')))

    def serve(app, **kwargs):
        assert firebase._initialized
        client = app.test_client()
        response = client.post('/sessions/1/checkouts/1/payment', json={}, headers={
            'Authorization': 'Bearer owner', 'Idempotency-Key': 'key'})
        assert response.status_code == 503
        assert response.json['error'] == 'payment_unavailable'
        assert client.post('/demo/sessions/1/checkouts/1/simulate-payment-success').status_code == 404

    import waitress
    monkeypatch.setattr(waitress, 'serve', serve)
    run_server.main()
