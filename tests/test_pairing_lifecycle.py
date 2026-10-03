"""Offline lifecycle coverage: real SQLite transactions and fake Firebase only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import sqlite3
from threading import Barrier
from types import SimpleNamespace

import pytest

from edge.api.server import create_app
from edge.billing import billing
from edge.database import database
from edge.database.product_repository import add_product
from edge.detection.product_resolver import Detection
from edge.firebase import client as firebase_client
from edge.pairing import session_store
from edge.pairing.pair_cart import PairingError, pair_customer_to_cart
from edge.runtime.cart_runtime import resume_session
from tests.test_runtime_firebase_sync import _FakeReference


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_path / 'lifecycle.db')
    monkeypatch.delenv('SMARTCART_ALLOWED_CARTS', raising=False)
    database.initialize_database()


@pytest.fixture
def firebase(monkeypatch):
    root = _FakeReference()
    monkeypatch.setattr(firebase_client, 'get_reference', lambda path: root)
    return root


def pair_http(uid='uid-a', cart='CART-001', **body):
    app = create_app(token_verifier=lambda token: {'uid': uid})
    return app.test_client().post('/pair-cart', json={'cart_id': cart, **body},
                                  headers={'Authorization': 'Bearer offline-token'})


def session_rows():
    with closing(database.get_connection()) as conn:
        return conn.execute('SELECT * FROM sessions ORDER BY session_id').fetchall()


def snapshot(sid):
    return (billing.get_session_meta(sid), billing.get_bill(sid), billing.get_session_log(sid))


def firebase_snapshot(root, sid):
    return deepcopy({path: vars(node) for path, node in root._children.items()
                     if path == f'sessions/{sid}' or path.startswith(f'sessions/{sid}/')})


def resume(root):
    return resume_session('CART-001', SimpleNamespace(class_names=('Snack',)), root)


def test_available_cart_creates_owned_empty_session_from_verified_uid(firebase):
    response = pair_http(customer_uid='uid-impostor')
    assert response.status_code == 200
    result = response.get_json()
    assert result == {'session_id': result['session_id'], 'cart_id': 'CART-001', 'status': 'paired'}
    sid = result['session_id']
    meta = billing.get_session_meta(sid)
    assert meta == dict(session_id=sid, cart_id='CART-001', customer_uid='uid-a',
                        start_time=meta['start_time'], end_time=None, total_amount=0)
    assert meta['start_time']
    assert billing.get_bill(sid) == billing.get_session_log(sid) == []
    assert len(session_rows()) == 1
    node = firebase.node(f'sessions/{sid}')
    assert node.last_update_data == {key: value for key, value in meta.items() if key != 'session_id'}
    assert len(node._update_calls) == 1
    assert firebase.node(f'sessions/{sid}/bill_items') is None


def test_reconnect_preserves_nonempty_bill_history_and_firebase(firebase):
    add_product('Snack', 'snack', 10, 75, 'Snack')
    sid = pair_http().get_json()['session_id']
    session = resume(firebase)
    assert session.process_add([Detection('Snack', .99)], 75).success
    before, cloud_before = snapshot(sid), firebase_snapshot(firebase, sid)
    response = pair_http()
    assert response.status_code == 200
    assert response.get_json() == {'session_id': sid, 'cart_id': 'CART-001', 'status': 'already_paired'}
    assert snapshot(sid) == before
    assert firebase_snapshot(firebase, sid) == cloud_before
    assert len(session_rows()) == 1


def test_occupied_cart_does_not_create_or_reassign(firebase):
    sid = pair_http().get_json()['session_id']
    before = session_rows()
    response = pair_http('uid-b')
    assert response.status_code == 409
    assert response.get_json()['error'] == 'cart_already_assigned'
    assert session_rows() == before
    assert len(firebase.node(f'sessions/{sid}')._update_calls) == 1


def test_customer_on_another_cart_does_not_create_orphan(firebase, monkeypatch):
    monkeypatch.setenv('SMARTCART_ALLOWED_CARTS', 'CART-001,CART-002')
    assert pair_http(cart='CART-002').status_code == 200
    before = session_rows()
    response = pair_http()
    assert response.status_code == 409
    assert response.get_json()['error'] == 'customer_already_has_active_session'
    assert session_rows() == before
    assert session_store.get_active_session_for_cart('CART-001') is None


@pytest.mark.parametrize('cart', ['CART-002', 'arbitrary', 'cart-001'])
def test_default_configuration_rejects_other_carts(firebase, cart):
    response = pair_http(cart=cart)
    assert response.status_code == 404
    assert response.get_json()['error'] == 'cart_not_allowed'
    assert session_rows() == []
    assert firebase._children == {}


@pytest.mark.parametrize('config', ['', ' , , ', 'CART-002'])
def test_explicit_configuration_never_falls_back_to_default(firebase, monkeypatch, config):
    monkeypatch.setenv('SMARTCART_ALLOWED_CARTS', config)
    assert pair_http().get_json()['error'] == 'cart_not_allowed'
    assert session_rows() == []


def test_configuration_and_request_whitespace_are_trimmed(firebase, monkeypatch):
    monkeypatch.setenv('SMARTCART_ALLOWED_CARTS', ' CART-001, CART-002 , , CART-002, CART-003 ')
    for number in (1, 2, 3):
        response = pair_http(f'uid-{number}', f' CART-00{number} ')
        assert response.status_code == 200
        assert response.get_json()['cart_id'] == f'CART-00{number}'
    assert len(session_rows()) == 3


def test_precreated_unowned_session_still_assignable(firebase):
    sid = billing.create_session('CART-001')
    response = pair_http()
    assert response.status_code == 200
    assert response.get_json()['session_id'] == sid
    assert response.get_json()['status'] == 'paired'
    assert billing.get_session_meta(sid)['customer_uid'] == 'uid-a'
    assert len(session_rows()) == 1


@pytest.mark.parametrize('mode', ['same-cart', 'same-customer', 'same-request'])
def test_simultaneous_pairing_creates_exactly_one_session(firebase, monkeypatch, mode):
    monkeypatch.setenv('SMARTCART_ALLOWED_CARTS', 'CART-001,CART-002')
    requests = {
        'same-cart': [('uid-a', 'CART-001'), ('uid-b', 'CART-001')],
        'same-customer': [('uid-a', 'CART-001'), ('uid-a', 'CART-002')],
        'same-request': [('uid-a', 'CART-001'), ('uid-a', 'CART-001')],
    }[mode]
    barrier = Barrier(2)

    def worker(request):
        barrier.wait(timeout=5)
        try:
            result = pair_customer_to_cart(*request)
            return result.status, result.session_id
        except PairingError as error:
            return error.code, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, request) for request in requests]
        outcomes = [future.result(timeout=10) for future in futures]
    expected = {'same-cart': 'cart_already_assigned',
                'same-customer': 'customer_already_has_active_session',
                'same-request': 'already_paired'}[mode]
    assert sorted(status for status, sid in outcomes) == sorted(['paired', expected])
    rows = session_rows()
    assert len(rows) == 1
    sid = rows[0][0]
    assert all(value == sid for status, value in outcomes if value is not None)
    assert rows[0][2] is None  # end_time
    assert len(firebase.node(f'sessions/{sid}')._update_calls) == 1


def test_creation_failure_rolls_back_insert_and_never_syncs(firebase, monkeypatch):
    original = session_store.create_session

    def insert_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        assert kwargs['_conn'].in_transaction
        raise sqlite3.OperationalError('injected failure after creation')

    monkeypatch.setattr(session_store, 'create_session', insert_then_fail)
    response = pair_http()
    assert response.status_code == 500
    assert response.get_json()['error'] == 'internal_error'
    assert session_rows() == []
    assert firebase._children == {}
    monkeypatch.setattr(session_store, 'create_session', original)
    assert pair_http().status_code == 200  # Writer lock was also released.
    assert len(session_rows()) == 1


def test_creation_sync_observes_commit_and_released_writer():
    calls = []

    def sync(sid, uid, cart):
        with closing(database.get_connection()) as conn:
            conn.execute('PRAGMA busy_timeout=0')
            conn.execute('BEGIN IMMEDIATE')
            assert conn.execute('SELECT customer_uid FROM sessions WHERE session_id=?',
                                (sid,)).fetchone() == (uid,)
            conn.rollback()
        calls.append((sid, uid, cart))

    result = pair_customer_to_cart('uid-a', 'CART-001', _firebase_sync_fn=sync)
    assert calls == [(result.session_id, 'uid-a', 'CART-001')]


def test_firebase_failure_keeps_created_session_and_retry_does_not_duplicate(firebase, monkeypatch):
    calls = []

    def fail_update(data):
        calls.append(data)
        raise OSError('offline Firebase')

    monkeypatch.setattr(_FakeReference, 'update', lambda self, data: fail_update(data))
    response = pair_http()
    assert response.status_code == 200
    sid = response.get_json()['session_id']
    assert billing.get_session_meta(sid)['customer_uid'] == 'uid-a'
    assert pair_http().get_json() == {'session_id': sid, 'cart_id': 'CART-001', 'status': 'already_paired'}
    assert len(session_rows()) == len(calls) == 1  # Existing no-repair-on-reconnect policy.


def test_completion_reuse_preserves_old_sqlite_and_firebase_history(firebase):
    add_product('Snack', 'snack', 10, 75, 'Snack')
    old_sid = pair_http().get_json()['session_id']
    old_session = resume(firebase)
    assert old_session.session_id == old_sid
    assert old_session.process_add([Detection('Snack', .99)], 75).success
    assert old_session.complete() == 10
    before, cloud_before = snapshot(old_sid), firebase_snapshot(firebase, old_sid)
    assert before[0]['end_time'] is not None
    assert before[1] and before[2]

    response = pair_http('uid-b')
    assert response.status_code == 200
    new_sid = response.get_json()['session_id']
    assert new_sid != old_sid
    meta = billing.get_session_meta(new_sid)
    assert meta['customer_uid'] == 'uid-b'
    assert meta['cart_id'] == 'CART-001'
    assert meta['total_amount'] == 0 and meta['end_time'] is None
    new_session = resume(firebase)
    assert new_session.session_id == new_sid
    assert new_session.cart_state.snapshot() == new_session.get_bill() == []
    assert billing.get_session_log(new_sid) == []
    assert firebase.node(f'sessions/{new_sid}').last_update_data['total_amount'] == 0
    assert firebase.node(f'sessions/{new_sid}/bill_items') is None
    assert snapshot(old_sid) == before
    assert firebase_snapshot(firebase, old_sid) == cloud_before
    assert len(session_rows()) == 2
    # The resumed new session can use the unchanged ADD/REMOVE path independently.
    assert new_session.process_add([Detection('Snack', .99)], 75).success
    assert new_session.process_remove([], -75).success
    assert new_session.get_bill() == []
    assert snapshot(old_sid) == before
    assert firebase_snapshot(firebase, old_sid) == cloud_before


@pytest.mark.parametrize('finish', ['commit', 'rollback'])
def test_canonical_creation_leaves_caller_transaction_owned_by_caller(monkeypatch, finish):
    with closing(database.get_connection()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        statements = []
        conn.set_trace_callback(statements.append)
        monkeypatch.setattr(billing, 'get_connection', lambda: pytest.fail('Competing connection opened'))
        sid = billing.create_session('CART-001', customer_uid='uid-a', _conn=conn)
        assert conn.in_transaction
        assert len(statements) == 1 and statements[0].lstrip().startswith('INSERT')
        assert session_rows() == []  # A separate reader cannot see an uncommitted session.
        getattr(conn, finish)()
        assert conn.execute('SELECT 1').fetchone() == (1,)  # Still open.
    rows = session_rows()
    assert len(rows) == (1 if finish == 'commit' else 0)
    if finish == 'commit':
        assert rows[0][0] == sid
        assert rows[0][4:] == ('CART-001', 'uid-a', None)


def test_canonical_creation_requires_active_injected_transaction():
    with closing(database.get_connection()) as conn:
        with pytest.raises(ValueError, match='active caller transaction'):
            billing.create_session('CART-001', _conn=conn)
        assert not conn.in_transaction
        assert conn.execute('SELECT COUNT(*) FROM sessions').fetchone() == (0,)


def test_pairing_creation_commits_once_on_injected_connection():
    with closing(database.get_connection()) as conn:
        statements = []
        conn.set_trace_callback(statements.append)
        result = session_store.pair_and_assign_in_transaction('CART-001', 'uid-a', _conn=conn)
        assert statements[0] == 'BEGIN IMMEDIATE'
        assert statements[-1] == 'COMMIT'
        assert statements.count('COMMIT') == 1
        assert not conn.in_transaction
        assert conn.execute('SELECT customer_uid FROM sessions WHERE session_id=?',
                            (result.session_id,)).fetchone() == ('uid-a',)


def test_duplicate_customer_sessions_block_creation_on_available_cart(firebase):
    billing.create_session('CART-OLD-A', customer_uid='uid-a')
    billing.create_session('CART-OLD-B', customer_uid='uid-a')
    before = session_rows()
    response = pair_http()
    assert response.status_code == 409
    assert response.get_json()['error'] == 'multiple_active_sessions_for_customer'
    assert session_rows() == before
    assert firebase._children == {}
