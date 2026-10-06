"""Offline payment authority tests using real, independent SQLite connections."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from threading import Barrier, Event
import socket
import sqlite3

import pytest

from edge.billing import billing, checkout
from edge.database import database
from edge.payment import store
from edge.payment.fake import FakeProvider
from edge.payment.money import PaymentError
from edge.payment.provider import InvalidEvidence
from edge.payment.service import PaymentService
from edge.pairing.session_store import pair_and_assign_in_transaction, IntegrityConflict
from edge.firebase.reconcile import reconcile_session_to_firebase
from tests.test_checkout import isolated, populated, sql, state
from tests.test_session_reconciliation import StatefulReference, cloud_session, expected


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Payment tests must not contact any network service')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


@pytest.fixture
def payment(populated):
    sid, pid = populated
    frozen = checkout.begin_checkout(sid, 'owner', 'checkout-key')
    provider = FakeProvider()
    return sid, frozen.checkout_id, provider, PaymentService(provider)


def reserve(payment, key='payment-key'):
    sid, cid, provider, service = payment
    return store.begin_payment(sid, cid, 'owner', key, provider_scope=provider.scope)


def start(payment):
    sid, cid, provider, service = payment
    return service.start(sid, cid, 'owner', 'payment-key')


def verify(payment):
    sid, cid, provider, service = payment
    attempt = start(payment)
    paid = provider.record_payment(attempt['provider_order_id'])
    return service.verify(sid, cid, 'owner', provider.evidence(paid.payment_id))


def test_reservation_atomic_and_idempotent(payment):
    sid, cid, provider, service = payment
    first = reserve(payment)
    before = state()
    assert reserve(payment) == first
    assert state() == before
    assert sql('SELECT amount,amount_paise,payment_started_at FROM checkouts') == [(20, 2000, first['created_at'])]
    assert first['state'] == 'RESERVED' and first['request_key'] == 'payment-key'
    with pytest.raises(PaymentError, match='already'):
        reserve(payment, 'different')
    assert state() == before


def test_checkout_key_is_independent(payment):
    attempt = reserve(payment, 'checkout-key')
    assert attempt['request_key'] == 'checkout-key'  # Same text, separate namespace.
    assert checkout.begin_checkout(payment[0], 'owner', 'checkout-key').checkout_id == payment[1]


@pytest.mark.parametrize('which', ['owner', 'session', 'checkout', 'completed', 'cancelled', 'stale'])
def test_reservation_rejects_ineligible(payment, which):
    sid, cid, provider, service = payment
    uid = 'owner'
    if which == 'owner':
        uid = 'other'
    if which == 'session':
        sid = billing.create_session('CART-B', customer_uid='other')
        uid = 'other'
    if which == 'checkout':
        cid += 100
    if which == 'completed':
        billing.complete_session(sid)
    if which in ('cancelled', 'stale'):
        checkout.cancel_checkout(sid, cid, uid)
    if which == 'stale':
        checkout.begin_checkout(sid, uid, 'second')
    before = state()
    with pytest.raises(checkout.CheckoutError):
        store.begin_payment(sid, cid, uid, 'key', provider_scope=provider.scope)
    assert state() == before and not provider.ledger.orders


@pytest.mark.parametrize('amount', [0, 20 + 1e-10])
def test_strict_conversion_preserves_frozen_history(populated, amount):
    sid, _ = populated
    if amount == 0:
        sql('UPDATE bill_items SET price=0,total=0')
    sql('UPDATE sessions SET total_amount=?', (amount,))
    frozen = checkout.begin_checkout(sid, 'owner', 'quote')
    before = state()
    with pytest.raises(PaymentError):
        store.begin_payment(sid, frozen.checkout_id, 'owner', 'pay', provider_scope=FakeProvider.scope)
    assert state() == before


def test_reservation_rollback_if_insert_fails(payment):
    sql("CREATE TRIGGER reject_payment BEFORE INSERT ON payment_attempts BEGIN SELECT RAISE(ABORT,'injected'); END")
    before = state()
    with pytest.raises(sqlite3.IntegrityError):
        reserve(payment)
    assert state() == before


@pytest.mark.parametrize('creation,state_name,error', [('success', 'ORDER_READY', None),
    ('reject', 'FAILED', 'provider_rejected'), ('timeout', 'CREATING', 'recovery_required'),
    ('lost_response', 'CREATING', 'recovery_required')])
def test_creation_outcomes_frozen_and_sticky(payment, creation, state_name, error):
    sid, cid, provider, service = payment
    provider.creation = creation
    attempt = start(payment)
    assert (attempt['state'], attempt['error_code']) == (state_name, error)
    assert billing.get_session_meta(sid)['end_time'] is None
    with pytest.raises(checkout.CheckoutError):
        checkout.cancel_checkout(sid, cid, 'owner')
    with pytest.raises(PaymentError):
        billing.complete_session(sid)
    with pytest.raises(billing.CheckoutFrozenError):
        billing.add_runtime_item(sid, 1, expected_generation=0)
    assert sql('SELECT state FROM checkouts') == [('FROZEN',)]


@pytest.mark.parametrize('creation,safe_recover,expected_state', [('lost_response', False, 'ORDER_READY'),
    ('timeout', False, 'CREATING'), ('timeout', True, 'ORDER_READY')])
def test_restart_recovery_targets_same_attempt(payment, creation, safe_recover, expected_state):
    sid, cid, provider, service = payment
    provider.creation = creation
    first = start(payment)
    database.initialize_database()
    restarted = FakeProvider(provider.ledger, recover_missing=safe_recover)
    second = PaymentService(restarted).start(sid, cid, 'owner', 'payment-key')
    assert second['payment_attempt_id'] == first['payment_attempt_id']
    assert second['state'] == expected_state
    assert provider.create_calls == 1 and restarted.create_calls == 0
    assert len(provider.ledger.orders) <= 1
    assert sql('SELECT COUNT(*) FROM payment_attempts') == [(1,)]


def test_crash_before_and_after_claim(payment):
    sid, cid, provider, service = payment
    first = reserve(payment)
    assert not provider.ledger.orders
    claimed, won = store.claim_dispatch(first['payment_attempt_id'])
    assert won and claimed['state'] == 'CREATING'
    assert start(payment)['state'] == 'CREATING'
    assert provider.create_calls == 0
    provider.recover_missing = True
    assert start(payment)['state'] == 'ORDER_READY'


def test_provider_response_db_failure_recovers(payment, monkeypatch):
    original = store.save_order
    def fail(*args):
        raise sqlite3.OperationalError('locked')
    monkeypatch.setattr(store, 'save_order', fail)
    with pytest.raises(sqlite3.OperationalError):
        start(payment)
    assert sql('SELECT state FROM payment_attempts') == [('CREATING',)]
    monkeypatch.setattr(store, 'save_order', original)
    assert start(payment)['state'] == 'ORDER_READY'
    assert payment[2].create_calls == 1


def test_dispatch_claim_and_network_outside_lock(payment, monkeypatch):
    sid, cid, provider, service = payment
    entered, release = Event(), Event()
    original = provider.create_order
    def held(request):
        with closing(database.get_connection()) as conn:
            conn.execute('PRAGMA busy_timeout=0')
            conn.execute('BEGIN EXCLUSIVE')
            conn.rollback()
        entered.set()
        assert release.wait(5)
        return original(request)
    monkeypatch.setattr(provider, 'create_order', held)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(start, payment)
        assert entered.wait(5)
        try:
            second = pool.submit(start, payment).result(5)
            assert second['state'] == 'CREATING'
        finally:
            release.set()
        assert first.result(5)['state'] == 'ORDER_READY'
    assert provider.create_calls == 1 and len(provider.ledger.orders) == 1


@pytest.mark.parametrize('winner', ['cancel', 'payment'])
def test_cancel_reservation_serialization(payment, monkeypatch, winner):
    sid, cid, provider, service = payment
    held, release, loser_started = Event(), Event(), Event()
    # Both operations read under a real writer transaction before this hook.
    original_history = checkout.checkout_history
    def hold_history(*args):
        rows = original_history(*args)
        if not held.is_set():
            held.set()
            assert release.wait(5)
        return rows
    monkeypatch.setattr(checkout, 'checkout_history', hold_history)
    def cancel():
        return checkout.cancel_checkout(sid, cid, 'owner')
    winning, losing = (cancel, lambda: reserve(payment)) if winner == 'cancel' else (lambda: reserve(payment), cancel)
    def loser():
        loser_started.set()
        return losing()
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(winning)
        assert held.wait(5)
        second = pool.submit(loser)
        assert loser_started.wait(5)
        release.set()
        first.result(5)
        with pytest.raises(checkout.CheckoutError):
            second.result(5)
    assert sql('SELECT state FROM checkouts') == [('CANCELLED' if winner == 'cancel' else 'FROZEN',)]
    assert sql('SELECT COUNT(*) FROM payment_attempts') == [(0 if winner == 'cancel' else 1,)]


@pytest.mark.parametrize('field,value', [('provider_scope', 'other:test:account'), ('order_id', 'other-order'),
    ('payment_attempt_id', 'other-attempt'), ('checkout_id', 999), ('amount_paise', 1),
    ('amount_paise', True), ('currency', 'USD'), ('payment_id', 'substituted'), ('status', 'FAILED')])
def test_independent_verification_rejects_mismatches(payment, monkeypatch, field, value):
    sid, cid, provider, service = payment
    attempt = start(payment)
    paid = provider.record_payment(attempt['provider_order_id'])
    evidence = provider.evidence(paid.payment_id)
    monkeypatch.setattr(provider, 'fetch_payment', lambda _: replace(paid, **{field: value}))
    before = state()
    with pytest.raises(PaymentError):
        service.verify(sid, cid, 'owner', evidence)
    assert state() == before


@pytest.mark.parametrize('evidence', [{'verified': True}, {'success': True}, {},
    {'payment_id': 'unknown', 'signature': 'forged'}])
def test_client_cannot_authorize_payment(payment, evidence):
    start(payment)
    before = state()
    with pytest.raises(PaymentError):
        payment[3].verify(payment[0], payment[1], 'owner', evidence)
    assert state() == before


@pytest.mark.parametrize('extra_item', [False, True])
def test_stale_equal_or_changed_amount_evidence_never_completes_current(payment, populated, extra_item):
    sid, old_cid, provider, service = payment
    checkout.cancel_checkout(sid, old_cid, 'owner')
    if extra_item:
        billing.add_item(sid, populated[1])
    current = checkout.begin_checkout(sid, 'owner', 'new')
    attempt = service.start(sid, current.checkout_id, 'owner', 'new-payment')
    paid = provider.record_payment(attempt['provider_order_id'])
    provider.ledger.payments[paid.payment_id] = replace(paid, checkout_id=old_cid)
    with pytest.raises(PaymentError):
        service.submit_evidence(sid, current.checkout_id, 'owner', provider.evidence(paid.payment_id))
    with pytest.raises(PaymentError):
        service.submit_evidence(sid, old_cid, 'owner', provider.evidence(paid.payment_id))
    assert billing.get_session_meta(sid)['end_time'] is None


def test_verification_completion_and_lost_response_idempotency(payment):
    sid, cid, provider, service = payment
    verified = verify(payment)
    assert verified['state'] == 'VERIFIED'
    assert billing.get_session_meta(sid)['end_time'] is None
    first = service.complete(cid, verified['payment_attempt_id'])
    before = state()
    assert service.complete(cid, verified['payment_attempt_id']) == first
    assert state() == before
    assert start(payment)['state'] == 'COMPLETED'
    assert sql('SELECT state FROM checkouts') == [('COMPLETED',)]
    assert sql('SELECT total_amount,end_time FROM sessions') == [(20, first['completed_at'])]
    with pytest.raises(PaymentError):
        service.complete(cid + 1, verified['payment_attempt_id'])


def test_duplicate_evidence_and_different_payment_conflict(payment):
    sid, cid, provider, service = payment
    verified = verify(payment)
    first_paid = next(iter(provider.ledger.payments.values()))
    assert service.verify(sid, cid, 'owner', provider.evidence(first_paid.payment_id)) == verified
    service.complete(cid, verified['payment_attempt_id'])
    before = state()
    assert service.submit_evidence(sid, cid, 'owner', provider.evidence(first_paid.payment_id))['state'] == 'COMPLETED'
    assert state() == before
    second = provider.record_payment(first_paid.order_id, suffix='2')
    with pytest.raises(PaymentError):
        service.submit_evidence(sid, cid, 'owner', provider.evidence(second.payment_id))
    assert state() == before


def test_completion_rollback_and_recovery(payment):
    verified = verify(payment)
    sql("CREATE TRIGGER fail_end BEFORE UPDATE OF end_time ON sessions BEGIN SELECT RAISE(ABORT,'injected'); END")
    before = state()
    with pytest.raises(PaymentError):
        payment[3].complete(payment[1], verified['payment_attempt_id'])
    assert state() == before
    sql('DROP TRIGGER fail_end')
    assert payment[3].complete(payment[1], verified['payment_attempt_id'])['state'] == 'COMPLETED'


def test_corrupt_bill_blocks_verified_completion(payment):
    verified = verify(payment)
    sql('UPDATE bill_items SET total=999')
    before = state()
    with pytest.raises(checkout.CheckoutError):
        payment[3].complete(payment[1], verified['payment_attempt_id'])
    assert state() == before


def test_concurrent_paid_completion_and_generic_bypass(payment):
    verified = verify(payment)
    gate = Barrier(2)
    def finish():
        gate.wait(5)
        return store.complete_paid_checkout(payment[1], verified['payment_attempt_id'])
    with pytest.raises(PaymentError):
        billing.complete_session(payment[0])
    with ThreadPoolExecutor(2) as pool:
        results = [pool.submit(finish) for _ in range(2)]
        assert sorted(f.result(5)[1] for f in results) == [False, True]


def test_firebase_failure_and_controlled_repair_no_recompletion(payment, monkeypatch):
    sid, cid, provider, service = payment
    root = StatefulReference()
    root.failure = 'before'
    calls = []
    def sync(sid):
        calls.append(sid)
        assert sql('SELECT state FROM payment_attempts') == [('COMPLETED',)]
        reconcile_session_to_firebase(sid, _ref=root)
    service.completion_sync = sync
    verified = verify(payment)
    result = service.complete(cid, verified['payment_attempt_id'])
    assert result['projection_sync'] == 'reconciliation_required'
    before = state()
    service.complete(cid, verified['payment_attempt_id'])
    assert calls == [sid]
    monkeypatch.setattr(store, 'complete_paid_checkout', lambda *a: pytest.fail('recompletion'))
    root.failure = None
    reconcile_session_to_firebase(sid, _ref=root)
    assert cloud_session(root, sid) == expected(sid)
    assert state() == before


def test_pairing_release_only_after_commit(payment):
    sid, cid, provider, service = payment
    for operation in (lambda: reserve(payment), lambda: start(payment), lambda: verify(payment)):
        attempt = operation()
        with pytest.raises(IntegrityConflict):
            pair_and_assign_in_transaction('CART-A', 'next-owner')
    service.complete(cid, attempt['payment_attempt_id'])
    paired = pair_and_assign_in_transaction('CART-A', 'next-owner')
    assert paired.session_id != sid
    assert billing.get_session_meta(paired.session_id)['end_time'] is None


def test_fake_webhook_authentication_contract():
    provider = FakeProvider()
    body = b'{"event_id":"event-1","payment_id":"payment-1"}'
    assert provider.verify_webhook(body, provider._signature(body)) == ('event-1', 'payment-1')
    with pytest.raises(InvalidEvidence):
        provider.verify_webhook(body, 'forged')


def test_verification_network_outside_transaction(payment, monkeypatch):
    sid, cid, provider, service = payment
    attempt = start(payment)
    paid = provider.record_payment(attempt['provider_order_id'])
    evidence = provider.evidence(paid.payment_id)
    original = provider.fetch_payment
    def fetch(payment_id):
        with closing(database.get_connection()) as conn:
            conn.execute('PRAGMA busy_timeout=0')
            conn.execute('BEGIN EXCLUSIVE')
            conn.rollback()
        return original(payment_id)
    monkeypatch.setattr(provider, 'fetch_payment', fetch)
    assert service.verify(sid, cid, 'owner', evidence)['state'] == 'VERIFIED'


def test_verification_race_duplicate_evidence(payment):
    sid, cid, provider, service = payment
    attempt = start(payment)
    paid = provider.record_payment(attempt['provider_order_id'])
    evidence = provider.evidence(paid.payment_id)
    barrier = Barrier(2)
    def submit():
        barrier.wait(5)
        return service.submit_evidence(sid, cid, 'owner', evidence)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        assert all(f.result(5)['state'] == 'COMPLETED' for f in futures)
    assert sql('SELECT COUNT(*) FROM payment_attempts') == [(1,)]


def test_runtime_generation_and_remove_stay_frozen(payment, populated):
    sid, cid, provider, service = payment
    from tests.test_cart_runtime import runtime, acquire, dispatch_next
    checkout.cancel_checkout(sid, cid, 'owner')
    old = runtime()
    frozen = checkout.begin_checkout(sid, 'owner', 'new-generation')
    provider.creation = 'timeout'
    service.start(sid, frozen.checkout_id, 'owner', 'new-pay')
    acquire(old, -75)
    assert dispatch_next(old).status == 'rejected'
    assert old.session._checkout_generation == cid
    with pytest.raises(billing.CheckoutFrozenError):
        billing.remove_runtime_item(sid, populated[1], expected_generation=cid)
    assert billing.calculate_total(sid) == 20
    assert sql('SELECT MAX(checkout_id) FROM checkouts') == [(frozen.checkout_id,)]


def test_db_locked_before_reservation_no_provider_call(payment, monkeypatch):
    original = checkout.get_connection
    def impatient():
        conn = original()
        conn.execute('PRAGMA busy_timeout=0')
        return conn
    monkeypatch.setattr(checkout, 'get_connection', impatient)
    with closing(database.get_connection()) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError):
            start(payment)
        blocker.rollback()
    assert not payment[2].ledger.orders
    assert sql('SELECT COUNT(*) FROM payment_attempts') == [(0,)]
    assert start(payment)['state'] == 'ORDER_READY'


def test_generic_completion_marker_alone_rejected(payment):
    sql("UPDATE checkouts SET payment_started_at=datetime('now')")
    with pytest.raises(PaymentError):
        billing.complete_session(payment[0])


def test_unverified_or_different_attempt_cannot_complete(payment):
    attempt = start(payment)
    with pytest.raises(PaymentError):
        store.complete_paid_checkout(payment[1], attempt['payment_attempt_id'])
    with pytest.raises(PaymentError):
        store.complete_paid_checkout(payment[1], 'unknown')
    assert billing.get_session_meta(payment[0])['end_time'] is None
