"""Real SQLite checkout transactions, offline authentication and runtime I/O."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
import sqlite3
from threading import Event, Barrier

import pytest

from edge.api.server import create_app
from edge.billing import billing, checkout
from edge.database import database
from edge.database.product_repository import add_product, get_all_products
from edge.firebase import client
from edge.pairing.session_store import pair_and_assign_in_transaction, IntegrityConflict
from tests.test_cart_runtime import runtime, acquire, Detector
from edge.runtime.cart_runtime import resume_session, StartupError


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_path / 'checkout.db')
    monkeypatch.setenv('SMARTCART_ALLOWED_CARTS', 'CART-A,CART-B')
    database.initialize_database()
    monkeypatch.setattr(client, 'initialize', lambda: pytest.fail('Live Firebase'))
    monkeypatch.setattr(client, 'get_reference', lambda *a: pytest.fail('Live Firebase'))


@pytest.fixture
def populated():
    add_product('Snack', 'sku', 10, 75, 'Snack')
    pid = get_all_products()[0][0]
    sid = billing.create_session('CART-A', customer_uid='owner')
    billing.add_item(sid, pid)
    billing.add_item(sid, pid)
    return sid, pid


def sql(statement, args=()):
    with closing(database.get_connection()) as conn:
        result = conn.execute(statement, args).fetchall()
        conn.commit()
        return result


def state():
    with closing(database.get_connection()) as conn:
        return tuple(conn.iterdump())


def api(uid='owner'):
    def verify(token):
        if token != 'valid':
            raise ValueError('invalid token')
        return {'uid': uid}
    return create_app(token_verifier=verify).test_client()


def post(client, sid, body=None, token='valid'):
    return client.post(f'/sessions/{sid}/checkout', json={} if body is None else body,
                       headers={'Authorization': f'Bearer {token}', 'Idempotency-Key': 'request-1'})


def test_fresh_schema_and_repeat_preserves_checkout(populated):
    sid, _ = populated
    columns = {r[1]: r for r in sql('PRAGMA table_info(sessions)')}
    assert columns['checkout_started_at'][2:4] == ('TEXT', 0)
    result = checkout.begin_checkout(sid, 'owner', 'request-1')
    before = state()
    database.initialize_database()
    database.initialize_database()
    assert state() == before
    assert checkout.begin_checkout(sid, 'owner', 'request-1') == result


def test_old_schema_migration_preserves_active_and_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_path / 'old.db')
    sql('CREATE TABLE sessions (session_id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'start_time TEXT NOT NULL, end_time TEXT, total_amount REAL DEFAULT 0, '
        'cart_id TEXT, customer_uid TEXT)')
    sql("INSERT INTO sessions VALUES (1, '2026-01-01 00:00:00', NULL, 0, 'A', 'u')")
    sql("INSERT INTO sessions VALUES (2, '2026-01-01 00:00:00', '2026-01-02 00:00:00', 10, 'B', 'v')")
    before = sql('SELECT * FROM sessions')
    database.initialize_database()
    assert sql('SELECT * FROM sessions') == [row + (None,) for row in before]
    database.initialize_database()
    assert sql('SELECT * FROM sessions') == [row + (None,) for row in before]


def test_migration_failure_propagates_and_closes_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_path / 'migration-failure.db')
    sql('CREATE TABLE sessions (session_id INTEGER PRIMARY KEY, start_time TEXT, '
        'end_time TEXT, total_amount REAL, cart_id TEXT, customer_uid TEXT)')
    connection = database.get_connection()
    def authorize(action, first, second, db, trigger):
        if action == sqlite3.SQLITE_ALTER_TABLE and second == 'sessions':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    connection.set_authorizer(authorize)
    monkeypatch.setattr(database, 'get_connection', lambda: connection)
    with pytest.raises(sqlite3.DatabaseError):
        database.initialize_database()
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')


def test_frozen_projection_does_not_publish_or_erase_checkout(populated):
    from tests.test_session_reconciliation import StatefulReference
    from edge.firebase.reconcile import reconcile_session_to_firebase
    sid, _ = populated
    result = checkout.begin_checkout(sid, 'owner', 'request-1')
    root = StatefulReference({'sessions': {str(sid): {'event_log': {'old': 'event'}}}})
    before = state()
    reconcile_session_to_firebase(sid, _ref=root)
    assert 'checkout_started_at' not in root.data['sessions'][str(sid)]
    assert root.data['sessions'][str(sid)]['event_log'] == {'old': 'event'}
    assert checkout.begin_checkout(sid, 'owner', 'request-1') == result
    assert state() == before


@pytest.mark.parametrize('change,status,code', [
    ('DELETE FROM sessions', 404, 'session_not_found'),
    ('DELETE FROM bill_items', 409, 'empty_cart'),
    ('UPDATE sessions SET total_amount = 99', 409, 'invalid_bill'),
])
def test_http_source_errors(populated, change, status, code):
    sid, _ = populated
    sql(change)
    response = post(api(), sid)
    assert response.status_code == status
    assert response.json['error'] == code


def test_freeze_stored_amount_no_completion_or_ownership_change(populated):
    sid, pid = populated
    sql('UPDATE products SET price = 999 WHERE product_id = ?', (pid,))
    before_bill, before_log = billing.get_bill(sid), billing.get_session_log(sid)
    result = checkout.begin_checkout(sid, 'owner', 'request-1')
    assert asdict(result) == dict(session_id=sid, cart_id='CART-A',
                                 checkout_started_at=result.checkout_started_at,
                                 checkout_id=result.checkout_id, state='FROZEN',
                                 cancelled_at=None, runtime_restart_required=False,
                                 final_amount=20, currency='INR')
    assert sql('SELECT checkout_started_at, end_time, customer_uid, cart_id, total_amount FROM sessions') == [
        (result.checkout_started_at, None, 'owner', 'CART-A', 20)]
    assert billing.get_bill(sid) == before_bill
    assert billing.get_session_log(sid) == before_log
    before = state()
    assert checkout.begin_checkout(sid, 'owner', 'request-1') == result
    assert state() == before


def test_empty_cart_never_freezes_or_releases():
    sid = billing.create_session('CART-A', customer_uid='owner')
    before = state()
    with pytest.raises(checkout.CheckoutError, match='empty'):
        checkout.begin_checkout(sid, 'owner', 'request-1')
    assert state() == before


@pytest.mark.parametrize('statement', [
    'UPDATE bill_items SET price = -1', 'UPDATE bill_items SET total = -1',
    'UPDATE bill_items SET price = 1e999', 'UPDATE bill_items SET total = 1e999',
    'UPDATE sessions SET total_amount = 1e999', 'UPDATE sessions SET total_amount = -1',
    'UPDATE sessions SET total_amount = NULL', 'UPDATE sessions SET total_amount = 21',
    'UPDATE bill_items SET total = 21', 'UPDATE bill_items SET quantity = 0',
    'UPDATE bill_items SET quantity = 1.5', 'DELETE FROM products',
    "UPDATE products SET name = ''",
    'INSERT INTO bill_items(session_id, product_id, quantity, price, total) '
    'SELECT session_id, product_id, quantity, price, total FROM bill_items',
])
@pytest.mark.parametrize('frozen', [False, True])
def test_invalid_bill_rejected_without_writes(populated, statement, frozen):
    sid, _ = populated
    if frozen:
        checkout.begin_checkout(sid, 'owner', 'request-1')
    sql(statement)
    before = state()
    with pytest.raises(checkout.CheckoutError) as error:
        checkout.begin_checkout(sid, 'owner', 'request-1')
    assert error.value.code == 'invalid_bill'
    assert state() == before


@pytest.mark.parametrize('sid', [0, -1, True, None, '1', 2**63])
def test_invalid_direct_ids(sid):
    with pytest.raises(checkout.CheckoutError) as error:
        checkout.begin_checkout(sid, 'owner', 'request-1')
    assert error.value.http_status == 400


@pytest.mark.parametrize('change,code', [
    ("UPDATE sessions SET customer_uid = NULL", 'session_forbidden'),
    ("UPDATE sessions SET customer_uid = 'other'", 'session_forbidden'),
    ("UPDATE sessions SET cart_id = NULL", 'invalid_session'),
    ("UPDATE sessions SET cart_id = ''", 'invalid_session'),
    ("UPDATE sessions SET end_time = datetime('now')", 'session_completed'),
    ("UPDATE sessions SET checkout_started_at = 'invalid'", 'invalid_session'),
    ('DELETE FROM sessions', 'session_not_found'),
])
def test_invalid_session_no_writes(populated, change, code):
    sid, _ = populated
    sql(change)
    before = state()
    with pytest.raises(checkout.CheckoutError) as error:
        checkout.begin_checkout(sid, 'owner', 'request-1')
    assert error.value.code == code
    assert state() == before


@pytest.mark.parametrize('cart,owner', [('CART-A', 'other'), ('CART-B', 'owner')])
def test_duplicate_active_ownership_rejected(populated, cart, owner):
    sid, _ = populated
    billing.create_session(cart, customer_uid=owner)
    before = state()
    with pytest.raises(checkout.CheckoutError) as error:
        checkout.begin_checkout(sid, 'owner', 'request-1')
    assert error.value.code == 'session_conflict'
    assert state() == before


@pytest.mark.parametrize('field', ['customer_uid', 'cart_id', 'amount', 'total', 'currency',
                                  'payment_status', 'payment_success', 'provider_result', 'other'])
def test_http_forbids_fields(populated, field):
    sid, _ = populated
    before = state()
    assert post(api(), sid, {field: 'spoof'}).status_code == 400
    assert state() == before


@pytest.mark.parametrize('sid', ['0', '-1', 'abc', '1.0', '9223372036854775808', '١'])
def test_http_invalid_ids(sid):
    assert post(api(), sid).status_code == 400


def test_http_auth_owner_success_and_lost_response_retry(populated):
    sid, _ = populated
    client = api()
    assert client.post(f'/sessions/{sid}/checkout', json={}).status_code == 401
    assert post(client, sid, token='').status_code == 401
    assert post(client, sid, token='bad').status_code == 403
    assert post(api('other'), sid).status_code == 403
    first = post(client, sid)
    assert first.status_code == 200
    before = state()
    # Recreate the application: all state must come from persisted SQLite.
    assert post(api(), sid).json == first.json
    assert state() == before
    assert set(first.json) == {'session_id', 'cart_id', 'checkout_started_at', 'final_amount', 'currency',
                               'checkout_id', 'state', 'cancelled_at', 'runtime_restart_required'}


@pytest.mark.parametrize('body', ['[]', 'null', '{', '"text"'])
def test_http_malformed_body(populated, body):
    sid, _ = populated
    response = api().post(f'/sessions/{sid}/checkout', data=body,
                          content_type='application/json', headers={'Authorization': 'Bearer valid'})
    assert response.status_code == 400


def test_concurrent_duplicate_checkout(populated):
    sid, _ = populated
    barrier = Barrier(2)
    def freeze():
        barrier.wait(timeout=5)
        return checkout.begin_checkout(sid, 'owner', 'request-1')
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(freeze), pool.submit(freeze)
        assert a.result(timeout=10) == b.result(timeout=10)
    assert billing.calculate_total(sid) == 20


@pytest.mark.parametrize('operation,expected', [('add_item', 30), ('remove_item', 10)])
@pytest.mark.parametrize('first', ['mutation', 'checkout'])
def test_writer_race_both_orderings(populated, monkeypatch, operation, expected, first):
    sid, pid = populated
    entered, release, competing = Event(), Event(), Event()
    module = billing if first == 'mutation' else checkout
    original = module.get_connection
    def held_connection():
        conn = original()
        def trace(statement):
            if statement == 'COMMIT':
                entered.set()
                assert release.wait(5)
        conn.set_trace_callback(trace)
        return conn
    monkeypatch.setattr(module, 'get_connection', held_connection)
    mutate = lambda: getattr(billing, operation)(sid, pid)
    freeze = lambda: checkout.begin_checkout(sid, 'owner', 'request-1')
    def second_action():
        competing.set()
        return (freeze if first == 'mutation' else mutate)()
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(mutate if first == 'mutation' else freeze)
        assert entered.wait(5)
        b = pool.submit(second_action)
        assert competing.wait(5)
        release.set()
        a.result(timeout=10)
        if first == 'checkout':
            with pytest.raises(billing.CheckoutFrozenError):
                b.result(timeout=10)
            expected = 20
        else:
            assert b.result(timeout=10).final_amount == expected
    assert billing.calculate_total(sid) == expected


def test_rollback_and_lock_failure_never_success(populated, monkeypatch):
    sid, _ = populated
    sql("CREATE TRIGGER reject_freeze BEFORE UPDATE OF checkout_started_at ON sessions "
        "BEGIN SELECT RAISE(ABORT, 'private failure'); END")
    before = state()
    response = post(api(), sid)
    assert response.status_code == 503
    assert 'private' not in response.get_data(as_text=True)
    assert state() == before
    sql('DROP TRIGGER reject_freeze')
    original = checkout.get_connection
    def impatient():
        conn = original()
        conn.execute('PRAGMA busy_timeout=0')
        return conn
    monkeypatch.setattr(checkout, 'get_connection', impatient)
    with closing(database.get_connection()) as writer:
        writer.execute('BEGIN IMMEDIATE')
        assert post(api(), sid).status_code == 503
        writer.rollback()
    assert sql('SELECT checkout_started_at FROM sessions') == [(None,)]


@pytest.mark.parametrize('kind', ['ADD', 'REMOVE'])
def test_inflight_event_freeze_restores_memory_and_halts_queue(populated, monkeypatch, kind):
    sid, _ = populated
    rt = runtime()
    before = rt.session.cart_state.snapshot()
    acquire(rt, 75 if kind == 'ADD' else -75)
    acquire(rt, 75, offset=2)
    # Freeze after resolution, immediately before the authoritative billing call.
    import edge.cart_session as module
    attr = '_billing_add' if kind == 'ADD' else '_billing_remove'
    original = getattr(module, attr)
    def freeze_first(*args, **kwargs):
        checkout.begin_checkout(sid, 'owner', 'request-1')
        return original(*args, **kwargs)
    monkeypatch.setattr(module, attr, freeze_first)
    outcomes = []
    rt.on_outcome = outcomes.append
    rt._work()
    assert rt.failed.is_set() and rt.stopping.is_set() and rt.jobs.empty()
    assert len(outcomes) == 1 and 'frozen' in outcomes[0].detail
    assert rt.session.cart_state.snapshot() == before
    assert billing.calculate_total(sid) == 20
    assert len(billing.get_session_log(sid)) == 2


def test_frozen_startup_pairing_ownership_completion_history(populated):
    sid, pid = populated
    shopping_runtime = runtime()
    result = checkout.begin_checkout(sid, 'owner', 'request-1')
    with pytest.raises(StartupError, match='frozen'):
        resume_session('CART-A', Detector(), None)
    assert pair_and_assign_in_transaction('CART-A', 'owner').session_id == sid
    for cart, owner in [('CART-A', 'other'), ('CART-B', 'owner')]:
        with pytest.raises(IntegrityConflict):
            pair_and_assign_in_transaction(cart, owner)
    for mutation, value in [(billing.set_session_cart_id, 'CART-B'),
                            (billing.assign_customer_to_session, 'other')]:
        with pytest.raises(billing.CheckoutFrozenError):
            mutation(sid, value)
    assert shopping_runtime.session.complete() == 20
    assert sql('SELECT checkout_started_at FROM sessions WHERE session_id = ?', (sid,)) == [
        (result.checkout_started_at,)]
    new = pair_and_assign_in_transaction('CART-A', 'owner')
    assert new.session_id != sid
    assert post(api(), sid).status_code == 409
    assert sql('SELECT checkout_started_at FROM sessions WHERE session_id = ?', (new.session_id,)) == [(None,)]
    assert billing.get_bill(sid)[0][2] == 2
