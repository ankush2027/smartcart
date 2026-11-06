"""Payment schema/migration invariants and authenticated response boundaries."""
from contextlib import closing
import sqlite3

import pytest

from edge.api.server import create_app
from edge.billing import billing, checkout
from edge.database import database
from edge.payment import store
from edge.payment.fake import FakeProvider
from edge.payment.money import PaymentError
from tests.test_checkout import isolated, populated, sql, state
from tests.test_payment import payment, reserve, start, verify, offline


def test_schema_repeat_preserves_historical_rows(payment):
    sid, cid, provider, service = payment
    checkout.cancel_checkout(sid, cid, 'owner')
    second = checkout.begin_checkout(sid, 'owner', 'second')
    billing.complete_session(sid)
    before = state()
    database.initialize_database()
    database.initialize_database()
    assert state() == before
    assert sql('SELECT amount_paise FROM checkouts') == [(None,), (None,)]


@pytest.mark.parametrize('phase', ['RESERVED', 'CREATING', 'ORDER_READY', 'VERIFIED', 'COMPLETED', 'FAILED'])
def test_restart_preserves_each_payment_state(payment, phase):
    if phase in ('RESERVED', 'CREATING'):
        attempt = reserve(payment)
        if phase == 'CREATING':
            store.claim_dispatch(attempt['payment_attempt_id'])
    elif phase == 'FAILED':
        payment[2].creation = 'reject'
        start(payment)
    elif phase == 'ORDER_READY':
        start(payment)
    else:
        attempt = verify(payment)
        if phase == 'COMPLETED':
            payment[3].complete(payment[1], attempt['payment_attempt_id'])
    before = state()
    database.initialize_database()
    assert state() == before
    assert sql('SELECT state FROM payment_attempts') == [(phase,)]


def test_payment_migration_atomic_from_previous_schema(payment, monkeypatch):
    # Construct the actual previous checkout schema using its existing migration.
    with closing(database.get_connection()) as conn:
        conn.execute('DROP TABLE payment_attempts')
        conn.execute('DROP TABLE checkouts')
        conn.execute('BEGIN IMMEDIATE')
        checkout.migrate_checkouts(conn)
        conn.commit()
    before = state()
    original = store.migrate_payments
    def fail(conn):
        original(conn)
        raise RuntimeError('injected after DDL')
    monkeypatch.setattr(store, 'migrate_payments', fail)
    with pytest.raises(RuntimeError):
        database.initialize_database()
    assert state() == before
    assert 'amount_paise' not in {r[1] for r in sql('PRAGMA table_info(checkouts)')}
    monkeypatch.setattr(store, 'migrate_payments', original)
    database.initialize_database()
    assert sql('SELECT amount_paise FROM checkouts') == [(None,)]
    assert sql('SELECT amount FROM checkouts') == [(20,)]


@pytest.mark.parametrize('column,value', [('payment_attempt_id', 'replacement'), ('checkout_id', 999),
    ('request_key', 'replacement'), ('provider_scope', 'other'), ('amount_paise', 1),
    ('currency', 'USD'), ('created_at', '2020-01-01 00:00:00')])
def test_immutable_attempt_identity(payment, column, value):
    reserve(payment)
    before = state()
    with pytest.raises(sqlite3.IntegrityError):
        sql(f'UPDATE payment_attempts SET {column}=?', (value,))
    assert state() == before


@pytest.mark.parametrize('value', [None, 1, 2000, -1, 2.5])
def test_paise_snapshot_once(payment, value):
    reserve(payment)
    with pytest.raises(sqlite3.IntegrityError):
        sql('UPDATE checkouts SET amount_paise=?', (value,))


def test_no_attempt_deletion_or_second_attempt(payment):
    reserve(payment)
    with pytest.raises(sqlite3.IntegrityError):
        sql('DELETE FROM payment_attempts')
    with pytest.raises(sqlite3.IntegrityError):
        sql('''INSERT INTO payment_attempts SELECT 'another',checkout_id,request_key,provider_scope,
            amount_paise,currency,state,provider_order_id,provider_payment_id,created_at,updated_at,
            verified_at,completed_at,error_code FROM payment_attempts''')
    with pytest.raises(sqlite3.IntegrityError):
        sql('DELETE FROM checkouts')
    assert sql('PRAGMA foreign_key_list(payment_attempts)')[0][2:5] == ('checkouts', 'checkout_id', 'checkout_id')
    assert sql('PRAGMA foreign_keys') == [(1,)]


def test_replace_cannot_erase_payment_history(payment):
    reserve(payment)
    before = state()
    with pytest.raises(sqlite3.IntegrityError):
        sql('''INSERT OR REPLACE INTO payment_attempts SELECT 'replacement',checkout_id,request_key,
            provider_scope,amount_paise,currency,state,provider_order_id,provider_payment_id,
            created_at,updated_at,verified_at,completed_at,error_code FROM payment_attempts''')
    assert state() == before


@pytest.mark.parametrize('assignment', ["state='COMPLETED'", "state='VERIFIED'", "state='FAILED'",
    "provider_order_id='invented'", "provider_payment_id='invented'", "verified_at=datetime('now')",
    "completed_at=datetime('now')", "updated_at='invalid'", "updated_at='2000-01-01 00:00:00'",
    "error_code='raw secret message'"])
def test_schema_rejects_invalid_lifecycle(payment, assignment):
    reserve(payment)
    with pytest.raises(sqlite3.IntegrityError):
        sql('UPDATE payment_attempts SET ' + assignment)


@pytest.mark.parametrize('state_name', ['VERIFIED', 'COMPLETED', 'FAILED'])
def test_terminal_states_no_downgrade(payment, state_name):
    if state_name == 'FAILED':
        payment[2].creation = 'reject'
        attempt = start(payment)
    else:
        attempt = verify(payment)
        if state_name == 'COMPLETED':
            payment[3].complete(payment[1], attempt['payment_attempt_id'])
    before = state()
    with pytest.raises(sqlite3.IntegrityError):
        sql("UPDATE payment_attempts SET state='CREATING',provider_order_id=NULL,provider_payment_id=NULL,verified_at=NULL,completed_at=NULL")
    assert state() == before
    assert store.creation_problem(attempt['payment_attempt_id'], rejected=True)['state'] == state_name


def test_scoped_order_and_payment_uniqueness(payment):
    sid, cid, provider, service = payment
    first = verify(payment)
    first_raw = store.read_payment(sid, cid, 'owner')
    other_sid = billing.create_session('CART-B', customer_uid='other')
    billing.add_item(other_sid, sql('SELECT product_id FROM products')[0][0])
    other_cid = checkout.begin_checkout(other_sid, 'other', 'quote').checkout_id
    other = store.begin_payment(other_sid, other_cid, 'other', 'pay', provider_scope=provider.scope)
    store.claim_dispatch(other['payment_attempt_id'])
    with pytest.raises(sqlite3.IntegrityError):
        sql("UPDATE payment_attempts SET state='ORDER_READY',provider_order_id=? WHERE checkout_id=?",
            (first_raw['provider_order_id'], other_cid))
    sql("UPDATE payment_attempts SET state='ORDER_READY',provider_order_id='other-order' WHERE checkout_id=?", (other_cid,))
    with pytest.raises(sqlite3.IntegrityError):
        sql("UPDATE payment_attempts SET state='VERIFIED',provider_payment_id=?,verified_at=datetime('now') WHERE checkout_id=?",
            (first_raw['provider_payment_id'], other_cid))


@pytest.mark.parametrize('corruption', ['marker_only', 'missing_marker', 'wrong_paise'])
def test_inconsistent_reservation_fails_closed(payment, corruption):
    sid, cid, provider, service = payment
    if corruption == 'marker_only':
        sql("UPDATE checkouts SET payment_started_at=datetime('now')")
    else:
        reserve(payment)
        if corruption == 'missing_marker':
            sql('DROP TRIGGER checkout_payment_sticky')
            sql('UPDATE checkouts SET payment_started_at=NULL')
        else:
            sql('DROP TRIGGER checkout_paise_once')
            sql('UPDATE checkouts SET amount_paise=1')
    before = state()
    for operation in (lambda: reserve(payment), lambda: checkout.cancel_checkout(sid, cid, 'owner'),
                      lambda: billing.complete_session(sid), database.initialize_database):
        with pytest.raises(checkout.CheckoutError):
            operation()
        assert state() == before


def client_for(payment, *, owner='owner', enabled=True):
    def token(value):
        if value != 'valid':
            raise ValueError('secret credential')
        return {'uid': owner}
    return create_app(token_verifier=token,
                      payment_service=payment[3] if enabled else None).test_client()


def url(payment):
    return f'/sessions/{payment[0]}/checkouts/{payment[1]}/payment'


HEADERS = {'Authorization': 'Bearer valid', 'Idempotency-Key': 'payment-key'}


@pytest.mark.parametrize('body', [None, [], {'amount_paise': 1}, {'customer_uid': 'owner'},
                                   {'success': True}, {'verified': True}])
def test_api_rejects_untrusted_body(payment, body):
    response = client_for(payment).post(url(payment), json=body, headers=HEADERS)
    assert response.status_code == 400
    assert sql('SELECT COUNT(*) FROM payment_attempts') == [(0,)]


@pytest.mark.parametrize('headers,status', [({}, 401), ({'Authorization': 'Bearer wrong'}, 403),
    ({'Authorization': 'Bearer valid'}, 400)])
def test_api_auth_and_key(payment, headers, status):
    assert client_for(payment).post(url(payment), json={}, headers=headers).status_code == status


def test_api_owner_and_disabled_default(payment):
    assert client_for(payment, owner='other').post(url(payment), json={}, headers=HEADERS).status_code == 403
    assert client_for(payment, enabled=False).post(url(payment), json={}, headers=HEADERS).status_code == 503
    assert not payment[2].ledger.orders


def test_api_safe_status_evidence_and_completion(payment):
    client = client_for(payment)
    response = client.post(url(payment), json={}, headers=HEADERS)
    assert response.status_code == 200
    assert set(response.json) == {'payment_attempt_id', 'checkout_id', 'amount_paise', 'currency',
        'state', 'provider_order_id', 'created_at', 'verified_at', 'completed_at', 'error_code'}
    assert client.get(url(payment), headers=HEADERS).json == response.json
    assert client_for(payment, owner='other').get(url(payment), headers=HEADERS).status_code == 403
    assert client.post(url(payment) + '/evidence', json={'success': True}, headers=HEADERS).status_code == 400
    paid = payment[2].record_payment(response.json['provider_order_id'])
    result = client.post(url(payment) + '/evidence', json=payment[2].evidence(paid.payment_id), headers=HEADERS)
    assert result.status_code == 200 and result.json['state'] == 'COMPLETED'
    assert 'signature' not in result.json and 'provider_scope' not in result.json


def test_api_ambiguity_and_safe_errors(payment, monkeypatch):
    payment[2].creation = 'timeout'
    client = client_for(payment)
    response = client.post(url(payment), json={}, headers=HEADERS)
    assert response.json['state'] == 'CREATING' and response.json['error_code'] == 'recovery_required'
    def fail(*args):
        raise sqlite3.OperationalError('secret database details')
    monkeypatch.setattr(payment[3], 'status', fail)
    error = client.get(url(payment), headers=HEADERS)
    assert error.status_code == 503 and 'secret' not in error.get_data(as_text=True)
